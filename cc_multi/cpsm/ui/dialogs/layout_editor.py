# -*- coding: utf-8 -*-
"""
cpsm.ui.dialogs.layout_editor — Layout Editor dialog.

Spec sections: §4.10

Wraps ScreenMapWidget in a QDialog that allows creating or editing a
ScreenLayout. Provides a header form (id, name, inherits_from) and embeds
ScreenMapWidget in single-group editing mode.  Validates via pydantic and
commits to ConfigService on Save.

Gap B: After _populate(), calls self._screen_map.set_layout() so the canvas
renders the layout (possibly blank) with live monitor data.

Gap C: Right-click context menus on the embedded Screen Map canvas to add
monitors, viewports, and panes without needing active sessions.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from pydantic import ValidationError
from PySide6.QtCore import Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from cpsm.data.schema import (
    CpsmDocument,
    GeometryPct,
    Pane,
    ScreenLayout,
    Viewport,
)
from cpsm.data.schema import (
    Monitor as SchemaMonitor,
)
from cpsm.ui.widgets.screen_map import ScreenMapWidget

__all__ = ["LayoutEditorDialog"]

_ID_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")

# ---------------------------------------------------------------------------
# Profile-glyph mapping (mirrors main_window._PROFILE_GLYPHS)
# ---------------------------------------------------------------------------

_PROFILE_GLYPHS: dict[str, str] = {
    "claude-remote": "🔗",
    "claude-local": "💻",
    "ssh-shell": "⌨",
    "local-shell": "$",
    "custom": "⚙",
}


def _new_slug() -> str:
    """Generate a new random slug-safe id."""
    return "vp-" + uuid.uuid4().hex[:8]


def _new_viewport_id() -> str:
    return "vp-" + uuid.uuid4().hex[:8]


def _new_viewport(x: float, y: float, w: float, h: float) -> Viewport:
    """Create a new Viewport with the given geometry percentages."""
    return Viewport(
        id=_new_viewport_id(),
        geometry_pct=GeometryPct(x=x, y=y, w=w, h=h),
        tmux_window_name=None,
        tmux_layout="tiled",
        panes=[],
    )


# ---------------------------------------------------------------------------
# Connection Picker dialog
# ---------------------------------------------------------------------------


class _ConnectionPickerDialog(QDialog):
    """Small dialog to pick a connection from doc.connections[].

    objectName: ``dlg_pick_connection``
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        document: CpsmDocument,
        title: str = "Pick Connection",
    ) -> None:
        super().__init__(parent)
        self.setObjectName("dlg_pick_connection")
        self.setAccessibleName("Pick Connection Dialog")
        self.setAccessibleDescription("Select a connection to assign to this pane")
        self.setWindowTitle(title)
        self.setMinimumWidth(360)

        self._document = document
        self._selected_connection_id: str | None = None

        layout = QVBoxLayout(self)

        self._list = QListWidget()
        self._list.setObjectName("list_pick_connection")
        self._list.setAccessibleName("Connection List")
        self._list.setAccessibleDescription("List of available connections to assign to this pane")
        layout.addWidget(self._list)

        for conn in document.connections:
            glyph = _PROFILE_GLYPHS.get(conn.launch_profile, "?")
            display = f"{glyph}  {conn.name or conn.id}  [{conn.id}]"
            item = QListWidgetItem(display)
            item.setData(256, conn.id)  # Qt.ItemDataRole.UserRole = 256
            self._list.addItem(item)

        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.setObjectName("btn_box_pick_connection")

        btn_ok = btn_box.button(QDialogButtonBox.StandardButton.Ok)
        if btn_ok is not None:
            btn_ok.setObjectName("btn_ok_pick_connection")
            btn_ok.setAccessibleName("OK button")

        btn_cancel = btn_box.button(QDialogButtonBox.StandardButton.Cancel)
        if btn_cancel is not None:
            btn_cancel.setObjectName("btn_cancel_pick_connection")
            btn_cancel.setAccessibleName("Cancel button")

        btn_box.accepted.connect(self._on_ok)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

        self._list.itemDoubleClicked.connect(lambda _: self._on_ok())

    def _on_ok(self) -> None:
        item = self._list.currentItem()
        if item is not None:
            self._selected_connection_id = item.data(256)
        self.accept()

    def selected_connection_id(self) -> str | None:
        """Return the selected connection id, or None if cancelled / none selected."""
        return self._selected_connection_id


# ---------------------------------------------------------------------------
# LayoutEditorDialog
# ---------------------------------------------------------------------------


class LayoutEditorDialog(QDialog):
    """Dialog for creating or editing a ScreenLayout.

    Parameters
    ----------
    parent:
        Optional Qt parent widget.
    document:
        The current CpsmDocument (used to populate ``inherits_from`` options
        and to commit on save).
    layout:
        The ScreenLayout to edit.  For a new layout pass a freshly-created
        ScreenLayout instance and set *is_new=True*.
    config_service:
        A ConfigService instance whose ``save(doc)`` method is called on
        successful save.  May be a test double.
    is_new:
        When True the ``id`` field is editable; when False it is locked.
    monitor_service:
        Optional monitor service passed straight through to ScreenMapWidget.
    connection_lookup:
        Optional callable passed straight through to ScreenMapWidget.
    """

    # -----------------------------------------------------------------------
    # Signals
    # -----------------------------------------------------------------------

    saved: Signal = Signal(object)
    """saved(layout: ScreenLayout) — emitted after a successful save."""

    # -----------------------------------------------------------------------
    # Construction
    # -----------------------------------------------------------------------

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        document: CpsmDocument | None = None,
        layout: ScreenLayout | None = None,
        config_service: Any = None,
        is_new: bool = True,
        monitor_service: Any = None,
        connection_lookup: Any = None,
    ) -> None:
        super().__init__(parent)

        self._document: CpsmDocument = document if document is not None else CpsmDocument()
        self._layout: ScreenLayout = (
            layout if layout is not None else ScreenLayout(id="new-layout", name="New Layout")
        )
        self._config_service = config_service
        self._is_new = is_new
        self._monitor_service = monitor_service
        self._connection_lookup = connection_lookup
        self._dirty = False

        self.setObjectName("dlg_layout_editor")
        self.setAccessibleName("Layout Editor Dialog")
        self.setAccessibleDescription("Dialog for creating or editing a screen layout")
        self.setWindowTitle("New Layout" if is_new else "Edit Layout")
        self.setMinimumWidth(800)
        self.setMinimumHeight(600)

        self._build_ui()
        self._populate()
        self._validate_inherits_from()

    # -----------------------------------------------------------------------
    # UI construction
    # -----------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(12, 12, 12, 12)

        # ---- Header form ----
        grp_header = QGroupBox("Layout Properties")
        grp_header.setObjectName("grp_layout_properties")
        grp_header.setAccessibleName("Layout properties group box")
        header_form = QFormLayout(grp_header)
        header_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        # ID field
        self._edit_id = QLineEdit()
        self._edit_id.setObjectName("edit_layout_id")
        self._edit_id.setAccessibleName("Layout ID field")
        self._edit_id.setAccessibleDescription(
            "Unique slug ID for this layout; locked after creation"
        )
        self._edit_id.setPlaceholderText("e.g. main-layout")
        if not self._is_new:
            self._edit_id.setReadOnly(True)
            self._edit_id.setToolTip("ID is locked after creation")

        self._err_id = QLabel()
        self._err_id.setObjectName("error_layout_id")
        self._err_id.setAccessibleName("Layout ID error label")
        self._err_id.setStyleSheet("color: red;")
        self._err_id.setVisible(False)

        header_form.addRow("ID *", self._edit_id)
        header_form.addRow("", self._err_id)

        # Name field
        self._edit_name = QLineEdit()
        self._edit_name.setObjectName("edit_layout_name")
        self._edit_name.setAccessibleName("Layout name field")
        self._edit_name.setAccessibleDescription("Human-readable name for this layout")
        self._edit_name.setPlaceholderText("e.g. Main Layout")

        self._err_name = QLabel()
        self._err_name.setObjectName("error_layout_name")
        self._err_name.setAccessibleName("Layout name error label")
        self._err_name.setStyleSheet("color: red;")
        self._err_name.setVisible(False)

        header_form.addRow("Name *", self._edit_name)
        header_form.addRow("", self._err_name)

        # Inherits-from combo
        self._combo_inherits = QComboBox()
        self._combo_inherits.setObjectName("combo_inherits_from")
        self._combo_inherits.setAccessibleName("Inherits from combo")
        self._combo_inherits.setAccessibleDescription(
            "Optional parent layout this layout inherits from"
        )
        self._combo_inherits.addItem("(none)", "")
        for sl in self._document.screen_layouts:
            if sl.id != self._layout.id:
                self._combo_inherits.addItem(sl.id, sl.id)

        self._err_inherits = QLabel()
        self._err_inherits.setObjectName("error_inherits_from")
        self._err_inherits.setAccessibleName("Inherits from error label")
        self._err_inherits.setStyleSheet("color: red;")
        self._err_inherits.setVisible(False)

        header_form.addRow("Inherits From:", self._combo_inherits)
        header_form.addRow("", self._err_inherits)

        root.addWidget(grp_header)

        # ---- Embedded ScreenMapWidget ----
        conn_lookup = (
            self._connection_lookup if self._connection_lookup is not None else (lambda _: None)
        )
        self._screen_map = ScreenMapWidget(
            monitor_service=self._monitor_service,
            connection_lookup=conn_lookup,
            parent=self,
        )
        self._screen_map.setObjectName("layout_editor_screen_map")
        self._screen_map.setAccessibleName("Layout editor screen map")
        self._screen_map.setAccessibleDescription(
            "Visual representation of the screen layout being edited"
        )
        root.addWidget(self._screen_map, stretch=1)

        # Wire right-click on the screen map view
        from PySide6.QtCore import Qt

        self._screen_map.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._screen_map.view.customContextMenuRequested.connect(self._on_screen_map_context_menu)

        # ---- Button box ----
        self._btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self._btn_box.setObjectName("btn_box_layout_editor")

        self._btn_save = self._btn_box.button(QDialogButtonBox.StandardButton.Save)
        assert self._btn_save is not None
        self._btn_save.setObjectName("btn_save_layout")
        self._btn_save.setAccessibleName("Save layout button")
        self._btn_save.setAccessibleDescription("Validate and save this layout")

        btn_cancel = self._btn_box.button(QDialogButtonBox.StandardButton.Cancel)
        assert btn_cancel is not None
        btn_cancel.setObjectName("btn_cancel_layout")
        btn_cancel.setAccessibleName("Cancel layout button")
        btn_cancel.setAccessibleDescription("Discard changes and close the dialog")

        self._btn_box.accepted.connect(self._on_save)
        self._btn_box.rejected.connect(self.reject)
        root.addWidget(self._btn_box)

        # Wire change signals
        self._edit_id.textChanged.connect(self._on_id_changed)
        self._edit_name.textChanged.connect(self._on_name_changed)
        self._combo_inherits.currentIndexChanged.connect(self._on_inherits_changed)

    # -----------------------------------------------------------------------
    # Population
    # -----------------------------------------------------------------------

    def _populate(self) -> None:
        """Fill widgets from the current _layout."""
        self._edit_id.setText(self._layout.id)
        self._edit_name.setText(self._layout.name)

        inherits = self._layout.inherits_from or ""
        idx = self._combo_inherits.findData(inherits)
        if idx >= 0:
            self._combo_inherits.setCurrentIndex(idx)
        else:
            self._combo_inherits.setCurrentIndex(0)  # (none)

        # Gap B: show the layout in ScreenMapWidget with live monitors
        monitors = self._get_monitors()
        self._screen_map.set_layout(self._layout, monitors)

    # -----------------------------------------------------------------------
    # Monitor helpers
    # -----------------------------------------------------------------------

    def _get_monitors(self) -> list[Any]:
        """Return monitor snapshots from the monitor service (or empty list)."""
        if self._monitor_service is None:
            return []
        try:
            return list(self._monitor_service.snapshot())
        except Exception:
            return []

    def _redraw_screen_map(self) -> None:
        """Re-render the screen map with current _layout and live monitors."""
        monitors = self._get_monitors()
        self._screen_map.set_layout(self._layout, monitors)

    # -----------------------------------------------------------------------
    # Gap C — geometry helpers for hit-testing the screen map canvas
    # -----------------------------------------------------------------------

    def _compute_scale_and_offset(self) -> tuple[float, float, float]:
        """Return (min_x, min_y, scale) matching the screen_map rendering code."""
        from cpsm.ui.widgets.screen_map import _MAX_CANVAS_DIM

        monitors = self._get_monitors()
        if monitors:
            min_x = float(min(m.geometry[0] for m in monitors))
            min_y = float(min(m.geometry[1] for m in monitors))
            total_w = float(max(m.geometry[0] + m.geometry[2] for m in monitors)) - min_x
            total_h = float(max(m.geometry[1] + m.geometry[3] for m in monitors)) - min_y
            scale = (
                min(_MAX_CANVAS_DIM / total_w, _MAX_CANVAS_DIM / total_h)
                if (total_w > 0 and total_h > 0)
                else 0.5
            )
        else:
            min_x = 0.0
            min_y = 0.0
            scale = 0.5
        return min_x, min_y, scale

    def _monitor_scene_rect(
        self, schema_monitor: SchemaMonitor
    ) -> tuple[float, float, float, float] | None:
        """Return the scene-coord bounding box of *schema_monitor*, or None."""
        monitors = self._get_monitors()
        if not monitors:
            return None
        ident_map = {m.identifier: m for m in monitors}
        index_map = {m.qt_index: m for m in monitors}

        live: Any = None
        if schema_monitor.identifier and schema_monitor.identifier in ident_map:
            live = ident_map[schema_monitor.identifier]
        elif schema_monitor.monitor_index_hint is not None:
            live = index_map.get(schema_monitor.monitor_index_hint)
        if live is None:
            return None

        min_x, min_y, scale = self._compute_scale_and_offset()
        gx, gy, gw, gh = live.geometry
        cx = (gx - min_x) * scale
        cy = (gy - min_y) * scale
        cw = gw * scale
        ch = gh * scale
        return cx, cy, cw, ch

    def _viewport_scene_rect(
        self, schema_monitor: SchemaMonitor, vp: Viewport
    ) -> tuple[float, float, float, float] | None:
        """Return scene-coord rect for *vp* on *schema_monitor*, or None."""
        mrect = self._monitor_scene_rect(schema_monitor)
        if mrect is None:
            return None
        cx, cy, cw, ch = mrect
        gp = vp.geometry_pct
        vx = cx + (gp.x / 100.0) * cw
        vy = cy + (gp.y / 100.0) * ch
        vw = (gp.w / 100.0) * cw
        vh = (gp.h / 100.0) * ch
        return vx, vy, vw, vh

    def _hit_test(
        self, scene_x: float, scene_y: float
    ) -> tuple[
        SchemaMonitor | None,
        Viewport | None,
        int,  # pane index or -1
    ]:
        """Return (monitor, viewport, pane_index) under scene point, or Nones."""
        for mon in self._layout.monitors:
            mrect = self._monitor_scene_rect(mon)
            if mrect is None:
                continue
            cx, cy, cw, ch = mrect
            if not (cx <= scene_x <= cx + cw and cy <= scene_y <= cy + ch):
                continue
            # Inside this monitor — check viewports
            for vp in mon.viewports:
                vrect = self._viewport_scene_rect(mon, vp)
                if vrect is None:
                    continue
                vx, vy, vw, vh = vrect
                if not (vx <= scene_x <= vx + vw and vy <= scene_y <= vy + vh):
                    continue
                # Inside viewport — find pane
                from cpsm.ui.widgets.screen_map import _compute_pane_rects

                n = len(vp.panes)
                if n > 0:
                    pane_coords = _compute_pane_rects(vp.tmux_layout, n, vx, vy, vw, vh)
                    for pi, (px, py, pw, ph) in enumerate(pane_coords):
                        if px <= scene_x <= px + pw and py <= scene_y <= py + ph:
                            return mon, vp, pi
                return mon, vp, -1
            return mon, None, -1
        return None, None, -1

    # -----------------------------------------------------------------------
    # Gap C — context menu entry point
    # -----------------------------------------------------------------------

    def _on_screen_map_context_menu(self, view_pos: Any) -> None:
        """Build and show the right-click context menu on the screen map canvas."""
        from PySide6.QtCore import QPoint

        view = self._screen_map.view
        vp = view_pos if isinstance(view_pos, QPoint) else view_pos.toPoint()
        scene_pt = view.mapToScene(vp)
        sx, sy = scene_pt.x(), scene_pt.y()

        schema_mon, vp_obj, pane_idx = self._hit_test(sx, sy)

        if schema_mon is None:
            # Empty canvas — offer "Add monitor from system"
            menu = self._build_empty_canvas_menu()
        elif vp_obj is None:
            # On a monitor rect (no viewport)
            menu = self._build_monitor_menu(schema_mon)
        elif pane_idx < 0:
            # On a viewport rect (no pane)
            menu = self._build_viewport_menu(schema_mon, vp_obj)
        else:
            # On a pane
            menu = self._build_pane_menu(schema_mon, vp_obj, pane_idx)

        if menu is not None and not menu.isEmpty():
            global_pos = view.viewport().mapToGlobal(vp)
            self._exec_menu(menu, global_pos)

    def _exec_menu(self, menu: QMenu, pos: Any) -> None:
        """Show *menu* at *pos*.  Centralised hook so tests can patch it."""
        menu.exec(pos)

    # -----------------------------------------------------------------------
    # Gap C — menu builders
    # -----------------------------------------------------------------------

    def _build_empty_canvas_menu(self) -> QMenu:
        """Menu shown when right-clicking empty canvas (no monitor under cursor)."""
        monitors = self._get_monitors()
        # Identifiers already in the layout
        used_ids = {m.identifier for m in self._layout.monitors if m.identifier}

        menu = QMenu(self)
        menu.setObjectName("menu_layout_editor_canvas")
        menu.setAccessibleName("Layout Editor Canvas Menu")

        sub = QMenu("Add monitor from system", menu)
        sub.setObjectName("menu_layout_editor_add_monitor")
        sub.setAccessibleName("Add Monitor Submenu")

        available = [m for m in monitors if m.identifier not in used_ids]
        if available:
            for info in available:
                act = QAction(f"{info.name}  [{info.identifier}]", sub)
                act.setObjectName(f"action_add_monitor_{info.qt_index}")
                act.setWhatsThis(f"Add monitor {info.name}")
                act.triggered.connect(lambda _checked=False, i=info: self._add_monitor_from_info(i))
                sub.addAction(act)
        else:
            no_act = QAction("(all system monitors already added)", sub)
            no_act.setObjectName("action_no_monitors_available")
            no_act.setEnabled(False)
            sub.addAction(no_act)

        menu.addMenu(sub)
        return menu

    def _build_monitor_menu(self, schema_mon: SchemaMonitor) -> QMenu:
        """Menu shown when right-clicking a monitor rect."""
        menu = QMenu(self)
        menu.setObjectName("menu_layout_editor_monitor")
        menu.setAccessibleName("Layout Editor Monitor Menu")

        act_full = QAction("Add full-screen viewport", self)
        act_full.setObjectName("action_add_viewport_full")
        act_full.setWhatsThis("Add full-screen viewport")
        act_full.triggered.connect(
            lambda _c=False, m=schema_mon: self._add_viewport(m, 0, 0, 100, 100)
        )
        menu.addAction(act_full)

        act_left = QAction("Add half-screen viewport (left)", self)
        act_left.setObjectName("action_add_viewport_half_left")
        act_left.setWhatsThis("Add half-screen viewport left")
        act_left.triggered.connect(
            lambda _c=False, m=schema_mon: self._add_viewport(m, 0, 0, 50, 100)
        )
        menu.addAction(act_left)

        act_right = QAction("Add half-screen viewport (right)", self)
        act_right.setObjectName("action_add_viewport_half_right")
        act_right.setWhatsThis("Add half-screen viewport right")
        act_right.triggered.connect(
            lambda _c=False, m=schema_mon: self._add_viewport(m, 50, 0, 50, 100)
        )
        menu.addAction(act_right)

        menu.addSeparator()

        act_remove = QAction("Remove monitor", self)
        act_remove.setObjectName("action_remove_monitor")
        act_remove.setWhatsThis("Remove monitor")
        act_remove.triggered.connect(lambda _c=False, m=schema_mon: self._remove_monitor(m))
        menu.addAction(act_remove)

        return menu

    def _build_viewport_menu(self, schema_mon: SchemaMonitor, vp: Viewport) -> QMenu:
        """Menu shown when right-clicking a viewport rect."""
        menu = QMenu(self)
        menu.setObjectName("menu_layout_editor_viewport")
        menu.setAccessibleName("Layout Editor Viewport Menu")

        act_add_pane = QAction("Add pane (connection)…", self)
        act_add_pane.setObjectName("action_add_pane_connection")
        act_add_pane.setWhatsThis("Add pane with connection")
        act_add_pane.triggered.connect(
            lambda _c=False, m=schema_mon, v=vp: self._add_pane_with_picker(m, v)
        )
        menu.addAction(act_add_pane)

        act_add_empty = QAction("Add empty pane", self)
        act_add_empty.setObjectName("action_add_pane_empty")
        act_add_empty.setWhatsThis("Add empty pane")
        act_add_empty.triggered.connect(
            lambda _c=False, m=schema_mon, v=vp: self._add_empty_pane(m, v)
        )
        menu.addAction(act_add_empty)

        menu.addSeparator()

        act_remove_vp = QAction("Remove viewport", self)
        act_remove_vp.setObjectName("action_remove_viewport")
        act_remove_vp.setWhatsThis("Remove viewport")
        act_remove_vp.triggered.connect(
            lambda _c=False, m=schema_mon, v=vp: self._remove_viewport(m, v)
        )
        menu.addAction(act_remove_vp)

        return menu

    def _build_pane_menu(self, schema_mon: SchemaMonitor, vp: Viewport, pane_idx: int) -> QMenu:
        """Menu shown when right-clicking a pane."""
        pane = vp.panes[pane_idx]
        menu = QMenu(self)
        menu.setObjectName("menu_layout_editor_pane")
        menu.setAccessibleName("Layout Editor Pane Menu")

        if pane.connection_id is not None:
            act_change = QAction("Change connection…", self)
            act_change.setObjectName("action_change_pane_connection")
            act_change.setWhatsThis("Change pane connection")
            act_change.triggered.connect(
                lambda _c=False, m=schema_mon, v=vp, pi=pane_idx: self._change_pane_connection(
                    m, v, pi
                )
            )
            menu.addAction(act_change)

            act_clear = QAction("Clear pane (make empty)", self)
            act_clear.setObjectName("action_clear_pane")
            act_clear.setWhatsThis("Clear pane")
            act_clear.triggered.connect(
                lambda _c=False, m=schema_mon, v=vp, pi=pane_idx: self._clear_pane(m, v, pi)
            )
            menu.addAction(act_clear)
        else:
            act_set = QAction("Set connection…", self)
            act_set.setObjectName("action_set_pane_connection")
            act_set.setWhatsThis("Set pane connection")
            act_set.triggered.connect(
                lambda _c=False, m=schema_mon, v=vp, pi=pane_idx: self._change_pane_connection(
                    m, v, pi
                )
            )
            menu.addAction(act_set)

        menu.addSeparator()

        act_remove_pane = QAction("Remove pane", self)
        act_remove_pane.setObjectName("action_remove_pane")
        act_remove_pane.setWhatsThis("Remove pane")
        act_remove_pane.triggered.connect(
            lambda _c=False, m=schema_mon, v=vp, pi=pane_idx: self._remove_pane(m, v, pi)
        )
        menu.addAction(act_remove_pane)

        return menu

    # -----------------------------------------------------------------------
    # Gap C — mutation helpers
    # -----------------------------------------------------------------------

    def _add_monitor_from_info(self, info: Any) -> None:
        """Append a new Monitor to the layout from a MonitorInfo."""
        new_mon = SchemaMonitor(
            identifier=info.identifier,
            monitor_index_hint=info.qt_index,
            viewports=[],
        )
        new_monitors = [*self._layout.monitors, new_mon]
        self._layout = self._layout.model_copy(update={"monitors": new_monitors})
        self._dirty = True
        self._redraw_screen_map()

    def _add_viewport(
        self,
        schema_mon: SchemaMonitor,
        x: float,
        y: float,
        w: float,
        h: float,
    ) -> None:
        """Append a new Viewport to *schema_mon*."""
        new_vp = _new_viewport(x, y, w, h)
        new_vps = [*schema_mon.viewports, new_vp]
        updated_mon = schema_mon.model_copy(update={"viewports": new_vps})
        new_mons = [updated_mon if m is schema_mon else m for m in self._layout.monitors]
        self._layout = self._layout.model_copy(update={"monitors": new_mons})
        self._dirty = True
        self._redraw_screen_map()

    def _remove_monitor(self, schema_mon: SchemaMonitor) -> None:
        """Confirm then remove *schema_mon* from the layout."""
        reply = QMessageBox.question(
            self,
            "Remove Monitor",
            "Remove this monitor and all its viewports?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        new_mons = [m for m in self._layout.monitors if m is not schema_mon]
        self._layout = self._layout.model_copy(update={"monitors": new_mons})
        self._dirty = True
        self._redraw_screen_map()

    def _remove_viewport(self, schema_mon: SchemaMonitor, vp: Viewport) -> None:
        """Confirm then remove *vp* from *schema_mon*."""
        reply = QMessageBox.question(
            self,
            "Remove Viewport",
            "Remove this viewport and all its panes?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        new_vps = [v for v in schema_mon.viewports if v is not vp]
        updated_mon = schema_mon.model_copy(update={"viewports": new_vps})
        new_mons = [updated_mon if m is schema_mon else m for m in self._layout.monitors]
        self._layout = self._layout.model_copy(update={"monitors": new_mons})
        self._dirty = True
        self._redraw_screen_map()

    def _add_pane_with_picker(self, schema_mon: SchemaMonitor, vp: Viewport) -> None:
        """Open connection picker and append pane to *vp*."""
        conn_id = self._pick_connection()
        if conn_id is None:
            return  # cancelled
        self._append_pane(schema_mon, vp, conn_id)

    def _add_empty_pane(self, schema_mon: SchemaMonitor, vp: Viewport) -> None:
        """Append an empty pane (connection_id=None) to *vp*."""
        self._append_pane(schema_mon, vp, None)

    def _append_pane(
        self,
        schema_mon: SchemaMonitor,
        vp: Viewport,
        connection_id: str | None,
    ) -> None:
        """Append a Pane to *vp*; rebuild layout immutably."""
        new_pane = Pane(connection_id=connection_id)
        new_panes = [*vp.panes, new_pane]
        updated_vp = vp.model_copy(update={"panes": new_panes})
        new_vps = [updated_vp if v is vp else v for v in schema_mon.viewports]
        updated_mon = schema_mon.model_copy(update={"viewports": new_vps})
        new_mons = [updated_mon if m is schema_mon else m for m in self._layout.monitors]
        self._layout = self._layout.model_copy(update={"monitors": new_mons})
        self._dirty = True
        self._redraw_screen_map()

    def _change_pane_connection(
        self, schema_mon: SchemaMonitor, vp: Viewport, pane_idx: int
    ) -> None:
        """Open connection picker and update pane at *pane_idx*."""
        conn_id = self._pick_connection()
        if conn_id is None:
            return
        self._update_pane_connection(schema_mon, vp, pane_idx, conn_id)

    def _clear_pane(self, schema_mon: SchemaMonitor, vp: Viewport, pane_idx: int) -> None:
        """Set pane at *pane_idx* to connection_id=None."""
        self._update_pane_connection(schema_mon, vp, pane_idx, None)

    def _remove_pane(self, schema_mon: SchemaMonitor, vp: Viewport, pane_idx: int) -> None:
        """Remove pane at *pane_idx* from *vp*."""
        new_panes = [p for i, p in enumerate(vp.panes) if i != pane_idx]
        updated_vp = vp.model_copy(update={"panes": new_panes})
        new_vps = [updated_vp if v is vp else v for v in schema_mon.viewports]
        updated_mon = schema_mon.model_copy(update={"viewports": new_vps})
        new_mons = [updated_mon if m is schema_mon else m for m in self._layout.monitors]
        self._layout = self._layout.model_copy(update={"monitors": new_mons})
        self._dirty = True
        self._redraw_screen_map()

    def _update_pane_connection(
        self,
        schema_mon: SchemaMonitor,
        vp: Viewport,
        pane_idx: int,
        conn_id: str | None,
    ) -> None:
        """Update the connection_id of pane at *pane_idx*; rebuild immutably."""
        new_pane = Pane(connection_id=conn_id)
        new_panes = [new_pane if i == pane_idx else p for i, p in enumerate(vp.panes)]
        updated_vp = vp.model_copy(update={"panes": new_panes})
        new_vps = [updated_vp if v is vp else v for v in schema_mon.viewports]
        updated_mon = schema_mon.model_copy(update={"viewports": new_vps})
        new_mons = [updated_mon if m is schema_mon else m for m in self._layout.monitors]
        self._layout = self._layout.model_copy(update={"monitors": new_mons})
        self._dirty = True
        self._redraw_screen_map()

    def _pick_connection(self) -> str | None:
        """Open the connection picker dialog and return the selected id or None."""
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

    # -----------------------------------------------------------------------
    # Validation helpers
    # -----------------------------------------------------------------------

    def _validate_id(self) -> bool:
        """Validate the ID field. Returns True when valid."""
        gid = self._edit_id.text().strip()
        if not gid:
            self._set_field_error(self._edit_id, self._err_id, "ID is required")
            return False
        if not _ID_SLUG_RE.match(gid):
            self._set_field_error(
                self._edit_id,
                self._err_id,
                f"id '{gid}' must match ^[a-z0-9][a-z0-9-]{{1,62}}$",
            )
            return False
        self._set_field_error(self._edit_id, self._err_id, "")
        return True

    def _validate_name(self) -> bool:
        """Validate the Name field. Returns True when valid."""
        name = self._edit_name.text().strip()
        if not name:
            self._set_field_error(self._edit_name, self._err_name, "Name is required")
            return False
        self._set_field_error(self._edit_name, self._err_name, "")
        return True

    def _validate_inherits_from(self) -> bool:
        """Validate the inherits_from combo for cycles. Returns True when valid."""
        chosen = self._combo_inherits.currentData() or ""
        if not chosen:
            self._set_field_error(None, self._err_inherits, "")
            self._update_save_button()
            return True

        current_id = self._edit_id.text().strip()

        # Cannot inherit from self
        if chosen == current_id:
            msg = "A layout cannot inherit from itself"
            self._set_field_error(None, self._err_inherits, msg)
            self._update_save_button()
            return False

        # Check if chosen layout already (directly or transitively) inherits
        # from current_id → that would create a cycle.
        if self._would_create_cycle(current_id, chosen):
            msg = f"Inheriting from '{chosen}' would create a cycle"
            self._set_field_error(None, self._err_inherits, msg)
            self._update_save_button()
            return False

        self._set_field_error(None, self._err_inherits, "")
        self._update_save_button()
        return True

    def _would_create_cycle(self, new_id: str, proposed_parent: str) -> bool:
        """Return True if making *new_id* inherit from *proposed_parent* would cycle.

        Walks the inheritance chain starting from *proposed_parent* through
        the document's existing screen_layouts.  If *new_id* appears anywhere
        in that chain the result is True (cycle detected).
        """
        layout_map: dict[str, str | None] = {
            sl.id: sl.inherits_from for sl in self._document.screen_layouts
        }
        # Simulate the proposed assignment for the traversal
        layout_map[new_id] = proposed_parent

        visited: set[str] = set()
        current: str | None = proposed_parent
        while current is not None:
            if current == new_id:
                return True
            if current in visited:
                # Pre-existing cycle in the document — not our concern here
                return False
            visited.add(current)
            current = layout_map.get(current)
        return False

    def _set_field_error(
        self,
        widget: QWidget | None,
        err_label: QLabel,
        error: str,
    ) -> None:
        if error:
            if widget is not None:
                widget.setStyleSheet("border: 1px solid red;")
            err_label.setText(error)
            err_label.setVisible(True)
        else:
            if widget is not None:
                widget.setStyleSheet("")
            err_label.setText("")
            err_label.setVisible(False)

    def _has_errors(self) -> bool:
        return (
            bool(self._err_id.text())
            or bool(self._err_name.text())
            or bool(self._err_inherits.text())
        )

    def _update_save_button(self) -> None:
        id_ok = bool(self._edit_id.text().strip())
        name_ok = bool(self._edit_name.text().strip())
        can_save = id_ok and name_ok and not self._has_errors()
        self._btn_save.setEnabled(can_save)
        if not can_save and self._has_errors():
            self._btn_save.setToolTip("Fix validation errors before saving")
        else:
            self._btn_save.setToolTip("")

    # -----------------------------------------------------------------------
    # Slots
    # -----------------------------------------------------------------------

    def _on_id_changed(self, _text: str) -> None:
        self._validate_id()
        self._validate_inherits_from()

    def _on_name_changed(self, _text: str) -> None:
        self._validate_name()
        self._update_save_button()

    def _on_inherits_changed(self, _index: int) -> None:
        self._validate_inherits_from()

    def _on_save(self) -> None:
        """Validate, build ScreenLayout, commit to ConfigService, emit saved."""
        id_ok = self._validate_id()
        name_ok = self._validate_name()
        inh_ok = self._validate_inherits_from()

        if not (id_ok and name_ok and inh_ok):
            self._update_save_button()
            return

        # Build the updated layout dict
        inherits = self._combo_inherits.currentData() or None
        layout_data = {
            "id": self._edit_id.text().strip(),
            "name": self._edit_name.text().strip(),
            "inherits_from": inherits,
            "monitors": [m.model_dump() for m in self._layout.monitors],
        }

        try:
            new_layout = ScreenLayout.model_validate(layout_data)
        except ValidationError as exc:
            # Surface the first error in the ID error label as a fallback
            first = exc.errors()[0]
            msg = first.get("msg", str(exc))
            self._set_field_error(self._edit_id, self._err_id, msg)
            self._update_save_button()
            return

        # Commit to ConfigService if provided
        if self._config_service is not None:
            # Build an updated document replacing/adding this layout
            existing = [sl for sl in self._document.screen_layouts if sl.id != new_layout.id]
            updated_layouts = [*existing, new_layout]
            updated_doc = self._document.model_copy(update={"screen_layouts": updated_layouts})
            self._config_service.save(updated_doc)

        self.saved.emit(new_layout)
        self.accept()

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def get_layout(self) -> ScreenLayout:
        """Return the layout currently represented by the form (not validated)."""
        inherits = self._combo_inherits.currentData() or None
        return ScreenLayout(
            id=self._edit_id.text().strip() or self._layout.id,
            name=self._edit_name.text().strip() or self._layout.name,
            inherits_from=inherits,
            monitors=list(self._layout.monitors),
        )
