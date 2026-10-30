import pytest

from helpers import (
    SellSeries,
    candle,
    make_config,
    mirror,
    run,
    scenario_a_sell,
    scenario_b_sell,
)
from xauassist.config.models import ScenarioAConfig, ScenarioBConfig, TrendConfig, ZoneConfig
from xauassist.domain.enums import Direction, Pattern, Scenario, ZoneMode
from xauassist.domain.signal import Signal
from xauassist.strategy.engine import StrategyEngine
from xauassist.strategy.errors import IncompleteCandleError, OutOfOrderCandleError


def only(signals: list[Signal]) -> Signal:
    assert len(signals) == 1, signals
    return signals[0]


# ---------------- Scenario A ----------------


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [("engulfing", Pattern.BEARISH_ENGULFING), ("pin", Pattern.BEARISH_PIN_BAR)],
)
def test_scenario_a_sell(pattern: str, expected: Pattern) -> None:
    candles = scenario_a_sell(pattern)
    signals = run(candles)
    sig = only(signals)
    assert sig.direction is Direction.SELL
    assert sig.scenario is Scenario.A
    assert sig.patterns == (expected,)
    assert sig.bar_open_time == candles[-1].time_open
    assert sig.entry_price == candles[-1].close
    assert sig.time == candles[-1].time_open + (candles[1].time_open - candles[0].time_open)
    assert "below EMA20" in sig.reason


@pytest.mark.parametrize("pattern", ["engulfing", "pin"])
def test_scenario_a_buy_is_exact_mirror(pattern: str) -> None:
    sig = only(run(mirror(scenario_a_sell(pattern))))
    assert sig.direction is Direction.BUY
    assert sig.scenario is Scenario.A
    assert "above EMA20" in sig.reason


def test_scenario_a_no_signal_without_confirmation_pattern() -> None:
    candles = scenario_a_sell("engulfing")
    ma = only(run(candles)).ma_value
    # Same touch of the zone, but a small balanced bearish candle: neither pin bar nor engulfing.
    candles[-1] = candle(len(candles) - 1, ma - 0.1, ma, ma - 0.4, ma - 0.3)
    assert run(candles) == []


def test_scenario_a_no_signal_when_retest_stops_far_from_the_ma() -> None:
    s = SellSeries()
    for _ in range(3):  # weak rally, stays > 1 below the zone
        s.add(s.last.close, s.last.close + 0.4, s.last.close - 0.1, s.last.close + 0.3)
    prev = s.last
    s.add(prev.close + 0.1, prev.close + 0.2, prev.open - 0.4, prev.open - 0.2)  # engulfing
    assert run(s.candles) == []
    # Control: the same candles do signal once the zone is wide enough to include the retest.
    wide = make_config(zone=ZoneConfig(mode=ZoneMode.PRICE, value=5.0))
    last = run(s.candles, wide)[-1]
    assert last.bar_open_time == s.last.time_open and last.scenario is Scenario.A


def test_scenario_a_no_signal_when_ma_not_sloping_down() -> None:
    candles = scenario_a_sell("engulfing")
    assert only(run(candles))
    # Disabling the slope filter must not remove the signal; demanding a big slope must.
    strict = make_config(trend=TrendConfig(min_slope=5.0))
    assert run(candles, strict) == []


def test_scenario_a_disabled() -> None:
    assert run(scenario_a_sell(), make_config(scenario_a=ScenarioAConfig(enabled=False))) == []


def test_scenario_a_close_above_ma_is_not_a_retest() -> None:
    candles = scenario_a_sell("engulfing")
    last = candles[-1]
    # Keep it bearish but close above the MA by pushing everything up; (open=close+0.1)
    candles[-1] = candle(
        len(candles) - 1, last.high + 0.5, last.high + 0.6, last.high + 0.2, last.high + 0.3
    )
    assert run(candles) == []


def test_zone_in_atr_mode_also_works() -> None:
    from xauassist.config.models import StrategyConfig

    sig = only(run(scenario_a_sell("engulfing"), StrategyConfig()))
    assert sig.scenario is Scenario.A


# ---------------- Scenario B ----------------


def test_scenario_b_sell_only_after_pullback_confirmation() -> None:
    candles, confirm = scenario_b_sell()
    signals = run(candles)
    sig = only(signals)
    assert sig.scenario is Scenario.B
    assert sig.direction is Direction.SELL
    assert sig.bar_open_time == candles[confirm].time_open
    assert "broke above" in sig.reason
    # Nothing at or before the breakout/pullback bars: the signal needs the confirmation candle.
    assert run(candles[:confirm]) == []


def test_scenario_b_buy_is_exact_mirror() -> None:
    candles, confirm = scenario_b_sell()
    sig = only(run(mirror(candles)))
    assert sig.direction is Direction.BUY
    assert sig.scenario is Scenario.B
    assert sig.bar_open_time == candles[confirm].time_open
    assert "broke below" in sig.reason


def test_scenario_b_pullback_within_window_but_late() -> None:
    candles, _ = scenario_b_sell(hover_bars=6)
    sig = only(run(candles))
    assert sig.scenario is Scenario.B


def test_scenario_b_expires_after_max_pullback_bars() -> None:
    candles, _ = scenario_b_sell(hover_bars=12)  # confirmation lands > 10 bars after the break
    assert run(candles) == []


def test_scenario_b_window_is_configurable() -> None:
    candles, _ = scenario_b_sell(hover_bars=12)
    cfg = make_config(scenario_b=ScenarioBConfig(max_pullback_bars=20))
    assert only(run(candles, cfg))


def test_scenario_b_requires_close_back_on_trend_side_by_default() -> None:
    candles, confirm = scenario_b_sell()
    ma = only(run(candles)).ma_value
    prev = candles[confirm - 1]
    # Bearish engulfing that reaches the zone but closes ~1 above the MA (price never re-enters).
    candles[confirm] = candle(confirm, prev.close + 0.1, prev.close + 0.3, ma + 0.2, prev.open)
    assert run(candles) == []
    relaxed = make_config(scenario_b=ScenarioBConfig(require_close_reenter=False))
    assert only(run(candles, relaxed)).scenario is Scenario.B


def test_scenario_b_disabled() -> None:
    candles, _ = scenario_b_sell()
    assert run(candles, make_config(scenario_b=ScenarioBConfig(enabled=False))) == []


# ---------------- data integrity ----------------


def test_incomplete_candle_is_rejected_and_ignored() -> None:
    engine = StrategyEngine(make_config(), "XAUUSD")
    forming = candle(0, 2700, 2701, 2699, 2700.5, closed=False)
    with pytest.raises(IncompleteCandleError):
        engine.on_candle_closed(forming)
    assert engine.last_candle_time is None


def test_duplicate_candle_is_ignored_and_older_candle_raises() -> None:
    engine = StrategyEngine(make_config(), "XAUUSD")
    c1, c2 = candle(1, 2700, 2701, 2699, 2700.5), candle(2, 2700.5, 2701, 2699, 2700)
    engine.on_candle_closed(c1)
    engine.on_candle_closed(c2)
    assert engine.on_candle_closed(c2) == []
    with pytest.raises(OutOfOrderCandleError):
        engine.on_candle_closed(c1)


def test_no_signals_during_warmup() -> None:
    candles = scenario_a_sell()
    # Keep only the last 40 candles: far fewer than 5 x 20 bars of history.
    assert run(candles[-40:]) == []


def test_no_repainting_prefix_runs_agree_with_full_run() -> None:
    """Signals for bar t must be identical whether or not later candles exist."""
    for candles in (scenario_a_sell(), scenario_b_sell()[0], mirror(scenario_b_sell()[0])):
        full = [(s.key, s.entry_price, s.ma_value) for s in run(candles)]
        for cut in range(len(candles) - 12, len(candles) + 1):
            part = [(s.key, s.entry_price, s.ma_value) for s in run(candles[:cut])]
            cutoff = candles[cut - 1].time_open
            assert part == [t for t in full if t[0].bar_open_time <= cutoff]
