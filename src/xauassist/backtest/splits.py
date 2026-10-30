"""Chronological exploration / validation / test split and the one-shot test-set lock.

Rules that keep the out-of-sample number honest:
  1. Splits are chronological (never shuffled).
  2. Development runs are handed ONLY the exploration + validation candles; the test candles are
     never read, so they cannot influence anything.
  3. A trade whose outcome window reaches past the end of its segment is purged, so no segment's
     score depends on prices from the next one.
  4. The final test can be evaluated once per dataset. A lock file records the first evaluation;
     a repeat is refused (or, if forced, flagged as NOT out-of-sample in the report).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from xauassist.backtest.models import BacktestResult, Segment, SkippedSignal, Trade
from xauassist.config.models import AppConfig, BacktestConfig
from xauassist.config.writer import dumps_config
from xauassist.domain.candle import Candle

MIN_CANDLES = 500


@dataclass(frozen=True, slots=True)
class SegmentBounds:
    segment: Segment
    start: int  # first candle index (inclusive)
    end: int  # one past the last candle index


def segment_bounds(n: int, cfg: BacktestConfig) -> dict[Segment, SegmentBounds]:
    if n < MIN_CANDLES:
        raise ValueError(f"need at least {MIN_CANDLES} candles for a train/validation/test split")
    a = int(n * cfg.exploration_fraction)
    b = int(n * (cfg.exploration_fraction + cfg.validation_fraction))
    return {
        Segment.EXPLORATION: SegmentBounds(Segment.EXPLORATION, 0, a),
        Segment.VALIDATION: SegmentBounds(Segment.VALIDATION, a, b),
        Segment.TEST: SegmentBounds(Segment.TEST, b, n),
    }


def select_segment(
    result: BacktestResult, bounds: SegmentBounds
) -> tuple[list[Trade], list[SkippedSignal], int]:
    """Trades and skipped signals generated inside `bounds`; plus how many trades were purged."""
    inside = [t for t in result.trades if bounds.start <= t.signal_index < bounds.end]
    kept = [t for t in inside if t.last_index_used < bounds.end]
    skipped = [s for s in result.skipped if bounds.start <= s.signal_index < bounds.end]
    return kept, skipped, len(inside) - len(kept)


def dataset_fingerprint(candles: Sequence[Candle]) -> str:
    """Stable identity of a dataset (times and closes), independent of file name or format."""
    digest = hashlib.sha256()
    digest.update(str(len(candles)).encode())
    for c in candles:
        digest.update(f"{c.time_open.isoformat()}|{c.close:.5f}".encode())
    return digest.hexdigest()[:16]


def config_hash(config: AppConfig) -> str:
    return hashlib.sha256(dumps_config(config).encode()).hexdigest()[:16]


class TestSetAlreadyUsedError(RuntimeError):
    __test__ = False  # not a pytest test class

    def __init__(self, previous: dict[str, Any]) -> None:
        super().__init__(
            "The final test set of this dataset was already evaluated on "
            f"{previous.get('used_at')} (config {previous.get('config_hash')}). Evaluating it "
            "again after seeing the results would make it an exploration set, not an out-of-sample "
            "test. Use new data, or pass allow_reuse to override (the report will say so)."
        )
        self.previous = previous


@dataclass(frozen=True, slots=True)
class LockOutcome:
    reused: bool
    previous: dict[str, Any] | None


class FinalTestLock:
    """Records the first final-test evaluation of each dataset in `directory`."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def path_for(self, fingerprint: str) -> Path:
        return self._dir / f"final_test_{fingerprint}.json"

    def claim(self, fingerprint: str, cfg_hash: str, *, allow_reuse: bool) -> LockOutcome:
        path = self.path_for(fingerprint)
        if path.exists():
            previous: dict[str, Any] = json.loads(path.read_text())
            if not allow_reuse:
                raise TestSetAlreadyUsedError(previous)
            return LockOutcome(True, previous)
        self._dir.mkdir(parents=True, exist_ok=True)
        record = {
            "fingerprint": fingerprint,
            "config_hash": cfg_hash,
            "used_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        path.write_text(json.dumps(record, indent=2))
        return LockOutcome(False, None)
