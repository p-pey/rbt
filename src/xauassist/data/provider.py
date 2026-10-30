"""Market-data provider interface. The strategy engine never imports anything MT5-specific."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from xauassist.domain.candle import Candle
from xauassist.domain.enums import Timeframe


class ConnectionStatus(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"  # terminal reachable but not connected to the broker
    TERMINAL_MISSING = "terminal_missing"  # MetaTrader5 package / terminal not installed
    FAILED = "failed"  # terminal could not be initialised


@dataclass(frozen=True, slots=True)
class ConnectionInfo:
    status: ConnectionStatus
    message: str = ""


class MarketDataProvider(Protocol):
    def connect(self) -> ConnectionInfo:
        """Connect (or re-connect). Must not raise for expected failures; return a status."""
        ...

    def disconnect(self) -> None: ...

    def is_connected(self) -> bool:
        """Cheap health check, called on every poll."""
        ...

    def resolve_symbol(self, pattern: str) -> str | None:
        """Broker symbol matching the regex `pattern` (e.g. XAUUSD, XAUUSD.a), or None."""
        ...

    def get_closed_candles(self, symbol: str, timeframe: Timeframe, count: int) -> list[Candle]:
        """Up to `count` most recent CLOSED candles, oldest first, times in UTC.

        Never includes the currently forming candle. Raises ProviderError subclasses.
        """
        ...

    def get_spread_points(self, symbol: str) -> float:
        """Current spread in broker points."""
        ...

    def last_tick_age_seconds(self, symbol: str) -> float | None:
        """Seconds since the last tick, or None if unknown."""
        ...
