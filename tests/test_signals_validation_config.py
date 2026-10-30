from datetime import timedelta
from pathlib import Path

import pytest

from helpers import run, scenario_a_sell
from xauassist.config.loader import build_config, load_config
from xauassist.config.models import (
    AppConfig,
    ConfigError,
    SignalsConfig,
    StrategyConfig,
    ValidationConfig,
)
from xauassist.domain.enums import Direction, MaMethod, Timeframe
from xauassist.domain.signal import Signal
from xauassist.signals.dedup_store import InMemoryDedupStore, SqliteDedupStore
from xauassist.signals.manager import Decision, SignalManager
from xauassist.validation.validator import SignalValidator

REPO = Path(__file__).resolve().parents[1]


def one_signal() -> Signal:
    from helpers import make_config

    signals = run(scenario_a_sell(), make_config())
    assert len(signals) == 1
    return signals[0]


# ---- duplicate prevention / cooldown ----


def test_duplicate_signal_is_rejected() -> None:
    mgr = SignalManager(InMemoryDedupStore(), SignalsConfig(cooldown_bars=0))
    sig = one_signal()
    assert mgr.accept(sig) is Decision.ACCEPTED
    assert mgr.accept(sig) is Decision.DUPLICATE


def test_cooldown_blocks_same_direction_but_not_after_it_elapses() -> None:
    from dataclasses import replace

    mgr = SignalManager(InMemoryDedupStore(), SignalsConfig(cooldown_bars=3))
    sig = one_signal()
    assert mgr.accept(sig) is Decision.ACCEPTED
    soon = replace(sig, bar_open_time=sig.bar_open_time + timedelta(minutes=10))
    later = replace(sig, bar_open_time=sig.bar_open_time + timedelta(minutes=15))
    assert mgr.accept(soon) is Decision.COOLDOWN
    assert mgr.accept(later) is Decision.ACCEPTED


def test_sqlite_store_survives_restart(tmp_path: Path) -> None:
    db = tmp_path / "signals.db"
    sig = one_signal()
    store = SqliteDedupStore(db)
    assert SignalManager(store, SignalsConfig(cooldown_bars=0)).accept(sig) is Decision.ACCEPTED
    store.close()
    reopened = SqliteDedupStore(db)
    assert SignalManager(reopened, SignalsConfig(cooldown_bars=0)).accept(sig) is Decision.DUPLICATE
    assert reopened.last_bar_time(sig.symbol, Direction.SELL) == sig.bar_open_time
    reopened.close()


# ---- validator ----


@pytest.mark.parametrize("symbol", ["XAUUSD", "XAUUSD.a", "XAUUSDm", "XAUUSD_i"])
def test_symbol_suffixes_accepted(symbol: str) -> None:
    from dataclasses import replace

    result = SignalValidator(ValidationConfig()).validate(replace(one_signal(), symbol=symbol))
    assert result.ok, result.reasons


@pytest.mark.parametrize("symbol", ["EURUSD", "XAGUSD", "GOLDXAUUSD"])
def test_other_symbols_rejected(symbol: str) -> None:
    from dataclasses import replace

    assert not SignalValidator(ValidationConfig()).validate(replace(one_signal(), symbol=symbol)).ok


def test_spread_and_timeframe_and_incomplete_data_gates() -> None:
    from dataclasses import replace

    v = SignalValidator(ValidationConfig(max_spread_points=30.0))
    sig = one_signal()  # candle spread 20
    assert v.validate(sig).ok
    assert not v.validate(sig, current_spread_points=45.0).ok
    assert not v.validate(sig, data_complete=False).ok
    assert not v.validate(replace(sig, timeframe=Timeframe.M15)).ok


# ---- config ----


def test_example_config_loads_and_matches_defaults() -> None:
    cfg = load_config(REPO / "config" / "config.example.toml")
    assert cfg == AppConfig()
    assert cfg.strategy.ma.method is MaMethod.EMA


def test_config_overrides_and_enum_case(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text('[strategy.ma]\nperiod = 50\nmethod = "sma"\n[strategy.zone]\nvalue = 1\n')
    cfg = load_config(p)
    assert cfg.strategy.ma.period == 50 and cfg.strategy.ma.method is MaMethod.SMA
    assert cfg.strategy.zone.value == 1.0


@pytest.mark.parametrize(
    "toml",
    [
        "[strategy.ma]\nperiod = 1\n",  # out of range
        "[strategy.ma]\nperiod = 20.5\n",  # wrong type
        "[strategy.ma]\nmethod = 'WMA'\n",  # bad enum
        "[strategy.mystery]\nx = 1\n",  # unknown table
        "[strategy.ma]\nperiod = true\n",  # bool is not int
        "[strategy.trend]\nhtf_enabled = true\nhtf_timeframe = 'M5'\n",  # HTF == base tf
    ],
)
def test_invalid_config_is_rejected(tmp_path: Path, toml: str) -> None:
    p = tmp_path / "c.toml"
    p.write_text(toml)
    with pytest.raises(ConfigError):
        load_config(p)


def test_missing_config_file() -> None:
    with pytest.raises(ConfigError):
        load_config(Path("/nonexistent/config.toml"))


def test_build_config_defaults() -> None:
    assert build_config(StrategyConfig, {}) == StrategyConfig()
