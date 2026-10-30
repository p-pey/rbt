import pytest

from xauassist.domain.enums import MaMethod
from xauassist.indicators.atr import AverageTrueRange
from xauassist.indicators.ma import MovingAverage, compute_ma


def test_sma20_exact() -> None:
    prices = [2700.0 + i * 0.5 for i in range(25)]
    out = compute_ma(prices, 20, MaMethod.SMA)
    assert out[:19] == [None] * 19
    assert out[19] == pytest.approx(sum(prices[:20]) / 20)
    assert out[24] == pytest.approx(sum(prices[5:25]) / 20)


def test_ema20_matches_manual_recursion() -> None:
    prices = [2700.0, 2701.5, 2699.0, 2703.0, 2704.5] + [2700.0 + (i % 7) for i in range(30)]
    alpha = 2 / 21
    expected = [prices[0]]
    for p in prices[1:]:
        expected.append(alpha * p + (1 - alpha) * expected[-1])
    out = compute_ma(prices, 20, MaMethod.EMA)
    assert out[:19] == [None] * 19
    for got, want in zip(out[19:], expected[19:], strict=True):
        assert got == pytest.approx(want)


def test_ema_constant_series_is_constant() -> None:
    out = compute_ma([2700.0] * 40, 20, MaMethod.EMA)
    assert out[-1] == pytest.approx(2700.0)


def test_ema_is_independent_of_far_history() -> None:
    """After ~5N bars of warm-up the start seed no longer matters (matches MT5 chart values)."""
    prices = [2700.0 + ((i * 7) % 13) * 0.3 for i in range(300)]
    full = compute_ma(prices, 20, MaMethod.EMA)[-1]
    short = compute_ma(prices[100:], 20, MaMethod.EMA)[-1]
    assert full is not None and short is not None
    assert full == pytest.approx(short, abs=1e-4)


def test_invalid_period() -> None:
    with pytest.raises(ValueError):
        MovingAverage(1, MaMethod.EMA)


def test_atr_wilder() -> None:
    atr = AverageTrueRange(3)
    assert atr.update(10, 8, 9) is None  # TR 2
    assert atr.update(11, 9, 10) is None  # TR 2
    assert atr.update(13, 10, 12) == pytest.approx((2 + 2 + 3) / 3)  # TR = max(3, 3, 0) = 3
    # TR = max(1, |12.5-12|, |11.5-12|) = 1 -> (7/3 * 2 + 1) / 3
    assert atr.update(12.5, 11.5, 12) == pytest.approx((7 / 3 * 2 + 1) / 3)
