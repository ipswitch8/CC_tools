# -*- coding: utf-8 -*-
"""
cpsm.ui.dialogs.welcome — First-run Welcome dialog.

Spec sections: §4.2
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QVBoxLayout,
    QWidget,
)

__all__ = ["WelcomeDialog"]

_LEGACY_DEFAULT = Path("~/.claude-projects.yaml")
_CPSM_DEFAULT = Path("~/.cpsm.yaml")


class WelcomeDialog(QDialog):
    """First-run dialog: Import / Empty / Open.

    Object name: ``dlg_welcome`` (§3.1 stable objectName convention).
    """

    class Choice(Enum):
        IMPORT = "import"
        EMPTY = "empty"
        OPEN = "open"
        CANCEL = "cancel"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._choice: WelcomeDialog.Choice = WelcomeDialog.Choice.CANCEL
        self._source_path: Path | None = None

        self.setObjectName("dlg_welcome")
        self.setAccessibleName("Welcome Dialog")
        self.setAccessibleDescription("First-run dialog presenting Import, Empty, or Open options")
        self.setWindowTitle("Welcome to CPSM")
        self.setMinimumWidth(480)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)

        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(32, 32, 32, 24)

        # Title
        lbl_title = QLabel("Welcome to CPSM")
        lbl_title.setObjectName("lbl_title")
        lbl_title.setAccessibleName("Welcome Title")
        lbl_title.setAccessibleDescription("Welcome dialog title label")
        font = lbl_title.font()
        font.setPointSize(font.pointSize() + 4)
        font.setBold(True)
        lbl_title.setFont(font)
        lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl_title)

        # Description
        lbl_description = QLabel(
            "Cross-Platform Session Manager — manage tmux/SSH sessions and Claude "
            "Code projects.\n\nChoose how you want to get started:"
        )
        lbl_description.setObjectName("lbl_description")
        lbl_description.setAccessibleName("Welcome Description")
        lbl_description.setAccessibleDescription(
            "Short description of CPSM and prompt to choose a startup option"
        )
        lbl_description.setWordWrap(True)
        lbl_description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl_description)

        layout.addItem(QSpacerItem(0, 8, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed))

        # Import button
        btn_import = QPushButton("Import from .claude-projects.yaml…")
        btn_import.setObjectName("btn_import")
        btn_import.setAccessibleName("Import Legacy Config Button")
        btn_import.setAccessibleDescription(
            "Browse for a .claude-projects.yaml file and import it into CPSM"
        )
        btn_import.setMinimumHeight(48)
        btn_import.clicked.connect(self._on_import_clicked)
        layout.addWidget(btn_import)

        # Empty button
        btn_empty = QPushButton("Start with an empty config")
        btn_empty.setObjectName("btn_empty")
        btn_empty.setAccessibleName("Start Empty Config Button")
        btn_empty.setAccessibleDescription("Create a new empty CPSM configuration from scratch")
        btn_empty.setMinimumHeight(48)
        btn_empty.clicked.connect(self._on_empty_clicked)
        layout.addWidget(btn_empty)

        # Open button
        btn_open = QPushButton("Open existing .cpsm.yaml…")
        btn_open.setObjectName("btn_open")
        btn_open.setAccessibleName("Open Existing Config Button")
        btn_open.setAccessibleDescription(
            "Browse for an existing .cpsm.yaml configuration file to open"
        )
        btn_open.setMinimumHeight(48)
        btn_open.clicked.connect(self._on_open_clicked)
        layout.addWidget(btn_open)

        layout.addItem(QSpacerItem(0, 8, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed))

        # Cancel button
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        btn_cancel = button_box.button(QDialogButtonBox.StandardButton.Cancel)
        assert btn_cancel is not None
        btn_cancel.setObjectName("btn_cancel")
        btn_cancel.setAccessibleName("Cancel Button")
        btn_cancel.setAccessibleDescription("Cancel the welcome dialog without choosing an option")
        button_box.rejected.connect(self._on_cancel)
        layout.addWidget(button_box)

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def choice(self) -> WelcomeDialog.Choice:
        """The user's selection."""
        return self._choice

    @property
    def source_path(self) -> Path | None:
        """Path selected when choice is IMPORT or OPEN; ``None`` otherwise."""
        return self._source_path

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_import_clicked(self) -> None:
        """Open a file picker pre-populated with ~/.claude-projects.yaml."""
        default = _LEGACY_DEFAULT.expanduser()
        start_dir = str(default) if default.exists() else str(default.parent.expanduser())
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Select legacy .claude-projects.yaml",
            start_dir,
            "YAML files (*.yaml *.yml);;All files (*)",
        )
        if path_str:
            self._source_path = Path(path_str)
            self._choice = WelcomeDialog.Choice.IMPORT
            self.accept()

    def _on_empty_clicked(self) -> None:
        """Select an empty config and close the dialog."""
        self._source_path = None
        self._choice = WelcomeDialog.Choice.EMPTY
        self.accept()

    def _on_open_clicked(self) -> None:
        """Open a file picker to locate an existing .cpsm.yaml."""
        default = _CPSM_DEFAULT.expanduser()
        start_dir = str(default) if default.exists() else str(default.parent.expanduser())
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Open existing .cpsm.yaml",
            start_dir,
            "CPSM config (*.yaml *.yml);;All files (*)",
        )
        if path_str:
            self._source_path = Path(path_str)
            self._choice = WelcomeDialog.Choice.OPEN
            self.accept()

    def _on_cancel(self) -> None:
        """Set choice to CANCEL and reject the dialog."""
        self._choice = WelcomeDialog.Choice.CANCEL
        self.reject()
