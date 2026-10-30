"""Render backtest reports (Markdown, JSON, trades CSV)."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from xauassist.backtest.session import BacktestReport, Mode, SegmentReport
from xauassist.backtest.splits import select_segment
from xauassist.backtest.stats import MIN_SAMPLE, GroupStats
from xauassist.config.writer import config_to_dict


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def _num(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _ci(ci: tuple[float, float] | None, pct: bool = False, digits: int = 3) -> str:
    if ci is None:
        return "-"
    if pct:
        return f"{ci[0] * 100:.1f}% .. {ci[1] * 100:.1f}%"
    return f"{ci[0]:+.{digits}f} .. {ci[1]:+.{digits}f}"


def assumptions_table(report: BacktestReport) -> list[str]:
    bt = report.config.backtest
    return [
        "| Assumption | Value |",
        "|---|---|",
        "| Candles | BID prices; SELL fills at bid, BUY at ask (= bid + spread) |",
        "| Entry | open of the candle AFTER the confirmation candle (skipped if a market gap intervenes) |",
        f"| Spread | candle's recorded spread; {bt.assumed_spread_points:g} points assumed when absent |",
        f"| Slippage | {bt.slippage_points:g} points, adverse, on entry and stop fills |",
        f"| Stop loss | beyond the confirmation candle's extreme + {bt.sl_buffer:g} |",
        f"| Take profit | {bt.r_multiple:g} x risk |",
        f"| Stop and target in one candle | {bt.same_candle.value.replace('_', ' ').lower()} assumed |",
        f"| Time exit | after {bt.max_hold_bars} candles, at the close |",
        f"| MFE / MAE horizon | the same {bt.max_hold_bars} candles, independent of stop/target |",
        "| Win | pnl > 0 after spread and slippage (a time exit can be a win or a loss) |",
        "| Trades | scored independently (overlap allowed); not a portfolio simulation |",
        f"| Split | {bt.exploration_fraction:.0%} exploration / {bt.validation_fraction:.0%} validation / "
        f"{1 - bt.exploration_fraction - bt.validation_fraction:.0%} test, chronological, "
        "trades crossing a boundary purged |",
    ]


def _stats_rows(stats: GroupStats) -> list[str]:
    return [
        f"| Signals scored | {stats.n} (BUY {stats.buys} / SELL {stats.sells}) |",
        f"| Winning / losing | {stats.wins} / {stats.losses} |",
        f"| Win rate | {_pct(stats.win_rate)}  (95% CI {_ci(stats.win_rate_ci, pct=True)}) |",
        f"| Exits | TP {stats.tp_exits} / SL {stats.sl_exits} / time {stats.time_exits} |",
        f"| Mean result per signal | {_num(stats.mean_r, 3)} R  (95% CI {_ci(stats.mean_r_ci)}) |",
        f"| Total result | {stats.total_r:+.2f} R |",
        f"| Profit factor | {_num(stats.profit_factor)} |",
        f"| Avg favourable excursion (MFE) | {_num(stats.avg_mfe)} price units = {_num(stats.avg_mfe_r)} R |",
        f"| Avg adverse excursion (MAE) | {_num(stats.avg_mae)} price units = {_num(stats.avg_mae_r)} R |",
        f"| Max consecutive losses | {stats.max_consecutive_losses} |",
        f"| Max drawdown | {stats.max_drawdown_r:.2f} R |",
        f"| Avg bars held | {_num(stats.avg_bars_held, 1)} |",
    ]


def _group_table(title: str, groups: dict[str, GroupStats]) -> list[str]:
    if not groups:
        return []
    lines = [
        f"**{title}**",
        "",
        "| Group | n | Win rate | Mean R | 95% CI (R) | MFE R | MAE R | Max consec. losses |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, g in groups.items():
        flag = "" if g.n >= MIN_SAMPLE else " (n too small)"
        lines.append(
            f"| {name}{flag} | {g.n} | {_pct(g.win_rate)} | {_num(g.mean_r, 3)} | {_ci(g.mean_r_ci)} "
            f"| {_num(g.avg_mfe_r)} | {_num(g.avg_mae_r)} | {g.max_consecutive_losses} |"
        )
    return [*lines, ""]


def _segment_section(seg: SegmentReport) -> list[str]:
    lines = [
        f"## {seg.segment.value.capitalize()} segment",
        "",
        f"{seg.start_time:%Y-%m-%d %H:%M} to {seg.end_time:%Y-%m-%d %H:%M} UTC "
        f"({seg.bounds.end - seg.bounds.start} candles)",
        "",
        f"> {seg.evidence}",
        "",
        "| Metric | Value |",
        "|---|---|",
        *_stats_rows(seg.stats),
        "",
    ]
    lines += _group_table("By scenario", seg.by_scenario)
    lines += _group_table("By candlestick pattern", seg.by_pattern)
    lines += _group_table("By direction", seg.by_direction)
    if seg.skipped:
        lines.append(
            "Signals not scored: " + ", ".join(f"{n} x {r}" for r, n in seg.skipped.items())
        )
        lines.append("")
    return lines


def render_markdown(report: BacktestReport) -> str:
    final = report.mode is Mode.FINAL_TEST
    title = "FINAL TEST (out-of-sample)" if final else "DEVELOPMENT (exploration + validation)"
    lines = [
        f"# Backtest report - {report.symbol} {report.config.strategy.timeframe.value} - {title}",
        "",
        f"Data: {report.candles} candles, {report.first_time:%Y-%m-%d} to {report.last_time:%Y-%m-%d}; "
        f"dataset {report.fingerprint}, config {report.config_hash}.",
        f"Engine produced {report.raw_signals} raw signal(s); "
        + (
            "rejected before scoring: "
            + ", ".join(f"{n} x {r}" for r, n in report.rejected.items())
            if report.rejected
            else "none rejected before scoring"
        )
        + ".",
        "",
    ]
    if not final:
        lines += [
            "The test segment was NOT read by this run. Do not tune parameters on a result you "
            "have already seen on the final test.",
            "",
        ]
    lines += ["## Warnings", ""] + [f"- {w}" for w in report.warnings] + [""]
    for seg in report.segments:
        lines += _segment_section(seg)
    lines += ["## Assumptions", "", *assumptions_table(report), ""]
    lines += [
        "## How to read this",
        "",
        f"- No statement here says the strategy is or will be profitable. Fewer than {MIN_SAMPLE} "
        "signals is never conclusive, and an interval that includes zero means no evidence of an edge.",
        "- Win rate alone is meaningless without the reward/risk ratio: with a take-profit of R x "
        "risk the break-even win rate is 1 / (1 + R) before costs.",
        "- Results describe one historical sample under the listed assumptions only.",
        "",
    ]
    return "\n".join(lines)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not serialisable: {type(value)}")


def report_to_dict(report: BacktestReport) -> dict[str, Any]:
    return {
        "mode": report.mode.value,
        "symbol": report.symbol,
        "dataset_fingerprint": report.fingerprint,
        "config_hash": report.config_hash,
        "config": config_to_dict(report.config),
        "candles": report.candles,
        "first_time": report.first_time,
        "last_time": report.last_time,
        "unexpected_gaps": report.unexpected_gaps,
        "raw_signals": report.raw_signals,
        "rejected": report.rejected,
        "test_reused": report.test_reused,
        "warnings": list(report.warnings),
        "segments": [
            {
                "segment": s.segment.value,
                "start": s.start_time,
                "end": s.end_time,
                "stats": asdict(s.stats),
                "by_scenario": {k: asdict(v) for k, v in s.by_scenario.items()},
                "by_pattern": {k: asdict(v) for k, v in s.by_pattern.items()},
                "by_direction": {k: asdict(v) for k, v in s.by_direction.items()},
                "purged": s.purged,
                "skipped": s.skipped,
                "evidence": s.evidence,
            }
            for s in report.segments
        ],
    }


TRADE_COLUMNS = [
    "signal_time", "direction", "scenario", "patterns", "entry_time", "entry_price", "stop_loss",
    "take_profit", "risk", "exit_time", "exit_price", "exit_reason", "pnl", "r_result", "mfe",
    "mae", "bars_held", "spread", "assumed_spread",
]  # fmt: skip


def write_trades_csv(report: BacktestReport, path: Path) -> None:
    """Exactly the trades that were scored (purged trades and unseen segments are excluded)."""
    scored = [t for seg in report.segments for t in select_segment(report.result, seg.bounds)[0]]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(TRADE_COLUMNS)
        for t in sorted(scored, key=lambda x: x.signal_index):
            writer.writerow([
                t.signal.time.isoformat(), t.signal.direction.value, t.signal.scenario.value,
                " + ".join(p.value for p in t.signal.patterns), t.entry_time.isoformat(),
                f"{t.entry_price:.2f}", f"{t.stop_loss:.2f}", f"{t.take_profit:.2f}", f"{t.risk:.2f}",
                t.exit_time.isoformat(), f"{t.exit_price:.2f}", t.exit_reason.value, f"{t.pnl:.2f}",
                f"{t.r_result:.3f}", f"{t.mfe:.2f}", f"{t.mae:.2f}", t.bars_held, f"{t.spread:.2f}",
                t.assumed_spread,
            ])  # fmt: skip


def write_report(report: BacktestReport, directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "markdown": directory / "report.md",
        "json": directory / "summary.json",
        "trades": directory / "trades.csv",
    }
    paths["markdown"].write_text(render_markdown(report), encoding="utf-8")
    paths["json"].write_text(
        json.dumps(report_to_dict(report), indent=2, default=_json_default), encoding="utf-8"
    )
    write_trades_csv(report, paths["trades"])
    return paths
