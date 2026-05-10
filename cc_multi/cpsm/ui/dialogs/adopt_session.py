# -*- coding: utf-8 -*-
"""
cpsm.ui.dialogs.adopt_session — AdoptSessionDialog.

Modal dialog used when the user chooses to adopt an outside-CPSM session.
The flow has three states:

  Phase 1 (initial)
    Shows process details and asks how to close the running terminal:
    "I'll close it myself" / "SIGTERM for me" / "Cancel".

  Phase 2 (waiting)
    Polls every 200 ms until the pid is gone or a timeout (10s) elapses.
    The user sees a status line and a Cancel button.  If timeout fires,
    we offer to escalate to SIGKILL.

  Phase 3 (resolved)
    Dialog accepts (pid is gone).  The caller is responsible for invoking
    SessionService.launch_one() with ``--continue`` appended; this dialog
    only handles the close-the-existing-process step.

Returned via ``dialog.exec()`` plus the ``adopted`` attribute.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from cpsm.services.discovery_service import (
    DiscoveredSession,
    is_pid_alive,
    send_sigkill,
    send_sigterm,
)

__all__ = ["AdoptSessionDialog"]


class AdoptSessionDialog(QDialog):
    """Walk the user through closing an outside session before adoption.

    Parameters
    ----------
    session:
        The DiscoveredSession being adopted.
    target_label:
        Human-readable label for the target connection (e.g. "Dotfiles").
        Shown in the explanation text so the user understands what will
        happen after the close completes.
    parent:
        Optional Qt parent widget.

    Attributes
    ----------
    adopted:
        True when the dialog accepts because the pid is gone (success).
        False when the user cancelled or the pid never exited.
    """

    # Polling cadence and timeout for waiting on pid exit. Both are
    # short so the UI stays responsive but the test can override via
    # the constructor for deterministic runs.
    _POLL_INTERVAL_MS = 200
    _DEFAULT_TIMEOUT_MS = 10_000

    def __init__(
        self,
        session: DiscoveredSession,
        target_label: str,
        parent: QWidget | None = None,
        *,
        poll_interval_ms: int | None = None,
        timeout_ms: int | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("dialog_adopt_session")
        self.setWindowTitle("Adopt outside session")
        self.setModal(True)
        self.setMinimumWidth(480)

        self._session = session
        self._target_label = target_label or session.suggested_connection_id or "(new)"
        self._poll_interval_ms = poll_interval_ms or self._POLL_INTERVAL_MS
        self._timeout_ms = timeout_ms if timeout_ms is not None else self._DEFAULT_TIMEOUT_MS
        self._elapsed_ms = 0
        self.adopted = False

        self._build_ui()
        self._show_phase_1()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)

        # Static info block describing the discovered process.
        info = QLabel(self._format_info())
        info.setObjectName("label_adopt_info")
        info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        info.setWordWrap(True)
        root.addWidget(info)

        # Phase-dependent prompt text. Updated as the dialog moves through
        # phases.
        self._prompt = QLabel("")
        self._prompt.setObjectName("label_adopt_prompt")
        self._prompt.setWordWrap(True)
        root.addWidget(self._prompt)

        # Status line shown during the waiting phase. Hidden initially.
        self._status = QLabel("")
        self._status.setObjectName("label_adopt_status")
        self._status.setStyleSheet("color: #94a3b8;")
        self._status.setVisible(False)
        root.addWidget(self._status)

        # Button row — actual buttons swapped per phase via _set_buttons.
        self._button_row = QHBoxLayout()
        self._button_row.addStretch(1)
        root.addLayout(self._button_row)

    def _format_info(self) -> str:
        s = self._session
        lines: list[str] = [
            f"<b>Outside session:</b> PID {s.pid}",
            f"<b>Kind:</b> {s.kind}",
        ]
        if s.cwd:
            lines.append(f"<b>Working dir:</b> {s.cwd}")
        if s.host:
            who = f"{s.user}@{s.host}" if s.user else s.host
            lines.append(f"<b>Host:</b> {who}")
        if s.tty:
            lines.append(f"<b>TTY:</b> {s.tty}")
        if s.cmdline:
            lines.append(f"<b>Command:</b> <code>{s.cmdline}</code>")
        return "<br>".join(lines)

    # ------------------------------------------------------------------
    # Phase 1 — initial choice
    # ------------------------------------------------------------------

    def _show_phase_1(self) -> None:
        self._prompt.setText(
            f"Close that session, then CPSM will relaunch it as "
            f"<b>{self._target_label}</b> in a managed tmux pane "
            f"(with <code>--continue</code> appended for Claude profiles)."
        )
        self._status.setVisible(False)
        self._set_buttons([
            ("close_self", "I'll close it myself"),
            ("sigterm", "SIGTERM for me"),
            ("cancel", "Cancel"),
        ])

    def _on_phase1_choice(self, key: str) -> None:
        if key == "cancel":
            self.reject()
            return
        if key == "sigterm":
            ok = send_sigterm(self._session.pid)
            if not ok and is_pid_alive(self._session.pid):
                self._status.setText("Could not send SIGTERM (permission denied?). Try closing it manually.")
                self._status.setVisible(True)
                return
        self._show_phase_2()

    # ------------------------------------------------------------------
    # Phase 2 — wait for pid to exit
    # ------------------------------------------------------------------

    def _show_phase_2(self) -> None:
        self._prompt.setText(
            f"Waiting for PID {self._session.pid} to exit…"
        )
        self._status.setText("Polling every 0.2s.")
        self._status.setVisible(True)
        self._elapsed_ms = 0
        self._set_buttons([("cancel", "Cancel")])
        self._timer = QTimer(self)
        self._timer.setInterval(self._poll_interval_ms)
        self._timer.timeout.connect(self._poll_tick)  # type: ignore[arg-type]
        self._timer.start()
        self._poll_tick()  # check immediately rather than wait one tick

    def _poll_tick(self) -> None:
        if not is_pid_alive(self._session.pid):
            self._timer.stop()
            self.adopted = True
            self.accept()
            return
        self._elapsed_ms += self._poll_interval_ms
        self._status.setText(
            f"Still alive after {self._elapsed_ms / 1000:.1f}s "
            f"(timeout {self._timeout_ms / 1000:.0f}s)."
        )
        if self._elapsed_ms >= self._timeout_ms:
            self._timer.stop()
            self._show_phase_3_timeout()

    def _show_phase_3_timeout(self) -> None:
        self._prompt.setText(
            f"PID {self._session.pid} didn't exit within "
            f"{self._timeout_ms / 1000:.0f} seconds."
        )
        self._status.setText("You can force-kill it with SIGKILL or cancel and close it yourself.")
        self._status.setVisible(True)
        self._set_buttons([
            ("sigkill", "Force kill (SIGKILL)"),
            ("cancel", "Cancel"),
        ])

    def _on_phase3_choice(self, key: str) -> None:
        if key == "cancel":
            self.reject()
            return
        if key == "sigkill":
            send_sigkill(self._session.pid)
            # Give the kernel a brief window then re-enter the wait phase.
            # If SIGKILL didn't take, Phase 2 will time out again.
            self._show_phase_2()

    # ------------------------------------------------------------------
    # Button row management
    # ------------------------------------------------------------------

    def _set_buttons(self, buttons: list[tuple[str, str]]) -> None:
        # Clear existing widgets in the button row (keep the leading stretch).
        while self._button_row.count() > 1:
            item = self._button_row.takeAt(self._button_row.count() - 1)
            w = item.widget() if item is not None else None
            if w is not None:
                w.deleteLater()

        for key, label in buttons:
            btn = QPushButton(label, self)
            btn.setObjectName(f"btn_adopt_{key}")
            btn.clicked.connect(lambda _checked=False, k=key: self._dispatch(k))  # type: ignore[arg-type]
            self._button_row.addWidget(btn)

    def _dispatch(self, key: str) -> None:
        # Phase routing: phase 1 has close_self/sigterm/cancel, phase 3
        # timeout has sigkill/cancel. We disambiguate by checking which
        # set of keys is currently relevant.
        if key in ("close_self", "sigterm"):
            self._on_phase1_choice(key)
            return
        if key == "sigkill":
            self._on_phase3_choice(key)
            return
        # cancel is universal
        if hasattr(self, "_timer"):
            try:
                self._timer.stop()
            except Exception:
                pass
        self.reject()
