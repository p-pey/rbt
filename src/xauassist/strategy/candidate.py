"""A detected setup, before it becomes a Signal."""

from __future__ import annotations

from dataclasses import dataclass

from xauassist.domain.enums import Direction, Pattern, Scenario


@dataclass(frozen=True, slots=True)
class Candidate:
    direction: Direction
    scenario: Scenario
    patterns: tuple[Pattern, ...]
    pullback_bars: int | None = None  # Scenario B: candles between the breakout and confirmation
