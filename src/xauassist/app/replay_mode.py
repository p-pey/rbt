"""Replay mode: run the whole app against a CSV history instead of a live MT5 terminal.

Candles are revealed one by one on a simulated clock, so every real code path runs - warm-up,
detection, validation, de-duplication, sound, Telegram, GUI - with no broker needed. Useful for
trying the app on any OS and for checking what it would have alerted on historical data.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path

from xauassist.data.csv_loader import load_candles_csv
from xauassist.data.replay_provider import ReplayProvider
from xauassist.domain.candle import Candle
from xauassist.domain.enums import Timeframe


class ReplaySession:
    def __init__(
        self,
        candles: list[Candle],
        timeframe: Timeframe,
        *,
        start_visible: int = 300,
        interval_seconds: float = 1.0,
        symbol: str = "XAUUSD",
    ) -> None:
        if len(candles) < 2:
            raise ValueError("replay needs at least 2 candles")
        self._candles = candles
        self._step = timedelta(minutes=timeframe.minutes)
        self._interval = interval_seconds
        self.provider = ReplayProvider(
            candles, symbol=symbol, visible=min(start_visible, len(candles))
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def from_csv(cls, path: Path, timeframe: Timeframe, **kwargs: float) -> ReplaySession:
        return cls(load_candles_csv(path), timeframe, **kwargs)  # type: ignore[arg-type]

    def clock(self) -> datetime:
        """Simulated 'now': just after the latest revealed candle closed."""
        last = self._candles[self.provider.visible - 1]
        return last.time_open + self._step + timedelta(seconds=3)

    @property
    def finished(self) -> bool:
        return self.provider.visible >= len(self._candles)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="replay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)

    def _run(self) -> None:
        while not self.finished and not self._stop.wait(self._interval):
            self.provider.reveal(1)
