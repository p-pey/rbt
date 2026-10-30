"""Command line: `python -m xauassist.backtest`.

By default only the exploration and validation segments are evaluated. `--final-test` evaluates
the untouched test segment, and can be used once per dataset.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from xauassist.backtest.report import render_markdown, write_report
from xauassist.backtest.session import BacktestReport, run_development, run_final_test
from xauassist.backtest.splits import FinalTestLock, TestSetAlreadyUsedError
from xauassist.backtest.synthetic import generate_candles
from xauassist.config.loader import load_config
from xauassist.config.models import AppConfig, ConfigError
from xauassist.data.csv_loader import load_candles_csv
from xauassist.data.errors import DataFormatError
from xauassist.domain.candle import Candle
from xauassist.domain.enums import SameCandleRule


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m xauassist.backtest",
        description="Backtest the MA20 strategy on historical M5 candles (alerts-only strategy).",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", type=Path, help="candles CSV (MT5 history export or simple CSV)")
    src.add_argument(
        "--synthetic", type=int, metavar="N",
        help="N random-walk candles: exercises the machinery; NEVER evidence about the strategy",
    )  # fmt: skip
    p.add_argument("--config", type=Path, help="config.toml (default: built-in defaults)")
    p.add_argument(
        "--utc-offset-hours", type=float, default=0.0, help="broker server UTC offset of the CSV"
    )
    p.add_argument("--out", type=Path, default=Path("reports"), help="output folder")
    p.add_argument(
        "--final-test", action="store_true", help="evaluate the untouched test segment (once!)"
    )
    p.add_argument(
        "--allow-test-reuse",
        action="store_true",
        help="re-run the final test (flagged as not out-of-sample)",
    )
    p.add_argument("--seed", type=int, default=1, help="seed for --synthetic")
    g = p.add_argument_group("execution assumptions (override the config)")
    g.add_argument("--r", type=float, dest="r_multiple", help="take-profit as a multiple of risk")
    g.add_argument("--sl-buffer", type=float, help="stop distance beyond the candle extreme")
    g.add_argument("--max-hold", type=int, dest="max_hold_bars", help="time exit, in candles")
    g.add_argument(
        "--spread", type=float, dest="assumed_spread_points", help="assumed spread (points)"
    )
    g.add_argument(
        "--slippage", type=float, dest="slippage_points", help="adverse slippage (points)"
    )
    g.add_argument(
        "--same-candle",
        choices=[r.value for r in SameCandleRule],
        help="which level wins when one candle reaches both stop and target",
    )
    return p


def apply_cli_overrides(config: AppConfig, args: argparse.Namespace) -> AppConfig:
    mapping: dict[str, Any] = {
        "same_candle": SameCandleRule(args.same_candle) if args.same_candle else None,
        "r_multiple": args.r_multiple,
        "sl_buffer": args.sl_buffer,
        "max_hold_bars": args.max_hold_bars,
        "assumed_spread_points": args.assumed_spread_points,
        "slippage_points": args.slippage_points,
    }
    changes = {k: v for k, v in mapping.items() if v is not None}
    return replace(config, backtest=replace(config.backtest, **changes)) if changes else config


def load_candles(args: argparse.Namespace) -> list[Candle]:
    if args.synthetic:
        return generate_candles(args.synthetic, seed=args.seed)
    return load_candles_csv(args.csv, utc_offset_hours=args.utc_offset_hours)


def execute(args: argparse.Namespace) -> tuple[BacktestReport, Path]:
    config = load_config(args.config) if args.config else AppConfig()
    config = apply_cli_overrides(config, args)
    candles = load_candles(args)
    out_dir = args.out / datetime.now().strftime("backtest_%Y%m%d_%H%M%S")
    if args.final_test:
        report = run_final_test(
            candles, config, FinalTestLock(args.out / "locks"), allow_reuse=args.allow_test_reuse
        )
    else:
        report = run_development(candles, config)
    write_report(report, out_dir)
    return report, out_dir


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report, out_dir = execute(args)
    except (ConfigError, DataFormatError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except TestSetAlreadyUsedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    print(render_markdown(report))
    print(f"\nReport written to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
