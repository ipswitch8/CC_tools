# -*- coding: utf-8 -*-
"""
cpsm.ui.widgets.session_list — Sidebar session/connection tree widget.

Spec section: §3.1

Renders a QTreeWidget with four top-level categories:
  ▼ Connections  — one child per connection (with profile glyph)
  ▼ Groups       — one child per group
  ▼ Layouts      — one child per screen_layout
  ▼ Scenes       — one child per scene

Real interactive wiring (double-click → open editor, drag-and-drop, context
menus) is deferred to later phases.  This phase establishes the stable
objectNames and the tree structure.
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QMimeData, Qt
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cpsm.data.schema import CpsmDocument

__all__ = ["SessionListWidget"]


# ---------------------------------------------------------------------------
# Drag-source subclass (Round C)
# ---------------------------------------------------------------------------


_MIME_CONNECTION_ID = "application/x-cpsm-connection-id"


class _SidebarTreeWidget(QTreeWidget):
    """QTreeWidget subclass that emits MIME_CONNECTION_ID when a Connection
    item is dragged out of the sidebar.

    Used by the Screens canvas as the drag source for placing connections
    on the layout. Non-Connection items are not draggable.
    """

    def startDrag(self, supported_actions: Qt.DropAction) -> None:
        item = self.currentItem()
        if item is None:
            return
        parent = item.parent()
        if parent is None:
            return
        category_id: str = parent.data(0, Qt.ItemDataRole.UserRole) or ""
        if category_id != "category_cat_connections":
            return
        conn_id: str = item.data(0, Qt.ItemDataRole.UserRole) or ""
        if not conn_id:
            return

        mime = QMimeData()
        mime.setData(_MIME_CONNECTION_ID, QByteArray(conn_id.encode("utf-8")))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)

# ---------------------------------------------------------------------------
# Profile-glyph mapping (§3.1)
# ---------------------------------------------------------------------------

_PROFILE_GLYPHS: dict[str, str] = {
    "claude-remote": "🔗",
    "claude-local": "💻",
    "ssh-shell": "⌨",
    "local-shell": "$",
    "custom": "⚙",
}


class SessionListWidget(QWidget):
    """Sidebar widget containing the connections/groups/layouts/scenes tree.

    Parameters
    ----------
    parent:
        Optional Qt parent widget.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.setObjectName("widget_session_list")
        self.setAccessibleName("Session List Widget")
        self.setAccessibleDescription(
            "Sidebar widget for navigating connections, groups, layouts, and scenes"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        tree = _SidebarTreeWidget(self)
        tree.setObjectName("sidebar_tree")
        tree.setAccessibleName("Sidebar Tree")
        tree.setAccessibleDescription("Tree showing connections, groups, layouts, and scenes")
        tree.setColumnCount(1)
        tree.setHeaderHidden(True)
        tree.setUniformRowHeights(True)
        tree.setAlternatingRowColors(False)
        tree.setEditTriggers(QTreeWidget.EditTrigger.NoEditTriggers)
        tree.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        tree.setDragEnabled(True)
        tree.setDragDropMode(QTreeWidget.DragDropMode.DragOnly)
        layout.addWidget(tree)

        self._tree = tree

        # Top-level category items. Round C: Layouts and Scenes were removed
        # from the visible sidebar — Layouts are now an implementation detail
        # of Groups (one auto-managed layout per group), Scenes is unused.
        # The hidden categories are still constructed so existing code that
        # iterates `_cat_layouts` / `_cat_scenes` keeps working.
        self._cat_connections = self._make_category("Connections", "cat_connections")
        self._cat_groups = self._make_category("Groups", "cat_groups")
        self._cat_layouts = self._make_category("Layouts", "cat_layouts")
        self._cat_scenes = self._make_category("Scenes", "cat_scenes")

        tree.addTopLevelItem(self._cat_connections)
        tree.addTopLevelItem(self._cat_groups)
        # Layouts/Scenes categories are constructed but not added to the tree.

        self._cat_connections.setExpanded(True)
        self._cat_groups.setExpanded(True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_category(label: str, object_suffix: str) -> QTreeWidgetItem:
        """Return an un-checkable top-level category item."""
        item = QTreeWidgetItem([f"▼ {label}"])
        # QTreeWidgetItem does not support setObjectName directly;
        # we store it as UserRole data so lint tests can verify via the tree.
        item.setData(0, Qt.ItemDataRole.UserRole, f"category_{object_suffix}")
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        return item

    @staticmethod
    def _make_child(text: str, item_id: str) -> QTreeWidgetItem:
        """Return a child item with *item_id* stored as UserRole data."""
        child = QTreeWidgetItem([text])
        child.setData(0, Qt.ItemDataRole.UserRole, item_id)
        child.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsDragEnabled
        )
        return child

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_document(self, doc: CpsmDocument) -> None:
        """Populate the tree from *doc*.

        Parameters
        ----------
        doc:
            The ``CpsmDocument`` to render.
        """
        # Connections
        self._cat_connections.takeChildren()
        for conn in doc.connections:
            glyph = _PROFILE_GLYPHS.get(conn.launch_profile, "?")
            display = conn.name or conn.id
            child = self._make_child(f"{glyph} {display}", conn.id)
            self._cat_connections.addChild(child)

        # Groups
        self._cat_groups.takeChildren()
        for grp in doc.groups:
            child = self._make_child(grp.name, grp.id)
            self._cat_groups.addChild(child)

        # Layouts
        self._cat_layouts.takeChildren()
        for layout in doc.screen_layouts:
            child = self._make_child(layout.name, layout.id)
            self._cat_layouts.addChild(child)

        # Scenes
        self._cat_scenes.takeChildren()
        for scene in doc.scenes:
            child = self._make_child(scene.id, scene.id)
            self._cat_scenes.addChild(child)

        self._tree.update()

    @property
    def tree(self) -> QTreeWidget:
        """The underlying QTreeWidget (for testing and external access)."""
        return self._tree
