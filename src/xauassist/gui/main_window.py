"""Main window: status, last signal, history, notification settings. Rendering only - all state
comes from AppController and all text/level decisions from view_models."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QBrush, QCloseEvent, QColor, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from xauassist.app.controller import AppController
from xauassist.app.monitor_service import MonitorSnapshot
from xauassist.app.pipeline import SignalRecord
from xauassist.config.models import ConfigError, SoundConfig
from xauassist.domain.enums import Direction
from xauassist.gui.qt_bridge import BackgroundRunner, MonitorBridge
from xauassist.gui.settings_dialog import SettingsDialog
from xauassist.gui.theme import BUY_COLOR, LEVEL_COLORS, SELL_COLOR, app_icon, pill_style
from xauassist.gui.view_models import (
    HISTORY_HEADERS,
    Level,
    StatusView,
    connection_view,
    format_ma,
    format_time,
    history_row,
    last_signal_text,
    telegram_view,
)
from xauassist.notify.telegram import SendOutcome, TelegramStatus

TITLE = "XAUUSD Smart Trading Assistant - alerts only, no trading"


class StatusPill(QLabel):
    def show_view(self, view: StatusView) -> None:
        self.setText(view.text)
        self.setStyleSheet(pill_style(view.level))
        self.setToolTip(view.detail)


class MainWindow(QMainWindow):
    def __init__(
        self,
        controller: AppController,
        *,
        on_start: Callable[[], None] | None = None,
        on_stop: Callable[[], None] | None = None,
        use_tray: bool = True,
    ) -> None:
        super().__init__()
        self._c = controller
        self._on_start = on_start
        self._on_stop = on_stop
        self._quitting = False
        self._tray_hint_shown = False
        self._runner = BackgroundRunner(self)

        self.setWindowTitle(TITLE)
        self.setWindowIcon(app_icon())
        self.resize(980, 720)
        self._build_ui()

        self._bridge = MonitorBridge(controller, self)
        self._bridge.snapshot_changed.connect(self._on_snapshot)
        self._bridge.record_added.connect(self._on_record)

        self._tray = self._build_tray() if use_tray else None
        if self._tray is not None:
            QApplication.setQuitOnLastWindowClosed(False)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh_notifications)
        self._timer.start(1000)

        self._reload_history()
        self._on_snapshot(controller.snapshot())
        self.refresh_notifications()

    # ------------------------------------------------------------------ layout

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        self.setCentralWidget(root)

        top = QHBoxLayout()
        self.start_button = QPushButton("Start monitoring")
        self.stop_button = QPushButton("Stop")
        self.status_pill = StatusPill()
        self.settings_button = QPushButton("Settings...")
        self.logs_button = QPushButton("Open log folder")
        top.addWidget(self.start_button)
        top.addWidget(self.stop_button)
        top.addWidget(self.status_pill)
        top.addStretch(1)
        top.addWidget(self.settings_button)
        top.addWidget(self.logs_button)
        layout.addLayout(top)

        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet(pill_style(Level.WARN))
        self.warning_label.hide()
        layout.addWidget(self.warning_label)

        market = QGroupBox("Market")
        grid = QGridLayout(market)
        self.symbol_label = QLabel("-")
        self.timeframe_label = QLabel("-")
        self.ma_label = QLabel("-")
        self.candle_label = QLabel("-")
        self.detail_label = QLabel("")
        self.detail_label.setWordWrap(True)
        for row, (name, widget) in enumerate(
            [
                ("Symbol", self.symbol_label),
                ("Timeframe", self.timeframe_label),
                ("Moving average", self.ma_label),
                ("Last closed candle (opened)", self.candle_label),
            ]
        ):
            grid.addWidget(QLabel(name + ":"), row, 0)
            grid.addWidget(widget, row, 1)
        grid.addWidget(self.detail_label, 4, 0, 1, 2)
        layout.addWidget(market)

        last = QGroupBox("Last signal")
        last_layout = QVBoxLayout(last)
        self.last_signal_label = QLabel("No signal yet.")
        self.last_signal_label.setWordWrap(True)
        self.last_signal_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        last_layout.addWidget(self.last_signal_label)
        layout.addWidget(last)

        layout.addWidget(self._build_notifications_group())

        history = QGroupBox("Signal history")
        history_layout = QVBoxLayout(history)
        self.show_all_check = QCheckBox("Also show rejected / dropped / duplicate signals")
        history_layout.addWidget(self.show_all_check)
        self.table = QTableWidget(0, len(HISTORY_HEADERS))
        self.table.setHorizontalHeaderLabels(list(HISTORY_HEADERS))
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        history_layout.addWidget(self.table)
        layout.addWidget(history, 1)

        self.start_button.clicked.connect(self.start_monitoring)
        self.stop_button.clicked.connect(self.stop_monitoring)
        self.settings_button.clicked.connect(self.open_settings)
        self.logs_button.clicked.connect(self.open_logs)
        self.show_all_check.toggled.connect(lambda _: self._apply_history_filter())

    def _build_notifications_group(self) -> QGroupBox:
        box = QGroupBox("Notifications")
        form = QFormLayout(box)

        tg_row = QHBoxLayout()
        self.telegram_pill = StatusPill()
        self.telegram_button = QPushButton("Test Telegram")
        self.telegram_result = QLabel("")
        tg_row.addWidget(self.telegram_pill)
        tg_row.addWidget(self.telegram_button)
        tg_row.addWidget(self.telegram_result, 1)
        form.addRow("Telegram:", tg_row)

        sound = self._c.config.sound
        self.sound_check = QCheckBox("Play a sound for every signal")
        self.sound_check.setChecked(sound.enabled)
        form.addRow("Sound:", self.sound_check)
        self.buy_edit, self.buy_test = self._sound_row(form, "BUY sound (WAV):", sound.buy_sound)
        self.sell_edit, self.sell_test = self._sound_row(
            form, "SELL sound (WAV):", sound.sell_sound
        )

        self.telegram_button.clicked.connect(self.test_telegram)
        self.sound_check.toggled.connect(lambda _: self._sound_changed())
        self.buy_test.clicked.connect(lambda: self._c.test_sound(Direction.BUY))
        self.sell_test.clicked.connect(lambda: self._c.test_sound(Direction.SELL))
        return box

    def _sound_row(
        self, form: QFormLayout, label: str, value: str
    ) -> tuple[QLineEdit, QPushButton]:
        row = QHBoxLayout()
        edit = QLineEdit(value)
        browse = QPushButton("Browse...")
        test = QPushButton("Test")
        row.addWidget(edit, 1)
        row.addWidget(browse)
        row.addWidget(test)
        form.addRow(label, row)
        edit.editingFinished.connect(self._sound_changed)
        browse.clicked.connect(lambda: self._browse_sound(edit))
        return edit, test

    # ------------------------------------------------------------------ actions

    def start_monitoring(self) -> None:
        if self._on_start is not None:
            self._on_start()
        self._c.start()
        self._on_snapshot(self._c.snapshot())

    def stop_monitoring(self) -> None:
        self._c.stop()
        if self._on_stop is not None:
            self._on_stop()
        self._on_snapshot(self._c.snapshot())

    def open_settings(self) -> None:
        dialog = SettingsDialog(self._c, self)
        if dialog.exec():
            self._sync_sound_widgets()
            self._reload_history()
            self._on_snapshot(self._c.snapshot())

    def open_logs(self) -> None:
        self._c.paths.log_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._c.paths.log_dir)))

    def test_telegram(self) -> None:
        self.telegram_button.setEnabled(False)
        self.telegram_result.setText("Testing...")
        c = self._c

        def work() -> tuple[bool, str]:
            if not c.has_telegram_credentials():
                return False, "No bot token / chat id saved (Settings > Telegram)."
            state = c.check_telegram()
            if state.status is TelegramStatus.DISABLED:
                return False, "Telegram is disabled (Settings > Telegram > Enabled)."
            if state.status is not TelegramStatus.OK:
                return False, f"Token check failed: {state.detail}"
            result = c.send_test_telegram()
            ok = result.outcome is SendOutcome.SENT
            return ok, "Test message sent." if ok else f"Send failed: {result.error}"

        self._runner.run(work, self._telegram_tested)

    def _telegram_tested(self, result: Any) -> None:
        self.telegram_button.setEnabled(True)
        if isinstance(result, Exception):
            self.telegram_result.setText(f"Unexpected error: {result!r}")
        else:
            self.telegram_result.setText(result[1])
        self.refresh_notifications()

    def _browse_sound(self, edit: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose a WAV file", "", "WAV audio (*.wav)")
        if path:
            edit.setText(path)
            self._sound_changed()

    def _sound_changed(self) -> None:
        new = SoundConfig(
            enabled=self.sound_check.isChecked(),
            buy_sound=self.buy_edit.text().strip(),
            sell_sound=self.sell_edit.text().strip(),
        )
        if new == self._c.config.sound:
            return
        try:
            self._c.apply_config(replace(self._c.config, sound=new))
        except (ConfigError, OSError) as exc:
            QMessageBox.warning(self, "Could not save sound settings", str(exc))

    def _sync_sound_widgets(self) -> None:
        sound = self._c.config.sound
        for widget, value in ((self.buy_edit, sound.buy_sound), (self.sell_edit, sound.sell_sound)):
            widget.setText(value)
        self.sound_check.blockSignals(True)
        self.sound_check.setChecked(sound.enabled)
        self.sound_check.blockSignals(False)

    # ------------------------------------------------------------------ state -> widgets

    def _on_snapshot(self, snapshot: MonitorSnapshot) -> None:
        tz = self._c.display_tz
        self.status_pill.show_view(connection_view(snapshot))
        self.symbol_label.setText(snapshot.symbol or "-")
        self.timeframe_label.setText(snapshot.timeframe.value)
        self.ma_label.setText(format_ma(snapshot, self._c.config.strategy.ma.label))
        self.candle_label.setText(format_time(snapshot.last_candle_time, tz))
        self.detail_label.setText(connection_view(snapshot).detail)
        self.last_signal_label.setText(last_signal_text(snapshot.last_signal, tz))
        running = self._c.is_running
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)

    def refresh_notifications(self) -> None:
        self.telegram_pill.show_view(telegram_view(self._c.telegram_state()))
        warnings = self._c.warnings()
        self.warning_label.setText("\n".join(warnings))
        self.warning_label.setVisible(bool(warnings))

    def _on_record(self, record: SignalRecord) -> None:
        self._insert_row(record, at_top=True)
        self._apply_history_filter()
        if record.outcome.value == "delivered" and self._tray is not None:
            s = record.signal
            self._tray.showMessage(
                f"{s.direction.value} signal - {s.symbol}",
                f"{' + '.join(p.value for p in s.patterns)} at {s.entry_price:.2f}",
                QSystemTrayIcon.MessageIcon.Information,
                10_000,
            )

    # ------------------------------------------------------------------ history table

    def _reload_history(self) -> None:
        self.table.setRowCount(0)
        for record in self._c.records:  # oldest -> newest; insert at top so newest ends first
            self._insert_row(record, at_top=True)
        self._apply_history_filter()

    def _insert_row(self, record: SignalRecord, *, at_top: bool) -> None:
        row_view = history_row(record, self._c.display_tz)
        row = 0 if at_top else self.table.rowCount()
        self.table.insertRow(row)
        for col, text in enumerate(row_view.cells):
            item = QTableWidgetItem(text)
            if col == 1:
                color = BUY_COLOR if row_view.direction is Direction.BUY else SELL_COLOR
                item.setForeground(QBrush(QColor(color)))
            elif col == 6:
                item.setForeground(QBrush(QColor(LEVEL_COLORS[row_view.level])))
            item.setData(Qt.ItemDataRole.UserRole, row_view.delivered)
            self.table.setItem(row, col, item)
        while self.table.rowCount() > self._c.config.monitor.history_size:
            self.table.removeRow(self.table.rowCount() - 1)

    def _apply_history_filter(self) -> None:
        show_all = self.show_all_check.isChecked()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            delivered = bool(item.data(Qt.ItemDataRole.UserRole)) if item else True
            self.table.setRowHidden(row, not (show_all or delivered))

    # ------------------------------------------------------------------ tray / lifecycle

    def _build_tray(self) -> QSystemTrayIcon | None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        tray = QSystemTrayIcon(app_icon(), self)
        tray.setToolTip(TITLE)
        self._tray_menu = QMenu(self)
        show = QAction("Show window", self)
        show.triggered.connect(self.show_window)
        start = QAction("Start monitoring", self)
        start.triggered.connect(self.start_monitoring)
        stop = QAction("Stop monitoring", self)
        stop.triggered.connect(self.stop_monitoring)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.quit_app)
        for action in (show, start, stop, quit_action):
            self._tray_menu.addAction(action)
        tray.setContextMenu(self._tray_menu)
        tray.activated.connect(
            lambda reason: (
                self.show_window() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
            )
        )
        tray.show()
        return tray

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_app(self) -> None:
        self._quitting = True
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        if self._tray is not None and not self._quitting:
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self._tray.showMessage(
                    "Still running",
                    "Monitoring continues in the background. Use the tray icon to quit.",
                    QSystemTrayIcon.MessageIcon.Information,
                    5000,
                )
            return
        self._timer.stop()
        self._c.shutdown()
        if self._on_stop is not None:
            self._on_stop()
        if self._tray is not None:
            self._tray.hide()
        event.accept()
        QApplication.quit()
