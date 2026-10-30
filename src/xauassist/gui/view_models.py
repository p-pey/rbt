"""Presentation logic with no Qt: turns monitor/notifier state into display strings and levels."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, tzinfo
from enum import StrEnum

from xauassist.app.monitor_service import MonitorSnapshot
from xauassist.app.pipeline import Outcome, SignalRecord
from xauassist.data.feed import FeedStatus
from xauassist.domain.enums import Direction
from xauassist.notify.telegram import TelegramState, TelegramStatus


class Level(StrEnum):
    OK = "ok"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    IDLE = "idle"


@dataclass(frozen=True, slots=True)
class StatusView:
    text: str
    level: Level
    detail: str = ""


_FEED: dict[FeedStatus, tuple[str, Level]] = {
    FeedStatus.OK: ("Connected", Level.OK),
    FeedStatus.NO_NEW_DATA: ("Connected - waiting for next candle", Level.OK),
    FeedStatus.MARKET_CLOSED: ("Market closed", Level.INFO),
    FeedStatus.DATA_GAP: ("Waiting for missing candles", Level.WARN),
    FeedStatus.DATA_ERROR: ("Data problem", Level.WARN),
    FeedStatus.DISCONNECTED: ("Disconnected from MetaTrader 5 / broker", Level.ERROR),
    FeedStatus.TERMINAL_MISSING: ("MetaTrader 5 not found", Level.ERROR),
    FeedStatus.SYMBOL_MISSING: ("XAUUSD symbol not found", Level.ERROR),
    FeedStatus.RECONNECTING: ("Reconnecting...", Level.WARN),
}


def connection_view(snapshot: MonitorSnapshot) -> StatusView:
    if not snapshot.running:
        return StatusView("Stopped", Level.IDLE)
    if snapshot.feed_status is None:
        return StatusView("Starting...", Level.INFO)
    text, level = _FEED[snapshot.feed_status]
    return StatusView(text, level, snapshot.message)


_TELEGRAM: dict[TelegramStatus, tuple[str, Level]] = {
    TelegramStatus.DISABLED: ("Disabled", Level.IDLE),
    TelegramStatus.NOT_CONFIGURED: ("Not configured (add bot token and chat id)", Level.WARN),
    TelegramStatus.OK: ("Ready", Level.OK),
    TelegramStatus.RETRYING: ("Retrying...", Level.WARN),
    TelegramStatus.ERROR: ("Error", Level.ERROR),
}


def telegram_view(state: TelegramState) -> StatusView:
    text, level = _TELEGRAM[state.status]
    if state.sent or state.failed:
        text += f" - sent {state.sent}, failed {state.failed}"
    return StatusView(text, level, state.detail)


def format_time(moment: datetime | None, tz: tzinfo) -> str:
    return "-" if moment is None else moment.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def format_ma(snapshot: MonitorSnapshot, label: str) -> str:
    return "-" if snapshot.latest_ma is None else f"{label}: {snapshot.latest_ma:.2f}"


_OUTCOME_LEVEL = {
    Outcome.DELIVERED: Level.OK,
    Outcome.REJECTED: Level.WARN,
    Outcome.STALE: Level.WARN,
    Outcome.DUPLICATE: Level.IDLE,
    Outcome.COOLDOWN: Level.IDLE,
}


@dataclass(frozen=True, slots=True)
class HistoryRow:
    cells: tuple[str, ...]
    level: Level
    direction: Direction
    delivered: bool


HISTORY_HEADERS = (
    "Candle close",
    "Direction",
    "Scenario",
    "Confirmation",
    "Price",
    "MA",
    "Result",
    "Details",
)


def history_row(record: SignalRecord, tz: tzinfo) -> HistoryRow:
    s = record.signal
    return HistoryRow(
        cells=(
            format_time(s.time, tz),
            s.direction.value,
            s.scenario.value,
            " + ".join(p.value for p in s.patterns),
            f"{s.entry_price:.2f}",
            f"{s.ma_value:.2f}",
            record.outcome.value.capitalize(),
            "; ".join(record.reasons),
        ),
        level=_OUTCOME_LEVEL[record.outcome],
        direction=s.direction,
        delivered=record.outcome is Outcome.DELIVERED,
    )


def last_signal_text(record: SignalRecord | None, tz: tzinfo) -> str:
    if record is None:
        return "No signal yet."
    s = record.signal
    return (
        f"{s.direction.value}  {s.symbol}  {s.timeframe.value}  -  {s.scenario.label} "
        f"(Scenario {s.scenario.value})\n"
        f"{' + '.join(p.value for p in s.patterns)} at {s.entry_price:.2f}  "
        f"(MA {s.ma_value:.2f})  -  {format_time(s.time, tz)}\n{s.reason}"
    )
