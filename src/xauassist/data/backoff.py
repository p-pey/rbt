"""Exponential backoff schedule for reconnect attempts."""

from __future__ import annotations


class Backoff:
    def __init__(self, initial: float, maximum: float, factor: float = 2.0) -> None:
        if not 0 < initial <= maximum or factor < 1:
            raise ValueError("require 0 < initial <= maximum and factor >= 1")
        self._initial = initial
        self._maximum = maximum
        self._factor = factor
        self._next = initial

    def next_delay(self) -> float:
        delay = self._next
        self._next = min(self._next * self._factor, self._maximum)
        return delay

    def reset(self) -> None:
        self._next = self._initial
