"""Provider that replays a fixed candle list, revealing it progressively.

Used for tests, demo mode and offline development (e.g. on non-Windows machines).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from xauassist.data.csv_loader import load_candles_csv
from xauassist.data.errors import DataUnavailableError, SymbolNotFoundError
from xauassist.data.provider import ConnectionInfo, ConnectionStatus
from xauassist.domain.candle import Candle
from xauassist.domain.enums import Timeframe


class ReplayProvider:
    def __init__(
        self,
        candles: Sequence[Candle],
        *,
        symbol: str = "XAUUSD",
        visible: int | None = None,
        spread_points: float = 20.0,
    ) -> None:
        self._candles = list(candles)
        self._symbol = symbol
        self._visible = len(self._candles) if visible is None else visible
        self._spread = spread_points
        self._connected = False

    @classmethod
    def from_csv(
        cls, path: Path | str, *, utc_offset_hours: float = 0.0, **kwargs: float
    ) -> ReplayProvider:
        return cls(load_candles_csv(path, utc_offset_hours=utc_offset_hours), **kwargs)  # type: ignore[arg-type]

    # ---- replay control ----

    @property
    def visible(self) -> int:
        return self._visible

    def reveal(self, count: int = 1) -> None:
        """Make `count` more candles visible (they are 'closed' as of now)."""
        self._visible = min(len(self._candles), self._visible + count)

    # ---- MarketDataProvider ----

    def connect(self) -> ConnectionInfo:
        self._connected = True
        return ConnectionInfo(ConnectionStatus.CONNECTED, "replay")

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def resolve_symbol(self, pattern: str) -> str | None:
        return self._symbol if re.fullmatch(pattern, self._symbol) else None

    def get_closed_candles(self, symbol: str, timeframe: Timeframe, count: int) -> list[Candle]:
        if symbol != self._symbol:
            raise SymbolNotFoundError(symbol)
        window = self._candles[: self._visible][-count:]
        if not window:
            raise DataUnavailableError("no candles visible yet")
        return window

    def get_spread_points(self, symbol: str) -> float:
        return self._spread

    def last_tick_age_seconds(self, symbol: str) -> float | None:
        return 0.0
