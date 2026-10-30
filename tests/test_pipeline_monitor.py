import threading
import time
from dataclasses import dataclass, replace
from datetime import timedelta

from fakes import FakeClock, FlakyProvider
from helpers import make_config, run, scenario_a_sell, scenario_b_sell
from xauassist.app.monitor_service import MonitorService, MonitorSnapshot
from xauassist.app.pipeline import Outcome, SignalPipeline, SignalRecord
from xauassist.config.models import FeedConfig, MonitorConfig, SignalsConfig, ValidationConfig
from xauassist.data.feed import DataFeed, FeedStatus, FeedUpdate
from xauassist.data.replay_provider import ReplayProvider
from xauassist.domain.candle import Candle
from xauassist.domain.enums import Direction, Timeframe
from xauassist.domain.signal import Signal
from xauassist.notify.base import NotificationDispatcher
from xauassist.signals.dedup_store import InMemoryDedupStore
from xauassist.signals.manager import SignalManager
from xauassist.strategy.engine import StrategyEngine
from xauassist.validation.validator import SignalValidator

PATTERN = r"XAUUSD[\.\w]*"


class Recorder:
    def __init__(self) -> None:
        self.signals: list[Signal] = []

    def notify(self, signal: Signal) -> None:
        self.signals.append(signal)


@dataclass
class World:
    provider: FlakyProvider
    feed: DataFeed
    pipeline: SignalPipeline
    recorder: Recorder
    clock: FakeClock  # the pipeline's "now"
    candles: list[Candle]
    store: InMemoryDedupStore

    def step(self, n: int = 1) -> list[SignalRecord]:
        """Reveal `n` candles, poll once, process."""
        self.provider.reveal(n)
        return self.pipeline.handle(self.feed.poll())


def world(
    candles: list[Candle],
    warm: int,
    *,
    cooldown: int = 0,
    max_spread: float = 50.0,
    retries: int = 0,
    store: InMemoryDedupStore | None = None,
    bootstrap: bool = True,
) -> World:
    provider = FlakyProvider(candles, visible=warm)
    feed_cfg = FeedConfig(backfill_retries=retries, min_history_bars=20)
    feed_clock = FakeClock(candles[-1].time_open + timedelta(minutes=10))
    feed = DataFeed(provider, Timeframe.M5, PATTERN, feed_cfg, clock=feed_clock)
    recorder = Recorder()
    store = store if store is not None else InMemoryDedupStore()
    clock = FakeClock(candles[-1].time_open + timedelta(minutes=5, seconds=3))  # just after close
    pipeline = SignalPipeline(
        lambda: StrategyEngine(make_config(), "XAUUSD"),
        SignalValidator(ValidationConfig(max_spread_points=max_spread)),
        SignalManager(store, SignalsConfig(cooldown_bars=cooldown)),
        NotificationDispatcher([recorder]),
        feed.current_spread_points,
        MonitorConfig(),
        clock=clock,
    )
    w = World(provider, feed, pipeline, recorder, clock, candles, store)
    if bootstrap:
        assert pipeline.handle(feed.bootstrap()) == []
    return w


# ---------------- pipeline ----------------


def test_history_never_alerts_even_if_it_contains_a_setup() -> None:
    candles = scenario_a_sell()
    w = world(candles, warm=len(candles))  # the setup is in the past
    assert w.recorder.signals == [] and w.pipeline.history == ()
    assert w.pipeline.latest_ma is not None


def test_live_signal_is_delivered_exactly_once() -> None:
    candles = scenario_a_sell()
    w = world(candles, warm=len(candles) - 6)
    records: list[SignalRecord] = []
    for _ in range(6):
        records += w.step()
        w.pipeline.handle(w.feed.poll())  # an extra poll must not repeat anything
    (record,) = records
    assert record.outcome is Outcome.DELIVERED and record.signal.direction is Direction.SELL
    assert w.recorder.signals == [record.signal]
    assert w.pipeline.last_delivered == record
    assert w.pipeline.history == (record,)


def test_stale_signal_is_dropped_not_marked_and_not_sent() -> None:
    candles = scenario_a_sell()
    w = world(candles, warm=len(candles) - 1)
    w.clock.advance(3600)  # delivered an hour after the candle closed (e.g. after an outage)
    (record,) = w.step()
    assert record.outcome is Outcome.STALE and "closed" in record.reasons[0]
    assert w.recorder.signals == [] and w.pipeline.last_delivered is None
    assert not w.store.contains(record.signal.key)  # a dropped signal is not 'used up'


def test_spread_gate_rejects_signal() -> None:
    candles = scenario_a_sell()
    w = world(candles, warm=len(candles) - 1, max_spread=10.0)
    w.provider._spread = 80.0  # live spread blows out at the moment of the signal
    (record,) = w.step()
    assert record.outcome is Outcome.REJECTED and "spread 80" in record.reasons[0]
    assert w.recorder.signals == []


def test_duplicate_across_restarts_is_not_sent_again() -> None:
    candles = scenario_a_sell()
    store = InMemoryDedupStore()
    first = world(candles, warm=len(candles) - 1, store=store)
    assert first.step()[0].outcome is Outcome.DELIVERED
    restarted = world(candles, warm=len(candles) - 1, store=store)  # app restarted, same db
    (record,) = restarted.step()
    assert record.outcome is Outcome.DUPLICATE and restarted.recorder.signals == []


def test_cooldown_blocks_a_second_signal_in_the_same_direction() -> None:
    candles = scenario_a_sell()
    store = InMemoryDedupStore()
    earlier = run(candles, make_config())[0]
    SignalManager(store, SignalsConfig(cooldown_bars=0)).accept(
        replace(earlier, bar_open_time=earlier.bar_open_time - timedelta(minutes=10))
    )
    w = world(candles, warm=len(candles) - 1, cooldown=5, store=store)
    (record,) = w.step()
    assert record.outcome is Outcome.COOLDOWN and w.recorder.signals == []


def test_missing_candles_suppress_signals_built_on_incomplete_data() -> None:
    candles = scenario_a_sell()
    w = world(candles, warm=len(candles) - 6, retries=0)
    w.provider.hide = {candles[-4].time_open}  # a hole just before the setup
    (record,) = w.step(6)
    assert record.outcome is Outcome.REJECTED and "incomplete" in record.reasons[0]
    assert w.recorder.signals == []


def test_data_hole_clears_pending_scenario_b_setup() -> None:
    candles, confirm = scenario_b_sell()
    breakout = confirm - 3
    w = world(candles, warm=breakout + 1)  # history already contains the breakout
    w.provider.hide = {candles[breakout + 1].time_open}  # pullback candle missing
    assert w.step(3) == []  # not even a rejected record: the setup no longer exists
    assert w.recorder.signals == []


def test_suppression_window_is_configurable() -> None:
    def outcomes(suppress_bars: int) -> list[Outcome]:
        candles = scenario_a_sell()
        w = world(candles, warm=len(candles) - 6, retries=0)
        w.pipeline._cfg = MonitorConfig(post_gap_suppress_bars=suppress_bars)
        w.provider.hide = {
            candles[-5].time_open
        }  # hole; the signal candle is 5th delivered after it
        return [r.outcome for r in w.step(6)]

    assert outcomes(2) == [Outcome.DELIVERED]  # window over before the setup completes
    assert outcomes(5) == [Outcome.REJECTED]  # setup completes inside the window


def test_engine_rejection_requests_resync_once() -> None:
    candles = scenario_a_sell()
    w = world(candles, warm=len(candles) - 1)
    assert not w.pipeline.consume_resync_request()
    w.pipeline.handle(FeedUpdate(FeedStatus.OK, (candles[5],)))  # older than the last candle
    assert w.pipeline.consume_resync_request() is True
    assert w.pipeline.consume_resync_request() is False


def test_live_candles_before_history_request_resync() -> None:
    candles = scenario_a_sell()
    w = world(candles, warm=len(candles) - 1, bootstrap=False)
    assert w.pipeline.handle(FeedUpdate(FeedStatus.OK, (candles[0],))) == []
    assert w.pipeline.consume_resync_request()


def test_new_history_rebuilds_the_engine_instead_of_failing() -> None:
    candles = scenario_a_sell()
    w = world(candles, warm=len(candles) - 1)
    w.feed.reset()
    assert w.pipeline.handle(w.feed.poll()) == []  # same history again: fresh engine, no error
    assert not w.pipeline.consume_resync_request()


def test_non_ok_updates_are_ignored() -> None:
    w = world(scenario_a_sell(), warm=100)
    for status in (FeedStatus.DISCONNECTED, FeedStatus.MARKET_CLOSED, FeedStatus.DATA_GAP):
        assert w.pipeline.handle(FeedUpdate(status)) == []


# ---------------- monitor service ----------------


def make_monitor(w: World, poll: float = 0.01) -> MonitorService:
    return MonitorService(w.feed, w.pipeline, Timeframe.M5, poll, clock=w.clock)


def test_run_once_and_snapshot() -> None:
    candles = scenario_a_sell()
    w = world(candles, warm=len(candles) - 1)
    monitor = make_monitor(w)
    w.provider.reveal(1)
    (record,) = monitor.run_once()
    snap = monitor.snapshot()
    assert record.outcome is Outcome.DELIVERED
    assert snap.feed_status is FeedStatus.OK and snap.symbol == "XAUUSD"
    assert snap.timeframe is Timeframe.M5 and snap.latest_ma is not None
    assert snap.last_candle_time == candles[-1].time_open and snap.last_signal == record
    monitor.run_once()
    assert monitor.snapshot().feed_status is FeedStatus.NO_NEW_DATA  # live state, not stale 'OK'


def test_run_once_survives_unexpected_exceptions() -> None:
    w = world(scenario_a_sell(), warm=100)
    monitor = make_monitor(w)

    def boom() -> FeedUpdate:
        raise RuntimeError("driver exploded")

    w.feed.poll = boom  # type: ignore[method-assign]
    assert monitor.run_once() == []
    snap = monitor.snapshot()
    assert snap.feed_status is FeedStatus.DATA_ERROR and "driver exploded" in snap.message


def test_resync_request_makes_the_feed_rebootstrap() -> None:
    w = world(scenario_a_sell(), warm=100)
    monitor = make_monitor(w)
    w.pipeline._resync_requested = True
    monitor.run_once()
    assert w.feed.poll().is_history


def test_listener_errors_do_not_break_the_monitor() -> None:
    candles = scenario_a_sell()
    w = world(candles, warm=len(candles) - 1)
    monitor = make_monitor(w)
    seen: list[SignalRecord] = []

    def bad(_: object) -> None:
        raise RuntimeError("gui bug")

    monitor.add_signal_listener(bad)
    monitor.add_snapshot_listener(bad)
    monitor.add_signal_listener(seen.append)
    w.provider.reveal(1)
    monitor.run_once()
    assert len(seen) == 1


def test_background_thread_delivers_signal_and_stops_cleanly() -> None:
    candles = scenario_a_sell()
    provider = ReplayProvider(candles, visible=len(candles) - 6)
    feed = DataFeed(
        provider,
        Timeframe.M5,
        PATTERN,
        FeedConfig(min_history_bars=20),
        clock=FakeClock(candles[-1].time_open + timedelta(minutes=10)),
    )
    recorder = Recorder()
    pipeline = SignalPipeline(
        lambda: StrategyEngine(make_config(), "XAUUSD"),
        SignalValidator(ValidationConfig()),
        SignalManager(InMemoryDedupStore(), SignalsConfig(cooldown_bars=0)),
        NotificationDispatcher([recorder]),
        feed.current_spread_points,
        MonitorConfig(),
        clock=FakeClock(candles[-1].time_open + timedelta(minutes=5, seconds=3)),
    )
    monitor = MonitorService(feed, pipeline, Timeframe.M5, 0.01)
    snapshots: list[MonitorSnapshot] = []
    delivered = threading.Event()
    monitor.add_snapshot_listener(snapshots.append)
    monitor.add_signal_listener(lambda r: delivered.set())

    monitor.start()
    assert monitor.is_running
    deadline = time.monotonic() + 5
    while pipeline.latest_ma is None and time.monotonic() < deadline:
        time.sleep(0.01)  # wait for warm-up before revealing live candles
    for _ in range(6):
        provider.reveal(1)
        time.sleep(0.03)
    assert delivered.wait(5), "signal not delivered by the background thread"
    monitor.stop()
    assert not monitor.is_running
    assert len(recorder.signals) == 1
    assert snapshots and snapshots[-1].running is False
