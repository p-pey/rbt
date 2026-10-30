from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from helpers import make_config, run, scenario_a_sell
from xauassist.app.monitor_service import MonitorSnapshot
from xauassist.app.pipeline import Outcome, SignalRecord
from xauassist.config.loader import load_config
from xauassist.config.models import (
    AppConfig,
    ConfigError,
    EngulfingConfig,
    MaConfig,
    PinBarConfig,
    StrategyConfig,
    ValidationConfig,
)
from xauassist.config.writer import config_to_dict, dumps_config, save_config
from xauassist.data.feed import FeedStatus
from xauassist.domain.enums import Direction, MaMethod, Timeframe
from xauassist.gui.forms import FieldKind, apply_values, current_values, field_specs, sections
from xauassist.gui.view_models import (
    HISTORY_HEADERS,
    Level,
    connection_view,
    format_ma,
    format_time,
    history_row,
    last_signal_text,
    telegram_view,
)
from xauassist.notify.telegram import TelegramState, TelegramStatus

# ---------------- writer ----------------


def test_default_config_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    save_config(AppConfig(), path)
    assert load_config(path) == AppConfig()
    assert not (tmp_path / "config.toml.tmp").exists()


def test_modified_config_round_trips_including_tricky_strings(tmp_path: Path) -> None:
    cfg = AppConfig(
        strategy=StrategyConfig(
            ma=MaConfig(period=50, method=MaMethod.SMA),
            pin_bar=PinBarConfig(wick_body_ratio=3.5, min_range=0.25),
            engulfing=EngulfingConfig(strict=True),
        ),
        validation=ValidationConfig(symbol_pattern=r'GOLD[\.\w]*"x"', max_spread_points=12.5),
    )
    path = tmp_path / "sub" / "config.toml"
    save_config(cfg, path)
    assert load_config(path) == cfg


def test_writer_output_is_documented_and_has_no_secrets() -> None:
    text = dumps_config(AppConfig())
    assert text.startswith("#") and "[strategy.ma]" in text and "[telegram]" in text
    assert "bot_token" not in text and "chat_id" not in text


def test_config_to_dict_uses_plain_values() -> None:
    d = config_to_dict(AppConfig())
    assert d["strategy"]["ma"]["method"] == "EMA" and d["strategy"]["timeframe"] == "M5"


# ---------------- forms ----------------


def test_specs_cover_every_config_field_exactly_once() -> None:
    specs = field_specs()
    assert {s.path for s in specs} == set(current_values(AppConfig()))
    assert len({s.path for s in specs}) == len(specs)


def test_spec_kinds_and_choices() -> None:
    by_key = {s.key: s for s in field_specs()}
    assert by_key["strategy.ma.period"].kind is FieldKind.INT
    assert by_key["strategy.pin_bar.wick_body_ratio"].kind is FieldKind.FLOAT
    assert by_key["strategy.trend.htf_enabled"].kind is FieldKind.BOOL
    assert by_key["display.timezone"].kind is FieldKind.TEXT
    method = by_key["strategy.ma.method"]
    assert method.kind is FieldKind.CHOICE and method.choices == ("EMA", "SMA")
    assert by_key["strategy.zone.mode"].choices == ("PRICE", "PERCENT", "ATR")
    assert by_key["strategy.ma.period"].label == "Period" and by_key["strategy.ma.period"].help


def test_sections_group_by_top_level_name() -> None:
    grouped = sections(field_specs())
    assert list(grouped) == [
        "strategy",
        "validation",
        "signals",
        "mt5",
        "feed",
        "telegram",
        "sound",
        "display",
        "monitor",
        "backtest",
    ]
    assert all(s.path[0] == name for name, specs in grouped.items() for s in specs)


def test_apply_values_changes_only_what_was_edited() -> None:
    base = AppConfig()
    new = apply_values(
        base,
        {
            ("strategy", "ma", "period"): 34,
            ("strategy", "ma", "method"): "SMA",
            ("validation", "max_spread_points"): 25,  # an int from a spin box becomes a float
        },
    )
    assert new.strategy.ma == MaConfig(period=34, method=MaMethod.SMA)
    assert new.validation.max_spread_points == 25.0
    assert replace(new, strategy=base.strategy, validation=base.validation) == base
    assert base.strategy.ma.period == 20  # original untouched


def test_apply_values_rejects_invalid_input_with_a_readable_message() -> None:
    with pytest.raises(ConfigError, match="period"):
        apply_values(AppConfig(), {("strategy", "ma", "period"): 1})
    with pytest.raises(ConfigError):
        apply_values(AppConfig(), {("strategy", "ma", "method"): "WMA"})


def test_applying_current_values_is_a_no_op() -> None:
    cfg = AppConfig(strategy=make_config())
    assert apply_values(cfg, current_values(cfg)) == cfg


# ---------------- view models ----------------

NOW = datetime(2026, 1, 5, 11, 10, tzinfo=UTC)


def snap(
    running: bool = True,
    status: FeedStatus | None = FeedStatus.OK,
    message: str = "",
    latest_ma: float | None = 2713.456,
) -> MonitorSnapshot:
    return MonitorSnapshot(
        running=running,
        feed_status=status,
        message=message,
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        latest_ma=latest_ma,
        last_candle_time=NOW,
        last_signal=None,
        updated_at=NOW,
    )


@pytest.mark.parametrize(
    ("status", "level"),
    [
        (FeedStatus.OK, Level.OK),
        (FeedStatus.NO_NEW_DATA, Level.OK),
        (FeedStatus.MARKET_CLOSED, Level.INFO),
        (FeedStatus.DATA_GAP, Level.WARN),
        (FeedStatus.DATA_ERROR, Level.WARN),
        (FeedStatus.RECONNECTING, Level.WARN),
        (FeedStatus.DISCONNECTED, Level.ERROR),
        (FeedStatus.TERMINAL_MISSING, Level.ERROR),
        (FeedStatus.SYMBOL_MISSING, Level.ERROR),
    ],
)
def test_every_feed_status_has_a_view(status: FeedStatus, level: Level) -> None:
    view = connection_view(snap(status=status, message="detail here"))
    assert view.level is level and view.text and view.detail == "detail here"


def test_stopped_and_starting_views() -> None:
    assert connection_view(snap(running=False)).text == "Stopped"
    assert connection_view(snap(status=None)).text == "Starting..."


@pytest.mark.parametrize("status", list(TelegramStatus))
def test_every_telegram_status_has_a_view(status: TelegramStatus) -> None:
    assert telegram_view(TelegramState(status)).text


def test_telegram_view_shows_counters_and_detail() -> None:
    view = telegram_view(TelegramState(TelegramStatus.ERROR, "HTTP 401", sent=3, failed=1))
    assert (
        view.level is Level.ERROR and "sent 3, failed 1" in view.text and view.detail == "HTTP 401"
    )


def test_formatting_helpers() -> None:
    assert format_ma(snap(), "EMA20") == "EMA20: 2713.46"
    assert format_ma(snap(latest_ma=None), "EMA20") == "-"
    assert format_time(NOW, timezone(timedelta(hours=3, minutes=30))) == "2026-01-05 14:40"
    assert format_time(None, UTC) == "-"


def record(outcome: Outcome, reasons: tuple[str, ...] = ()) -> SignalRecord:
    (sig,) = run(scenario_a_sell(), make_config())
    return SignalRecord(sig, outcome, reasons, NOW)


def test_history_row_content_and_levels() -> None:
    row = history_row(record(Outcome.DELIVERED), UTC)
    assert len(row.cells) == len(HISTORY_HEADERS)
    assert row.cells[1:4] == ("SELL", "A", "Bearish Engulfing") and row.cells[6] == "Delivered"
    assert row.level is Level.OK and row.delivered and row.direction is Direction.SELL
    rejected = history_row(record(Outcome.REJECTED, ("spread 80 > max 50",)), UTC)
    assert rejected.level is Level.WARN and not rejected.delivered
    assert rejected.cells[7] == "spread 80 > max 50"
    assert history_row(record(Outcome.DUPLICATE), UTC).level is Level.IDLE


def test_last_signal_text() -> None:
    assert last_signal_text(None, UTC) == "No signal yet."
    text = last_signal_text(record(Outcome.DELIVERED), UTC)
    assert (
        text.startswith("SELL  XAUUSD  M5") and "Bearish Engulfing" in text and "retested" in text
    )
