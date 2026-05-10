# -*- coding: utf-8 -*-
"""
cpsm.ui.dialogs.reimport_merge — Re-import three-way merge dialog.

Spec section: §4.4

Presents a three-column comparison: Existing connection (from ``existing``
CpsmDocument), Source proposal (from ``source_preview``), and a Decision
combo-box per row.

The ``merge_decisions`` property exposes per-row decisions keyed by
connection id.  The caller (Phase 14+ main_window code) passes these to
``ImportService.import_legacy_to`` to perform the actual merge.
"""

from __future__ import annotations

from typing import Any, Literal

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from cpsm.data.importer import ImportPreview
from cpsm.data.schema import CpsmDocument

__all__ = ["ReimportMergeDialog"]

MergeDecision = Literal["add", "update", "skip"]

_DECISIONS: list[MergeDecision] = ["add", "update", "skip"]
_DECISION_LABELS = {"add": "Add", "update": "Update", "skip": "Skip"}

# Column indices
_COL_EXISTING = 0
_COL_SOURCE = 1
_COL_DECISION = 2

_COLUMNS = ("Existing", "Source (proposed)", "Decision")

# Colour for conflict rows
_CONFLICT_COLOUR = QColor("#fff3cd")  # amber / yellow


def _conn_summary(conn: Any) -> str:
    """Return a short summary string for a connection."""
    parts: list[str] = []
    parts.append(conn.id)
    if getattr(conn, "name", None):
        parts.append(conn.name)
    if getattr(conn, "host", None):
        parts.append(conn.host)
    return " | ".join(parts)


def _conn_fields(conn: Any) -> dict[str, Any]:
    """Return a flat dict of displayable fields for diffing."""
    return {
        "name": getattr(conn, "name", None),
        "host": getattr(conn, "host", None),
        "port": getattr(conn, "port", None),
        "user": getattr(conn, "user", None),
        "sudo_user": getattr(conn, "sudo_user", None),
        "project_folder": getattr(conn, "project_folder", None),
        "claude_options": getattr(conn, "claude_options", None),
        "launch_profile": getattr(conn, "launch_profile", None),
    }


def _diff_fields(existing: Any, source: Any) -> list[str]:
    """Return list of field names that differ between two connections."""
    e = _conn_fields(existing)
    s = _conn_fields(source)
    return [k for k in e if e[k] != s.get(k)]


class _DecisionDelegate(QStyledItemDelegate):
    """Renders and edits the Decision column using a QComboBox."""

    def createEditor(
        self,
        parent: QWidget,
        option: Any,  # QStyleOptionViewItem
        index: QModelIndex | QPersistentModelIndex,
    ) -> QWidget:
        combo = QComboBox(parent)
        combo.setObjectName(f"combo_decision_{index.row()}")
        combo.setAccessibleName(f"Decision combo for row {index.row()}")
        combo.setAccessibleDescription("Select Add, Update, or Skip for this connection row")
        for d in _DECISIONS:
            combo.addItem(_DECISION_LABELS[d], d)
        return combo

    def setEditorData(
        self,
        editor: QWidget,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        value = index.data(Qt.ItemDataRole.EditRole)
        assert isinstance(editor, QComboBox)
        combo = editor
        idx = next((i for i, d in enumerate(_DECISIONS) if d == value), 0)
        combo.setCurrentIndex(idx)

    def setModelData(
        self,
        editor: QWidget,
        model: Any,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        assert isinstance(editor, QComboBox)
        combo = editor
        decision: MergeDecision = combo.currentData()
        model.setData(index, decision, Qt.ItemDataRole.EditRole)

    def displayText(self, value: Any, locale: Any) -> str:
        if isinstance(value, str) and value in _DECISION_LABELS:
            return _DECISION_LABELS[value]
        return super().displayText(value, locale)


class _MergeTableModel(QAbstractTableModel):
    """Three-column model: Existing | Source | Decision.

    Each row corresponds to a connection that appears in either the
    existing document or the source preview (or both).
    """

    def __init__(
        self,
        existing: CpsmDocument,
        source_preview: ImportPreview,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        # Index existing connections by id
        existing_by_id = {c.id: c for c in existing.connections}
        # Source connections from the preview document
        source_by_id = {c.id: c for c in source_preview.document.connections}

        # Union of all ids, source order first, then any existing-only
        all_ids: list[str] = []
        seen: set[str] = set()
        for c in source_preview.document.connections:
            all_ids.append(c.id)
            seen.add(c.id)
        for c in existing.connections:
            if c.id not in seen:
                all_ids.append(c.id)
                seen.add(c.id)

        self._rows: list[dict[str, Any]] = []
        for conn_id in all_ids:
            ex = existing_by_id.get(conn_id)
            src = source_by_id.get(conn_id)

            # Determine default decision
            if ex is None and src is not None:
                decision: MergeDecision = "add"
                conflict = False
                diff: list[str] = []
            elif ex is not None and src is None:
                decision = "skip"
                conflict = False
                diff = []
            elif ex is not None and src is not None:
                diff = _diff_fields(ex, src)
                if diff:
                    decision = "update"
                    conflict = True
                else:
                    decision = "skip"
                    conflict = False
            else:
                # Should not happen
                decision = "skip"
                conflict = False
                diff = []

            self._rows.append(
                {
                    "id": conn_id,
                    "existing": ex,
                    "source": src,
                    "decision": decision,
                    "conflict": conflict,
                    "diff_fields": diff,
                }
            )

    # ------------------------------------------------------------------
    # QAbstractTableModel interface
    # ------------------------------------------------------------------

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return len(self._rows)

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
        row_data = self._rows[index.row()]
        col = index.column()

        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if col == _COL_EXISTING:
                ex = row_data["existing"]
                return _conn_summary(ex) if ex is not None else ""
            if col == _COL_SOURCE:
                src = row_data["source"]
                return _conn_summary(src) if src is not None else ""
            if col == _COL_DECISION:
                return row_data["decision"]

        if role == Qt.ItemDataRole.BackgroundRole and row_data["conflict"]:
            return _CONFLICT_COLOUR

        if role == Qt.ItemDataRole.ToolTipRole and row_data["conflict"]:
            diff_str = ", ".join(row_data["diff_fields"]) or "none"
            return f"Conflict: differing fields — {diff_str}"

        return None

    def setData(
        self,
        index: QModelIndex | QPersistentModelIndex,
        value: Any,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if not index.isValid():
            return False
        if (
            index.column() == _COL_DECISION
            and role == Qt.ItemDataRole.EditRole
            and value in _DECISIONS
        ):
            self._rows[index.row()]["decision"] = value
            self.dataChanged.emit(index, index, [role])
            return True
        return False

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == _COL_DECISION:
            return base | Qt.ItemFlag.ItemIsEditable
        return base

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def get_decisions(self) -> dict[str, MergeDecision]:
        """Return {conn_id: decision} for all rows."""
        return {r["id"]: r["decision"] for r in self._rows}

    def is_conflict_row(self, row: int) -> bool:
        return bool(self._rows[row]["conflict"])

    def diff_fields_for_row(self, row: int) -> list[str]:
        return list(self._rows[row].get("diff_fields", []))


class ReimportMergeDialog(QDialog):
    """Three-way merge: existing / source / proposed.

    Object name: ``dlg_reimport_merge`` (§3.1 stable objectName convention).
    """

    def __init__(
        self,
        existing: CpsmDocument,
        source_preview: ImportPreview,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self.setObjectName("dlg_reimport_merge")
        self.setAccessibleName("Re-Import Merge Dialog")
        self.setAccessibleDescription(
            "Three-way merge dialog comparing existing config against proposed import"
        )
        self.setWindowTitle("Re-Import Merge")
        self.setMinimumWidth(800)
        self.setMinimumHeight(480)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(16, 16, 16, 12)

        lbl_info = QLabel(
            f"Merging from: {source_preview.source_path}\n"
            "Review each connection and choose Add, Update, or Skip."
        )
        lbl_info.setObjectName("lbl_merge_info")
        lbl_info.setAccessibleName("Merge Info Label")
        lbl_info.setAccessibleDescription("Source path and instructions for the merge dialog")
        lbl_info.setWordWrap(True)
        layout.addWidget(lbl_info)

        self._model = _MergeTableModel(existing, source_preview, self)

        table = QTableView()
        table.setObjectName("table_merge")
        table.setAccessibleName("Merge Table")
        table.setAccessibleDescription(
            "Three-column table showing existing, source, and decision per connection"
        )
        table.setModel(self._model)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        table.setItemDelegateForColumn(_COL_DECISION, _DecisionDelegate(table))
        # Open persistent editors so combos are always visible
        for row in range(self._model.rowCount()):
            idx = self._model.index(row, _COL_DECISION)
            table.openPersistentEditor(idx)
        layout.addWidget(table)
        self._table = table

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btn_ok = button_box.button(QDialogButtonBox.StandardButton.Ok)
        assert btn_ok is not None
        btn_ok.setObjectName("btn_ok")
        btn_ok.setAccessibleName("OK Button")
        btn_ok.setAccessibleDescription("Accept the merge decisions and close the dialog")

        btn_cancel = button_box.button(QDialogButtonBox.StandardButton.Cancel)
        assert btn_cancel is not None
        btn_cancel.setObjectName("btn_cancel")
        btn_cancel.setAccessibleName("Cancel Button")
        btn_cancel.setAccessibleDescription("Cancel and discard all merge decisions")

        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    # ------------------------------------------------------------------
    # Public property
    # ------------------------------------------------------------------

    @property
    def merge_decisions(self) -> dict[str, MergeDecision]:
        """Per-row decision keyed by connection id."""
        return self._model.get_decisions()
