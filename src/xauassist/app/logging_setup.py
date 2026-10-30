"""File + console logging with credential redaction."""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_BOT_URL_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_TOKEN_RE = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{30,}\b")


class RedactingFormatter(logging.Formatter):
    """Scrubs Telegram tokens and registered secrets from the final text, tracebacks included."""

    def __init__(self, fmt: str) -> None:
        super().__init__(fmt)
        self._secrets: set[str] = set()

    def add_secret(self, value: str) -> None:
        if len(value) >= 6:
            self._secrets.add(value)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "<hidden>")
        text = _BOT_URL_RE.sub("bot<token>", text)
        return _TOKEN_RE.sub("<token>", text)

    def format(self, record: logging.LogRecord) -> str:
        return self.redact(super().format(record))


_formatter = RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")


def register_secret(value: str) -> None:
    """Make sure `value` (e.g. a chat id or token) never appears in logs."""
    _formatter.add_secret(value)


def setup_logging(log_dir: Path, *, level: int = logging.INFO, console: bool = True) -> Path:
    """Configure the root logger. Returns the log file path."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "xauassist.log"
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        if getattr(handler, "_xauassist", False):
            root.removeHandler(handler)
            handler.close()
    handlers: list[logging.Handler] = [
        RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    ]
    if console and sys.stderr is not None:  # a windowed .exe has no console at all
        handlers.append(logging.StreamHandler())
    for handler in handlers:
        handler.setFormatter(_formatter)
        handler._xauassist = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    return log_file
