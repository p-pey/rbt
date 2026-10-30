"""Backtest data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from xauassist.domain.signal import Signal


class Segment(StrEnum):
    EXPLORATION = "exploration"
    VALIDATION = "validation"
    TEST = "test"


class ExitReason(StrEnum):
    TP = "TP"
    SL = "SL"
    TIME = "TIME"


class SkipReason(StrEnum):
    NO_NEXT_BAR = "no candle after the signal"
    GAP_BEFORE_ENTRY = "market gap before the entry candle (alert would be stale)"
    INVALID_RISK = "stop distance zero, negative or below min_risk"
    UNRESOLVED = "data ended before the trade was resolved"


@dataclass(frozen=True, slots=True)
class Trade:
    """One signal scored under the execution assumptions. Prices are in XAUUSD price units."""

    signal: Signal
    signal_index: int
    entry_index: int
    entry_time: datetime
    entry_price: float  # actual fill: SELL at bid, BUY at ask, both after slippage
    stop_loss: float
    take_profit: float
    risk: float  # entry-to-stop distance
    exit_index: int
    exit_time: datetime
    exit_price: float
    exit_reason: ExitReason
    pnl: float  # price units, positive = profit (spread and slippage already included)
    r_result: float  # pnl / risk
    mfe: float  # max favourable excursion within the horizon (independent of SL/TP)
    mae: float  # max adverse excursion within the horizon (independent of SL/TP)
    bars_held: int
    spread: float  # price units charged at entry
    assumed_spread: bool  # True if the candle had no spread and the assumption was used
    last_index_used: int  # last candle whose prices influenced this trade or its MFE/MAE

    @property
    def is_win(self) -> bool:
        return self.pnl > 0


@dataclass(frozen=True, slots=True)
class SkippedSignal:
    signal: Signal
    signal_index: int
    reason: SkipReason


@dataclass(slots=True)
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    skipped: list[SkippedSignal] = field(default_factory=list)
    raw_signals: int = 0  # engine output before validation/de-duplication
    rejected: dict[str, int] = field(default_factory=dict)  # reason -> count
    candles: int = 0
    first_time: datetime | None = None
    last_time: datetime | None = None
    unexpected_gaps: int = 0
