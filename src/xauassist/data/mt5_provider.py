"""MetaTrader 5 provider (Architecture A: the official `MetaTrader5` Python package).

The package talks to an already-installed MT5 terminal over IPC; it cannot work without one, and it
exists only for Windows. The module is injected (`Mt5Api`) so that:
  - a missing package/terminal is reported as a status instead of crashing, and
  - everything here is testable without MetaTrader.

Timing facts this code relies on:
  - `copy_rates_from_pos(..., start_pos=1, ...)` skips position 0, the forming candle.
  - Candle and tick times are broker *server* time expressed as epoch seconds; they are converted
    to true UTC with the CONFIGURED server offset. The offset is deliberately never changed while
    running (it would shift the whole candle timeline); if the tick clock disagrees with the
    configured value `offset_warning()` says so, e.g. after a DST change.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from xauassist.config.models import Mt5Config
from xauassist.data.errors import (
    ConnectionFailedError,
    DataUnavailableError,
    ProviderError,
    SymbolNotFoundError,
)
from xauassist.data.provider import ConnectionInfo, ConnectionStatus
from xauassist.domain.candle import Candle
from xauassist.domain.enums import Timeframe


class Mt5Api(Protocol):
    """The subset of the `MetaTrader5` module used here."""

    def initialize(self, *args: Any, **kwargs: Any) -> bool: ...
    def shutdown(self) -> None: ...
    def last_error(self) -> tuple[int, str]: ...
    def terminal_info(self) -> Any: ...
    def symbols_get(self, *args: Any, **kwargs: Any) -> Any: ...
    def symbol_select(self, symbol: str, enable: bool = ...) -> bool: ...
    def symbol_info(self, symbol: str) -> Any: ...
    def symbol_info_tick(self, symbol: str) -> Any: ...
    def copy_rates_from_pos(
        self, symbol: str, timeframe: int, start_pos: int, count: int
    ) -> Any: ...


def load_installed_api() -> Mt5Api | None:
    """The real `MetaTrader5` module, or None if it is not installed (e.g. Linux/macOS)."""
    try:
        import MetaTrader5  # type: ignore[import-not-found,unused-ignore]
    except ImportError:
        return None
    return cast(Mt5Api, MetaTrader5)


def detect_server_offset_seconds(tick_time: float, utc_now: float) -> int | None:
    """Broker-server offset from UTC in seconds, if `tick_time` is fresh enough to tell.

    A fresh tick is at most ~2 minutes old, so (tick - now) must sit within 120 s of a whole
    multiple of 30 minutes; otherwise the tick is stale and nothing can be concluded.
    """
    diff = tick_time - utc_now
    rounded = round(diff / 1800) * 1800
    return int(rounded) if abs(diff - rounded) <= 120 else None


class Mt5Provider:
    def __init__(
        self,
        config: Mt5Config,
        api: Mt5Api | None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._cfg = config
        self._api = api
        self._clock = clock
        self._offset_s = int(config.server_utc_offset_hours * 3600)
        self._observed: tuple[int, float] | None = None  # (offset, tick time) awaiting confirmation
        self._suspected_offset_s: int | None = None

    @classmethod
    def from_installed(cls, config: Mt5Config) -> Mt5Provider:
        return cls(config, load_installed_api())

    @property
    def server_offset_seconds(self) -> int:
        return self._offset_s

    def offset_warning(self) -> str | None:
        """Warning text if the tick clock confirms an offset different from the configured one."""
        if self._suspected_offset_s is None:
            return None
        return (
            f"MT5 server time looks like UTC{self._suspected_offset_s / 3600:+g}h but the config "
            f"says UTC{self._offset_s / 3600:+g}h (mt5.server_utc_offset_hours). Candle times and "
            "alert times are off until it is updated (daylight-saving change?)."
        )

    # ---- connection ----

    def connect(self) -> ConnectionInfo:
        api = self._api
        if api is None:
            return ConnectionInfo(
                ConnectionStatus.TERMINAL_MISSING,
                "MetaTrader5 Python package not available. It is Windows-only: install "
                "MetaTrader 5 and run `pip install MetaTrader5` on Windows.",
            )
        try:
            ok = (
                api.initialize(self._cfg.terminal_path)
                if self._cfg.terminal_path
                else api.initialize()
            )
            if not ok:
                code, desc = api.last_error()
                return ConnectionInfo(
                    ConnectionStatus.FAILED,
                    f"MT5 initialize() failed ({code}: {desc}). Is the MetaTrader 5 terminal "
                    "installed, running and logged in to your broker?",
                )
            if not self.is_connected():
                return ConnectionInfo(
                    ConnectionStatus.DISCONNECTED,
                    "MT5 terminal is running but not connected to the broker server.",
                )
        except Exception as exc:  # the C-extension can raise on a dead IPC pipe
            return ConnectionInfo(ConnectionStatus.FAILED, f"MT5 error during connect: {exc!r}")
        return ConnectionInfo(ConnectionStatus.CONNECTED, "connected")

    def disconnect(self) -> None:
        if self._api is not None:
            try:
                self._api.shutdown()
            except Exception:  # shutting down a dead connection must never raise
                pass

    def is_connected(self) -> bool:
        if self._api is None:
            return False
        try:
            info = self._api.terminal_info()
        except Exception:
            return False
        return info is not None and bool(getattr(info, "connected", False))

    # ---- market data ----

    def resolve_symbol(self, pattern: str) -> str | None:
        api = self._require_api()
        regex = re.compile(pattern)
        try:
            symbols = api.symbols_get() or ()
            names = [str(s.name) for s in symbols if regex.fullmatch(str(s.name))]
            # Prefer the plain name, then the shortest (XAUUSD before XAUUSD.a before XAUUSDmicro).
            for name in sorted(names, key=lambda n: (n.upper() != "XAUUSD", len(n), n)):
                if api.symbol_select(name, True):  # ensure it is in Market Watch
                    return name
        except Exception as exc:
            raise ConnectionFailedError(f"MT5 symbol lookup failed: {exc!r}") from exc
        return None

    def get_closed_candles(self, symbol: str, timeframe: Timeframe, count: int) -> list[Candle]:
        api = self._require_api()
        constant = getattr(api, f"TIMEFRAME_{timeframe.value}", None)
        if constant is None:
            raise ValueError(f"MT5 has no constant for timeframe {timeframe.value}")
        self._observe_offset(symbol)
        try:
            rates = api.copy_rates_from_pos(symbol, constant, 1, count)  # 1 = skip forming candle
        except Exception as exc:
            raise ConnectionFailedError(f"MT5 copy_rates failed: {exc!r}") from exc
        if rates is None or len(rates) == 0:
            code, desc = self._safe_last_error()
            if code == -2:  # RES_E_INVALID_PARAMS: usually an unknown symbol
                raise SymbolNotFoundError(f"{symbol}: {desc}")
            raise DataUnavailableError(f"MT5 returned no candles for {symbol} ({code}: {desc})")
        candles: dict[datetime, Candle] = {}
        for row in rates:
            opened = datetime.fromtimestamp(int(row["time"]) - self._offset_s, tz=UTC)
            candles[opened] = Candle(
                time_open=opened,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                tick_volume=int(row["tick_volume"]),
                spread_points=float(row["spread"]),
                is_closed=True,
            )
        return [candles[t] for t in sorted(candles)]

    def get_spread_points(self, symbol: str) -> float:
        api = self._require_api()
        try:
            info = api.symbol_info(symbol)
            if info is None:
                raise SymbolNotFoundError(symbol)
            if info.spread:
                return float(info.spread)
            tick = api.symbol_info_tick(symbol)
            if tick is not None and info.point:
                return float((tick.ask - tick.bid) / info.point)
        except ProviderError:
            raise
        except Exception as exc:
            raise ConnectionFailedError(f"MT5 spread lookup failed: {exc!r}") from exc
        raise DataUnavailableError(f"spread unavailable for {symbol}")

    def last_tick_age_seconds(self, symbol: str) -> float | None:
        if self._api is None:
            return None
        try:
            tick = self._api.symbol_info_tick(symbol)
        except Exception:
            return None
        if tick is None:
            return None
        server_now = self._clock().timestamp() + self._offset_s
        return max(0.0, server_now - float(tick.time))

    # ---- helpers ----

    def _require_api(self) -> Mt5Api:
        if self._api is None:
            raise ConnectionFailedError("MetaTrader5 package not available")
        return self._api

    def _safe_last_error(self) -> tuple[int, str]:
        try:
            code, desc = self._require_api().last_error()
            return int(code), str(desc)
        except Exception:
            return (0, "unknown")

    def _observe_offset(self, symbol: str) -> None:
        """Track the offset implied by fresh ticks; only a confirmed mismatch raises a warning.

        One sample is not enough: a stale tick (weekend) can look fresh at a wrong offset. The same
        offset must be seen on two *different* ticks at least 30 s apart; a stale tick repeats the
        same tick time, so it can never confirm anything.
        """
        try:
            tick = self._require_api().symbol_info_tick(symbol)
        except Exception:
            return
        if tick is None:
            return
        tick_time = float(tick.time)
        detected = detect_server_offset_seconds(tick_time, self._clock().timestamp())
        if detected is None:
            return
        if detected == self._offset_s:
            self._observed = None
            self._suspected_offset_s = None
        elif self._observed is not None and self._observed[0] == detected:
            if tick_time - self._observed[1] >= 30:
                self._suspected_offset_s = detected
        else:
            self._observed = (detected, tick_time)
