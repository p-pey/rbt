import pytest

from fakes import FakeClock, FakeMt5Api, mt5_rows, utc
from helpers import scenario_a_sell
from xauassist.config.models import Mt5Config
from xauassist.data.errors import ConnectionFailedError, DataUnavailableError, SymbolNotFoundError
from xauassist.data.mt5_provider import Mt5Provider, detect_server_offset_seconds
from xauassist.data.provider import ConnectionStatus
from xauassist.domain.enums import Timeframe

NOW = utc(2026, 1, 6, 12, 0, 30)


def make(api: FakeMt5Api | None, **cfg: object) -> Mt5Provider:
    return Mt5Provider(Mt5Config(**cfg), api, clock=FakeClock(NOW))  # type: ignore[arg-type]


# ---- connection ----


def test_missing_package_reports_terminal_missing_without_crashing() -> None:
    info = make(None).connect()
    assert info.status is ConnectionStatus.TERMINAL_MISSING
    assert "Windows" in info.message
    assert not make(None).is_connected()


def test_initialize_failure_is_reported_with_mt5_error() -> None:
    api = FakeMt5Api()
    api.init_ok = False
    info = make(api).connect()
    assert info.status is ConnectionStatus.FAILED
    assert "-10003" in info.message and "running" in info.message


def test_terminal_not_connected_to_broker() -> None:
    api = FakeMt5Api()
    api.connected = False
    assert make(api).connect().status is ConnectionStatus.DISCONNECTED


def test_successful_connect_and_terminal_path_is_forwarded() -> None:
    api = FakeMt5Api()
    p = make(api, terminal_path=r"C:\MT5\terminal64.exe")
    assert p.connect().status is ConnectionStatus.CONNECTED
    assert api.initialize_args == (r"C:\MT5\terminal64.exe",)
    assert p.is_connected()
    p.disconnect()
    assert api.shutdown_called


def test_connection_loss_is_visible_through_is_connected() -> None:
    api = FakeMt5Api()
    p = make(api)
    p.connect()
    api.connected = False
    assert not p.is_connected()


# ---- symbols ----


def test_resolve_prefers_plain_name_and_selects_it() -> None:
    api = FakeMt5Api()
    assert make(api).resolve_symbol(r"XAUUSD[\.\w]*") == "XAUUSD"
    assert ("select", "XAUUSD", True) in api.calls


def test_resolve_broker_suffix() -> None:
    api = FakeMt5Api()
    api.symbol_names = ["EURUSD", "XAUUSD.a", "XAUUSD.a_micro"]
    assert make(api).resolve_symbol(r"XAUUSD[\.\w]*") == "XAUUSD.a"


def test_resolve_none_when_absent_or_not_selectable() -> None:
    api = FakeMt5Api()
    api.symbol_names = ["EURUSD"]
    assert make(api).resolve_symbol(r"XAUUSD[\.\w]*") is None
    api.symbol_names = ["XAUUSD"]
    api.selectable = False
    assert make(api).resolve_symbol(r"XAUUSD[\.\w]*") is None


# ---- candles ----


def test_closed_candles_skip_forming_bar_and_convert_server_time() -> None:
    api = FakeMt5Api()
    candles = scenario_a_sell()[-5:]
    api.rates = mt5_rows(candles, server_offset_s=7200)  # broker is UTC+2
    p = make(api, server_utc_offset_hours=2.0)
    got = p.get_closed_candles("XAUUSD", Timeframe.M5, 5)
    assert ("rates", "XAUUSD", 5, 1, 5) in api.calls  # start_pos=1 skips the forming candle
    assert [c.time_open for c in got] == [c.time_open for c in candles]
    assert got[0].close == candles[0].close and got[0].spread_points == 20.0
    assert all(c.is_closed for c in got)


def test_unsorted_and_duplicate_rows_are_normalised() -> None:
    api = FakeMt5Api()
    rows = mt5_rows(scenario_a_sell()[-3:])
    api.rates = [rows[2], rows[0], rows[1], rows[1]]
    got = make(api).get_closed_candles("XAUUSD", Timeframe.M5, 9)
    assert [c.time_open for c in got] == sorted({c.time_open for c in got}) and len(got) == 3


@pytest.mark.parametrize("empty", [None, []])
def test_empty_history_raises_data_unavailable(empty: object) -> None:
    api = FakeMt5Api()
    api.rates = empty
    with pytest.raises(DataUnavailableError):
        make(api).get_closed_candles("XAUUSD", Timeframe.M5, 5)


def test_invalid_symbol_error_code_maps_to_symbol_not_found() -> None:
    api = FakeMt5Api()
    api.rates = None
    api.error = (-2, "Invalid params")
    with pytest.raises(SymbolNotFoundError):
        make(api).get_closed_candles("NOPE", Timeframe.M5, 5)


def test_ipc_exception_becomes_connection_failed() -> None:
    api = FakeMt5Api()
    api.raise_on_rates = True
    with pytest.raises(ConnectionFailedError):
        make(api).get_closed_candles("XAUUSD", Timeframe.M5, 5)


def test_no_api_raises_connection_failed() -> None:
    with pytest.raises(ConnectionFailedError):
        make(None).get_closed_candles("XAUUSD", Timeframe.M5, 5)


def test_unknown_timeframe_constant() -> None:
    with pytest.raises(ValueError):
        make(FakeMt5Api()).get_closed_candles("XAUUSD", Timeframe.H4, 5)


# ---- spread, tick age, server offset ----


def test_spread_points() -> None:
    api = FakeMt5Api()
    api.spread = 31
    assert make(api).get_spread_points("XAUUSD") == 31.0


def test_spread_falls_back_to_ask_minus_bid() -> None:
    api = FakeMt5Api()
    api.spread = 0
    api.tick_time = int(NOW.timestamp())
    api.tick_spread_points = 30.0
    assert make(api).get_spread_points("XAUUSD") == pytest.approx(30.0)


def test_tick_age_uses_server_offset() -> None:
    api = FakeMt5Api()
    api.tick_time = int(NOW.timestamp()) + 7200 - 45  # 45 s old, broker UTC+2
    p = make(api, server_utc_offset_hours=2.0)
    assert p.last_tick_age_seconds("XAUUSD") == pytest.approx(45.0, abs=1.0)


def test_tick_age_unknown_without_tick() -> None:
    assert make(FakeMt5Api()).last_tick_age_seconds("XAUUSD") is None
    assert make(None).last_tick_age_seconds("XAUUSD") is None


@pytest.mark.parametrize(
    ("offset_h", "age_s", "expected"),
    [(2, 5, 7200), (3, 90, 10800), (0, 10, 0), (-5, 30, -18000), (2, 600, None), (2, 300, None)],
)
def test_server_offset_detection(offset_h: int, age_s: int, expected: int | None) -> None:
    now = 1_767_700_000.0
    assert detect_server_offset_seconds(now + offset_h * 3600 - age_s, now) == expected


def test_candle_times_always_use_the_configured_offset() -> None:
    """The offset must never change under our feet: a moving timeline would stall the feed."""
    api = FakeMt5Api()
    api.rates = mt5_rows(scenario_a_sell()[-2:], server_offset_s=7200)
    api.tick_time = int(NOW.timestamp()) + 10800 - 5  # ticks say UTC+3, config says UTC+2
    p = make(api, server_utc_offset_hours=2.0)
    for _ in range(3):
        got = p.get_closed_candles("XAUUSD", Timeframe.M5, 2)
        assert got[0].time_open == scenario_a_sell()[-2].time_open
    assert p.server_offset_seconds == 7200


def tick_clock_provider(api: FakeMt5Api) -> tuple[Mt5Provider, FakeClock]:
    clock = FakeClock(NOW)
    api.rates = mt5_rows(scenario_a_sell()[-2:], server_offset_s=7200)
    return Mt5Provider(Mt5Config(server_utc_offset_hours=2.0), api, clock=clock), clock


def test_confirmed_offset_mismatch_produces_warning() -> None:
    api = FakeMt5Api()
    p, clock = tick_clock_provider(api)
    for _ in range(3):  # live ticks, 20 s apart, all implying UTC+3
        api.tick_time = int(clock.now.timestamp()) + 10800 - 2
        p.get_closed_candles("XAUUSD", Timeframe.M5, 2)
        clock.advance(20)
    warning = p.offset_warning()
    assert warning is not None and "UTC+3h" in warning and "UTC+2h" in warning


def test_single_sample_is_not_enough_for_a_warning() -> None:
    api = FakeMt5Api()
    p, clock = tick_clock_provider(api)
    api.tick_time = int(clock.now.timestamp()) + 10800 - 2
    p.get_closed_candles("XAUUSD", Timeframe.M5, 2)
    assert p.offset_warning() is None


def test_stale_tick_repeated_never_warns_even_when_it_looks_fresh() -> None:
    """Weekend: the same old tick, whose age happens to be ~1h, would imply a bogus offset."""
    api = FakeMt5Api()
    p, clock = tick_clock_provider(api)
    api.tick_time = int(clock.now.timestamp()) + 7200 - 3600 + 3  # 'offset' 3600 with age 3597 s
    for _ in range(10):
        p.get_closed_candles("XAUUSD", Timeframe.M5, 2)
        clock.advance(40)
    assert p.offset_warning() is None


def test_matching_offset_clears_a_warning() -> None:
    api = FakeMt5Api()
    p, clock = tick_clock_provider(api)
    for _ in range(3):
        api.tick_time = int(clock.now.timestamp()) + 10800 - 2
        p.get_closed_candles("XAUUSD", Timeframe.M5, 2)
        clock.advance(20)
    assert p.offset_warning() is not None
    api.tick_time = int(clock.now.timestamp()) + 7200 - 2  # the user fixed MT5's clock / DST ended
    p.get_closed_candles("XAUUSD", Timeframe.M5, 2)
    assert p.offset_warning() is None
