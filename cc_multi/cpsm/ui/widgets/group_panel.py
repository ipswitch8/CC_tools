# -*- coding: utf-8 -*-
"""
cpsm.ui.widgets.group_panel — GroupPanel two-list drag-drop widget.

Spec sections: §4.6

Displays two QListView widgets side by side:
  - Left:  Available connections (filterable by search)
  - Right: Group members (re-orderable by drag)

Each row shows the profile glyph and connection name.  Connections already
in any other group show an info icon with a tooltip listing those groups.

Buttons between the lists: ▶ (add), ◀ (remove), ▲/▼ (reorder members).
"""

from __future__ import annotations

from typing import NamedTuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

__all__ = ["ConnectionEntry", "GroupPanel"]

# ---------------------------------------------------------------------------
# Profile glyphs (matching connection_form.py / main_window sidebar)
# ---------------------------------------------------------------------------

_PROFILE_GLYPHS: dict[str, str] = {
    "claude-remote": "\U0001f517",  # 🔗
    "claude-local": "\U0001f4bb",  # 💻
    "ssh-shell": "⌨",  # ⌨
    "local-shell": "$",
    "custom": "⚙",  # ⚙
}

# U+2139 INFORMATION SOURCE + U+FE0F VARIATION SELECTOR-16 (emoji form)
_INFO_ICON: str = chr(0x2139) + chr(0xFE0F)


class ConnectionEntry(NamedTuple):
    """Represents one connection for display in GroupPanel lists."""

    conn_id: str
    name: str
    profile: str
    other_groups: list[str]  # group names this connection is already in


class GroupPanel(QWidget):
    """Two-list drag-drop panel for group membership editing.

    Signals:
        members_changed(list[str]): emitted whenever the members list changes;
            carries the new ordered list of connection IDs.
    """

    members_changed: Signal = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.setObjectName("group_panel")
        self.setAccessibleName("Group Panel")
        self.setAccessibleDescription("Two-list drag-drop widget for editing group membership")

        self._all_connections: list[ConnectionEntry] = []
        self._member_ids: list[str] = []

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        # ---- Left side: Available connections ----
        left_widget = QWidget()
        left_widget.setObjectName("widget_available_side")
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        lbl_available = QLabel("Available Connections")
        lbl_available.setObjectName("lbl_available")
        lbl_available.setAccessibleName("Available connections label")
        left_layout.addWidget(lbl_available)

        self._search_box = QLineEdit()
        self._search_box.setObjectName("search_available")
        self._search_box.setAccessibleName("Search available connections")
        self._search_box.setAccessibleDescription(
            "Filter the available connections list by name or ID"
        )
        self._search_box.setPlaceholderText("Search…")
        self._search_box.textChanged.connect(self._on_search_changed)
        left_layout.addWidget(self._search_box)

        self._list_available = QListView()
        self._list_available.setObjectName("list_available")
        self._list_available.setAccessibleName("Available connections list")
        self._list_available.setAccessibleDescription(
            "Connections not yet in the group; drag or click Add to move them"
        )
        self._list_available.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list_available.setDragEnabled(True)
        self._list_available.setAcceptDrops(True)
        self._list_available.setDropIndicatorShown(True)
        self._list_available.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self._model_available = QStandardItemModel()
        self._list_available.setModel(self._model_available)
        left_layout.addWidget(self._list_available)

        root.addWidget(left_widget, stretch=1)

        # ---- Middle: control buttons ----
        btn_layout = QVBoxLayout()
        btn_layout.setSpacing(4)
        btn_layout.addStretch()

        self._btn_add = QPushButton("▶")
        self._btn_add.setObjectName("btn_add_member")
        self._btn_add.setAccessibleName("Add to group button")
        self._btn_add.setAccessibleDescription("Move selected available connections into the group")
        self._btn_add.setFixedWidth(36)
        self._btn_add.setToolTip("Add to group")
        self._btn_add.clicked.connect(self._on_add_clicked)
        btn_layout.addWidget(self._btn_add)

        self._btn_remove = QPushButton("◀")
        self._btn_remove.setObjectName("btn_remove_member")
        self._btn_remove.setAccessibleName("Remove from group button")
        self._btn_remove.setAccessibleDescription("Move selected group members back to available")
        self._btn_remove.setFixedWidth(36)
        self._btn_remove.setToolTip("Remove from group")
        self._btn_remove.clicked.connect(self._on_remove_clicked)
        btn_layout.addWidget(self._btn_remove)

        btn_layout.addSpacing(12)

        self._btn_up = QPushButton("▲")
        self._btn_up.setObjectName("btn_move_up")
        self._btn_up.setAccessibleName("Move up button")
        self._btn_up.setAccessibleDescription("Move selected member up in launch order")
        self._btn_up.setFixedWidth(36)
        self._btn_up.setToolTip("Move up")
        self._btn_up.clicked.connect(self._on_move_up)
        btn_layout.addWidget(self._btn_up)

        self._btn_down = QPushButton("▼")
        self._btn_down.setObjectName("btn_move_down")
        self._btn_down.setAccessibleName("Move down button")
        self._btn_down.setAccessibleDescription("Move selected member down in launch order")
        self._btn_down.setFixedWidth(36)
        self._btn_down.setToolTip("Move down")
        self._btn_down.clicked.connect(self._on_move_down)
        btn_layout.addWidget(self._btn_down)

        btn_layout.addStretch()
        root.addLayout(btn_layout)

        # ---- Right side: Group members ----
        right_widget = QWidget()
        right_widget.setObjectName("widget_members_side")
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)

        lbl_members = QLabel("Group Members")
        lbl_members.setObjectName("lbl_members")
        lbl_members.setAccessibleName("Group members label")
        right_layout.addWidget(lbl_members)

        # Spacer to align with the search box on the left
        right_layout.addSpacing(self._search_box.sizeHint().height() + 4)

        self._list_members = QListView()
        self._list_members.setObjectName("list_members")
        self._list_members.setAccessibleName("Group members list")
        self._list_members.setAccessibleDescription(
            "Ordered list of connections in this group; drag to reorder"
        )
        self._list_members.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list_members.setDragEnabled(True)
        self._list_members.setAcceptDrops(True)
        self._list_members.setDropIndicatorShown(True)
        self._list_members.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self._list_members.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._model_members = QStandardItemModel()
        self._list_members.setModel(self._model_members)
        right_layout.addWidget(self._list_members)

        root.addWidget(right_widget, stretch=1)

    # ------------------------------------------------------------------
    # Data management
    # ------------------------------------------------------------------

    def set_all_connections(self, connections: list[ConnectionEntry]) -> None:
        """Set the full list of available connections.

        Call this before set_members() to ensure the available list is built
        correctly.
        """
        self._all_connections = list(connections)
        self._refresh_lists()

    def set_members(self, member_ids: list[str]) -> None:
        """Set the initial group members by connection ID."""
        self._member_ids = list(member_ids)
        self._refresh_lists()

    def get_members(self) -> list[str]:
        """Return the current ordered list of member connection IDs."""
        return list(self._member_ids)

    # ------------------------------------------------------------------
    # Internal list refresh
    # ------------------------------------------------------------------

    def _refresh_lists(self) -> None:
        """Rebuild both list models from current state."""
        self._refresh_members_model()
        self._refresh_available_model()

    def _make_item(self, entry: ConnectionEntry) -> QStandardItem:
        """Create a QStandardItem for a ConnectionEntry."""
        glyph = _PROFILE_GLYPHS.get(entry.profile, "?")
        label = f"{glyph}  {entry.name or entry.conn_id}"
        if entry.other_groups:
            label += f"  {_INFO_ICON}"
        item = QStandardItem(label)
        item.setData(entry.conn_id, Qt.ItemDataRole.UserRole)
        item.setEditable(False)
        if entry.other_groups:
            groups_str = ", ".join(entry.other_groups)
            item.setToolTip(f"Also in: {groups_str}")
        return item

    def _refresh_members_model(self) -> None:
        self._model_members.clear()
        id_to_entry = {e.conn_id: e for e in self._all_connections}
        for cid in self._member_ids:
            entry = id_to_entry.get(
                cid,
                ConnectionEntry(cid, cid, "custom", []),
            )
            self._model_members.appendRow(self._make_item(entry))

    def _refresh_available_model(self, filter_text: str = "") -> None:
        self._model_available.clear()
        ft = filter_text.lower()
        for entry in self._all_connections:
            if entry.conn_id in self._member_ids:
                continue
            if ft and ft not in entry.name.lower() and ft not in entry.conn_id.lower():
                continue
            self._model_available.appendRow(self._make_item(entry))

    def _on_search_changed(self, text: str) -> None:
        self._refresh_available_model(text)

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    def _on_add_clicked(self) -> None:
        """Add selected available items to members."""
        indices = self._list_available.selectedIndexes()
        for index in indices:
            cid = self._model_available.data(index, Qt.ItemDataRole.UserRole)
            if cid and cid not in self._member_ids:
                self._member_ids.append(cid)
        self._refresh_lists()
        self._emit_members_changed()

    def _on_remove_clicked(self) -> None:
        """Remove selected member items back to available."""
        indices = self._list_members.selectedIndexes()
        ids_to_remove = {self._model_members.data(i, Qt.ItemDataRole.UserRole) for i in indices}
        self._member_ids = [m for m in self._member_ids if m not in ids_to_remove]
        self._refresh_lists()
        self._emit_members_changed()

    def _on_move_up(self) -> None:
        """Move the first selected member up by one position."""
        indices = self._list_members.selectedIndexes()
        if not indices:
            return
        row = indices[0].row()
        if row <= 0:
            return
        self._member_ids.insert(row - 1, self._member_ids.pop(row))
        self._refresh_members_model()
        # Reselect
        new_index = self._model_members.index(row - 1, 0)
        self._list_members.setCurrentIndex(new_index)
        self._emit_members_changed()

    def _on_move_down(self) -> None:
        """Move the first selected member down by one position."""
        indices = self._list_members.selectedIndexes()
        if not indices:
            return
        row = indices[0].row()
        if row >= len(self._member_ids) - 1:
            return
        self._member_ids.insert(row + 1, self._member_ids.pop(row))
        self._refresh_members_model()
        new_index = self._model_members.index(row + 1, 0)
        self._list_members.setCurrentIndex(new_index)
        self._emit_members_changed()

    def _emit_members_changed(self) -> None:
        self.members_changed.emit(list(self._member_ids))
