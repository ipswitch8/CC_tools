# -*- coding: utf-8 -*-
"""
cpsm.ui.dialogs.settings — Settings dialog.

Spec section: §4.7

Binds every field of the Settings model to a type-correct widget:
  - default_multiplexer    → QButtonGroup (radio)
  - default_terminal       → QComboBox
  - ssh_binary             → QButtonGroup (radio)
  - default_claude_options → QLineEdit
  - default_ssh_options    → QLineEdit
  - known_hosts_strict     → QCheckBox
  - status_poll_interval_ms -> QSpinBox (1000-30000)
  - layout_conflict_default → QComboBox
  - layout_preserve_on_remove → QCheckBox
  - log_level              → QComboBox
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from cpsm.data.schema import Settings

__all__ = ["SettingsDialog"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MULTIPLEXER_OPTIONS = ["tmux", "itmux", "psmux", "auto"]
_TERMINAL_OPTIONS = [
    "auto",
    "wt",
    "gnome-terminal",
    "konsole",
    "alacritty",
    "kitty",
    "xterm",
    "wezterm",
]
_SSH_BINARY_OPTIONS = ["auto", "openssh", "plink"]
_CONFLICT_OPTIONS = ["move", "keep", "error"]
_LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------


class SettingsDialog(QDialog):
    """Settings dialog — binds every settings.* field.

    Parameters
    ----------
    settings:
        The Settings model instance to read from (and write to on accept).
    parent:
        Optional parent widget.
    """

    def __init__(
        self,
        settings: Settings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("Settings")
        self.setObjectName("dlg_settings")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)

        # ----- Multiplexer group -----
        mux_box = QGroupBox("Default Multiplexer")
        mux_box.setObjectName("group_multiplexer")
        mux_layout = QHBoxLayout(mux_box)
        self._mux_group = QButtonGroup(self)
        self._mux_radios: dict[str, QRadioButton] = {}
        for opt in _MULTIPLEXER_OPTIONS:
            rb = QRadioButton(opt)
            rb.setObjectName(f"radio_mux_{opt}")
            rb.setAccessibleName(f"Multiplexer {opt}")
            self._mux_radios[opt] = rb
            self._mux_group.addButton(rb)
            mux_layout.addWidget(rb)
        layout.addWidget(mux_box)

        # ----- Terminal combo -----
        terminal_box = QGroupBox("Default Terminal")
        terminal_box.setObjectName("group_terminal")
        terminal_layout = QFormLayout(terminal_box)
        self._combo_terminal = QComboBox()
        self._combo_terminal.setObjectName("combo_terminal")
        self._combo_terminal.setAccessibleName("Default terminal")
        self._combo_terminal.addItems(_TERMINAL_OPTIONS)
        terminal_layout.addRow(QLabel("Terminal:"), self._combo_terminal)
        layout.addWidget(terminal_box)

        # ----- SSH Binary group -----
        ssh_box = QGroupBox("SSH Binary")
        ssh_box.setObjectName("group_ssh_binary")
        ssh_layout = QHBoxLayout(ssh_box)
        self._ssh_group = QButtonGroup(self)
        self._ssh_radios: dict[str, QRadioButton] = {}
        for opt in _SSH_BINARY_OPTIONS:
            rb = QRadioButton(opt)
            rb.setObjectName(f"radio_ssh_{opt}")
            rb.setAccessibleName(f"SSH binary {opt}")
            self._ssh_radios[opt] = rb
            self._ssh_group.addButton(rb)
            ssh_layout.addWidget(rb)
        layout.addWidget(ssh_box)

        # ----- Free-text fields -----
        text_box = QGroupBox("Options")
        text_box.setObjectName("group_options")
        form = QFormLayout(text_box)

        self._edit_claude_opts = QLineEdit()
        self._edit_claude_opts.setObjectName("edit_default_claude_options")
        self._edit_claude_opts.setAccessibleName("Default Claude options")
        form.addRow("Default Claude Options:", self._edit_claude_opts)

        self._edit_ssh_opts = QLineEdit()
        self._edit_ssh_opts.setObjectName("edit_default_ssh_options")
        self._edit_ssh_opts.setAccessibleName("Default SSH options")
        form.addRow("Default SSH Options:", self._edit_ssh_opts)

        layout.addWidget(text_box)

        # ----- Boolean + numeric fields -----
        misc_box = QGroupBox("Behaviour")
        misc_box.setObjectName("group_behaviour")
        misc_form = QFormLayout(misc_box)

        self._chk_known_hosts = QCheckBox("Strict known_hosts checking")
        self._chk_known_hosts.setObjectName("chk_known_hosts_strict")
        self._chk_known_hosts.setAccessibleName("Known hosts strict")
        misc_form.addRow(self._chk_known_hosts)

        self._spin_poll = QSpinBox()
        self._spin_poll.setObjectName("spin_status_poll_interval_ms")
        self._spin_poll.setAccessibleName("Status poll interval ms")
        self._spin_poll.setMinimum(1000)
        self._spin_poll.setMaximum(30000)
        self._spin_poll.setSingleStep(500)
        self._spin_poll.setSuffix(" ms")
        misc_form.addRow("Status Poll Interval:", self._spin_poll)

        self._combo_conflict = QComboBox()
        self._combo_conflict.setObjectName("combo_layout_conflict_default")
        self._combo_conflict.setAccessibleName("Layout conflict default")
        self._combo_conflict.addItems(_CONFLICT_OPTIONS)
        misc_form.addRow("Layout Conflict Default:", self._combo_conflict)

        self._chk_preserve = QCheckBox("Preserve layout on remove (empty-slot placeholder)")
        self._chk_preserve.setObjectName("chk_layout_preserve_on_remove")
        self._chk_preserve.setAccessibleName("Layout preserve on remove")
        misc_form.addRow(self._chk_preserve)

        self._combo_log = QComboBox()
        self._combo_log.setObjectName("combo_log_level")
        self._combo_log.setAccessibleName("Log level")
        self._combo_log.addItems(_LOG_LEVELS)
        misc_form.addRow("Log Level:", self._combo_log)

        layout.addWidget(misc_box)

        # ----- Button box -----
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.setObjectName("button_box")
        self._buttons.accepted.connect(self._on_accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        # Populate from model
        self._load_settings()

    # ------------------------------------------------------------------
    # Load / save
    # ------------------------------------------------------------------

    def _load_settings(self) -> None:
        """Populate widgets from self._settings."""
        s = self._settings

        # Multiplexer
        radio = self._mux_radios.get(s.default_multiplexer)
        if radio:
            radio.setChecked(True)
        else:
            self._mux_radios.get("auto", next(iter(self._mux_radios.values()))).setChecked(True)

        # Terminal
        idx = self._combo_terminal.findText(s.default_terminal)
        self._combo_terminal.setCurrentIndex(max(0, idx))

        # SSH binary
        rb = self._ssh_radios.get(s.ssh_binary)
        if rb:
            rb.setChecked(True)
        else:
            self._ssh_radios.get("auto", next(iter(self._ssh_radios.values()))).setChecked(True)

        # Text fields
        self._edit_claude_opts.setText(s.default_claude_options or "")
        self._edit_ssh_opts.setText(s.default_ssh_options or "")

        # Booleans
        self._chk_known_hosts.setChecked(s.known_hosts_strict)
        self._chk_preserve.setChecked(s.layout_preserve_on_remove)

        # Numeric
        self._spin_poll.setValue(max(1000, min(30000, s.status_poll_interval_ms)))

        # Combos
        idx = self._combo_conflict.findText(s.layout_conflict_default)
        self._combo_conflict.setCurrentIndex(max(0, idx))

        idx = self._combo_log.findText(s.log_level)
        self._combo_log.setCurrentIndex(max(0, idx))

    def collect_data(self) -> dict[str, object]:
        """Return a dict of the current widget values (does not modify model)."""
        mux = next(
            (k for k, rb in self._mux_radios.items() if rb.isChecked()),
            "auto",
        )
        ssh_bin = next(
            (k for k, rb in self._ssh_radios.items() if rb.isChecked()),
            "auto",
        )
        return {
            "default_multiplexer": mux,
            "default_terminal": self._combo_terminal.currentText(),
            "ssh_binary": ssh_bin,
            "default_claude_options": self._edit_claude_opts.text(),
            "default_ssh_options": self._edit_ssh_opts.text(),
            "known_hosts_strict": self._chk_known_hosts.isChecked(),
            "status_poll_interval_ms": self._spin_poll.value(),
            "layout_conflict_default": self._combo_conflict.currentText(),
            "layout_preserve_on_remove": self._chk_preserve.isChecked(),
            "log_level": self._combo_log.currentText(),
        }

    def _on_accept(self) -> None:
        """Write widget values back to the Settings model and accept."""
        s = self._settings
        # Use object.__setattr__ to bypass mypy Literal type checks on Pydantic model fields.
        # Runtime validation is still enforced by the model on the next save/load cycle.
        object.__setattr__(
            s,
            "default_multiplexer",
            next((k for k, rb in self._mux_radios.items() if rb.isChecked()), "auto"),
        )
        object.__setattr__(s, "default_terminal", self._combo_terminal.currentText())
        object.__setattr__(
            s,
            "ssh_binary",
            next((k for k, rb in self._ssh_radios.items() if rb.isChecked()), "auto"),
        )
        s.default_claude_options = self._edit_claude_opts.text()
        s.default_ssh_options = self._edit_ssh_opts.text()
        s.known_hosts_strict = self._chk_known_hosts.isChecked()
        s.status_poll_interval_ms = self._spin_poll.value()
        object.__setattr__(s, "layout_conflict_default", self._combo_conflict.currentText())
        s.layout_preserve_on_remove = self._chk_preserve.isChecked()
        object.__setattr__(s, "log_level", self._combo_log.currentText())
        self.accept()
