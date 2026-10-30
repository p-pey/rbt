"""Application entry point: `python -m xauassist` (or the packaged .exe)."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from xauassist.app.crash import install_crash_handlers
from xauassist.app.first_run import ensure_first_run
from xauassist.app.logging_setup import setup_logging
from xauassist.app.paths import AppPaths, default_paths
from xauassist.app.selfcheck import format_results, passed, run_selfcheck
from xauassist.config.loader import load_config
from xauassist.config.models import AppConfig, ConfigError
from xauassist.config.secrets import default_secret_store
from xauassist.data.provider import MarketDataProvider

log = logging.getLogger(__name__)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="xauassist", description="XAUUSD MA20 signal assistant (alerts only; never trades)."
    )
    parser.add_argument("--config", type=Path, help="path to config.toml")
    parser.add_argument("--data-dir", type=Path, help="folder for config, logs and the signal db")
    parser.add_argument("--autostart", action="store_true", help="start monitoring immediately")
    parser.add_argument("--no-tray", action="store_true", help="do not minimise to the tray")
    parser.add_argument(
        "--selfcheck", action="store_true", help="verify the installation and exit (0 = ok)"
    )
    replay = parser.add_argument_group("replay mode (no MetaTrader needed)")
    replay.add_argument("--replay", type=Path, help="CSV of candles to replay as if live")
    replay.add_argument("--replay-interval", type=float, default=1.0, help="seconds per candle")
    replay.add_argument("--replay-start", type=int, default=300, help="candles shown as history")
    return parser.parse_args(argv)


def load_app_config(path: Path) -> AppConfig:
    return load_config(path) if path.exists() else AppConfig()


def run_selfcheck_command(paths: AppPaths) -> int:
    results = run_selfcheck(paths)
    report = format_results(results)
    report_file = paths.log_dir / "selfcheck.txt"
    report_file.write_text(report + "\n", encoding="utf-8")
    if sys.stdout is not None:  # no console in a windowed .exe: the file is the output
        print(report)
    return 0 if passed(results) else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    defaults = default_paths()
    paths = AppPaths(defaults.app_dir, args.data_dir or defaults.user_dir)
    config_path = args.config or paths.config_path
    created = ensure_first_run(paths)  # folders + an editable config.toml on the first launch
    log_file = setup_logging(paths.log_dir)
    log.info("Starting XAUUSD assistant; log file %s", log_file)
    log.info("App folder %s, user folder %s", paths.app_dir, paths.user_dir)
    if args.selfcheck:
        return run_selfcheck_command(paths)

    # Qt is imported here so `--help` and the pure modules work without a display or PySide6.
    from PySide6.QtCore import QLockFile
    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication(sys.argv[:1])
    app.setApplicationName("XAUUSD Assistant")

    def show_crash(text: str) -> None:
        QMessageBox.critical(None, "Unexpected error", text)

    install_crash_handlers(log_file, show_crash)
    if created:
        log.info("First launch: %s", "; ".join(created))

    lock = QLockFile(str(paths.user_dir / "xauassist.lock"))
    lock.setStaleLockTime(30_000)
    if not lock.tryLock(100):  # two instances would send every alert twice
        QMessageBox.warning(None, "Already running", "The assistant is already running.")
        return 1

    try:
        config = load_app_config(config_path)
    except ConfigError as exc:
        log.error("Invalid configuration: %s", exc)
        QMessageBox.critical(None, "Invalid configuration", f"{config_path}\n\n{exc}")
        return 2

    from xauassist.app.controller import AppController
    from xauassist.app.replay_mode import ReplaySession
    from xauassist.data.mt5_provider import Mt5Provider
    from xauassist.gui.main_window import MainWindow

    session: ReplaySession | None = None
    if args.replay:
        session = ReplaySession.from_csv(
            args.replay,
            config.strategy.timeframe,
            start_visible=args.replay_start,
            interval_seconds=args.replay_interval,
        )
        replay = session

        def provider_factory(cfg: AppConfig) -> MarketDataProvider:
            return replay.provider

        clock = session.clock
    else:

        def provider_factory(cfg: AppConfig) -> MarketDataProvider:
            return Mt5Provider.from_installed(cfg.mt5)

        def clock() -> datetime:
            return datetime.now(UTC)

    controller = AppController(
        paths,
        config,
        provider_factory=provider_factory,
        secret_store=default_secret_store(paths.secrets_path),
        clock=clock,
    )
    window = MainWindow(
        controller,
        on_start=session.start if session else None,
        on_stop=session.stop if session else None,
        use_tray=not args.no_tray,
    )
    window.show()
    if args.autostart:
        window.start_monitoring()
    code = app.exec()
    lock.unlock()
    return code


if __name__ == "__main__":
    sys.exit(main())
