"""Settings form model (no Qt): describes editable fields and applies edits to a config.

The dialog generates widgets from `field_specs()`, so adding a config field automatically adds it
to the UI, and validation is the config layer's own (`build_config`), not duplicated in widgets.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum, StrEnum
from typing import Any, cast, get_type_hints

from xauassist.config.models import AppConfig
from xauassist.config.overrides import apply_overrides
from xauassist.config.writer import config_to_dict

FieldPath = tuple[str, ...]


class FieldKind(StrEnum):
    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    CHOICE = "choice"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class FieldSpec:
    path: FieldPath
    label: str
    kind: FieldKind
    choices: tuple[str, ...] = ()
    help: str = ""

    @property
    def key(self) -> str:
        return ".".join(self.path)


SECTION_TITLES = {
    "strategy": "Strategy",
    "validation": "Validation",
    "signals": "Signals",
    "mt5": "MetaTrader 5",
    "feed": "Data feed",
    "telegram": "Telegram",
    "sound": "Sound",
    "display": "Display",
    "monitor": "Monitor",
    "backtest": "Backtest",
}

GROUP_TITLES = {
    "ma": "Moving average",
    "pin_bar": "Pin bar",
    "scenario_a": "Scenario A - trend + MA retest",
    "scenario_b": "Scenario B - MA break + pullback",
    "htf": "Higher timeframe",
}

HELP = {
    "strategy.ma.period": "Moving average period (default 20).",
    "strategy.ma.method": "EMA or SMA, applied to the close price.",
    "strategy.pin_bar.wick_body_ratio": "Dominant wick must be at least this many times the body.",
    "strategy.pin_bar.opposite_wick_max_frac": "Opposite wick: at most this fraction of the range.",
    "strategy.trend.slope_lookback": "Bars used to measure the MA slope.",
    "strategy.trend.htf_enabled": "Also require the higher timeframe (closed bars only) to agree.",
    "strategy.zone.mode": "PRICE = dollars, PERCENT = % of MA, ATR = multiple of ATR.",
    "strategy.zone.value": "Half-width of the MA proximity zone, in the unit chosen above.",
    "strategy.scenario_b.max_pullback_bars": "Candles allowed between MA break and confirmation.",
    "validation.max_spread_points": "Signals are rejected above this spread (broker points).",
    "signals.cooldown_bars": "Minimum bars between signals in the same direction (0 = off).",
    "mt5.server_utc_offset_hours": "Broker server time minus UTC (often 2 winter / 3 summer).",
    "display.timezone": "'local', 'UTC' or an IANA name such as Europe/Berlin.",
    "monitor.max_signal_age_seconds": "Alerts for candles that closed longer ago are dropped.",
}


def _label(name: str) -> str:
    text = name.replace("_", " ")
    return text[:1].upper() + text[1:]


def field_specs(cls: type = AppConfig, prefix: FieldPath = ()) -> list[FieldSpec]:
    specs: list[FieldSpec] = []
    hints = get_type_hints(cls)
    for f in fields(cls):
        hint = hints[f.name]
        path = (*prefix, f.name)
        if is_dataclass(hint):
            specs.extend(field_specs(cast(type, hint), path))
            continue
        key = ".".join(path)
        if isinstance(hint, type) and issubclass(hint, Enum):
            specs.append(
                FieldSpec(
                    path,
                    _label(f.name),
                    FieldKind.CHOICE,
                    tuple(m.value for m in hint),
                    HELP.get(key, ""),
                )
            )
        elif hint is bool:
            specs.append(FieldSpec(path, _label(f.name), FieldKind.BOOL, help=HELP.get(key, "")))
        elif hint is int:
            specs.append(FieldSpec(path, _label(f.name), FieldKind.INT, help=HELP.get(key, "")))
        elif hint is float:
            specs.append(FieldSpec(path, _label(f.name), FieldKind.FLOAT, help=HELP.get(key, "")))
        elif hint is str:
            specs.append(FieldSpec(path, _label(f.name), FieldKind.TEXT, help=HELP.get(key, "")))
        else:
            raise TypeError(f"unsupported field type {hint!r} at {key}")
    return specs


def group_title(group: str) -> str:
    return GROUP_TITLES.get(group) or _label(group)


def sections(specs: list[FieldSpec]) -> dict[str, list[FieldSpec]]:
    """Specs grouped by top-level config section, in declaration order."""
    grouped: dict[str, list[FieldSpec]] = {}
    for spec in specs:
        grouped.setdefault(spec.path[0], []).append(spec)
    return grouped


def current_values(config: AppConfig) -> dict[FieldPath, Any]:
    flat: dict[FieldPath, Any] = {}

    def walk(prefix: FieldPath, table: Mapping[str, Any]) -> None:
        for key, value in table.items():
            if isinstance(value, dict):
                walk((*prefix, key), value)
            else:
                flat[(*prefix, key)] = value

    walk((), config_to_dict(config))
    return flat


def apply_values(config: AppConfig, values: Mapping[FieldPath, Any]) -> AppConfig:
    """New config with `values` applied. Raises ConfigError (with a readable message) if invalid."""
    return apply_overrides(config, values)
