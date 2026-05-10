# -*- coding: utf-8 -*-
"""
cpsm.ui.widgets.group_legend — GroupLegendDock for multi-group overlay control.

Spec sections: §6.4, §6.5

Phase 17 additions:
  - ``GroupLegendDock(QDockWidget)`` — one row per visible group with:
      * Color swatch (clickable → QColorDialog to override group color)
      * Eye toggle (show/hide group in overlay)
      * Lock toggle (prevent editing — only one group can be edit target)
      * Dirty indicator (filled dot when the group has unsaved changes)
      * Per-group Save and Revert buttons
  - Signals: ``visibility_changed``, ``edit_target_changed``, ``save_requested``,
    ``revert_requested``, ``color_changed``.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from cpsm.ui.widgets.screen_map import group_color_for_id

__all__ = ["GroupLegendDock"]

# ---------------------------------------------------------------------------
# Per-row widget
# ---------------------------------------------------------------------------


class _GroupRow(QWidget):
    """One row in the legend list representing a single group.

    Emits high-level events via the ``GroupLegendDock`` parent signals.
    The row keeps its own mutable state (visible, locked, dirty) that the
    dock can query.
    """

    def __init__(
        self,
        group_id: str,
        group_name: str,
        color: QColor,
        dock: GroupLegendDock,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.group_id = group_id
        self._dock = dock
        self._color = color
        self._is_visible = True
        self._is_locked = False
        self._is_dirty = False

        self.setObjectName(f"group_legend_row_{group_id}")
        self.setAccessibleName(f"Group legend row for {group_name}")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(6)

        # Color swatch button
        self._btn_color = QPushButton()
        self._btn_color.setObjectName(f"group_legend_swatch_{group_id}")
        self._btn_color.setAccessibleName(f"Color swatch for {group_name}")
        self._btn_color.setFixedSize(20, 20)
        self._btn_color.setToolTip("Click to change group color")
        self._apply_swatch_style()
        self._btn_color.clicked.connect(self._on_color_clicked)
        layout.addWidget(self._btn_color)

        # Group name label
        self._lbl_name = QLabel(group_name)
        self._lbl_name.setObjectName(f"group_legend_name_{group_id}")
        self._lbl_name.setAccessibleName(f"Group name: {group_name}")
        self._lbl_name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(self._lbl_name)

        # Dirty indicator — a small filled dot label
        self._lbl_dirty = QLabel("●")  # filled circle
        self._lbl_dirty.setObjectName(f"group_legend_dirty_{group_id}")
        self._lbl_dirty.setAccessibleName(f"Dirty indicator for {group_name}")
        self._lbl_dirty.setToolTip("Unsaved changes")
        self._lbl_dirty.setVisible(False)
        layout.addWidget(self._lbl_dirty)

        # Eye toggle (visibility)
        self._btn_eye = QToolButton()
        self._btn_eye.setObjectName(f"group_legend_eye_{group_id}")
        self._btn_eye.setAccessibleName(f"Visibility toggle for {group_name}")
        self._btn_eye.setCheckable(True)
        self._btn_eye.setChecked(True)
        self._btn_eye.setText("\U0001f441")  # eye emoji
        self._btn_eye.setToolTip("Toggle visibility")
        self._btn_eye.toggled.connect(self._on_eye_toggled)
        layout.addWidget(self._btn_eye)

        # Lock toggle (prevents this group from being the edit target)
        self._btn_lock = QToolButton()
        self._btn_lock.setObjectName(f"group_legend_lock_{group_id}")
        self._btn_lock.setAccessibleName(f"Lock toggle for {group_name}")
        self._btn_lock.setCheckable(True)
        self._btn_lock.setChecked(False)
        self._btn_lock.setText("\U0001f513")  # open lock
        self._btn_lock.setToolTip("Lock editing for this group")
        self._btn_lock.toggled.connect(self._on_lock_toggled)
        layout.addWidget(self._btn_lock)

        # Save button
        self._btn_save = QPushButton("Save")
        self._btn_save.setObjectName(f"group_legend_save_{group_id}")
        self._btn_save.setAccessibleName(f"Save changes for {group_name}")
        self._btn_save.setFixedWidth(50)
        self._btn_save.clicked.connect(self._on_save_clicked)
        layout.addWidget(self._btn_save)

        # Revert button
        self._btn_revert = QPushButton("Revert")
        self._btn_revert.setObjectName(f"group_legend_revert_{group_id}")
        self._btn_revert.setAccessibleName(f"Revert changes for {group_name}")
        self._btn_revert.setFixedWidth(55)
        self._btn_revert.clicked.connect(self._on_revert_clicked)
        layout.addWidget(self._btn_revert)

    # ------------------------------------------------------------------
    # State accessors
    # ------------------------------------------------------------------

    @property
    def is_visible(self) -> bool:
        return self._is_visible

    @property
    def is_locked(self) -> bool:
        return self._is_locked

    @property
    def is_dirty(self) -> bool:
        return self._is_dirty

    @property
    def color(self) -> QColor:
        return self._color

    def set_dirty(self, dirty: bool) -> None:
        """Update the dirty marker."""
        self._is_dirty = dirty
        self._lbl_dirty.setVisible(dirty)

    def set_color(self, color: QColor) -> None:
        """Update the color swatch."""
        self._color = color
        self._apply_swatch_style()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _apply_swatch_style(self) -> None:
        hex_color = self._color.name()
        self._btn_color.setStyleSheet(
            f"QPushButton {{ background-color: {hex_color}; border: 1px solid #555; }}"
        )

    def _on_color_clicked(self) -> None:
        chosen = QColorDialog.getColor(self._color, self, f"Group color — {self.group_id}")
        if chosen.isValid():
            self._color = chosen
            self._apply_swatch_style()
            self._dock.color_changed.emit(self.group_id, chosen.name())

    def _on_eye_toggled(self, checked: bool) -> None:
        self._is_visible = checked
        self._btn_eye.setText("\U0001f441" if checked else "\U0001f648")  # eye / hidden
        self._dock.visibility_changed.emit(self.group_id, checked)
        self._dock._on_visibility_changed(self.group_id, checked)

    def _on_lock_toggled(self, locked: bool) -> None:
        self._is_locked = locked
        self._btn_lock.setText("\U0001f512" if locked else "\U0001f513")  # lock / open
        self._dock._on_lock_changed(self.group_id, locked)

    def _on_save_clicked(self) -> None:
        self._dock.save_requested.emit(self.group_id)

    def _on_revert_clicked(self) -> None:
        self._dock.revert_requested.emit(self.group_id)


# ---------------------------------------------------------------------------
# GroupLegendDock
# ---------------------------------------------------------------------------


class GroupLegendDock(QDockWidget):
    """Dock widget showing one row per visible group for multi-group overlay control.

    Signals
    -------
    visibility_changed(group_id, visible):
        Emitted when the eye toggle is clicked for a group.
    edit_target_changed(group_id):
        Emitted when the effective edit target changes (due to lock toggles).
        An empty string means all groups are locked (no edit target).
    save_requested(group_id):
        Emitted when the Save button is clicked for a group.
    revert_requested(group_id):
        Emitted when the Revert button is clicked for a group.
    color_changed(group_id, hex_color):
        Emitted when the user picks a new color for a group.
    """

    visibility_changed: Signal = Signal(str, bool)
    """visibility_changed(group_id, visible)"""

    edit_target_changed: Signal = Signal(str)
    """edit_target_changed(group_id) — empty string when all locked."""

    save_requested: Signal = Signal(str)
    """save_requested(group_id)"""

    revert_requested: Signal = Signal(str)
    """revert_requested(group_id)"""

    color_changed: Signal = Signal(str, str)
    """color_changed(group_id, hex_color)"""

    def __init__(self, parent: Any = None) -> None:
        super().__init__("Group Legend", parent)

        self.setObjectName("dock_group_legend")
        self.setAccessibleName("Group Legend Dock")
        self.setAccessibleDescription(
            "Shows one row per visible group with color, visibility, and lock controls"
        )

        # Internal row registry: group_id → _GroupRow
        self._rows: dict[str, _GroupRow] = {}

        # Container widget
        self._container = QWidget()
        self._container.setObjectName("group_legend_container")

        root_layout = QVBoxLayout(self._container)
        root_layout.setContentsMargins(2, 2, 2, 2)
        root_layout.setSpacing(0)

        # QListWidget — one item per group (objectName required for Selenium)
        self._list_widget = QListWidget()
        self._list_widget.setObjectName("legend_list")
        self._list_widget.setAccessibleName("Legend list")
        self._list_widget.setSpacing(1)
        self._list_widget.setSelectionMode(QListWidget.SelectionMode.SingleSelection)

        root_layout.addWidget(self._list_widget)
        self.setWidget(self._container)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_group(
        self,
        group_id: str,
        group_name: str,
        color_override: str | None = None,
    ) -> None:
        """Add a group row to the legend.

        Parameters
        ----------
        group_id:
            Unique group identifier.
        group_name:
            Human-readable group name displayed in the row.
        color_override:
            Optional hex color string.  When ``None``, the auto-palette
            color is used (stable based on group_id hash).
        """
        if group_id in self._rows:
            return  # already present

        color = group_color_for_id(group_id, color_override)
        row_widget = _GroupRow(group_id, group_name, color, self)

        item = QListWidgetItem(self._list_widget)
        item.setData(Qt.ItemDataRole.UserRole, group_id)
        item.setSizeHint(row_widget.sizeHint())
        self._list_widget.setItemWidget(item, row_widget)
        self._rows[group_id] = row_widget

    def remove_group(self, group_id: str) -> None:
        """Remove the row for *group_id* from the legend."""
        if group_id not in self._rows:
            return
        # Find the item and remove it
        for i in range(self._list_widget.count()):
            item = self._list_widget.item(i)
            if item and item.data(Qt.ItemDataRole.UserRole) == group_id:
                self._list_widget.takeItem(i)
                break
        del self._rows[group_id]

    def update_dirty(self, group_id: str, is_dirty: bool) -> None:
        """Update the dirty marker for *group_id*."""
        row = self._rows.get(group_id)
        if row is not None:
            row.set_dirty(is_dirty)

    def update_color(self, group_id: str, hex_color: str) -> None:
        """Update the color swatch for *group_id* programmatically."""
        row = self._rows.get(group_id)
        if row is not None:
            color = QColor(hex_color)
            if color.isValid():
                row.set_color(color)

    def visible_group_ids(self) -> list[str]:
        """Return IDs of all currently visible (eye-on) groups in display order."""
        result: list[str] = []
        for i in range(self._list_widget.count()):
            item = self._list_widget.item(i)
            if item is None:
                continue
            gid = item.data(Qt.ItemDataRole.UserRole)
            row = self._rows.get(gid)
            if row is not None and row.is_visible:
                result.append(gid)
        return result

    def current_edit_target(self) -> str | None:
        """Return the group_id of the current edit target, or None if all locked."""
        candidates: list[str] = []
        for i in range(self._list_widget.count()):
            item = self._list_widget.item(i)
            if item is None:
                continue
            gid = item.data(Qt.ItemDataRole.UserRole)
            row = self._rows.get(gid)
            if row is not None and row.is_visible and not row.is_locked:
                candidates.append(gid)
        return candidates[0] if candidates else None

    # ------------------------------------------------------------------
    # Internal callbacks (called by _GroupRow)
    # ------------------------------------------------------------------

    def _on_visibility_changed(self, group_id: str, visible: bool) -> None:
        """Re-derive edit target after a visibility change."""
        self._emit_edit_target()

    def _on_lock_changed(self, group_id: str, locked: bool) -> None:
        """Re-derive edit target after a lock toggle."""
        self._emit_edit_target()

    def _emit_edit_target(self) -> None:
        """Compute and emit the current edit target."""
        target = self.current_edit_target()
        self.edit_target_changed.emit(target or "")
