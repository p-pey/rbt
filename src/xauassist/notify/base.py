"""Notifier interface and fan-out dispatcher."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol

from xauassist.domain.signal import Signal

log = logging.getLogger(__name__)


class Notifier(Protocol):
    def notify(self, signal: Signal) -> None:
        """Announce a signal. Must return promptly (never block the monitoring loop)."""
        ...


class NotificationDispatcher:
    """Calls every notifier in order; one failing notifier never prevents the others."""

    def __init__(self, notifiers: Sequence[Notifier]) -> None:
        self._notifiers = list(notifiers)

    def dispatch(self, signal: Signal) -> None:
        for notifier in self._notifiers:
            try:
                notifier.notify(signal)
            except Exception:
                log.exception("Notifier %s failed for %s", type(notifier).__name__, signal.key)
