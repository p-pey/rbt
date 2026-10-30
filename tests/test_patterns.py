import pytest

from helpers import candle
from xauassist.config.models import EngulfingConfig, PinBarConfig
from xauassist.domain.candle import Candle
from xauassist.patterns.engulfing import is_bearish_engulfing, is_bullish_engulfing
from xauassist.patterns.pin_bar import is_bearish_pin_bar, is_bullish_pin_bar

PIN = PinBarConfig()
ENG = EngulfingConfig()


def test_bearish_pin_bar() -> None:
    # body 0.4, upper 1.6 (4x), lower 0.1, bearish close
    assert is_bearish_pin_bar(candle(0, 2700.0, 2702.0, 2699.5, 2699.6), PIN)


def test_bearish_pin_bar_bullish_close_but_clear_rejection() -> None:
    # closes up but in the lowest third of the range: still a rejection
    assert is_bearish_pin_bar(candle(0, 2699.8, 2702.0, 2699.7, 2700.0), PIN)


def test_bearish_pin_bar_rejected_when_closing_high() -> None:
    # long upper wick but bullish close near the high -> wick is not the dominant feature
    assert not is_bearish_pin_bar(candle(0, 2700.0, 2702.0, 2699.9, 2701.5), PIN)


def test_bearish_pin_bar_wick_not_long_enough() -> None:
    # upper 0.5 < 2 x body 0.4? 0.5 < 0.8
    assert not is_bearish_pin_bar(candle(0, 2700.0, 2700.5, 2699.0, 2699.6), PIN)


def test_bearish_pin_bar_large_lower_wick_rejected() -> None:
    assert not is_bearish_pin_bar(candle(0, 2700.0, 2702.0, 2697.0, 2699.5), PIN)


def test_bullish_pin_bar() -> None:
    assert is_bullish_pin_bar(candle(0, 2700.0, 2700.5, 2698.0, 2700.4), PIN)


def test_bullish_pin_bar_not_bearish() -> None:
    assert not is_bearish_pin_bar(candle(0, 2700.0, 2700.5, 2698.0, 2700.4), PIN)
    assert not is_bullish_pin_bar(candle(0, 2700.0, 2702.0, 2699.5, 2699.6), PIN)


def test_doji_zero_body_with_long_upper_wick_is_safe_and_valid() -> None:
    doji = candle(0, 2700.0, 2702.0, 2699.9, 2700.0)  # body exactly 0
    assert is_bearish_pin_bar(doji, PIN)


def test_zero_range_candle_is_rejected_without_error() -> None:
    flat = candle(0, 2700.0, 2700.0, 2700.0, 2700.0)
    assert not is_bearish_pin_bar(flat, PIN)
    assert not is_bullish_pin_bar(flat, PIN)


def test_wick_body_ratio_is_configurable() -> None:
    c = candle(0, 2700.0, 2701.0, 2699.4, 2699.6)  # body 0.4, upper 1.0 (2.5x), lower 0.2
    assert is_bearish_pin_bar(c, PIN)
    assert not is_bearish_pin_bar(c, PinBarConfig(wick_body_ratio=3.0))


def test_min_range_filter() -> None:
    c = candle(0, 2700.0, 2700.8, 2699.9, 2700.0)
    assert is_bearish_pin_bar(c, PIN)
    assert not is_bearish_pin_bar(c, PinBarConfig(min_range=2.0))


def test_candle_rejects_inconsistent_prices() -> None:
    with pytest.raises(ValueError):
        Candle(candle(0, 1, 1, 1, 1).time_open, 2700.0, 2699.0, 2698.0, 2700.0)


def test_bearish_engulfing() -> None:
    prev = candle(0, 2700.0, 2701.2, 2699.9, 2701.0)
    cur = candle(1, 2701.1, 2701.3, 2699.5, 2699.8)
    assert is_bearish_engulfing(prev, cur, ENG)
    assert not is_bullish_engulfing(prev, cur, ENG)


def test_bearish_engulfing_requires_full_body_cover() -> None:
    prev = candle(0, 2700.0, 2701.2, 2699.9, 2701.0)
    cur = candle(1, 2701.1, 2701.3, 2700.2, 2700.4)  # close above prev open
    assert not is_bearish_engulfing(prev, cur, ENG)


def test_engulfing_boundaries_strict_vs_non_strict() -> None:
    prev = candle(0, 2700.0, 2701.2, 2699.9, 2701.0)
    cur = candle(1, 2701.0, 2701.2, 2699.5, 2700.0)  # open == prev close, close == prev open
    assert is_bearish_engulfing(prev, cur, ENG)
    assert not is_bearish_engulfing(prev, cur, EngulfingConfig(strict=True))


def test_bullish_engulfing() -> None:
    prev = candle(0, 2701.0, 2701.2, 2699.9, 2700.0)
    cur = candle(1, 2699.9, 2701.6, 2699.8, 2701.3)
    assert is_bullish_engulfing(prev, cur, ENG)
    assert not is_bearish_engulfing(prev, cur, ENG)


def test_engulfing_same_colour_is_not_engulfing() -> None:
    prev = candle(0, 2700.0, 2701.2, 2699.9, 2701.0)
    cur = candle(1, 2699.8, 2702.0, 2699.7, 2701.8)
    assert not is_bullish_engulfing(prev, cur, ENG)
    assert not is_bearish_engulfing(prev, cur, ENG)
