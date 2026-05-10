# -*- coding: utf-8 -*-
"""
cpsm.controllers.layout_controller — LayoutController.

Spec sections: §6.7, §6.8, §3.3

Mediates ScreenMapWidget signals → LayoutService + backend operations.
All tmux operations are issued here; the widget emits signals only.

After every tmux operation the split-and-lock invariant (§6.8) is enforced
by calling ``LayoutService.apply_change()``, which captures the live
``#{window_layout}`` string, writes it to ``viewport.custom_layout_string``,
and sets ``viewport.tmux_layout = "custom"``.

The controller marks the configuration dirty after each operation but does
NOT save it — the user must click "Save All" to persist.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Literal

from PySide6.QtCore import QObject, Slot

from cpsm.data.schema import CpsmDocument, ScreenLayout, Viewport
from cpsm.platform.base import MultiplexerBackend
from cpsm.services.config_service import ConfigService
from cpsm.services.layout_service import LayoutService
from cpsm.services.session_service import SessionService
from cpsm.services.template_service import TemplateService

if TYPE_CHECKING:
    pass

__all__ = ["LayoutController"]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Drop-zone → split direction mapping (§6.7)
# ---------------------------------------------------------------------------

# Maps a drop zone to (direction, before).
# "top" → split vertical with before=True (new pane appears above target).
# "bottom" → split vertical with before=False (new pane below).
# "left" → split horizontal with before=True (new pane left of target).
# "right" → split horizontal with before=False (new pane right of target).
_ZONE_TO_SPLIT: dict[str, tuple[Literal["h", "v"], bool]] = {
    "top": ("v", True),
    "bottom": ("v", False),
    "left": ("h", True),
    "right": ("h", False),
}


def _zone_for_modifiers(zone: str, modifiers: int) -> str:
    """Apply Shift/Ctrl modifier overrides to the detected drop zone.

    Spec §6.7:
        Shift → force horizontal split (left/right) regardless of zone.
        Ctrl  → force vertical split (top/bottom) regardless of zone.
        When both are held, Shift takes precedence.
    """
    from PySide6.QtCore import Qt

    qt_mods = Qt.KeyboardModifier(modifiers)
    if qt_mods & Qt.KeyboardModifier.ShiftModifier:
        # Force horizontal split — treat as "right" edge
        return "right"
    if qt_mods & Qt.KeyboardModifier.ControlModifier:
        # Force vertical split — treat as "bottom" edge
        return "bottom"
    return zone


# ---------------------------------------------------------------------------
# LayoutController
# ---------------------------------------------------------------------------


class LayoutController(QObject):
    """Mediates ScreenMapWidget drag-drop signals to LayoutService + backend.

    Parameters
    ----------
    config:
        ConfigService for document lookups.
    layout:
        LayoutService that implements the split-and-lock invariant.
    session:
        SessionService for pane-level launch helpers.
    backend:
        MultiplexerBackend (e.g. TmuxBackend) for raw tmux operations.
    templates:
        TemplateService to render placeholder and launcher scripts.
    parent:
        Optional Qt parent object.
    """

    def __init__(
        self,
        config: ConfigService,
        layout: LayoutService,
        session: SessionService,
        backend: MultiplexerBackend,
        templates: TemplateService,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._layout = layout
        self._session = session
        self._backend = backend
        self._templates = templates

        # Active document and layout — must be set before any slot is called.
        self._doc: CpsmDocument | None = None
        self._screen_layout: ScreenLayout | None = None

        # Dirty flag — set after every operation; cleared on save
        self._dirty: bool = False

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    def set_document(self, doc: CpsmDocument) -> None:
        """Set the active CPSM document."""
        self._doc = doc

    def set_screen_layout(self, screen_layout: ScreenLayout) -> None:
        """Set the active ScreenLayout (the one currently shown in the widget)."""
        self._screen_layout = screen_layout

    @property
    def is_dirty(self) -> bool:
        """True if the layout was modified since the last save."""
        return self._dirty

    def mark_clean(self) -> None:
        """Clear the dirty flag (called after the user saves)."""
        self._dirty = False

    # ------------------------------------------------------------------
    # Slots — wired to ScreenMapWidget signals
    # ------------------------------------------------------------------

    @Slot(str, str, str, int)
    def on_drop_connection(
        self,
        connection_id: str,
        target_pane_id: str,
        zone: str,
        modifiers: int,
    ) -> None:
        """Handle a connection dropped onto a pane.

        Parameters
        ----------
        connection_id:
            ID of the connection being dropped from the sidebar.
        target_pane_id:
            Pane ID of the drop target.
        zone:
            Drop zone (``"top"``, ``"bottom"``, ``"left"``, ``"right"``,
            or ``"center"``).
        modifiers:
            Qt.KeyboardModifiers integer at drop time.
        """
        if not self._check_ready("on_drop_connection"):
            return

        effective_zone = _zone_for_modifiers(zone, modifiers)
        logger.debug(
            "on_drop_connection conn=%s target=%s zone=%s (effective=%s)",
            connection_id,
            target_pane_id,
            zone,
            effective_zone,
        )

        # Render the launcher for the dropped connection
        assert self._doc is not None  # guarded by _check_ready
        doc = self._doc
        conn = self._config.find_connection(doc, connection_id)
        if conn is None:
            logger.warning("on_drop_connection: connection '%s' not found", connection_id)
            return

        try:
            launcher = self._templates.render(
                conn.launch_profile,
                conn,
                settings=doc.settings,
                templates=doc.launch_templates,
            )
        except Exception as exc:
            logger.error("on_drop_connection: failed to render launcher: %s", exc)
            return

        vp = self._find_viewport_for_pane(target_pane_id)
        if vp is None:
            logger.warning("on_drop_connection: viewport for pane '%s' not found", target_pane_id)
            return

        is_empty = self._pane_is_empty(target_pane_id, vp)
        window_target = vp.tmux_window_name or vp.id

        if is_empty:
            # Drop on empty slot → respawn without dialog
            self._backend.respawn_pane(target_pane_id, launcher, kill_existing=True)
            self._apply_and_lock(vp, window_target, "respawn")

        elif effective_zone == "center":
            # Drop on occupied pane centre → disambiguation dialog
            choice = self._ask_disambiguation(connection_id, target_pane_id)
            if choice == "cancel":
                return
            if choice == "replace":
                self._backend.respawn_pane(target_pane_id, launcher, kill_existing=True)
                self._apply_and_lock(vp, window_target, "respawn")
            elif choice == "split-right":
                new_pane = self._backend.split_pane(target_pane_id, "h", before=False)
                self._backend.send_keys(new_pane.id, launcher)
                self._apply_and_lock(vp, window_target, "split")
            elif choice == "split-below":
                new_pane = self._backend.split_pane(target_pane_id, "v", before=False)
                self._backend.send_keys(new_pane.id, launcher)
                self._apply_and_lock(vp, window_target, "split")

        else:
            # Drop on an edge → split
            direction, before = _ZONE_TO_SPLIT.get(effective_zone, ("h", False))
            new_pane = self._backend.split_pane(target_pane_id, direction, before=before)
            self._backend.send_keys(new_pane.id, launcher)
            self._apply_and_lock(vp, window_target, "split")

    @Slot(str, str, str, int)
    def on_drop_pane(
        self,
        src_pane_id: str,
        dst_pane_id: str,
        zone: str,
        modifiers: int,
    ) -> None:
        """Handle a pane dragged onto another pane.

        Parameters
        ----------
        src_pane_id:
            Pane ID of the pane being dragged.
        dst_pane_id:
            Pane ID of the drop target.  Empty string means "empty space"
            (pane-drag-out gesture).
        zone:
            Drop zone.
        modifiers:
            Qt.KeyboardModifiers integer at drop time.
        """
        if not self._check_ready("on_drop_pane"):
            return

        effective_zone = _zone_for_modifiers(zone, modifiers)
        logger.debug(
            "on_drop_pane src=%s dst=%s zone=%s (effective=%s)",
            src_pane_id,
            dst_pane_id,
            zone,
            effective_zone,
        )

        from PySide6.QtCore import Qt

        shift_held = bool(Qt.KeyboardModifier(modifiers) & Qt.KeyboardModifier.ShiftModifier)

        if not dst_pane_id:
            # Pane dragged to empty space
            if shift_held:
                # Shift+drag-out → kill source
                self._backend.kill_pane(src_pane_id)
            else:
                # Normal drag-out → break_pane detached
                self._backend.break_pane(src_pane_id, detached=True)
            # No apply_change needed (pane removed from window)
            return

        src_vp = self._find_viewport_for_pane(src_pane_id)
        dst_vp = self._find_viewport_for_pane(dst_pane_id)

        if src_vp is None or dst_vp is None:
            logger.warning(
                "on_drop_pane: could not find viewports for src=%s dst=%s",
                src_pane_id,
                dst_pane_id,
            )
            return

        same_window = src_vp.id == dst_vp.id
        window_target = dst_vp.tmux_window_name or dst_vp.id

        if same_window:
            if effective_zone == "center":
                # Same-window center → swap
                self._backend.swap_panes(src_pane_id, dst_pane_id)
                self._apply_and_lock(dst_vp, window_target, "swap")
            else:
                # Same-window edge → swap then respawn source as placeholder
                self._backend.swap_panes(src_pane_id, dst_pane_id)
                try:
                    placeholder = self._templates.render_placeholder()
                    self._backend.respawn_pane(src_pane_id, placeholder, kill_existing=True)
                except Exception as exc:
                    logger.warning("on_drop_pane: placeholder respawn failed: %s", exc)
                self._apply_and_lock(dst_vp, window_target, "swap")
        else:
            # Cross-viewport drag
            src_window = src_vp.tmux_window_name or src_vp.id
            # Break source pane → placeholder in source window
            self._backend.break_pane(src_pane_id, detached=True)
            # Move broken pane to destination window
            self._backend.move_pane(src_pane_id, window_target)
            # Apply split at drop zone if not center
            if effective_zone != "center":
                direction, before = _ZONE_TO_SPLIT.get(effective_zone, ("h", False))
                self._backend.split_pane(dst_pane_id, direction, before=before)
            self._apply_and_lock(dst_vp, window_target, "split")
            # Also capture source window if still valid
            with contextlib.suppress(Exception):
                self._backend.capture_layout(src_window)

    @Slot(str)
    def on_remove_pane(self, pane_id: str) -> None:
        """Remove a pane from the layout (right-click → Remove from Layout).

        Behaviour depends on ``settings.layout_preserve_on_remove``:
          - True (default) → respawn with placeholder (geometry preserved).
          - False → kill_pane.
        """
        if not self._check_ready("on_remove_pane"):
            return

        assert self._doc is not None  # guarded by _check_ready
        assert self._screen_layout is not None  # guarded by _check_ready
        doc = self._doc
        vp = self._find_viewport_for_pane(pane_id)
        if vp is None:
            logger.warning("on_remove_pane: viewport for pane '%s' not found", pane_id)
            return

        pane_index = self._find_pane_index(pane_id, vp)
        window_target = vp.tmux_window_name or vp.id

        try:
            placeholder = self._templates.render_placeholder()
        except Exception as exc:
            logger.error("on_remove_pane: could not render placeholder: %s", exc)
            placeholder = "bash"

        layout = self._screen_layout
        new_layout = self._layout.remove_pane(
            doc,
            layout,
            vp,
            pane_index,
            window_target=window_target,
            pane_target=pane_id,
            placeholder_command=placeholder,
        )
        self._screen_layout = new_layout
        self._dirty = True
        logger.debug("on_remove_pane: pane '%s' removed", pane_id)

    @Slot(str)
    def on_kill_pane(self, pane_id: str) -> None:
        """Kill a pane unconditionally (right-click → Kill Pane).

        This always kills regardless of ``layout_preserve_on_remove``.
        """
        if not self._check_ready("on_kill_pane"):
            return

        logger.debug("on_kill_pane: killing pane '%s'", pane_id)
        try:
            self._backend.kill_pane(pane_id)
        except Exception as exc:
            logger.error("on_kill_pane: backend.kill_pane failed: %s", exc)
            return

        # Capture layout of the owning viewport if possible
        vp = self._find_viewport_for_pane(pane_id)
        if vp is not None:
            window_target = vp.tmux_window_name or vp.id
            self._apply_and_lock(vp, window_target, "split")
        self._dirty = True

    @Slot(str, int, int)
    def on_resize_pane(self, pane_id: str, w: int, h: int) -> None:
        """Resize a pane and capture-and-lock the result.

        Parameters
        ----------
        pane_id:
            tmux pane target.
        w, h:
            New width and height in cells.
        """
        if not self._check_ready("on_resize_pane"):
            return

        logger.debug("on_resize_pane: pane='%s' w=%d h=%d", pane_id, w, h)
        try:
            self._backend.resize_pane(pane_id, w, h)
        except Exception as exc:
            logger.error("on_resize_pane: backend.resize_pane failed: %s", exc)
            return

        vp = self._find_viewport_for_pane(pane_id)
        if vp is not None:
            window_target = vp.tmux_window_name or vp.id
            self._apply_and_lock(vp, window_target, "resize")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_ready(self, slot_name: str) -> bool:
        """Return True if doc and screen_layout are set; log warning otherwise."""
        if self._doc is None:
            logger.warning("%s: no active document set on LayoutController", slot_name)
            return False
        if self._screen_layout is None:
            logger.warning("%s: no active screen_layout set on LayoutController", slot_name)
            return False
        return True

    def _find_viewport_for_pane(self, pane_id: str) -> Viewport | None:
        """Return the Viewport that contains *pane_id*, or None."""
        if self._screen_layout is None:
            return None
        for monitor in self._screen_layout.monitors:
            for vp in monitor.viewports:
                for pane in vp.panes:
                    if pane.connection_id == pane_id:
                        return vp
                # Also check by empty-slot synthetic id pattern
                for i, _pane in enumerate(vp.panes):
                    synthetic = f"__empty_{i}"
                    if pane_id == synthetic:
                        return vp
        return None

    def _find_pane_index(self, pane_id: str, vp: Viewport) -> int:
        """Return the zero-based index of *pane_id* within *vp.panes*, or 0."""
        for i, pane in enumerate(vp.panes):
            if pane.connection_id == pane_id:
                return i
        return 0

    def _pane_is_empty(self, pane_id: str, vp: Viewport) -> bool:
        """Return True if the pane identified by *pane_id* has no connection."""
        return all(pane.connection_id != pane_id for pane in vp.panes)

    def _apply_and_lock(
        self,
        vp: Viewport,
        window_target: str,
        change_kind: Literal["split", "swap", "resize", "respawn"],
    ) -> None:
        """Invoke LayoutService.apply_change and mark dirty."""
        if self._doc is None or self._screen_layout is None:
            return
        try:
            new_layout = self._layout.apply_change(
                self._doc,
                self._screen_layout,
                vp,
                change_kind=change_kind,
                window_target=window_target,
            )
            self._screen_layout = new_layout
            self._dirty = True
        except Exception as exc:
            logger.error("_apply_and_lock: apply_change failed: %s", exc)

    def _ask_disambiguation(self, connection_id: str, pane_id: str) -> str:
        """Show DropDisambiguationDialog and return the user's choice.

        Imported lazily to avoid circular imports and to allow tests to
        patch the dialog class.
        """
        from cpsm.ui.dialogs.drop_disambiguation import DropDisambiguationDialog

        return DropDisambiguationDialog.ask(
            connection_name=connection_id,
            pane_label=pane_id,
        )
