"""Settings dialog. Widgets are generated from `forms.field_specs()`; validation is the config
layer's (`apply_values`), so this file holds layout and nothing else."""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from xauassist.app.controller import AppController
from xauassist.config.models import ConfigError
from xauassist.gui.forms import (
    SECTION_TITLES,
    FieldKind,
    FieldPath,
    FieldSpec,
    apply_values,
    current_values,
    field_specs,
    group_title,
    sections,
)
from xauassist.gui.qt_bridge import BackgroundRunner
from xauassist.gui.theme import pill_style
from xauassist.gui.view_models import Level
from xauassist.notify.telegram import SendOutcome, TelegramStatus

CREDENTIALS_TITLE = (
    "Bot credentials - kept in the OS credential store (or secrets.toml); never in config.toml"
)
_RESTART_NOTE = (
    "Strategy, validation, data-feed and MT5 changes restart monitoring "
    "(history is re-read; it never raises alerts)."
)


class SettingsDialog(QDialog):
    def __init__(self, controller: AppController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(640, 640)
        self._controller = controller
        self._inputs: dict[FieldPath, QWidget] = {}
        self._runner = BackgroundRunner(self)
        values = current_values(controller.config)

        layout = QVBoxLayout(self)
        note = QLabel(_RESTART_NOTE)
        note.setWordWrap(True)
        layout.addWidget(note)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        for name, specs in sections(field_specs()).items():
            self.tabs.addTab(self._build_tab(name, specs, values), SECTION_TITLES[name])
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ---- generated tabs ----

    def _build_tab(
        self, name: str, specs: list[FieldSpec], values: dict[FieldPath, Any]
    ) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        if name == "telegram":
            page_layout.addWidget(self._build_credentials_group())
        groups: dict[str, QFormLayout] = {}
        for spec in specs:
            group_name = spec.path[1] if len(spec.path) > 2 else "general"
            form = groups.get(group_name)
            if form is None:
                box = QGroupBox(group_title(group_name))
                form = QFormLayout(box)
                groups[group_name] = form
                page_layout.addWidget(box)
            widget = self._make_input(spec, values[spec.path])
            self._inputs[spec.path] = widget
            label = QLabel(spec.label)
            if spec.help:
                label.setToolTip(spec.help)
                widget.setToolTip(spec.help)
            form.addRow(label, widget)
        page_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    @staticmethod
    def _make_input(spec: FieldSpec, value: Any) -> QWidget:
        if spec.kind is FieldKind.BOOL:
            check = QCheckBox()
            check.setChecked(bool(value))
            return check
        if spec.kind is FieldKind.INT:
            spin = QSpinBox()
            spin.setRange(0, 1_000_000)
            spin.setValue(int(value))
            return spin
        if spec.kind is FieldKind.FLOAT:
            dspin = QDoubleSpinBox()
            dspin.setDecimals(4)
            dspin.setRange(0.0, 1_000_000.0)
            dspin.setSingleStep(0.1)
            dspin.setValue(float(value))
            return dspin
        if spec.kind is FieldKind.CHOICE:
            combo = QComboBox()
            combo.addItems(list(spec.choices))
            combo.setCurrentText(str(value))
            return combo
        edit = QLineEdit(str(value))
        edit.setMinimumWidth(260)
        return edit

    @staticmethod
    def _read(widget: QWidget) -> Any:
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        if isinstance(widget, QSpinBox):
            return widget.value()
        if isinstance(widget, QDoubleSpinBox):
            return widget.value()
        if isinstance(widget, QComboBox):
            return widget.currentText()
        assert isinstance(widget, QLineEdit)
        return widget.text().strip()

    def widget_for(self, key: str) -> QWidget:
        return self._inputs[tuple(key.split("."))]

    def spin(self, key: str) -> QSpinBox:
        widget = self.widget_for(key)
        assert isinstance(widget, QSpinBox)
        return widget

    def combo(self, key: str) -> QComboBox:
        widget = self.widget_for(key)
        assert isinstance(widget, QComboBox)
        return widget

    def check(self, key: str) -> QCheckBox:
        widget = self.widget_for(key)
        assert isinstance(widget, QCheckBox)
        return widget

    def line(self, key: str) -> QLineEdit:
        widget = self.widget_for(key)
        assert isinstance(widget, QLineEdit)
        return widget

    # ---- applying ----

    def collect(self) -> dict[FieldPath, Any]:
        return {path: self._read(widget) for path, widget in self._inputs.items()}

    def apply(self) -> bool:
        """Validate and apply the form. Returns False (after telling the user) if invalid."""
        try:
            new_config = apply_values(self._controller.config, self.collect())
            self._controller.apply_config(new_config)
        except (ConfigError, OSError) as exc:
            QMessageBox.warning(self, "Invalid settings", str(exc))
            return False
        return True

    def accept(self) -> None:
        if self.apply():
            super().accept()

    # ---- telegram credentials ----

    def _build_credentials_group(self) -> QGroupBox:
        box = QGroupBox(CREDENTIALS_TITLE)
        form = QFormLayout(box)
        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.chat_edit = QLineEdit(self._controller.saved_chat_id())
        has_saved = self._controller.has_telegram_credentials()
        self.token_edit.setPlaceholderText(
            "saved - type to replace" if has_saved else "123456789:AA..."
        )
        self.chat_edit.setPlaceholderText("your chat id, e.g. 123456789")
        form.addRow("Bot token", self.token_edit)
        form.addRow("Chat id", self.chat_edit)
        self.save_credentials_button = QPushButton("Save credentials")
        self.remove_credentials_button = QPushButton("Remove")
        self.test_button = QPushButton("Test connection + send test message")
        for button in (
            self.save_credentials_button,
            self.remove_credentials_button,
            self.test_button,
        ):
            form.addRow(button)
        self.credentials_status = QLabel("")
        self.credentials_status.setWordWrap(True)
        form.addRow(self.credentials_status)
        self.save_credentials_button.clicked.connect(self.save_credentials)
        self.remove_credentials_button.clicked.connect(self.remove_credentials)
        self.test_button.clicked.connect(self.test_telegram)
        return box

    def _show_status(self, text: str, level: Level) -> None:
        self.credentials_status.setText(text)
        self.credentials_status.setStyleSheet(pill_style(level))

    def save_credentials(self) -> None:
        token = self.token_edit.text().strip()
        chat = self.chat_edit.text().strip()
        if not token and self._controller.has_telegram_credentials():
            self._show_status("Enter the token again to change the credentials.", Level.WARN)
            return
        try:
            self._controller.save_telegram_credentials(token, chat)
        except ConfigError as exc:
            self._show_status(str(exc), Level.ERROR)
            return
        self.token_edit.clear()
        self.token_edit.setPlaceholderText("saved - type to replace")
        self._show_status("Credentials saved.", Level.OK)

    def remove_credentials(self) -> None:
        self._controller.clear_telegram_credentials()
        self.token_edit.clear()
        self.token_edit.setPlaceholderText("123456789:AA...")
        self.chat_edit.clear()
        self._show_status("Credentials removed.", Level.INFO)

    def test_telegram(self) -> None:
        self.test_button.setEnabled(False)
        self._show_status("Testing...", Level.INFO)
        controller = self._controller

        def work() -> tuple[bool, str]:
            if not controller.has_telegram_credentials():
                return False, "No credentials saved yet."
            state = controller.check_telegram()
            if state.status is TelegramStatus.DISABLED:
                return False, "Telegram is disabled: tick 'Enabled' (and Save) first."
            if state.status is not TelegramStatus.OK:
                return False, f"Token check failed: {state.detail or state.status.value}"
            result = controller.send_test_telegram()
            if result.outcome is SendOutcome.SENT:
                return True, "Connected. A test message was sent to your chat."
            return False, f"Could not send: {result.error}"

        self._runner.run(work, self._test_finished)

    def _test_finished(self, result: Any) -> None:
        self.test_button.setEnabled(True)
        if isinstance(result, Exception):
            self._show_status(f"Unexpected error: {result!r}", Level.ERROR)
            return
        ok, message = result
        self._show_status(message, Level.OK if ok else Level.ERROR)
