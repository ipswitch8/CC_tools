# -*- coding: utf-8 -*-
"""
cpsm.ui.main_window — QMainWindow: menu / toolbar / sidebar / tabs / inspector / status bar.

Spec sections: §3.1, §3.2, §3.3

Layout
------
┌─ Menu Bar: File / Edit / View / Sessions / Tools / Help ─────────┐
├─ Toolbar: New Conn │ New Group │ Launch │ Stop │ Validate │ Save  │
├─ Sidebar (QTreeView, dock left)  ── Main Content (QTabWidget) ───┤
│  ▼ Connections                       Tab: Connections             │
│    🔗 web01                          Tab: Groups                  │
│    💻 dotfiles                       Tab: Screens                 │
│    ⌨ bastion                         Tab: Active Sessions         │
│    $ scratch                                                       │
│  ▼ Groups                                                        │
│  ▼ Layouts                                                        │
│  ▼ Scenes                                                        │
│                                  ── Inspector (QDockWidget right)│
├─ Status bar: Backend │ N active │ ✓ valid │ N conflicts ─────────┤
└──────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from PySide6.QtCore import QByteArray, QMimeData, QPoint, Qt
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QDrag,
    QKeySequence,
    QShortcut,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDockWidget,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from cpsm.data.schema import (
    ClaudeLocalConnection,
    ClaudeRemoteConnection,
    Connection,
    CpsmDocument,
    CustomConnection,
    Group,
    LocalShellConnection,
    Pane,
    ScreenLayout,
    SshShellConnection,
)

# ---------------------------------------------------------------------------
# Drag-drop diagnostic logger (cpsm.ui.dragdrop)
# ---------------------------------------------------------------------------

_dd_log = logging.getLogger("cpsm.ui.dragdrop")

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

# ---------------------------------------------------------------------------
# Tab indices (Layouts tab removed — now 4 tabs)
# ---------------------------------------------------------------------------

TAB_CONNECTIONS = 0
TAB_GROUPS = 1
TAB_SCREENS = 2
TAB_ACTIVE_SESSIONS = 3


def _glyph_for(conn: Connection) -> str:
    """Return the Unicode glyph for the connection's launch_profile."""
    return _PROFILE_GLYPHS.get(conn.launch_profile, "?")


# ---------------------------------------------------------------------------
# _MembersListWidget — proper QListWidget subclass for drag source
# ---------------------------------------------------------------------------


class _MembersListWidget(QListWidget):
    """QListWidget subclass that overrides startDrag to emit MIME_CONNECTION_ID.

    Using a proper subclass instead of instance-level method assignment because
    PySide6 does not reliably honour Python-level overrides of Qt virtual
    methods when assigned directly to an instance.
    """

    def startDrag(self, supported_actions: Qt.DropAction) -> None:
        """Build a QDrag with MIME_CONNECTION_ID payload and execute it."""
        from cpsm.ui.widgets.screen_map import MIME_CONNECTION_ID

        item = self.currentItem()
        if item is None:
            return
        conn_id: str = item.data(Qt.ItemDataRole.UserRole) or ""
        if not conn_id:
            return

        _dd_log.info("members_list.startDrag: conn_id=%s", conn_id)

        mime = QMimeData()
        mime.setData(MIME_CONNECTION_ID, QByteArray(conn_id.encode("utf-8")))

        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)


# ---------------------------------------------------------------------------
# MainWindow
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    """CPSM main application window (§3.1).

    Parameters
    ----------
    services:
        Namespace produced by ``cpsm.app._make_services()`` or ``cpsm.cli._make_services()``.
        May be *None* for bare-minimum testing (stub document is used).
    document:
        Pre-loaded ``CpsmDocument``.  When *None* the window starts with an
        empty document; call :py:meth:`load_document` to populate the UI.
    parent:
        Optional Qt parent widget.
    """

    def __init__(
        self,
        services: SimpleNamespace | None = None,
        document: CpsmDocument | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._services = services
        self._document: CpsmDocument = document if document is not None else CpsmDocument()

        # Fix #4 — LayoutController (wired after UI is built)
        self._layout_controller: Any = None
        if self._services is not None:
            try:
                from cpsm.controllers.layout_controller import LayoutController

                self._layout_controller = LayoutController(
                    config=self._services.config,
                    layout=self._services.layout,
                    session=self._services.session,
                    backend=self._services.session._backend,
                    templates=self._services.templates,
                    parent=self,
                )
            except Exception:
                self._layout_controller = None

        # Wire StatusPoller updates → canvas + sidebar refresh.
        if self._services is not None:
            poller = getattr(self._services, "status_poller", None)
            if poller is not None and hasattr(poller, "poll_complete"):
                import contextlib
                with contextlib.suppress(Exception):
                    poller.poll_complete.connect(self._on_status_poll_complete)

        self.setObjectName("main_window")
        self.setWindowTitle("CPSM — Cross-Platform Session Manager")
        self.setAccessibleName("CPSM Main Window")
        self.setAccessibleDescription(
            "Main application window for the Cross-Platform Session Manager"
        )

        # Minimum sensible size
        self.resize(1280, 800)

        # Build UI in order
        self._create_actions()
        self._create_menu_bar()
        self._create_toolbar()
        self._create_sidebar()
        self._create_tab_widget()
        self._create_inspector_dock()
        self._create_status_bar()
        self._wire_shortcuts()

        # Populate from initial document
        self.load_document(self._document)

    # ------------------------------------------------------------------
    # Actions (§3.3)
    # ------------------------------------------------------------------

    def _create_actions(self) -> None:
        """Create all QActions with shortcuts and objectNames."""

        def _make(
            name: str,
            text: str,
            shortcut: str | None = None,
            tooltip: str = "",
            accessible_name: str = "",
        ) -> QAction:
            action = QAction(text, self)
            action.setObjectName(name)
            # QAction.setWhatsThis serves as the accessible description
            action.setWhatsThis(accessible_name or text)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
            if tooltip:
                action.setToolTip(tooltip)
            # Placeholder slot — real implementation wired in later phases
            action.triggered.connect(lambda _checked=False, n=name: print(f"TODO: {n}"))
            return action

        # File menu
        self.action_new_connection = _make(
            "action_new_connection",
            "&New Connection…",
            "Ctrl+N",
            "Create a new connection",
            "New Connection",
        )

        self.action_new_group = _make(
            "action_new_group",
            "New &Group…",
            "Ctrl+Shift+N",
            "Create a new group",
            "New Group",
        )
        self.action_open_config = _make(
            "action_open_config",
            "&Open Config…",
            "Ctrl+O",
            "Open a .cpsm.yaml file",
            "Open Config",
        )
        self.action_save_config = _make(
            "action_save_config",
            "&Save Config",
            "Ctrl+S",
            "Save the current configuration",
            "Save Config",
        )
        self.action_import = _make(
            "action_import",
            "&Import legacy CC_multi config…",
            "Ctrl+I",
            "Import a legacy .claude-projects.yaml from CC_multi.sh "
            "(only useful when migrating from the old shell-based tool).",
            "Import legacy CC_multi config",
        )
        self.action_quit = _make(
            "action_quit",
            "&Quit",
            "Ctrl+Q",
            "Exit CPSM",
            "Quit",
        )
        self.action_quit.triggered.disconnect()
        self.action_quit.triggered.connect(self.close)

        self.action_new_connection.triggered.disconnect()
        self.action_new_connection.triggered.connect(
            lambda _checked=False: self._open_connection_editor(None)
        )
        self.action_new_group.triggered.disconnect()
        self.action_new_group.triggered.connect(
            lambda _checked=False: self._open_group_editor(None)
        )

        # Edit menu
        self.action_find = _make(
            "action_find",
            "&Find…",
            "Ctrl+F",
            "Find a connection or group",
            "Find",
        )

        # View menu
        self.action_toggle_inspector = _make(
            "action_toggle_inspector",
            "Toggle &Inspector",
            "F4",
            "Show or hide the inspector dock",
            "Toggle Inspector",
        )
        self.action_toggle_inspector.triggered.disconnect()
        self.action_toggle_inspector.triggered.connect(self._toggle_inspector)

        self.action_toggle_live_preview = _make(
            "action_toggle_live_preview",
            "Toggle &Live/Preview",
            "Ctrl+M",
            "Toggle between Live and Preview mode",
            "Toggle Live/Preview",
        )

        # Sessions menu
        self.action_launch = _make(
            "action_launch",
            "&Launch Selected",
            "F5",
            "Launch the selected connection or group",
            "Launch Selected",
        )
        self.action_launch_scene = _make(
            "action_launch_scene",
            "Launch &Scene…",
            "Ctrl+Shift+L",
            "Launch a scene (all groups)",
            "Launch Scene",
        )
        self.action_stop = _make(
            "action_stop",
            "&Stop Selected",
            "Shift+F5",
            "Stop the selected session",
            "Stop Selected",
        )
        self.action_reconnect = _make(
            "action_reconnect",
            "&Reconnect Selected",
            "Ctrl+R",
            "Reconnect the selected session",
            "Reconnect Selected",
        )
        self.action_validate = _make(
            "action_validate",
            "&Validate Config",
            "Ctrl+L",
            "Validate the configuration",
            "Validate Config",
        )

        # Tools menu
        self.action_launcher_templates = _make(
            "action_launcher_templates",
            "Launcher &Templates…",
            "Ctrl+T",
            "Edit launcher templates",
            "Launcher Templates",
        )
        self.action_manage_keys = _make(
            "action_manage_keys",
            "&SSH Keys…",
            tooltip="Manage SSH keys",
            accessible_name="SSH Keys",
        )
        self.action_settings = _make(
            "action_settings",
            "&Settings…",
            tooltip="Application settings",
            accessible_name="Settings",
        )

        # Help menu
        self.action_about = _make(
            "action_about",
            "&About CPSM…",
            tooltip="Show the About dialog",
            accessible_name="About CPSM",
        )
        self.action_about.triggered.disconnect()
        self.action_about.triggered.connect(self._show_about)

        # Wire Fix #1 handlers (all _make'd actions are now defined)
        self.action_save_config.triggered.disconnect()
        self.action_save_config.triggered.connect(self._on_save_config_action)

        self.action_open_config.triggered.disconnect()
        self.action_open_config.triggered.connect(self._on_open_config_action)

        self.action_import.triggered.disconnect()
        self.action_import.triggered.connect(self._on_import_action)

        self.action_validate.triggered.disconnect()
        self.action_validate.triggered.connect(self._on_validate_action)

        self.action_launch.triggered.disconnect()
        self.action_launch.triggered.connect(self._on_launch_action)

        self.action_stop.triggered.disconnect()
        self.action_stop.triggered.connect(self._on_stop_action)

        self.action_reconnect.triggered.disconnect()
        self.action_reconnect.triggered.connect(self._on_reconnect_action)

        self.action_launch_scene.triggered.disconnect()
        self.action_launch_scene.triggered.connect(self._on_launch_scene_action)

        self.action_launcher_templates.triggered.disconnect()
        self.action_launcher_templates.triggered.connect(self._on_launcher_templates_action)

        self.action_find.triggered.disconnect()
        self.action_find.triggered.connect(self._on_find_action)

        self.action_toggle_live_preview.triggered.disconnect()
        self.action_toggle_live_preview.triggered.connect(self._on_toggle_live_preview_action)

        self.action_manage_keys.triggered.disconnect()
        self.action_manage_keys.triggered.connect(self._on_manage_keys_action)

        self.action_settings.triggered.disconnect()
        self.action_settings.triggered.connect(self._on_settings_action)

    # ------------------------------------------------------------------
    # Menu bar
    # ------------------------------------------------------------------

    def _create_menu_bar(self) -> None:
        """Build the menu bar with all top-level menus."""
        menubar: QMenuBar = self.menuBar()
        menubar.setObjectName("menubar_main")
        menubar.setAccessibleName("Main Menu Bar")
        menubar.setAccessibleDescription("Application main menu bar")

        # File
        menu_file: QMenu = menubar.addMenu("&File")
        menu_file.setObjectName("menu_file")
        menu_file.setAccessibleName("File Menu")
        menu_file.setAccessibleDescription("File operations")
        menu_file.addAction(self.action_new_connection)
        menu_file.addAction(self.action_new_group)
        menu_file.addSeparator()
        menu_file.addAction(self.action_open_config)
        # Round-late tweak: every change auto-persists, so Save Config is
        # no longer a user action. The QAction is still constructed for
        # backward-compat with any tests/handlers that look it up.
        menu_file.addSeparator()
        menu_file.addAction(self.action_import)
        menu_file.addSeparator()
        menu_file.addAction(self.action_quit)

        # Edit
        menu_edit: QMenu = menubar.addMenu("&Edit")
        menu_edit.setObjectName("menu_edit")
        menu_edit.setAccessibleName("Edit Menu")
        menu_edit.setAccessibleDescription("Edit operations")
        menu_edit.addAction(self.action_find)

        # View
        menu_view: QMenu = menubar.addMenu("&View")
        menu_view.setObjectName("menu_view")
        menu_view.setAccessibleName("View Menu")
        menu_view.setAccessibleDescription("View options")
        menu_view.addAction(self.action_toggle_inspector)
        # Round-late tweak: Live/Preview was merged earlier — the toggle
        # action no longer has a meaningful effect, so it's hidden from
        # the menu (the QAction is still constructed for tests).

        # Sessions
        menu_sessions: QMenu = menubar.addMenu("&Sessions")
        menu_sessions.setObjectName("menu_sessions")
        menu_sessions.setAccessibleName("Sessions Menu")
        menu_sessions.setAccessibleDescription("Session management operations")
        menu_sessions.addAction(self.action_launch)
        # Scenes were removed in an earlier round; Launch Scene is no
        # longer reachable, so the menu entry is suppressed (QAction
        # kept for backward compatibility with tests).
        menu_sessions.addSeparator()
        menu_sessions.addAction(self.action_stop)
        menu_sessions.addAction(self.action_reconnect)
        # Validate Config: auto-save means the document is always
        # validated at write-time; an explicit action is redundant.

        # Tools
        menu_tools: QMenu = menubar.addMenu("&Tools")
        menu_tools.setObjectName("menu_tools")
        menu_tools.setAccessibleName("Tools Menu")
        menu_tools.setAccessibleDescription("Tools and settings")
        menu_tools.addAction(self.action_launcher_templates)
        menu_tools.addSeparator()
        menu_tools.addAction(self.action_manage_keys)
        menu_tools.addAction(self.action_settings)

        # Help
        menu_help: QMenu = menubar.addMenu("&Help")
        menu_help.setObjectName("menu_help")
        menu_help.setAccessibleName("Help Menu")
        menu_help.setAccessibleDescription("Help and about information")
        menu_help.addAction(self.action_about)

    # ------------------------------------------------------------------
    # Toolbar
    # ------------------------------------------------------------------

    def _create_toolbar(self) -> None:
        """Build the main toolbar."""
        toolbar: QToolBar = QToolBar("Main Toolbar", self)
        toolbar.setObjectName("toolbar_main")
        toolbar.setAccessibleName("Main Toolbar")
        toolbar.setAccessibleDescription("Main application toolbar with common actions")
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)

        _toolbar_actions = [
            (self.action_new_connection, "toolbtn_new_connection"),
            (self.action_new_group, "toolbtn_new_group"),
            (None, None),  # separator
            (self.action_launch, "toolbtn_launch"),
            (self.action_stop, "toolbtn_stop"),
            (None, None),  # separator
            (self.action_validate, "toolbtn_validate"),
            (self.action_save_config, "toolbtn_save_config"),
        ]
        from PySide6.QtWidgets import QToolButton

        for action, btn_name in _toolbar_actions:
            if action is None:
                toolbar.addSeparator()
            else:
                toolbar.addAction(action)
                btn = toolbar.widgetForAction(action)
                if isinstance(btn, QToolButton) and btn_name:
                    btn.setObjectName(btn_name)
                    btn.setAccessibleName(
                        btn_name.replace("toolbtn_", "").replace("_", " ").title()
                    )
                    btn.setAccessibleDescription(action.toolTip() or action.text())

        self._toolbar_main = toolbar

    # ------------------------------------------------------------------
    # Sidebar
    # ------------------------------------------------------------------

    def _create_sidebar(self) -> None:
        """Build the left sidebar dock containing the session tree."""
        from cpsm.ui.widgets.session_list import SessionListWidget

        self._session_list = SessionListWidget(parent=self)

        # Wire sidebar double-click
        self._session_list.tree.itemDoubleClicked.connect(self._on_sidebar_double_clicked)
        # Wire sidebar single-click selection to populate the Inspector dock
        # when a Connection is selected (Round B: Connections tab is gone).
        self._session_list.tree.currentItemChanged.connect(self._on_sidebar_item_selected)
        # Round C: Ctrl-click a Connection while a Group is active to toggle
        # the connection's membership in that group.
        self._session_list.tree.itemClicked.connect(self._on_sidebar_item_clicked)

        # Wire sidebar right-click context menu
        self._session_list.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._session_list.tree.customContextMenuRequested.connect(self._on_sidebar_context_menu)

        dock: QDockWidget = QDockWidget("Connections", self)
        dock.setObjectName("dock_sidebar")
        dock.setAccessibleName("Sidebar Dock")
        dock.setAccessibleDescription("Sidebar containing connection and group tree")
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        dock.setWidget(self._session_list)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        self._dock_sidebar = dock

    # ------------------------------------------------------------------
    # Main tab widget
    # ------------------------------------------------------------------

    def _create_tab_widget(self) -> None:
        """Build the central QTabWidget — Round B reduced this to a single
        Screens tab. The Connections, Groups and Active Sessions widgets are
        still constructed (they back models referenced elsewhere and by tests)
        but they are no longer presented as tabs.
        """
        tabs = QTabWidget(self)
        tabs.setObjectName("tabwidget_main")
        tabs.setAccessibleName("Main Tab Widget")
        tabs.setAccessibleDescription("Main content area — Screens canvas")

        # Build the Connections / Groups / Active Sessions widgets as
        # standalone backing widgets so that ``self._connections_tab`` etc.
        # are still valid references for other code, but DO NOT add them to
        # the tab widget. They are hidden so they don't render at (0, 0).
        self._connections_tab = self._build_connections_tab()
        self._connections_tab.setObjectName("tab_connections")
        self._connections_tab.setParent(self)
        self._connections_tab.hide()

        groups_tab = self._build_groups_tab()
        groups_tab.setObjectName("tab_groups")
        groups_tab.setParent(self)
        groups_tab.hide()
        self._groups_tab = groups_tab

        active_tab = self._build_active_sessions_tab()
        active_tab.setObjectName("tab_active_sessions")
        active_tab.setParent(self)
        active_tab.hide()
        self._active_sessions_tab = active_tab

        # The only tab actually shown:
        screen_map_tab = self._build_screen_map_tab()
        screen_map_tab.setObjectName("tab_screen_map")
        screen_map_tab.setAccessibleName("Screens Tab")
        screen_map_tab.setAccessibleDescription("Visual screen mapping for connections")
        # Empty tab text + hide the tab bar (only one tab now)
        tabs.addTab(screen_map_tab, "")
        tabs.tabBar().hide()

        # Build the layouts list model and view (not shown as a tab, but kept
        # for backward-compat with code that accesses _layouts_model / _layouts_list)
        self._build_layouts_backing_model()

        self._tabs = tabs
        self.setCentralWidget(tabs)

    def _build_placeholder_tab(self, title: str, subtitle: str) -> QWidget:
        """Return a simple placeholder QWidget labelled *subtitle*."""
        w = QWidget()
        layout = QVBoxLayout(w)
        label = QLabel(f"<h2>{title}</h2><p><i>{subtitle}</i></p>")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setObjectName(f"label_placeholder_{title.lower().replace(' ', '_')}")
        label.setAccessibleName(f"{title} placeholder")
        label.setAccessibleDescription(subtitle)
        layout.addWidget(label)
        return w

    def _build_screen_map_tab(self) -> QWidget:
        """Build the Screens tab with Live/Preview mode toggle, group picker, and Save."""
        from cpsm.ui.widgets.screen_map import ScreenMapWidget

        connection_lookup = self._lookup_connection_by_id

        # Outer container
        container = QWidget()
        container.setObjectName("screen_map_container")
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(4, 4, 4, 4)
        container_layout.setSpacing(4)

        # ---- Top mode bar ----
        mode_bar = QWidget()
        mode_bar.setObjectName("widget_screens_mode_bar")
        mode_bar.setAccessibleName("Screens Mode Bar")
        mode_bar_layout = QHBoxLayout(mode_bar)
        mode_bar_layout.setContentsMargins(0, 0, 0, 0)
        mode_bar_layout.setSpacing(8)

        # Save button
        # Hidden holder for widgets that exist for test/back-compat but
        # must never appear on screen (Save Layout button, Live/Preview
        # radios, hidden combos). Built first so subsequent widgets can
        # reference it as their parent.
        self._mode_radio_holder = QWidget(self)
        self._mode_radio_holder.setObjectName("widget_mode_radio_holder")
        self._mode_radio_holder.hide()

        # Round-late tweak: Save Layout button removed from the visible
        # UI now that every layout edit auto-persists. Widget is parented
        # to the hidden holder so test / programmatic clicks still work.
        btn_save = QPushButton("Save Layout…", self._mode_radio_holder)
        btn_save.setObjectName("btn_screens_save")
        btn_save.setVisible(False)
        self._btn_screens_save = btn_save

        mode_bar_layout.addStretch()

        # Live / Preview radio buttons — kept as hidden state so existing
        # `_radio_screens_preview.isChecked()` callers continue to work.

        self._screens_mode_group = QButtonGroup(self)
        self._screens_mode_group.setObjectName("radio_group_screens_mode")

        radio_live = QRadioButton("Live", self._mode_radio_holder)
        radio_live.setObjectName("radio_screens_live")
        radio_live.setChecked(False)
        radio_live.setVisible(False)

        radio_preview = QRadioButton("Preview", self._mode_radio_holder)
        radio_preview.setObjectName("radio_screens_preview")
        radio_preview.setChecked(True)
        radio_preview.setVisible(False)

        self._screens_mode_group.addButton(radio_live)
        self._screens_mode_group.addButton(radio_preview)
        self._radio_screens_live = radio_live
        self._radio_screens_preview = radio_preview

        # Hidden helpers (Round B removed them from the visible UI; widgets
        # remain so tests & programmatic flows that look them up still work).
        # All parented to the hidden _mode_radio_holder.
        combo_group = QComboBox(self._mode_radio_holder)
        combo_group.setObjectName("combo_screens_group")
        combo_group.setVisible(False)
        self._combo_screens_group: QComboBox = combo_group

        btn_new_layout = QPushButton("New Layout", self._mode_radio_holder)
        btn_new_layout.setObjectName("button_screens_new_layout")
        btn_new_layout.setVisible(False)
        self._btn_screens_new_layout: QPushButton = btn_new_layout

        combo_legacy = QComboBox(self._mode_radio_holder)
        combo_legacy.setObjectName("combo_screen_map_layout")
        combo_legacy.setVisible(False)
        self._screen_map_layout_combo: QComboBox = combo_legacy

        container_layout.addWidget(mode_bar)

        # ---- Empty-state label ----
        empty_lbl = QLabel(
            "No screen layouts defined yet.\n"
            "Create a Group (Ctrl+Shift+N) — its default layout will appear here automatically.\n"
            "Or right-click on this canvas to add monitors and viewports manually."
        )
        empty_lbl.setObjectName("label_screen_map_empty")
        empty_lbl.setAccessibleName("Screen Map Empty State")
        empty_lbl.setAccessibleDescription(
            "Shown when no screen layouts are defined in the document"
        )
        empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_lbl.setWordWrap(True)
        empty_lbl.setStyleSheet("color: gray; font-style: italic;")
        empty_lbl.hide()  # hidden by default; canvas itself communicates empty state
        container_layout.addWidget(empty_lbl)
        self._screen_map_empty_label: QLabel = empty_lbl

        # ---- Canvas area ----
        # Round C: the in-tab group-members list was removed; the left
        # sidebar is the drag source for connections now. The members list
        # widget is kept as a hidden attribute so existing test/handler
        # references still resolve.
        members_list = _MembersListWidget(self._mode_radio_holder)
        members_list.setObjectName("list_screens_group_members")
        members_list.setVisible(False)
        self._list_screens_group_members: _MembersListWidget = members_list

        widget = ScreenMapWidget(
            monitor_service=getattr(self._services, "monitor_service", None)
            if self._services
            else None,
            connection_lookup=connection_lookup,
            status_lookup=self._lookup_connection_status,
        )
        widget.setObjectName("widget_screen_map")
        widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        container_layout.addWidget(widget, 1)
        self._screen_map_widget = widget

        # ---- Wire right-click on the canvas view ----
        widget.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        widget.view.customContextMenuRequested.connect(self._on_screens_canvas_context_menu)

        # Fix #4 — connect ScreenMapWidget drop signals to LayoutController
        widget.drop_connection_requested.connect(self._on_screen_map_drop_connection)
        widget.drop_pane_requested.connect(self._on_screen_map_drop_pane)
        widget.drop_connection_on_viewport_requested.connect(
            self._on_screen_map_drop_connection_on_viewport
        )
        widget.drop_pane_on_viewport_requested.connect(
            self._on_screen_map_drop_pane_on_viewport
        )
        widget.pane_clicked.connect(self._on_screen_map_pane_clicked)

        # ---- Wire signals ----
        combo_legacy.currentIndexChanged.connect(self._on_screen_map_layout_selected)
        # Radios are hidden but still drive mode-related code paths (combo
        # visibility, members list refresh). Round C will remove the dual-mode
        # logic entirely; for now the toggled.connect keeps things working.
        radio_live.toggled.connect(self._on_screens_mode_changed)
        radio_preview.toggled.connect(self._on_screens_mode_changed)
        combo_group.currentIndexChanged.connect(self._on_screens_group_selected)
        btn_save.clicked.connect(self._on_screens_save_clicked)
        btn_new_layout.clicked.connect(self._on_screens_new_layout_clicked)

        return container

    def _build_active_sessions_tab(self) -> QWidget:
        """Build the Active Sessions tab using the real ActiveSessionsWidget (Phase 14)."""
        from cpsm.ui.widgets.active_sessions import ActiveSessionsWidget

        # Use the services' status poller if available; else create an empty placeholder
        # widget that won't update until the user has launched something via the GUI loop.
        poller = getattr(self._services, "status_poller", None) if self._services else None
        if poller is None:
            # Stub poller — widget receives no signals until a real poller is wired.
            from PySide6.QtCore import QObject, Signal

            class _StubPoller(QObject):
                poll_complete = Signal(list)
                state_changed = Signal(object)

            poller = _StubPoller()

        widget = ActiveSessionsWidget(poller=poller)
        widget.setObjectName("widget_active_sessions")
        self._active_sessions_widget = widget
        return widget

    def _lookup_connection_by_id(self, connection_id: str) -> Connection | None:
        """Connection lookup by id — used by ScreenMapWidget to render pane labels."""
        if self._document is None:
            return None
        for conn in self._document.connections:
            if conn.id == connection_id:
                return conn
        return None

    def _on_status_poll_complete(self, _statuses: Any) -> None:
        """StatusPoller has a fresh snapshot — re-render canvas + sidebar so
        green/amber/blue pane borders and sidebar dots reflect reality."""
        # Re-render the canvas: re-running set_layout with the current data
        # walks the panes and re-evaluates _status_lookup for each.
        if hasattr(self, "_screen_map_widget") and self._screen_map_widget._layout_data:
            self._screen_map_widget.set_layout(
                self._screen_map_widget._layout_data,
                self._query_live_monitors(),
            )
        # Refresh sidebar status dots (member highlights also re-apply since
        # they touch the same items).
        self._refresh_sidebar_status_dots()

    def _refresh_sidebar_status_dots(self) -> None:
        """Prefix each Connection item's text with a status dot:
        🟢 connected, 🟠 error, otherwise no prefix.
        """
        if not hasattr(self, "_session_list") or self._document is None:
            return
        cat = self._session_list._cat_connections
        for i in range(cat.childCount()):
            item = cat.child(i)
            conn_id: str = item.data(0, Qt.ItemDataRole.UserRole) or ""
            if not conn_id:
                continue
            conn = self._find_connection_by_id(conn_id)
            base = (conn.name or conn_id) if conn is not None else conn_id
            status = self._lookup_connection_status(conn_id)
            if status == "connected":
                item.setText(0, f"🟢 {base}")
            elif status == "error":
                item.setText(0, f"🟠 {base}")
            else:
                item.setText(0, base)

    def _lookup_connection_status(self, connection_id: str) -> str:
        """Return one of {"connected", "error", "disconnected"} for
        *connection_id*.

        Round-late: the launch flow now creates one tmux session per
        monitor (``cpsm-group-<group>-mon-<idx>``), with one window per
        viewport and one pane per layout pane (in tree-leaf order). To
        find a connection's status we:
          1. Walk all groups' layouts; for each leaf whose connection_id
             matches, compute the expected ``(session_name,
             window_index, pane_index)``.
          2. Consult the StatusPoller snapshot for that triple.
          3. Map PaneState → external status.
        Connected states (CONNECTED/STALE/EMPTY_SLOT) → "connected";
        ERROR → "error"; everything else → "disconnected".
        """
        if self._services is None or self._document is None:
            return "disconnected"
        poller = getattr(self._services, "status_poller", None)
        if poller is None:
            return "disconnected"
        snap = getattr(poller, "last_snapshot", None)
        if not snap:
            return "disconnected"

        from cpsm.data.schema import _flatten_split_tree_leaves

        # Index the snapshot by (session, window_index, pane_index)
        snap_by_key: dict[tuple[str, int, int], Any] = {}
        for st in snap:
            sess = getattr(st, "session", None)
            wi = getattr(st, "window_index", -1)
            pi = getattr(st, "pane_index", -1)
            if sess and wi >= 0 and pi >= 0:
                snap_by_key[(sess, wi, pi)] = st

        result_priority = {"connected": 3, "error": 2, "disconnected": 1}
        best = "disconnected"

        for grp in self._document.groups:
            if not grp.default_layout_id:
                continue
            layout = self._find_layout_by_id(grp.default_layout_id)
            if layout is None:
                continue
            for mon_idx, monitor in enumerate(layout.monitors):
                session_name = f"cpsm-group-{grp.id}-mon-{mon_idx}"
                for vp_idx, vp in enumerate(monitor.viewports):
                    if vp.split_tree is not None:
                        leaves = _flatten_split_tree_leaves(vp.split_tree)
                    else:
                        leaves = list(vp.panes)
                    for leaf_idx, pane in enumerate(leaves):
                        if pane.connection_id != connection_id:
                            continue
                        key = (session_name, vp_idx, leaf_idx)
                        st = snap_by_key.get(key)
                        if st is None:
                            continue
                        state = getattr(st, "state", None)
                        sv = getattr(state, "value", state)
                        if sv in ("connected", "stale", "empty_slot"):
                            mapped = "connected"
                        elif sv == "error":
                            mapped = "error"
                        else:
                            mapped = "disconnected"
                        if result_priority[mapped] > result_priority[best]:
                            best = mapped
        return best

    def _build_connections_tab(self) -> QWidget:
        """Return the Connections tab widget (QTreeView over document connections)."""
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(4, 4, 4, 4)

        tree = QTreeView(w)
        tree.setObjectName("treeview_connections")
        tree.setAccessibleName("Connections Tree")
        tree.setAccessibleDescription("Tree view of all configured connections")
        tree.setUniformRowHeights(True)
        tree.setAlternatingRowColors(True)
        tree.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)

        model = QStandardItemModel(0, 3, tree)
        model.setHorizontalHeaderLabels(["Connection", "Profile", "Host / Folder"])
        tree.setModel(model)
        tree.selectionModel().selectionChanged.connect(self._on_connection_selected)
        tree.doubleClicked.connect(self._on_connection_double_clicked)
        tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tree.customContextMenuRequested.connect(self._on_connections_tab_context_menu)

        layout.addWidget(tree)
        self._connections_tree = tree
        self._connections_model = model
        return w

    def _build_groups_tab(self) -> QWidget:
        """Return the Groups tab widget (QListView over document groups)."""
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(4, 4, 4, 4)

        view = QListView(w)
        view.setObjectName("listview_groups")
        view.setAccessibleName("Groups List")
        view.setAccessibleDescription("List view of all configured groups")
        view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        view.setUniformItemSizes(True)
        view.setAlternatingRowColors(True)

        model = QStandardItemModel(0, 1, view)
        view.setModel(model)
        view.doubleClicked.connect(self._on_group_double_clicked)
        view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        view.customContextMenuRequested.connect(self._on_groups_tab_context_menu)

        layout.addWidget(view)
        self._groups_list = view
        self._groups_model = model
        return w

    def _build_layouts_tab(self) -> QWidget:
        """Return the Layouts tab widget (QListView over document screen_layouts).

        Note: This tab is not added to the main tab widget since Change 2 removed
        the Layouts tab.  The returned widget is kept for backward compatibility
        with code that calls this method directly.
        """
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(4, 4, 4, 4)

        view = QListView(w)
        view.setObjectName("listview_layouts")
        view.setAccessibleName("Layouts List")
        view.setAccessibleDescription("List view of all configured screen layouts")
        view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        view.setUniformItemSizes(True)
        view.setAlternatingRowColors(True)

        model = QStandardItemModel(0, 1, view)
        view.setModel(model)
        view.doubleClicked.connect(self._on_layout_double_clicked)
        view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        view.customContextMenuRequested.connect(self._on_layouts_tab_context_menu)

        layout.addWidget(view)
        self._layouts_list = view
        self._layouts_model = model
        return w

    def _build_layouts_backing_model(self) -> None:
        """Create the layouts model/view as standalone (not added to tab widget).

        Exists for backward-compat: tests may access ``_layouts_model`` and
        ``_layouts_list``.  The widget is parented to ``self`` so Qt owns it.
        """
        w = QWidget(self)  # parented to self — not shown, just owned
        w.setObjectName("widget_layouts_backing")
        # Child widgets default to visible. This backing widget exists only
        # to keep the QListView/QStandardItemModel alive for tests, so it
        # must NOT be shown — otherwise it sits at (0,0) of the main window
        # and overlaps the menu bar with whatever text its model contains.
        w.hide()
        layout_vlayout = QVBoxLayout(w)
        layout_vlayout.setContentsMargins(4, 4, 4, 4)

        view = QListView(w)
        view.setObjectName("listview_layouts")
        view.setAccessibleName("Layouts List")
        view.setAccessibleDescription("List view of all configured screen layouts")
        view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        view.setUniformItemSizes(True)
        view.setAlternatingRowColors(True)

        model = QStandardItemModel(0, 1, view)
        view.setModel(model)
        view.doubleClicked.connect(self._on_layout_double_clicked)
        view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        view.customContextMenuRequested.connect(self._on_layouts_tab_context_menu)

        layout_vlayout.addWidget(view)
        self._layouts_list = view
        self._layouts_model = model
        # Keep a reference to prevent GC
        self._layouts_backing_widget = w

    # ------------------------------------------------------------------
    # Inspector dock
    # ------------------------------------------------------------------

    def _create_inspector_dock(self) -> None:
        """Build the right-hand Inspector QDockWidget."""
        dock = QDockWidget("Inspector", self)
        dock.setObjectName("dock_inspector")
        dock.setAccessibleName("Inspector Dock")
        dock.setAccessibleDescription("Inspector panel showing properties of the selected item")
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
            | QDockWidget.DockWidgetFeature.DockWidgetClosable
        )

        # Scroll area wrapping a form layout
        scroll = QScrollArea()
        scroll.setObjectName("inspector_scroll")
        scroll.setAccessibleName("Inspector Scroll Area")
        scroll.setAccessibleDescription("Scrollable area for inspector form fields")
        scroll.setWidgetResizable(True)

        container = QWidget()
        container.setObjectName("inspector_container")
        container.setAccessibleName("Inspector Container")
        container.setAccessibleDescription("Container widget for inspector form layout")
        form = QFormLayout(container)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        # Placeholder label when nothing is selected
        self._inspector_placeholder = QLabel("Select a connection to inspect.")
        self._inspector_placeholder.setObjectName("label_inspector_placeholder")
        self._inspector_placeholder.setAccessibleName("Inspector Placeholder")
        self._inspector_placeholder.setAccessibleDescription("Shown when no connection is selected")
        self._inspector_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._inspector_placeholder.setWordWrap(True)
        form.addRow(self._inspector_placeholder)

        # Named read-only field rows (hidden until selection)
        self._inspector_fields: dict[str, QLabel] = {}
        for field_name in (
            "name",
            "id",
            "launch_profile",
            "host",
            "user",
            "sudo_user",
            "port",
            "project_folder",
            "claude_options",
            "identity_file_ref",
            "jump_host",
            "tags",
            "notes",
        ):
            value_label = QLabel("—")
            value_label.setObjectName(f"inspector_field_{field_name}")
            value_label.setAccessibleName(f"Inspector {field_name.replace('_', ' ').title()}")
            value_label.setAccessibleDescription(f"Inspector field: {field_name}")
            # Wrapping causes the dock to compress field text; disable it so
            # the dock auto-sizes wider when content is long. _show_inspector_for
            # resizes the dock after each populate.
            value_label.setWordWrap(False)
            value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            caption = QLabel(field_name.replace("_", " ").title() + ":")
            caption.setObjectName(f"inspector_label_{field_name}")
            caption.setAccessibleName(f"Inspector {field_name} label")
            caption.setAccessibleDescription(f"Label for inspector field: {field_name}")
            form.addRow(caption, value_label)
            self._inspector_fields[field_name] = value_label
            caption.hide()
            value_label.hide()

        scroll.setWidget(container)
        dock.setWidget(scroll)

        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self._dock_inspector = dock
        self._inspector_form = form  # keep a direct reference to avoid widget-chain lookups

    # ------------------------------------------------------------------
    # Status bar
    # ------------------------------------------------------------------

    def _create_status_bar(self) -> None:
        """Build the status bar with four labelled sections."""
        bar: QStatusBar = self.statusBar()
        bar.setObjectName("statusbar_main")
        bar.setAccessibleName("Status Bar")
        bar.setAccessibleDescription(
            "Status bar showing backend, active session count, validation, and conflict state"
        )

        self._statusbar_backend = QLabel("Backend: tmux")
        self._statusbar_backend.setObjectName("statusbar_backend")
        self._statusbar_backend.setAccessibleName("Backend Status")
        self._statusbar_backend.setAccessibleDescription("Shows the active multiplexer backend")

        self._statusbar_active = QLabel("0 active")
        self._statusbar_active.setObjectName("statusbar_active")
        self._statusbar_active.setAccessibleName("Active Sessions Count")
        self._statusbar_active.setAccessibleDescription("Shows the number of active sessions")

        self._statusbar_valid = QLabel("✓ valid")
        self._statusbar_valid.setObjectName("statusbar_valid")
        self._statusbar_valid.setAccessibleName("Validation Status")
        self._statusbar_valid.setAccessibleDescription("Shows whether the configuration is valid")

        self._statusbar_conflicts = QLabel("0 conflicts")
        self._statusbar_conflicts.setObjectName("statusbar_conflicts")
        self._statusbar_conflicts.setAccessibleName("Conflict Count")
        self._statusbar_conflicts.setAccessibleDescription("Shows the number of layout conflicts")

        for widget in (
            self._statusbar_backend,
            self._statusbar_active,
            self._statusbar_valid,
            self._statusbar_conflicts,
        ):
            bar.addWidget(widget)

    # ------------------------------------------------------------------
    # Keyboard shortcuts (§3.3 — tab switching via QShortcut)
    # ------------------------------------------------------------------

    def _wire_shortcuts(self) -> None:
        """Wire Ctrl+1..4 tab-switching shortcuts (4 tabs after Layouts removal)."""
        self._tab_shortcuts: list[QShortcut] = []
        for idx in range(4):
            key = QKeySequence(f"Ctrl+{idx + 1}")
            sc = QShortcut(key, self)
            sc.setObjectName(f"shortcut_tab_{idx + 1}")
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            # Capture idx in closure
            sc.activated.connect(lambda _i=idx: self._tabs.setCurrentIndex(_i))
            self._tab_shortcuts.append(sc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_document(self, doc: CpsmDocument) -> None:
        """Populate the sidebar and connections tab from *doc*.

        Parameters
        ----------
        doc:
            The ``CpsmDocument`` to render.
        """
        self._document = doc
        self._session_list.load_document(doc)
        self._populate_connections_tree(doc)
        self._populate_groups_list(doc)
        self._populate_layouts_list(doc)
        self._refresh_screen_map(doc)
        self._refresh_screens_group_combo(doc)
        # Round C: every group must have a layout. Auto-create any that are
        # missing so the canvas always renders something when a group is
        # selected.
        for grp in list(doc.groups):
            self._ensure_group_has_layout(grp.id)
        # Trigger the active Screens-tab mode render so that newly-added groups
        # / layouts show up immediately. Without this the canvas keeps showing
        # the stale empty-state until the user manually toggles modes.
        self._refresh_screens_active_mode()
        # The Live/Preview radios are now hidden, so the toggled signal that
        # used to populate the members list never fires. Call it directly so
        # the list reflects the current group selection.
        if hasattr(self, "_refresh_screens_members_list"):
            self._refresh_screens_members_list()
        # Apply sidebar group-membership highlights based on the current
        # active group (default first group at startup).
        if hasattr(self, "_refresh_sidebar_membership_highlights"):
            self._refresh_sidebar_membership_highlights()
        # Initial status dots (will refresh on every poll_complete signal).
        if hasattr(self, "_refresh_sidebar_status_dots"):
            self._refresh_sidebar_status_dots()

    def _refresh_screens_active_mode(self) -> None:
        """Re-render whichever Screens tab mode is currently selected."""
        if not hasattr(self, "_radio_screens_preview"):
            return
        if self._radio_screens_preview.isChecked():
            self._refresh_screens_preview()
        else:
            self._refresh_screens_live()

    def _populate_connections_tree(self, doc: CpsmDocument) -> None:
        """Fill the connections QTreeView model from *doc.connections*."""
        model = self._connections_model
        model.removeRows(0, model.rowCount())

        for conn in doc.connections:
            glyph = _glyph_for(conn)
            display_name = conn.name or conn.id
            name_item = QStandardItem(f"{glyph} {display_name}")
            name_item.setData(conn.id, Qt.ItemDataRole.UserRole)
            name_item.setEditable(False)

            profile_item = QStandardItem(conn.launch_profile)
            profile_item.setEditable(False)

            # Host / folder depending on profile type
            host_folder = ""
            if isinstance(conn, ClaudeRemoteConnection | SshShellConnection):
                host_folder = conn.host
            elif isinstance(conn, ClaudeLocalConnection | LocalShellConnection):
                host_folder = conn.project_folder
            elif isinstance(conn, CustomConnection):
                host_folder = conn.custom_template_id
            host_item = QStandardItem(host_folder)
            host_item.setEditable(False)

            model.appendRow([name_item, profile_item, host_item])

        self._connections_tree.resizeColumnToContents(0)
        self._connections_tree.resizeColumnToContents(1)

    def _populate_groups_list(self, doc: CpsmDocument) -> None:
        """Fill the groups QListView model from *doc.groups*."""
        model = self._groups_model
        model.removeRows(0, model.rowCount())

        for grp in doc.groups:
            member_count = len(grp.members)
            display = f"{grp.id}  —  {grp.name}  ({member_count} member{'s' if member_count != 1 else ''})"
            item = QStandardItem(display)
            item.setData(grp.id, Qt.ItemDataRole.UserRole)
            item.setEditable(False)
            model.appendRow(item)

    def _populate_layouts_list(self, doc: CpsmDocument) -> None:
        """Fill the layouts QListView model from *doc.screen_layouts*."""
        model = self._layouts_model
        model.removeRows(0, model.rowCount())

        for sl in doc.screen_layouts:
            monitor_count = len(sl.monitors)
            viewport_count = sum(len(m.viewports) for m in sl.monitors)
            display = (
                f"{sl.id}  —  {sl.name}  "
                f"({monitor_count} monitor{'s' if monitor_count != 1 else ''}, "
                f"{viewport_count} viewport{'s' if viewport_count != 1 else ''})"
            )
            item = QStandardItem(display)
            item.setData(sl.id, Qt.ItemDataRole.UserRole)
            item.setEditable(False)
            model.appendRow(item)

    def _refresh_screen_map(self, doc: CpsmDocument) -> None:
        """Update the ScreenMapWidget for the given document."""
        if not hasattr(self, "_screen_map_widget"):
            return
        self._refresh_screen_map_tab(doc)

    def _refresh_screen_map_tab(self, doc: CpsmDocument | None = None) -> None:
        """Rebuild the layout selector combo and render the selected layout.

        When *doc* is None the current ``self._document`` is used.
        Called both from :meth:`_refresh_screen_map` (on full document load)
        and after a new layout is added via the Layouts tab.
        """
        if doc is None:
            doc = self._document
        if not hasattr(self, "_screen_map_widget"):
            return
        sm = self._screen_map_widget
        combo = getattr(self, "_screen_map_layout_combo", None)
        empty_lbl = getattr(self, "_screen_map_empty_label", None)

        if not doc.screen_layouts:
            # Show empty-state label; hide canvas
            if empty_lbl is not None:
                empty_lbl.show()
            sm.hide()
            return

        # Rebuild combo without triggering the slot repeatedly. The combo is
        # not visible after Round B, but its model still drives the legacy
        # layout-selected callback used by some code paths.
        if combo is not None:
            combo.blockSignals(True)
            prev_layout_id: str | None = None
            if combo.currentIndex() >= 0:
                prev_layout_id = combo.currentData()
            combo.clear()
            new_selection_idx = 0
            for i, sl in enumerate(doc.screen_layouts):
                combo.addItem(f"{sl.name}  [{sl.id}]", sl.id)
                if sl.id == prev_layout_id:
                    new_selection_idx = i
            combo.setCurrentIndex(new_selection_idx)
            combo.blockSignals(False)

        # Hide empty-state label; show canvas
        if empty_lbl is not None:
            empty_lbl.hide()
        sm.show()

        # Determine selected layout
        selected_idx = combo.currentIndex() if combo is not None else 0
        if selected_idx < 0 or selected_idx >= len(doc.screen_layouts):
            selected_idx = 0
        layout = doc.screen_layouts[selected_idx]

        # Gather monitors
        monitors: list[Any] = []
        if self._services is not None:
            monitor_svc = getattr(self._services, "monitor_service", None)
            if monitor_svc is not None and hasattr(monitor_svc, "snapshot"):
                try:
                    monitors = monitor_svc.snapshot()
                except Exception:
                    monitors = []
        import contextlib

        with contextlib.suppress(Exception):
            sm.set_layout(layout, monitors)

    def _on_screen_map_layout_selected(self, _index: int) -> None:
        """Slot: legacy combo selection changed → re-render the newly selected layout."""
        self._refresh_screen_map_tab()

    def _on_screens_mode_changed(self, _checked: bool) -> None:
        """Slot: Live/Preview radio toggled.

        Round B removed all in-tab chrome (Group picker, Layout selector,
        New Layout button, Live/Preview radios) from the visible UI; the
        canvas is always rendered in Preview semantics. The handler only
        triggers a refresh — no visibility toggling.
        """
        is_preview = self._radio_screens_preview.isChecked()
        if is_preview:
            self._refresh_screens_preview()
            self._refresh_screens_members_list()
        else:
            self._refresh_screens_live()

    def _on_screens_group_selected(self, _index: int) -> None:
        """Slot: group picker selection changed in Preview mode."""
        if self._radio_screens_preview.isChecked():
            self._refresh_screens_preview()
            self._refresh_screens_members_list()

    def _on_screens_save_clicked(self) -> None:
        """Slot: 'Save Layout…' button clicked.

        Preview mode: persist the current canvas layout to disk.
        Live mode: persist AND push the layout to active tmux panes via LayoutController.
        """
        is_preview = self._radio_screens_preview.isChecked()

        # Gather canvas layout
        canvas_layout: ScreenLayout | None = None
        if hasattr(self, "_screen_map_widget"):
            canvas_layout = getattr(self._screen_map_widget, "_layout_data", None)

        if canvas_layout is None:
            self.statusBar().showMessage("No layout to save.", 3000)
            return

        if is_preview:
            # Overwrite the matching layout in the document (same id) or just save.
            self._screens_persist_canvas_layout(canvas_layout)
            self.statusBar().showMessage("Layout saved.", 3000)
        else:
            # Live mode: persist + push to tmux
            self._screens_persist_canvas_layout(canvas_layout)
            self._screens_push_live_layout(canvas_layout)
            self.statusBar().showMessage("Layout saved and applied to live sessions.", 3000)

    def _screens_persist_canvas_layout(self, canvas_layout: ScreenLayout) -> None:
        """Write *canvas_layout* to the document and persist to disk."""
        # Replace matching layout by id if it already exists, otherwise append.
        existing_idx: int | None = None
        for i, sl in enumerate(self._document.screen_layouts):
            if sl.id == canvas_layout.id:
                existing_idx = i
                break
        if existing_idx is not None:
            self._document.screen_layouts[existing_idx] = canvas_layout
        else:
            self._document.screen_layouts.append(canvas_layout)
        self._save_document()

    def _screens_push_live_layout(self, canvas_layout: ScreenLayout) -> None:
        """Push *canvas_layout* to active tmux sessions via LayoutController.

        Skips panes whose sessions are not currently running; logs a warning per
        missing pane rather than raising an error dialog.
        """
        if self._layout_controller is None:
            return
        try:
            # Set the active layout on the controller and let it apply diffs.
            if hasattr(self._layout_controller, "set_screen_layout"):
                self._layout_controller.set_screen_layout(canvas_layout)
            if hasattr(self._layout_controller, "set_document"):
                self._layout_controller.set_document(self._document)
            if hasattr(self._layout_controller, "apply_layout"):
                self._layout_controller.apply_layout(canvas_layout)
        except Exception:
            import logging

            logging.getLogger(__name__).warning(
                "Could not push layout to live sessions", exc_info=True
            )

    def _on_screens_new_layout_clicked(self) -> None:
        """Slot: 'New Layout' button clicked on the Screens tab."""
        if not self._radio_screens_preview.isChecked():
            return  # button is only shown in Preview mode

        combo = self._combo_screens_group
        group_id: str | None = combo.currentData() if combo.currentIndex() >= 0 else None
        if not group_id:
            self.statusBar().showMessage("Select a group first.", 3000)
            return

        grp = self._find_group_by_id(group_id)
        if grp is None:
            self.statusBar().showMessage("Group not found.", 3000)
            return

        # Gather monitors
        monitors: list[Any] = self._query_live_monitors()

        # Generate default layout
        from cpsm.services.default_layout_generator import generate_default_layout

        new_layout = generate_default_layout(
            group_id=grp.id,
            group_name=grp.name,
            member_count=len(grp.members),
            monitors=monitors,
        )

        # Wire the placeholder panes with real connection_ids, matching the
        # round-robin distribution used by generate_default_layout:
        #   monitor_index 0 gets members[0], members[m], members[2m], ...
        #   monitor_index 1 gets members[1], members[m+1], members[2m+1], ...
        # We walk the monitors in order and assign connection_ids in-place.
        if new_layout.monitors and grp.members:
            m = len(new_layout.monitors)
            for monitor_index, schema_monitor in enumerate(new_layout.monitors):
                pane_indices = list(range(monitor_index, len(grp.members), m))
                for vp in schema_monitor.viewports:
                    for slot, pane in enumerate(vp.panes):
                        if slot < len(pane_indices):
                            member_idx = pane_indices[slot]
                            pane.connection_id = grp.members[member_idx]

        # Fallback when headless (no monitors)
        if not new_layout.monitors and grp.members:
            from cpsm.data.schema import GeometryPct, Monitor, Pane, Viewport

            panes_fb = [Pane(connection_id=mid) for mid in grp.members]
            vp_fb = Viewport(
                id=f"{grp.id}-vp-0",
                geometry_pct=GeometryPct(x=0, y=0, w=100, h=100),
                tmux_window_name=f"{grp.id}-0",
                tmux_layout="tiled",
                panes=panes_fb,
            )
            new_layout = ScreenLayout(
                id=new_layout.id,
                name=new_layout.name,
                monitors=[Monitor(viewports=[vp_fb])],
            )

        # Build a unique name: "Default — <Group>" or "Default — <Group> (2)" etc.
        base_name = f"Default — {grp.name}"
        chosen_name = base_name
        existing_names = {sl.name for sl in self._document.screen_layouts}
        if chosen_name in existing_names:
            counter = 2
            while f"{base_name} ({counter})" in existing_names:
                counter += 1
            chosen_name = f"{base_name} ({counter})"

        # Build a unique id similarly
        base_id = new_layout.id
        chosen_id = base_id
        existing_ids = {sl.id for sl in self._document.screen_layouts}
        if chosen_id in existing_ids:
            # Append a short hash suffix
            chosen_id = base_id + "-" + uuid.uuid4().hex[:6]

        new_layout = new_layout.model_copy(update={"id": chosen_id, "name": chosen_name})

        # Append to document
        self._document.screen_layouts.append(new_layout)

        # Register with group: set as default_layout_id if it's the first layout,
        # or just append (Group only tracks default_layout_id, not a list).
        grp_idx = next(
            (i for i, g in enumerate(self._document.groups) if g.id == grp.id), None
        )
        if grp_idx is not None:
            updated_grp = self._document.groups[grp_idx]
            # Always point the group at the newly-created layout — otherwise
            # subsequent renders via default_layout_id would diverge from
            # what's actually on the canvas, and edits applied to the rendered
            # layout would silently disappear on the next refresh.
            updated_grp = updated_grp.model_copy(
                update={"default_layout_id": new_layout.id}
            )
            self._document.groups[grp_idx] = updated_grp

        # Persist
        self._save_document()

        # Refresh UI
        self.load_document(self._document)

        # Select the new layout's group in combo (it should already be selected)
        # and force a preview refresh so the new layout appears
        self._refresh_screens_preview()
        self.statusBar().showMessage(f"New layout '{chosen_name}' created.", 3000)

    # ------------------------------------------------------------------
    # Group members list helpers (Feature A)
    # ------------------------------------------------------------------

    def _refresh_screens_members_list(self) -> None:
        """Populate the group members list from the currently selected group.

        Items whose connection_id is already placed on the canvas are greyed
        out (foreground brush + tooltip suffix) but remain fully draggable.
        """
        if not hasattr(self, "_list_screens_group_members"):
            return
        lst = self._list_screens_group_members
        lst.clear()

        combo = getattr(self, "_combo_screens_group", None)
        if combo is None or combo.currentIndex() < 0:
            lst.setVisible(False)
            return

        group_id: str | None = combo.currentData()
        if not group_id:
            lst.setVisible(False)
            return

        grp = self._find_group_by_id(group_id)
        if grp is None:
            lst.setVisible(False)
            return

        # --- Gather connection_ids already placed on the canvas ---
        placed_ids: set[str] = set()
        if grp.default_layout_id:
            placed_layout = self._find_layout_by_id(grp.default_layout_id)
            if placed_layout is not None:
                for schema_monitor in placed_layout.monitors:
                    for vp in schema_monitor.viewports:
                        for pane in vp.panes:
                            if pane.connection_id:
                                placed_ids.add(pane.connection_id)

        _grey = QColor("#94a3b8")
        _grey_brush = QBrush(_grey)

        for member_id in grp.members:
            conn = self._find_connection_by_id(member_id)
            label = (conn.name or conn.id) if conn is not None else member_id
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, member_id)

            if member_id in placed_ids:
                item.setToolTip(
                    f"Connection ID: {member_id}\nAlready on canvas (drag to a pane to move/replace)"
                )
                item.setForeground(_grey_brush)
                # Greying is a visual hint only — drag stays enabled so the user
                # can re-place an already-placed connection by dropping it onto
                # a different pane.
            else:
                item.setToolTip(f"Connection ID: {member_id}\nDrag onto canvas to place")

            lst.addItem(item)

        lst.setVisible(True)

    def _on_members_list_start_drag(self, lst: QListWidget) -> None:
        """Custom startDrag for the members list: emits MIME_CONNECTION_ID."""
        from cpsm.ui.widgets.screen_map import MIME_CONNECTION_ID

        item = lst.currentItem()
        if item is None:
            return
        conn_id: str = item.data(Qt.ItemDataRole.UserRole) or ""
        if not conn_id:
            return

        mime = QMimeData()
        mime.setData(MIME_CONNECTION_ID, QByteArray(conn_id.encode("utf-8")))

        drag = QDrag(lst)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)

    # ------------------------------------------------------------------
    # Screens-tab canvas context menu (Feature C)
    # ------------------------------------------------------------------

    def _on_screens_canvas_context_menu(self, view_pos: Any) -> None:
        """Slot: right-click on the Screens-tab ScreenMapWidget view."""
        from PySide6.QtCore import QPoint

        from cpsm.ui.widgets.screen_map_context_menu import ScreenMapContextMenuMixin, hit_test

        vp_pt = view_pos if isinstance(view_pos, QPoint) else view_pos.toPoint()
        view = self._screen_map_widget.view
        scene_pt = view.mapToScene(vp_pt)
        sx, sy = scene_pt.x(), scene_pt.y()

        layout = self._cmx_get_layout()
        monitors = self._cmx_get_monitors()

        schema_mon, vp_obj, pane_idx = hit_test(sx, sy, layout, monitors)

        mixin = ScreenMapContextMenuMixin()
        # Bind _cmx_* helpers to self so mutations/picks go through MainWindow's methods.
        mixin._cmx_get_layout = self._cmx_get_layout  # type: ignore[method-assign]
        mixin._cmx_get_monitors = self._cmx_get_monitors  # type: ignore[method-assign]
        mixin._cmx_set_layout = self._cmx_set_layout  # type: ignore[method-assign]
        mixin._cmx_pick_connection = self._cmx_pick_connection  # type: ignore[method-assign]
        mixin._cmx_exec_menu = self._cmx_exec_menu  # type: ignore[method-assign]

        # Round-late tweak: the canvas context menu is reduced to a single
        # action — "Clear Pane" — when right-clicking a pane that holds a
        # connection. All other right-click positions (empty canvas,
        # monitor, viewport, empty pane) get no menu. The full
        # cmx_build_* hierarchy still exists for the mixin so unit tests
        # and programmatic callers can use it.
        if schema_mon is not None and vp_obj is not None and pane_idx >= 0:
            pane = vp_obj.panes[pane_idx]
            if pane.connection_id is not None:
                from PySide6.QtGui import QAction
                from PySide6.QtWidgets import QMenu
                menu = QMenu()
                menu.setObjectName("menu_screens_pane")
                menu.setAccessibleName("Screens Pane Menu")
                act_clear = QAction("Clear Pane", menu)
                act_clear.setObjectName("action_screens_clear_pane")
                act_clear.triggered.connect(
                    lambda _c=False, m=schema_mon, v=vp_obj, pi=pane_idx:
                    mixin._cmx_update_pane_connection(m, v, pi, None)
                )
                menu.addAction(act_clear)
                global_pos = view.viewport().mapToGlobal(vp_pt)
                self._exec_menu(menu, global_pos)
        # Suppress menu entirely for empty pane / viewport / monitor /
        # empty-canvas hits.

    def _cmx_get_layout(self) -> ScreenLayout:
        """Return the current canvas layout for cmx mixin calls."""
        if hasattr(self, "_screen_map_widget"):
            layout = getattr(self._screen_map_widget, "_layout_data", None)
            if isinstance(layout, ScreenLayout):
                return layout
        # Return an empty layout as fallback
        return ScreenLayout(id="empty", name="(empty)", monitors=[])

    def _cmx_get_monitors(self) -> list[Any]:
        """Return live monitors for cmx mixin calls."""
        return self._query_live_monitors()

    def _cmx_set_layout(self, layout: ScreenLayout) -> None:
        """Apply *layout* to the canvas and persist (called by cmx mixin mutations)."""
        _dd_log.info(
            "_cmx_set_layout: layout=%s monitors=%d panes=%s",
            layout.id,
            len(layout.monitors),
            [p.connection_id for m in layout.monitors for v in m.viewports for p in v.panes],
        )
        if hasattr(self, "_screen_map_widget"):
            self._screen_map_widget.set_layout(layout, self._query_live_monitors())
        # Persist — overwrite or append
        self._screens_persist_canvas_layout(layout)
        # Refresh the members list so freed connections un-grey and re-placed
        # connections grey out without needing a manual group-reselect.
        if hasattr(self, "_refresh_screens_members_list"):
            self._refresh_screens_members_list()
        # Propagate to live tmux state if the active group is running
        # (right-click context menu actions land here too).
        if hasattr(self, "_resync_tmux_to_layout"):
            self._resync_tmux_to_layout()

    def _cmx_pick_connection(self) -> str | None:
        """Open a connection picker dialog; return selected id or None."""
        from PySide6.QtWidgets import QDialog

        from cpsm.ui.dialogs.layout_editor import _ConnectionPickerDialog

        if not self._document.connections:
            QMessageBox.information(
                self,
                "No Connections",
                "No connections are defined in the document yet.",
            )
            return None
        dlg = _ConnectionPickerDialog(self, document=self._document)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return dlg.selected_connection_id()
        return None

    def _cmx_exec_menu(self, menu: Any, pos: Any) -> None:
        """Show *menu* at *pos* via the existing _exec_menu hook."""
        self._exec_menu(menu, pos)

    def _refresh_screens_group_combo(self, doc: CpsmDocument | None = None) -> None:
        """Rebuild the group picker combo from *doc.groups*."""
        if not hasattr(self, "_combo_screens_group"):
            return
        if doc is None:
            doc = self._document
        combo = self._combo_screens_group
        combo.blockSignals(True)
        prev_group_id: str | None = combo.currentData() if combo.currentIndex() >= 0 else None
        combo.clear()
        new_selection = 0
        for i, grp in enumerate(doc.groups):
            combo.addItem(grp.name or grp.id, grp.id)
            if grp.id == prev_group_id:
                new_selection = i
        if combo.count() > 0:
            combo.setCurrentIndex(new_selection)
        combo.blockSignals(False)

    def _query_live_monitors(self) -> list[Any]:
        """Return MonitorService.snapshot() output, or [] if unavailable."""
        if self._services is None:
            return []
        monitor_svc = getattr(self._services, "monitor_service", None)
        if monitor_svc is None or not hasattr(monitor_svc, "snapshot"):
            return []
        try:
            return list(monitor_svc.snapshot())
        except Exception:
            return []

    def _refresh_screens_preview(self) -> None:
        """Render the selected group's default_layout_id in the screen map.

        The ScreenMapWidget always renders — empty layouts fall back to ghost
        monitor rectangles inside the canvas (handled by ``_render_ghost_monitors``).
        We never show the standalone "empty" label in this mode because the
        canvas itself communicates the state.
        """
        if not hasattr(self, "_screen_map_widget"):
            return
        sm = self._screen_map_widget
        combo = self._combo_screens_group

        # Always show the map and hide the legacy empty label.
        if hasattr(self, "_screen_map_empty_label"):
            self._screen_map_empty_label.hide()
        sm.show()

        layout: ScreenLayout | None = None
        if combo.count() > 0:
            group_id = combo.currentData()
            grp = self._find_group_by_id(group_id) if group_id else None
            if grp is not None and grp.default_layout_id:
                layout = self._find_layout_by_id(grp.default_layout_id)

        # When no layout is selectable, render an "empty layout" so the widget
        # falls back to ghost monitors. ScreenLayout requires id+name so we
        # create a transient one — never persisted.
        if layout is None:
            from cpsm.data.schema import ScreenLayout as _SL

            layout = _SL(id="empty", name="(no layout)", monitors=[])

        import contextlib

        with contextlib.suppress(Exception):
            sm.set_layout(layout, self._query_live_monitors())

    def _refresh_screens_live(self) -> None:
        """Render a synthesized live layout from active sessions.

        Always shows the screen map (with ghost monitors when no sessions are
        active) so the user can right-click to start authoring without first
        toggling modes.
        """
        if not hasattr(self, "_screen_map_widget"):
            return
        sm = self._screen_map_widget
        if hasattr(self, "_screen_map_empty_label"):
            self._screen_map_empty_label.hide()
        sm.show()

        monitors = self._query_live_monitors()

        # Gather active panes from status poller if available
        active_panes: list[Any] = []
        if self._services is not None:
            poller = getattr(self._services, "status_poller", None)
            if poller is not None:
                snap = getattr(poller, "last_snapshot", None)
                if snap is not None:
                    active_panes = snap if isinstance(snap, list) else []

        live_layout = self._synthesize_live_layout(monitors, active_panes)

        import contextlib

        with contextlib.suppress(Exception):
            sm.set_layout(live_layout, monitors)

    def _synthesize_live_layout(self, monitors: list[Any], active_panes: list[Any]) -> ScreenLayout:
        """Build a temporary ScreenLayout from live monitor + session data.

        Each monitor gets one viewport at 100x100%.  All *active_panes* are
        placed in the first monitor's viewport (monitor mapping is not tracked
        yet).  When there are no monitors, a single placeholder monitor is used.
        """
        from cpsm.data.schema import GeometryPct, Monitor, Pane, Viewport

        layout_monitors: list[Monitor] = []
        if monitors:
            for idx, _m in enumerate(monitors):
                panes: list[Pane] = []
                if idx == 0:
                    # All active panes go into the first monitor for now
                    for pane_info in active_panes:
                        cid = getattr(pane_info, "connection_id", None)
                        if cid is None and isinstance(pane_info, dict):
                            cid = pane_info.get("connection_id")
                        panes.append(Pane(connection_id=cid))
                vp = Viewport(
                    id=f"live-vp-{idx}",
                    geometry_pct=GeometryPct(x=0, y=0, w=100, h=100),
                    tmux_layout="tiled",
                    panes=panes,
                )
                layout_monitors.append(Monitor(viewports=[vp]))
        elif active_panes:
            panes = []
            for pane_info in active_panes:
                cid = getattr(pane_info, "connection_id", None)
                if cid is None and isinstance(pane_info, dict):
                    cid = pane_info.get("connection_id")
                panes.append(Pane(connection_id=cid))
            vp = Viewport(
                id="live-vp-0",
                geometry_pct=GeometryPct(x=0, y=0, w=100, h=100),
                tmux_layout="tiled",
                panes=panes,
            )
            layout_monitors.append(Monitor(viewports=[vp]))

        return ScreenLayout(id="live-layout", name="Live", monitors=layout_monitors)

    def _open_screens_save_dialog(self) -> None:
        """Open the Save Layout dialog for the Screens tab."""
        from cpsm.ui.dialogs.screens_save import ScreensSaveDialog

        is_preview = self._radio_screens_preview.isChecked()
        current_layout_name: str | None = None
        current_layout_id: str | None = None
        if is_preview:
            group_id = self._combo_screens_group.currentData()
            grp = self._find_group_by_id(group_id) if group_id else None
            if grp and grp.default_layout_id:
                layout = self._find_layout_by_id(grp.default_layout_id)
                if layout:
                    current_layout_name = layout.name
                    current_layout_id = layout.id

        # Gather current canvas layout
        canvas_layout: ScreenLayout | None = None
        if hasattr(self, "_screen_map_widget"):
            canvas_layout = getattr(self._screen_map_widget, "_layout_data", None)

        dlg = ScreensSaveDialog(
            parent=self,
            doc=self._document,
            is_preview_mode=is_preview,
            current_layout_id=current_layout_id,
            current_layout_name=current_layout_name,
            canvas_layout=canvas_layout,
        )
        if dlg.exec() == ScreensSaveDialog.DialogCode.Accepted:
            result = dlg.get_result()
            self._apply_screens_save(result, canvas_layout)

    def _apply_screens_save(
        self, result: dict[str, Any], canvas_layout: ScreenLayout | None
    ) -> None:
        """Apply the save-dialog result to the document."""
        if canvas_layout is None:
            return
        mode = result.get("mode")
        if mode == "overwrite":
            layout_id = result.get("layout_id")
            for i, sl in enumerate(self._document.screen_layouts):
                if sl.id == layout_id:
                    updated = canvas_layout.model_copy(update={"id": sl.id, "name": sl.name})
                    self._document.screen_layouts[i] = updated
                    break
        elif mode == "new_layout":
            group_id = result.get("group_id")
            new_name = result.get("layout_name", "New Layout")
            new_id = (
                f"{group_id}-layout-{self._new_unique_id()}" if group_id else self._new_unique_id()
            )
            new_layout = canvas_layout.model_copy(update={"id": new_id, "name": new_name})
            self._document.screen_layouts.append(new_layout)
            grp = self._find_group_by_id(group_id) if group_id else None
            if grp is not None:
                idx = self._document.groups.index(grp)
                updated_grp = grp.model_copy(update={"default_layout_id": new_id})
                self._document.groups[idx] = updated_grp
        elif mode == "new_group":
            group_name = result.get("group_name", "New Group")
            new_group_id = self._new_unique_id()
            new_layout_id = f"{new_group_id}-default-layout"
            new_name = result.get("layout_name", f"{group_name} default")
            new_layout = canvas_layout.model_copy(update={"id": new_layout_id, "name": new_name})
            from cpsm.data.schema import Group as _Group

            new_grp = _Group(
                id=new_group_id,
                name=group_name,
                default_layout_id=new_layout_id,
            )
            self._document.screen_layouts.append(new_layout)
            self._document.groups.append(new_grp)
        self.load_document(self._document)
        self._save_document()

    # ------------------------------------------------------------------
    # Connection editor helpers
    # ------------------------------------------------------------------

    def _config_path(self) -> Path | None:
        """Return the path to save the config, if known."""
        if self._services is not None:
            for attr in ("config_path", "config_file", "path"):
                p = getattr(self._services, attr, None)
                if p is not None:
                    return Path(p)
        # Fallback to ~/.cpsm.yaml
        return Path.home() / ".cpsm.yaml"

    def _save_document(self) -> None:
        """Persist the current document if a repository is wired."""
        if self._services is None:
            return
        repo = getattr(self._services, "repository", None)
        if repo is None:
            return
        target = self._config_path()
        if target is None:
            return
        try:
            repo.save(self._document, target)
        except Exception as exc:
            QMessageBox.warning(self, "Save Failed", str(exc))

    def _make_conn_editor_kwargs(self, conn: Connection | None) -> dict[str, Any]:
        """Build kwargs dict for ConnectionEditorDialog."""
        groups_list = [
            {"id": g.id, "name": g.name, "members": list(g.members)} for g in self._document.groups
        ]
        conn_ids = [c.id for c in self._document.connections]
        kwargs: dict[str, Any] = {
            "groups": groups_list,
            "available_connection_ids": conn_ids,
        }
        if conn is not None:
            kwargs["connection_data"] = conn.model_dump(mode="python")
            kwargs["is_new"] = False
        else:
            kwargs["connection_data"] = None
            kwargs["is_new"] = True
        return kwargs

    def _reconstruct_connection(self, data: dict[str, Any]) -> Connection | None:
        """Rebuild a Connection model from dialog data dict."""
        profile = data.get("launch_profile", "")
        try:
            if profile == "claude-remote":
                return ClaudeRemoteConnection.model_validate(data)
            if profile == "claude-local":
                return ClaudeLocalConnection.model_validate(data)
            if profile == "ssh-shell":
                return SshShellConnection.model_validate(data)
            if profile == "local-shell":
                return LocalShellConnection.model_validate(data)
            if profile == "custom":
                return CustomConnection.model_validate(data)
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_connection_selected(self, selected: Any, _deselected: Any) -> None:
        """Update the inspector when a connection is selected in the tree."""
        indexes = selected.indexes()
        if not indexes:
            self._clear_inspector()
            return

        row = indexes[0].row()
        if row < 0 or row >= len(self._document.connections):
            self._clear_inspector()
            return

        conn = self._document.connections[row]
        self._show_inspector_for(conn)

    def _clear_inspector(self) -> None:
        """Reset inspector to the placeholder state."""
        self._inspector_placeholder.show()
        for lbl in self._inspector_fields.values():
            lbl.hide()
        # Hide caption labels too
        for row_idx in range(1, self._inspector_form.rowCount()):
            item = self._inspector_form.itemAt(row_idx, QFormLayout.ItemRole.LabelRole)
            if item is not None:
                w = item.widget()
                if w is not None:
                    w.hide()

    def _show_inspector_for(self, conn: Connection) -> None:
        """Populate inspector fields for *conn* and make them visible."""
        self._inspector_placeholder.hide()

        # Build the full field map — None means "hide this row"
        _none = object()  # sentinel

        def _get(attr: str) -> Any:
            """Return attribute value or _none if attribute doesn't exist on this profile."""
            val = getattr(conn, attr, _none)
            return val

        raw: dict[str, Any] = {
            "name": conn.name or "—",
            "id": conn.id,
            "launch_profile": conn.launch_profile,
            "host": _get("host"),
            "user": _get("user"),
            "sudo_user": _get("sudo_user"),
            "port": _get("port"),
            "project_folder": _get("project_folder"),
            "claude_options": _get("claude_options"),
            "identity_file_ref": _get("identity_file_ref"),
            "jump_host": _get("jump_host"),
            "tags": ", ".join(getattr(conn, "tags", [])) or "—",
            "notes": getattr(conn, "notes", None),
        }

        # Build display map (field → text or None-to-hide)
        field_map: dict[str, str | None] = {}
        for fname, val in raw.items():
            if val is _none or val is None:
                # Attribute absent on this profile type or explicitly None — hide
                field_map[fname] = None
            else:
                field_map[fname] = str(val) if val != "" else "—"

        # Show/hide caption labels to match value visibility
        for row_idx in range(1, self._inspector_form.rowCount()):
            lbl_item = self._inspector_form.itemAt(row_idx, QFormLayout.ItemRole.LabelRole)
            if lbl_item is None:
                continue
            cap_widget = lbl_item.widget()
            if cap_widget is None:
                continue
            # Find the matching field key by object name
            cap_name = cap_widget.objectName()  # "inspector_label_<field>"
            if cap_name.startswith("inspector_label_"):
                fname = cap_name[len("inspector_label_") :]
                should_show = field_map.get(fname) is not None
                cap_widget.setVisible(should_show)

        for fname, value in field_map.items():
            if fname not in self._inspector_fields:
                continue
            if value is None:
                self._inspector_fields[fname].hide()
            else:
                self._inspector_fields[fname].setText(value)
                self._inspector_fields[fname].show()

        self._auto_resize_inspector()

    def _auto_resize_inspector(self) -> None:
        """Resize the Inspector dock to fit the widest visible value label,
        so non-wrapping text doesn't get clipped."""
        from PySide6.QtGui import QFontMetricsF

        if not hasattr(self, "_dock_inspector"):
            return
        # Compute the widest visible value label
        max_value_w = 0.0
        for lbl in self._inspector_fields.values():
            if not lbl.isVisible():
                continue
            fm = QFontMetricsF(lbl.font())
            w = fm.horizontalAdvance(lbl.text())
            if w > max_value_w:
                max_value_w = w
        # Caption labels are roughly 110 px; add scrollbar + margins padding
        target_w = int(max_value_w + 110 + 40)
        # Bound to a sensible range
        target_w = max(280, min(target_w, 800))
        # Apply by setting the dock's min width — Qt will adjust accordingly
        self._dock_inspector.setMinimumWidth(target_w)
        # Resize when floating; for docked, the user can still drag the splitter
        if self._dock_inspector.isFloating():
            self._dock_inspector.resize(target_w, self._dock_inspector.height())

    def _on_connection_double_clicked(self, index: Any) -> None:
        """Open ConnectionEditorDialog for the double-clicked connection."""
        row = index.row()
        if row < 0 or row >= len(self._document.connections):
            return
        conn = self._document.connections[row]
        self._open_connection_editor(conn)

    def _on_group_double_clicked(self, index: Any) -> None:
        """Open GroupEditorDialog for the double-clicked group."""
        row = index.row()
        if row < 0 or row >= len(self._document.groups):
            return
        grp = self._document.groups[row]
        self._open_group_editor(grp)

    def _on_layout_double_clicked(self, index: Any) -> None:
        """Open LayoutEditorDialog for the double-clicked layout."""
        row = index.row()
        if row < 0 or row >= len(self._document.screen_layouts):
            return
        layout = self._document.screen_layouts[row]
        self._open_layout_editor(layout)

    def _open_connection_editor(self, conn: Connection | None) -> None:
        """Open ConnectionEditorDialog populated with *conn* (or empty for new)."""
        from cpsm.ui.dialogs.connection_editor import ConnectionEditorDialog

        kwargs = self._make_conn_editor_kwargs(conn)
        dlg = ConnectionEditorDialog(parent=self, **kwargs)
        dlg.setObjectName("dlg_connection_editor")
        if dlg.exec() == ConnectionEditorDialog.DialogCode.Accepted:
            data = dlg.get_connection_data()
            if conn is None:
                # New connection
                new_conn = self._reconstruct_connection(data)
                if new_conn is not None:
                    self._document.connections.append(new_conn)
            else:
                # Update in place
                new_conn = self._reconstruct_connection(data)
                if new_conn is not None:
                    idx = self._document.connections.index(conn)
                    self._document.connections[idx] = new_conn
            self.load_document(self._document)
            self._save_document()

    def _open_group_editor(self, grp: Group | None) -> None:
        """Open GroupEditorDialog populated with *grp* (or empty for new)."""
        from cpsm.ui.dialogs.group_editor import GroupEditorDialog
        from cpsm.ui.widgets.group_panel import ConnectionEntry

        all_connections = [
            ConnectionEntry(
                conn_id=c.id,
                name=c.name or c.id,
                profile=c.launch_profile,
                other_groups=[],
            )
            for c in self._document.connections
        ]
        layout_ids = [sl.id for sl in self._document.screen_layouts]
        group_data = grp.model_dump(mode="python") if grp is not None else None
        is_new = grp is None
        dlg = GroupEditorDialog(
            parent=self,
            group_data=group_data,
            all_connections=all_connections,
            available_layout_ids=layout_ids,
            is_new=is_new,
            doc=self._document,
            save_callback=self._save_document,
        )
        dlg.setObjectName("dlg_group_editor")
        if dlg.exec() == GroupEditorDialog.DialogCode.Accepted:
            data = dlg.get_group_data()
            try:
                new_grp = Group.model_validate(data)
            except Exception:
                return

            # Apply any layout deletions the dialog performed
            if hasattr(dlg, "_pending_layout_deletes"):
                for lid in dlg._pending_layout_deletes:
                    self._document.screen_layouts = [
                        sl for sl in self._document.screen_layouts if sl.id != lid
                    ]

            if is_new:
                # Auto-generate a default layout for the new group
                from cpsm.services.default_layout_generator import generate_default_layout

                monitors: list[Any] = []
                if self._services is not None:
                    monitor_svc = getattr(self._services, "monitor_service", None)
                    if monitor_svc is not None and hasattr(monitor_svc, "snapshot"):
                        try:
                            monitors = monitor_svc.snapshot()
                        except Exception:
                            monitors = []

                member_ids = new_grp.members
                default_layout = generate_default_layout(
                    group_id=new_grp.id,
                    group_name=new_grp.name,
                    member_count=len(member_ids),
                    monitors=monitors,  # original (may be empty)
                )
                # If no monitors were available, build a single-monitor fallback
                if not default_layout.monitors and member_ids:
                    from cpsm.data.schema import GeometryPct, Monitor, Pane, Viewport

                    panes_fb = [Pane(connection_id=mid) for mid in member_ids]
                    vp_fb = Viewport(
                        id=f"{new_grp.id}-vp-0",
                        geometry_pct=GeometryPct(x=0, y=0, w=100, h=100),
                        tmux_window_name=f"{new_grp.id}-0",
                        tmux_layout="tiled",
                        panes=panes_fb,
                    )
                    default_layout = ScreenLayout(
                        id=default_layout.id,
                        name=default_layout.name,
                        monitors=[Monitor(viewports=[vp_fb])],
                    )
                else:
                    # Wire real connection ids into panes round-robin
                    m = len(monitors) if monitors else 1
                    for mon_idx, monitor in enumerate(default_layout.monitors):
                        for vp in monitor.viewports:
                            from cpsm.data.schema import Pane

                            wired_panes = []
                            pane_member_indices = list(range(mon_idx, len(member_ids), m))
                            for slot_i, member_pos in enumerate(pane_member_indices):
                                if slot_i < len(vp.panes):
                                    wired_panes.append(Pane(connection_id=member_ids[member_pos]))
                            if wired_panes:
                                object.__setattr__(vp, "panes", wired_panes)

                self._document.screen_layouts.append(default_layout)
                new_grp = new_grp.model_copy(update={"default_layout_id": default_layout.id})
                self._document.groups.append(new_grp)
            else:
                assert grp is not None  # is_new=False guarantees grp is not None
                # Match by id, not object identity — auto-layout / model_copy
                # paths replace group objects in the list.
                idx = next(
                    (i for i, g in enumerate(self._document.groups) if g.id == grp.id),
                    None,
                )
                if idx is None:
                    return
                self._document.groups[idx] = new_grp
            self.load_document(self._document)
            self._save_document()

    def _open_layout_editor(self, layout: ScreenLayout | None) -> None:
        """Open LayoutEditorDialog populated with *layout* (or empty for new)."""
        from cpsm.ui.dialogs.layout_editor import LayoutEditorDialog

        dlg = LayoutEditorDialog(
            parent=self,
            document=self._document,
            layout=layout,
            is_new=(layout is None),
        )
        dlg.setObjectName("dlg_layout_editor")
        if dlg.exec() == LayoutEditorDialog.DialogCode.Accepted:
            # LayoutEditorDialog emits saved(layout) signal; also try get_layout_data
            if hasattr(dlg, "get_layout"):
                new_layout: ScreenLayout | None = dlg.get_layout()
            elif hasattr(dlg, "_layout"):
                new_layout = dlg._layout
            else:
                new_layout = None
            if new_layout is not None:
                if layout is None:
                    self._document.screen_layouts.append(new_layout)
                else:
                    idx = self._document.screen_layouts.index(layout)
                    self._document.screen_layouts[idx] = new_layout
            self.load_document(self._document)
            # Ensure Screen Map tab reflects the newly added/edited layout
            self._refresh_screen_map_tab()
            self._save_document()

    # ------------------------------------------------------------------
    # Sidebar double-click slot
    # ------------------------------------------------------------------

    def _on_sidebar_item_selected(self, current: Any, _previous: Any) -> None:
        """Single-click selection in the sidebar tree.

        - Connection selected → populate the Inspector dock.
        - Group selected → render that group's layout on the canvas (Round C).
        - Anything else → clear the Inspector.
        """
        from PySide6.QtWidgets import QTreeWidgetItem

        if not isinstance(current, QTreeWidgetItem):
            self._clear_inspector()
            return
        parent = current.parent()
        if parent is None:
            self._clear_inspector()
            return
        category_id: str = parent.data(0, Qt.ItemDataRole.UserRole) or ""
        item_id: str = current.data(0, Qt.ItemDataRole.UserRole) or ""

        if category_id == "category_cat_connections":
            conn = self._find_connection_by_id(item_id)
            if conn is None:
                self._clear_inspector()
                return
            self._show_inspector_for(conn)
            return

        if category_id == "category_cat_groups":
            # Drive the canvas: select this group in the (hidden) combo, which
            # fires the existing _on_screens_group_selected → refresh chain.
            self._set_active_group(item_id)
            self._clear_inspector()
            return

        # Layouts / Scenes / unknown: just clear the Inspector
        self._clear_inspector()

    def _set_active_group(self, group_id: str) -> None:
        """Select *group_id* in the (hidden) screens-group combo so the
        canvas renders that group's layout. Auto-creates a default layout
        if the group doesn't have one yet."""
        # Ensure the group has a layout BEFORE refreshing — otherwise the
        # preview will render an empty placeholder and the user has to
        # take an extra action to populate it.
        self._ensure_group_has_layout(group_id)
        combo = getattr(self, "_combo_screens_group", None)
        if combo is None:
            return
        for i in range(combo.count()):
            if combo.itemData(i) == group_id:
                if combo.currentIndex() != i:
                    combo.setCurrentIndex(i)
                else:
                    # currentIndexChanged won't fire when the index matches;
                    # force a refresh manually so the canvas always reflects
                    # the latest sidebar selection.
                    self._refresh_screens_preview()
                    self._refresh_screens_members_list()
                self._refresh_sidebar_membership_highlights()
                return

    def _ensure_group_has_layout(self, group_id: str) -> None:
        """If *group_id* lacks a usable default layout, create one in place
        using the same generator the (hidden) New Layout button used to."""
        if self._document is None:
            return
        grp_idx = next(
            (i for i, g in enumerate(self._document.groups) if g.id == group_id),
            None,
        )
        if grp_idx is None:
            return
        grp = self._document.groups[grp_idx]
        # Skip if the group already points at a valid layout
        if grp.default_layout_id and self._find_layout_by_id(grp.default_layout_id):
            return

        from cpsm.services.default_layout_generator import generate_default_layout

        monitors = self._query_live_monitors()
        new_layout = generate_default_layout(
            group_id=grp.id,
            group_name=grp.name,
            member_count=len(grp.members),
            monitors=monitors,
        )

        # Headless fallback (no live monitors) — generator returns an
        # empty layout in that case; build a single-monitor stub here so
        # the canvas has something to render.
        if not new_layout.monitors and grp.members:
            from cpsm.data.schema import GeometryPct, Monitor, Pane, Viewport
            panes_fb = [Pane(connection_id=mid) for mid in grp.members]
            vp_fb = Viewport(
                id=f"{grp.id}-vp-0",
                geometry_pct=GeometryPct(x=0, y=0, w=100, h=100),
                tmux_window_name=f"{grp.id}-0",
                tmux_layout="tiled",
                panes=panes_fb,
            )
            new_layout = ScreenLayout(
                id=new_layout.id,
                name=new_layout.name,
                monitors=[Monitor(viewports=[vp_fb])],
            )

        # Round-robin pre-fill for the live-monitor path
        if new_layout.monitors and grp.members:
            m = len(new_layout.monitors)
            for monitor_index, schema_monitor in enumerate(new_layout.monitors):
                pane_indices = list(range(monitor_index, len(grp.members), m))
                for vp in schema_monitor.viewports:
                    for slot, pane in enumerate(vp.panes):
                        if slot < len(pane_indices):
                            pane.connection_id = grp.members[pane_indices[slot]]

        # Ensure unique id within the document
        existing_ids = {sl.id for sl in self._document.screen_layouts}
        chosen_id = new_layout.id
        if chosen_id in existing_ids:
            import uuid as _uuid
            chosen_id = f"{new_layout.id}-{_uuid.uuid4().hex[:6]}"
        if chosen_id != new_layout.id:
            new_layout = new_layout.model_copy(update={"id": chosen_id})

        self._document.screen_layouts.append(new_layout)
        updated_grp = grp.model_copy(update={"default_layout_id": new_layout.id})
        self._document.groups[grp_idx] = updated_grp
        self._save_document()
        _dd_log.info(
            "auto-created layout %s for group %s",
            new_layout.id, group_id,
        )

    def _active_group_id(self) -> str | None:
        """Return the connection_id of the currently-active group, or None."""
        combo = getattr(self, "_combo_screens_group", None)
        if combo is None or combo.currentIndex() < 0:
            return None
        gid = combo.currentData()
        return gid if isinstance(gid, str) and gid else None

    def _placed_connection_ids(self) -> set[str]:
        """Return connection_ids currently placed as panes in the canvas
        layout (the layout the ScreenMapWidget is rendering)."""
        out: set[str] = set()
        layout = getattr(self._screen_map_widget, "_layout_data", None) if hasattr(
            self, "_screen_map_widget"
        ) else None
        if layout is None:
            return out
        for sm in layout.monitors:
            for vp in sm.viewports:
                for p in vp.panes:
                    if p.connection_id:
                        out.add(p.connection_id)
        return out

    def _refresh_sidebar_membership_highlights(self) -> None:
        """Update sidebar Connection items based on group membership and
        on whether the connection is already placed on the canvas.

        - Member of active group, not placed → bold + light blue.
        - Member of active group, placed → bold + light blue + italic + struck through.
        - Not a member, placed → italic + grey (still draggable, will auto-join on drop).
        - Otherwise → palette defaults.
        """
        if not hasattr(self, "_session_list") or self._document is None:
            return
        active = self._active_group_id()
        member_ids: set[str] = set()
        if active:
            grp = self._find_group_by_id(active)
            if grp is not None:
                member_ids = set(grp.members)
        placed_ids = self._placed_connection_ids()

        from PySide6.QtGui import QBrush, QColor, QFont
        member_color = QBrush(QColor("#60a5fa"))
        placed_color = QBrush(QColor("#94a3b8"))
        default_color = QBrush(QColor())

        cat_connections = self._session_list._cat_connections
        for i in range(cat_connections.childCount()):
            item = cat_connections.child(i)
            conn_id = item.data(0, Qt.ItemDataRole.UserRole) or ""
            is_member = conn_id in member_ids
            is_placed = conn_id in placed_ids
            font = QFont()
            font.setBold(is_member)
            font.setItalic(is_placed)
            item.setFont(0, font)
            if is_member:
                item.setForeground(0, member_color)
            elif is_placed:
                item.setForeground(0, placed_color)
            else:
                item.setForeground(0, default_color)

        # Mirror the highlight on the active Group itself so the user can
        # see at a glance which group's membership is being shown.
        cat_groups = self._session_list._cat_groups
        bold_font = QFont()
        bold_font.setBold(True)
        normal_font = QFont()
        for i in range(cat_groups.childCount()):
            item = cat_groups.child(i)
            grp_id = item.data(0, Qt.ItemDataRole.UserRole) or ""
            if grp_id == active:
                item.setForeground(0, member_color)
                item.setFont(0, bold_font)
            else:
                item.setForeground(0, default_color)
                item.setFont(0, normal_font)

    def _on_sidebar_item_clicked(self, item: Any, _column: int) -> None:
        """Single-click handler. With Ctrl held and a Group active, toggle
        the clicked Connection's membership in that group.
        """
        from PySide6.QtWidgets import QApplication, QTreeWidgetItem

        if not isinstance(item, QTreeWidgetItem):
            return
        modifiers = QApplication.keyboardModifiers()
        if not (modifiers & Qt.KeyboardModifier.ControlModifier):
            return
        parent = item.parent()
        if parent is None:
            return
        category_id: str = parent.data(0, Qt.ItemDataRole.UserRole) or ""
        if category_id != "category_cat_connections":
            return
        active_gid = self._active_group_id()
        if not active_gid:
            return
        conn_id: str = item.data(0, Qt.ItemDataRole.UserRole) or ""
        if not conn_id:
            return
        # Toggle membership in the active group
        grp_idx = next(
            (i for i, g in enumerate(self._document.groups) if g.id == active_gid),
            None,
        )
        if grp_idx is None:
            return
        grp = self._document.groups[grp_idx]
        members = list(grp.members)
        if conn_id in members:
            members.remove(conn_id)
        else:
            members.append(conn_id)
        updated = grp.model_copy(update={"members": members})
        self._document.groups[grp_idx] = updated
        self._save_document()
        self._refresh_sidebar_membership_highlights()
        self._refresh_screens_members_list()
        self.statusBar().showMessage(
            f"{'Removed' if conn_id not in members else 'Added'} {conn_id} "
            f"{'from' if conn_id not in members else 'to'} {grp.name}",
            3000,
        )

    def _on_sidebar_double_clicked(self, item: Any, _column: int) -> None:
        """Dispatch double-click on sidebar tree items to the appropriate editor."""
        from PySide6.QtWidgets import QTreeWidgetItem

        if not isinstance(item, QTreeWidgetItem):
            return

        # Category roots have no selectable parent — skip them
        parent = item.parent()
        if parent is None:
            return

        item_id: str = item.data(0, Qt.ItemDataRole.UserRole) or ""
        category_id: str = parent.data(0, Qt.ItemDataRole.UserRole) or ""

        if category_id == "category_cat_connections":
            conn = self._find_connection_by_id(item_id)
            if conn is not None:
                self._open_connection_editor(conn)
        elif category_id == "category_cat_groups":
            grp = self._find_group_by_id(item_id)
            if grp is not None:
                self._open_group_editor(grp)
        elif category_id == "category_cat_layouts":
            layout = self._find_layout_by_id(item_id)
            if layout is not None:
                self._open_layout_editor(layout)
        # Scenes: no-op for now

    # ------------------------------------------------------------------
    # Sidebar context menu slot
    # ------------------------------------------------------------------

    def _on_sidebar_context_menu(self, pos: QPoint) -> None:
        """Show a context menu for the right-clicked item in the sidebar tree."""
        tree = self._session_list.tree
        item = tree.itemAt(pos)
        global_pos = tree.viewport().mapToGlobal(pos)
        self._show_sidebar_item_menu(item, global_pos)

    def _show_sidebar_item_menu(self, item: Any, global_pos: Any) -> None:
        """Build and exec the context menu for *item* (may be None for empty area)."""
        if item is None:
            # Empty area — offer New Connection
            menu = QMenu(self)
            menu.setObjectName("menu_sidebar_empty")
            menu.setAccessibleName("Sidebar Empty Area Menu")
            act = QAction("New Connection…", self)
            act.setObjectName("action_ctx_sidebar_new_connection")
            act.setWhatsThis("New Connection from sidebar")
            act.triggered.connect(lambda _c=False: self._open_connection_editor(None))
            menu.addAction(act)
            self._exec_menu(menu, global_pos)
            return

        parent = item.parent()
        item_id: str = item.data(0, Qt.ItemDataRole.UserRole) or ""

        if parent is None:
            # Category root — offer New <type>
            category_id = item_id
            menu = QMenu(self)
            menu.setObjectName("menu_sidebar_category")
            menu.setAccessibleName("Sidebar Category Menu")
            if category_id == "category_cat_connections":
                act = QAction("New Connection…", self)
                act.setObjectName("action_ctx_sidebar_cat_new_connection")
                act.setWhatsThis("New Connection")
                act.triggered.connect(lambda _c=False: self._open_connection_editor(None))
                menu.addAction(act)
            elif category_id == "category_cat_groups":
                act = QAction("New Group…", self)
                act.setObjectName("action_ctx_sidebar_cat_new_group")
                act.setWhatsThis("New Group")
                act.triggered.connect(lambda _c=False: self._open_group_editor(None))
                menu.addAction(act)
            elif category_id == "category_cat_layouts":
                act = QAction("New Layout…", self)
                act.setObjectName("action_ctx_sidebar_cat_new_layout")
                act.setWhatsThis("New Layout")
                act.triggered.connect(lambda _c=False: self._open_layout_editor(None))
                menu.addAction(act)
            if not menu.isEmpty():
                self._exec_menu(menu, global_pos)
            return

        # Child item
        category_id = str(parent.data(0, Qt.ItemDataRole.UserRole) or "")

        if category_id == "category_cat_connections":
            conn = self._find_connection_by_id(item_id)
            if conn is not None:
                menu = self._build_connection_menu(conn, "menu_sidebar_connection")
                self._exec_menu(menu, global_pos)
        elif category_id == "category_cat_groups":
            grp = self._find_group_by_id(item_id)
            if grp is not None:
                menu = self._build_group_menu(grp, "menu_sidebar_group")
                self._exec_menu(menu, global_pos)
        elif category_id == "category_cat_layouts":
            layout = self._find_layout_by_id(item_id)
            if layout is not None:
                menu = self._build_layout_menu(layout, "menu_sidebar_layout")
                self._exec_menu(menu, global_pos)
        elif category_id == "category_cat_scenes":
            menu = QMenu(self)
            menu.setObjectName("menu_sidebar_scene")
            menu.setAccessibleName("Sidebar Scene Menu")
            act = QAction("Launch Scene", self)
            act.setObjectName("action_ctx_sidebar_launch_scene")
            act.setWhatsThis("Launch Scene")
            act.triggered.connect(lambda _c=False, sid=item_id: self._launch_scene_by_id(sid))
            menu.addAction(act)
            self._exec_menu(menu, global_pos)

    # ------------------------------------------------------------------
    # Tab context menus
    # ------------------------------------------------------------------

    def _on_connections_tab_context_menu(self, pos: QPoint) -> None:
        """Context menu for the Connections tab QTreeView."""
        index = self._connections_tree.indexAt(pos)
        global_pos = self._connections_tree.viewport().mapToGlobal(pos)
        if not index.isValid():
            # Empty area
            menu = QMenu(self)
            menu.setObjectName("menu_tab_connections_empty")
            menu.setAccessibleName("Connections Tab Empty Menu")
            act = QAction("New Connection…", self)
            act.setObjectName("action_ctx_tab_conn_new")
            act.setWhatsThis("New Connection")
            act.triggered.connect(lambda _c=False: self._open_connection_editor(None))
            menu.addAction(act)
            self._exec_menu(menu, global_pos)
            return
        row = index.row()
        if row < 0 or row >= len(self._document.connections):
            return
        conn = self._document.connections[row]
        menu = self._build_connection_menu(conn, "menu_tab_connection")
        menu.exec(global_pos)

    def _on_groups_tab_context_menu(self, pos: QPoint) -> None:
        """Context menu for the Groups tab QListView."""
        index = self._groups_list.indexAt(pos)
        global_pos = self._groups_list.viewport().mapToGlobal(pos)
        if not index.isValid():
            menu = QMenu(self)
            menu.setObjectName("menu_tab_groups_empty")
            menu.setAccessibleName("Groups Tab Empty Menu")
            act = QAction("New Group…", self)
            act.setObjectName("action_ctx_tab_grp_new")
            act.setWhatsThis("New Group")
            act.triggered.connect(lambda _c=False: self._open_group_editor(None))
            menu.addAction(act)
            self._exec_menu(menu, global_pos)
            return
        row = index.row()
        if row < 0 or row >= len(self._document.groups):
            return
        grp = self._document.groups[row]
        menu = self._build_group_menu(grp, "menu_tab_group")
        menu.exec(global_pos)

    def _on_layouts_tab_context_menu(self, pos: QPoint) -> None:
        """Context menu for the Layouts tab QListView."""
        index = self._layouts_list.indexAt(pos)
        global_pos = self._layouts_list.viewport().mapToGlobal(pos)
        if not index.isValid():
            menu = QMenu(self)
            menu.setObjectName("menu_tab_layouts_empty")
            menu.setAccessibleName("Layouts Tab Empty Menu")
            act = QAction("New Layout…", self)
            act.setObjectName("action_ctx_tab_layout_new")
            act.setWhatsThis("New Layout")
            act.triggered.connect(lambda _c=False: self._open_layout_editor(None))
            menu.addAction(act)
            self._exec_menu(menu, global_pos)
            return
        row = index.row()
        if row < 0 or row >= len(self._document.screen_layouts):
            return
        layout = self._document.screen_layouts[row]
        menu = self._build_layout_menu(layout, "menu_tab_layout")
        menu.exec(global_pos)

    # ------------------------------------------------------------------
    # Menu builders
    # ------------------------------------------------------------------

    def _build_connection_menu(self, conn: Any, object_name: str) -> QMenu:
        """Return a fully built context menu for *conn*."""
        menu = QMenu(self)
        menu.setObjectName(object_name)
        menu.setAccessibleName("Connection Context Menu")

        act_edit = QAction("Edit…", self)
        act_edit.setObjectName("action_ctx_edit_connection")
        act_edit.setWhatsThis("Edit Connection")
        act_edit.setShortcut(QKeySequence(Qt.Key.Key_Return))
        act_edit.triggered.connect(lambda _c=False, c=conn: self._open_connection_editor(c))
        menu.addAction(act_edit)

        act_launch = QAction("Launch", self)
        act_launch.setObjectName("action_ctx_launch_connection")
        act_launch.setWhatsThis("Launch Connection")
        act_launch.triggered.connect(lambda _c=False, c=conn: self._launch_connection(c))
        menu.addAction(act_launch)

        act_reconnect = QAction("Reconnect", self)
        act_reconnect.setObjectName("action_ctx_reconnect_connection")
        act_reconnect.setWhatsThis("Reconnect Connection")
        act_reconnect.triggered.connect(lambda _c=False, c=conn: self._reconnect_connection(c))
        menu.addAction(act_reconnect)

        act_stop = QAction("Stop", self)
        act_stop.setObjectName("action_ctx_stop_connection")
        act_stop.setWhatsThis("Stop Connection")
        act_stop.triggered.connect(lambda _c=False, c=conn: self._stop_connection(c))
        menu.addAction(act_stop)

        act_dup = QAction("Duplicate", self)
        act_dup.setObjectName("action_ctx_duplicate_connection")
        act_dup.setWhatsThis("Duplicate Connection")
        act_dup.triggered.connect(lambda _c=False, c=conn: self._duplicate_connection(c))
        menu.addAction(act_dup)

        menu.addSeparator()

        act_del = QAction("Delete", self)
        act_del.setObjectName("action_ctx_delete_connection")
        act_del.setWhatsThis("Delete Connection")
        act_del.triggered.connect(lambda _c=False, c=conn: self._delete_connection(c))
        menu.addAction(act_del)

        return menu

    def _build_group_menu(self, grp: Any, object_name: str) -> QMenu:
        """Return a fully built context menu for *grp*."""
        menu = QMenu(self)
        menu.setObjectName(object_name)
        menu.setAccessibleName("Group Context Menu")

        act_edit = QAction("Edit…", self)
        act_edit.setObjectName("action_ctx_edit_group")
        act_edit.setWhatsThis("Edit Group")
        act_edit.triggered.connect(lambda _c=False, g=grp: self._open_group_editor(g))
        menu.addAction(act_edit)

        act_launch = QAction("Launch group", self)
        act_launch.setObjectName("action_ctx_launch_group")
        act_launch.setWhatsThis("Launch Group")
        act_launch.triggered.connect(lambda _c=False, g=grp: self._launch_group(g))
        menu.addAction(act_launch)

        act_stop = QAction("Stop group", self)
        act_stop.setObjectName("action_ctx_stop_group")
        act_stop.setWhatsThis("Stop Group")
        act_stop.triggered.connect(lambda _c=False, g=grp: self._stop_group(g))
        menu.addAction(act_stop)

        act_dup = QAction("Duplicate", self)
        act_dup.setObjectName("action_ctx_duplicate_group")
        act_dup.setWhatsThis("Duplicate Group")
        act_dup.triggered.connect(lambda _c=False, g=grp: self._duplicate_group(g))
        menu.addAction(act_dup)

        menu.addSeparator()

        act_del = QAction("Delete", self)
        act_del.setObjectName("action_ctx_delete_group")
        act_del.setWhatsThis("Delete Group")
        act_del.triggered.connect(lambda _c=False, g=grp: self._delete_group(g))
        menu.addAction(act_del)

        return menu

    def _exec_menu(self, menu: QMenu, global_pos: Any) -> None:
        """Show *menu* at *global_pos*.

        Centralised through this hook so tests can monkey-patch it instead of
        PySide6's C++ ``QMenu.exec`` (which can't be replaced via Python class
        assignment and would block the test event loop).
        """
        menu.exec(global_pos)

    def _build_layout_menu(self, layout: Any, object_name: str) -> QMenu:
        """Return a fully built context menu for *layout*."""
        menu = QMenu(self)
        menu.setObjectName(object_name)
        menu.setAccessibleName("Layout Context Menu")

        act_edit = QAction("Edit…", self)
        act_edit.setObjectName("action_ctx_edit_layout")
        act_edit.setWhatsThis("Edit Layout")
        act_edit.triggered.connect(lambda _c=False, la=layout: self._open_layout_editor(la))
        menu.addAction(act_edit)

        act_dup = QAction("Duplicate", self)
        act_dup.setObjectName("action_ctx_duplicate_layout")
        act_dup.setWhatsThis("Duplicate Layout")
        act_dup.triggered.connect(lambda _c=False, la=layout: self._duplicate_layout(la))
        menu.addAction(act_dup)

        menu.addSeparator()

        act_del = QAction("Delete", self)
        act_del.setObjectName("action_ctx_delete_layout")
        act_del.setWhatsThis("Delete Layout")
        act_del.triggered.connect(lambda _c=False, la=layout: self._delete_layout(la))
        menu.addAction(act_del)

        return menu

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------

    def _find_connection_by_id(self, conn_id: str) -> Any | None:
        """Return the Connection with *conn_id* from the current document, or None."""
        for c in self._document.connections:
            if c.id == conn_id:
                return c
        return None

    def _find_group_by_id(self, grp_id: str) -> Any | None:
        """Return the Group with *grp_id* from the current document, or None."""
        for g in self._document.groups:
            if g.id == grp_id:
                return g
        return None

    def _find_layout_by_id(self, layout_id: str) -> Any | None:
        """Return the ScreenLayout with *layout_id* from the current document, or None."""
        for la in self._document.screen_layouts:
            if la.id == layout_id:
                return la
        return None

    # ------------------------------------------------------------------
    # Delete helpers
    # ------------------------------------------------------------------

    def _delete_connection(self, conn: Any) -> None:
        """Confirm then remove *conn* from the document."""
        name = conn.name or conn.id
        reply = QMessageBox.question(
            self,
            "Delete Connection",
            f'Delete connection "{name}"?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self._document.connections.remove(conn)
        except ValueError:
            return
        self.load_document(self._document)
        self._save_document()

    def _delete_group(self, grp: Any) -> None:
        """Confirm then remove *grp* from the document."""
        reply = QMessageBox.question(
            self,
            "Delete Group",
            f'Delete group "{grp.name}"?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        # Remove by id, not by object identity — auto-layout / model_copy
        # paths replace group objects in the list, so the parameter `grp`
        # may no longer be the same Python object that's stored in
        # self._document.groups.
        before = len(self._document.groups)
        self._document.groups[:] = [
            g for g in self._document.groups if g.id != grp.id
        ]
        if len(self._document.groups) == before:
            return  # nothing matched
        self.load_document(self._document)
        self._save_document()

    def _delete_layout(self, layout: Any) -> None:
        """Confirm then remove *layout* from the document."""
        reply = QMessageBox.question(
            self,
            "Delete Layout",
            f'Delete layout "{layout.name}"?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self._document.screen_layouts.remove(layout)
        except ValueError:
            return
        self.load_document(self._document)
        self._save_document()

    # ------------------------------------------------------------------
    # Duplicate helpers
    # ------------------------------------------------------------------

    def _new_unique_id(self) -> str:
        """Generate a short unique slug id."""
        return "id-" + uuid.uuid4().hex[:8]

    def _duplicate_connection(self, conn: Any) -> None:
        """Deep-copy *conn* with a new id/name and append it to the document."""
        new_id = self._new_unique_id()
        new_name = f"{conn.name or conn.id} (copy)"
        try:
            copy = conn.model_copy(deep=True, update={"id": new_id, "name": new_name})
        except Exception:
            return
        self._document.connections.append(copy)
        self.load_document(self._document)
        self._save_document()

    def _duplicate_group(self, grp: Any) -> None:
        """Deep-copy *grp* with a new id/name and append it to the document."""
        new_id = self._new_unique_id()
        new_name = f"{grp.name} (copy)"
        try:
            copy = grp.model_copy(deep=True, update={"id": new_id, "name": new_name})
        except Exception:
            return
        self._document.groups.append(copy)
        self.load_document(self._document)
        self._save_document()

    def _duplicate_layout(self, layout: Any) -> None:
        """Deep-copy *layout* with a new id/name and append it to the document."""
        new_id = self._new_unique_id()
        new_name = f"{layout.name} (copy)"
        try:
            copy = layout.model_copy(deep=True, update={"id": new_id, "name": new_name})
        except Exception:
            return
        self._document.screen_layouts.append(copy)
        self.load_document(self._document)
        self._save_document()

    # ------------------------------------------------------------------
    # Launch helpers
    # ------------------------------------------------------------------

    def _launch_connection(self, conn: Any) -> None:
        """Launch *conn* via SessionService if available; surface errors in status bar."""
        if self._services is None:
            self.statusBar().showMessage(
                f"No services — cannot launch {conn.name or conn.id}", 3000
            )
            return
        session_svc = getattr(self._services, "session", None)
        if session_svc is None:
            self.statusBar().showMessage("Session service unavailable", 3000)
            return
        if not self._ensure_auth_for_connection(conn):
            return
        try:
            session_svc.launch(self._document, conn.id)
            self.statusBar().showMessage(f"Launched {conn.name or conn.id}", 3000)
        except Exception as exc:
            QMessageBox.warning(self, "Launch Failed", str(exc))

    def _ensure_auth_for_connection(self, conn: Any) -> bool:
        """Run the Round C auth prompt for *conn* if needed.

        Returns True when the launch should proceed, False to abort.
        Persists any user choice (auth_method / key_deployed / identity_file_ref)
        to the document.
        """
        # Only SSH-using profiles need this gate
        profile = getattr(conn, "launch_profile", "")
        if profile not in ("claude-remote", "ssh-shell", "custom"):
            return True
        auth_method = getattr(conn, "auth_method", "ask")
        key_deployed = getattr(conn, "key_deployed", False)

        # Already configured paths
        if auth_method == "password":
            return True
        if auth_method == "key" and key_deployed:
            return True

        # Sibling inheritance: when several connections share the same
        # (user, host, port) — e.g. three SaaS connections to the same
        # box differing only in project_folder — deciding auth for one
        # is sufficient for all.  Copy the sibling's auth state (key or
        # password) onto this connection and skip the probe/prompt.
        sibling = self._find_decided_sibling(conn)
        if sibling is not None:
            sib_method = getattr(sibling, "auth_method", "key")
            self._persist_auth_choice(
                conn,
                auth_method=sib_method,
                key_deployed=getattr(sibling, "key_deployed", False),
                identity_file_ref=getattr(sibling, "identity_file_ref", None),
            )
            return True

        # Before prompting, see if a system SSH key (~/.ssh/id_ed25519,
        # id_rsa, …) already authenticates to this host.  When one does:
        #  1. register it in document.ssh_keys (if not already there)
        #  2. mark this connection as key+deployed with that identity_file_ref
        # so siblings can inherit and the user can manage the key from
        # the SSH Keys dialog.  Falls through to the prompt only when no
        # candidate works.
        working_key = self._identify_working_ssh_key(conn)
        if working_key is not None:
            key_id = self._register_system_ssh_key(working_key)
            if key_id is not None:
                self._persist_auth_choice(
                    conn,
                    auth_method="key",
                    key_deployed=True,
                    identity_file_ref=key_id,
                )
                return True

        # No silent path worked → ask the user
        choice = self._prompt_auth_choice(conn)
        if choice == "cancel":
            return False
        if choice == "password":
            self._persist_auth_choice(conn, auth_method="password")
            return True
        # choice == "key"
        return self._deploy_key_for_connection(conn)

    def _find_decided_sibling(self, conn: Any) -> Any | None:
        """Return another connection sharing *conn*'s (user, host, port)
        triplet whose auth method has been explicitly decided (either
        ``password`` or ``key`` with a deployed key + identity_file_ref).

        Used to short-circuit the auth prompt when the user maintains
        multiple connections to the same host — deciding once propagates
        to all siblings.
        """
        if self._document is None:
            return None
        host = getattr(conn, "host", None)
        user = getattr(conn, "user", None)
        port = getattr(conn, "port", 22) or 22
        if not host or not user:
            return None
        for other in self._document.connections:
            if other.id == conn.id:
                continue
            if getattr(other, "host", None) != host:
                continue
            if getattr(other, "user", None) != user:
                continue
            if (getattr(other, "port", 22) or 22) != port:
                continue
            method = getattr(other, "auth_method", "ask")
            if method == "password":
                return other
            if (
                method == "key"
                and getattr(other, "key_deployed", False)
                and getattr(other, "identity_file_ref", None)
            ):
                return other
        return None

    def _identify_working_ssh_key(self, conn: Any) -> Path | None:
        """Try each common system SSH key one at a time; return the path
        of the first that authenticates non-interactively to *conn*.

        Forces ``IdentitiesOnly=yes`` so each test isolates a single key
        rather than relying on the agent's full key list.  Returns None
        when no candidate works.
        """
        host = getattr(conn, "host", None)
        user = getattr(conn, "user", None)
        port = getattr(conn, "port", 22) or 22
        if not host or not user:
            return None
        candidates = [
            Path("~/.ssh/id_ed25519").expanduser(),
            Path("~/.ssh/id_ecdsa").expanduser(),
            Path("~/.ssh/id_rsa").expanduser(),
            Path("~/.ssh/id_dsa").expanduser(),
        ]
        import subprocess as _subprocess
        for key_path in candidates:
            if not key_path.exists():
                continue
            argv = [
                "ssh",
                "-p", str(port),
                "-o", "BatchMode=yes",
                "-o", "IdentitiesOnly=yes",
                "-o", "ConnectTimeout=5",
                "-o", "StrictHostKeyChecking=accept-new",
                "-i", str(key_path),
                f"{user}@{host}", "true",
            ]
            try:
                result = _subprocess.run(
                    argv, capture_output=True, timeout=8, text=True,
                )
            except Exception:
                continue
            if result.returncode == 0:
                _dd_log.info(
                    "system key %s authenticates to %s@%s:%s",
                    key_path, user, host, port,
                )
                return key_path
        return None

    def _register_system_ssh_key(self, priv_path: Path) -> str | None:
        """Ensure *priv_path* (and its .pub) exist as an entry in
        ``document.ssh_keys``.  Returns the key.id, or None if the
        document is missing or the public file can't be located.

        Idempotent — if a key with the same private_path already exists,
        return its id without modifying the list.
        """
        if self._document is None:
            return None
        priv_str = str(priv_path)
        for existing in self._document.ssh_keys:
            if Path(existing.private_path).expanduser() == priv_path:
                return existing.id
        pub_path = priv_path.with_suffix(priv_path.suffix + ".pub") \
            if priv_path.suffix \
            else Path(str(priv_path) + ".pub")
        if not pub_path.exists():
            _dd_log.warning(
                "system key %s found but %s missing — not registering",
                priv_path, pub_path,
            )
            return None
        # Build a unique slug-safe id from the file basename.
        from cpsm.data.schema import SshKey
        base = priv_path.name.lower().replace("_", "-")
        existing_ids = {k.id for k in self._document.ssh_keys}
        candidate = f"system-{base}"
        candidate = re.sub(r"[^a-z0-9-]", "-", candidate).strip("-") or "system-key"
        new_id = candidate
        suffix = 2
        while new_id in existing_ids:
            new_id = f"{candidate}-{suffix}"
            suffix += 1
        # Infer key type from filename.
        if "ed25519" in base:
            key_type = "ed25519"
        elif "ecdsa" in base:
            key_type = "ecdsa"
        else:
            key_type = "rsa"
        new_key = SshKey(
            id=new_id,
            name=f"System {priv_path.name}",
            type=key_type,  # type: ignore[arg-type]
            private_path=priv_str,
            public_path=str(pub_path),
        )
        self._document.ssh_keys = [*self._document.ssh_keys, new_key]
        self._save_document()
        _dd_log.info(
            "registered system SSH key '%s' (%s) in document",
            new_id, priv_str,
        )
        return new_id

    def _prompt_auth_choice(self, conn: Any) -> str:
        """Show the three-way auth-method prompt. Return one of
        {"key", "password", "cancel"}.
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("SSH Authentication")
        box.setText(
            f"How should CPSM authenticate to "
            f"<b>{conn.name or conn.id}</b>?"
        )
        box.setInformativeText(
            "Choose 'Deploy SSH key' to push your public key to the host "
            "(you'll be asked for the SSH password once). Choose 'Always "
            "use password' to skip key deployment for this connection. "
            "Your choice is remembered — change it later in the connection "
            "editor."
        )
        deploy_btn = box.addButton("Deploy SSH key", QMessageBox.ButtonRole.AcceptRole)
        password_btn = box.addButton("Always use password", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("Cancel launch", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(deploy_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is deploy_btn:
            return "key"
        if clicked is password_btn:
            return "password"
        if clicked is cancel_btn:
            return "cancel"
        return "cancel"

    def _persist_auth_choice(
        self,
        conn: Any,
        *,
        auth_method: str,
        key_deployed: bool | None = None,
        identity_file_ref: str | None | object = ...,
    ) -> None:
        """Update the connection in the document with the new auth fields,
        then save. Uses model_copy because Pydantic v2 models with
        ``extra=forbid`` are immutable by convention here."""
        if self._document is None:
            return
        idx = next(
            (i for i, c in enumerate(self._document.connections) if c.id == conn.id),
            None,
        )
        if idx is None:
            return
        update: dict[str, Any] = {"auth_method": auth_method}
        if key_deployed is not None:
            update["key_deployed"] = key_deployed
        if identity_file_ref is not ...:
            update["identity_file_ref"] = identity_file_ref
        new_conn = self._document.connections[idx].model_copy(update=update)
        self._document.connections[idx] = new_conn
        self._save_document()
        _dd_log.info(
            "auth choice persisted for %s: %s", conn.id, update,
        )

    def _deploy_key_for_connection(self, conn: Any) -> bool:
        """Open the key picker + DeployKeyDialog. On success,
        persist auth_method='key', key_deployed=True, identity_file_ref.
        Returns True if launch should proceed, False to abort.

        The picker shows every candidate key — registered (``document.
        ssh_keys``) plus unregistered ones at ``~/.ssh/`` — annotated as
        encrypted/unencrypted, with unencrypted ones first.  Encrypted
        keys depend on a working ssh-agent at launch time; unencrypted
        keys ssh can load straight from disk.
        """
        if self._document is None or self._services is None:
            return False
        candidates = self._gather_deploy_candidate_keys()
        if not candidates:
            QMessageBox.information(
                self,
                "No SSH Keys",
                "No SSH keys are configured and none were found at "
                "<b>~/.ssh/</b>.  Open <b>Tools → SSH Keys</b> to create "
                "one, then try launching again.",
            )
            return False

        # Build display labels.  Each label format:
        #   "<name> [<id>] — unencrypted"
        #   "id_rsa (~/.ssh) — unencrypted (will be added to SSH Keys)"
        labels: list[str] = []
        for cand in candidates:
            label = cand["label"]
            label += " — unencrypted" if not cand["encrypted"] else " — encrypted (needs agent)"
            if cand["registered_id"] is None:
                label += " (will be added to SSH Keys)"
            labels.append(label)

        # Auto-pick if exactly one candidate; otherwise ask.
        if len(candidates) == 1:
            chosen_idx = 0
        else:
            from PySide6.QtWidgets import QInputDialog
            label, ok = QInputDialog.getItem(
                self,
                "Pick SSH Key",
                f"Select an SSH key to deploy to {conn.name or conn.id}:\n"
                "Unencrypted keys work without ssh-agent; encrypted keys "
                "need the agent loaded with the passphrase.",
                labels,
                0,
                False,
            )
            if not ok:
                return False
            chosen_idx = labels.index(label)

        chosen = candidates[chosen_idx]
        # Register system keys before deploy so identity_file_ref points
        # to a real ssh_keys entry.
        try:
            if chosen["registered_id"] is None:
                new_id = self._register_system_ssh_key(chosen["private_path"])
                if new_id is None:
                    QMessageBox.warning(
                        self,
                        "Could not register key",
                        f"Failed to register {chosen['private_path']} in the "
                        "SSH Keys list (missing .pub?).",
                    )
                    return False
                chosen["registered_id"] = new_id
            # Look up the SshKey object for the deploy dialog.
            key_obj = next(
                (k for k in self._document.ssh_keys if k.id == chosen["registered_id"]),
                None,
            )
            if key_obj is None:
                QMessageBox.warning(
                    self,
                    "Key registration failed",
                    f"Registered key '{chosen['registered_id']}' is missing "
                    "from the SSH Keys list after registration — aborting "
                    "deploy.",
                )
                return False
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Could not prepare deploy",
                f"Error preparing to deploy {chosen.get('private_path', '?')}:\n\n{exc}",
            )
            return False

        # Open the existing DeployKeyDialog
        from cpsm.ui.dialogs.deploy_key import DeployKeyDialog
        key_service = getattr(self._services, "key_service", None)
        if key_service is None:
            QMessageBox.warning(
                self,
                "KeyService unavailable",
                "SSH key deployment requires the KeyService — not available.",
            )
            return False
        try:
            dlg = DeployKeyDialog(
                key=key_obj,
                candidates=[conn],
                key_service=key_service,
                parent=self,
            )
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Could not open Deploy dialog",
                f"Failed to open the Deploy SSH Key dialog:\n\n{exc}",
            )
            return False
        from PySide6.QtWidgets import QDialog
        result = dlg.exec()
        if result != QDialog.DialogCode.Accepted:
            return False
        # Post-deploy diagnostic: when the user picked an encrypted key,
        # warn if the local agent can't sign with it (the "agent refused
        # operation" scenario).  The deploy itself succeeded — the warning
        # is so launches don't silently fall through to password.
        if chosen["encrypted"]:
            self._warn_if_agent_cannot_sign(key_obj, conn)
        # Persist the new auth state
        self._persist_auth_choice(
            conn,
            auth_method="key",
            key_deployed=True,
            identity_file_ref=key_obj.id,
        )
        return True

    def _gather_deploy_candidate_keys(self) -> list[dict[str, Any]]:
        """Return a list of candidate keys for deployment.

        Each entry: ``{"label", "private_path", "encrypted", "registered_id"}``.
        Combines ``document.ssh_keys`` with unregistered private keys
        found at ``~/.ssh/`` (anything with a matching ``.pub``).
        Sorted with unencrypted keys first.
        """
        if self._document is None:
            return []
        candidates: list[dict[str, Any]] = []
        seen_paths: set[Path] = set()

        for k in self._document.ssh_keys:
            try:
                priv = Path(k.private_path).expanduser()
            except Exception:
                continue
            seen_paths.add(priv)
            if not priv.exists():
                continue  # skip orphaned ssh_keys entries
            candidates.append({
                "label": f"{k.name} [{k.id}]",
                "private_path": priv,
                "encrypted": self._is_key_encrypted(priv),
                "registered_id": k.id,
            })

        ssh_dir = Path("~/.ssh").expanduser()
        if ssh_dir.is_dir():
            for entry in sorted(ssh_dir.iterdir()):
                if not entry.is_file() or entry.suffix == ".pub":
                    continue
                if entry in seen_paths:
                    continue
                pub = Path(str(entry) + ".pub")
                if not pub.exists():
                    continue
                try:
                    head = entry.read_text(encoding="utf-8", errors="ignore")[:80]
                except Exception:
                    continue
                if "PRIVATE KEY" not in head:
                    continue
                candidates.append({
                    "label": f"{entry.name} (~/.ssh)",
                    "private_path": entry,
                    "encrypted": self._is_key_encrypted(entry),
                    "registered_id": None,
                })

        # Unencrypted first; then alphabetical by label.
        candidates.sort(key=lambda c: (c["encrypted"], c["label"].lower()))
        return candidates

    def _is_key_encrypted(self, priv_path: Path) -> bool:
        """Return True if *priv_path* is a passphrase-encrypted private key.

        Uses ``ssh-keygen -y -P "" -f <path>``: exit code 0 means the key
        loads with an empty passphrase (i.e. unencrypted).  Any failure
        is conservatively treated as encrypted.
        """
        import subprocess as _subprocess
        try:
            result = _subprocess.run(
                ["ssh-keygen", "-y", "-P", "", "-f", str(priv_path)],
                capture_output=True, timeout=5, text=True,
            )
        except Exception:
            return True
        return result.returncode != 0

    def _warn_if_agent_cannot_sign(self, key: Any, conn: Any) -> None:
        """If *key* is in the ssh-agent but the agent refuses to sign
        (the classic "agent refused operation" failure), surface a
        warning explaining how to fix it.  Silent on success or when
        the agent simply doesn't have the key loaded.

        This probe runs synchronously on the UI thread, so the timeouts
        are deliberately tight — a slow/unreachable host produces a
        silent false-negative (no warning) rather than a UI freeze.
        Worst-case wait is ConnectTimeout + a brief auth round-trip,
        bounded by the outer subprocess timeout.
        """
        import subprocess as _subprocess
        priv_path = Path(getattr(key, "private_path", "")).expanduser()
        if not priv_path.exists():
            return
        host = getattr(conn, "host", None)
        user = getattr(conn, "user", None)
        port = getattr(conn, "port", 22) or 22
        if not host or not user:
            return
        # Probe ssh with agent-only auth and this key's identity. If the
        # agent has the key but refuses, we'll see "agent refused" in
        # stderr or get rc != 0 with a publickey failure mode.
        argv = [
            "ssh",
            "-p", str(port),
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=3",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "PreferredAuthentications=publickey",
            "-i", str(priv_path),
            f"{user}@{host}", "true",
        ]
        try:
            result = _subprocess.run(
                argv, capture_output=True, timeout=4, text=True,
            )
        except Exception:
            return
        if result.returncode == 0:
            return  # auth works → no problem
        stderr = result.stderr or ""
        if "agent refused" in stderr.lower():
            QMessageBox.warning(
                self,
                "ssh-agent refusing to sign",
                "The deploy succeeded — your public key is on the server — "
                "but your local <b>ssh-agent is refusing to sign</b> with "
                f"<code>{priv_path}</code>.  Launches will fall through to "
                "password prompts until you refresh the agent:<br><br>"
                "<pre>ssh-add -D\nssh-add ~/.ssh/id_ed25519</pre><br>"
                "(re-enter your passphrase once when prompted).  "
                "Alternatively, deploy an unencrypted key — the picker "
                "lists keys at <b>~/.ssh/</b> with their encryption status.",
            )

    def _launch_group(self, grp: Any) -> None:
        """Launch *grp* via SessionService if available; surface errors."""
        if self._services is None:
            self.statusBar().showMessage(f"No services — cannot launch group {grp.name}", 3000)
            return
        session_svc = getattr(self._services, "session", None)
        if session_svc is None:
            self.statusBar().showMessage("Session service unavailable", 3000)
            return
        # Pre-flight: ensure each member's auth is configured. If the user
        # cancels for any one member, abort the whole group launch so we
        # don't half-launch a group with ambiguous state.
        for conn_id in grp.members:
            conn = self._find_connection_by_id(conn_id)
            if conn is None:
                continue
            if not self._ensure_auth_for_connection(conn):
                self.statusBar().showMessage(
                    f"Group launch cancelled at {conn.name or conn.id}", 3000
                )
                return
        try:
            session_svc.launch_group(
                self._document, grp.id,
                monitors=self._query_live_monitors(),
            )
            self.statusBar().showMessage(f"Launched group {grp.name}", 3000)
        except Exception as exc:
            QMessageBox.warning(self, "Launch Failed", str(exc))

    def _toggle_inspector(self) -> None:
        """Toggle the inspector dock visibility (F4 shortcut)."""
        self._dock_inspector.setVisible(not self._dock_inspector.isVisible())

    # ------------------------------------------------------------------
    # Fix #1 — real action handlers
    # ------------------------------------------------------------------

    def _on_save_config_action(self) -> None:
        """Save the current config and show status feedback."""
        try:
            self._save_document()
            self.statusBar().showMessage(f"Saved to {self._config_path()}", 3000)
        except Exception as exc:
            QMessageBox.warning(self, "Save Config failed", str(exc))

    def _on_open_config_action(self) -> None:
        """Open a .cpsm.yaml file and load it into the window."""
        from PySide6.QtWidgets import QFileDialog

        try:
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Open Config",
                str(Path.home()),
                "CPSM Config (*.yaml *.yml);;All Files (*)",
            )
            if not path:
                return
            if self._services is None:
                return
            doc = self._services.config.load(Path(path))
            self._services.config_path = Path(path)
            self.load_document(doc)
            self.statusBar().showMessage(f"Opened {path}", 3000)
        except Exception as exc:
            QMessageBox.warning(self, "Open Config failed", str(exc))

    def _on_import_action(self) -> None:
        """Import a legacy .claude-projects.yaml file."""
        from PySide6.QtWidgets import QFileDialog

        try:
            # Confirm intent first — this action is only useful for
            # migrating an existing CC_multi.sh / claude-multi-manager.sh
            # config; new users have no such file.
            reply = QMessageBox.information(
                self,
                "Import legacy CC_multi config",
                "This imports a legacy <b>.claude-projects.yaml</b> file "
                "produced by the old <b>CC_multi.sh</b> / "
                "<b>claude-multi-manager.sh</b> shell tool.<br><br>"
                "It is only useful for migrating from the old setup. "
                "Continue?",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Ok:
                return
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Import Legacy CC_multi Config",
                str(Path.home()),
                "Legacy Config (*.yaml *.yml);;All Files (*)",
            )
            if not path:
                return
            from cpsm.services.import_service import ImportService

            source = Path(path)
            target = self._config_path()
            if target is None:
                QMessageBox.warning(self, "Import failed", "No config path set.")
                return
            ImportService().import_legacy_to(source, target)
            if self._services is not None:
                doc = self._services.config.load(target)
                self.load_document(doc)
            self.statusBar().showMessage(f"Imported from {path}", 3000)
        except Exception as exc:
            QMessageBox.warning(self, "Import failed", str(exc))

    def _on_validate_action(self) -> None:
        """Validate the current document and show results."""
        try:
            if self._services is None:
                self.statusBar().showMessage("No services — cannot validate", 3000)
                return
            issues = self._services.config.validate(self._document)
            if issues:
                from cpsm.ui.dialogs.validation_errors import ValidationErrorsDialog

                ValidationErrorsDialog(issues, parent=self).exec()
            else:
                self.statusBar().showMessage("✓ valid", 3000)
        except Exception as exc:
            QMessageBox.warning(self, "Validate failed", str(exc))

    def _get_selected_item(self) -> Any:
        """Return the currently selected Connection or Group, or None."""
        # Check sidebar selection first
        tree = self._session_list.tree
        item = tree.currentItem()
        if item is not None:
            parent = item.parent()
            if parent is not None:
                item_id: str = item.data(0, Qt.ItemDataRole.UserRole) or ""
                category_id: str = parent.data(0, Qt.ItemDataRole.UserRole) or ""
                if category_id == "category_cat_connections":
                    return self._find_connection_by_id(item_id)
                if category_id == "category_cat_groups":
                    return self._find_group_by_id(item_id)
        # Fall back to connections tab selection
        indexes = self._connections_tree.selectedIndexes()
        if indexes:
            row = indexes[0].row()
            if 0 <= row < len(self._document.connections):
                return self._document.connections[row]
        # Fall back to groups tab selection
        indexes = self._groups_list.selectedIndexes()
        if indexes:
            row = indexes[0].row()
            if 0 <= row < len(self._document.groups):
                return self._document.groups[row]
        return None

    def _on_launch_action(self) -> None:
        """Launch the selected connection or group."""
        try:
            item = self._get_selected_item()
            if isinstance(item, Group):
                self._launch_group(item)
            elif item is not None:
                self._launch_connection(item)
            else:
                self.statusBar().showMessage("Select a connection or group to launch", 3000)
        except Exception as exc:
            QMessageBox.warning(self, "Launch failed", str(exc))

    def _on_stop_action(self) -> None:
        """Stop the selected connection or group session."""
        try:
            item = self._get_selected_item()
            if isinstance(item, Group):
                self._stop_group(item)
            elif item is not None:
                self._stop_connection(item)
            else:
                self.statusBar().showMessage("Select a connection or group to stop", 3000)
        except Exception as exc:
            QMessageBox.warning(self, "Stop failed", str(exc))

    def _on_reconnect_action(self) -> None:
        """Reconnect the selected connection."""
        try:
            item = self._get_selected_item()
            if isinstance(item, Group):
                self.statusBar().showMessage("Select a single connection to reconnect", 3000)
            elif item is not None:
                self._reconnect_connection(item)
            else:
                self.statusBar().showMessage("Select a connection to reconnect", 3000)
        except Exception as exc:
            QMessageBox.warning(self, "Reconnect failed", str(exc))

    def _on_launch_scene_action(self) -> None:
        """Launch a scene chosen from a list dialog."""
        from PySide6.QtWidgets import QInputDialog

        try:
            if self._services is None:
                self.statusBar().showMessage("No services — cannot launch scene", 3000)
                return
            scene_ids = [s.id for s in self._document.scenes]
            if not scene_ids:
                self.statusBar().showMessage("No scenes defined", 3000)
                return
            scene_id, ok = QInputDialog.getItem(self, "Launch Scene", "Scene:", scene_ids, 0, False)
            if not ok or not scene_id:
                return
            self._services.session.launch_scene(self._document, scene_id)
            self.statusBar().showMessage(f"Launched scene {scene_id}", 3000)
        except Exception as exc:
            QMessageBox.warning(self, "Launch Scene failed", str(exc))

    def _on_launcher_templates_action(self) -> None:
        """Open the Launcher Templates dialog."""
        try:
            from cpsm.services.template_service import TemplateService
            from cpsm.ui.dialogs.launcher_templates import LauncherTemplatesDialog

            template_svc: TemplateService | None = (
                self._services.templates if self._services else None
            )
            if template_svc is None:
                self.statusBar().showMessage("Template service unavailable", 3000)
                return
            LauncherTemplatesDialog(
                template_service=template_svc,
                doc=self._document,
                parent=self,
            ).exec()
        except Exception as exc:
            QMessageBox.warning(self, "Launcher Templates failed", str(exc))

    def _on_find_action(self) -> None:
        """Focus the sidebar filter or the sidebar tree."""
        try:
            sidebar_filter = getattr(self._session_list, "filter_edit", None)
            if sidebar_filter is not None:
                sidebar_filter.setFocus()
            else:
                self._session_list.tree.setFocus()
        except Exception as exc:
            QMessageBox.warning(self, "Find failed", str(exc))

    def _on_toggle_live_preview_action(self) -> None:
        """Toggle the Live/Preview radio button in the Screens tab."""
        try:
            is_preview = self._radio_screens_preview.isChecked()
            if is_preview:
                self._radio_screens_live.setChecked(True)
            else:
                self._radio_screens_preview.setChecked(True)
        except Exception as exc:
            QMessageBox.warning(self, "Toggle Live/Preview failed", str(exc))

    def _on_manage_keys_action(self) -> None:
        """Open the SSH Key Manager dialog."""
        try:
            from cpsm.services.key_service import KeyService
            from cpsm.ui.dialogs.ssh_key_manager import SshKeyManagerDialog

            key_svc = (
                getattr(self._services, "key_service", None) if self._services else None
            ) or KeyService()
            SshKeyManagerDialog(
                doc=self._document,
                key_service=key_svc,
                parent=self,
            ).exec()
            self._save_document()
        except Exception as exc:
            QMessageBox.warning(self, "Keys dialog failed", str(exc))

    def _on_settings_action(self) -> None:
        """Open the Settings dialog."""
        from PySide6.QtWidgets import QDialog

        try:
            from cpsm.ui.dialogs.settings import SettingsDialog

            dlg = SettingsDialog(settings=self._document.settings, parent=self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                data = dlg.collect_data()
                from cpsm.data.schema import Settings

                try:
                    new_settings = Settings.model_validate(data)
                except Exception:
                    new_settings = self._document.settings
                    for k, v in data.items():
                        with __import__("contextlib").suppress(Exception):
                            object.__setattr__(new_settings, k, v)
                self._document = self._document.model_copy(update={"settings": new_settings})
                self._save_document()
        except Exception as exc:
            QMessageBox.warning(self, "Settings dialog failed", str(exc))

    # ------------------------------------------------------------------
    # Fix #3 — Stop methods
    # ------------------------------------------------------------------

    def _stop_connection(self, conn: Any) -> None:
        """Stop the tmux session for *conn*."""
        try:
            if self._services is None:
                self.statusBar().showMessage("No services — cannot stop", 3000)
                return
            sess_name = self._services.session.session_name(conn.id)
            self._services.session.kill_session(sess_name)
            self.statusBar().showMessage(f"Stopped {conn.id}", 3000)
        except Exception as exc:
            QMessageBox.warning(self, "Stop failed", str(exc))

    def _stop_group(self, grp: Any) -> None:
        """Stop tmux sessions for all members of *grp*."""
        if self._services is None:
            self.statusBar().showMessage("No services — cannot stop group", 3000)
            return
        errors: list[str] = []
        for cid in grp.members:
            try:
                sess_name = self._services.session.session_name(cid)
                self._services.session.kill_session(sess_name)
            except Exception as exc:
                errors.append(f"{cid}: {exc}")
        if errors:
            QMessageBox.warning(self, "Stop group — partial failure", "\n".join(errors))
        else:
            self.statusBar().showMessage(f"Stopped group {grp.id}", 3000)

    # ------------------------------------------------------------------
    # Fix #9 — Reconnect method
    # ------------------------------------------------------------------

    def _reconnect_connection(self, conn: Any) -> None:
        """Kill then relaunch *conn*."""
        try:
            if self._services is not None:
                try:
                    sess_name = self._services.session.session_name(conn.id)
                    self._services.session.kill_session(sess_name)
                except Exception:
                    pass
            self._launch_connection(conn)
            self.statusBar().showMessage(f"Reconnected {conn.id}", 3000)
        except Exception as exc:
            QMessageBox.warning(self, "Reconnect failed", str(exc))

    # ------------------------------------------------------------------
    # Fix #4 — ScreenMapWidget drop handlers
    # ------------------------------------------------------------------

    def _remove_existing_pane_with_connection_id(
        self, layout: ScreenLayout, connection_id: str, exclude: Any = None
    ) -> None:
        """Strip any pane in *layout* whose connection_id matches, except
        *exclude*. Round 2: drives the split-tree mutation via
        ``remove_pane_from_viewport`` (collapses single-child Splits up the
        tree and re-syncs ``vp.panes``).
        """
        from cpsm.data.schema import remove_pane_from_viewport
        for sm in layout.monitors:
            for vp in sm.viewports:
                # Snapshot to avoid mutating during iteration.
                victims = [
                    p for p in list(vp.panes)
                    if p is not exclude and p.connection_id == connection_id
                ]
                for p in victims:
                    remove_pane_from_viewport(vp, p)

    def _resync_tmux_to_layout(self, group_id: str | None = None) -> None:
        """Re-apply the document's layout to live tmux state for *group_id*
        (defaults to the active group).

        Only does anything if the group already has at least one running
        tmux session named ``cpsm-group-{group_id}-mon-*``. The re-launch
        path is in-place: alive panes stay, dead panes get respawned,
        excess panes are killed, missing panes are split-pane'd in,
        select-layout is reapplied. Terminals are spawned only for
        sessions with no attached client (i.e. fresh ones).
        """
        if self._services is None or self._document is None:
            return
        session_svc = getattr(self._services, "session", None)
        if session_svc is None:
            return
        gid = group_id or self._active_group_id()
        if not gid:
            return
        backend = getattr(session_svc, "_backend", None)
        if backend is None:
            return
        try:
            sessions = backend.list_sessions()
        except Exception:
            return
        prefix = f"cpsm-group-{gid}-mon-"
        if not any(s.name.startswith(prefix) for s in sessions):
            return  # group not launched — don't auto-launch on edits
        try:
            session_svc.launch_group(
                self._document, gid,
                monitors=self._query_live_monitors(),
            )
        except Exception as exc:
            _dd_log.warning("resync tmux to layout failed for %s: %s", gid, exc)

    def _add_connection_to_active_group_if_missing(self, connection_id: str) -> None:
        """If a connection is dropped onto the active group's canvas but
        isn't a member of that group yet, add it. Persists the change."""
        gid = self._active_group_id()
        if not gid or self._document is None:
            return
        grp_idx = next(
            (i for i, g in enumerate(self._document.groups) if g.id == gid), None
        )
        if grp_idx is None:
            return
        grp = self._document.groups[grp_idx]
        if connection_id in grp.members:
            return
        updated = grp.model_copy(update={"members": [*grp.members, connection_id]})
        self._document.groups[grp_idx] = updated
        _dd_log.info(
            "auto-added %s to group %s (members now: %s)",
            connection_id, gid, updated.members,
        )

    def _find_pane_location_in_layout(
        self, layout: ScreenLayout, target_pane_id: str
    ) -> tuple[Any, int] | None:
        """Return (viewport, pane_index_within_viewport) for *target_pane_id*.

        Round C: pane_ids are always serial-based ``__pane_N`` (or the
        legacy ``__empty_N`` for old empty-pane records). The walk
        mirrors ``ScreenMapWidget._build_scene_with_registry`` so the
        serial increments for every leaf in tree-leaf order across all
        viewports of all monitors.
        """
        serial = 0
        for schema_monitor in layout.monitors:
            for vp in schema_monitor.viewports:
                for idx, pane in enumerate(vp.panes):
                    used_id = (
                        f"__pane_{serial}"
                        if not target_pane_id.startswith("__empty_")
                        else (pane.connection_id or f"__empty_{serial}")
                    )
                    if used_id == target_pane_id:
                        return vp, idx
                    serial += 1
        # Backwards-compat: handlers that still pass a connection_id
        # directly (e.g. external scripts or legacy tests) — fall back
        # to a name-based lookup, returning the FIRST match.
        for schema_monitor in layout.monitors:
            for vp in schema_monitor.viewports:
                for idx, pane in enumerate(vp.panes):
                    if pane.connection_id == target_pane_id:
                        return vp, idx
        return None

    def _find_pane_in_layout_by_serial(
        self, layout: ScreenLayout, target_pane_id: str
    ) -> Pane | None:
        """Locate Pane by pane_id using the same serial counting as
        ``ScreenMapWidget._build_scene_with_registry``.

        Round C: pane_ids are always serial-based (``__pane_N``); legacy
        ``__empty_N`` ids still resolve via the connection_id-or-empty
        fallback path. As a final fallback, plain connection_id lookups
        return the first match.
        """
        serial = 0
        for schema_monitor in layout.monitors:
            for vp in schema_monitor.viewports:
                for pane in vp.panes:
                    used_id = (
                        f"__pane_{serial}"
                        if not target_pane_id.startswith("__empty_")
                        else (pane.connection_id or f"__empty_{serial}")
                    )
                    serial += 1
                    if used_id == target_pane_id:
                        return pane
        # Diagnostic: serial walk failed. Log what the layout actually has.
        snapshot = []
        s = 0
        for schema_monitor in layout.monitors:
            for vp in schema_monitor.viewports:
                for pane in vp.panes:
                    snapshot.append(f"__pane_{s}={pane.connection_id}")
                    s += 1
        _dd_log.warning(
            "_find_pane_in_layout_by_serial: target=%s NOT FOUND. layout id=%s "
            "snapshot=[%s]",
            target_pane_id, layout.id, ", ".join(snapshot),
        )
        for schema_monitor in layout.monitors:
            for vp in schema_monitor.viewports:
                for pane in vp.panes:
                    if pane.connection_id == target_pane_id:
                        return pane
        return None

    def _on_screen_map_drop_connection(
        self,
        connection_id: str,
        target_pane_id: str,
        zone: str,
        modifiers: int,
    ) -> None:
        """Handle a connection drop from the Screens-tab ScreenMapWidget.

        Preview mode: mutate the document directly (replace target pane's
        connection_id) then persist and refresh the canvas.

        Live mode: also call LayoutController so the tmux session stays in sync.
        """
        is_preview = self._radio_screens_preview.isChecked()
        _dd_log.info(
            "_on_screen_map_drop_connection: conn=%s pane=%s zone=%s is_preview=%s",
            connection_id, target_pane_id, zone, is_preview,
        )

        if self._document is None:
            return

        if is_preview:
            # Use the layout the canvas is actually rendering as the source of
            # truth — it's a reference into self._document.screen_layouts, so
            # mutating it persists on save. Falling back to id-lookup masks
            # mismatches when the rendered layout is a different object than
            # the one resolved via group.default_layout_id (e.g., when the
            # group's default points at a stale id while the canvas shows a
            # newer layout).
            layout = getattr(self._screen_map_widget, "_layout_data", None)
            if layout is None or not layout.monitors:
                # No canvas layout — try id-lookup as a last resort
                combo = self._combo_screens_group
                group_id: str | None = combo.currentData() if combo.count() > 0 else None
                if group_id:
                    grp = self._find_group_by_id(group_id)
                    if grp and grp.default_layout_id:
                        layout = self._find_layout_by_id(grp.default_layout_id)
            _dd_log.info(
                "_on_screen_map_drop_connection: layout=%s (id=%s) panes=%s",
                layout.id if layout else None,
                id(layout) if layout else None,
                [p.connection_id for m in (layout.monitors if layout else []) for v in m.viewports for p in v.panes],
            )
            if layout is None:
                return

            located = self._find_pane_location_in_layout(layout, target_pane_id)
            _dd_log.info(
                "_on_screen_map_drop_connection: located=%s zone=%s",
                located is not None, zone,
            )
            if located is None:
                _dd_log.warning(
                    "_on_screen_map_drop_connection: pane '%s' not found in layout '%s'",
                    target_pane_id,
                    layout.id,
                )
                return

            vp, pane_idx = located
            target_pane = vp.panes[pane_idx]

            # Prevent duplication: if the dropped connection_id is already
            # somewhere else in the layout, remove the old instance so the
            # drop becomes a MOVE rather than a duplicate.
            self._remove_existing_pane_with_connection_id(
                layout, connection_id, exclude=target_pane
            )

            if zone == "center":
                # Replace target's connection_id (mutates the Pane in place,
                # which also updates the corresponding leaf in split_tree
                # since they share identity).
                target_pane.connection_id = connection_id
                _dd_log.info(
                    "_on_screen_map_drop_connection: REPLACE conn=%s at pane_idx=%d",
                    connection_id, pane_idx,
                )
            else:
                # Edge zone → split. Round 2: mutate the structured split
                # tree (supports mixed-orientation layouts like quadrants).
                from typing import cast as _cast

                from cpsm.data.schema import Pane as _Pane
                from cpsm.data.schema import split_pane_in_viewport
                if zone not in ("left", "right", "top", "bottom"):
                    return
                edge_zone: Literal["left", "right", "top", "bottom"] = _cast(
                    Literal["left", "right", "top", "bottom"], zone,
                )
                new_pane = _Pane(connection_id=connection_id)
                ok = split_pane_in_viewport(vp, target_pane, edge_zone, new_pane)
                if not ok:
                    _dd_log.warning(
                        "_on_screen_map_drop_connection: tree split failed; "
                        "target leaf not in vp.split_tree"
                    )
                    return
                _dd_log.info(
                    "_on_screen_map_drop_connection: SPLIT zone=%s conn=%s",
                    zone, connection_id,
                )

            # Auto-join the connection to the active group if needed
            self._add_connection_to_active_group_if_missing(connection_id)
            self._save_document()
            # Re-render the SAME layout we just mutated. Going through
            # _refresh_screens_preview can resolve a different layout via
            # group.default_layout_id and visually erase the just-edited one.
            self._screen_map_widget.set_layout(layout, self._query_live_monitors())
            self._refresh_screens_members_list()
            self._refresh_sidebar_membership_highlights()
            # Propagate to live tmux state if the group is already running.
            self._resync_tmux_to_layout()
        else:
            # Live mode — also drive the tmux backend
            if self._layout_controller is not None:
                try:
                    self._layout_controller.on_drop_connection(
                        connection_id, target_pane_id, zone, modifiers
                    )
                except Exception as exc:
                    QMessageBox.warning(self, "Drop failed", str(exc))
                    return
            # Mutate document side as well (best-effort; layout may not be found in Live)
            combo = self._combo_screens_group
            group_id = combo.currentData() if combo.count() > 0 else None
            if group_id:
                grp = self._find_group_by_id(group_id)
                if grp and grp.default_layout_id:
                    layout = self._find_layout_by_id(grp.default_layout_id)
                    if layout:
                        pane = self._find_pane_in_layout_by_serial(layout, target_pane_id)
                        if pane is not None:
                            pane.connection_id = connection_id
                            self._save_document()
            _dd_log.info(
                "_on_screen_map_drop_connection: live mode refresh, calling save+refresh",
            )
            self._refresh_screens_members_list()

    def _on_screen_map_drop_pane(
        self,
        src_pane_id: str,
        dst_pane_id: str,
        zone: str,
        modifiers: int,
    ) -> None:
        """Handle a pane-to-pane drop from ScreenMapWidget.

        Preview mode: mutate the document directly.
          - zone == "center"  → SWAP the connection_ids of src and dst panes.
          - edge zone         → INSERT a new pane in dst's viewport whose
                                connection_id = src's connection_id (split/copy).
                                Assumption: we do NOT remove src from its source
                                viewport — the user may want duplication; move
                                semantics can be obtained by dropping and then
                                removing via the right-click menu.

        Live mode: also call LayoutController (existing behaviour) AND apply
        the document mutation so save state stays consistent.
        """
        if self._document is None:
            return

        is_preview = self._radio_screens_preview.isChecked()
        _dd_log.info(
            "_on_screen_map_drop_pane: src=%s dst=%s zone=%s is_preview=%s",
            src_pane_id, dst_pane_id, zone, is_preview,
        )

        # Locate current layout
        combo = self._combo_screens_group
        group_id: str | None = combo.currentData() if combo.count() > 0 else None
        layout = None
        if group_id:
            grp = self._find_group_by_id(group_id)
            if grp and grp.default_layout_id:
                layout = self._find_layout_by_id(grp.default_layout_id)

        def _apply_pane_drop_mutation() -> bool:
            """Mutate the document; return True on success."""
            if layout is None or not dst_pane_id:
                return False

            src_pane = self._find_pane_in_layout_by_serial(layout, src_pane_id)
            dst_pane = self._find_pane_in_layout_by_serial(layout, dst_pane_id)

            _dd_log.info(
                "_on_screen_map_drop_pane: src_pane_found=%s dst_pane_found=%s",
                src_pane is not None, dst_pane is not None,
            )

            if src_pane is None or dst_pane is None:
                return False

            if zone == "center":
                # MOVE: dst takes src's connection_id; src leaf is removed
                # from the tree (collapsing single-child Splits up the
                # tree). Round C: must go through the tree so the
                # registry serial counts (which walk the tree) stay in
                # sync with vp.panes (re-derived from tree leaves).
                if src_pane is dst_pane:
                    _dd_log.info("_on_screen_map_drop_pane: MOVE skipped (src is dst)")
                    return False
                from cpsm.data.schema import remove_pane_from_viewport
                # Find src's containing viewport
                src_vp = None
                for schema_monitor in layout.monitors:
                    for vp in schema_monitor.viewports:
                        if any(p is src_pane for p in vp.panes):
                            src_vp = vp
                            break
                    if src_vp is not None:
                        break
                if src_vp is None:
                    return False
                src_cid = src_pane.connection_id
                dst_cid_old = dst_pane.connection_id
                dst_pane.connection_id = src_cid
                remove_pane_from_viewport(src_vp, src_pane)
                _dd_log.info(
                    "_on_screen_map_drop_pane: MOVE-replace applied src_cid=%s dst_freed=%s",
                    src_cid, dst_cid_old,
                )
            else:
                # SPLIT — MOVE src to dst's viewport (insert next to dst), removing
                # src from its source viewport. Set dst viewport's tmux_layout to
                # match the visual split direction.
                src_vp = None
                dst_vp = None
                for schema_monitor in layout.monitors:
                    for vp in schema_monitor.viewports:
                        for pane in vp.panes:
                            if pane is src_pane:
                                src_vp = vp
                            if pane is dst_pane:
                                dst_vp = vp
                if dst_vp is None or src_vp is None:
                    return False
                if src_pane is dst_pane:
                    _dd_log.info("_on_screen_map_drop_pane: SPLIT skipped (src is dst)")
                    return False

                # Round 2: tree-based MOVE. Remove src from its source
                # viewport's tree (collapse single-child Splits as needed),
                # then split dst's tree at dst_pane to insert src.
                from typing import cast as _cast

                from cpsm.data.schema import (
                    remove_pane_from_viewport,
                    split_pane_in_viewport,
                )
                remove_pane_from_viewport(src_vp, src_pane)
                if zone not in ("left", "right", "top", "bottom"):
                    # Defensive: should never happen since center handled
                    # earlier, but keep edge-zone narrowing for the typer.
                    return False
                edge_zone: Literal["left", "right", "top", "bottom"] = _cast(
                    Literal["left", "right", "top", "bottom"], zone,
                )
                ok = split_pane_in_viewport(dst_vp, dst_pane, edge_zone, src_pane)
                if not ok:
                    _dd_log.warning(
                        "_on_screen_map_drop_pane: tree split failed; "
                        "dst_pane not in dst_vp.split_tree"
                    )
                    return False
                _dd_log.info(
                    "_on_screen_map_drop_pane: MOVE applied conn=%s "
                    "src_vp=%s dst_vp=%s zone=%s",
                    src_pane.connection_id, src_vp.id, dst_vp.id, zone,
                )
            return True

        if is_preview:
            if _apply_pane_drop_mutation():
                self._save_document()
                self._refresh_screens_preview()
                self._refresh_screens_members_list()
                self._resync_tmux_to_layout()
        else:
            # Live mode: drive tmux backend AND mutate document
            if self._layout_controller is not None:
                try:
                    self._layout_controller.on_drop_pane(
                        src_pane_id, dst_pane_id, zone, modifiers
                    )
                    self.load_document(self._document)
                except Exception as exc:
                    QMessageBox.warning(self, "Drop failed", str(exc))
                    return
            # Also apply document mutation for save-state consistency
            if _apply_pane_drop_mutation():
                self._save_document()
            self._refresh_screens_members_list()

    def _on_screen_map_drop_connection_on_viewport(
        self,
        connection_id: str,
        viewport_id: str,
        modifiers: int,
    ) -> None:
        """Handle a connection dropped onto an empty viewport (no panes).

        Preview mode: append a new Pane(connection_id=connection_id) to the
        viewport with *viewport_id* in the currently-displayed layout, persist,
        and refresh the canvas.

        Live mode: same document mutation + log a warning that tmux-side append
        is a follow-up item (backend entry point does not exist yet).
        """
        if self._document is None:
            return

        is_preview = self._radio_screens_preview.isChecked()
        _dd_log.info(
            "_on_screen_map_drop_connection_on_viewport: conn=%s vp=%s is_preview=%s",
            connection_id, viewport_id, is_preview,
        )

        # Locate the currently displayed layout
        combo = self._combo_screens_group
        group_id: str | None = combo.currentData() if combo.count() > 0 else None
        layout = None
        if group_id:
            grp = self._find_group_by_id(group_id)
            if grp and grp.default_layout_id:
                layout = self._find_layout_by_id(grp.default_layout_id)
        _dd_log.info(
            "_on_screen_map_drop_connection_on_viewport: group=%s layout=%s",
            group_id, layout.id if layout else None,
        )
        if layout is None:
            return

        # Find the viewport in the layout
        target_vp = None
        for schema_monitor in layout.monitors:
            for vp in schema_monitor.viewports:
                if vp.id == viewport_id:
                    target_vp = vp
                    break
            if target_vp is not None:
                break

        _dd_log.info(
            "_on_screen_map_drop_connection_on_viewport: vp_found=%s", target_vp is not None
        )
        if target_vp is None:
            _dd_log.warning(
                "_on_screen_map_drop_connection_on_viewport: viewport '%s' not found",
                viewport_id,
            )
            return

        # Prevent duplication: drop becomes a MOVE if the connection already
        # has a pane in this layout.
        self._remove_existing_pane_with_connection_id(layout, connection_id)
        new_pane = Pane(connection_id=connection_id)
        # Round 2: append on the tree (right-of-last-leaf horizontal split)
        # so freeform layouts persist. If the viewport is empty, the new
        # pane becomes the tree root.
        if target_vp.split_tree is None and not target_vp.panes:
            target_vp.split_tree = new_pane
            target_vp.panes = [new_pane]
        else:
            from cpsm.data.schema import _flatten_split_tree_leaves, split_pane_in_viewport
            anchor: Pane | None = None
            if target_vp.split_tree is not None:
                leaves = _flatten_split_tree_leaves(target_vp.split_tree)
                if leaves:
                    anchor = leaves[-1]
            if anchor is None:
                # Tree missing but flat panes present (legacy state) — fall
                # back to plain append; migration validator will rebuild
                # the tree on next load.
                target_vp.panes.append(new_pane)
            else:
                split_pane_in_viewport(target_vp, anchor, "right", new_pane)
        # Auto-join active group if needed.
        self._add_connection_to_active_group_if_missing(connection_id)
        _dd_log.info(
            "_on_screen_map_drop_connection_on_viewport: pane appended conn=%s, calling save+refresh",
            connection_id,
        )
        self._save_document()
        self._refresh_screens_preview()
        self._refresh_sidebar_membership_highlights()
        self._refresh_screens_members_list()
        self._resync_tmux_to_layout()

        if not is_preview:
            _dd_log.warning(
                "_on_screen_map_drop_connection_on_viewport: Live-mode tmux append "
                "for viewport '%s' is not yet implemented; document was updated.",
                viewport_id,
            )

    def _on_screen_map_pane_clicked(self, connection_id: str) -> None:
        """Mirror a pane click on the canvas as a sidebar selection — the
        Inspector dock then populates via the existing currentItemChanged
        wiring. No-op when the connection isn't found in the sidebar tree.
        """
        if not connection_id or not hasattr(self, "_session_list"):
            return
        cat = self._session_list._cat_connections
        for i in range(cat.childCount()):
            item = cat.child(i)
            if item.data(0, Qt.ItemDataRole.UserRole) == connection_id:
                self._session_list.tree.setCurrentItem(item)
                # Belt-and-suspenders: in case currentItemChanged isn't
                # emitted (e.g. already-selected), populate Inspector
                # directly so the user sees the update immediately.
                conn = self._find_connection_by_id(connection_id)
                if conn is not None:
                    self._show_inspector_for(conn)
                return

    def _on_screen_map_drop_pane_on_viewport(
        self,
        src_pane_id: str,
        viewport_id: str,
        modifiers: int,
    ) -> None:
        """Handle a pane dragged onto an empty viewport (or empty monitor area).

        MOVE the src pane out of its source viewport into the target viewport.
        """
        if self._document is None:
            return
        _dd_log.info(
            "_on_screen_map_drop_pane_on_viewport: src=%s vp=%s",
            src_pane_id, viewport_id,
        )

        layout = getattr(self._screen_map_widget, "_layout_data", None)
        if layout is None:
            return

        # Locate src pane via the canonical serial-aware lookup (Round C).
        # Earlier code matched by connection_id which never works because
        # src_pane_id is now always ``__pane_N``.
        src_pane = self._find_pane_in_layout_by_serial(layout, src_pane_id)
        src_vp = None
        if src_pane is not None:
            for schema_monitor in layout.monitors:
                for vp in schema_monitor.viewports:
                    if any(p is src_pane for p in vp.panes):
                        src_vp = vp
                        break
                if src_vp is not None:
                    break

        # Locate destination viewport
        dst_vp = None
        for schema_monitor in layout.monitors:
            for vp in schema_monitor.viewports:
                if vp.id == viewport_id:
                    dst_vp = vp
                    break
            if dst_vp is not None:
                break

        _dd_log.info(
            "_on_screen_map_drop_pane_on_viewport: src_found=%s dst_found=%s",
            src_pane is not None, dst_vp is not None,
        )
        if src_pane is None or src_vp is None or dst_vp is None:
            return

        # Round 2: tree-based MOVE. Remove src from its source viewport
        # (collapses any single-child Splits), then attach it as the dst
        # viewport's new last leaf.
        from cpsm.data.schema import (
            _flatten_split_tree_leaves,
            remove_pane_from_viewport,
            split_pane_in_viewport,
        )
        remove_pane_from_viewport(src_vp, src_pane)
        if dst_vp.split_tree is None:
            dst_vp.split_tree = src_pane
            dst_vp.panes = [src_pane]
        else:
            leaves = _flatten_split_tree_leaves(dst_vp.split_tree)
            anchor = leaves[-1] if leaves else None
            if anchor is None:
                dst_vp.split_tree = src_pane
                dst_vp.panes = [src_pane]
            else:
                split_pane_in_viewport(dst_vp, anchor, "right", src_pane)
        _dd_log.info(
            "_on_screen_map_drop_pane_on_viewport: MOVE conn=%s -> vp=%s",
            src_pane.connection_id, dst_vp.id,
        )
        self._save_document()
        self._screen_map_widget.set_layout(layout, self._query_live_monitors())
        self._refresh_screens_members_list()
        self._resync_tmux_to_layout()

    # ------------------------------------------------------------------
    # Fix #10 — Launch scene from sidebar
    # ------------------------------------------------------------------

    def _launch_scene_by_id(self, scene_id: str) -> None:
        """Launch the scene with *scene_id* via SessionService."""
        scene = next((s for s in self._document.scenes if s.id == scene_id), None)
        if scene is None:
            return
        try:
            if self._services is None:
                self.statusBar().showMessage("No services — cannot launch scene", 3000)
                return
            self._services.session.launch_scene(self._document, scene.id)
            self.statusBar().showMessage(f"Launched scene {scene_id}", 3000)
        except Exception as exc:
            QMessageBox.warning(self, "Launch scene failed", str(exc))

    def _show_about(self) -> None:
        """Open the About dialog."""
        from cpsm.ui.dialogs.about import AboutDialog

        dlg = AboutDialog(parent=self)
        dlg.exec()
