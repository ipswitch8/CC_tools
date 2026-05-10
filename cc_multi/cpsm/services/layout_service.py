# -*- coding: utf-8 -*-
"""
LayoutService — split-and-lock invariant and pane removal per §6.8.

Spec sections: §6.8, §5.3
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from cpsm.data.schema import CpsmDocument, ScreenLayout, Viewport
from cpsm.platform.base import MultiplexerBackend

if TYPE_CHECKING:
    pass

__all__ = ["LayoutService"]

logger = logging.getLogger(__name__)


class LayoutService:
    """Post-change capture-and-lock for the split-and-lock invariant (§6.8).

    The split-and-lock invariant requires that after every add/resize operation
    that modifies a tmux window layout, CPSM immediately:
      1. Calls ``backend.capture_layout(window_target)`` to get ``#{window_layout}``.
      2. Writes the result to ``viewport.custom_layout_string``.
      3. Sets ``viewport.tmux_layout = "custom"``.

    This prevents tmux from silently redistributing panes when new panes are
    added to a window already using a preset layout.
    """

    def __init__(self, backend: MultiplexerBackend) -> None:
        self._backend = backend

    # ------------------------------------------------------------------
    # Post-change capture and lock
    # ------------------------------------------------------------------

    def apply_change(
        self,
        doc: CpsmDocument,
        layout: ScreenLayout,
        viewport: Viewport,
        *,
        change_kind: Literal["split", "swap", "resize", "respawn"],
        window_target: str,
    ) -> ScreenLayout:
        """Capture the current ``#{window_layout}`` and lock *viewport* to custom.

        This method should be called AFTER the caller has already issued the
        tmux operation (split-window, resize-pane, etc.).  It performs only
        the post-change capture-and-lock step.

        Parameters
        ----------
        doc:
            The full CPSM document (for context, not mutated here).
        layout:
            The ScreenLayout that contains *viewport*.
        viewport:
            The viewport whose pane layout just changed.
        change_kind:
            Informational tag for logging (``"split"``, ``"swap"``,
            ``"resize"``, or ``"respawn"``).
        window_target:
            The tmux window target string (e.g. ``"cpsm-myconn:0"``).

        Returns
        -------
        ScreenLayout
            A new ScreenLayout with the updated viewport (the original layout
            is *not* mutated; pydantic models are treated as immutable here).
        """
        layout_string = self._backend.capture_layout(window_target)
        logger.debug(
            "apply_change kind=%s window=%s captured layout: %s",
            change_kind,
            window_target,
            layout_string,
        )

        # Rebuild the viewport with updated custom_layout_string and tmux_layout
        updated_vp = viewport.model_copy(
            update={
                "custom_layout_string": layout_string,
                "tmux_layout": "custom",
            }
        )

        # Rebuild the layout with the updated viewport
        return _replace_viewport_in_layout(layout, viewport.id, updated_vp)

    # ------------------------------------------------------------------
    # Pane removal
    # ------------------------------------------------------------------

    def remove_pane(
        self,
        doc: CpsmDocument,
        layout: ScreenLayout,
        viewport: Viewport,
        pane_index: int,
        *,
        window_target: str,
        pane_target: str,
        placeholder_command: str,
    ) -> ScreenLayout:
        """Remove a pane from *viewport* honouring ``layout_preserve_on_remove``.

        When ``settings.layout_preserve_on_remove`` is True (the default):
          - Issue ``respawn-pane -k`` with the placeholder script.
          - The pane stays in the layout; its connection_id is set to None.
          - The window_layout string is re-captured and locked.

        When ``layout_preserve_on_remove`` is False:
          - Issue ``kill-pane`` so tmux redistributes remaining panes.
          - The pane entry is removed from the viewport's pane list.
          - The window_layout string is re-captured and locked.

        Parameters
        ----------
        doc:
            Full CPSM document (used to read settings).
        layout:
            ScreenLayout containing *viewport*.
        viewport:
            Viewport that owns the pane to remove.
        pane_index:
            Zero-based index into ``viewport.panes``.
        window_target:
            tmux window target (e.g. ``"session:0"``).
        pane_target:
            tmux pane target (e.g. ``"session:0.2"``).
        placeholder_command:
            Full command string for the placeholder script
            (e.g. ``"bash /tmp/cpsm-placeholder.sh"``).
        """
        preserve = doc.settings.layout_preserve_on_remove

        if preserve:
            # Respawn with placeholder — geometry is preserved
            self._backend.respawn_pane(pane_target, placeholder_command)
            layout_string = self._backend.capture_layout(window_target)

            # Set the pane's connection_id to None (empty slot)
            old_pane = viewport.panes[pane_index]
            updated_pane = old_pane.model_copy(update={"connection_id": None})
            new_panes = list(viewport.panes)
            new_panes[pane_index] = updated_pane

            updated_vp = viewport.model_copy(
                update={
                    "panes": new_panes,
                    "custom_layout_string": layout_string,
                    "tmux_layout": "custom",
                }
            )
            logger.debug("remove_pane: respawned placeholder at %s; layout captured", pane_target)
        else:
            # Kill pane — tmux redistributes
            self._backend.kill_pane(pane_target)
            layout_string = self._backend.capture_layout(window_target)

            # Drop the pane entry from the list
            new_panes = list(viewport.panes)
            if 0 <= pane_index < len(new_panes):
                del new_panes[pane_index]

            updated_vp = viewport.model_copy(
                update={
                    "panes": new_panes,
                    "custom_layout_string": layout_string,
                    "tmux_layout": "custom",
                }
            )
            logger.debug("remove_pane: killed pane %s; layout captured", pane_target)

        return _replace_viewport_in_layout(layout, viewport.id, updated_vp)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _replace_viewport_in_layout(
    layout: ScreenLayout,
    viewport_id: str,
    new_viewport: Viewport,
) -> ScreenLayout:
    """Return a copy of *layout* with the viewport matching *viewport_id* replaced."""
    new_monitors = []
    for monitor in layout.monitors:
        new_viewports = []
        for vp in monitor.viewports:
            if vp.id == viewport_id:
                new_viewports.append(new_viewport)
            else:
                new_viewports.append(vp)
        new_monitors.append(monitor.model_copy(update={"viewports": new_viewports}))

    return layout.model_copy(update={"monitors": new_monitors})
