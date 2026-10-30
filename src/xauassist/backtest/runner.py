"""Backtest runner: replays candles through the LIVE strategy path and scores the signals.

Same components as production - StrategyEngine, SignalValidator, SignalManager (de-duplication and
cooldown) and the same data-gap rules - fed one closed candle at a time, so there is no look-ahead
and nothing can repaint. Trades are scored independently (overlapping trades are allowed); this
measures signal quality, not a portfolio with position sizing.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import datetime

from xauassist.backtest.execution import ExecutionParams, simulate_trade
from xauassist.backtest.models import BacktestResult, SkippedSignal, Trade
from xauassist.config.models import AppConfig
from xauassist.data.gaps import find_gaps
from xauassist.domain.candle import Candle
from xauassist.domain.signal import Signal
from xauassist.signals.dedup_store import InMemoryDedupStore
from xauassist.signals.manager import Decision, SignalManager
from xauassist.strategy.engine import StrategyEngine
from xauassist.validation.validator import SignalValidator


def _reject_label(reasons: tuple[str, ...]) -> str:
    text = reasons[0] if reasons else "rejected"
    for key in ("spread", "incomplete", "symbol", "timeframe"):
        if key in text:
            return f"validation: {key}"
    return f"validation: {text}"


def run_backtest(
    candles: Sequence[Candle], config: AppConfig, symbol: str = "XAUUSD"
) -> BacktestResult:
    result = BacktestResult(candles=len(candles))
    if not candles:
        return result
    result.first_time, result.last_time = candles[0].time_open, candles[-1].time_open

    unexpected = [
        g
        for g in find_gaps([c.time_open for c in candles], config.strategy.timeframe, config.feed)
        if not g.expected
    ]
    result.unexpected_gaps = len(unexpected)
    gap_ends: set[datetime] = {g.end for g in unexpected}

    engine = StrategyEngine(config.strategy, symbol)
    validator = SignalValidator(config.validation)
    manager = SignalManager(InMemoryDedupStore(), config.signals)
    rejected: Counter[str] = Counter()
    accepted: list[tuple[Signal, int]] = []
    suppress_left = 0

    for index, candle in enumerate(candles):
        if candle.time_open in gap_ends:  # same rule as live: a hole clears pending setups
            engine.reset_setups()
            suppress_left = config.monitor.post_gap_suppress_bars
        signals = engine.on_candle_closed(candle)
        suppressed = suppress_left > 0
        if suppress_left > 0:
            suppress_left -= 1
        for signal in signals:
            result.raw_signals += 1
            check = validator.validate(signal, data_complete=not suppressed)
            if not check.ok:
                rejected[_reject_label(check.reasons)] += 1
                continue
            decision = manager.accept(signal)
            if decision is not Decision.ACCEPTED:
                rejected[decision.value] += 1
                continue
            accepted.append((signal, index))

    params = ExecutionParams.from_config(config.backtest)
    for signal, index in accepted:
        outcome = simulate_trade(signal, index, candles, params)
        if isinstance(outcome, Trade):
            result.trades.append(outcome)
        else:
            assert isinstance(outcome, SkippedSignal)
            result.skipped.append(outcome)
    result.rejected = dict(rejected)
    return result
