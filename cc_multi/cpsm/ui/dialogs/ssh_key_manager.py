# -*- coding: utf-8 -*-
"""
cpsm.ui.dialogs.ssh_key_manager — SSH Key Manager dialog.

Spec sections: §4.8, §9.2

Layout:
  - QTableWidget (objectName "table_keys"): columns
      Name | Type | Private Path | Deployments | Actions
  - For each key row:
      - "Deploy…" button → opens DeployKeyDialog
      - "Delete" button  → removes from doc.ssh_keys[]
  - "Generate New Key…" button → opens GenerateKeyDialog; on accept appends key.

Security:
  - Never reads or displays private key *content*.
  - Refuses to display a key row normally when private_path perms > 0600 on Linux.
    Instead shows a warning row: "⚠ permissions too broad — fix".
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from cpsm.data.schema import CpsmDocument, SshKey
    from cpsm.services.key_service import KeyService

# Imported at module level so they can be patched in tests.
from cpsm.ui.dialogs.deploy_key import DeployKeyDialog
from cpsm.ui.dialogs.generate_key import GenerateKeyDialog

__all__ = ["SshKeyManagerDialog"]

logger = logging.getLogger(__name__)

_COL_NAME = 0
_COL_TYPE = 1
_COL_PATH = 2
_COL_DEPS = 3
_COL_ACTIONS = 4
_NUM_COLS = 5
_HEADERS = ["Name", "Type", "Private Path", "Deployments", "Actions"]


class SshKeyManagerDialog(QDialog):
    """List/generate/deploy SSH keys.

    Parameters
    ----------
    doc:
        The CpsmDocument whose ssh_keys list is managed.
    key_service:
        KeyService for generate/deploy operations.
    parent:
        Optional parent widget.
    """

    def __init__(
        self,
        doc: CpsmDocument,
        key_service: KeyService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._doc = doc
        self._key_service = key_service

        self.setWindowTitle("SSH Key Manager")
        self.setObjectName("dlg_ssh_key_manager")
        self.setMinimumWidth(700)
        self.setMinimumHeight(380)

        layout = QVBoxLayout(self)

        # Table
        self._table = QTableWidget()
        self._table.setObjectName("table_keys")
        self._table.setAccessibleName("SSH Keys table")
        self._table.setColumnCount(_NUM_COLS)
        self._table.setHorizontalHeaderLabels(_HEADERS)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.horizontalHeader().setStretchLastSection(False)
        layout.addWidget(self._table)

        # Bottom bar
        bar = QHBoxLayout()
        self._btn_generate = QPushButton("Generate New Key…")
        self._btn_generate.setObjectName("btn_generate_new_key")
        self._btn_generate.setAccessibleName("Generate new key")
        self._btn_generate.clicked.connect(self._on_generate)
        bar.addWidget(self._btn_generate)
        bar.addStretch()

        close_btn = QPushButton("Close")
        close_btn.setObjectName("btn_close")
        close_btn.clicked.connect(self.accept)
        bar.addWidget(close_btn)
        layout.addLayout(bar)

        self._populate_table()

    # ------------------------------------------------------------------
    # Table population
    # ------------------------------------------------------------------

    def _populate_table(self) -> None:
        """Rebuild the table rows from doc.ssh_keys plus any pre-existing
        system keys found at ~/.ssh/id_* (Round-late tweak — surface
        keys connections may already be using via system-level SSH so the
        user can register them here for deploy/delete tracking).
        """
        self._table.setRowCount(0)
        keys: list[SshKey] = list(self._doc.ssh_keys)
        # Discover system keys at ~/.ssh/id_* and surface any that aren't
        # already tracked in doc.ssh_keys (compared by absolute path).
        system_keys = self._discover_system_keys(keys)
        total_rows = len(keys) + len(system_keys)
        self._table.setRowCount(total_rows)

        for row, key in enumerate(keys):
            perms_ok = self._check_permissions(key)

            if not perms_ok:
                # Warning row — spans all data columns
                self._table.setItem(row, _COL_NAME, QTableWidgetItem(key.name or key.id))
                warn_item = QTableWidgetItem("⚠ permissions too broad — fix")
                warn_item.setForeground(Qt.GlobalColor.red)
                warn_item.setToolTip(
                    f"Private key file permissions exceed 0600: {key.private_path}"
                )
                self._table.setItem(row, _COL_TYPE, warn_item)
                self._table.setItem(row, _COL_PATH, QTableWidgetItem(key.private_path))
                self._table.setItem(row, _COL_DEPS, QTableWidgetItem(""))
                # No action buttons for bad-permissions row
                self._table.setCellWidget(row, _COL_ACTIONS, QLabel(""))
            else:
                self._table.setItem(row, _COL_NAME, QTableWidgetItem(key.name or key.id))
                self._table.setItem(row, _COL_TYPE, QTableWidgetItem(key.type))
                self._table.setItem(row, _COL_PATH, QTableWidgetItem(key.private_path))
                dep_count = len(key.deployments)
                self._table.setItem(row, _COL_DEPS, QTableWidgetItem(str(dep_count)))

                # Action buttons
                actions_widget = QWidget()
                actions_widget.setObjectName(f"actions_row_{row}")
                actions_layout = QHBoxLayout(actions_widget)
                actions_layout.setContentsMargins(2, 2, 2, 2)

                deploy_btn = QPushButton("Deploy…")
                deploy_btn.setObjectName(f"btn_deploy_{key.id}")
                deploy_btn.setAccessibleName(f"Deploy key {key.name or key.id}")
                deploy_btn.setProperty("key_id", key.id)
                deploy_btn.clicked.connect(lambda checked=False, k=key: self._on_deploy(k))
                actions_layout.addWidget(deploy_btn)

                delete_btn = QPushButton("Delete")
                delete_btn.setObjectName(f"btn_delete_{key.id}")
                delete_btn.setAccessibleName(f"Delete key {key.name or key.id}")
                delete_btn.setProperty("key_id", key.id)
                delete_btn.clicked.connect(lambda checked=False, k=key: self._on_delete(k))
                actions_layout.addWidget(delete_btn)

                self._table.setCellWidget(row, _COL_ACTIONS, actions_widget)

        # Append discovered-system-key rows after the registered keys.
        for off, sk in enumerate(system_keys):
            row = len(keys) + off
            name_item = QTableWidgetItem(f"(system) {sk['name']}")
            name_item.setToolTip(
                "Discovered at " + sk["private_path"] + " — not yet tracked by CPSM."
            )
            self._table.setItem(row, _COL_NAME, name_item)
            self._table.setItem(row, _COL_TYPE, QTableWidgetItem(sk["type"]))
            self._table.setItem(row, _COL_PATH, QTableWidgetItem(sk["private_path"]))
            self._table.setItem(row, _COL_DEPS, QTableWidgetItem(""))
            register_btn = QPushButton("Register…")
            register_btn.setObjectName(f"btn_register_{sk['name']}")
            register_btn.setAccessibleName(
                f"Register system key {sk['name']} into the CPSM document"
            )
            register_btn.setToolTip(
                "Add this system key to ssh_keys[] in the document so it "
                "can be referenced by connections and deployed via CPSM."
            )
            register_btn.clicked.connect(
                lambda _checked=False, sk=sk: self._on_register_system_key(sk)
            )
            self._table.setCellWidget(row, _COL_ACTIONS, register_btn)

        self._table.resizeColumnsToContents()

    @staticmethod
    def _discover_system_keys(
        existing: list[SshKey],
    ) -> list[dict[str, str]]:
        """Scan ~/.ssh/ for id_* private keys with matching .pub files,
        excluding paths already tracked in *existing*. Returns a list of
        dicts with keys: name, type, private_path, public_path.
        """
        out: list[dict[str, str]] = []
        ssh_dir = Path("~/.ssh").expanduser()
        if not ssh_dir.is_dir():
            return out
        existing_paths: set[str] = set()
        for k in existing:
            try:
                existing_paths.add(str(Path(k.private_path).expanduser().resolve()))
            except OSError:
                existing_paths.add(k.private_path)
        type_map = {
            "id_ed25519": "ed25519",
            "id_rsa": "rsa",
            "id_ecdsa": "ecdsa",
            "id_dsa": "dsa",
        }
        try:
            entries = sorted(ssh_dir.iterdir())
        except OSError:
            return out
        for p in entries:
            if not p.is_file() or p.suffix == ".pub":
                continue
            if not p.name.startswith("id_"):
                continue
            pub = p.with_suffix(".pub")
            if not pub.exists():
                continue
            try:
                resolved = str(p.resolve())
            except OSError:
                resolved = str(p)
            if resolved in existing_paths:
                continue
            key_type = type_map.get(p.name, p.name.removeprefix("id_"))
            out.append({
                "name": p.name,
                "type": key_type,
                "private_path": str(p),
                "public_path": str(pub),
            })
        return out

    def _on_register_system_key(self, info: dict[str, str]) -> None:
        """Register a discovered system key as a doc-tracked SshKey."""
        from cpsm.data.schema import SshKey as _SshKey
        # Build an id slug from the filename (id_ed25519 → id-ed25519).
        slug = info["name"].replace("_", "-").lower()
        # Avoid collision with existing ids
        existing_ids = {k.id for k in self._doc.ssh_keys}
        candidate = slug
        suffix = 1
        while candidate in existing_ids:
            suffix += 1
            candidate = f"{slug}-{suffix}"
        new_key = _SshKey(
            id=candidate,
            name=info["name"],
            type=info["type"] if info["type"] in ("ed25519", "rsa", "ecdsa") else "ed25519",
            private_path=info["private_path"],
            public_path=info["public_path"],
        )
        self._doc.ssh_keys.append(new_key)
        self._populate_table()

    @staticmethod
    def _check_permissions(key: SshKey) -> bool:
        """Return False if private key has broader than 0600 on Linux."""
        if sys.platform == "win32":
            return True
        private_path = Path(key.private_path).expanduser()
        if not private_path.exists():
            # File doesn't exist — not a permissions problem; let it through
            return True
        try:
            mode = os.stat(private_path).st_mode & 0o777
            return mode <= 0o600
        except OSError:
            return True  # Can't stat — not a permissions error

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_generate(self) -> None:
        """Open GenerateKeyDialog; append created key to doc on accept."""
        default_dir = Path("~/.ssh").expanduser()
        dlg = GenerateKeyDialog(
            key_service=self._key_service,
            default_dir=default_dir,
            parent=self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.created_key is not None:
            self._doc.ssh_keys.append(dlg.created_key)
            self._populate_table()

    def _on_deploy(self, key: SshKey) -> None:
        """Open DeployKeyDialog for the given key."""
        # Build candidate list: connections that have host+user
        candidates = [
            c
            for c in self._doc.connections
            if getattr(c, "host", None) and getattr(c, "user", None)
        ]
        dlg = DeployKeyDialog(
            key=key,
            candidates=candidates,
            key_service=self._key_service,
            parent=self,
        )
        dlg.exec()
        # Refresh table in case deployments were added
        self._populate_table()

    def _on_delete(self, key: SshKey) -> None:
        """Remove key from doc.ssh_keys and refresh table."""
        self._doc.ssh_keys = [k for k in self._doc.ssh_keys if k.id != key.id]
        self._populate_table()
