"""Performance statistics for a list of scored trades."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from xauassist.backtest.models import ExitReason, Trade
from xauassist.domain.enums import Direction

MIN_SAMPLE = 30  # below this no conclusion is drawn
Z95 = 1.96


@dataclass(frozen=True, slots=True)
class GroupStats:
    n: int
    buys: int
    sells: int
    wins: int  # pnl > 0 after spread/slippage
    losses: int  # pnl <= 0
    win_rate: float | None
    win_rate_ci: tuple[float, float] | None  # Wilson 95%
    tp_exits: int
    sl_exits: int
    time_exits: int
    avg_mfe: float | None  # price units
    avg_mae: float | None
    avg_mfe_r: float | None  # in multiples of the trade's risk
    avg_mae_r: float | None
    mean_r: float | None  # expectancy per signal, in R
    mean_r_ci: tuple[float, float] | None  # normal-approximation 95% CI
    total_r: float
    profit_factor: float | None  # gross profit / gross loss (price units); None if no losses
    max_consecutive_losses: int
    max_drawdown_r: float  # worst peak-to-trough of cumulative R (entry order)
    avg_bars_held: float | None
    assumed_spread_trades: int  # trades that used the assumed spread (candle had none)

    @property
    def sufficient_sample(self) -> bool:
        return self.n >= MIN_SAMPLE


def wilson_interval(wins: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def mean_ci(values: Sequence[float], z: float = Z95) -> tuple[float, float] | None:
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    half = z * math.sqrt(var / n)
    return mean - half, mean + half


def _avg(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def compute_stats(trades: Sequence[Trade]) -> GroupStats:
    ordered = sorted(trades, key=lambda t: (t.entry_time, t.signal.direction.value))
    n = len(ordered)
    wins = sum(1 for t in ordered if t.is_win)
    r_values = [t.r_result for t in ordered]

    gross_profit = sum(t.pnl for t in ordered if t.pnl > 0)
    gross_loss = -sum(t.pnl for t in ordered if t.pnl < 0)

    streak = longest = 0
    cumulative = peak = drawdown = 0.0
    for t in ordered:
        streak = streak + 1 if not t.is_win else 0
        longest = max(longest, streak)
        cumulative += t.r_result
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)

    return GroupStats(
        n=n,
        buys=sum(1 for t in ordered if t.signal.direction is Direction.BUY),
        sells=sum(1 for t in ordered if t.signal.direction is Direction.SELL),
        wins=wins,
        losses=n - wins,
        win_rate=wins / n if n else None,
        win_rate_ci=wilson_interval(wins, n),
        tp_exits=sum(1 for t in ordered if t.exit_reason is ExitReason.TP),
        sl_exits=sum(1 for t in ordered if t.exit_reason is ExitReason.SL),
        time_exits=sum(1 for t in ordered if t.exit_reason is ExitReason.TIME),
        avg_mfe=_avg([t.mfe for t in ordered]),
        avg_mae=_avg([t.mae for t in ordered]),
        avg_mfe_r=_avg([t.mfe / t.risk for t in ordered]),
        avg_mae_r=_avg([t.mae / t.risk for t in ordered]),
        mean_r=_avg(r_values),
        mean_r_ci=mean_ci(r_values),
        total_r=sum(r_values),
        profit_factor=gross_profit / gross_loss if gross_loss > 0 else None,
        max_consecutive_losses=longest,
        max_drawdown_r=drawdown,
        avg_bars_held=_avg([float(t.bars_held) for t in ordered]),
        assumed_spread_trades=sum(1 for t in ordered if t.assumed_spread),
    )


def group_by(trades: Sequence[Trade], key: Callable[[Trade], str]) -> dict[str, GroupStats]:
    buckets: dict[str, list[Trade]] = defaultdict(list)
    for trade in trades:
        buckets[key(trade)].append(trade)
    return {name: compute_stats(group) for name, group in sorted(buckets.items())}


def scenario_key(trade: Trade) -> str:
    return f"Scenario {trade.signal.scenario.value} ({trade.signal.scenario.label})"


def pattern_key(trade: Trade) -> str:
    """Exact pattern combination, so a signal with two patterns is not counted twice."""
    return " + ".join(p.value for p in trade.signal.patterns)


def direction_key(trade: Trade) -> str:
    return trade.signal.direction.value


def evidence_statement(stats: GroupStats) -> str:
    """A cautious reading of the result. It never asserts that the strategy is profitable."""
    if stats.n < MIN_SAMPLE:
        return (
            f"Insufficient sample (n={stats.n}, need at least {MIN_SAMPLE}): no conclusion "
            "can be drawn from these numbers."
        )
    ci = stats.mean_r_ci
    assert ci is not None and stats.mean_r is not None
    if ci[0] > 0:
        return (
            f"Mean result {stats.mean_r:+.3f}R per signal; its 95% interval ({ci[0]:+.3f}, "
            f"{ci[1]:+.3f}) excludes zero in this sample under these assumptions. This is "
            "historical evidence only, not proof of future profitability."
        )
    if ci[1] < 0:
        return (
            f"Mean result {stats.mean_r:+.3f}R per signal; its 95% interval ({ci[0]:+.3f}, "
            f"{ci[1]:+.3f}) is entirely below zero: the strategy lost money in this sample "
            "under these assumptions."
        )
    return (
        f"Mean result {stats.mean_r:+.3f}R per signal; its 95% interval ({ci[0]:+.3f}, "
        f"{ci[1]:+.3f}) includes zero: no evidence of an edge in this sample."
    )
