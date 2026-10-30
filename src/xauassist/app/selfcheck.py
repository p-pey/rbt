"""Self-check: verifies a (packaged) installation can actually do its job.

Run it with `XAUAssistant.exe --selfcheck`: it exercises every bundled resource and the main code
paths without MetaTrader, Telegram or a display, writes `logs/selfcheck.txt`, and exits non-zero if
anything essential is missing. The release build runs it automatically.
"""

from __future__ import annotations

import os
import ssl
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from xauassist.app.paths import AppPaths, is_writable
from xauassist.config.loader import load_config
from xauassist.config.models import AppConfig, SoundConfig, StrategyConfig
from xauassist.config.secrets import FileSecretStore, TelegramSecrets


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    critical: bool = True


def _sound_files(paths: AppPaths) -> str:
    cfg = SoundConfig()
    missing = [p for p in (cfg.buy_sound, cfg.sell_sound) if not (paths.app_dir / p).is_file()]
    if missing:
        raise FileNotFoundError(f"missing bundled sound file(s): {missing}")
    return "buy.wav and sell.wav found"


def _example_config(paths: AppPaths) -> str:
    path = paths.app_dir / "config" / "config.example.toml"
    config = load_config(path)
    if config != AppConfig():
        raise ValueError("bundled config.example.toml does not match the built-in defaults")
    return f"{path.name} loads and matches the defaults"


def _user_dir(paths: AppPaths) -> str:
    if not is_writable(paths.user_dir):
        raise PermissionError(f"cannot write to {paths.user_dir}")
    from xauassist.signals.dedup_store import SqliteDedupStore

    with tempfile.TemporaryDirectory(dir=paths.user_dir) as tmp:
        store = SqliteDedupStore(Path(tmp) / "probe.db")
        store.close()
    return f"{paths.user_dir} is writable (sqlite works)"


def _timezones(_: AppPaths) -> str:
    from xauassist.notify.message import resolve_timezone

    for name in ("UTC", "Europe/Berlin", "Asia/Tehran"):
        resolve_timezone(name)  # needs the tzdata package on Windows
    return "IANA timezones available"


def _secrets(_: AppPaths) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        store = FileSecretStore(Path(tmp) / "secrets.toml")
        store.save(TelegramSecrets("0:selfcheck", "1"))
        loaded = store.load()
        if loaded is None or loaded.chat_id != "1":
            raise ValueError("secret store round trip failed")
    return "secret file store round trip ok"


def _engine(_: AppPaths) -> str:
    from xauassist.backtest.synthetic import generate_candles
    from xauassist.strategy.engine import StrategyEngine

    engine = StrategyEngine(StrategyConfig(), "XAUUSD")
    signals = sum(len(engine.on_candle_closed(c)) for c in generate_candles(1500, seed=1))
    return f"strategy engine processed 1500 candles ({signals} signal(s))"


def _qt(_: AppPaths) -> str:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        QApplication([])
    from xauassist.gui.theme import app_icon

    if app_icon().isNull():
        raise RuntimeError("could not draw the application icon")
    return "Qt loaded; widgets and drawing work"


def _tls(_: AppPaths) -> str:
    ctx = ssl.create_default_context()
    return f"TLS context created ({len(ctx.get_ca_certs())} CA certificate(s) loaded)"


def _keyring(_: AppPaths) -> str:
    try:
        import keyring  # type: ignore[import-not-found,unused-ignore]
    except ImportError:
        return "keyring not installed: credentials will use secrets.toml"
    return f"keyring backend: {type(keyring.get_keyring()).__name__}"


def _metatrader(_: AppPaths) -> str:
    try:
        import MetaTrader5  # type: ignore[import-not-found,unused-ignore]  # noqa: F401
    except ImportError:
        return (
            "MetaTrader5 package not installed (expected on non-Windows; replay mode still works)"
        )
    return "MetaTrader5 package importable (the terminal itself is not checked here)"


def _audio(_: AppPaths) -> str:
    from xauassist.notify.sound import create_default_player

    return f"audio backend: {type(create_default_player()).__name__}"


_CHECKS: list[tuple[str, bool, Callable[[AppPaths], str]]] = [
    ("bundled sounds", True, _sound_files),
    ("bundled example config", True, _example_config),
    ("writable user folder + sqlite", True, _user_dir),
    ("timezone database", True, _timezones),
    ("secret store", True, _secrets),
    ("strategy engine", True, _engine),
    ("Qt / GUI libraries", True, _qt),
    ("TLS (Telegram)", False, _tls),
    ("keyring", False, _keyring),
    ("MetaTrader5 package", False, _metatrader),
    ("audio backend", False, _audio),
]


def run_selfcheck(paths: AppPaths) -> list[CheckResult]:
    results: list[CheckResult] = []
    for name, critical, check in _CHECKS:
        try:
            results.append(CheckResult(name, True, check(paths), critical))
        except Exception as exc:  # report every failure instead of stopping at the first
            results.append(CheckResult(name, False, f"{type(exc).__name__}: {exc}", critical))
    return results


def format_results(results: list[CheckResult]) -> str:
    lines = []
    for r in results:
        mark = "OK  " if r.ok else ("FAIL" if r.critical else "WARN")
        lines.append(f"[{mark}] {r.name}: {r.detail}")
    failed = [r for r in results if not r.ok and r.critical]
    lines.append("")
    lines.append(
        "RESULT: " + ("PASS" if not failed else f"FAIL ({len(failed)} essential check(s))")
    )
    return "\n".join(lines)


def passed(results: list[CheckResult]) -> bool:
    return all(r.ok for r in results if r.critical)
