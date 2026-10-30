"""Crash handling for a windowed .exe, which has no console to show a traceback in."""

from __future__ import annotations

import logging
import sys
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from types import TracebackType

log = logging.getLogger(__name__)

ShowFn = Callable[[str], None]


def handle_exception(
    exc_type: type[BaseException],
    exc: BaseException,
    tb: TracebackType | None,
    log_file: Path,
    show: ShowFn | None,
) -> None:
    """Log the traceback and (optionally) tell the user where to find it."""
    if issubclass(exc_type, KeyboardInterrupt):
        return
    log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
    if show is not None:
        try:
            summary = "".join(traceback.format_exception_only(exc_type, exc)).strip()
            show(f"The assistant hit an unexpected error:\n\n{summary}\n\nDetails: {log_file}")
        except Exception:  # the crash dialog itself must never raise
            log.exception("Could not show the crash dialog")


def install_crash_handlers(log_file: Path, show: ShowFn | None = None) -> None:
    """Route uncaught exceptions (main thread, worker threads, Qt slots) to the log."""

    def hook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
        handle_exception(exc_type, exc, tb, log_file, show)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is not None:
            handle_exception(args.exc_type, args.exc_value, args.exc_traceback, log_file, None)

    sys.excepthook = hook
    threading.excepthook = thread_hook
