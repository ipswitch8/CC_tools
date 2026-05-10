# -*- coding: utf-8 -*-
"""
cpsm.ui.dialogs.launcher_templates — Launcher Templates dialog.

Spec section: §4.9

Layout:
  - Left: QListWidget of profiles (claude-remote / claude-local / ssh-shell /
          local-shell / _placeholder) + custom templates from doc.launch_templates[].
  - Right: QPlainTextEdit (monospace, objectName="text_template_body") showing body.
  - Below: "Save Override" / "Restore Default" / "Edit Custom Template" buttons.

Rules:
  - _placeholder.sh: read-only — Save Override and editor both disabled.
  - Built-ins (not _placeholder): Save Override writes to override_dir;
    Restore Default calls template_service.restore_default(profile).
  - Custom templates (from doc.launch_templates[]): editing updates
    doc.launch_templates[i].bash; "Edit Custom Template" lets the user
    rename id / change description.
"""

from __future__ import annotations

import logging
from importlib.resources import files
from typing import TYPE_CHECKING

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from cpsm.data.schema import CpsmDocument
    from cpsm.services.template_service import TemplateService

__all__ = ["LauncherTemplatesDialog"]

logger = logging.getLogger(__name__)

_BUILTIN_PROFILES = [
    "claude-remote",
    "claude-local",
    "ssh-shell",
    "local-shell",
]
_PLACEHOLDER_ID = "_placeholder"
_BUILTIN_PACKAGE = "cpsm.resources.launcher_templates"
_PROFILE_TO_FILE = {
    "claude-remote": "claude-remote.sh",
    "claude-local": "claude-local.sh",
    "ssh-shell": "ssh-shell.sh",
    "local-shell": "local-shell.sh",
    "_placeholder": "_placeholder.sh",
}


def _load_builtin_body(profile_key: str) -> str:
    """Load the built-in template body for the given profile key."""
    filename = _PROFILE_TO_FILE.get(profile_key)
    if filename is None:
        return ""
    resource = files(_BUILTIN_PACKAGE).joinpath(filename)
    return resource.read_text(encoding="utf-8")


class LauncherTemplatesDialog(QDialog):
    """View and edit launcher templates.

    Parameters
    ----------
    template_service:
        TemplateService for loading / restoring built-in templates.
    doc:
        Optional CpsmDocument; when supplied, custom templates from
        doc.launch_templates[] are shown and can be edited.
    parent:
        Optional parent widget.
    """

    def __init__(
        self,
        template_service: TemplateService,
        doc: CpsmDocument | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._template_service = template_service
        self._doc = doc

        self.setWindowTitle("Launcher Templates")
        self.setObjectName("dlg_launcher_templates")
        self.setMinimumSize(760, 480)

        main_layout = QVBoxLayout(self)

        splitter = QSplitter()
        splitter.setObjectName("splitter_templates")

        # ---- Left: list of templates ----
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self._list = QListWidget()
        self._list.setObjectName("list_templates")
        self._list.setAccessibleName("Template list")
        left_layout.addWidget(self._list)
        splitter.addWidget(left_widget)

        # ---- Right: editor ----
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self._text_body = QPlainTextEdit()
        self._text_body.setObjectName("text_template_body")
        self._text_body.setAccessibleName("Template body")
        mono = QFont("Monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._text_body.setFont(mono)
        right_layout.addWidget(self._text_body)

        # Buttons row
        btn_row = QHBoxLayout()
        self._btn_save = QPushButton("Save Override")
        self._btn_save.setObjectName("btn_save_override")
        self._btn_save.setAccessibleName("Save override")
        self._btn_save.clicked.connect(self._on_save_override)
        btn_row.addWidget(self._btn_save)

        self._btn_restore = QPushButton("Restore Default")
        self._btn_restore.setObjectName("btn_restore_default")
        self._btn_restore.setAccessibleName("Restore default")
        self._btn_restore.clicked.connect(self._on_restore_default)
        btn_row.addWidget(self._btn_restore)

        self._btn_edit_custom = QPushButton("Edit Custom Template")
        self._btn_edit_custom.setObjectName("btn_edit_custom")
        self._btn_edit_custom.setAccessibleName("Edit custom template")
        self._btn_edit_custom.clicked.connect(self._on_edit_custom)
        btn_row.addWidget(self._btn_edit_custom)
        btn_row.addStretch()

        right_layout.addLayout(btn_row)
        splitter.addWidget(right_widget)
        splitter.setSizes([200, 540])

        main_layout.addWidget(splitter)

        # Close button
        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_box.setObjectName("button_box")
        close_box.rejected.connect(self.reject)
        close_box.accepted.connect(self.accept)
        # "Close" maps to rejected in QDialogButtonBox
        main_layout.addWidget(close_box)

        # Populate list
        self._populate_list()
        self._list.currentRowChanged.connect(self._on_selection_changed)
        if self._list.count() > 0:
            self._list.setCurrentRow(0)

    # ------------------------------------------------------------------
    # Populate
    # ------------------------------------------------------------------

    def _populate_list(self) -> None:
        self._list.clear()
        # Built-in profiles
        for profile in _BUILTIN_PROFILES:
            item = QListWidgetItem(profile)
            item.setData(256, ("builtin", profile))  # Qt.UserRole = 256
            self._list.addItem(item)
        # _placeholder
        item = QListWidgetItem("_placeholder.sh (read-only)")
        item.setData(256, ("placeholder", _PLACEHOLDER_ID))
        self._list.addItem(item)
        # Custom templates
        if self._doc is not None:
            for tpl in self._doc.launch_templates:
                label = f"[custom] {tpl.id}"
                if tpl.description:
                    label += f" — {tpl.description}"
                tpl_item = QListWidgetItem(label)
                tpl_item.setData(256, ("custom", tpl.id))
                self._list.addItem(tpl_item)

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def _current_entry(self) -> tuple[str, str] | None:
        """Return (kind, key) for the currently selected item, or None."""
        item = self._list.currentItem()
        if item is None:
            return None
        data = item.data(256)
        if not isinstance(data, tuple) or len(data) != 2:
            return None
        return data[0], data[1]

    def _on_selection_changed(self, _row: int) -> None:
        entry = self._current_entry()
        if entry is None:
            self._text_body.setPlainText("")
            self._set_editing_enabled(False, is_placeholder=False, is_custom=False)
            return

        kind, key = entry
        if kind == "placeholder":
            body = _load_builtin_body("_placeholder")
            self._text_body.setPlainText(body)
            self._set_editing_enabled(False, is_placeholder=True, is_custom=False)
        elif kind == "builtin":
            # Load override or built-in
            try:
                body = self._template_service._load_builtin_or_override(key)
            except Exception:
                body = _load_builtin_body(key)
            self._text_body.setPlainText(body)
            self._set_editing_enabled(True, is_placeholder=False, is_custom=False)
        elif kind == "custom":
            body = ""
            if self._doc is not None:
                for tpl in self._doc.launch_templates:
                    if tpl.id == key:
                        body = tpl.bash
                        break
            self._text_body.setPlainText(body)
            self._set_editing_enabled(True, is_placeholder=False, is_custom=True)

    def _set_editing_enabled(self, enabled: bool, *, is_placeholder: bool, is_custom: bool) -> None:
        """Configure widget states based on selection type."""
        self._text_body.setReadOnly(not enabled)
        self._btn_save.setEnabled(enabled and not is_custom)
        self._btn_restore.setEnabled(enabled and not is_custom)
        self._btn_edit_custom.setEnabled(is_custom)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_save_override(self) -> None:
        """Write editor content to override_dir for the selected built-in profile."""
        entry = self._current_entry()
        if entry is None or entry[0] != "builtin":
            return
        _, profile = entry
        override_dir = getattr(self._template_service, "_override_dir", None)
        if override_dir is None:
            QMessageBox.warning(
                self,
                "No Override Directory",
                "TemplateService has no override_dir configured.",
            )
            return
        filename = _PROFILE_TO_FILE.get(profile)
        if filename is None:
            return
        import pathlib

        override_dir = pathlib.Path(override_dir)
        override_dir.mkdir(parents=True, exist_ok=True)
        target = override_dir / filename
        target.write_text(self._text_body.toPlainText(), encoding="utf-8")
        QMessageBox.information(self, "Saved", f"Override saved to:\n{target}")

    def _on_restore_default(self) -> None:
        """Delete the override file for the selected built-in profile."""
        entry = self._current_entry()
        if entry is None or entry[0] != "builtin":
            return
        _, profile = entry
        try:
            self._template_service.restore_default(profile)
        except Exception as exc:
            QMessageBox.warning(self, "Error", str(exc))
            return
        # Reload body from built-in
        body = _load_builtin_body(profile)
        self._text_body.setPlainText(body)
        QMessageBox.information(self, "Restored", f"Default template restored for '{profile}'.")

    def _on_edit_custom(self) -> None:
        """Prompt for a new description, then persist body changes to doc."""
        entry = self._current_entry()
        if entry is None or entry[0] != "custom" or self._doc is None:
            return
        _, key = entry
        for tpl in self._doc.launch_templates:
            if tpl.id == key:
                new_desc, ok = QInputDialog.getText(
                    self,
                    "Edit Custom Template",
                    "Description:",
                    text=tpl.description or "",
                )
                if ok:
                    tpl.description = new_desc
                # Always save the body from the editor
                tpl.bash = self._text_body.toPlainText()
                self._populate_list()
                # Re-select the same item
                for i in range(self._list.count()):
                    item = self._list.item(i)
                    if item and item.data(256) == ("custom", key):
                        self._list.setCurrentRow(i)
                        break
                return
