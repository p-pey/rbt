"""A closed candle together with the indicator values computed at its close."""

from __future__ import annotations

from dataclasses import dataclass

from xauassist.domain.candle import Candle


@dataclass(frozen=True, slots=True)
class Bar:
    candle: Candle
    ma: float
    atr: float | None
