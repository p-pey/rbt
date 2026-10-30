"""Load candles from CSV: MT5 history exports (tab-separated, <DATE> <TIME> ...) or simple CSV."""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path

from xauassist.data.errors import DataFormatError
from xauassist.domain.candle import Candle

_DATE_TIME_FORMATS = (
    "%Y.%m.%d %H:%M:%S",
    "%Y.%m.%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
)
_VOLUME_KEYS = ("tickvol", "tick_volume", "volume", "vol")


def load_candles_csv(path: Path | str, *, utc_offset_hours: float = 0.0) -> list[Candle]:
    """Parse a CSV file into closed candles, oldest first, with times converted to UTC.

    `utc_offset_hours` is broker server time minus UTC. Duplicate timestamps keep the last row.
    """
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise DataFormatError(f"Cannot read {path}: {exc}") from exc
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        raise DataFormatError(f"{path}: no data rows")
    try:
        delimiter = csv.Sniffer().sniff(lines[0], delimiters=",\t;").delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.reader(lines, delimiter=delimiter)
    header = [h.strip().strip("<>").lower() for h in next(reader)]
    index = {name: i for i, name in enumerate(header)}
    for needed in ("open", "high", "low", "close"):
        if needed not in index:
            raise DataFormatError(f"{path}: missing column {needed!r} (found {header})")
    if "date" not in index and not any(k in index for k in ("time", "datetime", "timestamp")):
        raise DataFormatError(f"{path}: no date/time column (found {header})")

    offset = timedelta(hours=utc_offset_hours)
    by_time: dict[datetime, Candle] = {}
    for line_no, row in enumerate(reader, start=2):
        try:
            when = _parse_time(row, index) - offset
            by_time[when] = Candle(
                time_open=when,
                open=float(row[index["open"]]),
                high=float(row[index["high"]]),
                low=float(row[index["low"]]),
                close=float(row[index["close"]]),
                tick_volume=_int_field(row, index, _VOLUME_KEYS),
                spread_points=float(_field(row, index, ("spread",)) or 0.0),
            )
        except (ValueError, IndexError) as exc:
            raise DataFormatError(f"{path}:{line_no}: bad row {row!r}: {exc}") from exc
    return [by_time[t] for t in sorted(by_time)]


def _field(row: list[str], index: dict[str, int], keys: tuple[str, ...]) -> str:
    for key in keys:
        if key in index:
            return row[index[key]].strip()
    return ""


def _int_field(row: list[str], index: dict[str, int], keys: tuple[str, ...]) -> int:
    raw = _field(row, index, keys)
    return int(float(raw)) if raw else 0


def _parse_time(row: list[str], index: dict[str, int]) -> datetime:
    if "date" in index:
        raw = f"{row[index['date']].strip()} {_field(row, index, ('time',))}".strip()
    else:
        raw = _field(row, index, ("time", "datetime", "timestamp"))
    for fmt in _DATE_TIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        return datetime.fromtimestamp(float(raw), tz=UTC)  # unix seconds
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"unrecognised timestamp {raw!r}") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
