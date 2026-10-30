"""SignalPipeline: feed updates -> engine -> validation -> de-duplication -> notification.

Pure orchestration with injected collaborators and no threads, so it is fully testable. The
monitor service only decides *when* to call `handle`.

Safety rules enforced here:
  - history (warm-up) candles never produce alerts;
  - a fresh engine is built whenever history arrives, so engine state can never be stale;
  - after a data hole, pending setups are cleared and signals are suppressed for a while;
  - signals for candles that closed too long ago (delivered late after an outage) are dropped.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from xauassist.config.models import MonitorConfig
from xauassist.data.feed import FeedStatus, FeedUpdate
from xauassist.domain.signal import Signal
from xauassist.notify.base import NotificationDispatcher
from xauassist.signals.manager import Decision, SignalManager
from xauassist.strategy.engine import StrategyEngine
from xauassist.strategy.errors import IncompleteCandleError, OutOfOrderCandleError
from xauassist.validation.validator import SignalValidator

log = logging.getLogger(__name__)


class Outcome(StrEnum):
    DELIVERED = "delivered"
    REJECTED = "rejected"  # failed a validation gate (spread, symbol, incomplete data ...)
    DUPLICATE = "duplicate"
    COOLDOWN = "cooldown"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class SignalRecord:
    signal: Signal
    outcome: Outcome
    reasons: tuple[str, ...]
    processed_at: datetime


class SignalPipeline:
    def __init__(
        self,
        engine_factory: Callable[[], StrategyEngine],
        validator: SignalValidator,
        manager: SignalManager,
        dispatcher: NotificationDispatcher,
        spread_source: Callable[[], float | None],
        config: MonitorConfig,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._engine_factory = engine_factory
        self._validator = validator
        self._manager = manager
        self._dispatcher = dispatcher
        self._spread_source = spread_source
        self._cfg = config
        self._clock = clock
        self._engine: StrategyEngine | None = None
        self._history: deque[SignalRecord] = deque(maxlen=config.history_size)
        self._suppress_left = 0
        self._resync_requested = False

    # ---- state for the UI ----

    @property
    def latest_ma(self) -> float | None:
        return self._engine.latest_ma if self._engine else None

    @property
    def last_candle_time(self) -> datetime | None:
        return self._engine.last_candle_time if self._engine else None

    @property
    def history(self) -> tuple[SignalRecord, ...]:
        return tuple(self._history)

    @property
    def last_delivered(self) -> SignalRecord | None:
        return next((r for r in reversed(self._history) if r.outcome is Outcome.DELIVERED), None)

    def consume_resync_request(self) -> bool:
        """True once if the engine hit inconsistent data and the feed should re-bootstrap."""
        requested, self._resync_requested = self._resync_requested, False
        return requested

    # ---- processing ----

    def handle(self, update: FeedUpdate) -> list[SignalRecord]:
        if update.status is not FeedStatus.OK or not update.candles:
            return []
        if update.is_history:
            self._engine = self._engine_factory()
            for candle in update.candles:
                self._engine.on_candle_closed(candle)  # warm-up only: signals are discarded
            self._suppress_left = 0 if update.data_complete else self._cfg.post_gap_suppress_bars
            log.info("Engine warmed up with %d history candles", len(update.candles))
            return []
        if self._engine is None:
            log.error("Live candles arrived before history; requesting re-sync")
            self._resync_requested = True
            return []
        if not update.data_complete:
            self._engine.reset_setups()
            self._suppress_left = self._cfg.post_gap_suppress_bars
            log.warning("Data hole detected; pending setups cleared, signals suppressed")

        records: list[SignalRecord] = []
        for candle in update.candles:
            try:
                signals = self._engine.on_candle_closed(candle)
            except (IncompleteCandleError, OutOfOrderCandleError):
                log.exception("Engine rejected candle %s; requesting re-sync", candle.time_open)
                self._resync_requested = True
                break
            suppressed = self._suppress_left > 0
            if suppressed:
                self._suppress_left -= 1
            for signal in signals:
                records.append(self._process(signal, data_complete=not suppressed))
        for record in records:
            self._history.append(record)
        return records

    def _process(self, signal: Signal, *, data_complete: bool) -> SignalRecord:
        now = self._clock()
        age = (now - signal.time).total_seconds()
        if age > self._cfg.max_signal_age_seconds:
            reason = f"candle closed {age:.0f}s ago (limit {self._cfg.max_signal_age_seconds:.0f}s)"
            log.warning("Dropping stale signal %s: %s", signal.key, reason)
            return SignalRecord(signal, Outcome.STALE, (reason,), now)
        if age < -300:
            log.warning(
                "Signal candle is %.0fs in the future: check mt5.server_utc_offset_hours "
                "and the PC clock",
                -age,
            )

        result = self._validator.validate(
            signal, current_spread_points=self._spread_source(), data_complete=data_complete
        )
        if not result.ok:
            log.info("Signal %s rejected: %s", signal.key, "; ".join(result.reasons))
            return SignalRecord(signal, Outcome.REJECTED, result.reasons, now)

        decision = self._manager.accept(signal)
        if decision is Decision.DUPLICATE:
            return SignalRecord(signal, Outcome.DUPLICATE, ("already signalled",), now)
        if decision is Decision.COOLDOWN:
            return SignalRecord(signal, Outcome.COOLDOWN, ("signal cooldown active",), now)

        log.info(
            "SIGNAL %s %s %s @ %.2f",
            signal.direction,
            signal.scenario,
            signal.symbol,
            signal.entry_price,
        )
        self._dispatcher.dispatch(signal)
        return SignalRecord(signal, Outcome.DELIVERED, (), now)
