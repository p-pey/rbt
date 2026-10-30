import json
import math
from pathlib import Path

import pytest

from fakes import utc
from helpers import scenario_a_sell
from test_backtest_execution import bar, signal_for
from xauassist.backtest.models import (
    BacktestResult,
    ExitReason,
    Segment,
    SkippedSignal,
    SkipReason,
    Trade,
)
from xauassist.backtest.splits import (
    FinalTestLock,
    SegmentBounds,
    TestSetAlreadyUsedError,
    dataset_fingerprint,
    segment_bounds,
    select_segment,
)
from xauassist.backtest.stats import (
    MIN_SAMPLE,
    compute_stats,
    evidence_statement,
    group_by,
    mean_ci,
    pattern_key,
    scenario_key,
    wilson_interval,
)
from xauassist.config.models import BacktestConfig
from xauassist.domain.enums import Direction, Pattern, Scenario


def make_trade(
    r: float,
    *,
    index: int = 0,
    direction: Direction = Direction.SELL,
    scenario: Scenario = Scenario.A,
    patterns: tuple[Pattern, ...] | None = None,
    risk: float = 2.0,
    last_used: int | None = None,
    mfe: float = 3.0,
    mae: float = 1.0,
    bars: int = 5,
) -> Trade:
    candles = [bar(0, 2700, 2702, 2698, 2699)]
    sig = signal_for(0, direction, candles)
    from dataclasses import replace

    sig = replace(sig, scenario=scenario, patterns=patterns or sig.patterns)
    t = utc(2026, 1, 6, 10) + (index * __import__("datetime").timedelta(minutes=5))
    return Trade(
        signal=sig, signal_index=index, entry_index=index + 1, entry_time=t, entry_price=2698.0,
        stop_loss=2700.0, take_profit=2694.0, risk=risk, exit_index=index + bars, exit_time=t,
        exit_price=2698.0 - r * risk, exit_reason=ExitReason.TP if r > 0 else ExitReason.SL,
        pnl=r * risk, r_result=r, mfe=mfe, mae=mae, bars_held=bars, spread=0.2, assumed_spread=False,
        last_index_used=index + bars if last_used is None else last_used,
    )  # fmt: skip


# ---------------- statistics ----------------


def test_wilson_interval_known_values() -> None:
    lo, hi = wilson_interval(5, 10)  # type: ignore[misc]
    assert lo == pytest.approx(0.2366, abs=1e-3) and hi == pytest.approx(0.7634, abs=1e-3)
    assert wilson_interval(0, 0) is None
    lo, hi = wilson_interval(10, 10)  # type: ignore[misc]
    assert hi == pytest.approx(1.0) and 0.7 < lo < 0.75


def test_mean_ci_known_values() -> None:
    lo, hi = mean_ci([1.0, 2.0, 3.0])  # type: ignore[misc]
    half = 1.96 * 1.0 / math.sqrt(3)
    assert (lo, hi) == pytest.approx((2 - half, 2 + half))
    assert mean_ci([1.0]) is None


def test_compute_stats_by_hand() -> None:
    # Sequence W L L W L L L W, R = +1 / -1 (risk 2.0 so pnl = +-2.0)
    results = [1, -1, -1, 1, -1, -1, -1, 1]
    trades = [
        make_trade(r, index=i, direction=Direction.BUY if i % 2 else Direction.SELL)
        for i, r in enumerate(results)
    ]
    s = compute_stats(trades)
    assert (s.n, s.wins, s.losses, s.buys, s.sells) == (8, 3, 5, 4, 4)
    assert s.win_rate == pytest.approx(3 / 8)
    assert s.mean_r == pytest.approx(-2 / 8) and s.total_r == pytest.approx(-2.0)
    assert s.max_consecutive_losses == 3
    assert s.profit_factor == pytest.approx(3 / 5)
    # cumulative R: 1,0,-1,0,-1,-2,-3,-2 -> peak 1, trough -3 => drawdown 4
    assert s.max_drawdown_r == pytest.approx(4.0)
    assert s.avg_mfe == pytest.approx(3.0) and s.avg_mae == pytest.approx(1.0)
    assert s.avg_mfe_r == pytest.approx(1.5) and s.avg_mae_r == pytest.approx(0.5)
    assert s.tp_exits == 3 and s.sl_exits == 5 and s.avg_bars_held == pytest.approx(5.0)
    assert not s.sufficient_sample


def test_stats_use_entry_order_not_list_order() -> None:
    trades = [make_trade(-1, index=2), make_trade(1, index=0), make_trade(-1, index=1)]
    assert compute_stats(trades).max_consecutive_losses == 2


def test_empty_stats_are_safe() -> None:
    s = compute_stats([])
    assert s.n == 0 and s.win_rate is None and s.mean_r is None and s.profit_factor is None
    assert s.max_consecutive_losses == 0 and s.total_r == 0.0


def test_profit_factor_is_none_without_losses() -> None:
    assert compute_stats([make_trade(1), make_trade(2)]).profit_factor is None


def test_group_by_scenario_and_pattern_partitions_without_double_counting() -> None:
    both = (Pattern.BEARISH_PIN_BAR, Pattern.BEARISH_ENGULFING)
    trades = [
        make_trade(1, index=0, scenario=Scenario.A, patterns=(Pattern.BEARISH_PIN_BAR,)),
        make_trade(-1, index=1, scenario=Scenario.A, patterns=(Pattern.BEARISH_ENGULFING,)),
        make_trade(-1, index=2, scenario=Scenario.B, patterns=both),
    ]
    by_scenario = group_by(trades, scenario_key)
    assert sum(g.n for g in by_scenario.values()) == 3 and len(by_scenario) == 2
    by_pattern = group_by(trades, pattern_key)
    assert sum(g.n for g in by_pattern.values()) == 3
    assert "Bearish Pin Bar + Bearish Engulfing" in by_pattern


def test_evidence_statement_never_claims_profitability() -> None:
    small = compute_stats([make_trade(1)] * 5)
    assert "Insufficient sample" in evidence_statement(small)
    flat = compute_stats([make_trade(1 if i % 2 else -1, index=i) for i in range(60)])
    assert "includes zero" in evidence_statement(
        flat
    ) and "no evidence of an edge" in evidence_statement(flat)
    winners = compute_stats([make_trade(1 if i % 10 else -1, index=i) for i in range(100)])
    text = evidence_statement(winners)
    assert "excludes zero" in text and "not proof of future profitability" in text
    losers = compute_stats([make_trade(-1 if i % 10 else 1, index=i) for i in range(100)])
    assert "lost money" in evidence_statement(losers)
    assert MIN_SAMPLE == 30


# ---------------- splits, purging, lock ----------------


def test_segment_bounds_are_chronological_and_partition_the_data() -> None:
    b = segment_bounds(1000, BacktestConfig())
    assert (b[Segment.EXPLORATION].start, b[Segment.EXPLORATION].end) == (0, 600)
    assert (b[Segment.VALIDATION].start, b[Segment.VALIDATION].end) == (600, 800)
    assert (b[Segment.TEST].start, b[Segment.TEST].end) == (800, 1000)


def test_too_little_data_is_refused() -> None:
    with pytest.raises(ValueError, match="at least"):
        segment_bounds(100, BacktestConfig())


def test_fractions_must_leave_a_test_set() -> None:
    from xauassist.config.models import ConfigError

    with pytest.raises(ConfigError):
        BacktestConfig(exploration_fraction=0.7, validation_fraction=0.3)


def test_trades_crossing_a_segment_boundary_are_purged() -> None:
    bounds = SegmentBounds(Segment.EXPLORATION, 0, 100)
    result = BacktestResult(
        trades=[
            make_trade(1, index=10, last_used=50),  # inside
            make_trade(1, index=95, last_used=100),  # outcome window reaches the next segment
            make_trade(1, index=150, last_used=160),  # belongs to a later segment
        ],
        skipped=[
            SkippedSignal(make_trade(1).signal, 20, SkipReason.UNRESOLVED),
            SkippedSignal(make_trade(1).signal, 200, SkipReason.UNRESOLVED),
        ],
    )
    kept, skipped, purged = select_segment(result, bounds)
    assert [t.signal_index for t in kept] == [10] and purged == 1 and len(skipped) == 1


def test_fingerprint_is_stable_and_sensitive() -> None:
    candles = scenario_a_sell()
    assert dataset_fingerprint(candles) == dataset_fingerprint(list(candles))
    from xauassist.domain.candle import Candle

    c = candles[50]
    altered = [
        *candles[:50],
        Candle(c.time_open, c.open, c.high, c.low, c.close + 0.01),
        *candles[51:],
    ]
    assert dataset_fingerprint(altered) != dataset_fingerprint(candles)
    assert dataset_fingerprint(candles[:-1]) != dataset_fingerprint(candles)


def test_final_test_lock_allows_once(tmp_path: Path) -> None:
    lock = FinalTestLock(tmp_path / "locks")
    first = lock.claim("abc123", "cfg1", allow_reuse=False)
    assert not first.reused
    record = json.loads(lock.path_for("abc123").read_text())
    assert record["config_hash"] == "cfg1" and record["used_at"]
    with pytest.raises(TestSetAlreadyUsedError) as info:
        lock.claim("abc123", "cfg2", allow_reuse=False)
    assert "out-of-sample" in str(info.value) and info.value.previous["config_hash"] == "cfg1"
    forced = lock.claim("abc123", "cfg2", allow_reuse=True)
    assert forced.reused and forced.previous is not None
    assert (
        json.loads(lock.path_for("abc123").read_text())["config_hash"] == "cfg1"
    )  # first claim kept
    assert not lock.claim("other-dataset", "cfg1", allow_reuse=False).reused
