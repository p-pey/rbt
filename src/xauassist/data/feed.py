"""DataFeed: turns a MarketDataProvider into a reliable stream of new closed candles.

Responsibilities (everything the raw provider does not do):
  - connect / reconnect with exponential backoff, without ever blocking the caller;
  - resolve the broker symbol (XAUUSD, XAUUSD.a, ...);
  - deliver each closed candle exactly once, oldest first;
  - detect missing candles, wait for the terminal to back-fill them, and flag data that stayed
    incomplete so signals built on it can be suppressed;
  - distinguish "market closed" from "disconnected".

Usage contract for the caller (the monitor):
  - candles with `is_history=True` are for indicator warm-up only; signals produced while feeding
    them must be discarded;
  - when `data_complete` is False, call `engine.reset_setups()` and pass `data_complete=False` to
    the signal validator;
  - `reset()` forces a fresh `bootstrap()` on the next poll (e.g. after a long outage).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from xauassist.config.models import FeedConfig
from xauassist.data.backoff import Backoff
from xauassist.data.errors import DataUnavailableError, ProviderError, SymbolNotFoundError
from xauassist.data.gaps import Gap, find_gaps
from xauassist.data.provider import ConnectionStatus, MarketDataProvider
from xauassist.domain.candle import Candle
from xauassist.domain.enums import Timeframe


class FeedStatus(StrEnum):
    OK = "ok"  # new candles delivered
    NO_NEW_DATA = "no_new_data"
    MARKET_CLOSED = "market_closed"
    DATA_GAP = "data_gap"  # waiting for missing candles to appear
    DATA_ERROR = "data_error"  # connected, but no usable data (e.g. history still syncing)
    DISCONNECTED = "disconnected"
    TERMINAL_MISSING = "terminal_missing"
    SYMBOL_MISSING = "symbol_missing"
    RECONNECTING = "reconnecting"  # backing off before the next attempt


@dataclass(frozen=True, slots=True)
class FeedUpdate:
    status: FeedStatus
    candles: tuple[Candle, ...] = ()
    data_complete: bool = True
    is_history: bool = False
    gaps: tuple[Gap, ...] = ()
    message: str = ""


class DataFeed:
    def __init__(
        self,
        provider: MarketDataProvider,
        timeframe: Timeframe,
        symbol_pattern: str,
        config: FeedConfig,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._provider = provider
        self._tf = timeframe
        self._pattern = symbol_pattern
        self._cfg = config
        self._clock = clock
        self._backoff = Backoff(config.reconnect_initial_seconds, config.reconnect_max_seconds)
        self._next_attempt: datetime | None = None
        self._last_failure: FeedUpdate | None = None
        self._connected = False
        self._symbol: str | None = None
        self._last_time: datetime | None = None  # open time of the last candle delivered
        self._gap_waits = 0

    # ---- public API ----

    @property
    def symbol(self) -> str | None:
        return self._symbol

    def reset(self) -> None:
        """Forget delivery state; the next `poll()` re-bootstraps from history."""
        self._last_time = None
        self._gap_waits = 0

    def current_spread_points(self) -> float | None:
        """Live spread for the validator, or None if it cannot be read."""
        if self._symbol is None:
            return None
        try:
            return self._provider.get_spread_points(self._symbol)
        except ProviderError:
            return None

    def bootstrap(self) -> FeedUpdate:
        """Connect and fetch warm-up history (marked `is_history`)."""
        not_ready = self._ensure_ready()
        if not_ready is not None:
            return not_ready
        assert self._symbol is not None
        try:
            fetched = self._provider.get_closed_candles(
                self._symbol, self._tf, self._cfg.history_bars
            )
        except ProviderError as exc:
            return self._handle_provider_error(exc)
        self._succeeded()
        candles = _normalize(fetched)
        if not candles:
            return self._fail(FeedStatus.DATA_ERROR, "provider returned no closed candles")
        gaps = find_gaps([c.time_open for c in candles], self._tf, self._cfg)
        unexpected = [g for g in gaps if not g.expected]
        complete = True
        if unexpected:
            tail = [c for c in candles if c.time_open >= unexpected[-1].end]
            if len(tail) >= self._cfg.min_history_bars:
                candles = tail  # older holes are irrelevant once enough clean history follows
            else:
                complete = False
        self._last_time = candles[-1].time_open
        self._gap_waits = 0
        return FeedUpdate(
            FeedStatus.OK,
            tuple(candles),
            data_complete=complete,
            is_history=True,
            gaps=tuple(gaps),
            message=f"{len(candles)} history candles",
        )

    def poll(self) -> FeedUpdate:
        """Check for newly closed candles. Never blocks, never raises provider errors."""
        if self._last_time is None:
            return self.bootstrap()
        not_ready = self._ensure_ready()
        if not_ready is not None:
            return not_ready
        assert self._symbol is not None
        try:
            fetched = self._provider.get_closed_candles(self._symbol, self._tf, self._fetch_count())
        except ProviderError as exc:
            return self._handle_provider_error(exc)
        self._succeeded()

        last = self._last_time
        new = [c for c in _normalize(fetched) if c.time_open > last]
        if not new:
            closed = self._market_closed()
            return FeedUpdate(FeedStatus.MARKET_CLOSED if closed else FeedStatus.NO_NEW_DATA)

        gaps = find_gaps([last, *(c.time_open for c in new)], self._tf, self._cfg)
        unexpected = [g for g in gaps if not g.expected]
        if unexpected and self._gap_waits < self._cfg.backfill_retries:
            self._gap_waits += 1  # give the terminal time to back-fill, then re-fetch deeper
            return FeedUpdate(
                FeedStatus.DATA_GAP,
                gaps=tuple(gaps),
                message=(
                    f"{sum(g.missing_bars for g in unexpected)} candle(s) missing; waiting "
                    f"({self._gap_waits}/{self._cfg.backfill_retries})"
                ),
            )
        self._gap_waits = 0
        self._last_time = new[-1].time_open
        return FeedUpdate(
            FeedStatus.OK,
            tuple(new),
            data_complete=not unexpected,
            gaps=tuple(gaps),
            message="gap not back-filled; data incomplete" if unexpected else "",
        )

    # ---- connection management ----

    def _ensure_ready(self) -> FeedUpdate | None:
        """None if connected with a resolved symbol; otherwise the failure/backoff update."""
        now = self._clock()
        if self._next_attempt is not None and now < self._next_attempt:
            wait = (self._next_attempt - now).total_seconds()
            reason = self._last_failure.message if self._last_failure else ""
            return FeedUpdate(
                FeedStatus.RECONNECTING, message=f"retrying in {wait:.0f}s (last error: {reason})"
            )
        if self._connected and not self._provider.is_connected():
            self._connected = False
            self._symbol = None
        if not self._connected:
            info = self._provider.connect()
            if info.status is not ConnectionStatus.CONNECTED:
                status = (
                    FeedStatus.TERMINAL_MISSING
                    if info.status is ConnectionStatus.TERMINAL_MISSING
                    else FeedStatus.DISCONNECTED
                )
                return self._fail(status, info.message)
            self._connected = True
        if self._symbol is None:
            try:
                symbol = self._provider.resolve_symbol(self._pattern)
            except ProviderError as exc:
                return self._handle_provider_error(exc)
            if symbol is None:
                return self._fail(
                    FeedStatus.SYMBOL_MISSING,
                    f"no broker symbol matches {self._pattern!r} (add it to Market Watch)",
                )
            self._symbol = symbol
        return None

    def _handle_provider_error(self, exc: ProviderError) -> FeedUpdate:
        if isinstance(exc, SymbolNotFoundError):
            self._symbol = None
            return self._fail(FeedStatus.SYMBOL_MISSING, str(exc))
        if isinstance(exc, DataUnavailableError):
            return self._fail(FeedStatus.DATA_ERROR, str(exc))
        self._connected = False  # treat as a lost connection; reconnect on the next attempt
        self._symbol = None
        return self._fail(FeedStatus.DISCONNECTED, str(exc))

    def _fail(self, status: FeedStatus, message: str) -> FeedUpdate:
        delay = self._backoff.next_delay()
        self._next_attempt = self._clock() + timedelta(seconds=delay)
        self._last_failure = FeedUpdate(status, message=message)
        return self._last_failure

    def _succeeded(self) -> None:
        self._backoff.reset()
        self._next_attempt = None
        self._last_failure = None

    # ---- helpers ----

    def _fetch_count(self) -> int:
        if self._gap_waits or self._last_time is None:
            return self._cfg.history_bars
        elapsed_bars = int((self._clock() - self._last_time) / timedelta(minutes=self._tf.minutes))
        return min(self._cfg.history_bars, max(3, elapsed_bars + 3))

    def _market_closed(self) -> bool:
        assert self._symbol is not None
        try:
            age = self._provider.last_tick_age_seconds(self._symbol)
        except ProviderError:
            return False
        return age is not None and age > self._cfg.stale_tick_seconds


def _normalize(candles: Iterable[Candle]) -> list[Candle]:
    """Closed candles only, unique by open time, oldest first."""
    by_time = {c.time_open: c for c in candles if c.is_closed}
    return [by_time[t] for t in sorted(by_time)]
