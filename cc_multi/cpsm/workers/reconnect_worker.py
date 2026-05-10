# -*- coding: utf-8 -*-
"""
ReconnectWorker — QThread that walks reconnect_backoff_ms, calling
respawn_pane until the pane is alive or attempts are exhausted.

Spec: §1.5, §5.7
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence

from PySide6.QtCore import QThread, Signal

from cpsm.platform.base import MultiplexerBackend

# ---------------------------------------------------------------------------
# Thread-safe atomic flag
#
# QAtomicInt is a C++ template and is not exposed in PySide6 Python bindings.
# We replicate the same loadRelaxed / storeRelaxed interface using a plain
# Python int guarded by threading.Lock.
# ---------------------------------------------------------------------------


class _AtomicInt:
    """Minimal thread-safe integer with loadRelaxed / storeRelaxed semantics."""

    def __init__(self, value: int = 0) -> None:
        self._lock = threading.Lock()
        self._value = value

    def loadRelaxed(self) -> int:
        with self._lock:
            return self._value

    def storeRelaxed(self, value: int) -> None:
        with self._lock:
            self._value = value


# ---------------------------------------------------------------------------
# Sentinel values returned by _reconnect_attempt
# ---------------------------------------------------------------------------

_RESULT_ALIVE = "alive"
_RESULT_DEAD = "dead"
_RESULT_STOPPED = "stopped"


def _reconnect_attempt(
    *,
    pane_id: str,
    launcher_command: str,
    respawn_fn: Callable[[], None],
    list_panes_fn: Callable[[], Sequence[object]],
    stop_fn: Callable[[], bool],
) -> tuple[str, str]:
    """Execute one reconnect attempt.

    Calls respawn_fn(), then list_panes_fn() to verify liveness.
    Checks stop_fn() between operations.

    Returns:
        (_RESULT_ALIVE|_RESULT_DEAD|_RESULT_STOPPED, last_error)
    """
    last_error = ""

    # Respawn
    try:
        respawn_fn()
    except Exception as exc:
        last_error = str(exc)

    if stop_fn():
        return _RESULT_STOPPED, last_error

    # Verify liveness
    alive = False
    try:
        panes = list_panes_fn()
        for pane in panes:
            pane_id_attr = getattr(pane, "id", None)
            dead_attr = getattr(pane, "dead", True)
            if pane_id_attr == pane_id and not dead_attr:
                alive = True
                break
    except Exception as exc:
        last_error = str(exc)

    if alive:
        return _RESULT_ALIVE, ""
    return _RESULT_DEAD, last_error


class ReconnectWorker(QThread):
    """QThread that attempts to reconnect a single dead pane.

    The worker calls ``backend.respawn_pane`` then verifies liveness via
    ``backend.list_panes``.  Between attempts it sleeps according to
    *backoff_ms* (repeating the last value once the list is exhausted and
    *max_attempts* is 0 for unlimited retries).

    Signals:
        progress:  ``(pane_id, attempt_number, max_attempts)`` — emitted before
                   each attempt.  ``max_attempts == 0`` means unlimited.
        succeeded: ``(pane_id,)`` — the pane is alive again.
        failed:    ``(pane_id, last_error)`` — all attempts exhausted.
        stopped:   ``(pane_id,)`` — :meth:`stop` was called before success.
    """

    progress: Signal = Signal(str, int, int)
    succeeded: Signal = Signal(str)
    failed: Signal = Signal(str, str)
    stopped: Signal = Signal(str)

    def __init__(
        self,
        backend: MultiplexerBackend,
        *,
        connection_id: str,
        pane_id: str,
        launcher_command: str,
        backoff_ms: list[int],
        max_attempts: int = 0,
        parent: object = None,
    ) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self._backend = backend
        self._connection_id = connection_id
        self._pane_id = pane_id
        self._launcher_command = launcher_command
        self._backoff_ms = list(backoff_ms) if backoff_ms else [1000]
        self._max_attempts = max_attempts
        # Atomic stop flag: 0 = running, 1 = stop requested
        self._stop = _AtomicInt(0)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Signal the worker to stop on the next iteration."""
        self._stop.storeRelaxed(1)

    # ------------------------------------------------------------------
    # QThread.run
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main reconnect loop."""
        attempt = 0
        last_error = ""

        while True:
            if self._stop.loadRelaxed():
                self.stopped.emit(self._pane_id)
                return

            attempt += 1

            # Check attempt cap (0 = unlimited).
            if self._max_attempts > 0 and attempt > self._max_attempts:
                self.failed.emit(self._pane_id, last_error)
                return

            # ── Emit progress before the attempt ──────────────────────
            self.progress.emit(self._pane_id, attempt, self._max_attempts)

            # ── Attempt respawn + liveness check ─────────────────────
            result, last_error = _reconnect_attempt(
                pane_id=self._pane_id,
                launcher_command=self._launcher_command,
                respawn_fn=lambda: self._backend.respawn_pane(
                    target=self._pane_id,
                    command=self._launcher_command,
                    kill_existing=True,
                ),
                list_panes_fn=lambda: self._backend.list_panes(self._pane_id),
                stop_fn=lambda: bool(self._stop.loadRelaxed()),
            )

            if result == _RESULT_STOPPED:
                self.stopped.emit(self._pane_id)
                return

            if result == _RESULT_ALIVE:
                self.succeeded.emit(self._pane_id)
                return

            # result == _RESULT_DEAD — check cap and sleep
            if self._stop.loadRelaxed():
                self.stopped.emit(self._pane_id)
                return

            if self._max_attempts > 0 and attempt >= self._max_attempts:
                self.failed.emit(self._pane_id, last_error)
                return

            # ── Sleep backoff ─────────────────────────────────────────
            backoff_idx = min(attempt - 1, len(self._backoff_ms) - 1)
            backoff_s = self._backoff_ms[backoff_idx] / 1000.0
            self._interruptible_sleep(backoff_s)

            if self._stop.loadRelaxed():
                self.stopped.emit(self._pane_id)
                return

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _interruptible_sleep(self, duration_s: float) -> None:
        """Sleep for *duration_s* seconds, checking the stop flag every 50 ms."""
        chunk = 0.05
        elapsed = 0.0
        while elapsed < duration_s:
            if self._stop.loadRelaxed():
                return
            sleep_for = min(chunk, duration_s - elapsed)
            time.sleep(sleep_for)
            elapsed += sleep_for
