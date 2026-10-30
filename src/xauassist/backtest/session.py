"""Backtest sessions: a development run (exploration + validation) and the one-shot final test."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from xauassist.backtest.models import BacktestResult, Segment, SkipReason
from xauassist.backtest.runner import run_backtest
from xauassist.backtest.splits import (
    FinalTestLock,
    SegmentBounds,
    config_hash,
    dataset_fingerprint,
    segment_bounds,
    select_segment,
)
from xauassist.backtest.stats import (
    GroupStats,
    compute_stats,
    direction_key,
    evidence_statement,
    group_by,
    pattern_key,
    scenario_key,
)
from xauassist.config.models import AppConfig, BacktestConfig, StrategyConfig, ValidationConfig
from xauassist.domain.candle import Candle

SMALL_SEGMENT_BARS = 2000  # about a week of M5 candles


class Mode(StrEnum):
    DEVELOPMENT = "development"
    FINAL_TEST = "final_test"


@dataclass(frozen=True, slots=True)
class SegmentReport:
    segment: Segment
    bounds: SegmentBounds
    start_time: datetime
    end_time: datetime
    stats: GroupStats
    by_scenario: dict[str, GroupStats]
    by_pattern: dict[str, GroupStats]
    by_direction: dict[str, GroupStats]
    purged: int
    skipped: dict[str, int]
    evidence: str


@dataclass(frozen=True, slots=True)
class BacktestReport:
    mode: Mode
    symbol: str
    config: AppConfig
    config_hash: str
    fingerprint: str
    candles: int
    first_time: datetime
    last_time: datetime
    unexpected_gaps: int
    raw_signals: int
    rejected: dict[str, int]
    segments: tuple[SegmentReport, ...]
    test_reused: bool
    warnings: tuple[str, ...]
    result: BacktestResult  # trades of the evaluated segments' run (for CSV export)


def _segment_report(
    result: BacktestResult, bounds: SegmentBounds, candles: Sequence[Candle]
) -> SegmentReport:
    trades, skipped, purged = select_segment(result, bounds)
    counts: dict[str, int] = {}
    for item in skipped:
        counts[item.reason.value] = counts.get(item.reason.value, 0) + 1
    stats = compute_stats(trades)
    return SegmentReport(
        segment=bounds.segment,
        bounds=bounds,
        start_time=candles[bounds.start].time_open,
        end_time=candles[bounds.end - 1].time_open,
        stats=stats,
        by_scenario=group_by(trades, scenario_key),
        by_pattern=group_by(trades, pattern_key),
        by_direction=group_by(trades, direction_key),
        purged=purged,
        skipped=counts,
        evidence=evidence_statement(stats),
    )


def _warnings(
    config: AppConfig, reports: Sequence[SegmentReport], result: BacktestResult, reused: bool
) -> tuple[str, ...]:
    out: list[str] = []
    default_bt, default_val = BacktestConfig(), ValidationConfig()
    bt = config.backtest
    unset = [
        name
        for name in (
            "r_multiple",
            "sl_buffer",
            "max_hold_bars",
            "assumed_spread_points",
            "slippage_points",
        )
        if getattr(bt, name) == getattr(default_bt, name)
    ]
    if unset:
        out.append(
            "Execution assumptions still at their UNCONFIRMED placeholder values: "
            + ", ".join(unset)
            + ". Results only describe this particular scoring convention."
        )
    if config.strategy.zone == StrategyConfig().zone:
        out.append("strategy.zone is still the unconfirmed placeholder (0.30 x ATR).")
    if config.validation.max_spread_points == default_val.max_spread_points:
        out.append("validation.max_spread_points is still the unconfirmed placeholder (50 points).")
    if result.unexpected_gaps:
        out.append(
            f"{result.unexpected_gaps} unexpected data gap(s) (missing candles); signals within "
            f"{config.monitor.post_gap_suppress_bars} candles after a gap were suppressed."
        )
    for r in reports:
        if r.stats.assumed_spread_trades:
            out.append(
                f"{r.segment.value}: {r.stats.assumed_spread_trades}/{r.stats.n} trades used the "
                "assumed spread because the data has no spread column."
            )
        if r.bounds.end - r.bounds.start < SMALL_SEGMENT_BARS:
            out.append(
                f"{r.segment.value} segment is very short "
                f"({r.bounds.end - r.bounds.start} candles)."
            )
        if r.purged:
            out.append(
                f"{r.segment.value}: {r.purged} trade(s) purged because their outcome window "
                "crossed into the next segment."
            )
        unresolved = r.skipped.get(SkipReason.UNRESOLVED.value, 0)
        if unresolved:
            out.append(
                f"{r.segment.value}: {unresolved} signal(s) unresolved at the end of the data."
            )
    if reused:
        out.append(
            "THE FINAL TEST SET WAS ALREADY EVALUATED BEFORE. These numbers are NOT out-of-sample: "
            "any decision made after seeing the earlier result has leaked into this one."
        )
    out.append(
        "Per-scenario and per-pattern tables are exploratory subgroups: with several groups, one "
        "looking good by chance is expected. Do not select a subgroup from the same data."
    )
    return tuple(out)


def run_development(
    candles: Sequence[Candle], config: AppConfig, symbol: str = "XAUUSD"
) -> BacktestReport:
    """Exploration + validation only. The test candles are cut off BEFORE the engine runs."""
    bounds = segment_bounds(len(candles), config.backtest)
    dev_end = bounds[Segment.VALIDATION].end
    dev_candles = candles[:dev_end]
    result = run_backtest(dev_candles, config, symbol)
    reports = tuple(
        _segment_report(result, bounds[s], dev_candles)
        for s in (Segment.EXPLORATION, Segment.VALIDATION)
    )
    return BacktestReport(
        mode=Mode.DEVELOPMENT,
        symbol=symbol,
        config=config,
        config_hash=config_hash(config),
        fingerprint=dataset_fingerprint(dev_candles),
        candles=len(dev_candles),
        first_time=dev_candles[0].time_open,
        last_time=dev_candles[-1].time_open,
        unexpected_gaps=result.unexpected_gaps,
        raw_signals=result.raw_signals,
        rejected=result.rejected,
        segments=reports,
        test_reused=False,
        warnings=_warnings(config, reports, result, reused=False),
        result=result,
    )


def run_final_test(
    candles: Sequence[Candle],
    config: AppConfig,
    lock: FinalTestLock,
    *,
    allow_reuse: bool = False,
    symbol: str = "XAUUSD",
) -> BacktestReport:
    """Evaluate the untouched test segment, once per dataset (see FinalTestLock)."""
    bounds = segment_bounds(len(candles), config.backtest)
    fingerprint = dataset_fingerprint(candles)
    cfg_hash = config_hash(config)
    # Run first (nothing is displayed yet), then claim the lock, then build the report.
    result = run_backtest(candles, config, symbol)
    outcome = lock.claim(fingerprint, cfg_hash, allow_reuse=allow_reuse)
    report = _segment_report(result, bounds[Segment.TEST], candles)
    return BacktestReport(
        mode=Mode.FINAL_TEST,
        symbol=symbol,
        config=config,
        config_hash=cfg_hash,
        fingerprint=fingerprint,
        candles=len(candles),
        first_time=candles[0].time_open,
        last_time=candles[-1].time_open,
        unexpected_gaps=result.unexpected_gaps,
        raw_signals=result.raw_signals,
        rejected=result.rejected,
        segments=(report,),
        test_reused=outcome.reused,
        warnings=_warnings(config, (report,), result, reused=outcome.reused),
        result=result,
    )
