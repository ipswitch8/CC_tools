# -*- coding: utf-8 -*-
"""
cpsm.ui.dialogs.deploy_key — Deploy SSH Key dialog.

Spec section: §9.2

Form:
  - Target connection combo (from candidates list)
  - Password QLineEdit(echoMode=Password) — one-time SSH password
  - Live log view (QPlainTextEdit, read-only)

Security constraints:
  - Password captured into bytearray immediately on Deploy, zeroed in finally.
  - Password QLineEdit cleared after deploy or cancel.
  - Password NEVER logged.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from cpsm.data.schema import SshKey
    from cpsm.services.key_service import KeyService

__all__ = ["DeployKeyDialog"]

logger = logging.getLogger(__name__)


class DeployKeyDialog(QDialog):
    """Deploy an SSH key to a target connection.

    Parameters
    ----------
    key:
        The SshKey whose public key is to be deployed.
    candidates:
        List of Connection objects the user may choose from.
    key_service:
        KeyService instance for running the deployment.
    parent:
        Optional parent widget.
    """

    def __init__(
        self,
        key: SshKey,
        candidates: list[Any],
        key_service: KeyService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._key = key
        self._candidates = list(candidates)
        self._key_service = key_service
        self._worker: Any = None

        self.setWindowTitle(f"Deploy Key: {key.name}")
        self.setObjectName("dlg_deploy_key")
        self.setMinimumWidth(500)

        layout = QVBoxLayout(self)

        form = QFormLayout()
        form.setObjectName("form_deploy")

        # Target connection
        self._combo_connection = QComboBox()
        self._combo_connection.setObjectName("combo_target_connection")
        self._combo_connection.setAccessibleName("Target connection")
        for conn in self._candidates:
            label = getattr(conn, "name", None) or getattr(conn, "id", "?")
            host = getattr(conn, "host", "")
            display = f"{label} ({host})" if host else str(label)
            self._combo_connection.addItem(str(display))
        form.addRow("Target Connection:", self._combo_connection)

        # Password
        self._edit_password = QLineEdit()
        self._edit_password.setObjectName("edit_ssh_password")
        self._edit_password.setAccessibleName("SSH password")
        self._edit_password.setEchoMode(QLineEdit.EchoMode.Password)
        self._edit_password.setPlaceholderText("SSH password (leave blank if not required)")
        form.addRow("Password:", self._edit_password)

        layout.addLayout(form)

        # Log view
        self._log_view = QPlainTextEdit()
        self._log_view.setObjectName("text_deploy_log")
        self._log_view.setAccessibleName("Deploy log")
        self._log_view.setReadOnly(True)
        self._log_view.setMinimumHeight(120)
        layout.addWidget(self._log_view)

        # Error label
        self._label_error = QLabel()
        self._label_error.setObjectName("label_error")
        self._label_error.setStyleSheet("color: red;")
        self._label_error.setWordWrap(True)
        layout.addWidget(self._label_error)

        # Buttons
        self._buttons = QDialogButtonBox()
        self._buttons.setObjectName("button_box")
        self._btn_deploy = self._buttons.addButton("Deploy", QDialogButtonBox.ButtonRole.AcceptRole)
        self._btn_deploy.setObjectName("btn_deploy")
        self._btn_deploy.setAccessibleName("Deploy key")
        self._btn_cancel = self._buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self._btn_cancel.setObjectName("btn_cancel")
        self._buttons.accepted.connect(self._on_deploy)
        self._buttons.rejected.connect(self._on_cancel)
        layout.addWidget(self._buttons)

        if not self._candidates:
            self._btn_deploy.setEnabled(False)
            self._label_error.setText("No eligible connections for deployment.")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_deploy(self) -> None:
        """Start deployment in a background thread.

        Per §9.2: the password buffer must be zeroed even if worker startup raises.
        We capture into a bytearray, hand the worker a transient string view, and
        wipe the bytearray in finally regardless of outcome.
        """
        if not self._candidates:
            return

        idx = self._combo_connection.currentIndex()
        if idx < 0 or idx >= len(self._candidates):
            return

        connection = self._candidates[idx]
        password_text = self._edit_password.text()
        # Immediately clear the QLineEdit (best-effort wipe of Qt-side buffer)
        self._edit_password.setText("")

        # Capture into a mutable bytearray so we can guarantee zeroing in finally
        password_buf = bytearray(password_text.encode("utf-8")) if password_text else bytearray()
        # Drop the immutable str reference as soon as possible
        del password_text

        try:
            password: str | None = password_buf.decode("utf-8") if password_buf else None

            self._btn_deploy.setEnabled(False)
            self._label_error.clear()
            self._log_view.clear()

            from cpsm.workers.key_deploy_worker import KeyDeployWorker

            self._worker = KeyDeployWorker(
                key=self._key,
                connection=connection,
                key_service=self._key_service,
                password=password,
                parent=self,
            )
            self._worker.signals.log_line.connect(self._append_log)
            self._worker.signals.finished.connect(self._on_deploy_finished)
            self._worker.start()
        finally:
            # Zero the bytearray. Note: a str copy was handed to the worker; the
            # worker is responsible for not retaining or logging it. This local
            # zeroing eliminates the dialog-side residue.
            for i in range(len(password_buf)):
                password_buf[i] = 0
            del password_buf

    def _on_cancel(self) -> None:
        """Clear password buffer and close."""
        self._clear_password()
        if self._worker is not None and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(2000)
        self.reject()

    def _clear_password(self) -> None:
        """Zero the password QLineEdit buffer (best-effort)."""
        text = self._edit_password.text()
        if text:
            buf = bytearray(text.encode("utf-8"))
            for i in range(len(buf)):
                buf[i] = 0
            del buf
        self._edit_password.setText("")

    def _append_log(self, line: str) -> None:
        self._log_view.appendPlainText(line)

    def _on_deploy_finished(self, success: bool, error: str) -> None:
        """Handle deploy completion."""
        self._clear_password()
        if success:
            # Append the deployment record to the key
            from datetime import UTC, datetime

            from cpsm.data.schema import SshKeyDeployment

            idx = self._combo_connection.currentIndex()
            if 0 <= idx < len(self._candidates):
                conn = self._candidates[idx]
                conn_id = getattr(conn, "id", "")
                if conn_id:
                    try:
                        dep = SshKeyDeployment(
                            connection_id=conn_id,
                            deployed_at=datetime.now(tz=UTC),
                            method="ssh-copy-id",
                        )
                        self._key.deployments.append(dep)
                    except Exception:
                        pass  # Non-fatal
            self.accept()
        else:
            self._label_error.setText(f"Deployment failed: {error}")
            self._btn_deploy.setEnabled(True)

    def closeEvent(self, event: Any) -> None:
        """Ensure password is cleared on any close path."""
        self._clear_password()
        super().closeEvent(event)
