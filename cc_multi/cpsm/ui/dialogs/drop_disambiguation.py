# -*- coding: utf-8 -*-
"""
cpsm.ui.dialogs.drop_disambiguation — DropDisambiguationDialog.

Spec section: §6.7

Modal dialog shown when a connection is dropped onto the centre zone of an
already-occupied pane.  Offers four choices:
    replace      — respawn_pane with the dragged connection's launcher
    split-right  — split_pane horizontal then send_keys
    split-below  — split_pane vertical then send_keys
    cancel       — abort the drop

Returns the choice as a Literal string via ``exec()`` (caller reads
``dialog.choice`` after the modal exits).
"""

from __future__ import annotations

from typing import Literal

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

__all__ = ["DropDisambiguationDialog"]

DropChoice = Literal["replace", "split-right", "split-below", "cancel"]


class DropDisambiguationDialog(QDialog):
    """Ask the user what to do when a connection is dropped on an occupied pane.

    Parameters
    ----------
    connection_name:
        Display name of the connection being dropped (for the prompt text).
    pane_label:
        Display label of the target pane (for the prompt text).
    parent:
        Optional Qt parent widget.

    Attributes
    ----------
    choice : DropChoice
        Set after the dialog is closed.  One of ``"replace"``,
        ``"split-right"``, ``"split-below"``, or ``"cancel"``.
    """

    def __init__(
        self,
        connection_name: str = "",
        pane_label: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("dlg_drop_disambiguation")
        self.setAccessibleName("Drop Disambiguation Dialog")
        self.setAccessibleDescription("Choose how to drop the connection onto the occupied pane")
        self.setWindowTitle("Drop Action")
        self.setModal(True)
        self.setMinimumWidth(360)

        self.choice: DropChoice = "cancel"

        # --- Layout ---
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # Prompt
        prompt_text = "The target pane is already occupied."
        if connection_name:
            prompt_text = f"Drop '{connection_name}'"
            if pane_label:
                prompt_text += f" onto '{pane_label}'"
            prompt_text += ":"
        label = QLabel(prompt_text)
        label.setObjectName("dlg_drop_disambiguation_prompt")
        label.setAccessibleName("Drop prompt")
        label.setWordWrap(True)
        root.addWidget(label)

        # Button row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self._btn_replace = QPushButton("Replace pane")
        self._btn_replace.setObjectName("dlg_drop_btn_replace")
        self._btn_replace.setAccessibleName("Replace pane button")
        self._btn_replace.clicked.connect(self._on_replace)
        btn_row.addWidget(self._btn_replace)

        self._btn_split_right = QPushButton("Split right")
        self._btn_split_right.setObjectName("dlg_drop_btn_split_right")
        self._btn_split_right.setAccessibleName("Split right button")
        self._btn_split_right.clicked.connect(self._on_split_right)
        btn_row.addWidget(self._btn_split_right)

        self._btn_split_below = QPushButton("Split below")
        self._btn_split_below.setObjectName("dlg_drop_btn_split_below")
        self._btn_split_below.setAccessibleName("Split below button")
        self._btn_split_below.clicked.connect(self._on_split_below)
        btn_row.addWidget(self._btn_split_below)

        self._btn_cancel = QPushButton("Cancel")
        self._btn_cancel.setObjectName("dlg_drop_btn_cancel")
        self._btn_cancel.setAccessibleName("Cancel button")
        self._btn_cancel.clicked.connect(self._on_cancel)
        btn_row.addWidget(self._btn_cancel)

        root.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    def _on_replace(self) -> None:
        self.choice = "replace"
        self.accept()

    def _on_split_right(self) -> None:
        self.choice = "split-right"
        self.accept()

    def _on_split_below(self) -> None:
        self.choice = "split-below"
        self.accept()

    def _on_cancel(self) -> None:
        self.choice = "cancel"
        self.reject()

    # ------------------------------------------------------------------
    # Convenience class method
    # ------------------------------------------------------------------

    @classmethod
    def ask(
        cls,
        connection_name: str = "",
        pane_label: str = "",
        parent: QWidget | None = None,
    ) -> DropChoice:
        """Show the dialog modally and return the chosen action.

        Parameters
        ----------
        connection_name:
            Display name of the dragged connection.
        pane_label:
            Display label of the target pane.
        parent:
            Optional Qt parent widget.

        Returns
        -------
        DropChoice
            One of ``"replace"``, ``"split-right"``, ``"split-below"``,
            or ``"cancel"``.
        """
        dlg = cls(connection_name=connection_name, pane_label=pane_label, parent=parent)
        dlg.exec()
        return dlg.choice
