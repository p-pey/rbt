"""Telegram Bot API notifications: HTTP client, error classification and a retrying worker.

Delivery semantics: at-least-once. A send that times out *after* Telegram accepted it is retried
and can therefore arrive twice; the Bot API has no idempotency key. Everything else (duplicate
signals, re-notification of the same setup) is prevented upstream and by `_sent` below.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from xauassist.config.models import TelegramConfig
from xauassist.config.secrets import TelegramSecrets
from xauassist.data.backoff import Backoff
from xauassist.domain.signal import Signal, SignalKey

log = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"


def redact(text: str, token: str) -> str:
    """Remove the bot token (and any `bot<id>:<hash>` URL fragment) from `text`."""
    if token:
        text = text.replace(token, "<token>")
    return re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot<token>", text)


# ---------------------------------------------------------------- transport


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: str


class TransportError(Exception):
    """Network-level failure (DNS, refused, timeout, TLS)."""


class HttpTransport(Protocol):
    def post_json(self, url: str, payload: Mapping[str, Any], timeout: float) -> HttpResponse: ...


class UrllibTransport:
    """Standard-library HTTPS transport (no third-party dependency to package)."""

    def post_json(self, url: str, payload: Mapping[str, Any], timeout: float) -> HttpResponse:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 - fixed https
                return HttpResponse(resp.status, resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:  # 4xx/5xx still carry Telegram's JSON explanation
            return HttpResponse(exc.code, exc.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(type(exc).__name__) from None  # drop the URL (it has the token)


# ---------------------------------------------------------------- client


class SendOutcome(StrEnum):
    SENT = "sent"
    RETRYABLE = "retryable"
    PERMANENT = "permanent"


@dataclass(frozen=True, slots=True)
class SendResult:
    outcome: SendOutcome
    error: str = ""
    retry_after: float = 0.0


class TelegramClient:
    def __init__(
        self,
        secrets: TelegramSecrets,
        transport: HttpTransport,
        *,
        timeout: float = 10.0,
        api_root: str = API_ROOT,
    ) -> None:
        self._secrets = secrets
        self._transport = transport
        self._timeout = timeout
        self._root = api_root

    def _url(self, method: str) -> str:
        return f"{self._root}/bot{self._secrets.bot_token}/{method}"

    def _call(self, method: str, payload: Mapping[str, Any]) -> SendResult:
        token = self._secrets.bot_token
        try:
            response = self._transport.post_json(self._url(method), payload, self._timeout)
        except TransportError as exc:
            return SendResult(SendOutcome.RETRYABLE, f"network error: {exc}")
        return classify_response(response, token)

    def send_message(self, text: str) -> SendResult:
        return self._call(
            "sendMessage",
            {
                "chat_id": self._secrets.chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
        )

    def get_me(self) -> SendResult:
        """Cheap credential check (does not message the chat)."""
        return self._call("getMe", {})


def classify_response(response: HttpResponse, token: str) -> SendResult:
    try:
        body = json.loads(response.body)
    except ValueError:
        body = {}
    parsed: dict[str, Any] = body if isinstance(body, dict) else {}
    if response.status == 200 and parsed.get("ok") is True:
        return SendResult(SendOutcome.SENT)
    description = redact(str(parsed.get("description") or response.body[:200]), token)
    error = f"HTTP {response.status}: {description}"
    params = parsed.get("parameters")
    retry_after = 0.0
    if isinstance(params, dict) and isinstance(params.get("retry_after"), int | float):
        retry_after = float(params["retry_after"])
    if response.status == 429 or response.status >= 500:
        return SendResult(SendOutcome.RETRYABLE, error, retry_after)
    # 400 bad request/chat not found, 401 bad token, 403 bot blocked/kicked, 404 bad token path:
    # retrying cannot succeed until the user fixes the configuration.
    return SendResult(SendOutcome.PERMANENT, error)


# ---------------------------------------------------------------- notifier


class TelegramStatus(StrEnum):
    DISABLED = "disabled"
    NOT_CONFIGURED = "not_configured"
    OK = "ok"  # configured; nothing failed (or the last send worked)
    RETRYING = "retrying"
    ERROR = "error"  # last alert could not be delivered


@dataclass(frozen=True, slots=True)
class TelegramState:
    status: TelegramStatus
    detail: str = ""
    sent: int = 0
    failed: int = 0
    pending: int = 0


_STOP = object()


class TelegramNotifier:
    """Queues alerts and sends them on a worker thread so the monitor loop never waits."""

    def __init__(
        self,
        client: TelegramClient | None,
        config: TelegramConfig,
        formatter: Callable[[Signal], str],
        *,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        self._client = client
        self._cfg = config
        self._format = formatter
        self._stop = threading.Event()
        self._wait = wait or self._stop.wait  # returns True if asked to stop
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=config.queue_size)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._sent_keys: set[SignalKey] = set()
        self._sent = 0
        self._failed = 0
        self._status = self._initial_status()
        self._detail = ""

    # ---- Notifier ----

    def notify(self, signal: Signal) -> None:
        if not self._cfg.enabled or self._client is None:
            return
        with self._lock:
            if signal.key in self._sent_keys:
                return  # never queue the same setup twice
            self._sent_keys.add(signal.key)
        try:
            self._queue.put_nowait((signal.key, self._format(signal)))
        except queue.Full:
            with self._lock:
                self._failed += 1
                self._status, self._detail = TelegramStatus.ERROR, "send queue full; alert dropped"
            log.error("Telegram queue full; dropped alert for %s", signal.key)
            return
        self._ensure_thread()

    # ---- status / control ----

    def reconfigure(self, client: TelegramClient | None, config: TelegramConfig) -> None:
        """Swap credentials/settings at runtime; queued alerts are kept and use the new client."""
        with self._lock:
            self._client = client
            self._cfg = config
            self._status = self._initial_status()
            self._detail = ""

    def state(self) -> TelegramState:
        with self._lock:
            return TelegramState(
                self._status, self._detail, self._sent, self._failed, self._queue.qsize()
            )

    def check_connection(self) -> TelegramState:
        """Verify the token with getMe (blocking; call from a worker thread, e.g. a Test button)."""
        if not self._cfg.enabled:
            return self.state()
        if self._client is None:
            return self.state()
        result = self._client.get_me()
        with self._lock:
            if result.outcome is SendOutcome.SENT:
                self._status, self._detail = TelegramStatus.OK, "bot token accepted"
            else:
                self._status, self._detail = TelegramStatus.ERROR, result.error
        return self.state()

    def send_test_message(self, text: str = "XAUUSD assistant: test message") -> SendResult:
        if self._client is None:
            return SendResult(SendOutcome.PERMANENT, "Telegram is not configured")
        return self._client.send_message(text)

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until queued alerts were processed (used by tests and on shutdown)."""
        done = threading.Event()

        def waiter() -> None:
            self._queue.join()
            done.set()

        threading.Thread(target=waiter, daemon=True).start()
        return done.wait(timeout)

    def stop(self, drain_timeout: float = 3.0) -> None:
        """Let pending alerts finish for `drain_timeout`, then abort any retry waits."""
        thread = self._thread
        if thread is None:
            return
        self._queue.put(_STOP)
        thread.join(drain_timeout)
        if thread.is_alive():
            self._stop.set()
            thread.join(2.0)

    # ---- worker ----

    def _initial_status(self) -> TelegramStatus:
        if not self._cfg.enabled:
            return TelegramStatus.DISABLED
        return TelegramStatus.OK if self._client is not None else TelegramStatus.NOT_CONFIGURED

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="telegram-sender", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if job is _STOP:
                    return
                self._deliver(job[1])
            except Exception:
                log.exception("Telegram worker error")
            finally:
                self._queue.task_done()

    def _deliver(self, text: str) -> None:
        assert self._client is not None
        backoff = Backoff(self._cfg.retry_initial_seconds, self._cfg.retry_max_seconds)
        last_error = ""
        for attempt in range(1, self._cfg.max_attempts + 1):
            result = self._client.send_message(text)
            if result.outcome is SendOutcome.SENT:
                with self._lock:
                    self._sent += 1
                    self._status, self._detail = TelegramStatus.OK, ""
                return
            last_error = result.error
            if result.outcome is SendOutcome.PERMANENT:
                break
            if attempt == self._cfg.max_attempts:
                break
            delay = max(backoff.next_delay(), result.retry_after)
            with self._lock:
                self._status = TelegramStatus.RETRYING
                self._detail = f"attempt {attempt} failed ({last_error}); retrying in {delay:.0f}s"
            log.warning("Telegram send failed (%s); retry in %.0fs", last_error, delay)
            if self._wait(delay):
                last_error = f"{last_error} (shutdown before retry)"
                break
        with self._lock:
            self._failed += 1
            self._status, self._detail = TelegramStatus.ERROR, f"alert not delivered: {last_error}"
        log.error("Telegram alert not delivered: %s", last_error)
