from datetime import timedelta
from pathlib import Path

import pytest

from fakes import utc
from xauassist.config.models import ConfigError, FeedConfig
from xauassist.data.backoff import Backoff
from xauassist.data.csv_loader import load_candles_csv
from xauassist.data.errors import DataFormatError
from xauassist.data.gaps import find_gaps
from xauassist.domain.enums import Timeframe

CFG = FeedConfig()
M5 = Timeframe.M5


def series(start: tuple[int, int, int, int, int], n: int) -> list:  # type: ignore[type-arg]
    t = utc(*start)
    return [t + timedelta(minutes=5 * i) for i in range(n)]


# ---- gaps ----


def test_contiguous_has_no_gaps() -> None:
    assert find_gaps(series((2026, 1, 6, 10, 0), 50), M5, CFG) == []


def test_missing_candles_mid_session_are_unexpected() -> None:
    times = series((2026, 1, 6, 10, 0), 10)
    del times[4:7]  # three candles missing -> 20 minutes of data gone
    (gap,) = find_gaps(times, M5, CFG)
    assert not gap.expected and gap.missing_bars == 3 and gap.reason == "missing candles"


def test_short_hole_at_any_hour_outside_break_window_is_unexpected() -> None:
    for hour in (3, 8, 12, 17):
        times = [utc(2026, 1, 6, hour, 0), utc(2026, 1, 6, hour, 20)]  # 3 candles missing
        (gap,) = find_gaps(times, M5, CFG)
        assert not gap.expected, hour
    times = [utc(2026, 1, 6, 15, 0), utc(2026, 1, 6, 16, 5)]  # a full hour missing mid-session
    assert not find_gaps(times, M5, CFG)[0].expected


def test_break_window_crosses_midnight_and_is_configurable() -> None:
    times = [utc(2026, 1, 6, 23, 55), utc(2026, 1, 7, 0, 55)]  # server-time style break 00:00-01:00
    assert find_gaps(times, M5, CFG)[0].expected
    narrow = FeedConfig(daily_break_window_start="22:00", daily_break_window_end="23:30")
    assert not find_gaps(times, M5, narrow)[0].expected


def test_break_longer_than_max_is_unexpected_even_inside_window() -> None:
    times = [utc(2026, 1, 6, 21, 55), utc(2026, 1, 6, 23, 55)]  # 2 h missing
    assert not find_gaps(times, M5, CFG)[0].expected


def test_invalid_break_window_config() -> None:
    with pytest.raises(ConfigError):
        FeedConfig(daily_break_window_start="25:00")
    with pytest.raises(ConfigError):
        FeedConfig(daily_break_window_end="noon")


def test_daily_break_is_expected() -> None:
    # XAUUSD typically pauses ~1h at 22:00 UTC: last candle 21:55, next 23:00
    times = [utc(2026, 1, 6, 21, 55), utc(2026, 1, 6, 23, 0)]
    (gap,) = find_gaps(times, M5, CFG)
    assert gap.expected and gap.reason == "daily break"


def test_weekend_is_expected() -> None:
    times = [utc(2026, 1, 2, 21, 55), utc(2026, 1, 4, 23, 0)]  # Fri 21:55 -> Sun 23:00
    (gap,) = find_gaps(times, M5, CFG)
    assert gap.expected and gap.reason == "weekend"


def test_long_gap_not_spanning_saturday_is_unexpected() -> None:
    times = [utc(2026, 1, 6, 10, 0), utc(2026, 1, 7, 10, 0)]  # a whole Tue-Wed day missing
    (gap,) = find_gaps(times, M5, CFG)
    assert not gap.expected


def test_weekend_longer_than_limit_is_unexpected() -> None:
    times = [utc(2026, 1, 2, 21, 55), utc(2026, 1, 8, 0, 0)]  # nearly a week
    (gap,) = find_gaps(times, M5, CFG)
    assert not gap.expected


def test_misaligned_times_are_unexpected() -> None:
    times = [utc(2026, 1, 6, 10, 0), utc(2026, 1, 6, 10, 3)]
    (gap,) = find_gaps(times, M5, CFG)
    assert not gap.expected and gap.reason == "misaligned candle times"


def test_non_increasing_times_raise() -> None:
    with pytest.raises(ValueError):
        find_gaps([utc(2026, 1, 6, 10, 5), utc(2026, 1, 6, 10, 0)], M5, CFG)


# ---- backoff ----


def test_backoff_doubles_up_to_max_and_resets() -> None:
    b = Backoff(2.0, 10.0)
    assert [b.next_delay() for _ in range(5)] == [2.0, 4.0, 8.0, 10.0, 10.0]
    b.reset()
    assert b.next_delay() == 2.0


def test_backoff_validates() -> None:
    with pytest.raises(ValueError):
        Backoff(5.0, 1.0)


# ---- csv ----

MT5_EXPORT = (
    "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
    "2026.01.06\t10:05:00\t2700.50\t2701.20\t2700.10\t2700.90\t312\t0\t21\n"
    "2026.01.06\t10:00:00\t2700.00\t2700.80\t2699.90\t2700.50\t280\t0\t20\n"  # out of order
)


def test_loads_mt5_export_sorted_and_converts_server_time(tmp_path: Path) -> None:
    p = tmp_path / "XAUUSD_M5.csv"
    p.write_text(MT5_EXPORT)
    candles = load_candles_csv(p, utc_offset_hours=2.0)
    assert [c.time_open for c in candles] == [utc(2026, 1, 6, 8, 0), utc(2026, 1, 6, 8, 5)]
    assert candles[0].close == 2700.50 and candles[0].tick_volume == 280
    assert candles[1].spread_points == 21.0 and all(c.is_closed for c in candles)


def test_loads_simple_iso_csv(tmp_path: Path) -> None:
    p = tmp_path / "d.csv"
    p.write_text(
        "time,open,high,low,close,tick_volume\n"
        "2026-01-06T10:00:00,2700,2701,2699,2700.5,10\n"
        "2026-01-06T10:00:00,2700,2702,2699,2701.0,11\n"  # duplicate timestamp: last wins
    )
    (c,) = load_candles_csv(p)
    assert c.high == 2702 and c.close == 2701.0


@pytest.mark.parametrize(
    "content",
    [
        "time,open,high,low\n2026-01-06T10:00:00,1,2,0\n",  # missing close
        "time,open,high,low,close\n2026-01-06T10:00:00,1,x,0,1\n",  # non-numeric
        "time,open,high,low,close\nnot-a-time,1,2,0,1\n",  # bad timestamp
        "time,open,high,low,close\n2026-01-06T10:00:00,1,0,2,1\n",  # high < low
        "time,open,high,low,close\n",  # no rows
    ],
)
def test_bad_csv_raises_data_format_error(tmp_path: Path, content: str) -> None:
    p = tmp_path / "bad.csv"
    p.write_text(content)
    with pytest.raises(DataFormatError):
        load_candles_csv(p)


def test_missing_csv_file() -> None:
    with pytest.raises(DataFormatError):
        load_candles_csv("/nonexistent/file.csv")
