from datetime import timedelta

from fakes import FakeClock, FlakyProvider, utc
from helpers import make_config, scenario_a_sell, scenario_b_sell
from xauassist.config.models import FeedConfig
from xauassist.data.errors import DataUnavailableError, SymbolNotFoundError
from xauassist.data.feed import DataFeed, FeedStatus
from xauassist.data.provider import ConnectionStatus
from xauassist.domain.candle import Candle
from xauassist.domain.enums import Timeframe
from xauassist.strategy.engine import StrategyEngine

PATTERN = r"XAUUSD[\.\w]*"
CFG = FeedConfig(backfill_retries=2, min_history_bars=20, history_bars=500)


def build(
    candles: list[Candle], visible: int, cfg: FeedConfig = CFG
) -> tuple[FlakyProvider, DataFeed, FakeClock]:
    provider = FlakyProvider(candles, visible=visible)
    clock = FakeClock(candles[visible - 1].time_open + timedelta(minutes=5, seconds=2))
    return provider, DataFeed(provider, Timeframe.M5, PATTERN, cfg, clock=clock), clock


def test_bootstrap_delivers_history_flagged_for_warmup_only() -> None:
    candles = scenario_a_sell()
    provider, feed, _ = build(candles, visible=100)
    update = feed.bootstrap()
    assert update.status is FeedStatus.OK and update.is_history and update.data_complete
    assert len(update.candles) == 100 and feed.symbol == "XAUUSD"


def test_poll_delivers_each_new_candle_exactly_once() -> None:
    candles = scenario_a_sell()
    provider, feed, _ = build(candles, visible=100)
    feed.bootstrap()
    assert feed.poll().status is FeedStatus.NO_NEW_DATA
    provider.reveal(2)
    update = feed.poll()
    assert update.status is FeedStatus.OK and not update.is_history
    assert [c.time_open for c in update.candles] == [c.time_open for c in candles[100:102]]
    assert feed.poll().status is FeedStatus.NO_NEW_DATA  # nothing re-delivered


def test_first_poll_without_bootstrap_bootstraps() -> None:
    _, feed, _ = build(scenario_a_sell(), visible=100)
    assert feed.poll().is_history


def test_market_closed_vs_no_new_data() -> None:
    provider, feed, _ = build(scenario_a_sell(), visible=100)
    feed.bootstrap()
    provider.tick_age = 600.0
    assert feed.poll().status is FeedStatus.MARKET_CLOSED
    provider.tick_age = None
    assert feed.poll().status is FeedStatus.NO_NEW_DATA


# ---- connection failures ----


def test_terminal_missing_then_recovers_with_backoff() -> None:
    provider, feed, clock = build(scenario_a_sell(), visible=100)
    provider.connect_status = ConnectionStatus.TERMINAL_MISSING
    provider.connect_message = "MetaTrader5 package not available"
    first = feed.poll()
    assert first.status is FeedStatus.TERMINAL_MISSING and "not available" in first.message
    # Immediately polling again must not hammer the terminal.
    calls = provider.connect_calls
    waiting = feed.poll()
    assert waiting.status is FeedStatus.RECONNECTING and provider.connect_calls == calls
    # After the backoff elapses it retries, and succeeds once the terminal is available.
    provider.connect_status = ConnectionStatus.CONNECTED
    clock.advance(3)
    assert feed.poll().status is FeedStatus.OK


def test_backoff_grows_and_resets_after_success() -> None:
    provider, feed, clock = build(scenario_a_sell(), visible=100)
    provider.connect_status = ConnectionStatus.FAILED
    waits = []
    for _ in range(4):
        feed.poll()
        before = clock.now
        waiting = feed.poll()
        assert waiting.status is FeedStatus.RECONNECTING
        delay = float(waiting.message.split("in ")[1].split("s")[0])
        waits.append(delay)
        clock.advance(delay + 0.1)
        assert clock.now > before
    assert waits[0] <= waits[1] <= waits[2] and waits[-1] <= CFG.reconnect_max_seconds
    provider.connect_status = ConnectionStatus.CONNECTED
    assert feed.poll().status is FeedStatus.OK
    provider.lost_connection = True  # lose the connection again: backoff restarts from the minimum
    provider.connect_status = ConnectionStatus.FAILED
    feed.poll()
    assert "retrying in 2s" in feed.poll().message


def test_connection_lost_mid_session_then_resumes_without_duplicates() -> None:
    candles = scenario_a_sell()
    provider, feed, clock = build(candles, visible=100)
    feed.bootstrap()
    provider.reveal(1)
    provider.lost_connection = True
    provider.connect_status = ConnectionStatus.DISCONNECTED
    assert feed.poll().status is FeedStatus.DISCONNECTED
    provider.connect_status = ConnectionStatus.CONNECTED
    provider.reveal(1)  # a candle closes while we are offline
    clock.advance(5)
    update = feed.poll()
    assert update.status is FeedStatus.OK
    assert [c.time_open for c in update.candles] == [c.time_open for c in candles[100:102]]
    assert update.data_complete


def test_fetch_error_marks_disconnected_and_reconnects() -> None:
    provider, feed, clock = build(scenario_a_sell(), visible=100)
    feed.bootstrap()
    provider.reveal(1)
    provider.fail_next_fetches = 1
    assert feed.poll().status is FeedStatus.DISCONNECTED
    clock.advance(5)
    assert feed.poll().status is FeedStatus.OK


def test_history_still_syncing_is_a_data_error_not_a_disconnect() -> None:
    provider, feed, clock = build(scenario_a_sell(), visible=100)
    feed.bootstrap()
    provider.fail_with = DataUnavailableError
    provider.fail_next_fetches = 1
    provider.reveal(1)
    assert feed.poll().status is FeedStatus.DATA_ERROR
    calls = provider.connect_calls
    clock.advance(5)
    assert feed.poll().status is FeedStatus.OK
    assert provider.connect_calls == calls  # no reconnect needed


def test_symbol_missing_is_reported() -> None:
    candles = scenario_a_sell()
    provider = FlakyProvider(candles, symbol="EURUSD")
    feed = DataFeed(provider, Timeframe.M5, PATTERN, CFG, clock=FakeClock(utc(2026, 1, 6, 12)))
    update = feed.poll()
    assert update.status is FeedStatus.SYMBOL_MISSING and "Market Watch" in update.message


def test_symbol_disappearing_mid_session() -> None:
    provider, feed, clock = build(scenario_a_sell(), visible=100)
    feed.bootstrap()
    provider.fail_with = SymbolNotFoundError
    provider.fail_next_fetches = 1
    assert feed.poll().status is FeedStatus.SYMBOL_MISSING
    clock.advance(5)
    provider.reveal(1)
    assert feed.poll().status is FeedStatus.OK and feed.symbol == "XAUUSD"


# ---- missing candles ----


def test_missing_candles_are_waited_for_then_backfilled() -> None:
    candles = scenario_a_sell()
    provider, feed, _ = build(candles, visible=100)
    feed.bootstrap()
    provider.reveal(4)
    provider.hide = {candles[101].time_open, candles[102].time_open}  # terminal has not synced them
    waiting = feed.poll()
    assert waiting.status is FeedStatus.DATA_GAP and not waiting.candles
    assert "2 candle(s) missing" in waiting.message
    provider.hide = set()  # history arrives
    update = feed.poll()
    assert update.status is FeedStatus.OK and update.data_complete
    assert [c.time_open for c in update.candles] == [c.time_open for c in candles[100:104]]


def test_unfilled_gap_is_delivered_flagged_incomplete_after_retries() -> None:
    candles = scenario_a_sell()
    provider, feed, _ = build(candles, visible=100)
    feed.bootstrap()
    provider.reveal(4)
    provider.hide = {candles[101].time_open}
    assert feed.poll().status is FeedStatus.DATA_GAP
    assert feed.poll().status is FeedStatus.DATA_GAP
    final = feed.poll()  # retries (2) exhausted
    assert final.status is FeedStatus.OK and not final.data_complete
    assert [c.time_open for c in final.candles] == [
        candles[100].time_open,
        candles[102].time_open,
        candles[103].time_open,
    ]
    assert any(not g.expected for g in final.gaps)
    assert feed.poll().status is FeedStatus.NO_NEW_DATA  # state moved on, nothing repeated


def test_bootstrap_trims_history_to_clean_tail() -> None:
    candles = scenario_a_sell()
    provider = FlakyProvider(candles, visible=140)
    provider.hide = {candles[40].time_open, candles[41].time_open}  # 03:20, mid-session
    feed = DataFeed(provider, Timeframe.M5, PATTERN, CFG, clock=FakeClock(utc(2026, 1, 6, 12)))
    update = feed.bootstrap()
    assert update.data_complete and update.candles[0].time_open == candles[42].time_open


def test_bootstrap_flags_incomplete_when_clean_tail_too_short() -> None:
    candles = scenario_a_sell()
    provider = FlakyProvider(candles, visible=60)
    provider.hide = {candles[50].time_open}
    cfg = FeedConfig(min_history_bars=30, history_bars=500)
    feed = DataFeed(provider, Timeframe.M5, PATTERN, cfg, clock=FakeClock(utc(2026, 1, 6, 12)))
    update = feed.bootstrap()
    assert not update.data_complete and len(update.candles) == 59


def test_reset_rebootstraps() -> None:
    provider, feed, _ = build(scenario_a_sell(), visible=100)
    feed.bootstrap()
    feed.reset()
    assert feed.poll().is_history


def test_current_spread() -> None:
    provider, feed, _ = build(scenario_a_sell(), visible=100)
    assert feed.current_spread_points() is None  # symbol not resolved yet
    feed.bootstrap()
    assert feed.current_spread_points() == 20.0


# ---- end to end: feed -> engine ----


def drive(candles: list[Candle], warm: int, polls_between: int = 1) -> list:  # type: ignore[type-arg]
    provider, feed, clock = build(candles, visible=warm)
    engine = StrategyEngine(make_config(), "XAUUSD")
    boot = feed.bootstrap()
    for c in boot.candles:
        engine.on_candle_closed(c)  # warm-up; signals from history are discarded
    signals = []
    for _ in range(len(candles) - warm):
        provider.reveal(1)
        clock.advance(300)
        for _ in range(polls_between):  # repeated polls must not repeat candles or signals
            for c in feed.poll().candles:
                signals.extend(engine.on_candle_closed(c))
    return signals


def test_end_to_end_scenario_a_through_feed_and_engine() -> None:
    candles = scenario_a_sell()
    (sig,) = drive(candles, warm=len(candles) - 6, polls_between=3)
    assert sig.bar_open_time == candles[-1].time_open


def test_end_to_end_scenario_b_through_feed_and_engine() -> None:
    candles, confirm = scenario_b_sell()
    (sig,) = drive(candles, warm=len(candles) - 8)
    assert sig.bar_open_time == candles[confirm].time_open


def test_history_signals_are_not_replayed_as_live() -> None:
    """Bootstrapping with a history that already contains a setup must not alert on it."""
    candles = scenario_a_sell()
    provider, feed, _ = build(candles, visible=len(candles))
    engine = StrategyEngine(make_config(), "XAUUSD")
    boot = feed.bootstrap()
    historic = [s for c in boot.candles for s in engine.on_candle_closed(c)]
    assert boot.is_history and historic  # the setup is in the past: caller must discard these
    assert feed.poll().candles == ()  # and the live stream starts clean
