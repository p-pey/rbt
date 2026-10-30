"""Human-readable explanation for a signal."""

from __future__ import annotations

from xauassist.domain.enums import Direction, Scenario
from xauassist.strategy.candidate import Candidate


def build_reason(candidate: Candidate, ma_label: str) -> str:
    sell = candidate.direction is Direction.SELL
    trend_side, break_side = ("below", "above") if sell else ("above", "below")
    patterns = " and a ".join(p.value.lower() for p in candidate.patterns)
    if candidate.scenario is Scenario.A:
        return (
            f"Price was {trend_side} {ma_label}, retested the moving average, "
            f"and produced a {patterns} candle."
        )
    return (
        f"Price was {trend_side} {ma_label}, broke {break_side} it, pulled back to the moving "
        f"average {candidate.pullback_bars} candle(s) later, and produced a {patterns} candle."
    )
