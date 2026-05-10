# -*- coding: utf-8 -*-
"""
cpsm.ui.widgets.screen_map — ScreenMapWidget for visualising screen layouts.

Spec sections: §6.1, §6.2, §6.3, §6.4, §6.5, §6.6, §6.7, §6.8

Renders a QGraphicsScene + QGraphicsView showing each monitor from a
ScreenLayout scaled to fit within a max-800-px canvas.  Viewports and panes
are drawn as nested rectangles with labels.

Phase 16 additions:
  - ``resolve_drop_zone()`` — 9-zone hit test with 20% edge threshold.
  - Drag-and-drop event handlers (dragEnterEvent, dragMoveEvent, dropEvent).
  - Live hover overlay showing the target drop zone during drag.
  - Right-click context menu on panes.
  - Qt signals for every user gesture; no tmux ops in the widget.

Phase 17 additions (§6.4, §6.5):
  - ``set_visible_groups(group_ids)`` — render multiple groups simultaneously.
  - ``set_edit_target(group_id)`` — only the edit target responds to drag/drop.
  - Per-group dirty tracking with ``group_dirty(group_id, is_dirty)`` signal.
  - Conflict detection with red diagonal hatch overlay.
  - ``conflict_count_changed(int)`` status-bar signal.
  - Auto-color palette (12 perceptually-distinct colors, stable by hash).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Any, Literal

from PySide6.QtCore import QByteArray, QMimeData, QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QContextMenuEvent,
    QDrag,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QFont,
    QMouseEvent,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from cpsm.data.schema import Connection, ScreenLayout, Viewport
from cpsm.data.schema import Monitor as SchemaMonitor
from cpsm.services.monitor_service import MonitorInfo

# Drag-drop diagnostic logger (same name as used in main_window)
_dd_log = logging.getLogger("cpsm.ui.dragdrop")

__all__ = [
    "GROUP_PALETTE",
    "ScreenMapWidget",
    "group_color_for_id",
    "resolve_drop_zone",
]

# ---------------------------------------------------------------------------
# MIME type constants
# ---------------------------------------------------------------------------

MIME_CONNECTION_ID = "application/x-cpsm-connection-id"
MIME_PANE_ID = "application/x-cpsm-pane-id"

# ---------------------------------------------------------------------------
# Profile-glyph mapping (mirrors session_list._PROFILE_GLYPHS)
# ---------------------------------------------------------------------------

_PROFILE_GLYPHS: dict[str, str] = {
    "claude-remote": "🔗",
    "claude-local": "💻",
    "ssh-shell": "⌨",
    "local-shell": "$",
    "custom": "⚙",
}

# ---------------------------------------------------------------------------
# Visual constants
# ---------------------------------------------------------------------------

_MAX_CANVAS_DIM = 800  # px — maximum canvas dimension for scale calculations
_MONITOR_PEN_COLOR = QColor("#000000")
_MONITOR_BRUSH_COLOR = QColor("#1a2b3c")
_VIEWPORT_PEN_COLOR = QColor("#7ec8e3")
_VIEWPORT_BRUSH_COLOR = QColor("#0d1b2a")
_PANE_PEN_COLOR = QColor("#b0c4de")
_PANE_BRUSH_COLOR = QColor("#2a2a2a")
# Status-driven pane border colors. The 4-color model:
#   green  — connected (SSH up, terminal attached)
#   amber  — dropped   (pane alive but SSH not running; reconnect-loop wait)
#   blue   — disconnected (terminal closed, pane gone, or clean exit)
#   red    — error     (pane died with non-zero exit code)
_PANE_PEN_CONNECTED = QColor("#22c55e")     # green
_PANE_PEN_DROPPED = QColor("#f59e0b")        # amber
_PANE_PEN_DISCONNECTED = QColor("#3b82f6")   # blue
_PANE_PEN_ERROR = QColor("#ef4444")          # red
_PANE_EMPTY_PEN_COLOR = QColor("#555577")
_PANE_EMPTY_BRUSH_COLOR = QColor("#111122")
_GHOST_BRUSH_COLOR = QColor(50, 60, 80, 140)
_GHOST_PEN_COLOR = QColor("#6666aa")
_LABEL_COLOR = QColor("#e0e8f0")
_EMPTY_LABEL_COLOR = QColor("#7788aa")
_GLYPH_COLOR = QColor("#aaccee")

# Drop-zone overlay colours
_ZONE_OVERLAY_COLOR = QColor(120, 200, 255, 80)  # translucent blue fill
_ZONE_OVERLAY_PEN = QColor(120, 200, 255, 200)  # brighter border

# Edge threshold: outer 20% of each dimension is an edge zone
_EDGE_THRESHOLD = 0.20

# ---------------------------------------------------------------------------
# Phase 17 — Multi-group overlay constants and palette
# ---------------------------------------------------------------------------

# 12 perceptually-distinct colors for group overlays
GROUP_PALETTE: list[str] = [
    "#e63946",  # vivid red
    "#2a9d8f",  # teal
    "#e9c46a",  # golden yellow
    "#457b9d",  # steel blue
    "#f4a261",  # sandy orange
    "#6a4c93",  # purple
    "#52b788",  # mint green
    "#e76f51",  # burnt orange
    "#48cae4",  # sky blue
    "#b5838d",  # mauve
    "#80b918",  # yellow-green
    "#c77dff",  # violet
]

# Conflict hatch overlay: red diagonal at 30% alpha
_CONFLICT_BRUSH_COLOR = QColor(220, 30, 30, 77)  # ~30% alpha
_CONFLICT_PEN_COLOR = QColor(220, 30, 30, 120)

# Non-edit group opacity factor
_DIM_OPACITY = 0.50


def group_color_for_id(group_id: str, override: str | None = None) -> QColor:
    """Return a stable QColor for *group_id*, respecting any override hex string.

    Uses a deterministic CRC32 hash rather than ``hash()`` so the same id
    always picks the same palette entry across runs (Python's ``hash()``
    is randomized per-process via ``PYTHONHASHSEED``, which made test
    color comparisons flaky).
    """
    if override:
        c = QColor(override)
        if c.isValid():
            return c
    import zlib
    idx = zlib.crc32(group_id.encode("utf-8")) % len(GROUP_PALETTE)
    return QColor(GROUP_PALETTE[idx])


# ---------------------------------------------------------------------------
# Drop-zone resolver (§6.6)
# ---------------------------------------------------------------------------

DropZone = Literal["top", "bottom", "left", "right", "center"]


def resolve_drop_zone(
    local_x: float,
    local_y: float,
    w: float,
    h: float,
) -> DropZone:
    """Return the drop zone for a position relative to a pane (0,0 to w,h).

    The pane is divided into a 3x3 grid using 20% edge thresholds:

        TL | T  | TR
        ---+----+---
        L  | C  | R
        ---+----+---
        BL | B  | BR

    Corner cells (TL, TR, BL, BR) resolve to the dominant edge based on
    the relative penetration depth into each edge:
      - deeper into the horizontal edge → top / bottom
      - deeper into the vertical edge  → left / right

    Parameters
    ----------
    local_x, local_y:
        Position relative to the pane top-left corner (may be any float).
    w, h:
        Width and height of the pane.

    Returns
    -------
    DropZone
        One of ``"top"``, ``"bottom"``, ``"left"``, ``"right"``, or
        ``"center"``.
    """
    if w <= 0 or h <= 0:
        return "center"

    # Normalise to [0, 1]
    nx = local_x / w
    ny = local_y / h

    t = _EDGE_THRESHOLD

    in_top = ny < t
    in_bottom = ny > (1.0 - t)
    in_left = nx < t
    in_right = nx > (1.0 - t)

    # Pure edge (non-corner)
    if in_top and not in_left and not in_right:
        return "top"
    if in_bottom and not in_left and not in_right:
        return "bottom"
    if in_left and not in_top and not in_bottom:
        return "left"
    if in_right and not in_top and not in_bottom:
        return "right"

    # Corner: resolve by dominant penetration depth
    if in_top and in_left:
        # Top penetration = t - ny; left penetration = t - nx
        return "top" if (t - ny) >= (t - nx) else "left"
    if in_top and in_right:
        # Top penetration = t - ny; right penetration = nx - (1-t)
        return "top" if (t - ny) >= (nx - (1.0 - t)) else "right"
    if in_bottom and in_left:
        # Bottom penetration = ny - (1-t); left penetration = t - nx
        return "bottom" if (ny - (1.0 - t)) >= (t - nx) else "left"
    if in_bottom and in_right:
        # Bottom penetration = ny - (1-t); right penetration = nx - (1-t)
        return "bottom" if (ny - (1.0 - t)) >= (nx - (1.0 - t)) else "right"

    return "center"


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------


def _tiled_rects(
    n: int, x: float, y: float, w: float, h: float
) -> list[tuple[float, float, float, float]]:
    """Return rects for *n* panes in a tiled (equal-area grid) arrangement."""
    if n <= 0:
        return []
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    cell_w = w / cols
    cell_h = h / rows
    rects: list[tuple[float, float, float, float]] = []
    for i in range(n):
        col = i % cols
        row = i // cols
        rects.append((x + col * cell_w, y + row * cell_h, cell_w, cell_h))
    return rects


def _even_horizontal_rects(
    n: int, x: float, y: float, w: float, h: float
) -> list[tuple[float, float, float, float]]:
    """Return rects for *n* panes in equal-width vertical strips."""
    if n <= 0:
        return []
    pane_w = w / n
    return [(x + i * pane_w, y, pane_w, h) for i in range(n)]


def _even_vertical_rects(
    n: int, x: float, y: float, w: float, h: float
) -> list[tuple[float, float, float, float]]:
    """Return rects for *n* panes in equal-height horizontal strips."""
    if n <= 0:
        return []
    pane_h = h / n
    return [(x, y + i * pane_h, w, pane_h) for i in range(n)]


def _main_horizontal_rects(
    n: int, x: float, y: float, w: float, h: float
) -> list[tuple[float, float, float, float]]:
    """Top half = main pane; bottom half divided equally among rest."""
    if n <= 0:
        return []
    if n == 1:
        return [(x, y, w, h)]
    half_h = h / 2.0
    rects: list[tuple[float, float, float, float]] = [(x, y, w, half_h)]
    rest = n - 1
    pane_w = w / rest
    for i in range(rest):
        rects.append((x + i * pane_w, y + half_h, pane_w, half_h))
    return rects


def _main_vertical_rects(
    n: int, x: float, y: float, w: float, h: float
) -> list[tuple[float, float, float, float]]:
    """Left half = main pane; right half divided equally among rest."""
    if n <= 0:
        return []
    if n == 1:
        return [(x, y, w, h)]
    half_w = w / 2.0
    rects: list[tuple[float, float, float, float]] = [(x, y, half_w, h)]
    rest = n - 1
    pane_h = h / rest
    for i in range(rest):
        rects.append((x + half_w, y + i * pane_h, half_w, pane_h))
    return rects


def _compute_pane_rects(
    layout: str,
    n: int,
    x: float,
    y: float,
    w: float,
    h: float,
) -> list[tuple[float, float, float, float]]:
    """Dispatch to the appropriate layout function."""
    if layout == "even-h":
        return _even_horizontal_rects(n, x, y, w, h)
    if layout == "even-v":
        return _even_vertical_rects(n, x, y, w, h)
    if layout == "main-h":
        return _main_horizontal_rects(n, x, y, w, h)
    if layout == "main-v":
        return _main_vertical_rects(n, x, y, w, h)
    # "tiled" and "custom" (fallback) both use tiled
    return _tiled_rects(n, x, y, w, h)


def _compute_tree_rects(
    node: Any,
    x: float,
    y: float,
    w: float,
    h: float,
) -> list[tuple[Any, tuple[float, float, float, float]]]:
    """Walk a Viewport.split_tree (Round C). Returns ``(Pane, (x, y, w, h))``
    pairs in left-to-right / top-to-bottom in-order traversal.

    *node* can be either a :class:`Pane` (leaf) or a :class:`Split`
    (internal node). Mixed-orientation layouts (e.g. quadrants =
    horizontal Split of two vertical Splits) work correctly because each
    Split divides only the rect it is given.
    """
    from cpsm.data.schema import Pane as _Pane
    from cpsm.data.schema import Split as _Split

    if isinstance(node, _Pane):
        return [(node, (x, y, w, h))]
    if not isinstance(node, _Split):
        return []
    n = len(node.children)
    if n == 0:
        return []
    sizes_pct = (
        [*node.ratios, 1.0 - sum(node.ratios)] if node.ratios else [1.0 / n] * n
    )
    out: list[tuple[Any, tuple[float, float, float, float]]] = []
    if node.direction == "h":
        cx = x
        for child, pct in zip(node.children, sizes_pct, strict=False):
            cw = w * pct
            out.extend(_compute_tree_rects(child, cx, y, cw, h))
            cx += cw
    else:  # "v"
        cy = y
        for child, pct in zip(node.children, sizes_pct, strict=False):
            ch = h * pct
            out.extend(_compute_tree_rects(child, x, cy, w, ch))
            cy += ch
    return out


# ---------------------------------------------------------------------------
# Scene-building helpers
# ---------------------------------------------------------------------------


def _make_pen(color: QColor, width: float = 1.0, dashed: bool = False) -> QPen:
    pen = QPen(color)
    pen.setWidthF(width)
    if dashed:
        pen.setStyle(Qt.PenStyle.DashLine)
    return pen


def _add_text(
    scene: QGraphicsScene,
    text: str,
    x: float,
    y: float,
    color: QColor,
    font_size: int = 7,
    *,
    center_in_rect: tuple[float, float, float, float] | None = None,
) -> QGraphicsTextItem:
    """Add a text item to *scene* and return it."""
    item = QGraphicsTextItem(text)
    font = QFont()
    font.setPointSize(font_size)
    item.setFont(font)
    item.setDefaultTextColor(color)
    if center_in_rect is not None:
        rx, ry, rw, rh = center_in_rect
        br = item.boundingRect()
        item.setPos(rx + (rw - br.width()) / 2, ry + (rh - br.height()) / 2)
    else:
        item.setPos(x, y)
    scene.addItem(item)
    return item


def _draw_pane(
    scene: QGraphicsScene,
    px: float,
    py: float,
    pw: float,
    ph: float,
    pane_idx: int,
    connection_id: str | None,
    connection_lookup: Callable[[str], Connection | None],
    parent_item: QGraphicsRectItem,
    status_lookup: Callable[[str], str] | None = None,
) -> QGraphicsRectItem | None:
    """Draw a single pane rect as a child of *parent_item*.

    Empty panes (connection_id is None) are NOT rendered — they're
    visually indistinguishable from "no pane here" so the user can drop
    onto the area and have a pane created automatically.
    """
    if connection_id is None:
        return None

    cid: str = connection_id

    # Status-driven border color. See _PANE_PEN_* constants for the model.
    status = status_lookup(cid) if status_lookup is not None else "disconnected"
    if status == "connected":
        pen_color = _PANE_PEN_CONNECTED
    elif status == "dropped":
        pen_color = _PANE_PEN_DROPPED
    elif status == "error":
        pen_color = _PANE_PEN_ERROR
    else:  # disconnected, untracked, unknown — blue
        pen_color = _PANE_PEN_DISCONNECTED
    pen = _make_pen(pen_color, 3.0)
    brush = QBrush(_PANE_BRUSH_COLOR)

    # Coordinates relative to parent (viewport rect)
    pane_rect = QGraphicsRectItem(px, py, pw, ph, parent_item)
    pane_rect.setPen(pen)
    pane_rect.setBrush(brush)
    pane_rect.setZValue(3.0)

    # Big centered session name — pick the largest font that fits the pane
    # (90 % of width and 50 % of height) using actual font metrics.
    conn = connection_lookup(cid)
    display_name = (conn.name if (conn is not None and conn.name) else cid)
    from PySide6.QtGui import QFontMetricsF
    max_w = pw * 0.90
    max_h = ph * 0.50
    font_size = 6
    probe = QFont()
    for size in range(28, 5, -1):
        probe.setPointSize(size)
        fm = QFontMetricsF(probe)
        if fm.horizontalAdvance(display_name) <= max_w and fm.height() <= max_h:
            font_size = size
            break
    label_item = _add_text(
        scene,
        display_name,
        0,
        0,
        _LABEL_COLOR,
        font_size=font_size,
        center_in_rect=(px, py, pw, ph),
    )
    label_item.setZValue(4.0)

    return pane_rect


def _draw_viewport(
    scene: QGraphicsScene,
    vp_x: float,
    vp_y: float,
    vp_w: float,
    vp_h: float,
    viewport: Viewport,
    connection_lookup: Callable[[str], Connection | None],
    parent_item: QGraphicsRectItem,
    status_lookup: Callable[[str], str] | None = None,
) -> QGraphicsRectItem:
    """Draw a viewport rect and its panes as children of *parent_item*."""
    vp_rect = QGraphicsRectItem(vp_x, vp_y, vp_w, vp_h, parent_item)
    vp_rect.setPen(_make_pen(_VIEWPORT_PEN_COLOR, 1.5))
    vp_rect.setBrush(QBrush(_VIEWPORT_BRUSH_COLOR))
    vp_rect.setZValue(2.0)

    # Viewport label (tmux_window_name) — added directly to scene to avoid GC
    window_name = viewport.tmux_window_name or viewport.id
    label_item = _add_text(scene, window_name, vp_x + 2, vp_y + 1, _LABEL_COLOR, font_size=6)
    label_item.setZValue(3.0)

    # Pane rects. Round C: prefer the structured split_tree (which can
    # represent mixed-orientation layouts like quadrants); fall back to
    # the legacy preset-based layout when no tree is set.
    tree = getattr(viewport, "split_tree", None)
    if tree is not None:
        pane_pairs = _compute_tree_rects(tree, vp_x, vp_y, vp_w, vp_h)
        for i, (pane, (px, py, pw, ph)) in enumerate(pane_pairs):
            _draw_pane(
                scene, px, py, pw, ph, i,
                pane.connection_id, connection_lookup, vp_rect, status_lookup,
            )
    else:
        n_panes = len(viewport.panes)
        if n_panes > 0:
            pane_rects_coords = _compute_pane_rects(
                viewport.tmux_layout,
                n_panes,
                vp_x,
                vp_y,
                vp_w,
                vp_h,
            )
            for i, (px, py, pw, ph) in enumerate(pane_rects_coords):
                pane = viewport.panes[i]
                _draw_pane(
                    scene, px, py, pw, ph, i,
                    pane.connection_id, connection_lookup, vp_rect, status_lookup,
                )

    return vp_rect


def _render_ghost_monitors(scene: QGraphicsScene, monitors: list[MonitorInfo]) -> None:
    """Render the live system monitors as ghost outlines on a blank scene.

    Used when the layout has no monitors[] yet — gives the user a visual
    surface to right-click for adding monitors to the layout, instead of
    showing a blank white canvas.
    """
    if not monitors:
        # No live monitors either — render a placeholder rectangle and a hint.
        rect = scene.addRect(0, 0, _MAX_CANVAS_DIM, _MAX_CANVAS_DIM // 2)
        rect.setPen(_make_pen(QColor("#cbd5e1"), 8.0, dashed=True))
        rect.setBrush(QColor("#f8fafc"))
        text = scene.addText("No monitors detected.\nRight-click to add a placeholder monitor.")
        text.setDefaultTextColor(QColor("#64748b"))
        # Center-ish
        tb = text.boundingRect()
        text.setPos((_MAX_CANVAS_DIM - tb.width()) / 2, _MAX_CANVAS_DIM / 4 - tb.height() / 2)
        return

    min_x = min(m.geometry[0] for m in monitors)
    min_y = min(m.geometry[1] for m in monitors)
    total_w = max(m.geometry[0] + m.geometry[2] for m in monitors) - min_x
    total_h = max(m.geometry[1] + m.geometry[3] for m in monitors) - min_y
    scale = (
        min(_MAX_CANVAS_DIM / total_w, _MAX_CANVAS_DIM / total_h) if total_w and total_h else 0.5
    )

    for m in monitors:
        gx, gy, gw, gh = m.geometry
        cx = (gx - min_x) * scale
        cy = (gy - min_y) * scale
        cw = gw * scale
        ch = gh * scale
        rect = scene.addRect(cx, cy, cw, ch)
        rect.setPen(_make_pen(QColor("#94a3b8"), 8.0, dashed=True))
        rect.setBrush(QColor("#f1f5f9"))
        # Label inside the rect
        label_text = f"{m.identifier or m.name or f'monitor-{m.qt_index}'}\n{gw}x{gh}"
        text = scene.addText(label_text)
        text.setDefaultTextColor(QColor("#475569"))
        tb = text.boundingRect()
        text.setPos(cx + (cw - tb.width()) / 2, cy + (ch - tb.height()) / 2)

    # Hint at the bottom
    hint = scene.addText("Right-click on a monitor to add a viewport.")
    hint.setDefaultTextColor(QColor("#64748b"))
    hb = hint.boundingRect()
    hint.setPos(
        (_MAX_CANVAS_DIM - hb.width()) / 2,
        total_h * scale + 12,
    )


def _build_scene(
    scene: QGraphicsScene,
    layout: ScreenLayout,
    monitors: list[MonitorInfo],
    connection_lookup: Callable[[str], Connection | None],
    status_lookup: Callable[[str], str] | None = None,
) -> None:
    """Populate *scene* from *layout* and *monitors*."""
    scene.clear()

    if not layout.monitors:
        # The layout has no monitors yet (e.g. a freshly-generated default
        # layout when MonitorService wasn't wired). If we have live system
        # monitors, render them as ghost outlines so the user can right-click
        # to add monitors to the layout — instead of showing a blank canvas.
        _render_ghost_monitors(scene, monitors)
        return

    # --- Compute bounding box of all monitor geometries ---
    # When monitors is empty (all disconnected) we still render ghost rects.
    if monitors:
        min_x = min(m.geometry[0] for m in monitors)
        min_y = min(m.geometry[1] for m in monitors)
        total_w = max(m.geometry[0] + m.geometry[2] for m in monitors) - min_x
        total_h = max(m.geometry[1] + m.geometry[3] for m in monitors) - min_y
        if total_w > 0 and total_h > 0:
            scale = min(_MAX_CANVAS_DIM / total_w, _MAX_CANVAS_DIM / total_h)
        else:
            scale = 0.5  # fallback when geometry is degenerate
    else:
        # No live monitors — use a default scale for ghost rendering
        min_x = 0
        min_y = 0
        scale = 0.5

    # Build identifier → MonitorInfo map for matching
    ident_map: dict[str, MonitorInfo] = {m.identifier: m for m in monitors}
    index_map: dict[int, MonitorInfo] = {m.qt_index: m for m in monitors}

    def _resolve_monitor_info(schema_monitor: SchemaMonitor) -> MonitorInfo | None:
        """Match a schema Monitor to a live MonitorInfo."""
        if schema_monitor.identifier and schema_monitor.identifier in ident_map:
            return ident_map[schema_monitor.identifier]
        if schema_monitor.monitor_index_hint is not None:
            return index_map.get(schema_monitor.monitor_index_hint)
        # Fall back: use index position in layout.monitors
        idx = layout.monitors.index(schema_monitor)
        return index_map.get(idx)

    for schema_monitor in layout.monitors:
        live_info = _resolve_monitor_info(schema_monitor)

        if live_info is not None:
            gx, gy, gw, gh = live_info.geometry
            cx = (gx - min_x) * scale
            cy = (gy - min_y) * scale
            cw = gw * scale
            ch = gh * scale
            # Inset the monitor rect by half the pen width so adjacent monitors
            # don't share their painted edges — gives each screen a visible
            # "fake bezel" with clear space between neighbors.
            _bezel = 6.0
            bx, by = cx + _bezel, cy + _bezel
            bw, bh = max(0.0, cw - 2 * _bezel), max(0.0, ch - 2 * _bezel)
            mon_rect = QGraphicsRectItem(bx, by, bw, bh)
            mon_rect.setPen(_make_pen(_MONITOR_PEN_COLOR, 8.0))
            mon_rect.setBrush(QBrush(_MONITOR_BRUSH_COLOR))
            mon_rect.setZValue(1.0)
            scene.addItem(mon_rect)

            # Monitor label — added directly to scene to avoid Python GC issues
            mon_label = _add_text(
                scene,
                live_info.name or live_info.identifier,
                cx + 3,
                cy + 2,
                _MONITOR_PEN_COLOR,
                font_size=7,
            )
            mon_label.setZValue(2.0)

            # Draw viewports — positioned inside the bezeled inner rect
            for vp in schema_monitor.viewports:
                gp = vp.geometry_pct
                vp_x = bx + (gp.x / 100.0) * bw
                vp_y = by + (gp.y / 100.0) * bh
                vp_w = (gp.w / 100.0) * bw
                vp_h = (gp.h / 100.0) * bh
                _draw_viewport(scene, vp_x, vp_y, vp_w, vp_h, vp, connection_lookup, mon_rect, status_lookup)

        else:
            # Ghost rect — monitor in layout but not connected
            # Use index-based fallback position; scale is always > 0 here
            idx = layout.monitors.index(schema_monitor)
            cx = idx * _MAX_CANVAS_DIM * 0.2 * scale
            cy = 0.0
            cw = _MAX_CANVAS_DIM * 0.8 * scale
            ch = _MAX_CANVAS_DIM * 0.6 * scale

            ghost_rect = QGraphicsRectItem(cx, cy, cw, ch)
            ghost_rect.setPen(_make_pen(_GHOST_PEN_COLOR, 8.0, dashed=True))
            ghost_rect.setBrush(QBrush(_GHOST_BRUSH_COLOR))
            ghost_rect.setZValue(1.0)
            scene.addItem(ghost_rect)

            n_viewports = len(schema_monitor.viewports)
            overlay_text = (
                f"Monitor disconnected — {n_viewports} viewport"
                f"{'s' if n_viewports != 1 else ''} remap pending"
            )
            # Add directly to scene to avoid Python GC issues with parent items
            overlay_item = _add_text(
                scene,
                overlay_text,
                0,
                0,
                _GHOST_PEN_COLOR,
                font_size=7,
                center_in_rect=(cx, cy, cw, ch),
            )
            overlay_item.setZValue(2.0)


# ---------------------------------------------------------------------------
# Pane-registry helpers (built during scene construction)
# ---------------------------------------------------------------------------


class _PaneRecord:
    """Metadata kept for each rendered pane rectangle."""

    __slots__ = (
        "connection_id",
        "is_empty",
        "pane_id",
        "scene_h",
        "scene_w",
        "scene_x",
        "scene_y",
        "viewport_id",
    )

    def __init__(
        self,
        pane_id: str,
        connection_id: str | None,
        viewport_id: str,
        scene_x: float,
        scene_y: float,
        scene_w: float,
        scene_h: float,
    ) -> None:
        self.pane_id = pane_id
        self.connection_id = connection_id
        self.viewport_id = viewport_id
        self.scene_x = scene_x
        self.scene_y = scene_y
        self.scene_w = scene_w
        self.scene_h = scene_h
        self.is_empty = connection_id is None

    def contains_scene_point(self, sx: float, sy: float) -> bool:
        return (
            self.scene_x <= sx <= self.scene_x + self.scene_w
            and self.scene_y <= sy <= self.scene_y + self.scene_h
        )

    def local_pos(self, sx: float, sy: float) -> tuple[float, float]:
        return sx - self.scene_x, sy - self.scene_y


def _build_scene_with_registry(
    scene: QGraphicsScene,
    layout: ScreenLayout,
    monitors: list[MonitorInfo],
    connection_lookup: Callable[[str], Connection | None],
    status_lookup: Callable[[str], str] | None = None,
) -> list[_PaneRecord]:
    """Build the scene AND return a pane registry for hit-testing.

    This wraps ``_build_scene`` and computes the same geometry to create
    ``_PaneRecord`` objects.  The geometry calculation must stay in sync
    with ``_build_scene``.
    """
    _build_scene(scene, layout, monitors, connection_lookup, status_lookup)

    records: list[_PaneRecord] = []

    if not layout.monitors:
        return records

    if monitors:
        min_x = min(m.geometry[0] for m in monitors)
        min_y = min(m.geometry[1] for m in monitors)
        total_w = max(m.geometry[0] + m.geometry[2] for m in monitors) - min_x
        total_h = max(m.geometry[1] + m.geometry[3] for m in monitors) - min_y
        scale = (
            min(_MAX_CANVAS_DIM / total_w, _MAX_CANVAS_DIM / total_h)
            if (total_w > 0 and total_h > 0)
            else 0.5
        )
    else:
        min_x = 0
        min_y = 0
        scale = 0.5

    ident_map: dict[str, MonitorInfo] = {m.identifier: m for m in monitors}
    index_map: dict[int, MonitorInfo] = {m.qt_index: m for m in monitors}

    def _resolve(schema_monitor: SchemaMonitor) -> MonitorInfo | None:
        if schema_monitor.identifier and schema_monitor.identifier in ident_map:
            return ident_map[schema_monitor.identifier]
        if schema_monitor.monitor_index_hint is not None:
            return index_map.get(schema_monitor.monitor_index_hint)
        idx = layout.monitors.index(schema_monitor)
        return index_map.get(idx)

    pane_serial = 0
    for schema_monitor in layout.monitors:
        live_info = _resolve(schema_monitor)
        if live_info is None:
            continue

        gx, gy, gw, gh = live_info.geometry
        cx = (gx - min_x) * scale
        cy = (gy - min_y) * scale
        cw = gw * scale
        ch = gh * scale
        # Match the bezel inset used by _build_scene so hit-tests line up
        # with the rendered viewports/panes.
        _bezel = 6.0
        bx, by = cx + _bezel, cy + _bezel
        bw, bh = max(0.0, cw - 2 * _bezel), max(0.0, ch - 2 * _bezel)

        for vp in schema_monitor.viewports:
            gp = vp.geometry_pct
            vp_x = bx + (gp.x / 100.0) * bw
            vp_y = by + (gp.y / 100.0) * bh
            vp_w = (gp.w / 100.0) * bw
            vp_h = (gp.h / 100.0) * bh

            tree = getattr(vp, "split_tree", None)
            if tree is not None:
                pane_pairs = _compute_tree_rects(tree, vp_x, vp_y, vp_w, vp_h)
                for pane_obj, (px, py, pw, ph) in pane_pairs:
                    # Round C: always use a unique serial-based id so
                    # duplicate connection_ids in different viewports
                    # don't collide.
                    pane_id = f"__pane_{pane_serial}"
                    pane_serial += 1
                    records.append(
                        _PaneRecord(
                            pane_id=pane_id,
                            connection_id=pane_obj.connection_id,
                            viewport_id=vp.id,
                            scene_x=px,
                            scene_y=py,
                            scene_w=pw,
                            scene_h=ph,
                        )
                    )
                continue

            n = len(vp.panes)
            if n == 0:
                continue

            pane_coords = _compute_pane_rects(vp.tmux_layout, n, vp_x, vp_y, vp_w, vp_h)
            for i, (px, py, pw, ph) in enumerate(pane_coords):
                pane_obj = vp.panes[i]
                # Round C: always unique serial id; never connection_id
                # (which can collide when one connection appears in
                # multiple viewports of the same layout).
                pane_id = f"__pane_{pane_serial}"
                pane_serial += 1
                records.append(
                    _PaneRecord(
                        pane_id=pane_id,
                        connection_id=pane_obj.connection_id,
                        viewport_id=vp.id,
                        scene_x=px,
                        scene_y=py,
                        scene_w=pw,
                        scene_h=ph,
                    )
                )

    return records


# ---------------------------------------------------------------------------
# Phase 17 — Multi-group geometry helpers
# ---------------------------------------------------------------------------


def _compute_monitor_scale(monitors: list[MonitorInfo]) -> tuple[float, float, float]:
    """Return (min_x, min_y, scale) for a set of monitors."""
    min_x: float
    min_y: float
    scale: float
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


def _viewport_scene_rect(
    schema_monitor: SchemaMonitor,
    vp: Viewport,
    monitors: list[MonitorInfo],
    min_x: float,
    min_y: float,
    scale: float,
    schema_index: int | None = None,
) -> QRectF | None:
    """Return the scene-coordinate QRectF for *vp* on *schema_monitor*, or None.

    Resolution mirrors ``_build_scene._resolve``: identifier → monitor_index_hint
    → positional schema_index. The positional fallback is required for layouts
    produced by ``generate_default_layout`` which leaves both identifier and
    hint empty.
    """
    ident_map: dict[str, MonitorInfo] = {m.identifier: m for m in monitors}
    index_map: dict[int, MonitorInfo] = {m.qt_index: m for m in monitors}

    live_info: MonitorInfo | None = None
    if schema_monitor.identifier and schema_monitor.identifier in ident_map:
        live_info = ident_map[schema_monitor.identifier]
    elif schema_monitor.monitor_index_hint is not None:
        live_info = index_map.get(schema_monitor.monitor_index_hint)
    elif schema_index is not None:
        live_info = index_map.get(schema_index)

    if live_info is None:
        return None

    gx, gy, gw, gh = live_info.geometry
    cx = (gx - min_x) * scale
    cy = (gy - min_y) * scale
    cw = gw * scale
    ch = gh * scale
    # Match bezel inset used by the rendering paths
    _bezel = 6.0
    bx, by = cx + _bezel, cy + _bezel
    bw, bh = max(0.0, cw - 2 * _bezel), max(0.0, ch - 2 * _bezel)

    gp = vp.geometry_pct
    vp_x = bx + (gp.x / 100.0) * bw
    vp_y = by + (gp.y / 100.0) * bh
    vp_w = (gp.w / 100.0) * bw
    vp_h = (gp.h / 100.0) * bh

    return QRectF(vp_x, vp_y, vp_w, vp_h)


def _collect_viewport_rects(
    layout: ScreenLayout,
    monitors: list[MonitorInfo],
    min_x: float,
    min_y: float,
    scale: float,
) -> list[tuple[str, QRectF]]:
    """Return list of (viewport_id, scene_rect) for all viewports in *layout*."""
    result: list[tuple[str, QRectF]] = []
    for schema_idx, schema_monitor in enumerate(layout.monitors):
        for vp in schema_monitor.viewports:
            r = _viewport_scene_rect(
                schema_monitor, vp, monitors, min_x, min_y, scale, schema_idx
            )
            if r is not None:
                result.append((vp.id, r))
    return result


def _detect_conflicts(
    group_rects: dict[str, list[tuple[str, QRectF]]],
) -> list[QRectF]:
    """Return list of conflict QRectFs — pairwise overlaps across groups.

    Only considers different groups, not within a single group (same-group
    overlap is already validated by schema).
    """
    # Flatten to (group_id, vp_id, rect) list
    all_items: list[tuple[str, str, QRectF]] = []
    for gid, rects in group_rects.items():
        for vp_id, r in rects:
            all_items.append((gid, vp_id, r))

    conflicts: list[QRectF] = []
    n = len(all_items)
    for i in range(n):
        gid_a, _vp_a, r_a = all_items[i]
        for j in range(i + 1, n):
            gid_b, _vp_b, r_b = all_items[j]
            if gid_a == gid_b:
                continue
            inter = r_a.intersected(r_b)
            if inter.isValid() and inter.width() > 0 and inter.height() > 0:
                conflicts.append(inter)
    return conflicts


# ---------------------------------------------------------------------------
# Main widget
# ---------------------------------------------------------------------------


class ScreenMapWidget(QWidget):
    """Widget that visualises a ScreenLayout as a scaled monitor map.

    Phase 16 additions: drag-and-drop handlers, zone hover overlays, right-
    click context menus, and Qt signals for all gestures.  Tmux operations
    are *not* performed here — they are delegated to LayoutController via
    signals.

    Phase 17 additions (§6.4, §6.5): multi-group overlay rendering, per-group
    dirty tracking, conflict detection, and auto-color palette.

    Parameters
    ----------
    monitor_service:
        A :class:`~cpsm.services.monitor_service.MonitorService` instance
        whose ``monitor_added`` / ``monitor_removed`` signals are connected
        to trigger a redraw on hot-plug events.
    connection_lookup:
        Callable that takes a *connection_id* string and returns the matching
        :class:`~cpsm.data.schema.Connection` (or ``None`` if not found).
    parent:
        Optional Qt parent widget.
    """

    # ------------------------------------------------------------------
    # Signals (Phase 16)
    # ------------------------------------------------------------------

    mode_changed: Signal = Signal(str)
    """Emitted with ``"live"`` or ``"preview"`` when the mode changes."""

    drop_connection_requested: Signal = Signal(str, str, str, int)
    """drop_connection_requested(connection_id, target_pane_id, zone, modifiers)

    Emitted when a connection is dropped from the sidebar onto a pane.
    *modifiers* is the raw Qt.KeyboardModifiers integer value at drop time.
    """

    drop_pane_requested: Signal = Signal(str, str, str, int)
    """drop_pane_requested(src_pane_id, dst_pane_id, zone, modifiers)

    Emitted when an existing pane is dragged onto another.
    *modifiers* is the raw Qt.KeyboardModifiers integer value at drop time.
    """

    remove_pane_requested: Signal = Signal(str)
    """remove_pane_requested(pane_id) — context-menu "Remove from Layout"."""

    kill_pane_requested: Signal = Signal(str)
    """kill_pane_requested(pane_id) — context-menu "Kill Pane"."""

    attach_requested: Signal = Signal(str)
    """attach_requested(pane_id) — context-menu "Attach"."""

    reconnect_requested: Signal = Signal(str)
    """reconnect_requested(pane_id) — context-menu "Reconnect"."""

    properties_requested: Signal = Signal(str)
    """properties_requested(pane_id) — context-menu "Properties"."""

    save_layout_requested: Signal = Signal()
    """save_layout_requested() — context-menu "Save Layout"."""

    # ------------------------------------------------------------------
    # Signals (Phase 17)
    # ------------------------------------------------------------------

    group_dirty: Signal = Signal(str, bool)
    """group_dirty(group_id, is_dirty) — emitted when a group's dirty state changes."""

    conflict_count_changed: Signal = Signal(int)
    """conflict_count_changed(count) — emitted when the number of overlay conflicts changes."""

    drop_connection_on_viewport_requested: Signal = Signal(str, str, int)
    """drop_connection_on_viewport_requested(connection_id, viewport_id, modifiers)

    Emitted when a connection is dropped onto a viewport that has no panes.
    *modifiers* is the raw Qt.KeyboardModifiers integer value at drop time.
    """

    drop_pane_on_viewport_requested: Signal = Signal(str, str, int)
    """drop_pane_on_viewport_requested(src_pane_id, viewport_id, modifiers)

    Emitted when an existing pane is dragged onto a viewport (or empty
    monitor area) that has no destination pane. The handler should MOVE the
    src pane out of its source viewport and into the target viewport.
    """

    pane_clicked: Signal = Signal(str)
    """pane_clicked(connection_id)

    Emitted on every left-button mouse press over a pane. Allows the
    main window to mirror the click as a sidebar selection so the
    Inspector populates with the pane's connection details.
    """

    def __init__(
        self,
        monitor_service: Any = None,
        connection_lookup: Callable[[str], Connection | None] | None = None,
        parent: QWidget | None = None,
        status_lookup: Callable[[str], str] | None = None,
    ) -> None:
        super().__init__(parent)

        self.setObjectName("widget_screen_map")
        self.setAccessibleName("Screen Map Widget")
        self.setAccessibleDescription("Visual representation of the screen layout")

        self._connection_lookup: Callable[[str], Connection | None] = (
            connection_lookup if connection_lookup is not None else lambda _: None
        )
        self._status_lookup: Callable[[str], str] | None = status_lookup
        self._layout_data: ScreenLayout | None = None
        self._monitors: list[MonitorInfo] = []
        self._mode: Literal["live", "preview"] = "live"
        self._monitor_service = monitor_service

        # Pane registry — rebuilt on every _redraw()
        self._pane_registry: list[_PaneRecord] = []

        # Hover-overlay state
        self._hover_pane: _PaneRecord | None = None
        self._hover_zone: DropZone | None = None

        # Phase 17 — multi-group state
        # map of group_id → ScreenLayout (the buffered/pending layout for each group)
        self._group_layouts: dict[str, ScreenLayout] = {}
        # group_id → optional hex color override
        self._group_color_overrides: dict[str, str] = {}
        # currently visible group IDs (ordered)
        self._visible_group_ids: list[str] = []
        # group_id that receives drag/drop; None = all locked
        self._edit_target_id: str | None = None
        # per-group dirty flags: group_id → bool
        self._group_dirty_flags: dict[str, bool] = {}
        # cached conflict rects (scene coords) — rebuilt on _redraw_multi
        self._conflict_rects: list[QRectF] = []
        # last reported conflict count (for change detection)
        self._last_conflict_count: int = 0

        # --- Build UI ---
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(4, 4, 4, 4)

        # Mode label + Live/Preview toggle buttons used to live in a header
        # row above the canvas. Round B removed them from the visible UI;
        # the widgets are still constructed (kept as attributes so tests and
        # internal `set_mode` callers continue to work) but parented to a
        # hidden holder and never added to any layout.
        self._mode_chrome_holder = QWidget(self)
        self._mode_chrome_holder.setObjectName("widget_screen_map_mode_holder")
        self._mode_chrome_holder.hide()

        self._mode_label = QLabel("Mode: Live", self._mode_chrome_holder)
        self._mode_label.setObjectName("screenmap_mode_label")
        self._mode_label.setVisible(False)

        self._btn_live = QPushButton("Live", self._mode_chrome_holder)
        self._btn_live.setObjectName("screenmap_btn_live")
        self._btn_live.setCheckable(True)
        self._btn_live.setChecked(True)
        self._btn_live.setVisible(False)
        self._btn_live.clicked.connect(lambda: self.set_mode("live"))

        self._btn_preview = QPushButton("Preview", self._mode_chrome_holder)
        self._btn_preview.setObjectName("screenmap_btn_preview")
        self._btn_preview.setCheckable(True)
        self._btn_preview.setChecked(False)
        self._btn_preview.setVisible(False)
        self._btn_preview.clicked.connect(lambda: self.set_mode("preview"))

        # Graphics view — enable drag-drop
        self._scene = QGraphicsScene(self)
        self._view = _DragDropView(self._scene, self)
        self._view.setObjectName("screenmap_view")
        self._view.setAccessibleName("Screen Map View")
        self._view.setAccessibleDescription("Graphics view showing monitor layout")
        self._view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._view.setRenderHint(self._view.renderHints())  # keep default rendering
        self._view.setAcceptDrops(True)
        # The scene is always fitInView'd, so scrollbars are never useful and
        # can occasionally appear as stray pixel artifacts.
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # Wire the view's drag/drop events up to our handlers
        self._view.drag_enter_handler = self._on_drag_enter
        self._view.drag_move_handler = self._on_drag_move
        self._view.drop_handler = self._on_drop
        self._view.context_menu_handler = self._on_context_menu_view

        root_layout.addWidget(self._view)

        # Connect to monitor service if provided
        if monitor_service is not None:
            try:
                monitor_service.monitor_added.connect(self._on_monitor_added)
                monitor_service.monitor_removed.connect(self._on_monitor_removed)
            except AttributeError:
                pass  # service stub without signals in tests

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_layout(self, layout: ScreenLayout, monitors: list[MonitorInfo]) -> None:
        """Redraw the canvas for *layout* scaled to the provided *monitors*.

        Parameters
        ----------
        layout:
            The :class:`~cpsm.data.schema.ScreenLayout` to render.
        monitors:
            Current list of connected :class:`~cpsm.services.monitor_service.MonitorInfo`
            records.  Monitors in the layout that are absent here will render
            as ghost rects.
        """
        self._layout_data = layout
        self._monitors = list(monitors)
        self._redraw()

    def set_mode(self, mode: Literal["live", "preview"]) -> None:
        """Switch between *live* and *preview* modes.

        Parameters
        ----------
        mode:
            Either ``"live"`` or ``"preview"``.
        """
        if mode == self._mode:
            return
        self._mode = mode
        self._mode_label.setText(f"Mode: {mode.capitalize()}")
        self._btn_live.setChecked(mode == "live")
        self._btn_preview.setChecked(mode == "preview")
        self.mode_changed.emit(mode)

    @property
    def view(self) -> QGraphicsView:
        """The underlying :class:`QGraphicsView` (for testing)."""
        return self._view

    @property
    def scene(self) -> QGraphicsScene:
        """The underlying :class:`QGraphicsScene` (for testing)."""
        return self._scene

    @property
    def pane_registry(self) -> list[_PaneRecord]:
        """Pane registry — rebuilt after each ``set_layout`` call."""
        return list(self._pane_registry)

    # ------------------------------------------------------------------
    # Phase 17 — Multi-group public API
    # ------------------------------------------------------------------

    def set_visible_groups(
        self,
        group_ids: list[str],
        layouts: dict[str, ScreenLayout] | None = None,
        color_overrides: dict[str, str] | None = None,
    ) -> None:
        """Render multiple groups simultaneously.

        Parameters
        ----------
        group_ids:
            Ordered list of group IDs to display.  Groups not present are
            removed from the overlay.
        layouts:
            Optional map of group_id → ScreenLayout to use for each group.
            Groups already stored internally retain their current layout if
            not supplied here.
        color_overrides:
            Optional map of group_id → hex color string overriding the
            auto-palette color for that group.
        """
        self._visible_group_ids = list(group_ids)
        if layouts:
            self._group_layouts.update(layouts)
        if color_overrides:
            self._group_color_overrides.update(color_overrides)
        # Initialise dirty flags for newly added groups
        for gid in group_ids:
            if gid not in self._group_dirty_flags:
                self._group_dirty_flags[gid] = False
        # Switch to multi-group rendering
        self._redraw_multi()

    def set_edit_target(self, group_id: str | None) -> None:
        """Set which group's viewports respond to drag/drop.

        Parameters
        ----------
        group_id:
            The group whose pane registry will be used for hit-testing.
            Pass ``None`` to lock all groups (no drag/drop).
        """
        self._edit_target_id = group_id
        self._redraw_multi()

    def mark_group_dirty(self, group_id: str, is_dirty: bool = True) -> None:
        """Update per-group dirty flag and emit ``group_dirty`` signal."""
        prev = self._group_dirty_flags.get(group_id, False)
        self._group_dirty_flags[group_id] = is_dirty
        if prev != is_dirty:
            self.group_dirty.emit(group_id, is_dirty)

    def is_group_dirty(self, group_id: str) -> bool:
        """Return True if the given group has unsaved changes."""
        return self._group_dirty_flags.get(group_id, False)

    def update_group_layout(self, group_id: str, layout: ScreenLayout) -> None:
        """Store a pending layout for *group_id* and mark it dirty."""
        self._group_layouts[group_id] = layout
        self.mark_group_dirty(group_id, True)
        if group_id in self._visible_group_ids:
            self._redraw_multi()

    def set_group_color(self, group_id: str, hex_color: str) -> None:
        """Override the auto-palette color for *group_id*."""
        self._group_color_overrides[group_id] = hex_color
        if group_id in self._visible_group_ids:
            self._redraw_multi()

    def group_color(self, group_id: str) -> QColor:
        """Return the effective QColor for *group_id*."""
        override = self._group_color_overrides.get(group_id)
        return group_color_for_id(group_id, override)

    @property
    def conflict_rects(self) -> list[QRectF]:
        """Current list of scene-coordinate conflict rectangles (read-only)."""
        return list(self._conflict_rects)

    # ------------------------------------------------------------------
    # Drop-zone hit testing (public for tests)
    # ------------------------------------------------------------------

    def pane_at_scene_pos(self, sx: float, sy: float) -> _PaneRecord | None:
        """Return the _PaneRecord at scene coordinates (*sx*, *sy*), or None."""
        for rec in reversed(self._pane_registry):  # later = higher z
            if rec.contains_scene_point(sx, sy):
                return rec
        return None

    def viewport_at_scene_pos(self, sx: float, sy: float) -> str | None:
        """Return the viewport_id at scene coordinates (*sx*, *sy*), or None.

        Uses ``_collect_viewport_rects`` which must have a valid ``_layout_data``
        and ``_monitors`` set.  Returns ``None`` when no viewport rect contains
        the given point.
        """
        if self._layout_data is None:
            return None

        # Derive the shared scale parameters (same as _build_scene_with_registry)
        monitors = self._monitors
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

        for vp_id, rect in _collect_viewport_rects(
            self._layout_data, monitors, min_x, min_y, scale
        ):
            if rect.contains(sx, sy):
                return vp_id
        return None

    # ------------------------------------------------------------------
    # Drag-drop event handlers (called by _DragDropView)
    # ------------------------------------------------------------------

    def _on_drag_enter(self, event: QDragEnterEvent) -> None:
        # In multi-group mode, only accept drags when there is an edit target
        if self._visible_group_ids and self._edit_target_id is None:
            event.ignore()
            return
        mime = event.mimeData()
        formats = ", ".join(mime.formats())
        _dd_log.info("screen_map.drag_enter: mime_formats=%s", formats)
        if mime.hasFormat(MIME_CONNECTION_ID) or mime.hasFormat(MIME_PANE_ID):
            event.acceptProposedAction()
        else:
            event.ignore()

    def _on_drag_move(self, event: QDragMoveEvent) -> None:
        mime = event.mimeData()
        if not (mime.hasFormat(MIME_CONNECTION_ID) or mime.hasFormat(MIME_PANE_ID)):
            event.ignore()
            return

        event.acceptProposedAction()

        # Compute scene position from view position
        view_pos: QPoint = event.position().toPoint()
        scene_pos: QPointF = self._view.mapToScene(view_pos)
        sx, sy = scene_pos.x(), scene_pos.y()

        rec = self.pane_at_scene_pos(sx, sy)
        if rec is not None:
            lx, ly = rec.local_pos(sx, sy)
            zone: DropZone | None = resolve_drop_zone(lx, ly, rec.scene_w, rec.scene_h)
        else:
            zone = None

        if rec is not self._hover_pane or zone != self._hover_zone:
            _dd_log.info(
                "screen_map.drag_move: pane=%s zone=%s",
                rec.pane_id if rec is not None else None,
                zone,
            )
            self._hover_pane = rec
            self._hover_zone = zone
            self._view.viewport().update()

    def _on_drop(self, event: QDropEvent) -> None:
        mime = event.mimeData()
        view_pos: QPoint = event.position().toPoint()
        scene_pos: QPointF = self._view.mapToScene(view_pos)
        sx, sy = scene_pos.x(), scene_pos.y()

        formats = ", ".join(mime.formats())
        _dd_log.info(
            "screen_map.drop: mime_formats=%s scene_pos=(%.1f, %.1f)",
            formats, sx, sy,
        )

        rec = self.pane_at_scene_pos(sx, sy)
        modifiers = event.modifiers().value

        _dd_log.info(
            "screen_map.drop: hit_pane=%s", rec.pane_id if rec is not None else None
        )

        if rec is not None:
            lx, ly = rec.local_pos(sx, sy)
            zone = resolve_drop_zone(lx, ly, rec.scene_w, rec.scene_h)

            if mime.hasFormat(MIME_CONNECTION_ID):
                conn_id = mime.data(MIME_CONNECTION_ID).toStdString()
                _dd_log.info(
                    "screen_map.drop: emit drop_connection_requested conn=%s pane=%s zone=%s",
                    conn_id, rec.pane_id, zone,
                )
                self.drop_connection_requested.emit(conn_id, rec.pane_id, zone, modifiers)
                event.acceptProposedAction()
            elif mime.hasFormat(MIME_PANE_ID):
                src_pane_id = mime.data(MIME_PANE_ID).toStdString()
                if src_pane_id != rec.pane_id:
                    _dd_log.info(
                        "screen_map.drop: emit drop_pane_requested src=%s dst=%s zone=%s",
                        src_pane_id, rec.pane_id, zone,
                    )
                    self.drop_pane_requested.emit(src_pane_id, rec.pane_id, zone, modifiers)
                event.acceptProposedAction()
        else:
            # Drop on empty space — try viewport hit-test first, then fall back
            # to the legacy "" target for compatibility.
            if mime.hasFormat(MIME_PANE_ID):
                src_pane_id = mime.data(MIME_PANE_ID).toStdString()
                vp_id = self.viewport_at_scene_pos(sx, sy)
                if vp_id is not None:
                    _dd_log.info(
                        "screen_map.drop: emit drop_pane_on_viewport_requested src=%s vp=%s",
                        src_pane_id, vp_id,
                    )
                    self.drop_pane_on_viewport_requested.emit(src_pane_id, vp_id, modifiers)
                else:
                    _dd_log.info(
                        "screen_map.drop: emit drop_pane_requested src=%s dst='' zone=center",
                        src_pane_id,
                    )
                    self.drop_pane_requested.emit(src_pane_id, "", "center", modifiers)
                event.acceptProposedAction()
            elif mime.hasFormat(MIME_CONNECTION_ID):
                # No pane at this position — check if there is a viewport (empty viewport)
                vp_id = self.viewport_at_scene_pos(sx, sy)
                if vp_id is not None:
                    conn_id = mime.data(MIME_CONNECTION_ID).toStdString()
                    _dd_log.info(
                        "screen_map.drop: emit drop_connection_on_viewport_requested conn=%s vp=%s",
                        conn_id, vp_id,
                    )
                    self.drop_connection_on_viewport_requested.emit(conn_id, vp_id, modifiers)
                    event.acceptProposedAction()
                else:
                    _dd_log.info("screen_map.drop: no pane and no viewport hit — ignoring")
                    event.ignore()
            else:
                event.ignore()

        # Clear hover overlay
        self._hover_pane = None
        self._hover_zone = None
        self._view.viewport().update()

    def _on_context_menu_view(self, event: QContextMenuEvent) -> None:
        """Show context menu if right-click lands on a pane."""
        view_pos = event.pos()
        scene_pos = self._view.mapToScene(view_pos)
        sx, sy = scene_pos.x(), scene_pos.y()

        rec = self.pane_at_scene_pos(sx, sy)
        if rec is None:
            return

        menu = QMenu(self)
        menu.setObjectName("screenmap_context_menu")

        act_attach = QAction("Attach", self)
        act_attach.setObjectName("screenmap_ctx_attach")
        act_attach.triggered.connect(lambda: self.attach_requested.emit(rec.pane_id))
        menu.addAction(act_attach)

        act_reconnect = QAction("Reconnect", self)
        act_reconnect.setObjectName("screenmap_ctx_reconnect")
        act_reconnect.triggered.connect(lambda: self.reconnect_requested.emit(rec.pane_id))
        menu.addAction(act_reconnect)

        menu.addSeparator()

        act_remove = QAction("Remove from Layout", self)
        act_remove.setObjectName("screenmap_ctx_remove")
        act_remove.triggered.connect(lambda: self.remove_pane_requested.emit(rec.pane_id))
        menu.addAction(act_remove)

        act_kill = QAction("Kill Pane", self)
        act_kill.setObjectName("screenmap_ctx_kill")
        act_kill.triggered.connect(lambda: self.kill_pane_requested.emit(rec.pane_id))
        menu.addAction(act_kill)

        menu.addSeparator()

        act_props = QAction("Properties", self)
        act_props.setObjectName("screenmap_ctx_properties")
        act_props.triggered.connect(lambda: self.properties_requested.emit(rec.pane_id))
        menu.addAction(act_props)

        act_save = QAction("Save Layout", self)
        act_save.setObjectName("screenmap_ctx_save_layout")
        act_save.triggered.connect(lambda: self.save_layout_requested.emit())
        menu.addAction(act_save)

        menu.exec(event.globalPos())

    # ------------------------------------------------------------------
    # Hover overlay drawing
    # ------------------------------------------------------------------

    def _draw_hover_overlay(self, painter: QPainter) -> None:
        """Paint the drop-zone highlight overlay on the view's viewport."""
        rec = self._hover_pane
        zone = self._hover_zone
        if rec is None or zone is None:
            return

        # Convert scene rect → view rect
        scene_rect_f = self._view.mapFromScene(
            rec.scene_x, rec.scene_y, rec.scene_w, rec.scene_h
        ).boundingRect()

        x = scene_rect_f.x()
        y = scene_rect_f.y()
        w = scene_rect_f.width()
        h = scene_rect_f.height()
        t = _EDGE_THRESHOLD

        # Compute the overlay sub-rect for the zone
        ox: float = x
        oy: float = y
        ow: float = w
        oh: float = h
        if zone == "top":
            oh = h * t
        elif zone == "bottom":
            oh = h * t
            oy = y + h - oh
        elif zone == "left":
            ow = w * t
        elif zone == "right":
            ow = w * t
            ox = x + w - ow

        painter.setBrush(QBrush(_ZONE_OVERLAY_COLOR))
        painter.setPen(QPen(QColor(_ZONE_OVERLAY_PEN), 2.0))
        painter.drawRect(int(ox), int(oy), int(ow), int(oh))

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _redraw(self) -> None:
        """Clear and rebuild the scene from current layout + monitor data."""
        if self._layout_data is None:
            self._scene.clear()
            self._pane_registry = []
            return
        self._pane_registry = _build_scene_with_registry(
            self._scene,
            self._layout_data,
            self._monitors,
            self._connection_lookup,
            self._status_lookup,
        )
        self._view.fitInView(self._scene.itemsBoundingRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def _redraw_multi(self) -> None:
        """Rebuild the scene for multi-group overlay mode (Phase 17).

        - Renders each visible group's layout at its group color.
        - Non-edit groups are rendered at 50% opacity.
        - Overlapping viewports across groups get a red diagonal-hatch overlay.
        - Updates ``_pane_registry`` to only include the edit target group's
          panes (so drag/drop only targets those panes).
        - Emits ``conflict_count_changed`` when the conflict count changes.
        """
        if not self._visible_group_ids:
            # Fall back to single-layout mode
            self._redraw()
            return

        self._scene.clear()
        self._pane_registry = []

        # Compute shared scale from monitors
        min_x, min_y, scale = _compute_monitor_scale(self._monitors)

        # Collect viewport scene rects per group (for conflict detection)
        group_viewport_rects: dict[str, list[tuple[str, QRectF]]] = {}

        for gid in self._visible_group_ids:
            layout = self._group_layouts.get(gid)
            if layout is None:
                continue

            is_edit = self._edit_target_id is None or gid == self._edit_target_id
            color = self.group_color(gid)
            opacity = 1.0 if is_edit else _DIM_OPACITY

            # Tinted viewport background color (group color at low alpha)
            tint = QColor(color)
            tint.setAlphaF(0.25 * opacity)
            tint_brush = QBrush(tint)

            vp_pen_color = QColor(color)
            vp_pen_color.setAlphaF(0.85 * opacity)
            vp_pen = QPen(vp_pen_color, 1.5)

            # Accumulate rects for this group
            group_rects: list[tuple[str, QRectF]] = []

            for schema_monitor in layout.monitors:
                for vp in schema_monitor.viewports:
                    r = _viewport_scene_rect(
                        schema_monitor, vp, self._monitors, min_x, min_y, scale
                    )
                    if r is None:
                        continue
                    group_rects.append((vp.id, r))

                    # Draw viewport rect
                    vp_item = QGraphicsRectItem(r)
                    vp_item.setPen(vp_pen)
                    vp_item.setBrush(tint_brush)
                    vp_item.setZValue(2.0 + (0.1 if is_edit else 0.0))
                    self._scene.addItem(vp_item)

                    # Viewport label
                    label_color = QColor(color)
                    label_color.setAlphaF(opacity)
                    label_item = QGraphicsTextItem(vp.tmux_window_name or vp.id)
                    font = QFont()
                    font.setPointSize(6)
                    label_item.setFont(font)
                    label_item.setDefaultTextColor(label_color)
                    label_item.setPos(r.x() + 2, r.y() + 1)
                    label_item.setZValue(3.0)
                    self._scene.addItem(label_item)

            group_viewport_rects[gid] = group_rects

            # Build pane registry only for edit target (or all if no target set)
            if is_edit and self._edit_target_id == gid:
                registry = _build_scene_with_registry(
                    QGraphicsScene(),  # throw-away scene — we only want the registry
                    layout,
                    self._monitors,
                    self._connection_lookup,
                    self._status_lookup,
                )
                self._pane_registry = registry

        # Conflict detection — pairwise across groups
        conflicts = _detect_conflicts(group_viewport_rects)
        self._conflict_rects = conflicts

        # Draw conflict hatching
        hatch_brush = QBrush(_CONFLICT_BRUSH_COLOR, Qt.BrushStyle.BDiagPattern)
        hatch_pen = QPen(_CONFLICT_PEN_COLOR, 1.0)
        for rect in conflicts:
            hatch_item = QGraphicsRectItem(rect)
            hatch_item.setPen(hatch_pen)
            hatch_item.setBrush(hatch_brush)
            hatch_item.setZValue(10.0)
            self._scene.addItem(hatch_item)

        # Emit conflict count signal if changed
        new_count = len(conflicts)
        if new_count != self._last_conflict_count:
            self._last_conflict_count = new_count
            self.conflict_count_changed.emit(new_count)

        # Fit view
        br = self._scene.itemsBoundingRect()
        if not br.isEmpty():
            self._view.fitInView(br, Qt.AspectRatioMode.KeepAspectRatio)

    def _on_monitor_added(self, info: MonitorInfo) -> None:
        """Handle monitor hot-plug addition."""
        # Refresh monitors list and redraw
        if self._monitor_service is not None:
            try:
                self._monitors = self._monitor_service.snapshot()
            except AttributeError:
                if info not in self._monitors:
                    self._monitors.append(info)
        else:
            if info not in self._monitors:
                self._monitors.append(info)
        self._redraw()

    def _on_monitor_removed(self, identifier: str) -> None:
        """Handle monitor hot-plug removal."""
        if self._monitor_service is not None:
            try:
                self._monitors = self._monitor_service.snapshot()
            except AttributeError:
                self._monitors = [m for m in self._monitors if m.identifier != identifier]
        else:
            self._monitors = [m for m in self._monitors if m.identifier != identifier]
        self._redraw()


# ---------------------------------------------------------------------------
# _DragDropView — thin QGraphicsView subclass that routes events
# ---------------------------------------------------------------------------


class _DragDropView(QGraphicsView):
    """QGraphicsView subclass that delegates drag-drop and context-menu events
    to the owning ScreenMapWidget via handler callables.

    The handlers are set by ScreenMapWidget immediately after construction.
    This avoids multiple inheritance complications while keeping all Qt
    event handling in one place.
    """

    def __init__(self, scene: QGraphicsScene, parent: ScreenMapWidget) -> None:
        super().__init__(scene, parent)
        self._smw = parent
        # Handler callables — set by ScreenMapWidget after __init__
        self.drag_enter_handler: Callable[[QDragEnterEvent], None] | None = None
        self.drag_move_handler: Callable[[QDragMoveEvent], None] | None = None
        self.drop_handler: Callable[[QDropEvent], None] | None = None
        self.context_menu_handler: Callable[[QContextMenuEvent], None] | None = None
        # State for pane-drag initiation (mouse-press → drag-distance → QDrag)
        self._press_pos: QPoint | None = None
        self._press_pane_id: str | None = None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            scene_pt = self.mapToScene(event.pos())
            rec = self._smw.pane_at_scene_pos(scene_pt.x(), scene_pt.y())
            if rec is not None:
                self._press_pos = event.pos()
                self._press_pane_id = rec.pane_id
                # Fire pane_clicked so the main window can mirror this as a
                # sidebar selection (Inspector populates). Only emit when the
                # pane has a real connection_id (not a placeholder slot).
                if rec.connection_id:
                    self._smw.pane_clicked.emit(rec.connection_id)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if (
            self._press_pos is not None
            and self._press_pane_id
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            distance = (event.pos() - self._press_pos).manhattanLength()
            if distance >= QApplication.startDragDistance():
                _dd_log.info("screen_map.pane_drag_start: pane=%s", self._press_pane_id)
                drag = QDrag(self)
                mime = QMimeData()
                mime.setData(
                    MIME_PANE_ID, QByteArray(self._press_pane_id.encode("utf-8"))
                )
                drag.setMimeData(mime)
                self._press_pos = None
                self._press_pane_id = None
                drag.exec(Qt.DropAction.MoveAction)
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._press_pos = None
        self._press_pane_id = None
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event: Any) -> None:
        # Re-fit the scene whenever the view is resized; otherwise the initial
        # fitInView (which runs before final layout) leaves the scene rendered
        # at a tiny size relative to the eventual viewport.
        super().resizeEvent(event)
        br = self.scene().itemsBoundingRect() if self.scene() is not None else QRectF()
        if not br.isNull() and not br.isEmpty():
            self.fitInView(br, Qt.AspectRatioMode.KeepAspectRatio)

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        br = self.scene().itemsBoundingRect() if self.scene() is not None else QRectF()
        if not br.isNull() and not br.isEmpty():
            self.fitInView(br, Qt.AspectRatioMode.KeepAspectRatio)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self.drag_enter_handler is not None:
            self.drag_enter_handler(event)
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if self.drag_move_handler is not None:
            self.drag_move_handler(event)
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        if self.drop_handler is not None:
            self.drop_handler(event)
        else:
            super().dropEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        if self.context_menu_handler is not None:
            self.context_menu_handler(event)
        else:
            super().contextMenuEvent(event)

    def paintEvent(self, event: Any) -> None:
        super().paintEvent(event)
        # Paint zone overlay on top after the normal scene rendering
        if self._smw._hover_pane is not None:
            painter = QPainter(self.viewport())
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            self._smw._draw_hover_overlay(painter)
            painter.end()
