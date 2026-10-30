import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from helpers import make_config, scenario_a_sell
from test_notify_telegram import OK, SECRETS, TOKEN, FakeTransport, err
from xauassist.app.controller import AppController
from xauassist.app.paths import AppPaths
from xauassist.app.pipeline import Outcome, SignalRecord
from xauassist.app.replay_mode import ReplaySession
from xauassist.config.loader import load_config
from xauassist.config.models import (
    AppConfig,
    ConfigError,
    DisplayConfig,
    FeedConfig,
    SignalsConfig,
    SoundConfig,
    TelegramConfig,
)
from xauassist.config.secrets import TelegramSecrets
from xauassist.data.feed import FeedStatus
from xauassist.domain.enums import Direction, Timeframe
from xauassist.notify.telegram import TelegramStatus

REPO = Path(__file__).resolve().parents[1]


class MemorySecretStore:
    def __init__(self, secrets: TelegramSecrets | None = None) -> None:
        self.secrets = secrets

    def load(self) -> TelegramSecrets | None:
        return self.secrets

    def save(self, secrets: TelegramSecrets) -> None:
        self.secrets = secrets

    def clear(self) -> None:
        self.secrets = None


class Player:
    def __init__(self) -> None:
        self.played: list[Path] = []
        self.beeps = 0

    def play_file(self, path: Path) -> None:
        self.played.append(path)

    def beep(self) -> None:
        self.beeps += 1


def wait_for(cond: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def app_config(**overrides: object) -> AppConfig:
    cfg = AppConfig(
        strategy=make_config(),
        signals=SignalsConfig(cooldown_bars=0),
        feed=FeedConfig(poll_seconds=0.01, min_history_bars=20),
        telegram=TelegramConfig(enabled=True, retry_initial_seconds=0.01, retry_max_seconds=0.02),
    )
    return replace(cfg, **overrides)  # type: ignore[arg-type]


class Rig:
    def __init__(self, tmp_path: Path, *, visible_from_end: int = 6, secrets: bool = True) -> None:
        candles = scenario_a_sell()
        self.session = ReplaySession(
            candles,
            Timeframe.M5,
            start_visible=len(candles) - visible_from_end,
            interval_seconds=0.02,
        )
        self.transport = FakeTransport(OK)
        self.player = Player()
        self.store = MemorySecretStore(SECRETS if secrets else None)
        self.paths = AppPaths(app_dir=REPO, user_dir=tmp_path)
        self.controller = AppController(
            self.paths,
            app_config(),
            provider_factory=lambda cfg: self.session.provider,
            secret_store=self.store,
            transport=self.transport,
            player=self.player,
            sound_runner=lambda job: job(),
            clock=self.session.clock,
        )

    def records(self) -> tuple[SignalRecord, ...]:
        return self.controller.records

    def close(self) -> None:
        self.session.stop()
        self.controller.shutdown()


@pytest.fixture()
def rig(tmp_path: Path):  # type: ignore[no-untyped-def]
    r = Rig(tmp_path)
    yield r
    r.close()


def test_full_stack_signal_reaches_sound_telegram_and_history(rig: Rig) -> None:
    c = rig.controller
    c.start()
    assert wait_for(lambda: c.snapshot().latest_ma is not None)  # warmed up from history
    rig.session.start()
    assert wait_for(lambda: len(rig.records()) == 1), "no signal produced"
    assert rig.controller.telegram_state() is not None
    assert wait_for(lambda: rig.controller.telegram_state().sent == 1)
    (record,) = rig.records()
    assert record.outcome is Outcome.DELIVERED and record.signal.direction is Direction.SELL
    assert rig.player.played == [REPO / "assets/sounds/sell.wav"]
    assert "GOLD SELL SIGNAL" in rig.transport.requests[0][1]["text"]
    snap = c.snapshot()
    assert snap.running and snap.symbol == "XAUUSD" and snap.last_signal == record
    assert snap.feed_status in (FeedStatus.OK, FeedStatus.NO_NEW_DATA)


def test_stop_disconnects_and_keeps_history_and_dedup_across_restart(rig: Rig) -> None:
    c = rig.controller
    c.start()
    assert wait_for(lambda: c.snapshot().latest_ma is not None)
    rig.session.start()
    assert wait_for(lambda: len(rig.records()) == 1)
    c.stop()
    assert not c.is_running and not rig.session.provider.is_connected()
    assert not c.snapshot().running and c.snapshot().symbol == "XAUUSD"
    assert len(rig.records()) == 1
    c.start()  # all candles are now history: nothing may alert again
    assert wait_for(lambda: c.snapshot().feed_status is not None)
    time.sleep(0.2)
    assert len(rig.records()) == 1 and len(rig.player.played) == 1


def test_listeners_survive_restarts(rig: Rig) -> None:
    snapshots: list[object] = []
    rig.controller.add_snapshot_listener(snapshots.append)
    rig.controller.start()
    assert wait_for(lambda: len(snapshots) > 0)
    rig.controller.stop()
    seen = len(snapshots)
    rig.controller.start()
    assert wait_for(lambda: len(snapshots) > seen)


def test_start_is_idempotent(rig: Rig) -> None:
    rig.controller.start()
    monitor = rig.controller._monitor
    rig.controller.start()
    assert rig.controller._monitor is monitor


def test_telegram_and_sound_changes_apply_live_without_restart(rig: Rig) -> None:
    c = rig.controller
    c.start()
    monitor = c._monitor
    new = replace(
        c.config,
        sound=SoundConfig(enabled=False),
        telegram=TelegramConfig(enabled=False),
        display=DisplayConfig(timezone="UTC"),
    )
    c.apply_config(new)
    assert c._monitor is monitor and c.is_running
    assert c.telegram_state().status is TelegramStatus.DISABLED
    assert str(c.display_tz) == "UTC"
    assert load_config(rig.paths.config_path) == new  # persisted


def test_strategy_change_restarts_monitoring_and_persists(rig: Rig) -> None:
    c = rig.controller
    c.start()
    monitor = c._monitor
    new = replace(c.config, validation=replace(c.config.validation, max_spread_points=5.0))
    c.apply_config(new)
    assert c.is_running and c._monitor is not monitor
    assert load_config(rig.paths.config_path).validation.max_spread_points == 5.0


def test_change_while_stopped_does_not_start_monitoring(rig: Rig) -> None:
    new = replace(
        rig.controller.config,
        validation=replace(rig.controller.config.validation, max_spread_points=9.0),
    )
    rig.controller.apply_config(new)
    assert not rig.controller.is_running and rig.controller.config == new


def test_invalid_timezone_is_rejected_before_anything_changes(rig: Rig) -> None:
    old = rig.controller.config
    with pytest.raises(ConfigError):
        rig.controller.apply_config(replace(old, display=DisplayConfig(timezone="Mars/Base")))
    assert rig.controller.config == old and not rig.paths.config_path.exists()


def test_tightened_spread_limit_rejects_the_signal_after_restart(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    try:
        c = r.controller
        c.apply_config(
            replace(c.config, validation=replace(c.config.validation, max_spread_points=5.0)),
            persist=False,
        )
        c.start()
        assert wait_for(lambda: c.snapshot().latest_ma is not None)
        r.session.start()
        assert wait_for(lambda: len(r.records()) == 1)
        assert r.records()[0].outcome is Outcome.REJECTED and r.player.played == []
    finally:
        r.close()


# ---------------- telegram credentials ----------------


def test_save_credentials_validates_stores_and_activates(tmp_path: Path) -> None:
    r = Rig(tmp_path, secrets=False)
    try:
        c = r.controller
        assert (
            c.telegram_state().status is TelegramStatus.NOT_CONFIGURED
            and not c.has_telegram_credentials()
        )
        with pytest.raises(ConfigError):
            c.save_telegram_credentials("bad", "worse")
        assert r.store.secrets is None
        c.save_telegram_credentials(f"  {TOKEN} ", " 987654321 ")
        assert r.store.secrets == TelegramSecrets(TOKEN, "987654321")
        assert c.has_telegram_credentials() and c.saved_chat_id() == "987654321"
        assert c.telegram_state().status is TelegramStatus.OK
        assert c.check_telegram().status is TelegramStatus.OK
        assert c.send_test_telegram().outcome.value == "sent"
        c.clear_telegram_credentials()
        assert (
            r.store.secrets is None and c.telegram_state().status is TelegramStatus.NOT_CONFIGURED
        )
    finally:
        r.close()


def test_bad_token_is_reported_not_raised(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    try:
        r.transport.script = [err(401, "Unauthorized")]
        state = r.controller.check_telegram()
        assert state.status is TelegramStatus.ERROR and "401" in state.detail
        assert r.controller.send_test_telegram().outcome.value == "permanent"
    finally:
        r.close()


def test_test_sound_works_even_when_disabled(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    try:
        r.controller.apply_config(
            replace(r.controller.config, sound=SoundConfig(enabled=False)), persist=False
        )
        r.controller.test_sound(Direction.BUY)
        assert r.player.played == [REPO / "assets/sounds/buy.wav"]
        assert r.controller.sound_file(Direction.SELL).name == "sell.wav"
    finally:
        r.close()


def test_warnings_come_from_the_provider(rig: Rig) -> None:
    assert rig.controller.warnings() == []
    rig.controller.start()
    rig.session.provider.offset_warning = lambda: "server offset looks wrong"  # type: ignore[attr-defined]
    assert rig.controller.warnings() == ["server offset looks wrong"]
