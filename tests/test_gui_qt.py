"""Widget tests on Qt's offscreen platform: real widgets, real AppController, fakes at the edges."""

import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QTableWidgetItem,
)

from test_controller import REPO, Rig  # noqa: E402
from test_notify_telegram import TOKEN, err  # noqa: E402
from xauassist.app.pipeline import Outcome, SignalRecord  # noqa: E402
from xauassist.config.loader import load_config  # noqa: E402
from xauassist.domain.enums import MaMethod  # noqa: E402
from xauassist.gui.main_window import MainWindow  # noqa: E402
from xauassist.gui.settings_dialog import SettingsDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    return app


def cell(window: MainWindow, row: int, col: int) -> QTableWidgetItem:
    item = window.table.item(row, col)
    assert item is not None
    return item


def pump(cond: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Run the Qt event loop until `cond` holds (queued cross-thread signals need it)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    QCoreApplication.processEvents()
    return cond()


@pytest.fixture()
def rig(qapp: QApplication, tmp_path: Path) -> Iterator[Rig]:
    r = Rig(tmp_path)
    yield r
    r.close()


@pytest.fixture()
def window(qapp: QApplication, rig: Rig) -> Iterator[MainWindow]:
    w = MainWindow(
        rig.controller, on_start=rig.session.start, on_stop=rig.session.stop, use_tray=False
    )
    w.show()
    yield w
    w._timer.stop()
    w.deleteLater()


# ---------------- main window ----------------


def test_initial_state(window: MainWindow) -> None:
    assert window.status_pill.text() == "Stopped"
    assert window.start_button.isEnabled() and not window.stop_button.isEnabled()
    assert window.symbol_label.text() == "-" and window.ma_label.text() == "-"
    assert window.timeframe_label.text() == "M5"
    assert window.last_signal_label.text() == "No signal yet."
    assert window.table.rowCount() == 0 and window.table.columnCount() == 8
    assert window.sound_check.isChecked()
    assert window.buy_edit.text().endswith("buy.wav") and window.sell_edit.text().endswith(
        "sell.wav"
    )
    assert "Not configured" not in window.telegram_pill.text()  # rig has credentials, enabled


def test_start_shows_live_status_and_stop_returns_to_idle(window: MainWindow) -> None:
    window.start_button.click()
    assert not window.start_button.isEnabled() and window.stop_button.isEnabled()
    assert pump(lambda: window.ma_label.text().startswith("EMA20: "))
    assert window.status_pill.text().startswith("Connected")
    assert window.symbol_label.text() == "XAUUSD"
    assert window.candle_label.text() != "-"
    window.stop_button.click()
    assert window.status_pill.text() == "Stopped"
    assert window.start_button.isEnabled() and not window.stop_button.isEnabled()


def test_signal_appears_in_history_and_last_signal_panel(window: MainWindow, rig: Rig) -> None:
    window.start_button.click()  # also starts the replay driver via on_start
    assert pump(lambda: window.table.rowCount() == 1), "signal never reached the history table"
    cells = [cell(window, 0, c).text() for c in range(window.table.columnCount())]
    assert cells[1:4] == ["SELL", "A", "Bearish Engulfing"] and cells[6] == "Delivered"
    assert cell(window, 0, 1).foreground().color().name() == "#c62828"  # SELL = red
    assert pump(lambda: window.last_signal_label.text().startswith("SELL  XAUUSD  M5"))
    assert "retested" in window.last_signal_label.text()


def test_history_filter_hides_non_delivered_rows(window: MainWindow, rig: Rig) -> None:
    window.start_button.click()
    assert pump(lambda: window.table.rowCount() == 1)
    delivered = rig.records()[0]
    window._on_record(
        SignalRecord(
            delivered.signal, Outcome.REJECTED, ("spread 80 > max 50",), delivered.processed_at
        )
    )
    assert window.table.rowCount() == 2
    assert window.table.isRowHidden(0) and not window.table.isRowHidden(1)  # newest first, hidden
    window.show_all_check.setChecked(True)
    assert not window.table.isRowHidden(0)
    assert cell(window, 0, 7).text() == "spread 80 > max 50"


def test_sound_controls_apply_and_persist(window: MainWindow, rig: Rig) -> None:
    window.sound_check.setChecked(False)
    assert rig.controller.config.sound.enabled is False
    assert load_config(rig.paths.config_path).sound.enabled is False
    window.buy_edit.setText("custom_buy.wav")
    window.buy_edit.editingFinished.emit()
    assert rig.controller.config.sound.buy_sound == "custom_buy.wav"
    window.buy_test.click()
    assert (
        rig.player.beeps == 1
    )  # file does not exist -> audible fallback, still plays when disabled
    window.sell_test.click()
    assert rig.player.played == [REPO / "assets/sounds/sell.wav"]


def test_telegram_test_button_reports_success_and_failure(window: MainWindow, rig: Rig) -> None:
    window.telegram_button.click()
    assert not window.telegram_button.isEnabled()
    assert pump(lambda: window.telegram_result.text() == "Test message sent.")
    assert window.telegram_button.isEnabled()
    assert rig.transport.requests[-1][1]["text"].startswith("XAUUSD assistant")
    rig.transport.script = [err(401, "Unauthorized")]
    window.telegram_button.click()
    assert pump(lambda: window.telegram_result.text().startswith("Token check failed"))
    assert "401" in window.telegram_result.text()
    assert window.telegram_pill.text().startswith("Error")


def test_telegram_not_configured_message(qapp: QApplication, tmp_path: Path) -> None:
    r = Rig(tmp_path, secrets=False)
    w = MainWindow(r.controller, use_tray=False)
    try:
        assert w.telegram_pill.text().startswith("Not configured")
        w.telegram_button.click()
        assert pump(lambda: "No bot token" in w.telegram_result.text())
        assert r.transport.requests == []
    finally:
        w._timer.stop()
        r.close()


def test_warnings_banner_follows_the_provider(window: MainWindow, rig: Rig) -> None:
    assert window.warning_label.isHidden()
    rig.session.provider.offset_warning = lambda: "server clock is UTC+3, config says UTC+2"  # type: ignore[attr-defined]
    rig.controller.start()
    window.refresh_notifications()
    assert not window.warning_label.isHidden() and "UTC+3" in window.warning_label.text()


def test_closing_without_tray_shuts_everything_down(window: MainWindow, rig: Rig) -> None:
    window.start_button.click()
    assert pump(lambda: window.ma_label.text() != "-")
    window.close()
    assert not rig.controller.is_running


def test_stopped_window_keeps_last_known_values(window: MainWindow) -> None:
    window.start_button.click()
    assert pump(lambda: window.ma_label.text() != "-")
    window.stop_button.click()
    assert window.symbol_label.text() == "XAUUSD"  # last known, not blanked


# ---------------- settings dialog ----------------


@pytest.fixture()
def dialog(qapp: QApplication, rig: Rig) -> SettingsDialog:
    return SettingsDialog(rig.controller)


def test_dialog_has_a_tab_per_section_populated_from_config(dialog: SettingsDialog) -> None:
    titles = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
    assert titles == [
        "Strategy",
        "Validation",
        "Signals",
        "MetaTrader 5",
        "Data feed",
        "Telegram",
        "Sound",
        "Display",
        "Monitor",
        "Backtest",
    ]
    period = dialog.widget_for("strategy.ma.period")
    assert isinstance(period, QSpinBox) and period.value() == 20
    assert isinstance(dialog.widget_for("strategy.ma.method"), QComboBox)
    assert dialog.combo("strategy.ma.method").currentText() == "EMA"
    assert isinstance(dialog.widget_for("strategy.trend.htf_enabled"), QCheckBox)
    wick = dialog.widget_for("strategy.pin_bar.wick_body_ratio")
    assert isinstance(wick, QDoubleSpinBox) and wick.value() == 2.0
    assert isinstance(dialog.widget_for("display.timezone"), QLineEdit)
    assert dialog.widget_for("strategy.ma.period").toolTip()  # help text attached


def test_dialog_apply_changes_config_and_restarts_monitoring(
    dialog: SettingsDialog, rig: Rig
) -> None:
    rig.controller.start()
    monitor = rig.controller._monitor
    dialog.spin("strategy.ma.period").setValue(34)
    dialog.combo("strategy.ma.method").setCurrentText("SMA")
    assert dialog.apply() is True
    cfg = rig.controller.config
    assert cfg.strategy.ma.period == 34 and cfg.strategy.ma.method is MaMethod.SMA
    assert rig.controller._monitor is not monitor and rig.controller.is_running
    assert load_config(rig.paths.config_path) == cfg


def test_dialog_rejects_invalid_values_without_changing_anything(
    dialog: SettingsDialog, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: shown.append(a[2]))
    before = rig.controller.config
    dialog.spin("strategy.ma.period").setValue(1)  # below the minimum of 2
    assert dialog.apply() is False
    assert rig.controller.config == before and not rig.paths.config_path.exists()
    assert shown and "period" in shown[0]
    dialog.accept()  # accept() must not close an invalid dialog
    assert dialog.result() != 1


def test_dialog_rejects_bad_timezone(
    dialog: SettingsDialog, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: shown.append(a[2]))
    dialog.line("display.timezone").setText("Mars/Base")
    assert dialog.apply() is False and "timezone" in shown[0]


def test_dialog_credentials_flow(qapp: QApplication, tmp_path: Path) -> None:
    r = Rig(tmp_path, secrets=False)
    try:
        d = SettingsDialog(r.controller)
        d.token_edit.setText("nonsense")
        d.chat_edit.setText("abc")
        d.save_credentials_button.click()
        assert d.credentials_status.text().startswith("Bot token should look like")
        assert r.store.secrets is None

        d.token_edit.setText(TOKEN)
        d.chat_edit.setText("987654321")
        d.save_credentials_button.click()
        assert d.credentials_status.text() == "Credentials saved."
        assert r.store.secrets is not None and d.token_edit.text() == ""  # token not left on screen
        assert (
            "saved" in d.token_edit.placeholderText()
            and TOKEN not in d.token_edit.placeholderText()
        )

        d.check("telegram.enabled").setChecked(True)
        d.test_button.click()
        assert pump(lambda: d.credentials_status.text().startswith("Connected"))
        assert d.test_button.isEnabled()

        r.transport.script = [err(401, "Unauthorized")]
        d.test_button.click()
        assert pump(lambda: d.credentials_status.text().startswith("Token check failed"))

        d.remove_credentials_button.click()
        assert r.store.secrets is None and d.credentials_status.text() == "Credentials removed."
    finally:
        r.close()
