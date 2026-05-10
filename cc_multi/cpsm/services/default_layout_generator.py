# -*- coding: utf-8 -*-
"""
cpsm.services.default_layout_generator — Generate a sensible default
ScreenLayout for a group given its members and live monitors.

Spec: Change 1 of UX redesign.
"""

from __future__ import annotations

from cpsm.data.schema import GeometryPct, Monitor, Pane, ScreenLayout, Viewport
from cpsm.services.monitor_service import MonitorInfo

__all__ = ["generate_default_layout"]


def generate_default_layout(
    *,
    group_id: str,
    group_name: str | None = None,
    member_count: int,
    monitors: list[MonitorInfo],
) -> ScreenLayout:
    """Return a sensible default :class:`~cpsm.data.schema.ScreenLayout` for *group_id*.

    Parameters
    ----------
    group_id:
        Slug id of the group.
    group_name:
        Human-readable name used in the layout name.  Falls back to *group_id*
        when *None*.
    member_count:
        Number of connections in the group.  Used to distribute panes
        round-robin across viewports.
    monitors:
        Live :class:`~cpsm.services.monitor_service.MonitorInfo` list from
        ``MonitorService.snapshot()``.  Each monitor gets one viewport.

    Returns
    -------
    ScreenLayout
        Layout with ``id = f"{group_id}-default-layout"`` and panes whose
        ``connection_id`` is ``None`` (caller must wire real ids).
        Returns an empty layout (no monitors) when *member_count* == 0 or
        *monitors* is empty.
    """
    display_name = group_name or group_id
    layout_id = f"{group_id}-default-layout"
    layout_name = f"{display_name} default"

    # Edge cases → empty layout
    if member_count == 0 or not monitors:
        return ScreenLayout(id=layout_id, name=layout_name, monitors=[])

    m = len(monitors)
    layout_monitors: list[Monitor] = []

    for monitor_index, _monitor_info in enumerate(monitors):
        # Determine which pane slots belong to this monitor (round-robin)
        # Pane indices: monitor_index, monitor_index + M, monitor_index + 2M, …
        pane_indices = list(range(monitor_index, member_count, m))

        # Build one Pane per assigned slot (connection_id=None as placeholder)
        panes = [Pane(connection_id=None) for _ in pane_indices]

        viewport_id = f"{group_id}-vp-{monitor_index}"
        window_name = f"{group_id}-{monitor_index}"

        vp = Viewport(
            id=viewport_id,
            geometry_pct=GeometryPct(x=0, y=0, w=100, h=100),
            tmux_window_name=window_name,
            tmux_layout="tiled",
            panes=panes,
        )

        layout_monitors.append(Monitor(viewports=[vp]))

    return ScreenLayout(id=layout_id, name=layout_name, monitors=layout_monitors)
