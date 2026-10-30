"""Manual check of the live MetaTrader 5 connection (run on Windows with MT5 open and logged in).

    python scripts/mt5_smoke_test.py [--offset-hours 2] [--terminal-path "C:\\...\\terminal64.exe"]

Prints what the data layer sees. Nothing is traded; this only reads market data.
"""

from __future__ import annotations

import argparse
import sys
import time

from xauassist.config.models import FeedConfig, Mt5Config
from xauassist.data.feed import DataFeed, FeedStatus
from xauassist.data.mt5_provider import Mt5Provider
from xauassist.domain.enums import Timeframe


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offset-hours", type=float, default=0.0, help="broker server UTC offset")
    parser.add_argument("--terminal-path", default="")
    parser.add_argument("--polls", type=int, default=20, help="how many 2-second polls to run")
    args = parser.parse_args()

    provider = Mt5Provider.from_installed(
        Mt5Config(terminal_path=args.terminal_path, server_utc_offset_hours=args.offset_hours)
    )
    feed = DataFeed(provider, Timeframe.M5, r"XAUUSD[\.\w]*", FeedConfig())

    boot = feed.bootstrap()
    print(f"bootstrap: {boot.status.value} - {boot.message}")
    if boot.status is not FeedStatus.OK:
        return 1
    last = boot.candles[-1]
    print(f"symbol: {feed.symbol}  candles: {len(boot.candles)}  complete: {boot.data_complete}")
    print(f"last closed candle (UTC): {last.time_open:%Y-%m-%d %H:%M}  close={last.close}")
    print(f"unexpected gaps: {[g for g in boot.gaps if not g.expected]}")
    print(f"spread (points): {feed.current_spread_points()}")
    for _ in range(args.polls):
        time.sleep(2)
        update = feed.poll()
        print(f"poll: {update.status.value} new={len(update.candles)} {update.message}")
        warning = provider.offset_warning()
        if warning:
            print(f"WARNING: {warning}")
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
