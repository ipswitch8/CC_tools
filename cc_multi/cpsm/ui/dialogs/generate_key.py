# -*- coding: utf-8 -*-
"""
cpsm.ui.dialogs.generate_key — Generate SSH Key dialog.

Spec section: §9.2

Form:
  - Type radio: ed25519 (default, recommended), rsa (4096)
  - Filename QLineEdit (default id_ed25519)
  - Comment QLineEdit
  - "Use passphrase (stored in OS keychain)" checkbox
  - Passphrase + Confirm passphrase QLineEdit(echoMode=Password)

Security constraints:
  - Passphrase NEVER echoed or logged.
  - After Generate: passphrase text captured into bytearray, zeroed, then
    QLineEdit.setText("") to wipe the Qt-side buffer.
  - KeyService stores passphrase in keyring under cpsm/<key_id>.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from cpsm.data.schema import SshKey
    from cpsm.services.key_service import KeyService

__all__ = ["GenerateKeyDialog"]

logger = logging.getLogger(__name__)

_TYPE_OPTIONS = ["ed25519", "rsa"]


class GenerateKeyDialog(QDialog):
    """Create a new ed25519 (or rsa) SSH key pair with optional passphrase.

    Parameters
    ----------
    key_service:
        KeyService instance used to generate the key.
    default_dir:
        Default directory for key file output.
    parent:
        Optional parent widget.
    """

    def __init__(
        self,
        key_service: KeyService,
        default_dir: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._key_service = key_service
        self._default_dir = default_dir
        self._created_key: SshKey | None = None

        self.setWindowTitle("Generate SSH Key")
        self.setObjectName("dlg_generate_key")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)

        # ----- Key type -----
        type_box = QGroupBox("Key Type")
        type_box.setObjectName("group_key_type")
        type_layout = QHBoxLayout(type_box)
        self._type_group = QButtonGroup(self)
        self._type_radios: dict[str, QRadioButton] = {}
        for opt in _TYPE_OPTIONS:
            label = f"{opt} (recommended)" if opt == "ed25519" else opt
            rb = QRadioButton(label)
            rb.setObjectName(f"radio_type_{opt}")
            rb.setAccessibleName(f"Key type {opt}")
            self._type_radios[opt] = rb
            self._type_group.addButton(rb)
            type_layout.addWidget(rb)
        self._type_radios["ed25519"].setChecked(True)
        layout.addWidget(type_box)

        # ----- File / comment -----
        file_box = QGroupBox("Key File")
        file_box.setObjectName("group_key_file")
        file_form = QFormLayout(file_box)

        self._edit_filename = QLineEdit("id_ed25519")
        self._edit_filename.setObjectName("edit_filename")
        self._edit_filename.setAccessibleName("Key filename")
        file_form.addRow("Filename:", self._edit_filename)

        self._label_path = QLabel()
        self._label_path.setObjectName("label_resolved_path")
        file_form.addRow("Path:", self._label_path)
        self._edit_filename.textChanged.connect(self._update_path_label)

        self._edit_comment = QLineEdit("cpsm-generated")
        self._edit_comment.setObjectName("edit_comment")
        self._edit_comment.setAccessibleName("Key comment")
        file_form.addRow("Comment:", self._edit_comment)

        layout.addWidget(file_box)

        # ----- Passphrase -----
        pp_box = QGroupBox("Passphrase")
        pp_box.setObjectName("group_passphrase")
        pp_form = QFormLayout(pp_box)

        self._chk_use_passphrase = QCheckBox("Use passphrase (stored in OS keychain)")
        self._chk_use_passphrase.setObjectName("chk_use_passphrase")
        self._chk_use_passphrase.setAccessibleName("Use passphrase")
        pp_form.addRow(self._chk_use_passphrase)

        self._edit_passphrase = QLineEdit()
        self._edit_passphrase.setObjectName("edit_passphrase")
        self._edit_passphrase.setAccessibleName("Passphrase")
        self._edit_passphrase.setEchoMode(QLineEdit.EchoMode.Password)
        self._edit_passphrase.setPlaceholderText("Passphrase")
        pp_form.addRow("Passphrase:", self._edit_passphrase)

        self._edit_confirm = QLineEdit()
        self._edit_confirm.setObjectName("edit_confirm_passphrase")
        self._edit_confirm.setAccessibleName("Confirm passphrase")
        self._edit_confirm.setEchoMode(QLineEdit.EchoMode.Password)
        self._edit_confirm.setPlaceholderText("Confirm passphrase")
        pp_form.addRow("Confirm:", self._edit_confirm)

        self._label_pp_error = QLabel()
        self._label_pp_error.setObjectName("label_passphrase_error")
        self._label_pp_error.setStyleSheet("color: red;")
        pp_form.addRow(self._label_pp_error)

        layout.addWidget(pp_box)

        # Wire passphrase checkbox
        self._chk_use_passphrase.toggled.connect(self._on_passphrase_toggled)
        self._edit_passphrase.textChanged.connect(self._validate_passphrase)
        self._edit_confirm.textChanged.connect(self._validate_passphrase)
        self._on_passphrase_toggled(False)

        # ----- Error label -----
        self._label_error = QLabel()
        self._label_error.setObjectName("label_error")
        self._label_error.setStyleSheet("color: red;")
        self._label_error.setWordWrap(True)
        layout.addWidget(self._label_error)

        # ----- Buttons -----
        self._buttons = QDialogButtonBox()
        self._buttons.setObjectName("button_box")
        self._btn_generate = self._buttons.addButton(
            "Generate", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self._btn_generate.setObjectName("btn_generate")
        self._btn_generate.setAccessibleName("Generate key")
        self._buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self._buttons.accepted.connect(self._on_generate)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        # Initial path label + validation (all widgets now created)
        self._update_path_label(self._edit_filename.text())
        self._validate_form()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def created_key(self) -> SshKey | None:
        """The SshKey created by the dialog, or None if cancelled."""
        return self._created_key

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _current_type(self) -> str:
        return next(
            (k for k, rb in self._type_radios.items() if rb.isChecked()),
            "ed25519",
        )

    def _resolved_path(self) -> Path:
        filename = self._edit_filename.text().strip()
        return self._default_dir / filename if filename else self._default_dir / "id_ed25519"

    def _update_path_label(self, _text: str = "") -> None:
        self._label_path.setText(str(self._resolved_path()))
        self._validate_form()

    def _on_passphrase_toggled(self, checked: bool) -> None:
        self._edit_passphrase.setVisible(checked)
        self._edit_confirm.setVisible(checked)
        self._label_pp_error.setVisible(checked)
        if not checked:
            self._label_pp_error.clear()
        self._validate_form()

    def _validate_passphrase(self, _text: str = "") -> None:
        if not self._chk_use_passphrase.isChecked():
            self._label_pp_error.clear()
            return
        p1 = self._edit_passphrase.text()
        p2 = self._edit_confirm.text()
        if p1 != p2:
            self._label_pp_error.setText("Passphrases do not match.")
        else:
            self._label_pp_error.clear()
        self._validate_form()

    def _validate_form(self) -> None:
        """Enable/disable Generate based on current form state."""
        # Guard: may be called before all widgets are initialised
        if not hasattr(self, "_btn_generate") or not hasattr(self, "_chk_use_passphrase"):
            return
        ok = True
        filename = self._edit_filename.text().strip()
        if not filename:
            ok = False
        if (
            self._chk_use_passphrase.isChecked()
            and self._edit_passphrase.text() != self._edit_confirm.text()
        ):
            ok = False
        self._btn_generate.setEnabled(ok)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_generate(self) -> None:
        """Validate then call KeyService.generate_ed25519."""
        filename = self._edit_filename.text().strip()
        if not filename:
            self._label_error.setText("Filename must not be empty.")
            return

        private_path = self._resolved_path()
        if private_path.exists():
            self._label_error.setText(
                f"File already exists: {private_path}\nChoose a different filename."
            )
            return

        comment = self._edit_comment.text().strip()
        # Use filename (without extension) as key_id, sanitised
        key_id_raw = private_path.stem.replace("_", "-").replace(".", "-")
        # Must match slug regex; strip leading non-alnum
        import re

        key_id = re.sub(r"[^a-z0-9-]", "", key_id_raw.lower())
        if not key_id or not re.match(r"^[a-z0-9]", key_id):
            key_id = "cpsm-key-" + key_id.lstrip("-") if key_id else "cpsm-key"

        passphrase: str | None = None
        if self._chk_use_passphrase.isChecked():
            p1 = self._edit_passphrase.text()
            p2 = self._edit_confirm.text()
            if p1 != p2:
                self._label_error.setText("Passphrases do not match.")
                return
            passphrase = p1

        key_type = self._current_type()
        # Only ed25519 is fully implemented in KeyService Phase 7
        if key_type != "ed25519":
            self._label_error.setText(
                "RSA generation is not yet implemented. Please choose ed25519."
            )
            return

        # Capture passphrase into bytearray immediately, then clear widgets
        # (best-effort in-memory zeroing as per spec §9.2)
        pp_buf: bytearray | None = None
        if passphrase is not None:
            pp_buf = bytearray(passphrase.encode("utf-8"))

        self._label_error.clear()
        try:
            from cpsm.services.key_service import KeyExistsError

            new_key = self._key_service.generate_ed25519(
                key_id=key_id,
                private_path=private_path,
                comment=comment,
                passphrase=passphrase,
            )
            self._created_key = new_key
        except KeyExistsError as exc:
            self._label_error.setText(str(exc))
            return
        except Exception as exc:
            self._label_error.setText(f"Key generation failed: {exc}")
            return
        finally:
            # Zero the bytearray and clear QLineEdit buffers (best-effort)
            if pp_buf is not None:
                for i in range(len(pp_buf)):
                    pp_buf[i] = 0
                del pp_buf
            self._edit_passphrase.setText("")
            self._edit_confirm.setText("")
            # Release local reference to passphrase string
            passphrase = None

        self.accept()
