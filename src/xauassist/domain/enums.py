"""Shared enumerations."""

from __future__ import annotations

from enum import StrEnum


class Direction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> Direction:
        return Direction.SELL if self is Direction.BUY else Direction.BUY


class Scenario(StrEnum):
    A = "A"
    B = "B"

    @property
    def label(self) -> str:
        return "MA Retest" if self is Scenario.A else "MA Break + Pullback"


class Pattern(StrEnum):
    BULLISH_PIN_BAR = "Bullish Pin Bar"
    BEARISH_PIN_BAR = "Bearish Pin Bar"
    BULLISH_ENGULFING = "Bullish Engulfing"
    BEARISH_ENGULFING = "Bearish Engulfing"

    @property
    def direction(self) -> Direction:
        return Direction.BUY if self.value.startswith("Bullish") else Direction.SELL


class MaMethod(StrEnum):
    EMA = "EMA"
    SMA = "SMA"


class ZoneMode(StrEnum):
    PRICE = "PRICE"  # half-width in price units (e.g. 0.50 = $0.50)
    PERCENT = "PERCENT"  # half-width as a percentage of the MA value
    ATR = "ATR"  # half-width as a multiple of ATR


class SameCandleRule(StrEnum):
    """Backtest: which level counts when one candle reaches both stop and target."""

    STOP_FIRST = "STOP_FIRST"  # pessimistic (default)
    TARGET_FIRST = "TARGET_FIRST"  # optimistic; use to bracket the result


class Timeframe(StrEnum):
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"

    @property
    def minutes(self) -> int:
        return _TIMEFRAME_MINUTES[self]


_TIMEFRAME_MINUTES: dict[Timeframe, int] = {
    Timeframe.M1: 1,
    Timeframe.M5: 5,
    Timeframe.M15: 15,
    Timeframe.M30: 30,
    Timeframe.H1: 60,
    Timeframe.H4: 240,
}
