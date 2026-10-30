"""Human-readable alert text and display-timezone handling."""

from __future__ import annotations

import re
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xauassist.config.models import ConfigError
from xauassist.domain.enums import Direction
from xauassist.domain.signal import Signal


def resolve_timezone(name: str) -> tzinfo:
    """ "local" -> the machine's timezone; otherwise an IANA name ("UTC", "Europe/Berlin")."""
    if name.strip().lower() == "local":
        local = datetime.now().astimezone().tzinfo
        assert local is not None
        return local
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"display.timezone: unknown timezone {name!r}") from exc


def _utc_label(moment: datetime) -> str:
    offset = moment.utcoffset()
    minutes = int(offset.total_seconds() // 60) if offset is not None else 0
    sign = "+" if minutes >= 0 else "-"
    return f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def _strategy_label(signal: Signal) -> str:
    ma_label = str(signal.info.get("ma_label") or "MA")
    digits = re.search(r"\d+", ma_label)
    base = signal.scenario.label  # "MA Retest" / "MA Break + Pullback"
    named = base.replace("MA", f"MA{digits.group()}", 1) if digits else base
    return f"{named} (Scenario {signal.scenario.value})"


def format_signal_message(signal: Signal, tz: tzinfo) -> str:
    """Plain text (no markup, so nothing needs escaping)."""
    local = signal.time.astimezone(tz)
    icon = "🟢" if signal.direction is Direction.BUY else "🔴"
    ma_label = str(signal.info.get("ma_label") or "MA")
    market: list[str] = []
    spread = signal.info.get("spread_points")
    if isinstance(spread, float):
        market.append(f"Spread: {spread:g} pts")
    atr = signal.info.get("atr")
    if isinstance(atr, float):
        market.append(f"ATR: {atr:.2f}")
    htf = signal.info.get("htf_trend")
    if isinstance(htf, str) and htf != "off":
        market.append(f"HTF trend: {htf}")
    lines = [
        f"{icon} GOLD {signal.direction.value} SIGNAL",
        "",
        f"Symbol: {signal.symbol}",
        f"Timeframe: {signal.timeframe.value}",
        f"Strategy: {_strategy_label(signal)}",
        f"Confirmation: {' + '.join(p.value for p in signal.patterns)}",
        "",
        f"Entry Price: {signal.entry_price:.2f}",
        f"{ma_label}: {signal.ma_value:.2f}",
    ]
    if market:
        lines.append(" | ".join(market))
    lines += [
        "",
        f"Time: {local:%H:%M} ({local:%Y-%m-%d}, {_utc_label(local)})",
        "",
        "Reason:",
        signal.reason,
        "",
        "Alert only - no order has been placed.",
    ]
    return "\n".join(lines)
