import pytest

from helpers import T0, candle
from xauassist.config.models import MaConfig, TrendConfig
from xauassist.domain.bar import Bar
from xauassist.domain.enums import Direction, Timeframe
from xauassist.trend.htf import HtfAggregator, HtfTrendTracker
from xauassist.trend.trend import slope_at, trend_matches


def bars(closes: list[float], mas: list[float]) -> list[Bar]:
    return [
        Bar(candle(i, c, c + 0.1, c - 0.1, c), m, None)
        for i, (c, m) in enumerate(zip(closes, mas, strict=True))
    ]


def test_bearish_trend_price_below_and_ma_falling() -> None:
    seq = bars([2690, 2689, 2688, 2687, 2686, 2685], [2700, 2699, 2698, 2697, 2696, 2695])
    s = slope_at(seq, 5, 5)
    assert s == pytest.approx(-1.0)
    assert trend_matches(Direction.SELL, seq[5], s, TrendConfig())
    assert not trend_matches(Direction.BUY, seq[5], s, TrendConfig())


def test_bullish_trend_price_above_and_ma_rising() -> None:
    seq = bars([2710, 2711, 2712, 2713, 2714, 2715], [2700, 2701, 2702, 2703, 2704, 2705])
    s = slope_at(seq, 5, 5)
    assert trend_matches(Direction.BUY, seq[5], s, TrendConfig())
    assert not trend_matches(Direction.SELL, seq[5], s, TrendConfig())


def test_price_below_but_ma_rising_is_not_bearish_trend() -> None:
    seq = bars([2690, 2691, 2692, 2693, 2694, 2695], [2700, 2701, 2702, 2703, 2704, 2705])
    assert not trend_matches(Direction.SELL, seq[5], slope_at(seq, 5, 5), TrendConfig())


def test_slope_filter_can_be_disabled() -> None:
    seq = bars([2690, 2691, 2692, 2693, 2694, 2695], [2700, 2701, 2702, 2703, 2704, 2705])
    cfg = TrendConfig(slope_filter_enabled=False)
    assert trend_matches(Direction.SELL, seq[5], slope_at(seq, 5, 5), cfg)


def test_min_slope_threshold() -> None:
    seq = bars(
        [2690, 2689.9, 2689.8, 2689.7, 2689.6, 2689.5],
        [2700, 2699.9, 2699.8, 2699.7, 2699.6, 2699.5],
    )
    assert trend_matches(Direction.SELL, seq[5], slope_at(seq, 5, 5), TrendConfig())
    assert not trend_matches(
        Direction.SELL, seq[5], slope_at(seq, 5, 5), TrendConfig(min_slope=0.5)
    )


def test_slope_needs_enough_history() -> None:
    seq = bars([2690, 2689], [2700, 2699])
    assert slope_at(seq, 1, 5) is None
    assert not trend_matches(Direction.SELL, seq[1], None, TrendConfig())


def test_htf_aggregator_emits_only_when_bucket_complete() -> None:
    agg = HtfAggregator(Timeframe.M5, Timeframe.M15)
    # Buckets are aligned to the epoch; T0 is 2026-01-05 00:00 UTC, a 15-minute boundary.
    assert agg.update(candle(0, 2700, 2701, 2699, 2700.5)) == []
    assert agg.update(candle(1, 2700.5, 2703, 2700, 2702)) == []  # still forming: nothing visible
    out = agg.update(candle(2, 2702, 2702.5, 2698, 2699))
    assert len(out) == 1
    m15 = out[0]
    assert m15.time_open == T0
    assert (m15.open, m15.high, m15.low, m15.close) == (2700, 2703, 2698, 2699)
    assert m15.tick_volume == 300


def test_htf_aggregator_closes_short_bucket_on_gap() -> None:
    agg = HtfAggregator(Timeframe.M5, Timeframe.M15)
    agg.update(candle(0, 2700, 2701, 2699, 2700.5))
    out = agg.update(candle(3, 2700, 2701, 2699, 2700.2))  # next bucket starts; i=1,2 are missing
    assert len(out) == 1 and out[0].time_open == T0


def test_htf_tracker_bearish() -> None:
    tracker = HtfTrendTracker(Timeframe.M5, MaConfig(), TrendConfig(htf_enabled=True))
    price = 2800.0
    i = 0
    for _ in range(40 * 3):  # 40 M15 bars falling
        price -= 0.5
        tracker.update(candle(i, price + 0.5, price + 0.6, price - 0.1, price))
        i += 1
    assert tracker.allows(Direction.SELL)
    assert not tracker.allows(Direction.BUY)
    assert tracker.label() == "bearish"


def test_htf_tracker_not_ready_blocks() -> None:
    tracker = HtfTrendTracker(Timeframe.M5, MaConfig(), TrendConfig(htf_enabled=True))
    tracker.update(candle(0, 2700, 2701, 2699, 2700))
    assert not tracker.allows(Direction.SELL) and not tracker.allows(Direction.BUY)
