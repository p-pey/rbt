"""Apply dotted-path overrides to an AppConfig (Settings form and parameter sweeps)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from xauassist.config.loader import build_config
from xauassist.config.models import AppConfig
from xauassist.config.writer import config_to_dict


def apply_overrides(config: AppConfig, values: Mapping[tuple[str, ...], Any]) -> AppConfig:
    """New config with `values` applied (path tuple -> value). Raises ConfigError if invalid."""
    data = config_to_dict(config)
    for path, value in values.items():
        table = data
        for part in path[:-1]:
            table = table[part]
        if path[-1] not in table:
            raise KeyError(".".join(path))
        table[path[-1]] = value
    return build_config(AppConfig, data)
