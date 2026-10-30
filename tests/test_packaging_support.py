"""Tests for the code that only matters once the app is packaged as a Windows .exe."""

import logging
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

from xauassist.app.crash import handle_exception, install_crash_handlers
from xauassist.app.first_run import ensure_first_run
from xauassist.app.logging_setup import setup_logging
from xauassist.app.main import main, parse_args
from xauassist.app.paths import (
    APP_FOLDER_NAME,
    AppPaths,
    default_paths,
    fallback_user_dir,
    is_writable,
)
from xauassist.app.selfcheck import CheckResult, format_results, passed, run_selfcheck
from xauassist.config.loader import load_config
from xauassist.config.models import AppConfig

REPO = Path(__file__).resolve().parents[1]


# ---------------- paths ----------------


def test_development_paths_are_the_repo_root() -> None:
    paths = default_paths(frozen=False)
    assert paths.app_dir == paths.user_dir == REPO


def test_frozen_portable_install_keeps_data_next_to_the_exe(tmp_path: Path) -> None:
    exe = tmp_path / "XAUAssistant" / "XAUAssistant.exe"
    exe.parent.mkdir()
    exe.write_text("")
    bundle = tmp_path / "XAUAssistant" / "_internal"
    paths = default_paths(frozen=True, executable=str(exe), bundle_dir=str(bundle), env={})
    assert paths.user_dir == exe.parent.resolve() and paths.app_dir == bundle
    assert paths.config_path == exe.parent.resolve() / "config.toml"


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a non-root POSIX user")
def test_frozen_read_only_program_folder_falls_back_to_appdata(tmp_path: Path) -> None:
    program_files = tmp_path / "Program Files" / "XAUAssistant"
    program_files.mkdir(parents=True)
    exe = program_files / "XAUAssistant.exe"
    exe.write_text("")
    program_files.chmod(stat.S_IRUSR | stat.S_IXUSR)  # read-only, like Program Files
    try:
        appdata = tmp_path / "AppData" / "Roaming"
        paths = default_paths(frozen=True, executable=str(exe), env={"APPDATA": str(appdata)})
        assert paths.user_dir == appdata / APP_FOLDER_NAME
        assert is_writable(paths.user_dir)
    finally:
        program_files.chmod(stat.S_IRWXU)


def test_fallback_without_appdata_uses_the_home_folder() -> None:
    assert fallback_user_dir({}) == Path.home() / ".xauassistant"
    assert fallback_user_dir({"APPDATA": "/x"}) == Path("/x") / APP_FOLDER_NAME


def test_one_file_bundle_dir_comes_from_meipass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "XAUAssistant.exe"
    exe.write_text("")
    temp = tmp_path / "_MEI12345"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(temp), raising=False)
    paths = default_paths(executable=str(exe))
    assert paths.app_dir == temp and paths.user_dir == tmp_path.resolve()


# ---------------- first run ----------------


def test_first_run_creates_folders_and_a_documented_config(tmp_path: Path) -> None:
    paths = AppPaths(app_dir=REPO, user_dir=tmp_path / "user")
    created = ensure_first_run(paths)
    assert paths.config_path.is_file() and paths.log_dir.is_dir() and paths.db_path.parent.is_dir()
    assert (tmp_path / "user" / "secrets.example.toml").is_file()
    assert len(created) >= 4
    text = paths.config_path.read_text()
    assert "PLACEHOLDER" in text  # copied from the documented example, comments included
    assert load_config(paths.config_path) == AppConfig()
    assert not paths.secrets_path.exists()  # never creates credentials


def test_first_run_never_overwrites_user_files(tmp_path: Path) -> None:
    paths = AppPaths(app_dir=REPO, user_dir=tmp_path)
    ensure_first_run(paths)
    paths.config_path.write_text("[strategy.ma]\nperiod = 34\n")
    assert ensure_first_run(paths) == []
    assert load_config(paths.config_path).strategy.ma.period == 34


def test_first_run_without_bundled_example_writes_defaults(tmp_path: Path) -> None:
    empty_app = tmp_path / "app"
    empty_app.mkdir()
    paths = AppPaths(app_dir=empty_app, user_dir=tmp_path / "user")
    ensure_first_run(paths)
    assert load_config(paths.config_path) == AppConfig()


# ---------------- crash handling ----------------


def test_unhandled_exception_is_logged_and_shown(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    shown: list[str] = []
    try:
        raise RuntimeError("kaboom")
    except RuntimeError:
        etype, exc, tb = sys.exc_info()
    assert etype is not None and exc is not None
    handle_exception(etype, exc, tb, tmp_path / "x.log", shown.append)
    assert "Unhandled exception" in caplog.text and "kaboom" in caplog.text
    assert len(shown) == 1 and "kaboom" in shown[0] and "x.log" in shown[0]


def test_keyboard_interrupt_is_not_reported() -> None:
    shown: list[str] = []
    handle_exception(KeyboardInterrupt, KeyboardInterrupt(), None, Path("x"), shown.append)
    assert shown == []


def test_a_failing_crash_dialog_does_not_raise() -> None:
    def broken(_: str) -> None:
        raise RuntimeError("no display")

    handle_exception(ValueError, ValueError("x"), None, Path("x.log"), broken)


def test_handlers_catch_worker_thread_exceptions(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    install_crash_handlers(tmp_path / "x.log", None)

    def boom() -> None:
        raise RuntimeError("worker died")

    t = threading.Thread(target=boom)
    t.start()
    t.join()
    assert "worker died" in caplog.text


# ---------------- logging without a console ----------------


def test_logging_works_when_stderr_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A windowed PyInstaller exe has sys.stderr = None; logging must not break."""
    monkeypatch.setattr(sys, "stderr", None)
    log_file = setup_logging(tmp_path / "logs", console=True)
    logging.getLogger("xauassist.test").error("still logging")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "still logging" in log_file.read_text()


# ---------------- self-check ----------------


def test_selfcheck_passes_in_a_fresh_install(tmp_path: Path) -> None:
    results = run_selfcheck(AppPaths(app_dir=REPO, user_dir=tmp_path))
    assert passed(results), format_results(results)
    names = {r.name for r in results}
    assert {"bundled sounds", "strategy engine", "Qt / GUI libraries", "timezone database"} <= names
    assert "RESULT: PASS" in format_results(results)


def test_selfcheck_detects_missing_bundled_resources(tmp_path: Path) -> None:
    broken_app = tmp_path / "app"
    broken_app.mkdir()  # no assets/, no config/
    results = run_selfcheck(AppPaths(app_dir=broken_app, user_dir=tmp_path / "user"))
    by_name = {r.name: r for r in results}
    assert not by_name["bundled sounds"].ok and "buy.wav" in by_name["bundled sounds"].detail
    assert not by_name["bundled example config"].ok
    assert not passed(results) and "FAIL (2 essential check(s))" in format_results(results)


def test_non_critical_failures_do_not_fail_the_check() -> None:
    results = [CheckResult("a", True, "fine"), CheckResult("b", False, "meh", critical=False)]
    assert passed(results)
    assert "[WARN] b" in format_results(results) and "RESULT: PASS" in format_results(results)


def test_selfcheck_command_line_writes_report_and_exit_code(tmp_path: Path) -> None:
    code = main(["--selfcheck", "--data-dir", str(tmp_path)])
    assert code == 0
    report = (tmp_path / "logs" / "selfcheck.txt").read_text()
    assert "RESULT: PASS" in report
    assert (tmp_path / "config.toml").is_file()  # first run happened on the way


def test_selfcheck_flag_is_parsed() -> None:
    assert parse_args(["--selfcheck"]).selfcheck is True and parse_args([]).selfcheck is False
