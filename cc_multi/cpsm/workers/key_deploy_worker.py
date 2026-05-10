# -*- coding: utf-8 -*-
"""
cpsm.workers.key_deploy_worker — QThread-based SSH key deployment worker.

Spec section: §9.2, §1.5

Runs KeyService.deploy() in a background QThread, emitting line-by-line
log output and a finished signal.  The password buffer is always zeroed
in a finally block (even on failure / cancel).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, QThread, Signal

if TYPE_CHECKING:
    from cpsm.data.schema import SshKey
    from cpsm.services.key_service import KeyService

__all__ = ["KeyDeploySignals", "KeyDeployWorker"]

logger = logging.getLogger(__name__)


class KeyDeploySignals(QObject):
    """Signals emitted by :class:`KeyDeployWorker`.

    Attributes:
        log_line: Emitted for each line of deploy output.
        finished: ``(success, error_message)`` pair on completion.
    """

    log_line: Signal = Signal(str)
    finished: Signal = Signal(bool, str)


class KeyDeployWorker(QThread):
    """Deploy an SSH public key to a remote connection in a background thread.

    Use ``worker.signals.log_line`` and ``worker.signals.finished`` to receive
    progress and completion notifications.
    """

    def __init__(
        self,
        key: SshKey,
        connection: Any,
        key_service: KeyService,
        password: str | None,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._key = key
        self._connection = connection
        self._key_service = key_service
        self._password = password
        self.signals = KeyDeploySignals()

    def run(self) -> None:
        """Run deployment; zero password buffer in finally."""
        password = self._password
        pp_buf: bytearray | None = None
        if password is not None:
            pp_buf = bytearray(password.encode("utf-8"))
        try:
            self.signals.log_line.emit(
                f"Deploying key '{self._key.name}' to {getattr(self._connection, 'host', '?')}…"
            )
            result = self._key_service.deploy(
                key=self._key,
                connection=self._connection,
                password=password,
            )
            if result.success:
                self.signals.log_line.emit(f"Success via {result.method}.")
                self.signals.finished.emit(True, "")
            else:
                errors = "; ".join(result.errors)
                self.signals.log_line.emit(f"Failed: {errors}")
                self.signals.finished.emit(False, errors)
        except Exception as exc:
            msg = str(exc)
            self.signals.log_line.emit(f"Error: {msg}")
            self.signals.finished.emit(False, msg)
        finally:
            # Zero the bytearray (best-effort)
            if pp_buf is not None:
                for i in range(len(pp_buf)):
                    pp_buf[i] = 0
                del pp_buf
            # Release reference to password string
            self._password = None
            password = None
