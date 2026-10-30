"""Trading signal model."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from xauassist.domain.enums import Direction, Pattern, Scenario, Timeframe

InfoValue = float | str | None


@dataclass(frozen=True, slots=True)
class SignalKey:
    """Identity of a setup; two signals with equal keys are duplicates."""

    symbol: str
    direction: Direction
    scenario: Scenario
    bar_open_time: datetime


@dataclass(frozen=True, slots=True)
class Signal:
    direction: Direction
    scenario: Scenario
    symbol: str
    timeframe: Timeframe
    bar_open_time: datetime  # open time of the confirmation candle
    time: datetime  # close time of the confirmation candle (when the signal exists)
    entry_price: float  # reference price: close of the confirmation candle
    ma_value: float
    patterns: tuple[Pattern, ...]
    reason: str
    info: Mapping[str, InfoValue] = field(default_factory=dict)

    @property
    def key(self) -> SignalKey:
        return SignalKey(self.symbol, self.direction, self.scenario, self.bar_open_time)
