# -*- coding: utf-8 -*-
"""
cpsm.ui.dialogs.group_editor — Group Editor dialog.

Spec sections: §4.6

Layout (top → bottom):
  - Identity: name, id (auto-suggest from name)
  - Color: QPushButton swatch → QColorDialog
  - Members: GroupPanel (two-list drag-drop)
  - Launch order: radio Sequential / Parallel + launch_delay_ms SpinBox
  - Default layout: combo of available screen_layout ids
  - Isolation: radio shared / per-group
  - Layout conflict: combo move / keep / error
  - Auto attach: checkbox
  - Bottom: Save / Cancel
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from cpsm.data.schema import Group
from cpsm.ui.widgets.group_panel import ConnectionEntry, GroupPanel

if TYPE_CHECKING:
    from cpsm.data.schema import CpsmDocument

__all__ = ["GroupEditorDialog"]

_ID_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")

# Auto-assigned color palette (12 distinct colors)
_COLOR_PALETTE: list[str] = [
    "#3b82f6",  # blue
    "#10b981",  # green
    "#f59e0b",  # amber
    "#ef4444",  # red
    "#8b5cf6",  # violet
    "#ec4899",  # pink
    "#14b8a6",  # teal
    "#f97316",  # orange
    "#6366f1",  # indigo
    "#84cc16",  # lime
    "#06b6d4",  # cyan
    "#a855f7",  # purple
]

_PALETTE_INDEX = 0


def _next_auto_color() -> str:
    """Return the next auto-assigned color from the palette."""
    global _PALETTE_INDEX
    color = _COLOR_PALETTE[_PALETTE_INDEX % len(_COLOR_PALETTE)]
    _PALETTE_INDEX += 1
    return color


class GroupEditorDialog(QDialog):
    """Dialog for creating or editing a Group record."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        group_data: dict[str, Any] | None = None,
        all_connections: list[ConnectionEntry] | None = None,
        available_layout_ids: list[str] | None = None,
        is_new: bool = True,
        # Injected QColorDialog for testing
        color_dialog_factory: Any | None = None,
        # Document reference for inline layout management (Change 3)
        doc: CpsmDocument | None = None,
        save_callback: Any | None = None,
    ) -> None:
        super().__init__(parent)

        self._is_new = is_new
        self._all_connections = all_connections or []
        self._available_layout_ids = available_layout_ids or []
        self._color_dialog_factory = color_dialog_factory
        self._current_color: str = _next_auto_color()
        self._doc = doc
        self._save_callback = save_callback
        # Track layout ids that the user has deleted during this edit session
        self._pending_layout_deletes: list[str] = []

        self.setObjectName("dlg_group_editor")
        self.setAccessibleName("Group Editor Dialog")
        self.setAccessibleDescription("Dialog for creating or editing a CPSM group")
        self.setWindowTitle("New Group" if is_new else "Edit Group")
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setMinimumWidth(700)
        self.setMinimumHeight(560)

        self._build_ui()

        if group_data:
            self._populate(group_data)
        else:
            self._update_color_swatch(self._current_color)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # Outer layout owns the scroll area + the dialog button box.
        # Round C: previously a single QVBoxLayout on `self` held all the
        # form fields directly, which caused some fields to clip on smaller
        # screens. Wrapping the form content in a QScrollArea keeps every
        # field reachable regardless of the dialog's height.
        outer = QVBoxLayout(self)
        outer.setSpacing(8)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setObjectName("scroll_group_editor")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll, 1)

        content = QWidget()
        content.setObjectName("widget_group_editor_content")
        scroll.setWidget(content)

        root = QVBoxLayout(content)
        root.setSpacing(10)
        root.setContentsMargins(16, 16, 16, 16)

        # ---- Identity group ----
        grp_identity = QGroupBox("Identity")
        grp_identity.setObjectName("grp_identity")
        grp_identity.setAccessibleName("Identity group box")
        id_form = QFormLayout(grp_identity)
        id_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self._edit_name = QLineEdit()
        self._edit_name.setObjectName("edit_name")
        self._edit_name.setAccessibleName("Group name field")
        self._edit_name.setAccessibleDescription("Human-readable name for this group")
        self._edit_name.setPlaceholderText("e.g. Project 1")
        self._err_name = QLabel()
        self._err_name.setObjectName("error_name")
        self._err_name.setAccessibleName("Name error label")
        self._err_name.setStyleSheet("color: red;")
        self._err_name.setVisible(False)
        id_form.addRow("Name *", self._edit_name)
        id_form.addRow("", self._err_name)

        self._edit_id = QLineEdit()
        self._edit_id.setObjectName("edit_id")
        self._edit_id.setAccessibleName("Group ID field")
        self._edit_id.setAccessibleDescription(
            "Unique slug ID; auto-suggested from name; locked after first save"
        )
        self._edit_id.setPlaceholderText("e.g. project-1")
        if not self._is_new:
            self._edit_id.setReadOnly(True)
            self._edit_id.setToolTip("ID is locked after creation")
        self._err_id = QLabel()
        self._err_id.setObjectName("error_id")
        self._err_id.setAccessibleName("ID error label")
        self._err_id.setStyleSheet("color: red;")
        self._err_id.setVisible(False)
        id_form.addRow("ID *", self._edit_id)
        id_form.addRow("", self._err_id)

        root.addWidget(grp_identity)

        # ---- Color row ----
        color_row = QHBoxLayout()
        lbl_color = QLabel("Color:")
        lbl_color.setObjectName("lbl_color")
        lbl_color.setAccessibleName("Color label")
        color_row.addWidget(lbl_color)

        self._btn_color = QPushButton()
        self._btn_color.setObjectName("btn_color")
        self._btn_color.setAccessibleName("Color picker button")
        self._btn_color.setAccessibleDescription(
            "Click to pick the group color from a color dialog"
        )
        self._btn_color.setFixedSize(40, 24)
        self._btn_color.clicked.connect(self._on_color_clicked)
        color_row.addWidget(self._btn_color)

        self._lbl_color_value = QLabel()
        self._lbl_color_value.setObjectName("lbl_color_value")
        self._lbl_color_value.setAccessibleName("Color value label")
        color_row.addWidget(self._lbl_color_value)
        color_row.addStretch()
        root.addLayout(color_row)

        # ---- Members (GroupPanel) ----
        grp_members = QGroupBox("Members")
        grp_members.setObjectName("grp_members")
        grp_members.setAccessibleName("Members group box")
        members_layout = QVBoxLayout(grp_members)

        self._group_panel = GroupPanel()
        self._group_panel.setObjectName("group_panel_embed")
        self._group_panel.set_all_connections(self._all_connections)
        members_layout.addWidget(self._group_panel)

        root.addWidget(grp_members, stretch=1)

        # ---- Launch settings ----
        grp_launch = QGroupBox("Launch Settings")
        grp_launch.setObjectName("grp_launch")
        grp_launch.setAccessibleName("Launch settings group box")
        launch_form = QFormLayout(grp_launch)

        # Launch order radio
        order_row = QHBoxLayout()
        self._radio_group_order = QButtonGroup(self)
        self._radio_group_order.setObjectName("radio_group_order")
        self._radio_sequential = QRadioButton("Sequential")
        self._radio_sequential.setObjectName("radio_sequential")
        self._radio_sequential.setAccessibleName("Sequential launch order radio")
        self._radio_sequential.setAccessibleDescription("Launch connections one after another")
        self._radio_sequential.setChecked(True)
        self._radio_parallel = QRadioButton("Parallel")
        self._radio_parallel.setObjectName("radio_parallel")
        self._radio_parallel.setAccessibleName("Parallel launch order radio")
        self._radio_parallel.setAccessibleDescription("Launch all connections simultaneously")
        self._radio_group_order.addButton(self._radio_sequential)
        self._radio_group_order.addButton(self._radio_parallel)
        order_row.addWidget(self._radio_sequential)
        order_row.addWidget(self._radio_parallel)
        order_row.addStretch()
        order_widget = QWidget()
        order_widget.setObjectName("widget_launch_order")
        order_widget.setLayout(order_row)
        launch_form.addRow("Launch Order:", order_widget)

        self._spin_launch_delay_ms = QSpinBox()
        self._spin_launch_delay_ms.setObjectName("spin_launch_delay_ms")
        self._spin_launch_delay_ms.setAccessibleName("Launch delay ms spin")
        self._spin_launch_delay_ms.setAccessibleDescription(
            "Delay in milliseconds between sequential connection launches"
        )
        self._spin_launch_delay_ms.setRange(0, 60000)
        self._spin_launch_delay_ms.setSuffix(" ms")
        launch_form.addRow("Launch Delay:", self._spin_launch_delay_ms)

        self._combo_default_layout = QComboBox()
        self._combo_default_layout.setObjectName("combo_default_layout")
        self._combo_default_layout.setAccessibleName("Default layout combo")
        self._combo_default_layout.setAccessibleDescription(
            "Screen layout to apply when launching this group"
        )
        self._combo_default_layout.addItem("(none)", "")
        for lid in self._available_layout_ids:
            self._combo_default_layout.addItem(lid, lid)
        launch_form.addRow("Default Layout:", self._combo_default_layout)

        root.addWidget(grp_launch)

        # ---- Isolation row ----
        isolation_row = QHBoxLayout()
        isolation_row.setSpacing(8)

        lbl_isolation = QLabel("Isolation:")
        lbl_isolation.setObjectName("lbl_isolation")
        lbl_isolation.setAccessibleName("Isolation label")
        isolation_row.addWidget(lbl_isolation)

        self._radio_group_isolation = QButtonGroup(self)
        self._radio_group_isolation.setObjectName("radio_group_isolation")
        self._radio_shared = QRadioButton("Shared")
        self._radio_shared.setObjectName("radio_shared")
        self._radio_shared.setAccessibleName("Shared isolation radio")
        self._radio_shared.setAccessibleDescription("Sessions are shared across groups (default)")
        self._radio_shared.setChecked(True)
        self._radio_per_group = QRadioButton("Per-group")
        self._radio_per_group.setObjectName("radio_per_group")
        self._radio_per_group.setAccessibleName("Per-group isolation radio")
        self._radio_per_group.setAccessibleDescription(
            "Sessions are isolated per group (uses cpsm-<group_id>-<conn_id> naming)"
        )
        self._radio_group_isolation.addButton(self._radio_shared)
        self._radio_group_isolation.addButton(self._radio_per_group)
        isolation_row.addWidget(self._radio_shared)
        isolation_row.addWidget(self._radio_per_group)

        isolation_row.addSpacing(20)

        lbl_conflict = QLabel("Layout Conflict:")
        lbl_conflict.setObjectName("lbl_layout_conflict")
        lbl_conflict.setAccessibleName("Layout conflict label")
        isolation_row.addWidget(lbl_conflict)

        self._combo_layout_conflict = QComboBox()
        self._combo_layout_conflict.setObjectName("combo_layout_conflict")
        self._combo_layout_conflict.setAccessibleName("Layout conflict combo")
        self._combo_layout_conflict.setAccessibleDescription(
            "Action to take when a layout conflict is detected"
        )
        for val in ("move", "keep", "error"):
            self._combo_layout_conflict.addItem(val, val)
        isolation_row.addWidget(self._combo_layout_conflict)

        self._chk_auto_attach = QCheckBox("Auto Attach")
        self._chk_auto_attach.setObjectName("chk_auto_attach")
        self._chk_auto_attach.setAccessibleName("Auto attach checkbox")
        self._chk_auto_attach.setAccessibleDescription(
            "Automatically attach to the first terminal after launching the group"
        )
        isolation_row.addWidget(self._chk_auto_attach)
        isolation_row.addStretch()
        root.addLayout(isolation_row)

        # ---- Layouts section (Change 3) ----
        grp_layouts = QGroupBox("Layouts")
        grp_layouts.setObjectName("grp_group_layouts")
        grp_layouts.setAccessibleName("Group layouts group box")
        grp_layouts.setAccessibleDescription(
            "Lists screen layouts associated with this group; allows deletion"
        )
        layouts_vbox = QVBoxLayout(grp_layouts)

        self._layouts_list_widget = QListWidget()
        self._layouts_list_widget.setObjectName("list_group_layouts")
        self._layouts_list_widget.setAccessibleName("Group layouts list")
        self._layouts_list_widget.setAccessibleDescription("Screen layouts belonging to this group")
        self._layouts_list_widget.setMaximumHeight(120)
        layouts_vbox.addWidget(self._layouts_list_widget)

        root.addWidget(grp_layouts)

        # Track rename state: layout_id -> QLineEdit widget currently renaming (or None)
        self._rename_edits: dict[str, QLineEdit] = {}

        # ---- Bottom buttons ----
        self._btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self._btn_box.setObjectName("btn_box")

        self._btn_save = self._btn_box.button(QDialogButtonBox.StandardButton.Save)
        assert self._btn_save is not None
        self._btn_save.setObjectName("btn_save")
        self._btn_save.setAccessibleName("Save button")
        self._btn_save.setAccessibleDescription("Save the group and close the dialog")

        btn_cancel = self._btn_box.button(QDialogButtonBox.StandardButton.Cancel)
        assert btn_cancel is not None
        btn_cancel.setObjectName("btn_cancel")
        btn_cancel.setAccessibleName("Cancel button")
        btn_cancel.setAccessibleDescription("Discard changes and close the dialog")

        self._btn_box.accepted.connect(self._on_save)
        self._btn_box.rejected.connect(self.reject)
        # Button box lives outside the scroll area so OK / Cancel are
        # always visible at the bottom of the dialog. `self.layout()` is
        # the top-level QVBoxLayout we set on the dialog; fall back to
        # the inner root layout if it's missing for any reason.
        outer_layout = self.layout()
        if outer_layout is not None:
            outer_layout.addWidget(self._btn_box)
        else:  # pragma: no cover — defensive
            root.addWidget(self._btn_box)

        # Wire signals
        self._edit_name.textChanged.connect(self._on_name_changed)
        self._edit_name.editingFinished.connect(self._validate_name)
        self._edit_id.editingFinished.connect(self._validate_id)

        # Initial save state
        self._update_save_button()

    # ------------------------------------------------------------------
    # Population
    # ------------------------------------------------------------------

    def _populate(self, data: dict[str, Any]) -> None:
        """Fill widgets from a group data dict."""
        self._edit_name.setText(str(data.get("name", "") or ""))
        self._edit_id.setText(str(data.get("id", "") or ""))

        color = str(data.get("color", "") or self._current_color)
        self._current_color = color
        self._update_color_swatch(color)

        members = list(data.get("members", []))
        self._group_panel.set_members(members)

        launch_order = data.get("launch_order", "sequential")
        if launch_order == "parallel":
            self._radio_parallel.setChecked(True)
        else:
            self._radio_sequential.setChecked(True)

        self._spin_launch_delay_ms.setValue(int(data.get("launch_delay_ms", 0) or 0))

        default_layout = data.get("default_layout_id") or ""
        idx = self._combo_default_layout.findData(default_layout)
        if idx >= 0:
            self._combo_default_layout.setCurrentIndex(idx)

        isolation = data.get("isolation", "shared")
        if isolation == "per-group":
            self._radio_per_group.setChecked(True)
        else:
            self._radio_shared.setChecked(True)

        conflict = data.get("layout_conflict", "move")
        idx = self._combo_layout_conflict.findData(conflict)
        if idx >= 0:
            self._combo_layout_conflict.setCurrentIndex(idx)

        self._chk_auto_attach.setChecked(bool(data.get("auto_attach", False)))

        self._validate_name()
        self._validate_id()

        # Populate associated layouts
        self._refresh_layouts_list(data.get("id") or "", data.get("default_layout_id"))

    # ------------------------------------------------------------------
    # Layouts section (Change 3)
    # ------------------------------------------------------------------

    def _refresh_layouts_list(self, group_id: str, default_layout_id: str | None) -> None:
        """Rebuild the layouts list widget from doc.screen_layouts associated with *group_id*."""
        self._layouts_list_widget.clear()
        if self._doc is None or not group_id:
            return

        associated: list[Any] = []
        for sl in self._doc.screen_layouts:
            # Include if it is the default_layout_id or follows the naming pattern
            if sl.id == default_layout_id or sl.id.startswith(f"{group_id}-"):
                associated.append(sl)

        for sl in associated:
            label = f"{sl.name}  [{sl.id}]"
            if sl.id == default_layout_id:
                label += "  (default)"

            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, sl.id)

            # Row widget with label + Rename + Delete buttons
            row_widget = QWidget()
            row_widget.setObjectName(f"row_layout_{sl.id}")
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(4, 0, 4, 0)
            row_layout.setSpacing(4)

            lbl = QLabel(label)
            lbl.setObjectName(f"lbl_layout_{sl.id}")
            lbl.setAccessibleName(f"Layout label {sl.id}")
            row_layout.addWidget(lbl)
            row_layout.addStretch()

            btn_rename = QPushButton("Rename")
            btn_rename.setObjectName(f"btn_rename_layout_{sl.id}")
            btn_rename.setAccessibleName(f"Rename layout {sl.id}")
            btn_rename.setAccessibleDescription(f"Rename layout {sl.name}")
            btn_rename.setFixedWidth(60)
            btn_rename.clicked.connect(
                lambda _checked=False, layout_id=sl.id, r_lbl=lbl, r_item=item: self._on_rename_layout(
                    layout_id, r_lbl, r_item
                )
            )
            row_layout.addWidget(btn_rename)

            btn_del = QPushButton("Delete")
            btn_del.setObjectName(f"btn_delete_layout_{sl.id}")
            btn_del.setAccessibleName(f"Delete layout {sl.id}")
            btn_del.setAccessibleDescription(f"Remove layout {sl.name} from the document")
            btn_del.setFixedWidth(60)
            btn_del.clicked.connect(
                lambda _checked=False, layout_id=sl.id: self._on_delete_layout(layout_id)
            )
            row_layout.addWidget(btn_del)

            self._layouts_list_widget.addItem(item)
            self._layouts_list_widget.setItemWidget(item, row_widget)

    def _on_delete_layout(self, layout_id: str) -> None:
        """Confirm and remove *layout_id* from the document."""
        if self._doc is None:
            return

        layout = next((sl for sl in self._doc.screen_layouts if sl.id == layout_id), None)
        if layout is None:
            return

        reply = QMessageBox.question(
            self,
            "Delete Layout",
            f'Delete layout "{layout.name}"?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Remove from doc
        self._doc.screen_layouts = [sl for sl in self._doc.screen_layouts if sl.id != layout_id]
        self._pending_layout_deletes.append(layout_id)

        # Clear default_layout_id in the combo if it matched
        default_layout = self._combo_default_layout.currentData() or ""
        if default_layout == layout_id:
            self._combo_default_layout.setCurrentIndex(0)

        # Rebuild the list from the current group id
        current_group_id = self._edit_id.text().strip()
        current_default = self._combo_default_layout.currentData() or None
        self._refresh_layouts_list(current_group_id, current_default)

    def _on_rename_layout(self, layout_id: str, lbl: QLabel, item: QListWidgetItem) -> None:
        """Replace the label text with an inline QLineEdit for renaming *layout_id*."""
        if self._doc is None:
            return

        layout = next((sl for sl in self._doc.screen_layouts if sl.id == layout_id), None)
        if layout is None:
            return

        # Build an inline editor inside a small widget that replaces the row widget
        row_widget = self._layouts_list_widget.itemWidget(item)
        if row_widget is None:
            return

        # Replace label with a QLineEdit pre-filled with current name
        edit = QLineEdit(layout.name)
        edit.setObjectName(f"edit_rename_layout_{layout_id}")
        edit.setAccessibleName(f"Rename layout {layout_id} inline editor")
        edit.selectAll()

        # Swap lbl → edit in the row layout
        row_layout_raw = row_widget.layout()
        if row_layout_raw is not None and hasattr(row_layout_raw, "insertWidget"):
            lbl.hide()
            row_layout_raw.insertWidget(0, edit)

        self._rename_edits[layout_id] = edit

        def _commit() -> None:
            new_name = edit.text().strip()
            if new_name and self._doc is not None:
                for i, sl in enumerate(self._doc.screen_layouts):
                    if sl.id == layout_id:
                        self._doc.screen_layouts[i] = sl.model_copy(update={"name": new_name})
                        break
                # Persist via save_callback if available
                if self._save_callback is not None:
                    import contextlib

                    with contextlib.suppress(Exception):
                        self._save_callback()
            # Rebuild list to show new name
            current_group_id = self._edit_id.text().strip()
            current_default = self._combo_default_layout.currentData() or None
            self._refresh_layouts_list(current_group_id, current_default)

        edit.editingFinished.connect(_commit)
        edit.returnPressed.connect(edit.clearFocus)
        edit.setFocus()

    def _update_id_from_name(self, name: str) -> None:
        """Auto-suggest slug from name when creating a new group."""
        if not self._is_new or self._edit_id.isReadOnly():
            return
        slug = name.lower()
        slug = re.sub(r"[^a-z0-9]+", "-", slug)
        slug = slug.strip("-")
        if slug and len(slug) >= 2:
            self._edit_id.setText(slug[:63])
        else:
            self._edit_id.setText("")

    def _update_color_swatch(self, color: str) -> None:
        """Update the color button background and value label."""
        self._btn_color.setStyleSheet(f"background-color: {color}; border: 1px solid #666;")
        self._lbl_color_value.setText(color)
        self._current_color = color

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_name(self) -> None:
        name = self._edit_name.text().strip()
        if not name:
            self._set_widget_error(self._edit_name, self._err_name, "Name is required")
        else:
            self._set_widget_error(self._edit_name, self._err_name, "")
        self._update_save_button()

    def _validate_id(self) -> None:
        gid = self._edit_id.text().strip()
        if not gid:
            self._set_widget_error(self._edit_id, self._err_id, "ID is required")
        elif not _ID_SLUG_RE.match(gid):
            self._set_widget_error(
                self._edit_id, self._err_id, "ID must match ^[a-z0-9][a-z0-9-]{1,62}$"
            )
        else:
            self._set_widget_error(self._edit_id, self._err_id, "")
        self._update_save_button()

    def _set_widget_error(self, widget: QWidget, err_label: QLabel, error: str) -> None:
        if error:
            widget.setStyleSheet("border: 1px solid red;")
            err_label.setText(error)
            err_label.setVisible(True)
        else:
            widget.setStyleSheet("")
            err_label.setText("")
            err_label.setVisible(False)

    def _has_errors(self) -> bool:
        return bool(self._err_name.text()) or bool(self._err_id.text())

    def _update_save_button(self) -> None:
        name_ok = bool(self._edit_name.text().strip())
        id_ok = bool(self._edit_id.text().strip())
        can_save = name_ok and id_ok and not self._has_errors()
        self._btn_save.setEnabled(can_save)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_name_changed(self, text: str) -> None:
        self._update_id_from_name(text)
        self._validate_name()

    def _on_color_clicked(self) -> None:
        """Open a QColorDialog and update the swatch."""
        initial = QColor(self._current_color)
        if self._color_dialog_factory is not None:
            # Injected for tests
            chosen = self._color_dialog_factory(initial)
        else:
            chosen = QColorDialog.getColor(initial, self, "Pick Group Color")
        if chosen.isValid():
            self._update_color_swatch(chosen.name())

    def _on_save(self) -> None:
        self._validate_name()
        self._validate_id()
        if self._has_errors():
            return
        self.accept()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_group_data(self) -> dict[str, Any]:
        """Collect and return group data as a dict."""
        launch_order = "parallel" if self._radio_parallel.isChecked() else "sequential"
        isolation = "per-group" if self._radio_per_group.isChecked() else "shared"
        layout_conflict = self._combo_layout_conflict.currentData() or "move"
        default_layout = self._combo_default_layout.currentData() or None

        return {
            "id": self._edit_id.text().strip(),
            "name": self._edit_name.text().strip(),
            "color": self._current_color,
            "members": self._group_panel.get_members(),
            "launch_order": launch_order,
            "launch_delay_ms": self._spin_launch_delay_ms.value(),
            "default_layout_id": default_layout or None,
            "isolation": isolation,
            "layout_conflict": layout_conflict,
            "auto_attach": self._chk_auto_attach.isChecked(),
        }

    def get_group_model(self) -> Group:
        """Return a validated Group pydantic model from current form state."""
        data = self.get_group_data()
        return Group.model_validate(data)
