# -*- coding: utf-8 -*-
"""
cpsm.ui.widgets.screen_map_context_menu — Shared right-click menu helpers.

Both LayoutEditorDialog and the Screens-tab in MainWindow need identical
right-click behaviour on a ScreenMapWidget canvas.  This mixin/module
centralises that logic so the two surfaces share exactly the same code path.

Usage
-----
Mix ``ScreenMapContextMenuMixin`` into any ``QWidget``/``QDialog`` subclass
that embeds a ``ScreenMapWidget``.  The host must implement three abstract
helpers (``_get_layout``, ``_get_monitors``, ``_set_layout``) and optionally
``_pick_connection`` (falls back to a simple QInputDialog-based picker if not
defined).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QPoint
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QMessageBox

from cpsm.data.schema import GeometryPct, Pane, ScreenLayout, Viewport
from cpsm.data.schema import Monitor as SchemaMonitor

if TYPE_CHECKING:
    pass

__all__ = ["ScreenMapContextMenuMixin"]


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
# Geometry helpers (needed for hit-testing)
# ---------------------------------------------------------------------------


def _compute_scale_and_offset(
    monitors: list[Any],
    max_dim: float = 800.0,
) -> tuple[float, float, float]:
    """Return (min_x, min_y, scale) matching ScreenMapWidget rendering code."""
    if monitors:
        min_x = float(min(m.geometry[0] for m in monitors))
        min_y = float(min(m.geometry[1] for m in monitors))
        total_w = float(max(m.geometry[0] + m.geometry[2] for m in monitors)) - min_x
        total_h = float(max(m.geometry[1] + m.geometry[3] for m in monitors)) - min_y
        scale = (
            min(max_dim / total_w, max_dim / total_h)
            if (total_w > 0 and total_h > 0)
            else 0.5
        )
    else:
        min_x = 0.0
        min_y = 0.0
        scale = 0.5
    return min_x, min_y, scale


def _monitor_scene_rect(
    schema_monitor: SchemaMonitor,
    monitors: list[Any],
    layout: ScreenLayout,
) -> tuple[float, float, float, float] | None:
    """Return scene-coord bounding box of *schema_monitor*, or None."""
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
        # Fall back to positional index in layout.monitors
        try:
            idx = layout.monitors.index(schema_monitor)
            live = index_map.get(idx)
        except ValueError:
            pass
    if live is None:
        return None

    min_x, min_y, scale = _compute_scale_and_offset(monitors)
    gx, gy, gw, gh = live.geometry
    cx = (gx - min_x) * scale
    cy = (gy - min_y) * scale
    cw = gw * scale
    ch = gh * scale
    return cx, cy, cw, ch


def _viewport_scene_rect(
    schema_monitor: SchemaMonitor,
    vp: Viewport,
    monitors: list[Any],
    layout: ScreenLayout,
) -> tuple[float, float, float, float] | None:
    """Return scene-coord rect for *vp* on *schema_monitor*, or None."""
    mrect = _monitor_scene_rect(schema_monitor, monitors, layout)
    if mrect is None:
        return None
    cx, cy, cw, ch = mrect
    gp = vp.geometry_pct
    vx = cx + (gp.x / 100.0) * cw
    vy = cy + (gp.y / 100.0) * ch
    vw = (gp.w / 100.0) * cw
    vh = (gp.h / 100.0) * ch
    return vx, vy, vw, vh


def hit_test(
    scene_x: float,
    scene_y: float,
    layout: ScreenLayout,
    monitors: list[Any],
) -> tuple[SchemaMonitor | None, Viewport | None, int]:
    """Return (monitor, viewport, pane_index) under scene point, or Nones.

    pane_index == -1 means "on viewport but no pane at that spot".
    """
    from cpsm.ui.widgets.screen_map import _compute_pane_rects

    for mon in layout.monitors:
        mrect = _monitor_scene_rect(mon, monitors, layout)
        if mrect is None:
            continue
        cx, cy, cw, ch = mrect
        if not (cx <= scene_x <= cx + cw and cy <= scene_y <= cy + ch):
            continue
        # Inside this monitor — check viewports
        for vp in mon.viewports:
            vrect = _viewport_scene_rect(mon, vp, monitors, layout)
            if vrect is None:
                continue
            vx, vy, vw, vh = vrect
            if not (vx <= scene_x <= vx + vw and vy <= scene_y <= vy + vh):
                continue
            # Inside viewport — find pane
            n = len(vp.panes)
            if n > 0:
                pane_coords = _compute_pane_rects(vp.tmux_layout, n, vx, vy, vw, vh)
                for pi, (px, py, pw, ph) in enumerate(pane_coords):
                    if px <= scene_x <= px + pw and py <= scene_y <= py + ph:
                        return mon, vp, pi
            return mon, vp, -1
        return mon, None, -1
    return None, None, -1


# ---------------------------------------------------------------------------
# The mixin itself
# ---------------------------------------------------------------------------


class ScreenMapContextMenuMixin:
    """Mixin that wires right-click menus on a ScreenMapWidget canvas.

    Concrete classes must implement (or have the host assign):
      - ``_cmx_get_layout() -> ScreenLayout``
      - ``_cmx_get_monitors() -> list[Any]``
      - ``_cmx_set_layout(layout: ScreenLayout) -> None``
      - ``_cmx_pick_connection() -> str | None``
      - ``_cmx_exec_menu(menu, global_pos) -> None``
    """

    # Stub attributes so mypy / subclasses know these exist.
    # Host objects override these (either via inheritance or instance assignment).

    def _cmx_get_layout(self) -> ScreenLayout:  # pragma: no cover
        """Return the current canvas layout. Must be overridden by the host."""
        raise NotImplementedError

    def _cmx_get_monitors(self) -> list[Any]:  # pragma: no cover
        """Return live monitors. Must be overridden by the host."""
        raise NotImplementedError

    def _cmx_set_layout(self, layout: ScreenLayout) -> None:  # pragma: no cover
        """Apply *layout* to the canvas and persist. Must be overridden by the host."""
        raise NotImplementedError

    def _cmx_pick_connection(self) -> str | None:  # pragma: no cover
        """Open a connection picker. Must be overridden by the host."""
        raise NotImplementedError

    def _cmx_exec_menu(self, menu: Any, pos: Any) -> None:  # pragma: no cover
        """Execute the menu at *pos*. Must be overridden by the host."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Entry point — call this from customContextMenuRequested
    # ------------------------------------------------------------------

    def _cmx_on_context_menu(self, view_pos: Any, view: Any) -> None:
        """Build and show the right-click context menu on *view*.

        Parameters
        ----------
        view_pos:
            The position as emitted by ``customContextMenuRequested``
            (a ``QPoint`` in view coordinates).
        view:
            The QGraphicsView whose ``mapToScene`` we use.
        """
        vp_pt = view_pos if isinstance(view_pos, QPoint) else view_pos.toPoint()
        scene_pt = view.mapToScene(vp_pt)
        sx, sy = scene_pt.x(), scene_pt.y()

        layout: ScreenLayout = self._cmx_get_layout()
        monitors: list[Any] = self._cmx_get_monitors()

        schema_mon, vp_obj, pane_idx = hit_test(sx, sy, layout, monitors)

        if schema_mon is None:
            menu = self._cmx_build_empty_canvas_menu(layout, monitors)
        elif vp_obj is None:
            menu = self._cmx_build_monitor_menu(schema_mon)
        elif pane_idx < 0:
            menu = self._cmx_build_viewport_menu(schema_mon, vp_obj)
        else:
            menu = self._cmx_build_pane_menu(schema_mon, vp_obj, pane_idx)

        if menu is not None and not menu.isEmpty():
            global_pos = view.viewport().mapToGlobal(vp_pt)
            self._cmx_exec_menu(menu, global_pos)

    # ------------------------------------------------------------------
    # Menu builders (public so tests can call them directly)
    # ------------------------------------------------------------------

    def _cmx_build_empty_canvas_menu(
        self,
        layout: ScreenLayout,
        monitors: list[Any],
    ) -> QMenu:
        """Menu shown when right-clicking empty canvas."""
        used_ids = {m.identifier for m in layout.monitors if m.identifier}

        menu = QMenu()
        menu.setObjectName("menu_screens_canvas")
        menu.setAccessibleName("Screens Canvas Menu")

        sub = QMenu("Add monitor from system", menu)
        sub.setObjectName("menu_screens_add_monitor")
        sub.setAccessibleName("Add Monitor Submenu")

        available = [m for m in monitors if m.identifier not in used_ids]
        if available:
            for info in available:
                act = QAction(f"{info.name}  [{info.identifier}]", sub)
                act.setObjectName(f"action_screens_add_monitor_{info.qt_index}")
                act.setWhatsThis(f"Add monitor {info.name}")
                act.triggered.connect(
                    lambda _checked=False, i=info: self._cmx_add_monitor_from_info(i)
                )
                sub.addAction(act)
        else:
            no_act = QAction("(all system monitors already added)", sub)
            no_act.setObjectName("action_screens_no_monitors_available")
            no_act.setEnabled(False)
            sub.addAction(no_act)

        menu.addMenu(sub)
        return menu

    def _cmx_build_monitor_menu(self, schema_mon: SchemaMonitor) -> QMenu:
        """Menu shown when right-clicking a monitor rect."""
        menu = QMenu()
        menu.setObjectName("menu_screens_monitor")
        menu.setAccessibleName("Screens Monitor Menu")

        act_full = QAction("Add full-screen viewport", menu)
        act_full.setObjectName("action_screens_add_viewport_full")
        act_full.setWhatsThis("Add full-screen viewport")
        act_full.triggered.connect(
            lambda _c=False, m=schema_mon: self._cmx_add_viewport(m, 0, 0, 100, 100)
        )
        menu.addAction(act_full)

        act_left = QAction("Add half-screen viewport (left)", menu)
        act_left.setObjectName("action_screens_add_viewport_half_left")
        act_left.setWhatsThis("Add half-screen viewport left")
        act_left.triggered.connect(
            lambda _c=False, m=schema_mon: self._cmx_add_viewport(m, 0, 0, 50, 100)
        )
        menu.addAction(act_left)

        act_right = QAction("Add half-screen viewport (right)", menu)
        act_right.setObjectName("action_screens_add_viewport_half_right")
        act_right.setWhatsThis("Add half-screen viewport right")
        act_right.triggered.connect(
            lambda _c=False, m=schema_mon: self._cmx_add_viewport(m, 50, 0, 50, 100)
        )
        menu.addAction(act_right)

        menu.addSeparator()

        act_remove = QAction("Remove monitor", menu)
        act_remove.setObjectName("action_screens_remove_monitor")
        act_remove.setWhatsThis("Remove monitor")
        act_remove.triggered.connect(lambda _c=False, m=schema_mon: self._cmx_remove_monitor(m))
        menu.addAction(act_remove)

        return menu

    def _cmx_build_viewport_menu(self, schema_mon: SchemaMonitor, vp: Viewport) -> QMenu:
        """Menu shown when right-clicking a viewport rect."""
        menu = QMenu()
        menu.setObjectName("menu_screens_viewport")
        menu.setAccessibleName("Screens Viewport Menu")

        act_add_pane = QAction("Add pane (connection)…", menu)
        act_add_pane.setObjectName("action_screens_add_pane_connection")
        act_add_pane.setWhatsThis("Add pane with connection")
        act_add_pane.triggered.connect(
            lambda _c=False, m=schema_mon, v=vp: self._cmx_add_pane_with_picker(m, v)
        )
        menu.addAction(act_add_pane)

        act_add_empty = QAction("Add empty pane", menu)
        act_add_empty.setObjectName("action_screens_add_pane_empty")
        act_add_empty.setWhatsThis("Add empty pane")
        act_add_empty.triggered.connect(
            lambda _c=False, m=schema_mon, v=vp: self._cmx_append_pane(m, v, None)
        )
        menu.addAction(act_add_empty)

        menu.addSeparator()

        act_remove_vp = QAction("Remove viewport", menu)
        act_remove_vp.setObjectName("action_screens_remove_viewport")
        act_remove_vp.setWhatsThis("Remove viewport")
        act_remove_vp.triggered.connect(
            lambda _c=False, m=schema_mon, v=vp: self._cmx_remove_viewport(m, v)
        )
        menu.addAction(act_remove_vp)

        return menu

    def _cmx_build_pane_menu(
        self, schema_mon: SchemaMonitor, vp: Viewport, pane_idx: int
    ) -> QMenu:
        """Menu shown when right-clicking a pane."""
        pane = vp.panes[pane_idx]
        menu = QMenu()
        menu.setObjectName("menu_screens_pane")
        menu.setAccessibleName("Screens Pane Menu")

        if pane.connection_id is not None:
            act_change = QAction("Change connection…", menu)
            act_change.setObjectName("action_screens_change_pane_connection")
            act_change.setWhatsThis("Change pane connection")
            act_change.triggered.connect(
                lambda _c=False, m=schema_mon, v=vp, pi=pane_idx: self._cmx_change_pane_connection(
                    m, v, pi
                )
            )
            menu.addAction(act_change)

            act_clear = QAction("Clear pane (make empty)", menu)
            act_clear.setObjectName("action_screens_clear_pane")
            act_clear.setWhatsThis("Clear pane")
            act_clear.triggered.connect(
                lambda _c=False, m=schema_mon, v=vp, pi=pane_idx: self._cmx_update_pane_connection(
                    m, v, pi, None
                )
            )
            menu.addAction(act_clear)
        else:
            act_set = QAction("Set connection…", menu)
            act_set.setObjectName("action_screens_set_pane_connection")
            act_set.setWhatsThis("Set pane connection")
            act_set.triggered.connect(
                lambda _c=False, m=schema_mon, v=vp, pi=pane_idx: self._cmx_change_pane_connection(
                    m, v, pi
                )
            )
            menu.addAction(act_set)

        menu.addSeparator()

        act_remove_pane = QAction("Remove pane", menu)
        act_remove_pane.setObjectName("action_screens_remove_pane")
        act_remove_pane.setWhatsThis("Remove pane")
        act_remove_pane.triggered.connect(
            lambda _c=False, m=schema_mon, v=vp, pi=pane_idx: self._cmx_remove_pane(m, v, pi)
        )
        menu.addAction(act_remove_pane)

        return menu

    # ------------------------------------------------------------------
    # Mutation helpers
    # ------------------------------------------------------------------

    def _cmx_add_monitor_from_info(self, info: Any) -> None:
        """Append a new Monitor to the layout from a MonitorInfo."""
        layout: ScreenLayout = self._cmx_get_layout()
        new_mon = SchemaMonitor(
            identifier=info.identifier,
            monitor_index_hint=info.qt_index,
            viewports=[],
        )
        new_monitors = [*layout.monitors, new_mon]
        new_layout = layout.model_copy(update={"monitors": new_monitors})
        self._cmx_set_layout(new_layout)

    def _cmx_add_viewport(
        self,
        schema_mon: SchemaMonitor,
        x: float,
        y: float,
        w: float,
        h: float,
    ) -> None:
        """Append a new Viewport to *schema_mon*."""
        layout: ScreenLayout = self._cmx_get_layout()
        new_vp = _new_viewport(x, y, w, h)
        new_vps = [*schema_mon.viewports, new_vp]
        updated_mon = schema_mon.model_copy(update={"viewports": new_vps})
        new_mons = [updated_mon if m is schema_mon else m for m in layout.monitors]
        new_layout = layout.model_copy(update={"monitors": new_mons})
        self._cmx_set_layout(new_layout)

    def _cmx_remove_monitor(self, schema_mon: SchemaMonitor) -> None:
        """Confirm then remove *schema_mon* from the layout."""
        reply = QMessageBox.question(
            None,
            "Remove Monitor",
            "Remove this monitor and all its viewports?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        layout: ScreenLayout = self._cmx_get_layout()
        new_mons = [m for m in layout.monitors if m is not schema_mon]
        new_layout = layout.model_copy(update={"monitors": new_mons})
        self._cmx_set_layout(new_layout)

    def _cmx_remove_viewport(self, schema_mon: SchemaMonitor, vp: Viewport) -> None:
        """Confirm then remove *vp* from *schema_mon*."""
        reply = QMessageBox.question(
            None,
            "Remove Viewport",
            "Remove this viewport and all its panes?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        layout: ScreenLayout = self._cmx_get_layout()
        new_vps = [v for v in schema_mon.viewports if v is not vp]
        updated_mon = schema_mon.model_copy(update={"viewports": new_vps})
        new_mons = [updated_mon if m is schema_mon else m for m in layout.monitors]
        new_layout = layout.model_copy(update={"monitors": new_mons})
        self._cmx_set_layout(new_layout)

    def _cmx_add_pane_with_picker(self, schema_mon: SchemaMonitor, vp: Viewport) -> None:
        """Open connection picker and append pane to *vp*."""
        conn_id = self._cmx_pick_connection()
        if conn_id is None:
            return
        self._cmx_append_pane(schema_mon, vp, conn_id)

    def _cmx_append_pane(
        self,
        schema_mon: SchemaMonitor,
        vp: Viewport,
        connection_id: str | None,
    ) -> None:
        """Append a Pane to *vp* via the canonical tree-aware path so
        ``vp.split_tree`` and ``vp.panes`` stay in lock-step. Round C: the
        previous ``model_copy(update={'panes': ...})`` pattern silently left
        the old tree in place, drifting it from the new panes list and
        breaking later drag-drop lookups."""
        from cpsm.data.schema import (
            _flatten_split_tree_leaves,
            split_pane_in_viewport,
        )
        new_pane = Pane(connection_id=connection_id)
        if vp.split_tree is None and not vp.panes:
            vp.split_tree = new_pane
            vp.panes = [new_pane]
        elif vp.split_tree is None:
            # Legacy state — single existing pane, rebuild tree from panes
            from cpsm.data.schema import _resync_viewport_panes
            vp.panes.append(new_pane)
            # Trigger migration logic by clearing+rebuilding the tree
            if len(vp.panes) == 1:
                vp.split_tree = vp.panes[0]
            else:
                from cpsm.data.schema import Split
                direction = "h" if vp.tmux_layout in ("even-h", "main-h") else "v"
                vp.split_tree = Split(direction=direction, children=list(vp.panes))
            _resync_viewport_panes(vp)
        else:
            leaves = _flatten_split_tree_leaves(vp.split_tree)
            anchor = leaves[-1] if leaves else None
            if anchor is None:
                vp.split_tree = new_pane
                vp.panes = [new_pane]
            else:
                split_pane_in_viewport(vp, anchor, "right", new_pane)
        self._cmx_set_layout(self._cmx_get_layout())

    def _cmx_change_pane_connection(
        self, schema_mon: SchemaMonitor, vp: Viewport, pane_idx: int
    ) -> None:
        """Open connection picker and update pane at *pane_idx*."""
        conn_id = self._cmx_pick_connection()
        if conn_id is None:
            return
        self._cmx_update_pane_connection(schema_mon, vp, pane_idx, conn_id)

    def _cmx_remove_pane(self, schema_mon: SchemaMonitor, vp: Viewport, pane_idx: int) -> None:
        """Remove pane at *pane_idx* from *vp*. Round C: routes through
        ``remove_pane_from_viewport`` so the tree and panes stay synced."""
        if not (0 <= pane_idx < len(vp.panes)):
            return
        from cpsm.data.schema import remove_pane_from_viewport
        target = vp.panes[pane_idx]
        remove_pane_from_viewport(vp, target)
        self._cmx_set_layout(self._cmx_get_layout())

    def _cmx_update_pane_connection(
        self,
        schema_mon: SchemaMonitor,
        vp: Viewport,
        pane_idx: int,
        conn_id: str | None,
    ) -> None:
        """Update the connection_id of pane at *pane_idx*. Round C: in-place
        mutation; the existing Pane is shared between ``vp.panes`` and the
        leaf in ``vp.split_tree`` so a single attribute write updates
        both."""
        if not (0 <= pane_idx < len(vp.panes)):
            return
        vp.panes[pane_idx].connection_id = conn_id
        self._cmx_set_layout(self._cmx_get_layout())
