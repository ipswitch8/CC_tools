# -*- coding: utf-8 -*-
"""
SshWorker — QRunnable wrappers for short SSH-related operations run from the
UI thread pool (e.g. "Test Connection" button in the Connection Editor).

Spec: §1.5, §5.7
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from cpsm.platform.process_runner import ProcessRunner
from cpsm.platform.ssh_binary import SshBinary

# ---------------------------------------------------------------------------
# Signal carrier (QObject required for signals on QRunnable)
# ---------------------------------------------------------------------------


class SshTestSignals(QObject):
    """Signals emitted by :class:`SshTestConnectionTask`.

    Attributes:
        finished: ``(success, error_message)`` — ``success`` is True when the
            SSH test command exited with code 0.  ``error_message`` is empty on
            success, or a human-readable description of the failure.
    """

    finished: Signal = Signal(bool, str)


# ---------------------------------------------------------------------------
# SshTestConnectionTask
# ---------------------------------------------------------------------------


class SshTestConnectionTask(QRunnable):
    """QRunnable that tests an SSH connection in a thread-pool thread.

    Runs ``ssh -o BatchMode=yes -o ConnectTimeout=5 user@host true`` (or the
    equivalent plink command) via :class:`ProcessRunner` and emits
    ``signals.finished(success, error_message)`` when done.

    This is used by the Connection Editor's "Test Connection" button.

    Args:
        ssh_binary:     Detected :class:`SshBinary` instance.
        host:           Remote hostname or IP.
        user:           Remote username.
        port:           SSH port (default 22).
        identity_file:  Path to private key (optional).
        runner:         Custom :class:`ProcessRunner` for testing; a fresh
                        instance is created when ``None``.
    """

    def __init__(
        self,
        ssh_binary: SshBinary,
        host: str,
        user: str,
        port: int = 22,
        identity_file: Path | None = None,
        runner: ProcessRunner | None = None,
    ) -> None:
        super().__init__()
        self._ssh_binary = ssh_binary
        self._host = host
        self._user = user
        self._port = port
        self._identity_file = identity_file
        self._runner = runner or ProcessRunner()
        self.signals = SshTestSignals()
        # Allow garbage collection after the task finishes.
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        """Execute the SSH test and emit ``signals.finished``."""
        # Build argv:  ssh -o BatchMode=yes -o ConnectTimeout=5 … true
        argv = self._ssh_binary.build_argv(
            host=self._host,
            user=self._user,
            port=self._port,
            identity_file=self._identity_file,
            ssh_options=["BatchMode=yes", "ConnectTimeout=5"],
            remote_command=["true"],
            force_tty=False,
        )

        try:
            result = self._runner.run(argv, timeout=15.0, check=False)
        except subprocess.TimeoutExpired:
            self.signals.finished.emit(False, "Connection timed out")
            return
        except OSError as exc:
            self.signals.finished.emit(False, f"Failed to launch SSH: {exc}")
            return
        except Exception as exc:
            self.signals.finished.emit(False, str(exc))
            return

        if result.returncode == 0:
            self.signals.finished.emit(True, "")
        else:
            error = (result.stderr or result.stdout or "").strip()
            if not error:
                error = f"SSH exited with code {result.returncode}"
            self.signals.finished.emit(False, error)
