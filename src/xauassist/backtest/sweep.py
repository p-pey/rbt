"""Parameter exploration that cannot touch the final test set.

Every configuration is evaluated by `run_development`, which only ever receives the exploration
and validation candles. The sweep reports both segments side by side: pick candidates on
exploration, confirm on validation, then evaluate the final test ONCE for the single chosen
configuration. Trying many configurations inflates the best in-sample score by selection alone,
which is why the number of trials is reported with every result.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from xauassist.backtest.models import Segment
from xauassist.backtest.session import run_development
from xauassist.backtest.stats import GroupStats
from xauassist.config.models import AppConfig
from xauassist.config.overrides import apply_overrides
from xauassist.domain.candle import Candle


@dataclass(frozen=True, slots=True)
class SweepRow:
    overrides: dict[str, Any]
    exploration: GroupStats
    validation: GroupStats


@dataclass(frozen=True, slots=True)
class SweepResult:
    rows: tuple[SweepRow, ...]
    n_trials: int
    warning: str

    def ranked_by_exploration(self) -> list[SweepRow]:
        return sorted(
            self.rows,
            key=lambda r: (
                r.exploration.mean_r if r.exploration.mean_r is not None else float("-inf")
            ),
            reverse=True,
        )


def run_sweep(
    candles: Sequence[Candle],
    base: AppConfig,
    grid: Mapping[str, Sequence[Any]],
    symbol: str = "XAUUSD",
) -> SweepResult:
    """Evaluate every combination of `grid` ({"strategy.zone.value": [0.2, 0.3], ...})."""
    keys = list(grid)
    rows: list[SweepRow] = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        overrides = dict(zip(keys, combo, strict=True))
        config = apply_overrides(base, {tuple(k.split(".")): v for k, v in overrides.items()})
        report = run_development(candles, config, symbol)
        by_segment = {s.segment: s.stats for s in report.segments}
        rows.append(
            SweepRow(overrides, by_segment[Segment.EXPLORATION], by_segment[Segment.VALIDATION])
        )
    n = len(rows)
    return SweepResult(
        rows=tuple(rows),
        n_trials=n,
        warning=(
            f"{n} configuration(s) tried. The best exploration score is biased upward by selection "
            "alone; trust only what holds on the validation segment, and evaluate the final test "
            "set once, for the single configuration you choose."
        ),
    )
