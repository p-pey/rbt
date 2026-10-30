"""Errors raised by the strategy engine for invalid input data."""

from __future__ import annotations


class IncompleteCandleError(ValueError):
    """A candle that is still forming was passed to the engine."""


class OutOfOrderCandleError(ValueError):
    """A candle older than the last processed candle was passed to the engine."""
