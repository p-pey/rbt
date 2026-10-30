"""Pre-notification signal validation gates (symbol, timeframe, spread, data completeness)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from xauassist.config.models import ValidationConfig
from xauassist.domain.signal import Signal


@dataclass(frozen=True, slots=True)
class ValidationResult:
    ok: bool
    reasons: tuple[str, ...] = ()


class SignalValidator:
    def __init__(self, config: ValidationConfig) -> None:
        self._cfg = config
        self._symbol_re = re.compile(config.symbol_pattern)

    def validate(
        self,
        signal: Signal,
        *,
        current_spread_points: float | None = None,
        data_complete: bool = True,
    ) -> ValidationResult:
        """`current_spread_points` is the live tick spread; if None, the candle's spread is used."""
        problems: list[str] = []
        if not self._symbol_re.fullmatch(signal.symbol):
            problems.append(f"symbol {signal.symbol!r} does not match {self._cfg.symbol_pattern!r}")
        if signal.timeframe is not self._cfg.expected_timeframe:
            problems.append(
                f"timeframe {signal.timeframe.value} != "
                f"expected {self._cfg.expected_timeframe.value}"
            )
        spread = current_spread_points
        if spread is None:
            candle_spread = signal.info.get("spread_points")
            spread = candle_spread if isinstance(candle_spread, float) else None
        if spread is None:
            problems.append("spread unavailable")
        elif spread > self._cfg.max_spread_points:
            problems.append(f"spread {spread} > max {self._cfg.max_spread_points}")
        if not data_complete:
            problems.append("market data incomplete")
        return ValidationResult(not problems, tuple(problems))
