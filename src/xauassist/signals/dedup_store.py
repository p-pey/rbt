"""Persistent record of emitted signals, for duplicate prevention across restarts."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from xauassist.domain.enums import Direction
from xauassist.domain.signal import SignalKey


class DedupStore(Protocol):
    def contains(self, key: SignalKey) -> bool: ...

    def add(self, key: SignalKey) -> None: ...

    def last_bar_time(self, symbol: str, direction: Direction) -> datetime | None: ...


class InMemoryDedupStore:
    def __init__(self) -> None:
        self._keys: set[SignalKey] = set()

    def contains(self, key: SignalKey) -> bool:
        return key in self._keys

    def add(self, key: SignalKey) -> None:
        self._keys.add(key)

    def last_bar_time(self, symbol: str, direction: Direction) -> datetime | None:
        times = [
            k.bar_open_time for k in self._keys if k.symbol == symbol and k.direction == direction
        ]
        return max(times) if times else None


class SqliteDedupStore:
    def __init__(self, path: Path | str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        with self._lock, self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS signals ("
                "symbol TEXT NOT NULL, direction TEXT NOT NULL, scenario TEXT NOT NULL, "
                "bar_open_time TEXT NOT NULL, "
                "PRIMARY KEY (symbol, direction, scenario, bar_open_time))"
            )

    @staticmethod
    def _ts(value: datetime) -> str:
        return value.astimezone(UTC).isoformat()

    def contains(self, key: SignalKey) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM signals WHERE symbol=? AND direction=? AND scenario=? "
                "AND bar_open_time=?",
                (key.symbol, key.direction.value, key.scenario.value, self._ts(key.bar_open_time)),
            ).fetchone()
        return row is not None

    def add(self, key: SignalKey) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO signals VALUES (?, ?, ?, ?)",
                (key.symbol, key.direction.value, key.scenario.value, self._ts(key.bar_open_time)),
            )

    def last_bar_time(self, symbol: str, direction: Direction) -> datetime | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(bar_open_time) FROM signals WHERE symbol=? AND direction=?",
                (symbol, direction.value),
            ).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
