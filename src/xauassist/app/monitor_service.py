"""MonitorService: runs the poll loop on a background thread and publishes status snapshots.

The GUI (Phase 7) subscribes to snapshots and signal records; it holds no business logic.
Listeners are called on the monitor thread, so a GUI must marshal them to its own thread.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from xauassist.app.pipeline import SignalPipeline, SignalRecord
from xauassist.data.feed import DataFeed, FeedStatus, FeedUpdate
from xauassist.domain.enums import Timeframe

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MonitorSnapshot:
    running: bool
    feed_status: FeedStatus | None
    message: str
    symbol: str | None
    timeframe: Timeframe
    latest_ma: float | None
    last_candle_time: datetime | None
    last_signal: SignalRecord | None
    updated_at: datetime


class MonitorService:
    def __init__(
        self,
        feed: DataFeed,
        pipeline: SignalPipeline,
        timeframe: Timeframe,
        poll_seconds: float,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._feed = feed
        self._pipeline = pipeline
        self._tf = timeframe
        self._poll = poll_seconds
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_update: FeedUpdate | None = None
        self._snapshot_listeners: list[Callable[[MonitorSnapshot], None]] = []
        self._signal_listeners: list[Callable[[SignalRecord], None]] = []

    # ---- subscriptions ----

    def add_snapshot_listener(self, listener: Callable[[MonitorSnapshot], None]) -> None:
        self._snapshot_listeners.append(listener)

    def add_signal_listener(self, listener: Callable[[SignalRecord], None]) -> None:
        self._signal_listeners.append(listener)

    # ---- control ----

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="monitor", daemon=True)
        self._thread.start()
        log.info("Monitor started")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self._notify_snapshot()
        log.info("Monitor stopped")

    # ---- one cycle ----

    def run_once(self) -> list[SignalRecord]:
        """One poll + processing cycle. Never raises: failures become a status message."""
        try:
            update = self._feed.poll()
            records = self._pipeline.handle(update)
            if self._pipeline.consume_resync_request():
                self._feed.reset()
        except Exception as exc:
            log.exception("Monitor cycle failed")
            update = FeedUpdate(FeedStatus.DATA_ERROR, message=f"internal error: {exc!r}")
            records = []
        with self._lock:
            self._last_update = update
        for record in records:
            self._call(self._signal_listeners, record)
        self._notify_snapshot()
        return records

    def snapshot(self) -> MonitorSnapshot:
        with self._lock:
            update = self._last_update
        return MonitorSnapshot(
            running=self.is_running,
            feed_status=update.status if update else None,
            message=update.message if update else "",
            symbol=self._feed.symbol,
            timeframe=self._tf,
            latest_ma=self._pipeline.latest_ma,
            last_candle_time=self._pipeline.last_candle_time,
            last_signal=self._pipeline.last_delivered,
            updated_at=self._clock(),
        )

    # ---- internals ----

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self._poll)

    def _notify_snapshot(self) -> None:
        if self._snapshot_listeners:
            self._call(self._snapshot_listeners, self.snapshot())

    @staticmethod
    def _call(listeners: list[Callable[..., None]], payload: object) -> None:
        for listener in list(listeners):
            try:
                listener(payload)
            except Exception:
                log.exception("Monitor listener failed")
