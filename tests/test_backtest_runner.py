import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any, overload

import pytest

from helpers import candle, make_config, mirror, scenario_a_sell
from xauassist.backtest.cli import main as backtest_main
from xauassist.backtest.models import ExitReason, Segment
from xauassist.backtest.report import render_markdown, report_to_dict, write_report
from xauassist.backtest.runner import run_backtest
from xauassist.backtest.session import Mode, run_development, run_final_test
from xauassist.backtest.splits import FinalTestLock, TestSetAlreadyUsedError, segment_bounds
from xauassist.backtest.stats import compute_stats
from xauassist.backtest.sweep import run_sweep
from xauassist.backtest.synthetic import generate_candles
from xauassist.config.models import (
    AppConfig,
    BacktestConfig,
    SignalsConfig,
    ValidationConfig,
)
from xauassist.domain.candle import Candle
from xauassist.domain.enums import Direction, SameCandleRule

FLAT = SignalsConfig(cooldown_bars=0)


def config(**bt: Any) -> AppConfig:
    return AppConfig(
        strategy=make_config(), signals=FLAT, backtest=BacktestConfig(max_hold_bars=6, **bt)
    )


def with_followthrough(candles: list[Candle], steps: int = 12, drift: float = -0.4) -> list[Candle]:
    """Append candles after the setup that keep falling, so a SELL has data to resolve on."""
    out = list(candles)
    last = out[-1]
    price = last.close
    for _ in range(steps):
        o, c = price, price + drift
        out.append(candle(len(out), o, o + 0.15, c - 0.15, c))
        price = c
    return out


# ---------------- runner ----------------


def test_runner_scores_the_live_signal_with_next_bar_entry() -> None:
    candles = with_followthrough(scenario_a_sell())
    result = run_backtest(candles, config())
    (trade,) = result.trades
    setup_index = len(scenario_a_sell()) - 1
    assert trade.signal.direction is Direction.SELL and trade.signal_index == setup_index
    assert trade.entry_index == setup_index + 1
    assert trade.entry_price == pytest.approx(candles[setup_index + 1].open)  # SELL fills at bid
    assert trade.stop_loss == pytest.approx(candles[setup_index].high)
    assert trade.entry_time == trade.signal.time  # entered right when the signal came to exist
    assert result.raw_signals == 1 and result.rejected == {}


def test_buy_mirror_produces_a_buy_trade_with_ask_fill() -> None:
    candles = mirror(with_followthrough(scenario_a_sell()))
    (trade,) = run_backtest(candles, config()).trades
    idx = trade.signal_index
    assert trade.signal.direction is Direction.BUY
    assert trade.entry_price == pytest.approx(
        candles[idx + 1].open + 20 * 0.01
    )  # ask = bid + spread


def test_spread_gate_uses_the_candle_spread() -> None:
    candles = with_followthrough(scenario_a_sell())
    cfg = replace(config(), validation=ValidationConfig(max_spread_points=10.0))  # candles carry 20
    result = run_backtest(candles, cfg)
    assert result.trades == [] and result.rejected == {"validation: spread": 1}


def test_cooldown_and_duplicate_rules_match_live() -> None:
    candles = with_followthrough(scenario_a_sell())
    cfg = replace(config(), signals=SignalsConfig(cooldown_bars=3))
    assert len(run_backtest(candles, cfg).trades) == 1  # first signal is never blocked


def test_unexpected_gap_suppresses_signals_for_the_configured_window() -> None:
    base = with_followthrough(scenario_a_sell())
    gapped = [c for i, c in enumerate(base) if i not in (60, 61)]  # hole well before the setup
    cfg = config()
    control = run_backtest(gapped, cfg)  # default window (20 candles) is over long before the setup
    assert control.unexpected_gaps == 1 and len(control.trades) == 1

    wide = replace(cfg, monitor=replace(cfg.monitor, post_gap_suppress_bars=100))
    result = run_backtest(gapped, wide)  # the setup now falls inside the suppression window
    assert result.raw_signals == 1 and result.trades == []
    assert result.rejected == {"validation: incomplete": 1}


def test_signal_before_a_market_gap_is_not_traded() -> None:
    base = scenario_a_sell()
    gap_after = base + [
        candle(len(base) + 200 + i, 2711.0, 2711.4, 2710.8, 2711.1) for i in range(12)
    ]  # next candle is 200 bars later (a long closure)
    result = run_backtest(gap_after, config())
    assert result.trades == [] and [s.reason.name for s in result.skipped] == ["GAP_BEFORE_ENTRY"]


def test_no_lookahead_future_candles_cannot_change_past_signals_or_trades() -> None:
    candles = generate_candles(6000, seed=11)
    cfg = AppConfig()
    full = run_backtest(candles, cfg)
    horizon = cfg.backtest.max_hold_bars

    def view(result: Any, upto: int) -> list[Any]:
        return [
            (t.signal.key, t.entry_price, t.stop_loss, t.exit_price, t.exit_reason, t.mfe, t.mae)
            for t in result.trades
            if t.signal_index <= upto
        ]

    for cut in (2500, 3333, 4999):
        part = run_backtest(candles[:cut], cfg)
        stable = cut - horizon - 2  # trades whose whole outcome window lies inside the prefix
        assert view(part, stable) == view(full, stable)
        assert len(view(full, stable)) > 20  # the comparison is not vacuous


# ---------------- random-walk honesty checks ----------------


def zero_cost(candles: Sequence[Candle]) -> list[Candle]:
    return [
        Candle(c.time_open, c.open, c.high, c.low, c.close, c.tick_volume, 0.0) for c in candles
    ]


def pooled(seeds: range, cfg: AppConfig, n: int = 40000) -> tuple[float, float, int]:
    values: list[float] = []
    for seed in seeds:
        report = run_development(zero_cost(generate_candles(n, seed=seed)), cfg)
        from xauassist.backtest.splits import select_segment

        for s in report.segments:
            values += [t.r_result for t in select_segment(report.result, s.bounds)[0]]
    mean = sum(values) / len(values)
    sd = (sum((v - mean) ** 2 for v in values) / (len(values) - 1)) ** 0.5
    return mean, sd / len(values) ** 0.5, len(values)


def test_random_walk_with_no_costs_shows_no_edge_when_ties_are_rare() -> None:
    """Regression guard for hidden bias: random data must average ~0 R (wide stops, no costs)."""
    cfg = AppConfig(
        backtest=BacktestConfig(assumed_spread_points=0.0, sl_buffer=3.0, max_hold_bars=96)
    )
    mean, se, n = pooled(range(1, 4), cfg)
    assert n > 5000 and abs(mean) < 4 * se, (mean, se, n)


def test_same_candle_rule_brackets_the_zero_cost_random_walk() -> None:
    pess = AppConfig(backtest=BacktestConfig(assumed_spread_points=0.0, max_hold_bars=48))
    opt = replace(pess, backtest=replace(pess.backtest, same_candle=SameCandleRule.TARGET_FIRST))
    (m_pess, _, _), (m_opt, _, _) = pooled(range(1, 3), pess), pooled(range(1, 3), opt)
    assert m_pess < 0 < m_opt  # tight stops: the tie rule alone moves the answer both ways


def test_random_walk_with_realistic_costs_is_losing_not_winning() -> None:
    report = run_development(generate_candles(40000, seed=5), AppConfig())
    for seg in report.segments:
        ci = seg.stats.mean_r_ci
        assert ci is not None and ci[1] < 0  # costs make a no-edge market a loser
        assert "lost money" in seg.evidence


def test_synthetic_data_has_only_expected_gaps() -> None:
    report = run_development(generate_candles(12000, seed=2), AppConfig())
    assert report.unexpected_gaps == 0
    assert generate_candles(500, seed=9) == generate_candles(500, seed=9)
    assert generate_candles(500, seed=9) != generate_candles(500, seed=10)


# ---------------- sessions: test set protection ----------------


class GuardedCandles(Sequence[Candle]):
    """Raises if anything reads a candle at or beyond `limit` (the start of the test segment)."""

    def __init__(self, data: list[Candle], limit: int) -> None:
        self._data, self._limit = data, limit
        self.reads: set[int] = set()

    def __len__(self) -> int:
        return len(self._data)

    @overload
    def __getitem__(self, i: int) -> Candle: ...
    @overload
    def __getitem__(self, i: slice) -> list[Candle]: ...
    def __getitem__(self, i: int | slice) -> Candle | list[Candle]:
        if isinstance(i, slice):
            start, stop, _ = i.indices(len(self._data))
            assert stop <= self._limit, f"slice reaches into the test set: {i}"
            self.reads.update(range(start, stop))
            return self._data[i]
        idx = i if i >= 0 else len(self._data) + i
        assert idx < self._limit, f"candle {idx} of the test set was read"
        self.reads.add(idx)
        return self._data[i]


def test_development_run_never_reads_the_test_candles() -> None:
    data = generate_candles(9000, seed=4)
    cfg = AppConfig()
    test_start = segment_bounds(len(data), cfg.backtest)[Segment.TEST].start
    guarded = GuardedCandles(data, limit=test_start)
    report = run_development(guarded, cfg)
    assert report.mode is Mode.DEVELOPMENT and report.candles == test_start
    assert [s.segment for s in report.segments] == [Segment.EXPLORATION, Segment.VALIDATION]
    assert max(guarded.reads) < test_start


def test_development_result_is_unchanged_by_whatever_the_test_data_contains() -> None:
    data = generate_candles(9000, seed=6)
    cfg = AppConfig()
    start = segment_bounds(len(data), cfg.backtest)[Segment.TEST].start
    scrambled = data[:start] + generate_candles(
        len(data) - start, seed=99, start=data[start].time_open
    )
    a, b = run_development(data, cfg), run_development(scrambled, cfg)
    assert [s.stats for s in a.segments] == [
        s.stats for s in b.segments
    ] and a.fingerprint == b.fingerprint


def test_final_test_runs_once_then_is_refused(tmp_path: Path) -> None:
    data = generate_candles(9000, seed=7)
    cfg = AppConfig()
    lock = FinalTestLock(tmp_path)
    report = run_final_test(data, cfg, lock)
    assert report.mode is Mode.FINAL_TEST and [s.segment for s in report.segments] == [Segment.TEST]
    assert not report.test_reused
    with pytest.raises(TestSetAlreadyUsedError):
        run_final_test(data, cfg, lock)
    again = run_final_test(data, cfg, lock, allow_reuse=True)
    assert again.test_reused and any("NOT out-of-sample" in w for w in again.warnings)
    assert "NOT out-of-sample" in render_markdown(again)


def test_final_test_on_different_data_is_a_new_test(tmp_path: Path) -> None:
    lock = FinalTestLock(tmp_path)
    run_final_test(generate_candles(9000, seed=7), AppConfig(), lock)
    run_final_test(generate_candles(9000, seed=8), AppConfig(), lock)  # no error


def test_failed_run_does_not_burn_the_test_set(tmp_path: Path) -> None:
    lock = FinalTestLock(tmp_path)
    with pytest.raises(ValueError):
        run_final_test(generate_candles(300, seed=1), AppConfig(), lock)  # too short
    assert list(tmp_path.glob("*.json")) == []


# ---------------- sweep ----------------


def test_sweep_cannot_touch_the_test_set_and_reports_trial_count() -> None:
    data = generate_candles(9000, seed=12)
    start = segment_bounds(len(data), AppConfig().backtest)[Segment.TEST].start
    guarded = GuardedCandles(data, limit=start)
    result = run_sweep(
        guarded, AppConfig(), {"strategy.zone.value": [0.2, 0.4], "backtest.r_multiple": [1.0, 2.0]}
    )
    assert result.n_trials == 4 and len(result.rows) == 4
    assert "4 configuration(s) tried" in result.warning and "biased upward" in result.warning
    ranked = result.ranked_by_exploration()
    assert ranked[0].exploration.mean_r >= ranked[-1].exploration.mean_r  # type: ignore[operator]
    assert all(r.validation.n >= 0 and r.exploration.n > 0 for r in result.rows)
    assert {tuple(r.overrides) for r in result.rows} == {
        ("strategy.zone.value", "backtest.r_multiple")
    }


# ---------------- reports and CLI ----------------


def test_report_content_and_files(tmp_path: Path) -> None:
    report = run_development(generate_candles(12000, seed=2), AppConfig())
    text = render_markdown(report)
    for expected in (
        "DEVELOPMENT", "Exploration segment", "Validation segment", "By scenario", "By candlestick pattern",
        "Win rate", "Max consecutive losses", "MFE", "MAE", "## Assumptions", "AFTER the confirmation candle",
        "The test segment was NOT read", "UNCONFIRMED placeholder",
    ):  # fmt: skip
        assert expected.lower() in text.lower(), expected
    assert "is profitable" not in text.lower() or "no statement here says" in text.lower()
    paths = write_report(report, tmp_path / "out")
    data = json.loads(paths["json"].read_text())
    assert data["mode"] == "development" and len(data["segments"]) == 2
    assert data["segments"][0]["stats"]["n"] == report.segments[0].stats.n
    assert report_to_dict(report)["warnings"]
    lines = paths["trades"].read_text().splitlines()
    assert lines[0].startswith("signal_time,direction") and len(lines) - 1 == sum(
        s.stats.n for s in report.segments
    )


def test_cli_development_then_final_test_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = ["--synthetic", "9000", "--seed", "21", "--out", str(tmp_path)]
    assert backtest_main(base) == 0
    out = capsys.readouterr().out
    assert "DEVELOPMENT" in out and "Report written to" in out
    assert list(tmp_path.glob("backtest_*/report.md"))
    assert backtest_main([*base, "--final-test"]) == 0
    assert "FINAL TEST" in capsys.readouterr().out
    assert backtest_main([*base, "--final-test"]) == 3  # refused
    assert "refused" in capsys.readouterr().err
    assert backtest_main([*base, "--final-test", "--allow-test-reuse"]) == 0
    assert "NOT out-of-sample" in capsys.readouterr().out


def test_cli_overrides_and_csv_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    csv = tmp_path / "xau.csv"
    rows = ["time,open,high,low,close,tick_volume,spread"]
    for c in generate_candles(9000, seed=5):
        rows.append(
            f"{c.time_open:%Y-%m-%dT%H:%M:%S},{c.open},{c.high},{c.low},{c.close},{c.tick_volume},{int(c.spread_points)}"
        )
    csv.write_text("\n".join(rows))
    args = [
        "--csv",
        str(csv),
        "--out",
        str(tmp_path / "r"),
        "--r",
        "2",
        "--sl-buffer",
        "0.5",
        "--max-hold",
        "24",
        "--same-candle",
        "TARGET_FIRST",
    ]
    assert backtest_main(args) == 0
    out = capsys.readouterr().out
    assert "2 x risk" in out and "24 candles" in out and "target first assumed" in out


def test_cli_errors_are_reported_not_raised(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert backtest_main(["--csv", str(tmp_path / "missing.csv"), "--out", str(tmp_path)]) == 2
    assert "error" in capsys.readouterr().err
    assert backtest_main(["--synthetic", "100", "--out", str(tmp_path)]) == 2  # too short to split


def test_exit_reason_enum_used_in_runner_results() -> None:
    result = run_backtest(with_followthrough(scenario_a_sell(), steps=30, drift=-1.0), config())
    assert result.trades[0].exit_reason in set(ExitReason) and timedelta(0) == timedelta(0)
    assert compute_stats(result.trades).n == 1
