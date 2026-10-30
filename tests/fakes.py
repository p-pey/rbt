"""Test doubles for the data layer: a fake `MetaTrader5` module and a scriptable provider."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from xauassist.data.errors import ConnectionFailedError, DataUnavailableError
from xauassist.data.provider import ConnectionInfo, ConnectionStatus
from xauassist.data.replay_provider import ReplayProvider
from xauassist.domain.candle import Candle
from xauassist.domain.enums import Timeframe

TIMEFRAME_M5 = 5
TIMEFRAME_M15 = 15


class FakeMt5Api:
    """Mimics the parts of the MetaTrader5 module the provider uses."""

    TIMEFRAME_M5 = TIMEFRAME_M5
    TIMEFRAME_M15 = TIMEFRAME_M15

    def __init__(self) -> None:
        self.init_ok = True
        self.error = (1, "Success")
        self.connected = True
        self.symbol_names = ["XAUUSD", "XAUUSDm", "EURUSD"]
        self.selectable = True
        self.rates: Any = []
        self.tick_time: int | None = None
        self.spread = 25
        self.tick_spread_points: float | None = None  # ask-bid override, independent of `spread`
        self.raise_on_rates = False
        self.calls: list[tuple[Any, ...]] = []
        self.initialize_args: tuple[Any, ...] = ()
        self.shutdown_called = False

    def initialize(self, *args: Any, **kwargs: Any) -> bool:
        self.initialize_args = args
        if not self.init_ok:
            self.error = (-10003, "IPC initialize failed, MetaTrader 5 x64 not found")
        return self.init_ok

    def shutdown(self) -> None:
        self.shutdown_called = True

    def last_error(self) -> tuple[int, str]:
        return self.error

    def terminal_info(self) -> Any:
        return SimpleNamespace(connected=self.connected)

    def symbols_get(self, *args: Any, **kwargs: Any) -> Any:
        return tuple(SimpleNamespace(name=n) for n in self.symbol_names)

    def symbol_select(self, symbol: str, enable: bool = True) -> bool:
        self.calls.append(("select", symbol, enable))
        return self.selectable

    def symbol_info(self, symbol: str) -> Any:
        return SimpleNamespace(spread=self.spread, point=0.01)

    def symbol_info_tick(self, symbol: str) -> Any:
        if self.tick_time is None:
            return None
        points = self.spread if self.tick_spread_points is None else self.tick_spread_points
        return SimpleNamespace(time=self.tick_time, bid=2700.00, ask=2700.00 + points * 0.01)

    def copy_rates_from_pos(self, symbol: str, timeframe: int, start_pos: int, count: int) -> Any:
        self.calls.append(("rates", symbol, timeframe, start_pos, count))
        if self.raise_on_rates:
            raise OSError("IPC pipe closed")
        return self.rates


def mt5_rows(candles: list[Candle], server_offset_s: int = 0) -> list[dict[str, Any]]:
    """Candles as MT5 rate rows (server-time epoch seconds)."""
    return [
        {
            "time": int(c.time_open.timestamp()) + server_offset_s,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "tick_volume": c.tick_volume,
            "spread": int(c.spread_points),
            "real_volume": 0,
        }
        for c in candles
    ]


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FlakyProvider(ReplayProvider):
    """ReplayProvider whose failures can be scripted from a test."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.connect_status = ConnectionStatus.CONNECTED
        self.connect_message = ""
        self.connect_calls = 0
        self.fail_next_fetches = 0
        self.fail_with: type[Exception] = ConnectionFailedError
        self.lost_connection = False
        self.tick_age: float | None = 0.0
        self.hide: set[datetime] = set()  # candle open times temporarily missing from history

    def connect(self) -> ConnectionInfo:
        self.connect_calls += 1
        if self.connect_status is ConnectionStatus.CONNECTED:
            self.lost_connection = False
            return super().connect()
        return ConnectionInfo(self.connect_status, self.connect_message)

    def is_connected(self) -> bool:
        return not self.lost_connection and super().is_connected()

    def get_closed_candles(self, symbol: str, timeframe: Timeframe, count: int) -> list[Candle]:
        if self.fail_next_fetches > 0:
            self.fail_next_fetches -= 1
            raise self.fail_with("scripted failure")
        everything = [c for c in super().get_closed_candles(symbol, timeframe, 10**9)]
        visible = [c for c in everything if c.time_open not in self.hide]
        if not visible:
            raise DataUnavailableError("nothing visible")
        return visible[-count:]

    def last_tick_age_seconds(self, symbol: str) -> float | None:
        return self.tick_age


def utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)
