# -*- coding: utf-8 -*-
"""
cpsm.ui.dialogs.import_preview — Import Preview dialog.

Spec sections: §4.3, §4.4

Shows every ImportTransform from an ImportPreview with per-row Skip checkboxes
and editable target-path ids for ``connections[<id>]`` rows.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from cpsm.data.importer import ImportPreview, ImportTransform
from cpsm.data.schema import CpsmDocument

__all__ = ["ImportPreviewDialog"]

# ---------------------------------------------------------------------------
# Colour map for transform kinds
# ---------------------------------------------------------------------------
_KIND_COLOURS: dict[str, QColor] = {
    "added": QColor("#d4edda"),  # light green
    "renamed": QColor("#fff3cd"),  # light amber
    "skipped": QColor("#f8d7da"),  # light red
    "warning": QColor("#ffe8a1"),  # yellow
    "synthesized": QColor("#cce5ff"),  # light blue
}

_COLUMNS = ("Skip", "Kind", "Target Path", "Detail")
_COL_SKIP = 0
_COL_KIND = 1
_COL_TARGET = 2
_COL_DETAIL = 3


class _TransformTableModel(QAbstractTableModel):
    """Table model for ImportPreview transforms.

    Column 0 — Skip checkbox (checkable, user-editable)
    Column 1 — Kind (read-only display)
    Column 2 — Target Path (editable only for connections[<id>] rows)
    Column 3 — Detail (read-only display)
    """

    def __init__(
        self,
        transforms: list[ImportTransform],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # Working copies so we don't mutate the original preview
        self._transforms: list[ImportTransform] = list(transforms)
        self._skipped: list[bool] = [False] * len(transforms)
        # Editable target paths (None means use the original)
        self._edited_targets: list[str | None] = [None] * len(transforms)

    # ------------------------------------------------------------------
    # QAbstractTableModel interface
    # ------------------------------------------------------------------

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return len(self._transforms)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return len(_COLUMNS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _COLUMNS[section]
        return None

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        transform = self._transforms[row]

        if role == Qt.ItemDataRole.CheckStateRole and col == _COL_SKIP:
            return Qt.CheckState.Checked if self._skipped[row] else Qt.CheckState.Unchecked

        if role == Qt.ItemDataRole.DisplayRole:
            if col == _COL_KIND:
                return transform.kind
            if col == _COL_TARGET:
                edited = self._edited_targets[row]
                return edited if edited is not None else transform.target_path
            if col == _COL_DETAIL:
                return transform.detail
            return None  # Skip column uses checkstate

        if role == Qt.ItemDataRole.BackgroundRole:
            return _KIND_COLOURS.get(transform.kind)

        if role == Qt.ItemDataRole.ToolTipRole and col == _COL_SKIP:
            return "Check to exclude this item from the import"

        return None

    def setData(
        self,
        index: QModelIndex | QPersistentModelIndex,
        value: Any,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if not index.isValid():
            return False
        row, col = index.row(), index.column()

        if col == _COL_SKIP and role == Qt.ItemDataRole.CheckStateRole:
            self._skipped[row] = value == Qt.CheckState.Checked
            self.dataChanged.emit(index, index, [role])
            return True

        if col == _COL_TARGET and role == Qt.ItemDataRole.EditRole:
            transform = self._transforms[row]
            if self._is_conn_row(transform):
                self._edited_targets[row] = str(value) if value else None
                self.dataChanged.emit(index, index, [role])
                return True

        return False

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        row, col = index.row(), index.column()
        if col == _COL_SKIP:
            return base | Qt.ItemFlag.ItemIsUserCheckable
        if col == _COL_TARGET and self._is_conn_row(self._transforms[row]):
            return base | Qt.ItemFlag.ItemIsEditable
        return base

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_conn_row(transform: ImportTransform) -> bool:
        """Return True if this row represents a connections[<id>] entry."""
        return transform.target_path.startswith("connections[") and transform.kind in (
            "added",
            "renamed",
        )

    def active_rows(self) -> list[tuple[int, ImportTransform, str | None]]:
        """Return (row_index, transform, edited_target) for non-skipped rows."""
        result = []
        for i, t in enumerate(self._transforms):
            if not self._skipped[i]:
                result.append((i, t, self._edited_targets[i]))
        return result

    def has_duplicate_ids(self) -> bool:
        """Return True if any two non-skipped connection rows share an edited id."""
        ids: list[str] = []
        for i, t in enumerate(self._transforms):
            if self._skipped[i]:
                continue
            if self._is_conn_row(t):
                edited = self._edited_targets[i]
                ids.append(edited if edited is not None else t.target_path)
        return len(ids) != len(set(ids))

    def get_effective_conn_ids(self) -> list[tuple[int, str]]:
        """Return (transform_index, effective_id) for non-skipped connection rows."""
        result: list[tuple[int, str]] = []
        for i, t in enumerate(self._transforms):
            if self._skipped[i]:
                continue
            if self._is_conn_row(t):
                edited = self._edited_targets[i]
                raw_id = edited if edited is not None else t.target_path
                # Strip "connections[" and "]" wrapper
                if raw_id.startswith("connections[") and raw_id.endswith("]"):
                    conn_id = raw_id[len("connections[") : -1]
                else:
                    conn_id = raw_id
                result.append((i, conn_id))
        return result


class ImportPreviewDialog(QDialog):
    """Shows ImportPreview with per-row controls.

    Object name: ``dlg_import_preview`` (§3.1 stable objectName convention).
    """

    def __init__(
        self,
        preview: ImportPreview,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self._preview = preview
        self._filtered: ImportPreview | None = None

        self.setObjectName("dlg_import_preview")
        self.setAccessibleName("Import Preview Dialog")
        self.setAccessibleDescription(
            "Dialog showing all import transformations with skip options and editable IDs"
        )
        self.setWindowTitle("Import Preview")
        self.setMinimumWidth(760)
        self.setMinimumHeight(480)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(16, 16, 16, 12)

        # Source path label
        lbl_source = QLabel(f"Importing from: {preview.source_path}")
        lbl_source.setObjectName("lbl_source")
        lbl_source.setAccessibleName("Source Path Label")
        lbl_source.setAccessibleDescription("Path of the legacy file being imported")
        lbl_source.setWordWrap(True)
        layout.addWidget(lbl_source)

        # Transform table
        self._model = _TransformTableModel(preview.transforms, self)
        self._model.dataChanged.connect(self._on_model_changed)

        table = QTableView()
        table.setObjectName("table_transforms")
        table.setAccessibleName("Transforms Table")
        table.setAccessibleDescription(
            "Table listing every import transformation with skip checkboxes and editable IDs"
        )
        table.setModel(self._model)
        table.horizontalHeader().setStretchLastSection(True)
        table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(False)
        table.resizeColumnsToContents()
        table.setColumnWidth(_COL_SKIP, 50)
        table.setColumnWidth(_COL_KIND, 100)
        table.setColumnWidth(_COL_TARGET, 220)
        layout.addWidget(table)
        self._table = table

        # Summary label
        lbl_summary = QLabel(self._make_summary(preview))
        lbl_summary.setObjectName("lbl_summary")
        lbl_summary.setAccessibleName("Import Summary Label")
        lbl_summary.setAccessibleDescription("Summary statistics for the import transformations")
        lbl_summary.setWordWrap(True)
        layout.addWidget(lbl_summary)

        # OK / Cancel buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btn_ok = button_box.button(QDialogButtonBox.StandardButton.Ok)
        assert btn_ok is not None
        btn_ok.setObjectName("btn_ok")
        btn_ok.setAccessibleName("OK Button")
        btn_ok.setAccessibleDescription("Accept the import with current skip/edit selections")

        btn_cancel = button_box.button(QDialogButtonBox.StandardButton.Cancel)
        assert btn_cancel is not None
        btn_cancel.setObjectName("btn_cancel")
        btn_cancel.setAccessibleName("Cancel Button")
        btn_cancel.setAccessibleDescription("Cancel and discard the import")

        button_box.accepted.connect(self._on_ok)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        self._btn_ok = btn_ok
        self._lbl_summary = lbl_summary
        self._update_ok_state()

    # ------------------------------------------------------------------
    # Public property
    # ------------------------------------------------------------------

    @property
    def filtered_preview(self) -> ImportPreview:
        """Return a new ImportPreview with skipped rows removed and edited ids applied."""
        if self._filtered is None:
            self._filtered = self._build_filtered_preview()
        return self._filtered

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_model_changed(self) -> None:
        self._filtered = None  # invalidate cached preview
        self._update_ok_state()

    def _on_ok(self) -> None:
        self._filtered = self._build_filtered_preview()
        self.accept()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _update_ok_state(self) -> None:
        has_dup = self._model.has_duplicate_ids()
        self._btn_ok.setEnabled(not has_dup)
        if has_dup:
            self._btn_ok.setToolTip(
                "Cannot import: two or more connection rows have the same target id. "
                "Edit the Target Path cells to make each id unique."
            )
        else:
            self._btn_ok.setToolTip("")

    def _build_filtered_preview(self) -> ImportPreview:
        """Construct a filtered ImportPreview respecting skip flags and edited ids."""
        active = self._model.active_rows()
        active_transforms = [t for _, t, _ in active]

        # Build new id mapping: original conn_id -> new conn_id (from edits)
        conn_id_remap: dict[str, str] = {}
        for _row_idx, transform, edited_target in active:
            if _TransformTableModel._is_conn_row(transform):
                orig_path = transform.target_path  # "connections[<id>]"
                if orig_path.startswith("connections[") and orig_path.endswith("]"):
                    orig_id = orig_path[len("connections[") : -1]
                else:
                    orig_id = orig_path
                if edited_target is not None:
                    # edited_target is the full "connections[<newid>]" or bare id
                    if edited_target.startswith("connections[") and edited_target.endswith("]"):
                        new_id = edited_target[len("connections[") : -1]
                    else:
                        new_id = edited_target
                    conn_id_remap[orig_id] = new_id
                else:
                    conn_id_remap[orig_id] = orig_id

        # Determine which connection ids are active (not skipped)
        active_conn_ids = set(conn_id_remap.keys())

        # Deep-copy and filter the document
        orig_doc = self._preview.document

        # Filter connections to only active ones, applying id remap
        new_connections = []
        for conn in orig_doc.connections:
            if conn.id in active_conn_ids:
                if conn.id in conn_id_remap and conn_id_remap[conn.id] != conn.id:
                    # Rebuild with new id — we use model_copy for pydantic v2
                    try:
                        new_conn = conn.model_copy(update={"id": conn_id_remap[conn.id]})
                    except Exception:
                        new_conn = conn
                    new_connections.append(new_conn)
                else:
                    new_connections.append(conn)

        # If no edits were made, keep all connections (handles case where
        # transforms don't cover all connections, e.g. if transform list
        # has skipped/warning rows but connections were added)
        if not conn_id_remap:
            new_connections = list(orig_doc.connections)

        # Build filtered document
        new_doc = CpsmDocument(
            schema_version=orig_doc.schema_version,
            settings=orig_doc.settings,
            ssh_keys=list(orig_doc.ssh_keys),
            connections=new_connections,
            groups=list(orig_doc.groups),
            screen_layouts=list(orig_doc.screen_layouts),
            scenes=list(orig_doc.scenes),
            launch_templates=list(orig_doc.launch_templates),
        )

        return ImportPreview(
            source_path=self._preview.source_path,
            transforms=active_transforms,
            document=new_doc,
        )

    @staticmethod
    def _make_summary(preview: ImportPreview) -> str:
        """Build a human-readable summary string from the preview."""
        from collections import Counter

        counts = Counter(t.kind for t in preview.transforms)
        parts: list[str] = []

        # Count connections and groups in the resulting document
        n_conn = len(preview.document.connections)
        n_groups = len(preview.document.groups)
        n_keys = len(preview.document.ssh_keys)

        if n_conn:
            parts.append(f"{n_conn} connection{'s' if n_conn != 1 else ''}")
        if n_groups:
            parts.append(f"{n_groups} group{'s' if n_groups != 1 else ''} synthesized")
        if n_keys:
            synth_key_ids = [t.target_path for t in preview.transforms if t.kind == "synthesized"]
            if synth_key_ids:
                parts.append(f"{len(synth_key_ids)} placeholder key(s)")
        if counts.get("renamed"):
            parts.append(f"{counts['renamed']} renamed")
        if counts.get("skipped"):
            parts.append(f"{counts['skipped']} skipped")
        if counts.get("warning"):
            parts.append(f"{counts['warning']} warning(s)")

        return ", ".join(parts) if parts else "No transformations"
