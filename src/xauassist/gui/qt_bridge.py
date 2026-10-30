"""Qt glue: marshal monitor-thread callbacks onto the GUI thread, and run tasks off it."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, cast

from PySide6.QtCore import QObject, Signal

from xauassist.app.controller import AppController


class MonitorBridge(QObject):
    """Re-emits controller callbacks (monitor thread) as Qt signals (queued to the GUI thread)."""

    snapshot_changed = Signal(object)  # MonitorSnapshot
    record_added = Signal(object)  # SignalRecord

    def __init__(self, controller: AppController, parent: QObject | None = None) -> None:
        super().__init__(parent)
        controller.add_snapshot_listener(self.snapshot_changed.emit)
        controller.add_signal_listener(self.record_added.emit)


class BackgroundRunner(QObject):
    """Run a blocking function (network call) on a thread; deliver its result on the GUI thread."""

    _done = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._done.connect(self._deliver)

    def run(self, work: Callable[[], Any], callback: Callable[[Any], None]) -> None:
        def target() -> None:
            try:
                result: Any = work()
            except Exception as exc:  # surface failures to the UI instead of losing them
                result = exc
            self._done.emit((callback, result))

        threading.Thread(target=target, name="gui-task", daemon=True).start()

    def _deliver(self, payload: object) -> None:
        callback, result = cast(tuple[Callable[[Any], None], Any], payload)
        callback(result)
