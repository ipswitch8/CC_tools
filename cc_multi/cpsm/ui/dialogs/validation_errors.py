# -*- coding: utf-8 -*-
"""
cpsm.ui.dialogs.validation_errors — Validation Errors dialog.

Spec: §4.10

Displays a list of ValidationIssue objects grouped by their top-level
location segment.  Each issue has an icon (error/warning), path, message,
and an "Open in editor" button that emits the open_requested(path) signal.

The title bar shows a count of errors and warnings.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cpsm.services.config_service import ValidationIssue

__all__ = ["ValidationErrorsDialog"]

# ---------------------------------------------------------------------------
# Top-level grouping keys per §4.10 (in display order)
# ---------------------------------------------------------------------------

_TOP_LEVEL_ORDER = (
    "settings",
    "ssh_keys",
    "connections",
    "groups",
    "screen_layouts",
    "scenes",
    "launch_templates",
)

_SEVERITY_ICONS = {
    "error": "✖",
    "warning": "⚠",
}

_SEVERITY_COLORS = {
    "error": "#ef4444",
    "warning": "#f59e0b",
}


def _top_level_key(location: str) -> str:
    """Extract the top-level segment from a dotted *location* string."""
    return location.split(".")[0] if location else "(root)"


def _sort_key(location: str) -> int:
    """Sort key that puts known top-level sections first, others last."""
    top = _top_level_key(location)
    try:
        return _TOP_LEVEL_ORDER.index(top)
    except ValueError:
        return len(_TOP_LEVEL_ORDER)


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------


class ValidationErrorsDialog(QDialog):
    """Dialog that displays validation issues grouped by top-level location.

    Signals
    -------
    open_requested(path: str)
        Emitted when the user clicks "Open in editor" for an issue row.
        *path* is the ``location`` field of the corresponding
        :class:`~cpsm.services.config_service.ValidationIssue`.
    """

    open_requested: Signal = Signal(str)

    def __init__(
        self,
        issues: list[ValidationIssue],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._issues = list(issues)

        self.setObjectName("dlg_validation_errors")
        self.setAccessibleName("Validation Errors Dialog")
        self.setModal(True)
        self.resize(720, 480)

        self._setup_ui()
        self._populate()

    # ------------------------------------------------------------------
    # UI setup
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        self._tree = QTreeWidget(self)
        self._tree.setObjectName("tree_issues")
        self._tree.setAccessibleName("Validation Issues Tree")
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(["", "Location", "Message"])
        self._tree.setAlternatingRowColors(True)
        self._tree.setAnimated(True)
        self._tree.header().setStretchLastSection(True)
        layout.addWidget(self._tree)

        # Close button
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        button_box.setObjectName("btn_box_close")
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    # ------------------------------------------------------------------
    # Population
    # ------------------------------------------------------------------

    def _populate(self) -> None:
        """Build the tree from self._issues and update the window title."""
        self._tree.clear()

        errors = sum(1 for i in self._issues if i.severity == "error")
        warnings = sum(1 for i in self._issues if i.severity == "warning")

        parts: list[str] = []
        if errors:
            parts.append(f"{errors} error{'s' if errors != 1 else ''}")
        if warnings:
            parts.append(f"{warnings} warning{'s' if warnings != 1 else ''}")
        title = "Validation: " + (", ".join(parts) if parts else "No issues")
        self.setWindowTitle(title)

        if not self._issues:
            return

        # Group issues by top-level key
        groups: dict[str, list[ValidationIssue]] = {}
        for issue in self._issues:
            top = _top_level_key(issue.location)
            groups.setdefault(top, []).append(issue)

        # Emit groups in canonical order, then any extras alphabetically
        ordered_tops: list[str] = []
        for key in _TOP_LEVEL_ORDER:
            if key in groups:
                ordered_tops.append(key)
        for key in sorted(groups):
            if key not in ordered_tops:
                ordered_tops.append(key)

        for top in ordered_tops:
            group_item = QTreeWidgetItem(self._tree, [top, "", ""])
            font = group_item.font(0)
            font.setBold(True)
            group_item.setFont(0, font)
            group_item.setExpanded(True)

            for issue in groups[top]:
                self._add_issue_row(group_item, issue)

        # Resize location column to content
        self._tree.resizeColumnToContents(0)
        self._tree.resizeColumnToContents(1)

    def _add_issue_row(
        self,
        parent: QTreeWidgetItem,
        issue: ValidationIssue,
    ) -> None:
        """Append a child row for *issue* under *parent*."""
        icon_glyph = _SEVERITY_ICONS.get(issue.severity, "●")
        child = QTreeWidgetItem(parent, [icon_glyph, issue.location, issue.message])

        color_hex = _SEVERITY_COLORS.get(issue.severity, "#6b7280")
        from PySide6.QtGui import QColor  # local import avoids top-level cycle risk

        child.setForeground(0, QColor(color_hex))

        child.setFlags(child.flags() | Qt.ItemFlag.ItemIsEnabled)

        # "Open in editor" button in a container widget
        container = QWidget()
        container.setObjectName(f"container_open_{issue.location}")
        h_layout = QHBoxLayout(container)
        h_layout.setContentsMargins(2, 1, 2, 1)

        btn = QPushButton("Open in editor", container)
        btn.setObjectName(f"btn_open_{issue.location}")
        btn.setAccessibleName(f"Open {issue.location} in editor")
        btn.setFixedHeight(22)
        btn.clicked.connect(lambda checked=False, loc=issue.location: self.open_requested.emit(loc))
        h_layout.addWidget(btn)
        h_layout.addStretch()

        # We use column 2 (Message) column width for the button; add button as
        # a sibling widget alongside the row by setting it via setItemWidget.
        # QTreeWidget only supports one widget per item — place it in column 2
        # and shift the message to a tooltip instead.
        child.setToolTip(2, issue.message)
        child.setText(2, issue.message)

        # Add a 4th column for the button
        self._tree.setColumnCount(4)
        if self._tree.headerItem().text(3) == "":
            self._tree.headerItem().setText(3, "Action")
        self._tree.setItemWidget(child, 3, container)

    # ------------------------------------------------------------------
    # Public accessors (for testing)
    # ------------------------------------------------------------------

    @property
    def tree(self) -> QTreeWidget:
        """The underlying QTreeWidget."""
        return self._tree

    @property
    def issue_count(self) -> int:
        """Total number of issues passed to the dialog."""
        return len(self._issues)
