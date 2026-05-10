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

import os
from typing import Any

from PySide6.QtCore import QByteArray, QMimeData, Qt, Signal
from PySide6.QtGui import QDrag, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLineEdit,
    QSizePolicy,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cpsm.data.schema import CpsmDocument
from cpsm.services.discovery_service import DiscoveredSession

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

# Glyphs for discovered (outside-CPSM) sessions in the Discovered category.
# Matches _PROFILE_GLYPHS where applicable so users see the same icon they
# already associate with a launch profile.
_DISCOVERED_GLYPHS: dict[str, str] = {
    "claude-local": "💻",
    "claude-remote": "🔗",
    "ssh-shell": "⌨",
    "unknown": "❓",
}


class SessionListWidget(QWidget):
    """Sidebar widget containing the connections/groups/layouts/scenes tree.

    Includes a search field (case-insensitive substring match against
    Connection names) and a sort-mode toggle. Default sort orders
    Connections by their primary group name, then by Connection name; the
    "A→Z only" toggle switches to a flat alphabetical sort with no group
    separator. Groups are always sorted alphabetically.

    The widget caches the most recently loaded document so the search and
    sort controls can re-render without the caller re-supplying it.

    Parameters
    ----------
    parent:
        Optional Qt parent widget.

    Signals
    -------
    tree_rebuilt:
        Emitted whenever the tree's items are re-rendered (load, search
        change, sort toggle). Consumers like MainWindow connect this to
        re-apply membership highlights and status dots, since the
        QTreeWidgetItem instances are recreated on each render.
    """

    # Emitted whenever the tree contents are rebuilt.
    tree_rebuilt: Signal = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.setObjectName("widget_session_list")
        self.setAccessibleName("Session List Widget")
        self.setAccessibleDescription(
            "Sidebar widget for navigating connections, groups, layouts, and scenes"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # Search + sort toolbar
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(4, 4, 4, 0)
        toolbar.setSpacing(4)

        self._search = QLineEdit(self)
        self._search.setObjectName("sidebar_search")
        self._search.setAccessibleName("Sidebar Search")
        self._search.setAccessibleDescription(
            "Filter connections by name (case-insensitive); also filters groups "
            "to those containing at least one matching connection"
        )
        self._search.setPlaceholderText("Search…")
        self._search.setClearButtonEnabled(True)
        # The clear button Qt auto-creates is a QToolButton without an
        # objectName, which trips the project-wide objectName lint.  Tag it
        # so the lint passes.
        for btn in self._search.findChildren(QToolButton):
            if not btn.objectName():
                btn.setObjectName("sidebar_search_clear")
        self._search.textChanged.connect(self._render)  # type: ignore[arg-type]

        self._alpha_only = QCheckBox("A→Z", self)
        self._alpha_only.setObjectName("sidebar_sort_alpha")
        self._alpha_only.setAccessibleName("Sidebar Alphabetical Sort")
        self._alpha_only.setToolTip(
            "Sort connections purely by name.\n"
            "When unchecked (default), sort by group, then by name."
        )
        self._alpha_only.stateChanged.connect(self._render)  # type: ignore[arg-type]

        toolbar.addWidget(self._search, 1)
        toolbar.addWidget(self._alpha_only)
        layout.addLayout(toolbar)

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
        # Multi-select: Ctrl-click adds/removes individual items, Shift-click
        # extends a range. Enables the "launch selection as temp group" flow.
        tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
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
        # Discovered (unmatched): outside-CPSM sessions that did NOT match
        # any existing Connection. Matched sessions render as italic
        # sub-rows under their parent Connection — see _render() — so the
        # relationship is structural rather than hinted-at.
        self._cat_discovered = self._make_category(
            "Discovered (unmatched)", "cat_discovered"
        )

        tree.addTopLevelItem(self._cat_connections)
        tree.addTopLevelItem(self._cat_groups)
        tree.addTopLevelItem(self._cat_discovered)
        self._cat_discovered.setHidden(True)
        # Layouts/Scenes categories are constructed but not added to the tree.

        self._cat_connections.setExpanded(True)
        self._cat_groups.setExpanded(True)
        self._cat_discovered.setExpanded(True)

        # Cached state so search/sort changes can re-render without the
        # caller re-supplying the document.
        self._last_doc: CpsmDocument | None = None
        self._last_temp_ids: set[str] = set()
        # Discovered sessions are kept in their own list so document
        # reloads (which call load_document) don't clobber the discovered
        # listing.  MainWindow drives this via set_discovered_sessions().
        self._discovered: list[DiscoveredSession] = []

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

    @staticmethod
    def _make_separator(label: str) -> QTreeWidgetItem:
        """Return a non-selectable, non-draggable separator row used to
        split the Connections category into in-group / orphan sections."""
        item = QTreeWidgetItem([f"── {label} ──"])
        # Tag so click handlers can ignore it explicitly. Using NoItemFlags
        # disables selection/drag/clicks while leaving the row visible.
        item.setData(0, Qt.ItemDataRole.UserRole, "_separator")
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        return item

    @staticmethod
    def _name_key(name_or_id: str) -> str:
        return (name_or_id or "").casefold()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_document(
        self,
        doc: CpsmDocument,
        temp_group_ids: set[str] | None = None,
    ) -> None:
        """Populate the tree from *doc*.

        Parameters
        ----------
        doc:
            The ``CpsmDocument`` to render.
        temp_group_ids:
            IDs of groups currently held in memory only (created via
            "Launch selected as temporary group" and not yet pinned).
            These render with a 📌 prefix and italic font so the user
            can tell they will not be saved on the next ``_save_document``.
        """
        self._last_doc = doc
        self._last_temp_ids = set(temp_group_ids or ())
        self._render()

    def _render(self) -> None:
        """Rebuild the tree from cached state honoring the current search
        text and sort-mode toggle. Emits ``tree_rebuilt`` when done.

        Safe to call before a document has been loaded — in that case
        only the Discovered (unmatched) section is rendered (matched
        sessions can't be nested without a document to look up their
        parent Connection in).
        """
        # Split discovered sessions into matched (rendered as sub-rows under
        # their parent Connection) and unmatched (rendered in the dedicated
        # category at the bottom of the tree).
        matched_by_conn: dict[str, list[DiscoveredSession]] = {}
        unmatched: list[DiscoveredSession] = []
        for s in self._discovered:
            cid = s.suggested_connection_id
            if cid:
                matched_by_conn.setdefault(cid, []).append(s)
            else:
                unmatched.append(s)

        if self._last_doc is None:
            # Pre-document path: only the unmatched section is renderable.
            # Matched sessions stay buffered in self._discovered until a
            # document arrives via load_document().
            self._render_unmatched(unmatched + [
                s for ss in matched_by_conn.values() for s in ss
            ])
            self._tree.update()
            self.tree_rebuilt.emit()
            return
        doc = self._last_doc
        temp_ids = self._last_temp_ids
        query = self._search.text().strip().casefold()
        alpha_only = self._alpha_only.isChecked()

        # Map each connection to its primary (alphabetically first) group
        # name. Connections in no group → None.
        sorted_groups_for_conn: dict[str, list[str]] = {}
        for grp in doc.groups:
            gname = (grp.name or grp.id) or ""
            for cid in grp.members:
                sorted_groups_for_conn.setdefault(cid, []).append(gname)

        def primary_group_name(conn_id: str) -> str | None:
            names = sorted_groups_for_conn.get(conn_id)
            if not names:
                return None
            return sorted(names, key=self._name_key)[0]

        def conn_matches(conn: Any) -> bool:
            if not query:
                return True
            display = (conn.name or conn.id) or ""
            return query in display.casefold()

        matching_conns = [c for c in doc.connections if conn_matches(c)]
        matching_ids = {c.id for c in matching_conns}

        # ── Connections section ─────────────────────────────────────────
        self._cat_connections.takeChildren()

        def _add_conn(conn: Any) -> None:
            child = self._make_conn_child(conn)
            self._cat_connections.addChild(child)
            for sess in matched_by_conn.get(conn.id, ()):
                sub = self._make_subrow(sess)
                child.addChild(sub)
            if matched_by_conn.get(conn.id):
                child.setExpanded(True)

        if alpha_only:
            sorted_conns = sorted(
                matching_conns, key=lambda c: self._name_key(c.name or c.id)
            )
            for conn in sorted_conns:
                _add_conn(conn)
        else:
            in_group = []
            orphans = []
            for c in matching_conns:
                if primary_group_name(c.id) is not None:
                    in_group.append(c)
                else:
                    orphans.append(c)
            in_group.sort(
                key=lambda c: (
                    self._name_key(primary_group_name(c.id) or ""),
                    self._name_key(c.name or c.id),
                )
            )
            orphans.sort(key=lambda c: self._name_key(c.name or c.id))

            for conn in in_group:
                _add_conn(conn)
            if orphans and in_group:
                self._cat_connections.addChild(self._make_separator("Not in any group"))
            for conn in orphans:
                _add_conn(conn)

        # ── Groups section (always alphabetical) ────────────────────────
        # When a search is active, hide groups that contain no matching
        # connections so the user can see at a glance which groups host
        # the search hits.
        self._cat_groups.takeChildren()

        def group_visible(grp: Any) -> bool:
            if not query:
                return True
            return any(cid in matching_ids for cid in grp.members)

        shown_groups = [g for g in doc.groups if group_visible(g)]
        shown_groups.sort(key=lambda g: self._name_key(g.name or g.id))
        for grp in shown_groups:
            is_temp = grp.id in temp_ids
            label = f"📌 {grp.name}" if is_temp else (grp.name or grp.id)
            child = self._make_child(label, grp.id)
            if is_temp:
                font = QFont()
                font.setItalic(True)
                child.setFont(0, font)
            self._cat_groups.addChild(child)

        # Layouts (hidden category, kept for back-compat with code that
        # still iterates _cat_layouts).
        self._cat_layouts.takeChildren()
        for screen_layout in doc.screen_layouts:
            child = self._make_child(screen_layout.name, screen_layout.id)
            self._cat_layouts.addChild(child)

        # Scenes (hidden category)
        self._cat_scenes.takeChildren()
        for scene in doc.scenes:
            child = self._make_child(scene.id, scene.id)
            self._cat_scenes.addChild(child)

        # ── Discovered (unmatched) section ──────────────────────────────
        self._render_unmatched(unmatched)

        self._tree.update()
        self.tree_rebuilt.emit()

    def _render_unmatched(self, unmatched: list[DiscoveredSession]) -> None:
        """Populate the Discovered (unmatched) category. Hidden when empty."""
        self._cat_discovered.takeChildren()
        if not unmatched:
            self._cat_discovered.setHidden(True)
            return
        self._cat_discovered.setHidden(False)
        for s in unmatched:
            glyph = _DISCOVERED_GLYPHS.get(s.kind, "❓")
            label = self._format_discovered_label(s)
            child = self._make_child(f"{glyph} {label}", f"discovered:{s.pid}")
            font = QFont()
            font.setItalic(True)
            child.setFont(0, font)
            child.setToolTip(0, self._format_discovered_tooltip(s))
            self._cat_discovered.addChild(child)

    def _make_subrow(self, s: DiscoveredSession) -> QTreeWidgetItem:
        """Build a sub-row item for a matched discovered session, intended
        as a child of its Connection's tree item.

        Sub-rows use a compact label since the parent Connection already
        names what they belong to.
        """
        child = self._make_child(self._format_subrow_label(s), f"discovered:{s.pid}")
        font = QFont()
        font.setItalic(True)
        child.setFont(0, font)
        child.setToolTip(0, self._format_discovered_tooltip(s))
        # Sub-rows are not draggable as Connections — make sure the
        # ItemIsDragEnabled flag we inherited from _make_child is cleared.
        flags = child.flags() & ~Qt.ItemFlag.ItemIsDragEnabled
        child.setFlags(flags)
        return child

    @staticmethod
    def _format_subrow_label(s: DiscoveredSession) -> str:
        """Compact label for a discovered sub-row under its Connection."""
        if s.kind == "claude-local":
            return f"↳ outside · claude in {_short_path(s.cwd)}  [PID {s.pid}]"
        if s.kind in ("claude-remote", "ssh-shell"):
            who = f"{s.user}@{s.host}" if s.user else s.host
            kind_label = "ssh+claude" if s.kind == "claude-remote" else "ssh"
            return f"↳ outside · {kind_label} {who}  [PID {s.pid}]"
        return f"↳ outside  [PID {s.pid}]"

    def _make_conn_child(self, conn: Any) -> QTreeWidgetItem:  # type: ignore[name-defined]
        glyph = _PROFILE_GLYPHS.get(conn.launch_profile, "?")
        display = conn.name or conn.id
        return self._make_child(f"{glyph} {display}", conn.id)

    # ------------------------------------------------------------------
    # Discovered (outside-CPSM) sessions
    # ------------------------------------------------------------------

    def set_discovered_sessions(
        self,
        sessions: list[DiscoveredSession],
    ) -> None:
        """Update the Discovered display from *sessions*.

        Matched sessions render as italic sub-rows under their parent
        Connection in the Connections category; unmatched sessions go to
        the Discovered (unmatched) category at the bottom of the tree.
        Both are tagged ``"discovered:<pid>"`` in UserRole so click
        handlers route uniformly via :meth:`get_discovered_session`.
        """
        self._discovered = list(sessions)
        self._render()

    def get_discovered_session(self, pid: int) -> DiscoveredSession | None:
        """Look up a previously-set DiscoveredSession by its pid."""
        for s in self._discovered:
            if s.pid == pid:
                return s
        return None

    @staticmethod
    def _format_discovered_label(s: DiscoveredSession) -> str:
        """Produce the visible row text for an unmatched discovered session.

        Matched sessions render as compact sub-rows under their Connection
        (see :meth:`_format_subrow_label`); this format is used only for
        the unmatched category, where there is no parent context.
        """
        if s.kind == "claude-local":
            return f"claude in {_short_path(s.cwd)}  [PID {s.pid}]"
        if s.kind in ("claude-remote", "ssh-shell"):
            who = f"{s.user}@{s.host}" if s.user else s.host
            kind_label = "ssh+claude" if s.kind == "claude-remote" else "ssh"
            return f"{kind_label} {who}  [PID {s.pid}]"
        return f"{s.cmdline[:60]}  [PID {s.pid}]"

    @staticmethod
    def _format_discovered_tooltip(s: DiscoveredSession) -> str:
        lines = [
            f"PID: {s.pid}",
            f"Kind: {s.kind}",
        ]
        # For SSH-based sessions the local cwd is just where the user
        # happened to be when running ssh — it's NOT the remote cwd that
        # would match a Connection's project_folder. Surface remote_cwd
        # (populated by the correlation probe) prominently when known.
        remote_cwd = getattr(s, "remote_cwd", "") or ""
        if remote_cwd:
            lines.append(f"Remote cwd: {remote_cwd}")
        if s.cwd:
            lines.append(
                f"Local cwd (ssh client): {s.cwd}"
                if s.kind in ("claude-remote", "ssh-shell")
                else f"Working dir: {s.cwd}"
            )
        if s.host:
            lines.append(f"Host: {s.host}")
        if s.user:
            lines.append(f"User: {s.user}")
        if s.tty:
            lines.append(f"TTY: {s.tty}")
        if s.cmdline:
            lines.append(f"Command: {s.cmdline}")
        if s.suggested_connection_id:
            lines.append(f"Suggested adopt-as: {s.suggested_connection_id}")
        return "\n".join(lines)

    @property
    def tree(self) -> QTreeWidget:
        """The underlying QTreeWidget (for testing and external access)."""
        return self._tree


def _short_path(path: str) -> str:
    """Replace ``$HOME`` prefix with ``~`` for compact display."""
    if not path:
        return ""
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path
