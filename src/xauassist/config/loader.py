"""Load typed configuration from a TOML file."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar, get_type_hints

from xauassist.config.models import AppConfig, ConfigError

T = TypeVar("T")


def load_config(path: Path) -> AppConfig:
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc
    return build_config(AppConfig, raw)


def build_config(cls: type[T], data: Mapping[str, Any], where: str = "") -> T:
    """Recursively build dataclass `cls` from `data`, rejecting unknown keys and wrong types."""
    names = {f.name for f in fields(cls)}  # type: ignore[arg-type]
    unknown = set(data) - names
    if unknown:
        raise ConfigError(f"Unknown config key(s) in [{where or 'root'}]: {sorted(unknown)}")
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for name in names & set(data):
        kwargs[name] = _convert(hints[name], data[name], f"{where}.{name}".lstrip("."))
    return cls(**kwargs)


def _convert(expected: Any, value: Any, where: str) -> Any:
    if is_dataclass(expected):
        if not isinstance(value, Mapping):
            raise ConfigError(f"[{where}] must be a table")
        return build_config(expected, value, where)  # type: ignore[arg-type]
    if isinstance(expected, type) and issubclass(expected, Enum):
        try:
            return expected(str(value).upper())
        except ValueError as exc:
            allowed = [m.value for m in expected]
            raise ConfigError(f"{where}: {value!r} not one of {allowed}") from exc
    if expected is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{where}: expected true/false, got {value!r}")
        return value
    if expected is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where}: expected integer, got {value!r}")
        return value
    if expected is float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"{where}: expected number, got {value!r}")
        return float(value)
    if expected is str:
        if not isinstance(value, str):
            raise ConfigError(f"{where}: expected string, got {value!r}")
        return value
    raise ConfigError(f"{where}: unsupported config type {expected!r}")
