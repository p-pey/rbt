"""Hand-computed checks of the trade simulator. Every expected number is worked out by hand."""

from datetime import timedelta

import pytest

from fakes import utc
from xauassist.backtest.execution import ExecutionParams, simulate_trade
from xauassist.backtest.models import ExitReason, SkippedSignal, SkipReason, Trade
from xauassist.config.models import BacktestConfig
from xauassist.domain.candle import Candle
from xauassist.domain.enums import Direction, Pattern, SameCandleRule, Scenario, Timeframe
from xauassist.domain.signal import Signal

T0 = utc(2026, 1, 6, 10, 0)
STEP = timedelta(minutes=5)


def bar(i: int, o: float, h: float, low: float, c: float, spread: float = 20.0) -> Candle:
    return Candle(T0 + STEP * i, o, h, low, c, 100, spread)


def signal_for(candle_index: int, direction: Direction, candles: list[Candle]) -> Signal:
    c = candles[candle_index]
    return Signal(
        direction=direction,
        scenario=Scenario.A,
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        bar_open_time=c.time_open,
        time=c.time_open + STEP,
        entry_price=c.close,
        ma_value=c.close,
        patterns=(
            Pattern.BEARISH_ENGULFING if direction is Direction.SELL else Pattern.BULLISH_ENGULFING,
        ),
        reason="test",
    )


def params(**kw: float) -> ExecutionParams:
    optimistic = kw.pop("same_candle_stop_first", 1) == 0
    cfg = BacktestConfig(
        **{"r_multiple": 2.0, "max_hold_bars": 4, **kw},  # type: ignore[arg-type]
        same_candle=SameCandleRule.TARGET_FIRST if optimistic else SameCandleRule.STOP_FIRST,
    )
    return ExecutionParams.from_config(cfg)


def run(direction: Direction, candles: list[Candle], p: ExecutionParams, idx: int = 0) -> Trade:
    out = simulate_trade(signal_for(idx, direction, candles), idx, candles, p)
    assert isinstance(out, Trade), out
    return out


# Confirmation candle 0: high 2702.00, low 2698.00.  Spread 20 points = 0.20.


def test_sell_hits_take_profit_with_spread_on_the_exit() -> None:
    candles = [
        bar(0, 2700, 2702.00, 2698.00, 2698.5),
        bar(1, 2698.4, 2699.0, 2697.0, 2697.5),  # entry at open 2698.40
        bar(2, 2697.5, 2698.0, 2693.0, 2694.0),  # ask_low = 2693.20
        bar(3, 2694, 2695, 2693, 2694),
        bar(4, 2694, 2695, 2693, 2694),
    ]
    t = run(Direction.SELL, candles, params())
    # SELL fills at the bid open: entry 2698.40; stop = 2702.00; risk = 3.60; target = 2698.40 - 7.20.
    assert t.entry_price == pytest.approx(2698.40) and t.stop_loss == pytest.approx(2702.00)
    assert t.risk == pytest.approx(3.60) and t.take_profit == pytest.approx(2691.20)
    # Closing a short buys at the ask: candle 2 ask_low = 2693.20 > 2691.20 -> no target yet.
    # Nothing hits within 4 candles -> TIME exit at candle 4 close + spread = 2694.20.
    assert t.exit_reason is ExitReason.TIME and t.exit_index == 4
    assert t.exit_price == pytest.approx(2694.20)
    assert t.pnl == pytest.approx(2698.40 - 2694.20) and t.r_result == pytest.approx(4.2 / 3.6)
    assert t.is_win and t.bars_held == 4


def test_sell_target_needs_bid_low_plus_spread_to_reach_it() -> None:
    candles = [
        bar(0, 2700, 2702.00, 2698.00, 2698.5),
        bar(1, 2698.4, 2699.0, 2697.0, 2697.5),
        bar(2, 2697.5, 2698.0, 2691.10, 2692.0),  # bid low 2691.10 -> ask 2691.30 > 2691.20
        bar(3, 2692, 2693, 2690.90, 2691),  # bid low 2690.90 -> ask 2691.10 <= 2691.20 -> TP
    ]
    t = run(Direction.SELL, candles, params())
    assert t.exit_reason is ExitReason.TP and t.exit_index == 3
    assert t.exit_price == pytest.approx(2691.20) and t.r_result == pytest.approx(2.0)


def test_sell_stop_triggers_on_ask_high_not_bid_high() -> None:
    candles = [
        bar(0, 2700, 2702.00, 2698.00, 2698.5),
        bar(1, 2698.4, 2701.85, 2697.0, 2698.0),  # bid high 2701.85 + 0.20 = ask 2702.05 >= stop
    ]
    t = run(Direction.SELL, candles, params())
    assert t.exit_reason is ExitReason.SL and t.exit_index == 1
    assert t.exit_price == pytest.approx(2702.00)
    assert t.pnl == pytest.approx(2698.40 - 2702.00) and t.r_result == pytest.approx(-1.0)
    assert not t.is_win


def test_stop_and_target_in_the_same_candle_counts_as_stop_first() -> None:
    candles = [
        bar(0, 2700, 2702.00, 2698.00, 2698.5),
        bar(1, 2698.4, 2703.0, 2690.0, 2698.0),  # reaches both 2702 stop and 2691.2 target
    ]
    t = run(Direction.SELL, candles, params())
    assert t.exit_reason is ExitReason.SL and t.r_result == pytest.approx(-1.0)


def test_buy_fills_at_ask_and_exits_at_bid() -> None:
    candles = [
        bar(0, 2700, 2702.00, 2698.00, 2701.5),
        bar(1, 2701.6, 2702.5, 2701.0, 2702.0),  # entry: bid open 2701.60 + 0.20 = 2701.80
        bar(2, 2702.0, 2708.5, 2701.5, 2708.0),  # bid high 2708.50
    ]
    t = run(Direction.BUY, candles, params(max_hold_bars=2))
    # stop = 2698.00 (candle low); risk = 3.80; target = 2701.80 + 7.60 = 2709.40.
    assert t.entry_price == pytest.approx(2701.80) and t.stop_loss == pytest.approx(2698.00)
    assert t.risk == pytest.approx(3.80) and t.take_profit == pytest.approx(2709.40)
    # Bid high 2708.50 never reaches 2709.40: time exit at the last candle's BID close 2708.00.
    assert t.exit_reason is ExitReason.TIME and t.exit_price == pytest.approx(2708.00)
    assert t.pnl == pytest.approx(2708.00 - 2701.80)


def test_buy_target_and_stop_levels_use_bid_prices() -> None:
    hit = [
        bar(0, 2700, 2702.00, 2698.00, 2701.5),
        bar(1, 2701.6, 2702.5, 2701.0, 2702.0),
        bar(2, 2702.0, 2709.45, 2701.5, 2709.0),  # bid high >= 2709.40
    ]
    t = run(Direction.BUY, hit, params())
    assert t.exit_reason is ExitReason.TP and t.exit_price == pytest.approx(2709.40)
    assert t.r_result == pytest.approx(2.0)
    stopped = [
        bar(0, 2700, 2702.00, 2698.00, 2701.5),
        bar(1, 2701.6, 2702.5, 2697.95, 2702.0),  # bid low <= 2698.00
    ]
    s = run(Direction.BUY, stopped, params())
    assert s.exit_reason is ExitReason.SL and s.exit_price == pytest.approx(2698.00)
    assert s.pnl == pytest.approx(2698.00 - 2701.80)


def test_stop_gapping_past_its_level_fills_at_the_worse_open() -> None:
    candles = [
        bar(0, 2700, 2702.00, 2698.00, 2698.5),
        bar(1, 2698.4, 2699.0, 2697.0, 2698.0),
        bar(2, 2704.0, 2705.0, 2703.0, 2704.5),  # gaps above the 2702 stop: ask open 2704.20
    ]
    t = run(Direction.SELL, candles, params())
    assert t.exit_reason is ExitReason.SL and t.exit_price == pytest.approx(2704.20)
    assert t.r_result == pytest.approx((2698.40 - 2704.20) / 3.60)  # worse than -1R


def test_slippage_is_adverse_on_entry_and_stop() -> None:
    candles = [
        bar(0, 2700, 2702.00, 2698.00, 2698.5),
        bar(1, 2698.4, 2701.9, 2697.0, 2698.0),
    ]
    t = run(Direction.SELL, candles, params(slippage_points=50))  # 0.50
    assert t.entry_price == pytest.approx(2697.90)  # sold 0.50 lower
    assert t.exit_reason is ExitReason.SL and t.exit_price == pytest.approx(2702.50)
    assert t.risk == pytest.approx(2702.00 - 2697.90)


def test_sl_buffer_widens_the_stop() -> None:
    candles = [bar(0, 2700, 2702.00, 2698.00, 2698.5), bar(1, 2698.4, 2699, 2697, 2698)]
    t = run(Direction.SELL, candles, params(sl_buffer=1.0, max_hold_bars=1))
    assert t.stop_loss == pytest.approx(2703.00) and t.risk == pytest.approx(4.60)


def test_mfe_and_mae_are_independent_of_exit() -> None:
    candles = [
        bar(0, 2700, 2702.00, 2698.00, 2698.5),
        bar(1, 2698.4, 2701.85, 2697.0, 2698.0),  # stopped here (ask high 2702.05)
        bar(2, 2698.0, 2699.0, 2690.0, 2691.0),  # later move still counts for MFE
        bar(3, 2691, 2692, 2690, 2691),
    ]
    t = run(Direction.SELL, candles, params())
    assert t.exit_reason is ExitReason.SL
    assert t.mfe == pytest.approx(2698.40 - (2690.0 + 0.20))  # best executable move
    assert t.mae == pytest.approx((2701.85 + 0.20) - 2698.40)


def test_assumed_spread_used_when_candle_has_none() -> None:
    candles = [
        bar(0, 2700, 2702, 2698, 2701.5, spread=0.0),
        bar(1, 2701.6, 2702.5, 2701.0, 2702.0, spread=0.0),
    ]
    t = run(Direction.BUY, candles, params(assumed_spread_points=30, max_hold_bars=1))
    assert t.assumed_spread and t.spread == pytest.approx(0.30)
    assert t.entry_price == pytest.approx(2701.90)


def test_skips() -> None:
    candles = [bar(0, 2700, 2702, 2698, 2698.5)]
    out = simulate_trade(signal_for(0, Direction.SELL, candles), 0, candles, params())
    assert isinstance(out, SkippedSignal) and out.reason is SkipReason.NO_NEXT_BAR

    gap = [bar(0, 2700, 2702, 2698, 2698.5), bar(30, 2698, 2699, 2697, 2698)]
    out = simulate_trade(signal_for(0, Direction.SELL, gap), 0, gap, params())
    assert isinstance(out, SkippedSignal) and out.reason is SkipReason.GAP_BEFORE_ENTRY

    # Entry candle opens above the SELL stop: stop distance is negative.
    inverted = [bar(0, 2700, 2702, 2698, 2698.5), bar(1, 2703, 2704, 2702, 2703)]
    out = simulate_trade(signal_for(0, Direction.SELL, inverted), 0, inverted, params())
    assert isinstance(out, SkippedSignal) and out.reason is SkipReason.INVALID_RISK

    short = [bar(0, 2700, 2702, 2698, 2698.5), bar(1, 2698.4, 2699, 2697, 2698)]
    out = simulate_trade(
        signal_for(0, Direction.SELL, short), 0, short, params()
    )  # horizon 4 > data
    assert isinstance(out, SkippedSignal) and out.reason is SkipReason.UNRESOLVED

    wide = [bar(0, 2700, 2702, 2698, 2698.5), bar(1, 2698.4, 2699, 2697, 2698)]
    out = simulate_trade(signal_for(0, Direction.SELL, wide), 0, wide, params(min_risk=10.0))
    assert isinstance(out, SkippedSignal) and out.reason is SkipReason.INVALID_RISK


def test_last_index_used_covers_the_full_horizon_even_after_an_early_exit() -> None:
    candles = [bar(i, 2698.4, 2699.0, 2697.0, 2698.0) for i in range(8)]
    candles[0] = bar(0, 2700, 2702.00, 2698.00, 2698.5)
    candles[1] = bar(1, 2698.4, 2701.85, 2697.0, 2698.0)  # stopped on the entry candle
    t = run(Direction.SELL, candles, params(max_hold_bars=4))
    assert t.exit_index == 1 and t.last_index_used == 4  # MFE/MAE read candles up to index 4


def test_pure_function_of_data_up_to_the_horizon() -> None:
    """Candles after the horizon cannot change a trade (no look-ahead)."""
    base = [bar(0, 2700, 2702.00, 2698.00, 2698.5), bar(1, 2698.4, 2699, 2697, 2698)]
    base += [bar(i, 2698, 2699, 2697, 2698) for i in range(2, 6)]
    a = run(Direction.SELL, base, params(max_hold_bars=4))
    changed = [*base, bar(6, 9999, 10000, 1, 5000)]
    b = run(Direction.SELL, changed, params(max_hold_bars=4))
    assert a == b


def test_same_candle_rule_brackets_the_result() -> None:
    both = [
        bar(0, 2700, 2702.00, 2698.00, 2698.5),
        bar(1, 2698.4, 2703.0, 2690.0, 2698.0),  # reaches both the 2702 stop and the 2691.2 target
    ]
    pessimistic = run(Direction.SELL, both, params(max_hold_bars=1))
    optimistic = run(Direction.SELL, both, params(max_hold_bars=1, same_candle_stop_first=0))
    assert pessimistic.exit_reason is ExitReason.SL and pessimistic.r_result == pytest.approx(-1.0)
    assert optimistic.exit_reason is ExitReason.TP and optimistic.r_result == pytest.approx(2.0)
    buy = [bar(0, 2700, 2702.00, 2698.00, 2701.5), bar(1, 2701.6, 2712.0, 2697.0, 2702.0)]
    assert run(Direction.BUY, buy, params(max_hold_bars=1)).exit_reason is ExitReason.SL
    assert (
        run(Direction.BUY, buy, params(max_hold_bars=1, same_candle_stop_first=0)).exit_reason
        is ExitReason.TP
    )
