# -*- coding: utf-8 -*-
"""
cpsm.ui.dialogs.about — Simple About dialog.

Spec section: §3.1 (dialogs list)
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from cpsm import __version__

__all__ = ["AboutDialog"]


class AboutDialog(QDialog):
    """Simple About dialog showing version and licence information.

    Object names follow the ``dlg_about`` convention from §3.1.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.setObjectName("dlg_about")
        self.setAccessibleName("About CPSM Dialog")
        self.setAccessibleDescription("Dialog showing version and license information for CPSM")

        self.setWindowTitle("About CPSM")
        self.setMinimumSize(560, 400)
        self.resize(640, 480)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)

        outer = QVBoxLayout(self)
        outer.setSpacing(8)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setObjectName("scroll_about")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll, 1)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)
        scroll.setWidget(content)

        title_label = QLabel("<h2>CPSM</h2><p>Cross-Platform Session Manager</p>")
        title_label.setObjectName("label_about_title")
        title_label.setAccessibleName("About Title")
        title_label.setAccessibleDescription("Application name and tagline")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title_label)

        version_label = QLabel(f"<b>Version:</b> {__version__}")
        version_label.setObjectName("label_about_version")
        version_label.setAccessibleName("About Version")
        version_label.setAccessibleDescription("Current version number")
        version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(version_label)

        desc_label = QLabel(
            "Manages tmux/SSH sessions and Claude Code projects across "
            "Linux and Windows terminals.\n\n"
            "Licensed under the MIT License."
        )
        desc_label.setObjectName("label_about_description")
        desc_label.setAccessibleName("About Description")
        desc_label.setAccessibleDescription("Short application description and license statement")
        desc_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)

        layout.addStretch()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.setObjectName("buttonbox_about")
        buttons.setAccessibleName("About Dialog Button Box")
        buttons.setAccessibleDescription("OK button to close the About dialog")
        buttons.accepted.connect(self.accept)
        outer.addWidget(buttons)
