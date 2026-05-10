# -*- coding: utf-8 -*-
"""
cpsm.ui.dialogs.launch_conflict — LaunchConflictDialog.

Shown when the user triggers a launch (single connection or group) and
DiscoveryService finds at least one matching outside-CPSM session for the
target connection(s).  The user picks per-connection how to resolve each
conflict:

  Adopt          — close the existing outside session and launch under CPSM
                   with ``--continue`` appended (for Claude profiles).
  Launch new     — proceed with a normal launch even though a duplicate
                   process already exists.  The user knows this will
                   produce two simultaneous instances.
  Skip           — omit this connection from the launch entirely.

The dialog is purely a chooser — it does NOT execute the kill or the
launch.  Callers read ``dialog.actions`` (a dict mapping connection_id →
``"adopt" | "duplicate" | "skip"``) after ``exec()`` returns Accepted.
"""

from __future__ import annotations

from typing import Literal

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QLabel,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from cpsm.services.discovery_service import DiscoveredSession

__all__ = ["LaunchConflictDialog", "LaunchConflictAction"]

LaunchConflictAction = Literal["adopt", "duplicate", "skip"]


class LaunchConflictDialog(QDialog):
    """One-stop resolution UI for outside-session conflicts.

    Parameters
    ----------
    conflicts:
        List of ``(connection_id, connection_label, discovered_session)``
        tuples.  The label is the connection's display name (or id if no
        name set).
    parent:
        Optional Qt parent widget.

    Attributes
    ----------
    actions:
        Populated on Accept: maps each connection_id to the user's choice.
        Empty dict on Cancel.
    """

    def __init__(
        self,
        conflicts: list[tuple[str, str, DiscoveredSession]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("dialog_launch_conflict")
        self.setWindowTitle("Outside sessions detected")
        self.setModal(True)
        self.setMinimumWidth(560)

        self._conflicts = conflicts
        self.actions: dict[str, LaunchConflictAction] = {}
        # connection_id → QButtonGroup managing the row's three radios.
        self._row_groups: dict[str, QButtonGroup] = {}

        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)

        intro = QLabel(self._format_intro())
        intro.setObjectName("label_conflict_intro")
        intro.setWordWrap(True)
        root.addWidget(intro)

        # Scrollable area in case there are many conflicts (e.g. a 10-conn
        # group with several already running outside CPSM).
        scroll = QScrollArea(self)
        scroll.setObjectName("scroll_conflict_rows")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        rows_widget = QWidget(scroll)
        rows_widget.setObjectName("widget_conflict_rows")
        grid = QGridLayout(rows_widget)
        grid.setColumnStretch(0, 1)
        grid.setSpacing(6)

        # Header
        for col, text in enumerate(("Connection", "Adopt", "Launch new", "Skip")):
            header = QLabel(f"<b>{text}</b>")
            header.setObjectName(f"label_conflict_header_{col}")
            grid.addWidget(header, 0, col)

        for row_idx, (conn_id, label, sess) in enumerate(self._conflicts, start=1):
            self._build_row(grid, row_idx, conn_id, label, sess)

        scroll.setWidget(rows_widget)
        root.addWidget(scroll, 1)

        # Standard OK / Cancel; OK is "Continue" since "OK" is ambiguous here.
        buttons = QDialogButtonBox(self)
        buttons.setObjectName("buttons_conflict")
        ok_btn = buttons.addButton("Continue", QDialogButtonBox.ButtonRole.AcceptRole)
        ok_btn.setObjectName("btn_conflict_continue")
        cancel_btn = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        cancel_btn.setObjectName("btn_conflict_cancel")
        buttons.accepted.connect(self._on_accept)  # type: ignore[arg-type]
        buttons.rejected.connect(self.reject)  # type: ignore[arg-type]
        root.addWidget(buttons)

    def _format_intro(self) -> str:
        n = len(self._conflicts)
        if n == 1:
            return (
                "An outside-CPSM session was detected for the connection you're "
                "about to launch. Choose how to resolve it:"
            )
        return (
            f"{n} outside-CPSM sessions were detected for connections in this "
            f"launch. Choose how to resolve each:"
        )

    def _build_row(
        self,
        grid: QGridLayout,
        row_idx: int,
        conn_id: str,
        label: str,
        sess: DiscoveredSession,
    ) -> None:
        # Description label spans the first column. Tooltip carries the
        # full process detail; the label keeps the main layout compact.
        descr = self._format_row_descr(label, sess)
        descr_lbl = QLabel(descr)
        descr_lbl.setObjectName(f"label_conflict_row_{row_idx}")
        descr_lbl.setWordWrap(True)
        descr_lbl.setToolTip(self._format_row_tooltip(sess))
        grid.addWidget(descr_lbl, row_idx, 0)

        group = QButtonGroup(self)
        for col, key in enumerate(("adopt", "duplicate", "skip"), start=1):
            radio = QRadioButton(self)
            radio.setObjectName(f"radio_conflict_{conn_id}_{key}")
            radio.setProperty("conn_id", conn_id)
            radio.setProperty("action_key", key)
            grid.addWidget(radio, row_idx, col, alignment=Qt.AlignmentFlag.AlignCenter)
            group.addButton(radio, col)

        # Default selection: Adopt is the safest choice (preserves state
        # without creating duplicates).
        first = group.button(1)
        if first is not None:
            first.setChecked(True)
        self._row_groups[conn_id] = group

    @staticmethod
    def _format_row_descr(label: str, sess: DiscoveredSession) -> str:
        if sess.kind == "claude-local":
            where = sess.cwd or "(unknown cwd)"
            return f"<b>{label}</b><br><i>claude in {where}</i> — PID {sess.pid}"
        if sess.kind in ("claude-remote", "ssh-shell"):
            who = f"{sess.user}@{sess.host}" if sess.user else sess.host
            kind_label = "ssh+claude" if sess.kind == "claude-remote" else "ssh"
            return f"<b>{label}</b><br><i>{kind_label} {who}</i> — PID {sess.pid}"
        return f"<b>{label}</b><br>PID {sess.pid}"

    @staticmethod
    def _format_row_tooltip(sess: DiscoveredSession) -> str:
        return "\n".join([
            f"PID: {sess.pid}",
            f"Kind: {sess.kind}",
            f"Cmdline: {sess.cmdline}",
            f"TTY: {sess.tty}",
        ])

    def _on_accept(self) -> None:
        out: dict[str, LaunchConflictAction] = {}
        for conn_id, group in self._row_groups.items():
            checked = group.checkedButton()
            if checked is None:
                # No selection means we can't proceed safely; treat as cancel.
                self.reject()
                return
            action = checked.property("action_key")
            if action not in ("adopt", "duplicate", "skip"):
                self.reject()
                return
            out[conn_id] = action  # type: ignore[assignment]
        self.actions = out
        self.accept()
