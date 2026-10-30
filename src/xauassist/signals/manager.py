"""Signal manager: duplicate prevention and per-direction cooldown."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum

from xauassist.config.models import SignalsConfig
from xauassist.domain.signal import Signal
from xauassist.signals.dedup_store import DedupStore


class Decision(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    COOLDOWN = "cooldown"


class SignalManager:
    def __init__(self, store: DedupStore, config: SignalsConfig) -> None:
        self._store = store
        self._cfg = config

    def accept(self, signal: Signal) -> Decision:
        """Record and accept a signal, or say why it was rejected (nothing is recorded then)."""
        if self._store.contains(signal.key):
            return Decision.DUPLICATE
        if self._cfg.cooldown_bars > 0:
            last = self._store.last_bar_time(signal.symbol, signal.direction)
            if last is not None:
                gap = signal.bar_open_time - last
                if gap < timedelta(minutes=self._cfg.cooldown_bars * signal.timeframe.minutes):
                    return Decision.COOLDOWN
        self._store.add(signal.key)
        return Decision.ACCEPTED
