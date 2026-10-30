"""AppController: the single, Qt-free object the GUI talks to.

It owns the object graph (provider, feed, engine, pipeline, notifiers, monitor), start/stop,
settings changes and credentials. Widgets call it and render what it returns; they contain no
business logic.

Lifecycle: `start()` builds a fresh graph (new provider, feed, engine) every time, and `stop()`
tears the monitor down and disconnects. Notifiers outlive start/stop cycles so queued Telegram
alerts are never lost when the user toggles monitoring or edits settings.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, tzinfo
from pathlib import Path

from xauassist.app.logging_setup import register_secret
from xauassist.app.monitor_service import MonitorService, MonitorSnapshot
from xauassist.app.paths import AppPaths
from xauassist.app.pipeline import SignalPipeline, SignalRecord
from xauassist.config.models import AppConfig, ConfigError
from xauassist.config.secrets import (
    SecretStore,
    TelegramSecrets,
    validate_telegram_credentials,
)
from xauassist.config.writer import save_config
from xauassist.data.feed import DataFeed
from xauassist.data.provider import MarketDataProvider
from xauassist.domain.enums import Direction
from xauassist.notify.base import NotificationDispatcher
from xauassist.notify.message import format_signal_message, resolve_timezone
from xauassist.notify.sound import SoundNotifier, SoundPlayer, create_default_player
from xauassist.notify.telegram import (
    HttpTransport,
    SendResult,
    TelegramClient,
    TelegramNotifier,
    TelegramState,
    UrllibTransport,
)
from xauassist.signals.dedup_store import DedupStore, SqliteDedupStore
from xauassist.signals.manager import SignalManager
from xauassist.strategy.engine import StrategyEngine
from xauassist.validation.validator import SignalValidator

log = logging.getLogger(__name__)

ProviderFactory = Callable[[AppConfig], MarketDataProvider]


class AppController:
    def __init__(
        self,
        paths: AppPaths,
        config: AppConfig,
        *,
        provider_factory: ProviderFactory,
        secret_store: SecretStore,
        transport: HttpTransport | None = None,
        player: SoundPlayer | None = None,
        sound_runner: Callable[[Callable[[], None]], None] | None = None,
        dedup_store: DedupStore | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._paths = paths
        self._config = config
        self._provider_factory = provider_factory
        self._secrets = secret_store
        self._transport = transport or UrllibTransport()
        self._clock = clock
        self._lock = threading.RLock()
        self._tz = resolve_timezone(config.display.timezone)
        self._store: DedupStore = dedup_store or self._open_store()

        self._records: deque[SignalRecord] = deque(maxlen=config.monitor.history_size)
        self._snapshot_listeners: list[Callable[[MonitorSnapshot], None]] = []
        self._signal_listeners: list[Callable[[SignalRecord], None]] = []
        self._monitor: MonitorService | None = None
        self._provider: MarketDataProvider | None = None
        self._last_snapshot: MonitorSnapshot | None = None

        saved = self._secrets.load()
        if saved is not None:
            register_secret(saved.bot_token)
            register_secret(saved.chat_id)
        self._telegram = TelegramNotifier(
            self._make_client(saved),
            config.telegram,
            lambda signal: format_signal_message(signal, self._tz),
        )
        sound_kwargs = {"runner": sound_runner} if sound_runner is not None else {}
        self._sound = SoundNotifier(
            config.sound, paths.app_dir, player or create_default_player(), **sound_kwargs
        )

    # ---- read access ----

    @property
    def config(self) -> AppConfig:
        return self._config

    @property
    def paths(self) -> AppPaths:
        return self._paths

    @property
    def display_tz(self) -> tzinfo:
        return self._tz

    @property
    def is_running(self) -> bool:
        return self._monitor is not None and self._monitor.is_running

    @property
    def records(self) -> tuple[SignalRecord, ...]:
        with self._lock:
            return tuple(self._records)

    def snapshot(self) -> MonitorSnapshot:
        monitor = self._monitor
        if monitor is not None:
            return monitor.snapshot()
        if self._last_snapshot is not None:
            return replace(self._last_snapshot, running=False)
        return MonitorSnapshot(
            running=False,
            feed_status=None,
            message="",
            symbol=None,
            timeframe=self._config.strategy.timeframe,
            latest_ma=None,
            last_candle_time=None,
            last_signal=None,
            updated_at=self._clock(),
        )

    def warnings(self) -> list[str]:
        """Conditions the user should fix (e.g. a wrong MT5 server offset)."""
        provider = self._provider
        warning = getattr(provider, "offset_warning", None)
        text = warning() if callable(warning) else None
        return [text] if text else []

    def telegram_state(self) -> TelegramState:
        return self._telegram.state()

    def has_telegram_credentials(self) -> bool:
        saved = self._secrets.load()
        return saved is not None and saved.is_complete

    def saved_chat_id(self) -> str:
        saved = self._secrets.load()
        return saved.chat_id if saved else ""

    # ---- subscriptions (survive restarts) ----

    def add_snapshot_listener(self, listener: Callable[[MonitorSnapshot], None]) -> None:
        self._snapshot_listeners.append(listener)

    def add_signal_listener(self, listener: Callable[[SignalRecord], None]) -> None:
        self._signal_listeners.append(listener)

    # ---- monitoring ----

    def start(self) -> None:
        with self._lock:
            if self.is_running:
                return
            self._monitor = self._build_monitor()
            self._monitor.start()

    def stop(self) -> None:
        with self._lock:
            monitor, provider = self._monitor, self._provider
            if monitor is None:
                return
            monitor.stop()
            self._last_snapshot = monitor.snapshot()
            self._monitor = None
        if provider is not None:
            try:
                provider.disconnect()
            except Exception:
                log.exception("Error while disconnecting provider")

    def shutdown(self) -> None:
        self.stop()
        self._telegram.stop()

    # ---- settings ----

    def apply_config(self, new: AppConfig, *, persist: bool = True) -> None:
        """Apply (and save) a new configuration.

        Telegram/sound/display changes take effect immediately. Anything that changes the
        strategy, data feed or validation restarts monitoring (history is re-fetched; warm-up
        candles never alert).
        """
        with self._lock:
            old = self._config
            live_only = replace(
                new,
                telegram=old.telegram,
                sound=old.sound,
                display=old.display,
                backtest=old.backtest,
            )
            needs_restart = live_only != old
            was_running = self.is_running
            self._tz = resolve_timezone(new.display.timezone)  # validate before changing anything
            if persist:
                save_config(new, self._paths.config_path)
            if needs_restart and was_running:
                self.stop()
            self._config = new
            self._records = deque(self._records, maxlen=new.monitor.history_size)
            self._sound.configure(new.sound)
            self._telegram.reconfigure(self._make_client(self._secrets.load()), new.telegram)
            if needs_restart and was_running:
                self.start()

    # ---- telegram ----

    def save_telegram_credentials(self, token: str, chat_id: str) -> None:
        problems = validate_telegram_credentials(token, chat_id)
        if problems:
            raise ConfigError(" ".join(problems))
        secrets = TelegramSecrets(token.strip(), chat_id.strip())
        self._secrets.save(secrets)
        register_secret(secrets.bot_token)
        register_secret(secrets.chat_id)
        self._telegram.reconfigure(self._make_client(secrets), self._config.telegram)

    def clear_telegram_credentials(self) -> None:
        self._secrets.clear()
        self._telegram.reconfigure(None, self._config.telegram)

    def check_telegram(self) -> TelegramState:
        """Blocking token check (call from a worker thread)."""
        return self._telegram.check_connection()

    def send_test_telegram(self) -> SendResult:
        """Blocking test message (call from a worker thread)."""
        return self._telegram.send_test_message()

    # ---- sound ----

    def test_sound(self, direction: Direction) -> None:
        self._sound.test_play(direction)

    def sound_file(self, direction: Direction) -> Path:
        return self._sound.sound_path(direction)

    # ---- internals ----

    def _open_store(self) -> DedupStore:
        self._paths.db_path.parent.mkdir(parents=True, exist_ok=True)
        return SqliteDedupStore(self._paths.db_path)

    def _make_client(self, secrets: TelegramSecrets | None) -> TelegramClient | None:
        if secrets is None or not secrets.is_complete:
            return None
        return TelegramClient(
            secrets, self._transport, timeout=self._config.telegram.timeout_seconds
        )

    def _build_monitor(self) -> MonitorService:
        cfg = self._config
        provider = self._provider_factory(cfg)
        self._provider = provider
        feed = DataFeed(
            provider,
            cfg.strategy.timeframe,
            cfg.validation.symbol_pattern,
            cfg.feed,
            clock=self._clock,
        )
        pipeline = SignalPipeline(
            lambda: StrategyEngine(cfg.strategy, feed.symbol or "XAUUSD"),
            SignalValidator(cfg.validation),
            SignalManager(self._store, cfg.signals),
            NotificationDispatcher([self._sound, self._telegram]),  # sound first: immediate
            feed.current_spread_points,
            cfg.monitor,
            clock=self._clock,
        )
        monitor = MonitorService(
            feed, pipeline, cfg.strategy.timeframe, cfg.feed.poll_seconds, clock=self._clock
        )
        monitor.add_signal_listener(self._on_record)
        for listener in self._snapshot_listeners:
            monitor.add_snapshot_listener(listener)
        return monitor

    def _on_record(self, record: SignalRecord) -> None:
        with self._lock:
            self._records.append(record)
        for listener in list(self._signal_listeners):
            try:
                listener(record)
            except Exception:
                log.exception("Signal listener failed")
