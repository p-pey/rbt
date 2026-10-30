"""Serialise AppConfig back to TOML (used by the Settings dialog)."""

from __future__ import annotations

import json
import os
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from xauassist.config.models import AppConfig


def config_to_dict(obj: object) -> dict[str, Any]:
    """Nested plain dict of a config dataclass; enums become their string values."""
    assert is_dataclass(obj) and not isinstance(obj, type)
    out: dict[str, Any] = {}
    for f in fields(obj):
        value = getattr(obj, f.name)
        if is_dataclass(value):
            out[f.name] = config_to_dict(value)
        elif isinstance(value, Enum):
            out[f.name] = value.value
        else:
            out[f.name] = value
    return out


def _value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)  # JSON string escapes are valid TOML basic-string escapes
    raise TypeError(f"unsupported config value {v!r}")


def _dump(path: list[str], table: dict[str, Any], out: list[str]) -> None:
    scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
    children = {k: v for k, v in table.items() if isinstance(v, dict)}
    if path and scalars:
        out.append(f"[{'.'.join(path)}]")
        out.extend(f"{k} = {_value(v)}" for k, v in scalars.items())
        out.append("")
    for name, child in children.items():
        _dump([*path, name], child, out)


def dumps_config(config: AppConfig) -> str:
    header = [
        "# XAUUSD assistant configuration (written by the Settings dialog).",
        "# Credentials are NOT stored here. See config.example.toml for documentation.",
        "",
    ]
    body: list[str] = []
    _dump([], config_to_dict(config), body)
    return "\n".join([*header, *body]).rstrip() + "\n"


def save_config(config: AppConfig, path: Path) -> None:
    """Atomic write: a crash mid-save can never leave a truncated config behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(dumps_config(config), encoding="utf-8")
    os.replace(tmp, path)
