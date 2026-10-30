"""Detection of missing candles, separating expected market closures from data holes.

Without a broker trading calendar a gap is classified by its shape and position:
- missing <= `daily_break_max_minutes` AND inside the daily-break time window -> expected
- gap spans a Saturday and <= `weekend_max_hours`                              -> expected
- anything else (including short holes at other times of day)                  -> unexpected
Limitations: a genuine hole inside the break window is indistinguishable from the break, and
holiday closures are reported as unexpected (the candles after them are flagged incomplete).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from xauassist.config.models import FeedConfig
from xauassist.domain.enums import Timeframe


@dataclass(frozen=True, slots=True)
class Gap:
    start: datetime  # open time of the candle before the gap
    end: datetime  # open time of the candle after the gap
    missing_bars: int
    expected: bool
    reason: str


def find_gaps(times: Sequence[datetime], timeframe: Timeframe, cfg: FeedConfig) -> list[Gap]:
    """Gaps between consecutive candle open times (UTC, strictly increasing)."""
    step = timedelta(minutes=timeframe.minutes)
    gaps: list[Gap] = []
    for prev, nxt in zip(times, times[1:], strict=False):
        delta = nxt - prev
        if delta <= timedelta(0):
            raise ValueError(f"candle times must be strictly increasing: {prev} -> {nxt}")
        if delta == step:
            continue
        missing_bars = max(0, delta // step - 1)
        if delta % step != timedelta(0):
            gaps.append(Gap(prev, nxt, missing_bars, False, "misaligned candle times"))
            continue
        missing = delta - step
        if missing <= timedelta(minutes=cfg.daily_break_max_minutes) and _in_break_window(
            prev + step, missing, cfg
        ):
            gaps.append(Gap(prev, nxt, missing_bars, True, "daily break"))
        elif _spans_saturday(prev + step, nxt) and missing <= timedelta(
            hours=cfg.weekend_max_hours
        ):
            gaps.append(Gap(prev, nxt, missing_bars, True, "weekend"))
        else:
            gaps.append(Gap(prev, nxt, missing_bars, False, "missing candles"))
    return gaps


def _in_break_window(start: datetime, missing: timedelta, cfg: FeedConfig) -> bool:
    """True if the missing interval [start, start + missing) lies inside the daily-break window."""
    window_start, window_end = cfg.daily_break_window_minutes
    length = (window_end - window_start) % 1440 or 1440
    begin = (start.hour * 60 + start.minute - window_start) % 1440
    return begin + missing.total_seconds() / 60 <= length


def _spans_saturday(start: datetime, end: datetime) -> bool:
    """True if the interval [start, end) contains a Saturday 12:00 UTC."""
    day = start.astimezone(UTC).date()
    last = end.astimezone(UTC).date()
    while day <= last:
        if day.weekday() == 5:
            noon = datetime.combine(day, time(12, 0), tzinfo=UTC)
            if start <= noon < end:
                return True
        day += timedelta(days=1)
    return False
