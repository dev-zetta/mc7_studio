"""Local editor for ordered automatic-profile application rules."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..automatic_profiles import (
    MAX_RULES,
    ApplicationProfileRule,
    AutomaticProfileError,
    AutomaticProfileSettings,
)
from .widgets import STYLE, SpinBox, label


class AutomaticProfilesDialog(QDialog):
    """Edit local automatic-profile settings without touching the mouse.

    ``settings()`` returns a normalized immutable value. The caller remains in
    charge of persisting it after the dialog is accepted.
    """

    def __init__(
        self,
        settings: AutomaticProfileSettings | None = None,
        parent=None,
    ):
        super().__init__(parent)
        initial = settings if settings is not None else AutomaticProfileSettings()
        self._initial_settings = initial.normalized()
        self._rules = list(self._initial_settings.rules)
        self._loading_rule = False
        self.result_settings: AutomaticProfileSettings | None = None

        self.setWindowTitle("Automatic profiles")
        self.resize(920, 620)
        self.setMinimumSize(720, 500)
        self.setStyleSheet(STYLE)

        body = QVBoxLayout(self)
        body.setContentsMargins(24, 22, 24, 22)
        body.setSpacing(14)
        body.addWidget(label("Automatic profiles", "title"))
        body.addWidget(
            label(
                "Match the foreground application by an exact executable path or "
                "macOS bundle identifier. The first enabled matching rule wins.",
                "muted",
                True,
            )
        )

        top = QHBoxLayout()
        self.enabled_checkbox = QCheckBox("Switch profiles automatically")
        self.enabled_checkbox.setChecked(self._initial_settings.enabled)
        self.enabled_checkbox.setAccessibleName("Enable automatic profile switching")
        top.addWidget(self.enabled_checkbox, 1)
        top.addWidget(QLabel("Default profile"))
        self.default_profile_spin = SpinBox()
        self.default_profile_spin.setRange(1, 5)
        self.default_profile_spin.setValue(self._initial_settings.default_profile_slot)
        self.default_profile_spin.setAccessibleName("Default automatic profile")
        top.addWidget(self.default_profile_spin)
        body.addLayout(top)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        body.addWidget(splitter, 1)

        rules_pane = QWidget()
        rules_layout = QVBoxLayout(rules_pane)
        rules_layout.setContentsMargins(0, 0, 8, 0)
        rules_layout.setSpacing(10)
        rules_layout.addWidget(label("Rules in priority order", "sectionTitle"))
        self.rule_list = QListWidget()
        self.rule_list.setAccessibleName("Automatic profile rules in priority order")
        self.rule_list.currentRowChanged.connect(self._load_rule)
        rules_layout.addWidget(self.rule_list, 1)

        add_row = QHBoxLayout()
        self.add_executable_button = QPushButton("Add executable…")
        self.add_executable_button.clicked.connect(self.add_executable)
        self.add_bundle_button = QPushButton("Add bundle ID…")
        self.add_bundle_button.clicked.connect(self.add_bundle_id)
        add_row.addWidget(self.add_executable_button)
        add_row.addWidget(self.add_bundle_button)
        rules_layout.addLayout(add_row)

        order_row = QHBoxLayout()
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self.remove_selected)
        self.move_up_button = QPushButton("Move up")
        self.move_up_button.clicked.connect(lambda: self.move_selected(-1))
        self.move_down_button = QPushButton("Move down")
        self.move_down_button.clicked.connect(lambda: self.move_selected(1))
        order_row.addWidget(self.remove_button)
        order_row.addStretch(1)
        order_row.addWidget(self.move_up_button)
        order_row.addWidget(self.move_down_button)
        rules_layout.addLayout(order_row)
        splitter.addWidget(rules_pane)

        self.editor = QFrame()
        self.editor.setObjectName("card")
        editor_layout = QVBoxLayout(self.editor)
        editor_layout.setContentsMargins(20, 18, 20, 20)
        editor_layout.setSpacing(14)
        editor_layout.addWidget(label("Selected rule", "sectionTitle"))
        form = QFormLayout()
        form.setSpacing(12)
        self.rule_enabled_checkbox = QCheckBox("Use this rule")
        self.rule_enabled_checkbox.toggled.connect(self._rule_edited)
        form.addRow("Status", self.rule_enabled_checkbox)
        self.rule_name_edit = QLineEdit()
        self.rule_name_edit.setMaxLength(80)
        self.rule_name_edit.setPlaceholderText("Application name")
        self.rule_name_edit.setAccessibleName("Automatic profile rule name")
        self.rule_name_edit.textChanged.connect(self._rule_edited)
        form.addRow("Name", self.rule_name_edit)
        self.rule_profile_spin = SpinBox()
        self.rule_profile_spin.setRange(1, 5)
        self.rule_profile_spin.setAccessibleName("Profile for selected application rule")
        self.rule_profile_spin.valueChanged.connect(self._rule_edited)
        form.addRow("Profile", self.rule_profile_spin)
        self.identity_kind_label = QLabel()
        self.identity_kind_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        form.addRow("Identity type", self.identity_kind_label)
        self.identity_value_edit = QLineEdit()
        self.identity_value_edit.setReadOnly(True)
        self.identity_value_edit.setAccessibleName("Exact application identity")
        form.addRow("Exact identity", self.identity_value_edit)
        editor_layout.addLayout(form)
        editor_layout.addWidget(
            label(
                "Application identities are stored only on this computer. Rule order "
                "sets priority when an identity matches more than one configured rule.",
                "muted",
                True,
            )
        )
        editor_layout.addStretch(1)
        splitter.addWidget(self.editor)
        splitter.setSizes([390, 490])

        self.error_label = label("", "error", True)
        self.error_label.setAccessibleName("Automatic profile validation error")
        self.error_label.hide()
        body.addWidget(self.error_label)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.save_button = self.button_box.button(QDialogButtonBox.StandardButton.Save)
        self.cancel_button = self.button_box.button(
            QDialogButtonBox.StandardButton.Cancel
        )
        self.save_button.setObjectName("primary")
        self.save_button.setText("Save rules")
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        body.addWidget(self.button_box)

        self._refresh_rule_list()
        if self._rules:
            self.rule_list.setCurrentRow(0)
        else:
            self._load_rule(-1)

    def settings(self) -> AutomaticProfileSettings:
        """Return the current editor state as a validated immutable value."""

        return AutomaticProfileSettings(
            enabled=self.enabled_checkbox.isChecked(),
            default_profile_slot=self.default_profile_spin.value(),
            rules=tuple(self._rules),
        ).normalized()

    def add_executable(self, path: str | Path | None = None) -> bool:
        """Add a rule for a real, absolute executable path.

        Supplying ``path`` bypasses the chooser, which also makes this method
        useful to callers that already selected an application.
        """

        if path is None or isinstance(path, bool):
            filename, _ = QFileDialog.getOpenFileName(
                self,
                "Choose an application executable",
                "",
                "Applications (*)",
            )
            if not filename:
                return False
            path = filename
        try:
            resolved = Path(path).expanduser().resolve(strict=True)
            if not resolved.is_file():
                raise AutomaticProfileError("Choose a regular application executable.")
            rule = ApplicationProfileRule(
                name=self._default_name(resolved.name, "Application"),
                profile_slot=self.default_profile_spin.value(),
                match_kind="executable_path",
                match_value=resolved.as_posix(),
            ).normalized()
        except (OSError, RuntimeError, AutomaticProfileError, TypeError) as error:
            self._show_error(str(error) or "The executable path is invalid.")
            return False
        return self._append_rule(rule)

    def add_bundle_id(self, bundle_id: str | None = None) -> bool:
        """Add a macOS bundle-identifier rule, optionally without a prompt."""

        if bundle_id is None or isinstance(bundle_id, bool):
            value, accepted = QInputDialog.getText(
                self,
                "Add macOS application",
                "Bundle identifier, for example com.example.Game:",
            )
            if not accepted:
                return False
            bundle_id = value
        try:
            text = str(bundle_id).strip()
            suffix = text.rsplit(".", 1)[-1] if text else "Application"
            rule = ApplicationProfileRule(
                name=self._default_name(suffix, "Application"),
                profile_slot=self.default_profile_spin.value(),
                match_kind="bundle_id",
                match_value=text,
            ).normalized()
        except (AutomaticProfileError, TypeError, ValueError) as error:
            self._show_error(str(error) or "The bundle identifier is invalid.")
            return False
        return self._append_rule(rule)

    def remove_selected(self) -> bool:
        row = self.rule_list.currentRow()
        if not 0 <= row < len(self._rules):
            return False
        del self._rules[row]
        next_row = min(row, len(self._rules) - 1)
        self._refresh_rule_list(next_row)
        self._clear_error()
        return True

    def move_selected(self, offset: int) -> bool:
        """Move the selected rule one or more rows and retain its selection."""

        row = self.rule_list.currentRow()
        destination = row + int(offset)
        if not 0 <= row < len(self._rules) or not 0 <= destination < len(self._rules):
            return False
        rule = self._rules.pop(row)
        self._rules.insert(destination, rule)
        self._refresh_rule_list(destination)
        self._clear_error()
        return True

    def accept(self) -> None:
        try:
            self.result_settings = self.settings()
        except AutomaticProfileError as error:
            self.result_settings = None
            self._show_error(str(error))
            return
        self._clear_error()
        super().accept()

    def _append_rule(self, rule: ApplicationProfileRule) -> bool:
        if len(self._rules) >= MAX_RULES:
            self._show_error(f"Automatic profiles support at most {MAX_RULES} rules.")
            return False
        identity = (rule.match_kind, rule.match_value)
        if any((existing.match_kind, existing.match_value) == identity for existing in self._rules):
            self._show_error("This application already has an automatic-profile rule.")
            return False
        self._rules.append(rule)
        self._refresh_rule_list(len(self._rules) - 1)
        self._clear_error()
        return True

    def _refresh_rule_list(self, selected_row: int | None = None) -> None:
        if selected_row is None:
            selected_row = self.rule_list.currentRow()
        self.rule_list.blockSignals(True)
        self.rule_list.clear()
        for rule in self._rules:
            status = "Enabled" if rule.enabled else "Disabled"
            identity_type = (
                "Executable" if rule.match_kind == "executable_path" else "Bundle ID"
            )
            self.rule_list.addItem(
                f"{rule.name}  →  Profile {rule.profile_slot}\n"
                f"{status} · {identity_type}: {rule.match_value}"
            )
        self.rule_list.blockSignals(False)
        if 0 <= selected_row < len(self._rules):
            self.rule_list.setCurrentRow(selected_row)
        else:
            self.rule_list.setCurrentRow(-1)
            self._load_rule(-1)
        self._update_buttons()

    def _load_rule(self, row: int) -> None:
        self._loading_rule = True
        try:
            available = 0 <= row < len(self._rules)
            self.editor.setEnabled(available)
            if available:
                rule = self._rules[row]
                self.rule_enabled_checkbox.setChecked(rule.enabled)
                self.rule_name_edit.setText(rule.name)
                self.rule_profile_spin.setValue(rule.profile_slot)
                self.identity_kind_label.setText(
                    "Executable path"
                    if rule.match_kind == "executable_path"
                    else "macOS bundle identifier"
                )
                self.identity_value_edit.setText(rule.match_value)
            else:
                self.rule_enabled_checkbox.setChecked(False)
                self.rule_name_edit.clear()
                self.rule_profile_spin.setValue(1)
                self.identity_kind_label.clear()
                self.identity_value_edit.clear()
        finally:
            self._loading_rule = False
        self._update_buttons()

    def _rule_edited(self, *_unused) -> None:
        if self._loading_rule:
            return
        row = self.rule_list.currentRow()
        if not 0 <= row < len(self._rules):
            return
        self._rules[row] = replace(
            self._rules[row],
            enabled=self.rule_enabled_checkbox.isChecked(),
            name=self.rule_name_edit.text(),
            profile_slot=self.rule_profile_spin.value(),
        )
        self._refresh_rule_list(row)
        self._clear_error()

    def _update_buttons(self) -> None:
        row = self.rule_list.currentRow()
        selected = 0 <= row < len(self._rules)
        self.remove_button.setEnabled(selected)
        self.move_up_button.setEnabled(selected and row > 0)
        self.move_down_button.setEnabled(selected and row < len(self._rules) - 1)
        can_add = len(self._rules) < MAX_RULES
        self.add_executable_button.setEnabled(can_add)
        self.add_bundle_button.setEnabled(can_add)

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    def _clear_error(self) -> None:
        self.error_label.clear()
        self.error_label.hide()

    @staticmethod
    def _default_name(candidate: str, fallback: str) -> str:
        value = candidate.strip() or fallback
        return value[:80]
