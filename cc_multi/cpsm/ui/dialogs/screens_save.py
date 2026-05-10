# -*- coding: utf-8 -*-
"""
cpsm.ui.dialogs.screens_save — "Save Layout…" dialog for the Screens tab.

Change 5 of UX redesign.

Three save modes
----------------
1. Overwrite — replace the currently selected layout (Preview mode only, layout selected).
2. New layout for group — append a new layout and set the chosen group's default_layout_id.
3. New group + layout — create a brand-new Group and a new ScreenLayout together.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from cpsm.data.schema import CpsmDocument, ScreenLayout

__all__ = ["ScreensSaveDialog"]


class ScreensSaveDialog(QDialog):
    """Dialog offering three save paths for the Screens tab canvas layout."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        doc: CpsmDocument,
        is_preview_mode: bool = False,
        current_layout_id: str | None = None,
        current_layout_name: str | None = None,
        canvas_layout: ScreenLayout | None = None,
    ) -> None:
        super().__init__(parent)

        self._doc = doc
        self._is_preview_mode = is_preview_mode
        self._current_layout_id = current_layout_id
        self._current_layout_name = current_layout_name
        self._canvas_layout = canvas_layout

        self.setObjectName("dlg_screens_save")
        self.setAccessibleName("Save Layout Dialog")
        self.setAccessibleDescription("Dialog to save the current screen layout")
        self.setWindowTitle("Save Layout…")
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setMinimumWidth(480)

        self._build_ui()
        self._update_save_button()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(16, 16, 16, 16)

        self._mode_group = QButtonGroup(self)
        self._mode_group.setObjectName("radio_group_save_mode")

        # ---- Option 1: Overwrite ----
        grp_overwrite = QGroupBox()
        grp_overwrite.setObjectName("grp_save_overwrite")
        grp_overwrite.setAccessibleName("Overwrite layout group box")
        overwrite_layout = QHBoxLayout(grp_overwrite)

        self._radio_overwrite = QRadioButton()
        self._radio_overwrite.setObjectName("radio_save_overwrite")
        self._radio_overwrite.setAccessibleName("Overwrite current layout radio")
        overwrite_layout.addWidget(self._radio_overwrite)

        overwrite_name = self._current_layout_name or "(no layout selected)"
        lbl_overwrite = QLabel(f"Overwrite <b>{overwrite_name}</b>")
        lbl_overwrite.setObjectName("lbl_save_overwrite")
        lbl_overwrite.setAccessibleName("Overwrite layout label")
        overwrite_layout.addWidget(lbl_overwrite)
        overwrite_layout.addStretch()

        # Only enabled in Preview mode with a layout selected
        can_overwrite = self._is_preview_mode and bool(self._current_layout_id)
        self._radio_overwrite.setEnabled(can_overwrite)
        if not can_overwrite:
            lbl_overwrite.setEnabled(False)

        self._mode_group.addButton(self._radio_overwrite)
        root.addWidget(grp_overwrite)

        # ---- Option 2: New layout for group ----
        grp_new_layout = QGroupBox()
        grp_new_layout.setObjectName("grp_save_new_layout")
        grp_new_layout.setAccessibleName("New layout for group group box")
        new_layout_vlayout = QVBoxLayout(grp_new_layout)

        row2 = QHBoxLayout()
        self._radio_new_layout = QRadioButton()
        self._radio_new_layout.setObjectName("radio_save_new_layout")
        self._radio_new_layout.setAccessibleName("Save as new layout radio")
        row2.addWidget(self._radio_new_layout)

        lbl_new_layout = QLabel("Save as new layout for group")
        lbl_new_layout.setObjectName("lbl_save_new_layout")
        lbl_new_layout.setAccessibleName("New layout label")
        row2.addWidget(lbl_new_layout)
        row2.addStretch()
        new_layout_vlayout.addLayout(row2)

        form2 = QFormLayout()

        self._combo_new_layout_group = QComboBox()
        self._combo_new_layout_group.setObjectName("combo_save_new_layout_group")
        self._combo_new_layout_group.setAccessibleName("Group picker for new layout")
        self._combo_new_layout_group.setAccessibleDescription(
            "Select the group to assign the new layout to"
        )
        for grp in self._doc.groups:
            self._combo_new_layout_group.addItem(grp.name or grp.id, grp.id)
        form2.addRow("Group:", self._combo_new_layout_group)

        self._edit_new_layout_name = QLineEdit()
        self._edit_new_layout_name.setObjectName("edit_save_new_layout_name")
        self._edit_new_layout_name.setAccessibleName("New layout name field")
        self._edit_new_layout_name.setPlaceholderText("Layout name (optional)")
        form2.addRow("Name:", self._edit_new_layout_name)

        new_layout_vlayout.addLayout(form2)

        has_groups = len(self._doc.groups) > 0
        self._radio_new_layout.setEnabled(has_groups)
        if not has_groups:
            self._combo_new_layout_group.setEnabled(False)
            self._edit_new_layout_name.setEnabled(False)

        self._mode_group.addButton(self._radio_new_layout)
        root.addWidget(grp_new_layout)

        # ---- Option 3: New group + new layout ----
        grp_new_group = QGroupBox()
        grp_new_group.setObjectName("grp_save_new_group")
        grp_new_group.setAccessibleName("New group and layout group box")
        new_group_vlayout = QVBoxLayout(grp_new_group)

        row3 = QHBoxLayout()
        self._radio_new_group = QRadioButton()
        self._radio_new_group.setObjectName("radio_save_new_group")
        self._radio_new_group.setAccessibleName("Create new group and layout radio")
        row3.addWidget(self._radio_new_group)

        lbl_new_group = QLabel("Create new group + new layout")
        lbl_new_group.setObjectName("lbl_save_new_group")
        lbl_new_group.setAccessibleName("New group label")
        row3.addWidget(lbl_new_group)
        row3.addStretch()
        new_group_vlayout.addLayout(row3)

        form3 = QFormLayout()

        self._edit_new_group_name = QLineEdit()
        self._edit_new_group_name.setObjectName("edit_save_new_group_name")
        self._edit_new_group_name.setAccessibleName("New group name field")
        self._edit_new_group_name.setPlaceholderText("New group name")
        form3.addRow("Group name:", self._edit_new_group_name)

        self._edit_new_group_layout_name = QLineEdit()
        self._edit_new_group_layout_name.setObjectName("edit_save_new_group_layout_name")
        self._edit_new_group_layout_name.setAccessibleName("New group layout name field")
        self._edit_new_group_layout_name.setPlaceholderText("Layout name (optional)")
        form3.addRow("Layout name:", self._edit_new_group_layout_name)

        new_group_vlayout.addLayout(form3)
        self._mode_group.addButton(self._radio_new_group)
        root.addWidget(grp_new_group)

        # ---- Bottom buttons ----
        self._btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self._btn_box.setObjectName("btn_box_screens_save")

        self._btn_save = self._btn_box.button(QDialogButtonBox.StandardButton.Save)
        assert self._btn_save is not None
        self._btn_save.setObjectName("btn_save_screens")
        self._btn_save.setAccessibleName("Save button")
        self._btn_save.setAccessibleDescription("Save the layout with the selected option")

        btn_cancel = self._btn_box.button(QDialogButtonBox.StandardButton.Cancel)
        assert btn_cancel is not None
        btn_cancel.setObjectName("btn_cancel_screens_save")
        btn_cancel.setAccessibleName("Cancel button")

        self._btn_box.accepted.connect(self._on_save)
        self._btn_box.rejected.connect(self.reject)
        root.addWidget(self._btn_box)

        # Wire signals
        self._mode_group.buttonToggled.connect(lambda _btn, _chk: self._update_save_button())
        self._edit_new_group_name.textChanged.connect(self._update_save_button)
        self._edit_new_layout_name.textChanged.connect(self._update_save_button)

    # ------------------------------------------------------------------
    # Validation / state
    # ------------------------------------------------------------------

    def _update_save_button(self) -> None:
        """Enable Save only when an option is selected and required fields are filled."""
        can_save = False
        if self._radio_overwrite.isChecked() and self._radio_overwrite.isEnabled():
            can_save = True
        elif self._radio_new_layout.isChecked() and self._radio_new_layout.isEnabled():
            can_save = self._combo_new_layout_group.count() > 0
        elif self._radio_new_group.isChecked():
            can_save = bool(self._edit_new_group_name.text().strip())

        self._btn_save.setEnabled(can_save)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_save(self) -> None:
        self._update_save_button()
        if not self._btn_save.isEnabled():
            return
        self.accept()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_result(self) -> dict[str, Any]:
        """Return the save options as a dict.

        Keys
        ----
        mode : ``"overwrite"`` | ``"new_layout"`` | ``"new_group"``
        layout_id : str (mode=overwrite only)
        group_id : str (mode=new_layout only)
        layout_name : str (mode=new_layout or new_group)
        group_name : str (mode=new_group only)
        """
        if self._radio_overwrite.isChecked():
            return {
                "mode": "overwrite",
                "layout_id": self._current_layout_id,
            }
        if self._radio_new_layout.isChecked():
            return {
                "mode": "new_layout",
                "group_id": self._combo_new_layout_group.currentData(),
                "layout_name": self._edit_new_layout_name.text().strip() or "New Layout",
            }
        # new_group
        return {
            "mode": "new_group",
            "group_name": self._edit_new_group_name.text().strip(),
            "layout_name": self._edit_new_group_layout_name.text().strip()
            or f"{self._edit_new_group_name.text().strip()} default",
        }
