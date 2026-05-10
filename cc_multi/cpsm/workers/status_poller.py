# -*- coding: utf-8 -*-
"""
StatusPoller — QThread that periodically polls MultiplexerBackend.list_panes()
and emits Qt signals on state transitions.

Spec: §1.5, §5.7
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from PySide6.QtCore import QThread, Signal

from cpsm.platform.base import MultiplexerBackend, Pane

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
# State model
# ---------------------------------------------------------------------------


class PaneState(Enum):
    """Possible states for a tracked pane."""

    CONNECTED = "connected"
    CONNECTING = "connecting"
    STALE = "stale"
    ERROR = "error"
    DISCONNECTED_CLEAN = "disconnected_clean"
    EMPTY_SLOT = "empty_slot"  # pane is running _placeholder.sh
    UNKNOWN = "unknown"  # pane disappeared from list_panes output


@dataclass(frozen=True)
class PaneStatus:
    """Snapshot of a single pane's status at a given moment."""

    pane_id: str
    session: str
    state: PaneState
    last_seen: datetime
    exit_code: int | None  # populated for dead panes
    last_output_tail: str | None  # populated for ERROR state via capture_pane
    # Round-late: positional info so callers can map status back to a
    # layout pane via (session, window_index, pane_index).
    window_index: int = -1
    pane_index: int = -1
    # Foreground command in the pane (tmux #{pane_current_command}).
    # Empty for dead/unknown panes. The poller surfaces this so higher-level
    # code can distinguish "ssh running" (green) from "bash at retry prompt
    # after ssh dropped" (amber) without re-querying the backend.
    current_command: str = ""
    # Whether any client is attached to the pane's tmux session. False when
    # the user has closed the terminal window — the pane is still alive
    # server-side but no one can see it (blue / disconnected).
    attached: bool = True
    # tmux ``pane_start_command`` — the argv used to spawn the pane (e.g.
    # ``bash /tmp/cpsm-launcher-<conn_id>-<rand>.sh``). Stable across the
    # pane's lifetime; running processes can't overwrite it. Used by the UI
    # status lookup as a fallback when the layout's positional key doesn't
    # match (e.g. a connection running in its own ``cpsm-<conn_id>`` session
    # before being moved into a group session).
    start_command: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PLACEHOLDER_MARKERS = ("_placeholder.sh", "_placeholder")


def _is_placeholder(cmd: str) -> bool:
    """Return True if *cmd* is the empty-slot placeholder."""
    return any(m in cmd for m in _PLACEHOLDER_MARKERS)


def _classify(
    pane: Pane,
    prev_status: PaneStatus | None,
    *,
    interval_s: float,
    last_output_tail: str | None = None,
    attached: bool = True,
) -> PaneStatus:
    """Determine the :class:`PaneState` for *pane*.

    Args:
        pane:            Current pane data from list_panes.
        prev_status:     Previous PaneStatus for the same pane (None on first
                         observation).
        interval_s:      Poll interval in seconds; stale threshold is 2x this.
        last_output_tail: Pre-fetched capture_pane output for ERROR panes.
        attached:        Whether any client is attached to ``pane.session``.

    Returns:
        A new :class:`PaneStatus` reflecting the current classification.
    """
    now = datetime.now(tz=UTC)

    # ── Dead pane ─────────────────────────────────────────────────────────
    if pane.dead:
        # dead_status is an Optional field on Pane (added in phase 8).
        # Use getattr for backward compatibility with backends that return
        # Pane instances constructed without this field.
        dead_status: int | None = getattr(pane, "dead_status", None)
        if dead_status is None:
            # Fallback: treat unknown exit as ERROR.
            state = PaneState.ERROR
        elif dead_status == 0:
            state = PaneState.DISCONNECTED_CLEAN
        else:
            state = PaneState.ERROR
        return PaneStatus(
            pane_id=pane.id,
            session=pane.session,
            state=state,
            last_seen=now,
            exit_code=dead_status,
            last_output_tail=last_output_tail if state is PaneState.ERROR else None,
            window_index=pane.window_index,
            pane_index=pane.pane_index,
            current_command=pane.current_command,
            attached=attached,
            start_command=getattr(pane, "start_command", "") or "",
        )

    # ── Alive pane ────────────────────────────────────────────────────────
    if _is_placeholder(pane.current_command):
        return PaneStatus(
            pane_id=pane.id,
            session=pane.session,
            state=PaneState.EMPTY_SLOT,
            last_seen=now,
            exit_code=None,
            last_output_tail=None,
            window_index=pane.window_index,
            pane_index=pane.pane_index,
            current_command=pane.current_command,
            attached=attached,
            start_command=getattr(pane, "start_command", "") or "",
        )

    # Stale: alive but no activity for >= 2x interval.
    # We use last_seen from prev_status; if the pane just appeared it cannot
    # be stale yet.
    if prev_status is not None and prev_status.state not in (
        PaneState.UNKNOWN,
        PaneState.DISCONNECTED_CLEAN,
        PaneState.ERROR,
    ):
        age_s = (now - prev_status.last_seen).total_seconds()
        if age_s >= 2.0 * interval_s:
            return PaneStatus(
                pane_id=pane.id,
                session=pane.session,
                state=PaneState.STALE,
                last_seen=prev_status.last_seen,  # keep original last_seen
                exit_code=None,
                last_output_tail=None,
                window_index=pane.window_index,
                pane_index=pane.pane_index,
                current_command=pane.current_command,
                attached=attached,
                start_command=getattr(pane, "start_command", "") or "",
            )

    return PaneStatus(
        pane_id=pane.id,
        session=pane.session,
        state=PaneState.CONNECTED,
        last_seen=now,
        exit_code=None,
        last_output_tail=None,
        window_index=pane.window_index,
        pane_index=pane.pane_index,
        current_command=pane.current_command,
        attached=attached,
        start_command=getattr(pane, "start_command", "") or "",
    )


def _process_poll(
    panes: list[Pane],
    prev_state: dict[str, PaneStatus],
    *,
    interval_s: float,
    capture_pane_fn: Callable[[str], str | None],
    attached_lookup: Callable[[str], bool] | None = None,
) -> tuple[list[PaneStatus], list[PaneStatus], set[str]]:
    """Process a single poll result and compute new statuses.

    This is extracted from the QThread so it can be unit-tested without
    needing to start a thread.

    Args:
        panes:           Fresh pane list from backend.list_panes().
        prev_state:      Mapping of pane_id -> last known PaneStatus.
        interval_s:      Poll interval; used for stale detection.
        capture_pane_fn: Callable(pane_id) -> str | None for dead panes.
        attached_lookup: Callable(session_name) -> bool returning whether any
                         tmux client is attached to the pane's session. When
                         None, every pane is treated as attached.

    Returns:
        (changed, all_statuses, current_ids):
            changed      — PaneStatus objects whose state transitioned.
            all_statuses — every PaneStatus for this poll cycle.
            current_ids  — set of pane_ids seen in this poll.
    """
    current_ids: set[str] = set()
    all_statuses: list[PaneStatus] = []
    changed: list[PaneStatus] = []

    for pane in panes:
        current_ids.add(pane.id)
        prev = prev_state.get(pane.id)

        tail: str | None = None
        if pane.dead:
            dead_status_val: int | None = getattr(pane, "dead_status", None)
            if dead_status_val is None or dead_status_val != 0:
                try:
                    tail = capture_pane_fn(pane.id)
                except Exception:
                    tail = None

        attached = True if attached_lookup is None else bool(attached_lookup(pane.session))

        new_status = _classify(
            pane,
            prev,
            interval_s=interval_s,
            last_output_tail=tail,
            attached=attached,
        )
        all_statuses.append(new_status)

        if prev is None or prev.state != new_status.state:
            changed.append(new_status)

        prev_state[pane.id] = new_status

    return changed, all_statuses, current_ids


# ---------------------------------------------------------------------------
# StatusPoller
# ---------------------------------------------------------------------------


class StatusPoller(QThread):
    """QThread that polls ``backend.list_panes()`` on a fixed interval.

    Signals:
        state_changed: Emitted when a pane's state transitions.  Carries the
            new :class:`PaneStatus`.
        poll_complete: Emitted after every poll cycle.  Carries a list of all
            current :class:`PaneStatus` objects.
    """

    state_changed: Signal = Signal(PaneStatus)
    poll_complete: Signal = Signal(list)

    def __init__(
        self,
        backend: MultiplexerBackend,
        *,
        interval_ms: int = 3000,
        parent: object = None,
    ) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self._backend = backend
        self._interval_ms = interval_ms
        # Atomic stop flag: 0 = running, 1 = stop requested
        self._stop = _AtomicInt(0)
        # pane_id -> last known PaneStatus
        self._state: dict[str, PaneStatus] = {}
        # Snapshot of all panes' statuses from the most recent poll
        # (round-late: callers like MainWindow's status-lookup read
        # this attribute directly instead of subscribing to the
        # poll_complete signal).
        self.last_snapshot: list[PaneStatus] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Request the polling loop to exit on the next iteration."""
        self._stop.storeRelaxed(1)

    # ------------------------------------------------------------------
    # QThread.run
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main polling loop.  Runs until :meth:`stop` is called."""
        interval_s = self._interval_ms / 1000.0

        while not self._stop.loadRelaxed():
            try:
                panes: list[Pane] = self._backend.list_panes()
            except Exception:
                # Backend unavailable — most commonly because the tmux server
                # exited after the last cpsm session was reaped.  Treat as
                # "no panes anywhere" so the disappeared-detection branch
                # below synthesizes UNKNOWN for any previously-known panes;
                # the UI then resolves them to "disconnected" instead of
                # rendering a stale snapshot forever.
                panes = []

            # session_name → attached.  Used to detect "user closed the
            # terminal window" — the tmux pane stays alive but nothing is
            # rendering it client-side, so we surface that as detached.
            attached_map: dict[str, bool] = {}
            try:
                for sess in self._backend.list_sessions():
                    attached_map[sess.name] = bool(getattr(sess, "attached", False))
            except Exception:
                # Sessions query failed; default everything to attached so
                # we don't spuriously mark live panes as disconnected.
                attached_map = {}

            if self._stop.loadRelaxed():
                return

            changed, all_statuses, current_ids = _process_poll(
                panes,
                self._state,
                interval_s=interval_s,
                capture_pane_fn=lambda pid: self._backend.capture_pane(pid, lines=200),
                attached_lookup=lambda s: attached_map.get(s, True),
            )

            for status in changed:
                if self._stop.loadRelaxed():
                    return
                self.state_changed.emit(status)

            # Detect panes that disappeared from the listing.
            disappeared = set(self._state.keys()) - current_ids
            for pane_id in disappeared:
                if self._stop.loadRelaxed():
                    return
                prev_status = self._state.pop(pane_id, None)
                unknown_status = PaneStatus(
                    pane_id=pane_id,
                    session=prev_status.session if prev_status else "",
                    state=PaneState.UNKNOWN,
                    last_seen=datetime.now(tz=UTC),
                    exit_code=None,
                    last_output_tail=None,
                    window_index=prev_status.window_index if prev_status else -1,
                    pane_index=prev_status.pane_index if prev_status else -1,
                    current_command=prev_status.current_command if prev_status else "",
                    attached=False,
                    start_command=prev_status.start_command if prev_status else "",
                )
                all_statuses.append(unknown_status)
                self.state_changed.emit(unknown_status)

            # Cache before emitting so any synchronous handler can read
            # it via poller.last_snapshot.
            self.last_snapshot = all_statuses
            self.poll_complete.emit(all_statuses)

            if self._stop.loadRelaxed():
                return
            self._interruptible_sleep(interval_s)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _interruptible_sleep(self, duration_s: float) -> None:
        """Sleep for *duration_s* seconds, waking early if stop is requested.

        Checks the stop flag every 50 ms so that :meth:`stop` returns quickly.
        """
        chunk = 0.05  # 50 ms granularity
        elapsed = 0.0
        while elapsed < duration_s:
            if self._stop.loadRelaxed():
                return
            sleep_for = min(chunk, duration_s - elapsed)
            time.sleep(sleep_for)
            elapsed += sleep_for
