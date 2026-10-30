import json
import socket
import threading
from collections.abc import Mapping
from datetime import UTC, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from helpers import make_config, run, scenario_a_sell, scenario_b_sell
from xauassist.config.models import ConfigError, TelegramConfig
from xauassist.config.secrets import TelegramSecrets
from xauassist.domain.enums import Direction
from xauassist.domain.signal import Signal
from xauassist.notify.message import format_signal_message, resolve_timezone
from xauassist.notify.telegram import (
    HttpResponse,
    SendOutcome,
    TelegramClient,
    TelegramNotifier,
    TelegramStatus,
    TransportError,
    UrllibTransport,
    classify_response,
    redact,
)

TOKEN = "123456789:AAEhBP0av28cEXAMPLEexampleEXAMPLE_abc12"
SECRETS = TelegramSecrets(TOKEN, "987654321")
OK = HttpResponse(200, json.dumps({"ok": True, "result": {"message_id": 1}}))


def err(status: int, description: str, retry_after: int | None = None) -> HttpResponse:
    body: dict[str, Any] = {"ok": False, "error_code": status, "description": description}
    if retry_after is not None:
        body["parameters"] = {"retry_after": retry_after}
    return HttpResponse(status, json.dumps(body))


class FakeTransport:
    """Replays scripted responses (or raises scripted exceptions) and records requests."""

    def __init__(self, *script: HttpResponse | Exception) -> None:
        self.script = list(script)
        self.requests: list[tuple[str, Mapping[str, Any]]] = []
        self.gate: threading.Event | None = None

    def post_json(self, url: str, payload: Mapping[str, Any], timeout: float) -> HttpResponse:
        if self.gate is not None:
            self.gate.wait(5)
        self.requests.append((url, payload))
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item


def sell_signal() -> Signal:
    (sig,) = run(scenario_a_sell(), make_config())
    return sig


def make_notifier(
    transport: FakeTransport, *, cfg: TelegramConfig | None = None
) -> tuple[TelegramNotifier, list[float]]:
    waits: list[float] = []

    def record_wait(delay: float) -> bool:
        waits.append(delay)
        return False

    config = cfg or TelegramConfig(enabled=True, max_attempts=4)
    client = TelegramClient(SECRETS, transport)
    notifier = TelegramNotifier(
        client,
        config,
        lambda s: format_signal_message(s, UTC),
        wait=record_wait,
    )
    return notifier, waits


# ---- message format ----


def test_message_matches_the_requested_layout() -> None:
    text = format_signal_message(sell_signal(), timezone(timedelta(hours=2)))
    lines = text.splitlines()
    assert lines[0] == "🔴 GOLD SELL SIGNAL"
    for expected in (
        "Symbol: XAUUSD",
        "Timeframe: M5",
        "Strategy: MA20 Retest (Scenario A)",
        "Confirmation: Bearish Engulfing",
        "Reason:",
    ):
        assert expected in lines
    assert any(ln.startswith("Entry Price: ") and ln.count(".") == 1 for ln in lines)
    assert any(ln.startswith("EMA20: ") for ln in lines)
    assert "Spread: 20 pts" in text
    assert "Alert only - no order has been placed." in text


def test_message_time_is_converted_to_the_display_timezone() -> None:
    sig = sell_signal()  # candle closes 11:10 UTC
    assert "Time: 13:10 (2026-01-05, UTC+02:00)" in format_signal_message(
        sig, timezone(timedelta(hours=2))
    )
    assert "Time: 11:10 (2026-01-05, UTC+00:00)" in format_signal_message(sig, UTC)
    assert "UTC-03:30" in format_signal_message(sig, timezone(-timedelta(hours=3, minutes=30)))


def test_buy_and_scenario_b_labels() -> None:
    candles, _ = scenario_b_sell()
    from helpers import mirror

    (sig,) = run(mirror(candles), make_config())
    text = format_signal_message(sig, UTC)
    assert text.startswith("🟢 GOLD BUY SIGNAL")
    assert "Strategy: MA20 Break + Pullback (Scenario B)" in text
    assert sig.direction is Direction.BUY


def test_resolve_timezone() -> None:
    assert resolve_timezone("UTC") is not None
    assert resolve_timezone("local") is not None
    with pytest.raises(ConfigError):
        resolve_timezone("Mars/Olympus")


# ---- response classification ----


@pytest.mark.parametrize(
    ("response", "outcome"),
    [
        (OK, SendOutcome.SENT),
        (err(429, "Too Many Requests: retry after 7", 7), SendOutcome.RETRYABLE),
        (err(500, "Internal Server Error"), SendOutcome.RETRYABLE),
        (err(502, "Bad Gateway"), SendOutcome.RETRYABLE),
        (HttpResponse(503, "<html>maintenance</html>"), SendOutcome.RETRYABLE),
        (err(400, "Bad Request: chat not found"), SendOutcome.PERMANENT),
        (err(401, "Unauthorized"), SendOutcome.PERMANENT),
        (err(403, "Forbidden: bot was blocked by the user"), SendOutcome.PERMANENT),
        (err(404, "Not Found"), SendOutcome.PERMANENT),
        (HttpResponse(200, "not json"), SendOutcome.PERMANENT),
        (HttpResponse(200, json.dumps({"ok": False})), SendOutcome.PERMANENT),
    ],
)
def test_classify_response(response: HttpResponse, outcome: SendOutcome) -> None:
    assert classify_response(response, TOKEN).outcome is outcome


def test_retry_after_is_extracted() -> None:
    assert classify_response(err(429, "slow down", 9), TOKEN).retry_after == 9.0


def test_redact_removes_token_everywhere() -> None:
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    cleaned = redact(f"failed {url} with {TOKEN}", TOKEN)
    assert TOKEN not in cleaned and "bot<token>" in cleaned
    assert TOKEN not in classify_response(err(401, f"bad token {TOKEN}"), TOKEN).error


# ---- client ----


def test_client_posts_to_send_message_with_chat_id_and_text() -> None:
    transport = FakeTransport(OK)
    result = TelegramClient(SECRETS, transport).send_message("hello")
    assert result.outcome is SendOutcome.SENT
    url, payload = transport.requests[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert payload["chat_id"] == "987654321" and payload["text"] == "hello"


def test_client_network_error_is_retryable_and_leaks_nothing() -> None:
    result = TelegramClient(SECRETS, FakeTransport(TransportError("TimeoutError"))).send_message(
        "x"
    )
    assert result.outcome is SendOutcome.RETRYABLE and TOKEN not in result.error


class LocalTelegramServer:
    """Real HTTP server on localhost so UrllibTransport is exercised end to end."""

    def __init__(self, responses: list[tuple[int, str]]) -> None:
        outer = self
        self.responses = responses
        self.seen: list[tuple[str, dict[str, Any]]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", 0))
                outer.seen.append((self.path, json.loads(self.rfile.read(length) or b"{}")))
                status, body = outer.responses.pop(0)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *args: Any) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "all_proxy",
        "ALL_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("no_proxy", "*")


@pytest.mark.usefixtures("no_proxy")
def test_urllib_transport_against_a_real_http_server() -> None:
    srv = LocalTelegramServer(
        [
            (200, '{"ok": true}'),
            (429, json.dumps({"ok": False, "parameters": {"retry_after": 3}})),
            (500, "oops"),
        ]
    )
    try:
        client = TelegramClient(SECRETS, UrllibTransport(), timeout=5.0, api_root=srv.url)
        assert client.send_message("hi").outcome is SendOutcome.SENT
        limited = client.send_message("hi")
        assert limited.outcome is SendOutcome.RETRYABLE and limited.retry_after == 3.0
        assert client.send_message("hi").outcome is SendOutcome.RETRYABLE
        path, payload = srv.seen[0]
        assert path == f"/bot{TOKEN}/sendMessage" and payload["text"] == "hi"
    finally:
        srv.close()


@pytest.mark.usefixtures("no_proxy")
def test_urllib_transport_connection_refused_is_a_retryable_network_error() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # nothing listens here once the socket is closed
    client = TelegramClient(
        SECRETS, UrllibTransport(), timeout=2.0, api_root=f"http://127.0.0.1:{port}"
    )
    result = client.send_message("hi")
    assert result.outcome is SendOutcome.RETRYABLE and "network error" in result.error
    assert TOKEN not in result.error


# ---- notifier ----


def test_successful_delivery() -> None:
    transport = FakeTransport(OK)
    notifier, _ = make_notifier(transport)
    notifier.notify(sell_signal())
    assert notifier.flush()
    state = notifier.state()
    assert state.status is TelegramStatus.OK and state.sent == 1 and state.failed == 0
    assert "GOLD SELL SIGNAL" in transport.requests[0][1]["text"]


def test_network_errors_are_retried_with_growing_backoff_then_succeed() -> None:
    transport = FakeTransport(TransportError("TimeoutError"), err(502, "Bad Gateway"), OK)
    notifier, waits = make_notifier(transport)
    notifier.notify(sell_signal())
    assert notifier.flush()
    assert len(transport.requests) == 3
    assert waits == [2.0, 4.0]
    state = notifier.state()
    assert state.status is TelegramStatus.OK and state.sent == 1


def test_rate_limit_retry_after_is_honoured() -> None:
    notifier, waits = make_notifier(FakeTransport(err(429, "Too Many Requests", 30), OK))
    notifier.notify(sell_signal())
    assert notifier.flush()
    assert waits == [30.0]  # longer than the 2 s backoff


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_errors_are_not_retried(status: int) -> None:
    transport = FakeTransport(err(status, "Unauthorized"))
    notifier, waits = make_notifier(transport)
    notifier.notify(sell_signal())
    assert notifier.flush()
    assert len(transport.requests) == 1 and waits == []
    state = notifier.state()
    assert (
        state.status is TelegramStatus.ERROR and state.failed == 1 and str(status) in state.detail
    )


def test_gives_up_after_max_attempts() -> None:
    transport = FakeTransport(TransportError("ConnectionRefusedError"))
    notifier, waits = make_notifier(transport)  # max_attempts = 4
    notifier.notify(sell_signal())
    assert notifier.flush()
    assert len(transport.requests) == 4 and len(waits) == 3
    state = notifier.state()
    assert state.status is TelegramStatus.ERROR and "not delivered" in state.detail


def test_later_alert_still_sent_after_an_earlier_failure() -> None:
    from dataclasses import replace

    transport = FakeTransport(err(400, "chat not found"), OK)
    notifier, _ = make_notifier(transport)
    first = sell_signal()
    notifier.notify(first)
    notifier.notify(replace(first, bar_open_time=first.bar_open_time + timedelta(minutes=30)))
    assert notifier.flush()
    state = notifier.state()
    assert state.failed == 1 and state.sent == 1 and state.status is TelegramStatus.OK


def test_same_signal_is_never_queued_twice() -> None:
    transport = FakeTransport(OK)
    notifier, _ = make_notifier(transport)
    sig = sell_signal()
    notifier.notify(sig)
    notifier.notify(sig)
    assert notifier.flush()
    assert len(transport.requests) == 1


def test_notify_does_not_block_while_telegram_is_slow() -> None:
    transport = FakeTransport(OK)
    transport.gate = threading.Event()  # the "network" hangs until released
    notifier, _ = make_notifier(transport)
    done = threading.Event()

    def call() -> None:
        notifier.notify(sell_signal())
        done.set()

    threading.Thread(target=call, daemon=True).start()
    assert done.wait(1.0), "notify() blocked the caller"
    assert not transport.requests
    transport.gate.set()
    assert notifier.flush()
    assert len(transport.requests) == 1


def test_disabled_and_unconfigured_send_nothing() -> None:
    transport = FakeTransport(OK)
    disabled = TelegramNotifier(
        TelegramClient(SECRETS, transport), TelegramConfig(enabled=False), lambda s: "x"
    )
    disabled.notify(sell_signal())
    assert disabled.state().status is TelegramStatus.DISABLED
    unconfigured = TelegramNotifier(None, TelegramConfig(enabled=True), lambda s: "x")
    unconfigured.notify(sell_signal())
    assert unconfigured.state().status is TelegramStatus.NOT_CONFIGURED
    assert transport.requests == []


def test_check_connection_uses_get_me() -> None:
    ok_transport = FakeTransport(OK)
    notifier, _ = make_notifier(ok_transport)
    assert notifier.check_connection().status is TelegramStatus.OK
    assert ok_transport.requests[0][0].endswith("/getMe")
    bad, _ = make_notifier(FakeTransport(err(401, "Unauthorized")))
    state = bad.check_connection()
    assert state.status is TelegramStatus.ERROR and "401" in state.detail


def test_stop_aborts_retry_wait_promptly() -> None:
    transport = FakeTransport(TransportError("TimeoutError"))
    stop_seen = threading.Event()
    notifier: TelegramNotifier

    def wait(delay: float) -> bool:
        stop_seen.set()
        return notifier._stop.wait(30)  # a long backoff, interrupted by stop()

    notifier = TelegramNotifier(
        TelegramClient(SECRETS, transport),
        TelegramConfig(enabled=True, max_attempts=5),
        lambda s: "x",
        wait=wait,
    )
    notifier.notify(sell_signal())
    assert stop_seen.wait(2)
    notifier.stop(drain_timeout=0.2)
    assert notifier.state().status is TelegramStatus.ERROR
    assert notifier._thread is not None and not notifier._thread.is_alive()


def test_token_never_appears_in_state_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    notifier, _ = make_notifier(FakeTransport(err(401, f"Unauthorized {TOKEN}")))
    notifier.notify(sell_signal())
    assert notifier.flush()
    assert TOKEN not in notifier.state().detail
    assert TOKEN not in caplog.text
