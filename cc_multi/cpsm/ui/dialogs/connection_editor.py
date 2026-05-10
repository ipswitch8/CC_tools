# -*- coding: utf-8 -*-
"""
cpsm.ui.dialogs.connection_editor — Connection Editor dialog.

Spec sections: §4.5, §2.3

Layout (top → bottom):
  - Identity group: name, id (auto-suggested; locked after create)
  - ConnectionForm (wraps all profile-conditional fields)
  - Test Connection button + result area
  - Member of: <read-only label with group links>
  - Bottom button bar: Save / Cancel

Test Connection behaviour per profile:
  - claude-remote / ssh-shell: SshTestConnectionTask via QThreadPool
  - claude-local / local-shell: Path(project_folder).expanduser().is_dir()
  - custom: TemplateService.render preview
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from cpsm.ui.widgets.connection_form import ConnectionForm

__all__ = ["ConnectionEditorDialog"]

_ID_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


class ConnectionEditorDialog(QDialog):
    """Dialog for creating or editing a Connection record.

    Signals:
        open_group_editor(str): emitted when the user clicks a group link in
            the "Member of" line; carries the group_id.
    """

    open_group_editor: Signal = Signal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        connection_data: dict[str, Any] | None = None,
        groups: list[dict[str, Any]] | None = None,
        available_key_ids: list[str] | None = None,
        available_template_ids: list[str] | None = None,
        available_connection_ids: list[str] | None = None,
        available_layout_ids: list[str] | None = None,
        is_new: bool = True,
        # Injected for tests
        ssh_test_factory: Any | None = None,
        template_service: Any | None = None,
    ) -> None:
        super().__init__(parent)

        self._is_new = is_new
        self._groups = groups or []
        self._ssh_test_factory = ssh_test_factory
        self._template_service = template_service
        self._connection_data: dict[str, Any] = dict(connection_data or {})
        self._closed = False  # Fix #11 — guard against post-close slot calls

        self.setObjectName("dlg_connection_editor")
        self.setAccessibleName("Connection Editor Dialog")
        self.setAccessibleDescription("Dialog for creating or editing a CPSM connection")
        self.setWindowTitle("New Connection" if is_new else "Edit Connection")
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setMinimumWidth(640)
        self.setMinimumHeight(600)

        self._build_ui(
            available_key_ids=available_key_ids or [],
            available_template_ids=available_template_ids or [],
            available_connection_ids=available_connection_ids or [],
        )

        # Populate with existing data
        if connection_data:
            self._populate(connection_data)
        else:
            self._update_id_from_name("")

        self._update_member_of_label()
        self._on_form_validation_changed(self._form.is_valid())

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(
        self,
        available_key_ids: list[str],
        available_template_ids: list[str],
        available_connection_ids: list[str],
    ) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(16, 16, 16, 16)

        # ---- Identity group ----
        grp_identity = QGroupBox("Identity")
        grp_identity.setObjectName("grp_identity")
        grp_identity.setAccessibleName("Identity group box")
        id_form = QFormLayout(grp_identity)
        id_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self._edit_name = QLineEdit()
        self._edit_name.setObjectName("edit_name")
        self._edit_name.setAccessibleName("Connection name field")
        self._edit_name.setAccessibleDescription("Human-readable display name for this connection")
        self._edit_name.setPlaceholderText("e.g. WebApp Frontend (prod)")
        self._err_name = QLabel()
        self._err_name.setObjectName("error_name")
        self._err_name.setAccessibleName("Name error label")
        self._err_name.setStyleSheet("color: red;")
        self._err_name.setVisible(False)
        id_form.addRow("Name *", self._edit_name)
        id_form.addRow("", self._err_name)

        self._edit_id = QLineEdit()
        self._edit_id.setObjectName("edit_id")
        self._edit_id.setAccessibleName("Connection ID field")
        self._edit_id.setAccessibleDescription(
            "Unique slug ID; auto-suggested from name; locked after first save"
        )
        self._edit_id.setPlaceholderText("e.g. webapp-frontend-prod")
        if not self._is_new:
            self._edit_id.setReadOnly(True)
            self._edit_id.setToolTip("ID is locked after creation")
        self._err_id = QLabel()
        self._err_id.setObjectName("error_id")
        self._err_id.setAccessibleName("ID error label")
        self._err_id.setStyleSheet("color: red;")
        self._err_id.setVisible(False)
        id_form.addRow("ID *", self._edit_id)
        id_form.addRow("", self._err_id)

        root.addWidget(grp_identity)

        # ---- ConnectionForm ----
        self._form = ConnectionForm(
            self,
            available_key_ids=available_key_ids,
            available_template_ids=available_template_ids,
        )
        self._form.populate_connections(available_connection_ids)
        self._form.setObjectName("connection_form_embed")
        root.addWidget(self._form, stretch=1)

        # ---- Test Connection row ----
        test_row = QHBoxLayout()

        self._btn_test = QPushButton("Test Connection")
        self._btn_test.setObjectName("btn_test_connection")
        self._btn_test.setAccessibleName("Test connection button")
        self._btn_test.setAccessibleDescription(
            "Test the connection using the current profile settings"
        )
        self._btn_test.clicked.connect(self._on_test_connection)
        test_row.addWidget(self._btn_test)

        self._lbl_test_result = QLabel()
        self._lbl_test_result.setObjectName("lbl_test_result")
        self._lbl_test_result.setAccessibleName("Test result label")
        self._lbl_test_result.setAccessibleDescription("Shows the result of the connection test")
        self._lbl_test_result.setWordWrap(True)
        test_row.addWidget(self._lbl_test_result, stretch=1)
        root.addLayout(test_row)

        # Preview area for custom profile test
        self._txt_test_preview = QPlainTextEdit()
        self._txt_test_preview.setObjectName("txt_test_preview")
        self._txt_test_preview.setAccessibleName("Test preview text area")
        self._txt_test_preview.setAccessibleDescription(
            "Shows rendered template preview for custom profile"
        )
        self._txt_test_preview.setReadOnly(True)
        font = self._txt_test_preview.font()
        font.setFamily("Monospace")
        self._txt_test_preview.setFont(font)
        self._txt_test_preview.setMaximumHeight(120)
        self._txt_test_preview.setVisible(False)
        root.addWidget(self._txt_test_preview)

        # ---- Member of line ----
        member_row = QHBoxLayout()
        lbl_member_title = QLabel("Member of:")
        lbl_member_title.setObjectName("lbl_member_of_title")
        lbl_member_title.setAccessibleName("Member of title label")
        member_row.addWidget(lbl_member_title)

        self._lbl_member_of = QLabel()
        self._lbl_member_of.setObjectName("lbl_member_of")
        self._lbl_member_of.setAccessibleName("Member of groups label")
        self._lbl_member_of.setAccessibleDescription(
            "Groups that contain this connection; click to open the group editor"
        )
        self._lbl_member_of.setTextFormat(Qt.TextFormat.RichText)
        self._lbl_member_of.setOpenExternalLinks(False)
        self._lbl_member_of.linkActivated.connect(self._on_group_link_clicked)
        member_row.addWidget(self._lbl_member_of, stretch=1)
        root.addLayout(member_row)

        # ---- Bottom buttons ----
        self._btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self._btn_box.setObjectName("btn_box")

        self._btn_save = self._btn_box.button(QDialogButtonBox.StandardButton.Save)
        assert self._btn_save is not None
        self._btn_save.setObjectName("btn_save")
        self._btn_save.setAccessibleName("Save button")
        self._btn_save.setAccessibleDescription("Save the connection and close the dialog")

        btn_cancel = self._btn_box.button(QDialogButtonBox.StandardButton.Cancel)
        assert btn_cancel is not None
        btn_cancel.setObjectName("btn_cancel")
        btn_cancel.setAccessibleName("Cancel button")
        btn_cancel.setAccessibleDescription("Discard changes and close the dialog")

        self._btn_box.accepted.connect(self._on_save)
        self._btn_box.rejected.connect(self.reject)
        root.addWidget(self._btn_box)

        # Wire signals
        self._edit_name.textChanged.connect(self._on_name_changed)
        self._edit_name.editingFinished.connect(self._validate_name)
        self._edit_id.editingFinished.connect(self._validate_id)
        self._form.validation_changed.connect(self._on_form_validation_changed)
        self._form.profile_changed.connect(self._on_profile_changed)

    # ------------------------------------------------------------------
    # Population
    # ------------------------------------------------------------------

    def _populate(self, data: dict[str, Any]) -> None:
        """Fill in identity fields and the embedded form."""
        self._edit_name.setText(str(data.get("name", "") or ""))
        self._edit_id.setText(str(data.get("id", "") or ""))
        self._form.load_data(data)
        self._validate_name()
        self._validate_id()

    def _update_id_from_name(self, name: str) -> None:
        """Auto-suggest an ID slug from name (only when editing a new connection)."""
        if not self._is_new or self._edit_id.isReadOnly():
            return
        slug = name.lower()
        slug = re.sub(r"[^a-z0-9]+", "-", slug)
        slug = slug.strip("-")
        if slug and len(slug) >= 2:
            # Truncate to 63 chars
            self._edit_id.setText(slug[:63])
        else:
            self._edit_id.setText("")

    def _update_member_of_label(self) -> None:
        """Update the Member of label based on current connection ID."""
        conn_id = self._edit_id.text().strip()
        member_groups = [g for g in self._groups if conn_id in g.get("members", [])]
        if member_groups:
            links = []
            for g in member_groups:
                gid = g.get("id", "")
                gname = g.get("name", gid)
                links.append(f'<a href="{gid}">{gname}</a>')
            self._lbl_member_of.setText(", ".join(links))
        else:
            self._lbl_member_of.setText("(none)")

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_name(self) -> None:
        name = self._edit_name.text().strip()
        if not name:
            self._set_widget_error(self._edit_name, self._err_name, "Name is required")
        else:
            self._set_widget_error(self._edit_name, self._err_name, "")
        self._update_save_button()

    def _validate_id(self) -> None:
        conn_id = self._edit_id.text().strip()
        if not conn_id:
            self._set_widget_error(self._edit_id, self._err_id, "ID is required")
        elif not _ID_SLUG_RE.match(conn_id):
            self._set_widget_error(
                self._edit_id,
                self._err_id,
                "ID must match ^[a-z0-9][a-z0-9-]{1,62}$",
            )
        else:
            self._set_widget_error(self._edit_id, self._err_id, "")
        self._update_save_button()

    def _set_widget_error(self, widget: QWidget, err_label: QLabel, error: str) -> None:
        if error:
            widget.setStyleSheet("border: 1px solid red;")
            err_label.setText(error)
            err_label.setVisible(True)
        else:
            widget.setStyleSheet("")
            err_label.setText("")
            err_label.setVisible(False)

    def _has_identity_errors(self) -> bool:
        return bool(self._err_name.text()) or bool(self._err_id.text())

    def _update_save_button(self) -> None:
        """Enable/disable Save and update tooltip."""
        form_valid = self._form.is_valid()
        identity_ok = not self._has_identity_errors()
        name_ok = bool(self._edit_name.text().strip())
        id_ok = bool(self._edit_id.text().strip())
        can_save = form_valid and identity_ok and name_ok and id_ok

        self._btn_save.setEnabled(can_save)

        if not can_save:
            issues: list[str] = []
            if not name_ok or self._err_name.text():
                issues.append(f"Name: {self._err_name.text() or 'required'}")
            if not id_ok or self._err_id.text():
                issues.append(f"ID: {self._err_id.text() or 'required'}")
            for field, err in self._form.errors().items():
                issues.append(f"{field}: {err}")
            self._btn_save.setToolTip("Outstanding issues:\n" + "\n".join(issues))
        else:
            self._btn_save.setToolTip("")

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    @Slot(str)
    def _on_name_changed(self, text: str) -> None:
        self._update_id_from_name(text)
        self._validate_name()

    @Slot(bool)
    def _on_form_validation_changed(self, valid: bool) -> None:
        self._update_save_button()

    @Slot(str)
    def _on_profile_changed(self, profile: str) -> None:
        # Show/hide preview area based on profile
        self._txt_test_preview.setVisible(False)
        self._lbl_test_result.setText("")

    @Slot(str)
    def _on_group_link_clicked(self, group_id: str) -> None:
        self.open_group_editor.emit(group_id)

    @Slot()
    def _on_save(self) -> None:
        self._validate_name()
        self._validate_id()
        if self._has_identity_errors():
            return
        if not self._form.is_valid():
            return
        self.accept()

    @Slot()
    def _on_test_connection(self) -> None:
        """Run the appropriate test for the current profile."""
        profile = self._form.current_profile
        data = self._form.collect_data()

        self._lbl_test_result.setText("Testing…")
        self._txt_test_preview.setVisible(False)

        if profile in {"claude-remote", "ssh-shell"}:
            self._run_ssh_test(data)
        elif profile in {"claude-local", "local-shell"}:
            self._run_local_test(data)
        elif profile == "custom":
            self._run_custom_preview(data)

    def _run_ssh_test(self, data: dict[str, Any]) -> None:
        """Test SSH connectivity using SshTestConnectionTask."""
        host = data.get("host") or ""
        user = data.get("user") or ""
        port = int(data.get("port") or 22)
        identity_ref = data.get("identity_file_ref") or ""

        if not host or not user:
            self._lbl_test_result.setText("⚠  Host and User are required for SSH test")
            return

        if self._ssh_test_factory is not None:
            # Injected factory for testing
            self._ssh_test_factory(
                host=host,
                user=user,
                port=port,
                identity_file=Path(identity_ref) if identity_ref else None,
                on_result=self._on_ssh_test_result,
            )
            return

        try:
            from PySide6.QtCore import QThreadPool

            from cpsm.platform.ssh_binary import SshBinary
            from cpsm.workers.ssh_worker import SshTestConnectionTask

            ssh_bin = SshBinary.detect()
            task = SshTestConnectionTask(
                ssh_binary=ssh_bin,
                host=host,
                user=user,
                port=port,
                identity_file=Path(identity_ref).expanduser() if identity_ref else None,
            )
            task.signals.finished.connect(
                self._on_ssh_test_result, Qt.ConnectionType.QueuedConnection
            )
            QThreadPool.globalInstance().start(task)
        except Exception as exc:
            self._lbl_test_result.setText(f"✗  Error: {exc}")

    @Slot(bool, str)
    def _on_ssh_test_result(self, success: bool, message: str) -> None:
        try:
            if not self.isVisible() or self._closed:
                return
            if success:
                self._lbl_test_result.setStyleSheet("color: green;")
                self._lbl_test_result.setText("✓  Connection successful")
            else:
                self._lbl_test_result.setStyleSheet("color: red;")
                self._lbl_test_result.setText(f"✗  {message}")
        except RuntimeError:
            # C++ widget already deleted
            pass

    def closeEvent(self, event: Any) -> None:
        """Mark dialog as closed to guard async slot callbacks."""
        self._closed = True
        super().closeEvent(event)

    def _run_local_test(self, data: dict[str, Any]) -> None:
        """Test that project_folder exists locally."""
        folder = data.get("project_folder") or ""
        if not folder:
            self._lbl_test_result.setStyleSheet("color: orange;")
            self._lbl_test_result.setText("⚠  No project folder specified")
            return
        path = Path(folder).expanduser()
        if path.is_dir():
            self._lbl_test_result.setStyleSheet("color: green;")
            self._lbl_test_result.setText(f"✓  Directory exists: {path}")
        else:
            self._lbl_test_result.setStyleSheet("color: red;")
            self._lbl_test_result.setText(f"✗  Directory not found: {path}")

    def _run_custom_preview(self, data: dict[str, Any]) -> None:
        """Render the template and show it in the preview area."""
        template_id = data.get("custom_template_id") or ""
        if not template_id:
            self._lbl_test_result.setStyleSheet("color: orange;")
            self._lbl_test_result.setText("⚠  No template selected")
            return

        if self._template_service is not None:
            try:
                rendered = self._template_service.render(data)
                self._txt_test_preview.setPlainText(rendered)
                self._txt_test_preview.setVisible(True)
                self._lbl_test_result.setStyleSheet("color: green;")
                self._lbl_test_result.setText("✓  Template rendered (preview above)")
            except Exception as exc:
                self._lbl_test_result.setStyleSheet("color: red;")
                self._lbl_test_result.setText(f"✗  Render error: {exc}")
            return

        try:
            from cpsm.services.template_service import TemplateService

            svc = TemplateService()

            # Build a minimal connection-like object
            class _FakeConn:
                def __init__(self, d: dict[str, Any]) -> None:
                    for k, v in d.items():
                        setattr(self, k, v)
                    self.launch_profile = d.get("launch_profile", "custom")

            rendered = svc.render(
                profile=data.get("launch_profile", "custom"),
                connection=_FakeConn(data),
            )
            self._txt_test_preview.setPlainText(rendered)
            self._txt_test_preview.setVisible(True)
            self._lbl_test_result.setStyleSheet("color: green;")
            self._lbl_test_result.setText("✓  Template rendered (preview above)")
        except Exception as exc:
            self._lbl_test_result.setStyleSheet("color: red;")
            self._lbl_test_result.setText(f"✗  Render error: {exc}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_connection_data(self) -> dict[str, Any]:
        """Return the collected connection data including name and id."""
        data = self._form.collect_data()
        data["name"] = self._edit_name.text().strip() or None
        data["id"] = self._edit_id.text().strip()
        return data

    @property
    def connection_name(self) -> str:
        return self._edit_name.text().strip()

    @property
    def connection_id(self) -> str:
        return self._edit_id.text().strip()
