"""Trade execution simulator: turns a signal into a scored Trade, with no look-ahead.

Conventions (all configurable in BacktestConfig; printed in every report):
  - Candles are BID prices. A SELL fills at the bid; a BUY fills at the ask = bid + spread.
  - Entry is the OPEN of the candle after the confirmation candle (the signal only exists once
    the confirmation candle has closed). If a market gap separates the two, the signal is skipped.
  - Stop loss: beyond the confirmation candle's extreme (+ sl_buffer).
    Take profit: r_multiple x risk.
  - A SELL is closed by buying at the ask, so its stop/target are tested against bid + spread; a BUY
    is closed at the bid. The spread of each candle is that candle's recorded spread, or the
    assumed spread if it recorded none.
  - If stop and target are both reachable in one candle, the STOP is assumed to hit first
    (pessimistic default; `same_candle = TARGET_FIRST` gives the optimistic bound).
  - Stops that gap past their level fill at the (worse) open; targets always fill at their level.
  - Slippage is adverse and applies to the entry and to stop fills.
  - No exit within max_hold_bars candles: closed at the last candle's close (TIME).
  - MFE/MAE are measured over the same horizon with executable prices, whatever SL/TP did.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from xauassist.backtest.models import ExitReason, SkippedSignal, SkipReason, Trade
from xauassist.config.models import BacktestConfig
from xauassist.domain.candle import Candle
from xauassist.domain.enums import Direction, SameCandleRule
from xauassist.domain.signal import Signal


@dataclass(frozen=True, slots=True)
class ExecutionParams:
    point: float
    assumed_spread: float  # price units
    slippage: float  # price units
    sl_buffer: float
    r_multiple: float
    max_hold_bars: int
    min_risk: float
    stop_first: bool = True

    @classmethod
    def from_config(cls, cfg: BacktestConfig) -> ExecutionParams:
        return cls(
            point=cfg.point_size,
            assumed_spread=cfg.assumed_spread_points * cfg.point_size,
            slippage=cfg.slippage_points * cfg.point_size,
            sl_buffer=cfg.sl_buffer,
            r_multiple=cfg.r_multiple,
            max_hold_bars=cfg.max_hold_bars,
            min_risk=cfg.min_risk,
            stop_first=cfg.same_candle is SameCandleRule.STOP_FIRST,
        )


def candle_spread(candle: Candle, p: ExecutionParams) -> tuple[float, bool]:
    """(spread in price units, whether it is the assumed value)."""
    if candle.spread_points > 0:
        return candle.spread_points * p.point, False
    return p.assumed_spread, True


def simulate_trade(
    signal: Signal, signal_index: int, candles: Sequence[Candle], p: ExecutionParams
) -> Trade | SkippedSignal:
    """Score `signal` (generated on candles[signal_index]) using only candles after it."""
    n = len(candles)
    entry_index = signal_index + 1
    if entry_index >= n:
        return SkippedSignal(signal, signal_index, SkipReason.NO_NEXT_BAR)
    confirm, entry_bar = candles[signal_index], candles[entry_index]
    if entry_bar.time_open != signal.time:
        return SkippedSignal(signal, signal_index, SkipReason.GAP_BEFORE_ENTRY)

    sell = signal.direction is Direction.SELL
    entry_spread, assumed = candle_spread(entry_bar, p)
    if sell:
        entry = entry_bar.open - p.slippage
        stop = confirm.high + p.sl_buffer
        risk = stop - entry
    else:
        entry = entry_bar.open + entry_spread + p.slippage
        stop = confirm.low - p.sl_buffer
        risk = entry - stop
    if risk <= 0 or risk < p.min_risk:
        return SkippedSignal(signal, signal_index, SkipReason.INVALID_RISK)
    target = entry - p.r_multiple * risk if sell else entry + p.r_multiple * risk

    horizon_end = entry_index + p.max_hold_bars - 1  # last candle of the full horizon
    last_available = min(horizon_end, n - 1)
    exit_index = -1
    exit_price = 0.0
    reason = ExitReason.TIME
    mfe = mae = 0.0

    for j in range(entry_index, last_available + 1):
        c = candles[j]
        s, _ = candle_spread(c, p)
        if sell:  # closing a short = buying at the ask
            ask_open, ask_high, ask_low = c.open + s, c.high + s, c.low + s
            mfe = max(mfe, entry - ask_low)
            mae = max(mae, ask_high - entry)
            if exit_index < 0:
                stop_hit, target_hit = ask_high >= stop, ask_low <= target
                if stop_hit and not (target_hit and not p.stop_first):
                    exit_index, reason = j, ExitReason.SL
                    exit_price = max(stop, ask_open) + p.slippage
                elif target_hit:
                    exit_index, reason, exit_price = j, ExitReason.TP, target
        else:  # closing a long = selling at the bid
            mfe = max(mfe, c.high - entry)
            mae = max(mae, entry - c.low)
            if exit_index < 0:
                stop_hit, target_hit = c.low <= stop, c.high >= target
                if stop_hit and not (target_hit and not p.stop_first):
                    exit_index, reason = j, ExitReason.SL
                    exit_price = min(stop, c.open) - p.slippage
                elif target_hit:
                    exit_index, reason, exit_price = j, ExitReason.TP, target

    if exit_index < 0:
        if last_available < horizon_end:  # ran out of data before the horizon completed
            return SkippedSignal(signal, signal_index, SkipReason.UNRESOLVED)
        last = candles[last_available]
        s, _ = candle_spread(last, p)
        exit_index, reason = last_available, ExitReason.TIME
        exit_price = last.close + s if sell else last.close

    pnl = entry - exit_price if sell else exit_price - entry
    return Trade(
        signal=signal,
        signal_index=signal_index,
        entry_index=entry_index,
        entry_time=entry_bar.time_open,
        entry_price=entry,
        stop_loss=stop,
        take_profit=target,
        risk=risk,
        exit_index=exit_index,
        exit_time=candles[exit_index].time_open,
        exit_price=exit_price,
        exit_reason=reason,
        pnl=pnl,
        r_result=pnl / risk,
        mfe=max(mfe, 0.0),
        mae=max(mae, 0.0),
        bars_held=exit_index - entry_index + 1,
        spread=entry_spread,
        assumed_spread=assumed,
        last_index_used=max(exit_index, last_available),
    )
