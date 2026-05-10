# -*- coding: utf-8 -*-
"""
cpsm.ui.widgets.active_sessions — Active Sessions panel.

Spec: §5.8, §5.4

Displays a live table of all non-placeholder panes reported by a StatusPoller.
Columns: Status | Profile | Connection | Launched From | tmux Target | Started
         | Latency | Actions

The widget subscribes to StatusPoller.poll_complete and StatusPoller.state_changed
and keeps its internal model in sync without blocking the GUI thread.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QSizePolicy,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from cpsm.workers.status_poller import PaneState, PaneStatus

__all__ = ["ActiveSessionsWidget"]

# ---------------------------------------------------------------------------
# Column indices
# ---------------------------------------------------------------------------

_COL_STATUS = 0
_COL_PROFILE = 1
_COL_CONNECTION = 2
_COL_LAUNCHED_FROM = 3
_COL_TMUX_TARGET = 4
_COL_STARTED = 5
_COL_LATENCY = 6
_COL_ACTIONS = 7

_HEADERS = (
    "Status",
    "Profile",
    "Connection",
    "Launched From",
    "tmux Target",
    "Started",
    "Latency",
    "Actions",
)

# ---------------------------------------------------------------------------
# Color tokens per §5.4
# ---------------------------------------------------------------------------

_STATE_COLORS: dict[PaneState, str] = {
    PaneState.CONNECTED: "#22c55e",  # green
    PaneState.CONNECTING: "#f59e0b",  # yellow/amber
    PaneState.STALE: "#f97316",  # orange
    PaneState.ERROR: "#ef4444",  # red
    PaneState.DISCONNECTED_CLEAN: "#6b7280",  # gray
    PaneState.EMPTY_SLOT: "#6b7280",  # gray
    PaneState.UNKNOWN: "#6b7280",  # gray
}

_STATE_GLYPHS: dict[PaneState, str] = {
    PaneState.CONNECTED: "●",
    PaneState.CONNECTING: "●",
    PaneState.STALE: "●",
    PaneState.ERROR: "●",
    PaneState.DISCONNECTED_CLEAN: "○",
    PaneState.EMPTY_SLOT: "○",
    PaneState.UNKNOWN: "○",
}

# ---------------------------------------------------------------------------
# Internal row data
# ---------------------------------------------------------------------------


class _RowData:
    """All data needed to display one active-session row."""

    __slots__ = (
        "connection_id",
        "connection_name",
        "latency_ms",
        "launched_from",
        "pane_id",
        "profile_glyph",
        "started",
        "state",
        "tmux_target",
    )

    def __init__(
        self,
        *,
        pane_id: str,
        connection_id: str,
        profile_glyph: str = "?",
        connection_name: str = "",
        launched_from: str = "",
        tmux_target: str = "",
        started: datetime | None = None,
        latency_ms: int | None = None,
        state: PaneState = PaneState.UNKNOWN,
    ) -> None:
        self.pane_id = pane_id
        self.connection_id = connection_id
        self.profile_glyph = profile_glyph
        self.connection_name = connection_name
        self.launched_from = launched_from
        self.tmux_target = tmux_target
        self.started = started
        self.latency_ms = latency_ms
        self.state = state


# ---------------------------------------------------------------------------
# Table model
# ---------------------------------------------------------------------------


class _SessionTableModel(QAbstractTableModel):
    """QAbstractTableModel backing ActiveSessionsWidget's QTableView.

    The Actions column (index 7) is not rendered by the model; the view uses
    a delegate / persistent widget approach instead.  The model returns an
    empty string for that column so the view can size the row correctly.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[_RowData] = []

    # ------------------------------------------------------------------
    # QAbstractTableModel interface
    # ------------------------------------------------------------------

    def rowCount(
        self,
        parent: QModelIndex | QPersistentModelIndex = QModelIndex(),  # noqa: B008
    ) -> int:
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(
        self,
        parent: QModelIndex | QPersistentModelIndex = QModelIndex(),  # noqa: B008
    ) -> int:
        if parent.isValid():
            return 0
        return len(_HEADERS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if (
            orientation == Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
            and 0 <= section < len(_HEADERS)
        ):
            return _HEADERS[section]
        return None

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid():
            return None

        row = index.row()
        col = index.column()

        if row < 0 or row >= len(self._rows):
            return None

        entry = self._rows[row]

        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(entry, col)

        if role == Qt.ItemDataRole.ForegroundRole and col == _COL_STATUS:
            color_hex = _STATE_COLORS.get(entry.state, "#6b7280")
            return QColor(color_hex)

        if role == Qt.ItemDataRole.UserRole:
            # Expose the pane_id so tests can retrieve it
            return entry.pane_id

        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _display(entry: _RowData, col: int) -> str:
        if col == _COL_STATUS:
            return _STATE_GLYPHS.get(entry.state, "○")
        if col == _COL_PROFILE:
            return entry.profile_glyph
        if col == _COL_CONNECTION:
            return entry.connection_name or entry.connection_id
        if col == _COL_LAUNCHED_FROM:
            return entry.launched_from
        if col == _COL_TMUX_TARGET:
            return entry.tmux_target
        if col == _COL_STARTED:
            if entry.started is None:
                return ""
            return entry.started.strftime("%H:%M:%S")
        if col == _COL_LATENCY:
            if entry.latency_ms is None:
                return ""
            return f"{entry.latency_ms} ms"
        if col == _COL_ACTIONS:
            return ""
        return ""

    # ------------------------------------------------------------------
    # Public mutation API (called from ActiveSessionsWidget)
    # ------------------------------------------------------------------

    def get_row(self, pane_id: str) -> int | None:
        """Return the row index for *pane_id*, or None if not found."""
        for i, row in enumerate(self._rows):
            if row.pane_id == pane_id:
                return i
        return None

    def upsert_status(self, status: PaneStatus) -> None:
        """Insert or update the row for *status.pane_id*."""
        existing = self.get_row(status.pane_id)
        if existing is not None:
            # Update state and last_seen
            old = self._rows[existing]
            self._rows[existing] = _RowData(
                pane_id=old.pane_id,
                connection_id=old.connection_id,
                profile_glyph=old.profile_glyph,
                connection_name=old.connection_name,
                launched_from=old.launched_from,
                tmux_target=old.tmux_target,
                started=old.started,
                latency_ms=old.latency_ms,
                state=status.state,
            )
            left = self.index(existing, 0)
            right = self.index(existing, len(_HEADERS) - 1)
            self.dataChanged.emit(left, right, [Qt.ItemDataRole.DisplayRole])
        else:
            # New row — derive tmux_target from pane_id/session
            target = f"{status.session}:{status.pane_id}" if status.session else status.pane_id
            new_row = _RowData(
                pane_id=status.pane_id,
                connection_id=status.pane_id,
                tmux_target=target,
                started=status.last_seen,
                state=status.state,
            )
            insert_at = len(self._rows)
            self.beginInsertRows(QModelIndex(), insert_at, insert_at)
            self._rows.append(new_row)
            self.endInsertRows()

    def remove_row(self, pane_id: str) -> None:
        """Remove the row for *pane_id* if present."""
        idx = self.get_row(pane_id)
        if idx is not None:
            self.beginRemoveRows(QModelIndex(), idx, idx)
            self._rows.pop(idx)
            self.endRemoveRows()

    def replace_all(self, statuses: list[PaneStatus]) -> None:
        """Replace model contents with *statuses*, filtering placeholders."""
        self.beginResetModel()
        self._rows = []
        for s in statuses:
            target = f"{s.session}:{s.pane_id}" if s.session else s.pane_id
            self._rows.append(
                _RowData(
                    pane_id=s.pane_id,
                    connection_id=s.pane_id,
                    tmux_target=target,
                    started=s.last_seen,
                    state=s.state,
                )
            )
        self.endResetModel()

    def enrich_row(
        self,
        pane_id: str,
        *,
        connection_id: str | None = None,
        profile_glyph: str | None = None,
        connection_name: str | None = None,
        launched_from: str | None = None,
        tmux_target: str | None = None,
        started: datetime | None = None,
        latency_ms: int | None = None,
    ) -> None:
        """Apply optional metadata fields to an existing row."""
        idx = self.get_row(pane_id)
        if idx is None:
            return
        old = self._rows[idx]
        self._rows[idx] = _RowData(
            pane_id=old.pane_id,
            connection_id=connection_id if connection_id is not None else old.connection_id,
            profile_glyph=profile_glyph if profile_glyph is not None else old.profile_glyph,
            connection_name=(
                connection_name if connection_name is not None else old.connection_name
            ),
            launched_from=launched_from if launched_from is not None else old.launched_from,
            tmux_target=tmux_target if tmux_target is not None else old.tmux_target,
            started=started if started is not None else old.started,
            latency_ms=latency_ms if latency_ms is not None else old.latency_ms,
            state=old.state,
        )
        left = self.index(idx, 0)
        right = self.index(idx, len(_HEADERS) - 1)
        self.dataChanged.emit(left, right, [Qt.ItemDataRole.DisplayRole])

    @property
    def rows(self) -> list[_RowData]:
        """Read-only view of current rows (for testing)."""
        return list(self._rows)


# ---------------------------------------------------------------------------
# Action buttons widget (one per row, inserted via setIndexWidget)
# ---------------------------------------------------------------------------


class _ActionBar(QWidget):
    """Compact row of Attach / Reconnect / Kill buttons for one session row."""

    attach_clicked: Signal = Signal()
    reconnect_clicked: Signal = Signal()
    kill_clicked: Signal = Signal()

    def __init__(self, pane_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pane_id = pane_id
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 1, 2, 1)
        layout.setSpacing(4)

        btn_attach = QPushButton("Attach", self)
        btn_attach.setObjectName(f"btn_attach_{pane_id}")
        btn_attach.setAccessibleName(f"Attach {pane_id}")
        btn_attach.setFixedHeight(22)
        btn_attach.clicked.connect(self.attach_clicked)

        btn_reconnect = QPushButton("Reconnect", self)
        btn_reconnect.setObjectName(f"btn_reconnect_{pane_id}")
        btn_reconnect.setAccessibleName(f"Reconnect {pane_id}")
        btn_reconnect.setFixedHeight(22)
        btn_reconnect.clicked.connect(self.reconnect_clicked)

        btn_kill = QPushButton("Kill", self)
        btn_kill.setObjectName(f"btn_kill_{pane_id}")
        btn_kill.setAccessibleName(f"Kill {pane_id}")
        btn_kill.setFixedHeight(22)
        btn_kill.clicked.connect(self.kill_clicked)

        layout.addWidget(btn_attach)
        layout.addWidget(btn_reconnect)
        layout.addWidget(btn_kill)
        layout.addStretch()


# ---------------------------------------------------------------------------
# Public widget
# ---------------------------------------------------------------------------

_PLACEHOLDER_MARKERS = ("_placeholder.sh", "_placeholder")


def _is_placeholder_state(status: PaneStatus) -> bool:
    """Return True if *status* represents an empty-slot placeholder pane."""
    return status.state == PaneState.EMPTY_SLOT


class ActiveSessionsWidget(QWidget):
    """Panel showing all active (non-placeholder) sessions.

    Subscribes to a ``StatusPoller``'s ``poll_complete`` and ``state_changed``
    signals; keeps the table model up to date.

    Signals
    -------
    attach_requested(connection_id: str)
        Emitted when the user clicks "Attach" on a row.
    reconnect_requested(connection_id: str)
        Emitted when the user clicks "Reconnect" on a row.
    kill_requested(connection_id: str)
        Emitted when the user clicks "Kill" on a row.
    """

    attach_requested: Signal = Signal(str)
    reconnect_requested: Signal = Signal(str)
    kill_requested: Signal = Signal(str)

    def __init__(
        self,
        poller: Any,  # StatusPoller — typed as Any to allow MagicMock in tests
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("widget_active_sessions")
        self.setAccessibleName("Active Sessions Panel")

        self._poller = poller
        self._model = _SessionTableModel(self)
        # pane_id -> _ActionBar (kept alive so signals stay connected)
        self._action_bars: dict[str, _ActionBar] = {}

        self._setup_ui()
        self._connect_poller()

    # ------------------------------------------------------------------
    # UI setup
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._table = QTableView(self)
        self._table.setObjectName("table_active_sessions")
        self._table.setAccessibleName("Active Sessions Table")
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )

        header = self._table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        # Actions column stretches
        header.setSectionResizeMode(_COL_ACTIONS, QHeaderView.ResizeMode.Stretch)

        self._table.verticalHeader().setVisible(False)

        layout.addWidget(self._table)

    # ------------------------------------------------------------------
    # Poller wiring
    # ------------------------------------------------------------------

    def _connect_poller(self) -> None:
        """Wire StatusPoller signals to local slots."""
        try:
            self._poller.poll_complete.connect(self._on_poll_complete)
            self._poller.state_changed.connect(self._on_state_changed)
        except AttributeError:
            # MagicMock or other stub without proper signal connect — ignored
            pass

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_poll_complete(self, statuses: list[PaneStatus]) -> None:
        """Full refresh: replace model with non-placeholder statuses."""
        visible = [s for s in statuses if not _is_placeholder_state(s)]

        # Track current pane_ids in model
        old_ids = {row.pane_id for row in self._model.rows}
        new_ids = {s.pane_id for s in visible}

        # Remove gone panes
        for gone_id in old_ids - new_ids:
            self._model.remove_row(gone_id)
            self._action_bars.pop(gone_id, None)

        # Upsert visible panes
        for status in visible:
            self._model.upsert_status(status)
            self._ensure_action_bar(status.pane_id)

        # Re-install action bar widgets after any model reset
        self._install_action_bars()

    def _on_state_changed(self, status: PaneStatus) -> None:
        """Partial update: update or insert a single pane's state."""
        if _is_placeholder_state(status):
            self._model.remove_row(status.pane_id)
            self._action_bars.pop(status.pane_id, None)
            return

        self._model.upsert_status(status)
        self._ensure_action_bar(status.pane_id)
        self._install_action_bars()

    # ------------------------------------------------------------------
    # Action bar management
    # ------------------------------------------------------------------

    def _ensure_action_bar(self, pane_id: str) -> _ActionBar:
        """Create an _ActionBar for *pane_id* if one doesn't already exist."""
        if pane_id not in self._action_bars:
            bar = _ActionBar(pane_id, self)
            bar.attach_clicked.connect(lambda pid=pane_id: self.attach_requested.emit(pid))
            bar.reconnect_clicked.connect(lambda pid=pane_id: self.reconnect_requested.emit(pid))
            bar.kill_clicked.connect(lambda pid=pane_id: self.kill_requested.emit(pid))
            self._action_bars[pane_id] = bar
        return self._action_bars[pane_id]

    def _install_action_bars(self) -> None:
        """Set action bar widgets for every row currently in the model."""
        for i, row in enumerate(self._model.rows):
            bar = self._action_bars.get(row.pane_id)
            if bar is not None:
                self._table.setIndexWidget(
                    self._model.index(i, _COL_ACTIONS),
                    bar,
                )

    # ------------------------------------------------------------------
    # Public accessors (for testing)
    # ------------------------------------------------------------------

    @property
    def model(self) -> _SessionTableModel:
        """The underlying _SessionTableModel."""
        return self._model

    @property
    def table(self) -> QTableView:
        """The underlying QTableView."""
        return self._table

    def action_bar_for(self, pane_id: str) -> _ActionBar | None:
        """Return the _ActionBar for *pane_id*, or None."""
        return self._action_bars.get(pane_id)
