# -*- coding: utf-8 -*-
"""
cpsm.ui.widgets.connection_form — ConnectionForm reusable widget.

Spec sections: §2.3, §4.5

ConnectionForm builds all possible fields once and toggles visibility
based on the selected launch_profile.  It is reusable: ConnectionEditor
embeds it; Phase 18 Layout Editor uses it via the connection picker.

Profile-switch confirmation:
    When the user changes the launch_profile radio AND the new profile
    forbids fields that carry non-default values, a QMessageBox.question
    is shown.  On Yes the forbidden fields are cleared and the profile is
    applied.  On No the radio is reverted.

    The confirmation is NOT shown during initial population (construction
    time) — only after the widget is fully constructed and the user
    interacts.  This makes it mockable in tests.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

__all__ = ["ConnectionForm"]

# ---------------------------------------------------------------------------
# Profile constants
# ---------------------------------------------------------------------------

_PROFILES: list[str] = [
    "claude-remote",
    "claude-local",
    "ssh-shell",
    "local-shell",
    "custom",
]

_PROFILE_GLYPHS: dict[str, str] = {
    "claude-remote": "\U0001f517",  # 🔗
    "claude-local": "\U0001f4bb",  # 💻
    "ssh-shell": "⌨",  # ⌨
    "local-shell": "$",
    "custom": "⚙",  # ⚙
}

# Fields that are *forbidden* for each profile — switching to that profile
# will prompt the user to clear these if they have non-default values.
_FORBIDDEN_FIELDS: dict[str, list[str]] = {
    "claude-remote": [],
    "claude-local": [
        "host",
        "port",
        "user",
        "identity_file_ref",
        "jump_host",
        "keepalive_interval_s",
        "connection_timeout_s",
    ],
    "ssh-shell": ["claude_options"],
    "local-shell": [
        "host",
        "port",
        "user",
        "identity_file_ref",
        "jump_host",
        "claude_options",
        "keepalive_interval_s",
        "connection_timeout_s",
    ],
    "custom": ["custom_template_id"],  # custom_template_id is *required* for custom
}

# Default / empty values for each field (used to decide "non-default" in confirmation)
_FIELD_DEFAULTS: dict[str, Any] = {
    "host": "",
    "port": 22,
    "user": "",
    "identity_file_ref": "",
    "jump_host": "",
    "claude_options": "",
    "keepalive_interval_s": 0,
    "connection_timeout_s": 0,
    "custom_template_id": "",
}


# ---------------------------------------------------------------------------
# Visibility matrix §2.3
# ---------------------------------------------------------------------------


def _field_visible(field: str, profile: str) -> bool:
    """Return True if *field* should be shown for *profile*."""
    matrix: dict[str, set[str]] = {
        "host": {"claude-remote", "ssh-shell", "custom"},
        "port": {"claude-remote", "ssh-shell", "custom"},
        "user": {"claude-remote", "ssh-shell", "custom"},
        "sudo_user": {"claude-remote", "claude-local", "ssh-shell", "local-shell", "custom"},
        "identity_file_ref": {"claude-remote", "ssh-shell", "custom"},
        "jump_host": {"claude-remote", "ssh-shell", "custom"},
        "project_folder": {"claude-remote", "claude-local", "ssh-shell", "local-shell", "custom"},
        "claude_options": {"claude-remote", "claude-local", "custom"},
        "custom_template_id": {"custom"},
        "keepalive_interval_s": {"claude-remote", "ssh-shell", "custom"},
        "connection_timeout_s": {"claude-remote", "ssh-shell", "custom"},
        # Always visible
        "env": set(_PROFILES),
        "pre_commands": set(_PROFILES),
        "post_commands": set(_PROFILES),
        "auto_reconnect": set(_PROFILES),
        "auto_reconnect_on_clean_exit": set(_PROFILES),
        "reconnect_backoff_ms": set(_PROFILES),
        "reconnect_max_attempts": set(_PROFILES),
        "tags": set(_PROFILES),
        "notes": set(_PROFILES),
    }
    return profile in matrix.get(field, set())


class ConnectionForm(QWidget):
    """Reusable form widget for editing a Connection record.

    Signals:
        profile_changed(str): emitted when the launch_profile radio changes
            (after any confirmation dialog resolves).
        validation_changed(bool): emitted when the overall validity changes;
            True = no errors.
    """

    profile_changed: Signal = Signal(str)
    validation_changed: Signal = Signal(bool)

    # Allow tests to replace the confirm callable.
    _confirm_fn: Callable[[QWidget, str, str], bool] | None = None

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        available_key_ids: list[str] | None = None,
        available_layout_ids: list[str] | None = None,
        available_template_ids: list[str] | None = None,
    ) -> None:
        super().__init__(parent)

        self.setObjectName("connection_form")
        self.setAccessibleName("Connection Form")
        self.setAccessibleDescription(
            "Form for editing a connection record; visible fields adapt to launch_profile"
        )

        self._available_key_ids: list[str] = available_key_ids or []
        self._available_template_ids: list[str] = available_template_ids or []

        # Track whether we are in construction / programmatic population
        # — suppress confirmation dialogs during that time.
        self._suppress_confirm: bool = True

        # Current profile
        self._current_profile: str = "claude-remote"

        # Errors: field_name -> error string (empty string = no error)
        self._errors: dict[str, str] = {}

        # Build UI
        self._build_ui()

        # Done constructing — future radio clicks will trigger confirmations.
        self._suppress_confirm = False

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        """Create all field widgets once."""
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        # Scroll area wrapping the form content
        scroll = QScrollArea()
        scroll.setObjectName("form_scroll_area")
        scroll.setAccessibleName("Form Scroll Area")
        scroll.setWidgetResizable(True)
        root.addWidget(scroll)

        container = QWidget()
        container.setObjectName("form_container")
        scroll.setWidget(container)

        layout = QVBoxLayout(container)
        layout.setSpacing(10)
        layout.setContentsMargins(8, 8, 8, 8)

        # ---- Launch Profile radio group ----
        self._grp_profile = self._make_group("Launch Profile")
        self._grp_profile.setObjectName("grp_profile")
        p_layout = QHBoxLayout()
        self._radio_group = QButtonGroup(self)
        self._radio_group.setObjectName("radio_group_profile")
        self._radios: dict[str, QRadioButton] = {}
        for prof in _PROFILES:
            glyph = _PROFILE_GLYPHS[prof]
            rb = QRadioButton(f"{glyph}  {prof}")
            rb.setObjectName(f"radio_{prof.replace('-', '_')}")
            rb.setAccessibleName(f"{prof} profile radio button")
            rb.setAccessibleDescription(f"Select {prof} launch profile")
            self._radios[prof] = rb
            self._radio_group.addButton(rb)
            p_layout.addWidget(rb)
        self._grp_profile.setLayout(p_layout)
        layout.addWidget(self._grp_profile)

        # ---- Network group ----
        self._grp_network = self._make_group("Network")
        self._grp_network.setObjectName("grp_network")
        nf = QFormLayout()
        nf.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self._lbl_host = QLabel("Host *")
        self._edit_host = QLineEdit()
        self._edit_host.setObjectName("edit_host")
        self._edit_host.setAccessibleName("Host field")
        self._edit_host.setAccessibleDescription("Remote hostname or IP address")
        self._edit_host.setPlaceholderText("e.g. dev.example.com")
        self._err_host = QLabel()
        self._err_host.setObjectName("error_host")
        self._err_host.setAccessibleName("Host error label")
        self._err_host.setStyleSheet("color: red;")
        self._err_host.setVisible(False)
        nf.addRow(self._lbl_host, self._edit_host)
        nf.addRow("", self._err_host)

        self._lbl_port = QLabel("Port *")
        self._spin_port = QSpinBox()
        self._spin_port.setObjectName("spin_port")
        self._spin_port.setAccessibleName("Port field")
        self._spin_port.setAccessibleDescription("SSH port number")
        self._spin_port.setRange(1, 65535)
        self._spin_port.setValue(22)
        self._err_port = QLabel()
        self._err_port.setObjectName("error_port")
        self._err_port.setAccessibleName("Port error label")
        self._err_port.setStyleSheet("color: red;")
        self._err_port.setVisible(False)
        nf.addRow(self._lbl_port, self._spin_port)
        nf.addRow("", self._err_port)

        self._lbl_user = QLabel("User *")
        self._edit_user = QLineEdit()
        self._edit_user.setObjectName("edit_user")
        self._edit_user.setAccessibleName("User field")
        self._edit_user.setAccessibleDescription("Remote username")
        self._edit_user.setPlaceholderText("e.g. ubuntu")
        self._err_user = QLabel()
        self._err_user.setObjectName("error_user")
        self._err_user.setAccessibleName("User error label")
        self._err_user.setStyleSheet("color: red;")
        self._err_user.setVisible(False)
        nf.addRow(self._lbl_user, self._edit_user)
        nf.addRow("", self._err_user)

        self._grp_network.setLayout(nf)
        layout.addWidget(self._grp_network)

        # ---- Auth group ----
        self._grp_auth = self._make_group("Authentication")
        self._grp_auth.setObjectName("grp_auth")
        af = QFormLayout()
        af.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        # Auth method combo (Round C): "ask" | "key" | "password"
        self._lbl_auth_method = QLabel("Authentication")
        self._combo_auth_method = QComboBox()
        self._combo_auth_method.setObjectName("combo_auth_method")
        self._combo_auth_method.setAccessibleName("Authentication method")
        self._combo_auth_method.setAccessibleDescription(
            "Whether to prompt to deploy an SSH key, always use the key, "
            "or always use password authentication"
        )
        self._combo_auth_method.addItem("Ask on first launch", "ask")
        self._combo_auth_method.addItem("Use SSH key", "key")
        self._combo_auth_method.addItem("Always use password", "password")
        af.addRow(self._lbl_auth_method, self._combo_auth_method)

        self._lbl_identity_file_ref = QLabel("Identity File")
        identity_row = QHBoxLayout()
        self._combo_identity_file_ref = QComboBox()
        self._combo_identity_file_ref.setObjectName("combo_identity_file_ref")
        self._combo_identity_file_ref.setAccessibleName("Identity file reference combo")
        self._combo_identity_file_ref.setAccessibleDescription(
            "SSH key for this connection. Optional when Authentication is "
            "'Ask on first launch' or 'Always use password'."
        )
        self._combo_identity_file_ref.setEditable(True)
        identity_row.addWidget(self._combo_identity_file_ref)
        self._btn_new_key = QPushButton("+ New Key…")
        self._btn_new_key.setObjectName("btn_new_key")
        self._btn_new_key.setAccessibleName("New SSH Key button")
        self._btn_new_key.setAccessibleDescription(
            "Open SSH key manager to create a new key (wired in Phase 13)"
        )
        self._btn_new_key.setFixedWidth(100)
        identity_row.addWidget(self._btn_new_key)
        identity_widget = QWidget()
        identity_widget.setObjectName("widget_identity_row")
        identity_widget.setLayout(identity_row)
        self._err_identity_file_ref = QLabel()
        self._err_identity_file_ref.setObjectName("error_identity_file_ref")
        self._err_identity_file_ref.setAccessibleName("Identity file error label")
        self._err_identity_file_ref.setStyleSheet("color: red;")
        self._err_identity_file_ref.setVisible(False)
        af.addRow(self._lbl_identity_file_ref, identity_widget)
        af.addRow("", self._err_identity_file_ref)

        self._lbl_sudo_user = QLabel("Sudo User")
        self._edit_sudo_user = QLineEdit()
        self._edit_sudo_user.setObjectName("edit_sudo_user")
        self._edit_sudo_user.setAccessibleName("Sudo user field")
        self._edit_sudo_user.setAccessibleDescription("Optional sudo/su target user")
        self._edit_sudo_user.setPlaceholderText("e.g. claude-user")
        self._err_sudo_user = QLabel()
        self._err_sudo_user.setObjectName("error_sudo_user")
        self._err_sudo_user.setAccessibleName("Sudo user error label")
        self._err_sudo_user.setStyleSheet("color: red;")
        self._err_sudo_user.setVisible(False)
        af.addRow(self._lbl_sudo_user, self._edit_sudo_user)
        af.addRow("", self._err_sudo_user)

        self._lbl_jump_host = QLabel("Jump Host")
        self._combo_jump_host = QComboBox()
        self._combo_jump_host.setObjectName("combo_jump_host")
        self._combo_jump_host.setAccessibleName("Jump host combo")
        self._combo_jump_host.setAccessibleDescription("Optional SSH jump/bastion host")
        self._combo_jump_host.setEditable(True)
        self._combo_jump_host.addItem("")
        self._err_jump_host = QLabel()
        self._err_jump_host.setObjectName("error_jump_host")
        self._err_jump_host.setAccessibleName("Jump host error label")
        self._err_jump_host.setStyleSheet("color: red;")
        self._err_jump_host.setVisible(False)
        af.addRow(self._lbl_jump_host, self._combo_jump_host)
        af.addRow("", self._err_jump_host)

        self._grp_auth.setLayout(af)
        layout.addWidget(self._grp_auth)

        # ---- Project group ----
        self._grp_project = self._make_group("Project")
        self._grp_project.setObjectName("grp_project")
        pf = QFormLayout()
        pf.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self._lbl_project_folder = QLabel("Project Folder")
        self._edit_project_folder = QLineEdit()
        self._edit_project_folder.setObjectName("edit_project_folder")
        self._edit_project_folder.setAccessibleName("Project folder field")
        self._edit_project_folder.setAccessibleDescription(
            "Local or remote path to the project directory"
        )
        self._edit_project_folder.setPlaceholderText("e.g. ~/projects/myapp")
        self._err_project_folder = QLabel()
        self._err_project_folder.setObjectName("error_project_folder")
        self._err_project_folder.setAccessibleName("Project folder error label")
        self._err_project_folder.setStyleSheet("color: red;")
        self._err_project_folder.setVisible(False)
        pf.addRow(self._lbl_project_folder, self._edit_project_folder)
        pf.addRow("", self._err_project_folder)

        self._lbl_claude_options = QLabel("Claude Options")
        self._edit_claude_options = QLineEdit()
        self._edit_claude_options.setObjectName("edit_claude_options")
        self._edit_claude_options.setAccessibleName("Claude options field")
        self._edit_claude_options.setAccessibleDescription(
            "Command-line options passed to the claude binary"
        )
        self._edit_claude_options.setPlaceholderText("e.g. --resume")
        self._err_claude_options = QLabel()
        self._err_claude_options.setObjectName("error_claude_options")
        self._err_claude_options.setAccessibleName("Claude options error label")
        self._err_claude_options.setStyleSheet("color: red;")
        self._err_claude_options.setVisible(False)
        pf.addRow(self._lbl_claude_options, self._edit_claude_options)
        pf.addRow("", self._err_claude_options)

        self._lbl_custom_template_id = QLabel("Template *")
        self._combo_custom_template_id = QComboBox()
        self._combo_custom_template_id.setObjectName("combo_custom_template_id")
        self._combo_custom_template_id.setAccessibleName("Custom template ID combo")
        self._combo_custom_template_id.setAccessibleDescription(
            "Select a launch template for the custom profile"
        )
        self._combo_custom_template_id.setEditable(True)
        self._err_custom_template_id = QLabel()
        self._err_custom_template_id.setObjectName("error_custom_template_id")
        self._err_custom_template_id.setAccessibleName("Custom template error label")
        self._err_custom_template_id.setStyleSheet("color: red;")
        self._err_custom_template_id.setVisible(False)
        pf.addRow(self._lbl_custom_template_id, self._combo_custom_template_id)
        pf.addRow("", self._err_custom_template_id)

        self._grp_project.setLayout(pf)
        layout.addWidget(self._grp_project)

        # ---- Reconnect group (collapsible) ----
        self._grp_reconnect = self._make_group("Reconnect")
        self._grp_reconnect.setObjectName("grp_reconnect")
        self._grp_reconnect.setCheckable(True)
        self._grp_reconnect.setChecked(True)
        rf = QFormLayout()
        rf.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self._chk_auto_reconnect = QCheckBox("Auto-reconnect on failure")
        self._chk_auto_reconnect.setObjectName("chk_auto_reconnect")
        self._chk_auto_reconnect.setAccessibleName("Auto reconnect checkbox")
        self._chk_auto_reconnect.setAccessibleDescription(
            "Automatically reconnect when the connection drops"
        )
        rf.addRow("", self._chk_auto_reconnect)

        self._chk_auto_reconnect_on_clean_exit = QCheckBox("Reconnect on clean exit")
        self._chk_auto_reconnect_on_clean_exit.setObjectName("chk_auto_reconnect_on_clean_exit")
        self._chk_auto_reconnect_on_clean_exit.setAccessibleName(
            "Auto reconnect on clean exit checkbox"
        )
        self._chk_auto_reconnect_on_clean_exit.setAccessibleDescription(
            "Reconnect even when the session exits cleanly"
        )
        rf.addRow("", self._chk_auto_reconnect_on_clean_exit)

        self._lbl_reconnect_backoff_ms = QLabel("Backoff (ms)")
        self._edit_reconnect_backoff_ms = QLineEdit()
        self._edit_reconnect_backoff_ms.setObjectName("edit_reconnect_backoff_ms")
        self._edit_reconnect_backoff_ms.setAccessibleName("Reconnect backoff ms field")
        self._edit_reconnect_backoff_ms.setAccessibleDescription(
            "Comma-separated list of backoff intervals in milliseconds"
        )
        self._edit_reconnect_backoff_ms.setPlaceholderText("e.g. 1000,3000,10000,30000")
        self._err_reconnect_backoff_ms = QLabel()
        self._err_reconnect_backoff_ms.setObjectName("error_reconnect_backoff_ms")
        self._err_reconnect_backoff_ms.setAccessibleName("Reconnect backoff error label")
        self._err_reconnect_backoff_ms.setStyleSheet("color: red;")
        self._err_reconnect_backoff_ms.setVisible(False)
        rf.addRow(self._lbl_reconnect_backoff_ms, self._edit_reconnect_backoff_ms)
        rf.addRow("", self._err_reconnect_backoff_ms)

        self._lbl_reconnect_max_attempts = QLabel("Max Attempts")
        self._spin_reconnect_max_attempts = QSpinBox()
        self._spin_reconnect_max_attempts.setObjectName("spin_reconnect_max_attempts")
        self._spin_reconnect_max_attempts.setAccessibleName("Reconnect max attempts spin")
        self._spin_reconnect_max_attempts.setAccessibleDescription(
            "Maximum reconnect attempts; 0 = unlimited"
        )
        self._spin_reconnect_max_attempts.setRange(0, 9999)
        self._spin_reconnect_max_attempts.setSpecialValueText("Unlimited")
        self._err_reconnect_max_attempts = QLabel()
        self._err_reconnect_max_attempts.setObjectName("error_reconnect_max_attempts")
        self._err_reconnect_max_attempts.setAccessibleName("Reconnect max error label")
        self._err_reconnect_max_attempts.setStyleSheet("color: red;")
        self._err_reconnect_max_attempts.setVisible(False)
        rf.addRow(self._lbl_reconnect_max_attempts, self._spin_reconnect_max_attempts)

        self._grp_reconnect.setLayout(rf)
        layout.addWidget(self._grp_reconnect)

        # ---- Advanced group (collapsible, default collapsed) ----
        self._grp_advanced = self._make_group("Advanced")
        self._grp_advanced.setObjectName("grp_advanced")
        self._grp_advanced.setCheckable(True)
        self._grp_advanced.setChecked(False)
        advf = QFormLayout()
        advf.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self._lbl_env = QLabel("Environment")
        self._edit_env = QPlainTextEdit()
        self._edit_env.setObjectName("edit_env")
        self._edit_env.setAccessibleName("Environment variables field")
        self._edit_env.setAccessibleDescription(
            "KEY=VALUE pairs, one per line, injected into the session environment"
        )
        self._edit_env.setPlaceholderText("KEY=value\nANOTHER=value")
        self._edit_env.setMaximumHeight(80)
        advf.addRow(self._lbl_env, self._edit_env)

        self._lbl_pre_commands = QLabel("Pre-commands")
        self._edit_pre_commands = QPlainTextEdit()
        self._edit_pre_commands.setObjectName("edit_pre_commands")
        self._edit_pre_commands.setAccessibleName("Pre-commands field")
        self._edit_pre_commands.setAccessibleDescription(
            "Shell commands to run before the main session command"
        )
        self._edit_pre_commands.setPlaceholderText("command1\ncommand2")
        self._edit_pre_commands.setMaximumHeight(60)
        advf.addRow(self._lbl_pre_commands, self._edit_pre_commands)

        self._lbl_post_commands = QLabel("Post-commands")
        self._edit_post_commands = QPlainTextEdit()
        self._edit_post_commands.setObjectName("edit_post_commands")
        self._edit_post_commands.setAccessibleName("Post-commands field")
        self._edit_post_commands.setAccessibleDescription(
            "Shell commands to run after the main session command exits"
        )
        self._edit_post_commands.setPlaceholderText("command1\ncommand2")
        self._edit_post_commands.setMaximumHeight(60)
        advf.addRow(self._lbl_post_commands, self._edit_post_commands)

        self._lbl_keepalive_interval_s = QLabel("Keepalive Interval (s)")
        self._spin_keepalive_interval_s = QSpinBox()
        self._spin_keepalive_interval_s.setObjectName("spin_keepalive_interval_s")
        self._spin_keepalive_interval_s.setAccessibleName("Keepalive interval spin")
        self._spin_keepalive_interval_s.setAccessibleDescription(
            "ServerAliveInterval in seconds; 0 = disabled"
        )
        self._spin_keepalive_interval_s.setRange(0, 3600)
        advf.addRow(self._lbl_keepalive_interval_s, self._spin_keepalive_interval_s)

        self._lbl_connection_timeout_s = QLabel("Connection Timeout (s)")
        self._spin_connection_timeout_s = QSpinBox()
        self._spin_connection_timeout_s.setObjectName("spin_connection_timeout_s")
        self._spin_connection_timeout_s.setAccessibleName("Connection timeout spin")
        self._spin_connection_timeout_s.setAccessibleDescription(
            "ConnectTimeout in seconds; 0 = OS default"
        )
        self._spin_connection_timeout_s.setRange(0, 600)
        advf.addRow(self._lbl_connection_timeout_s, self._spin_connection_timeout_s)

        self._lbl_tags = QLabel("Tags")
        self._edit_tags = QLineEdit()
        self._edit_tags.setObjectName("edit_tags")
        self._edit_tags.setAccessibleName("Tags field")
        self._edit_tags.setAccessibleDescription("Comma-separated tags for filtering and display")
        self._edit_tags.setPlaceholderText("production, web, api")
        advf.addRow(self._lbl_tags, self._edit_tags)

        self._lbl_notes = QLabel("Notes")
        self._edit_notes = QPlainTextEdit()
        self._edit_notes.setObjectName("edit_notes")
        self._edit_notes.setAccessibleName("Notes field")
        self._edit_notes.setAccessibleDescription("Free-form notes about this connection")
        self._edit_notes.setMaximumHeight(80)
        advf.addRow(self._lbl_notes, self._edit_notes)

        self._grp_advanced.setLayout(advf)
        layout.addWidget(self._grp_advanced)

        layout.addStretch()

        # Wire radio changes
        self._radio_group.buttonClicked.connect(self._on_radio_clicked)

        # Apply default profile visibility
        self._radios["claude-remote"].setChecked(True)
        self._apply_profile("claude-remote")

        # Wire live validation on blur
        self._edit_host.editingFinished.connect(lambda: self._validate_field("host"))
        self._edit_user.editingFinished.connect(lambda: self._validate_field("user"))
        self._edit_project_folder.editingFinished.connect(
            lambda: self._validate_field("project_folder")
        )
        self._edit_claude_options.editingFinished.connect(
            lambda: self._validate_field("claude_options")
        )
        self._combo_identity_file_ref.currentTextChanged.connect(
            lambda _: self._validate_field("identity_file_ref")
        )
        self._combo_custom_template_id.currentTextChanged.connect(
            lambda _: self._validate_field("custom_template_id")
        )

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_group(title: str) -> QGroupBox:
        gb = QGroupBox(title)
        return gb

    # ------------------------------------------------------------------
    # Profile management
    # ------------------------------------------------------------------

    def _on_radio_clicked(self, button: QRadioButton) -> None:
        """Handle profile radio click; show confirmation when needed."""
        new_profile = next(
            (p for p, rb in self._radios.items() if rb is button),
            None,
        )
        if new_profile is None or new_profile == self._current_profile:
            return

        if not self._suppress_confirm:
            forbidden = _FORBIDDEN_FIELDS.get(new_profile, [])
            non_default = self._get_non_default_forbidden_fields(forbidden)
            if non_default:
                field_list = ", ".join(non_default)
                msg = f"Switching to '{new_profile}' will discard: {field_list}.\n\nContinue?"
                if not self._ask_confirm(msg):
                    # Revert radio
                    self._radios[self._current_profile].setChecked(True)
                    return
                # Clear forbidden fields
                self._clear_fields(non_default)

        self._apply_profile(new_profile)

    def _ask_confirm(self, message: str) -> bool:
        """Show a QMessageBox.question or call the injected confirm function."""
        if self._confirm_fn is not None:
            return self._confirm_fn(self, "Switch Profile", message)
        result = QMessageBox.question(
            self,
            "Switch Profile",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return result == QMessageBox.StandardButton.Yes

    def _get_non_default_forbidden_fields(self, forbidden: list[str]) -> list[str]:
        """Return which forbidden fields carry non-default values."""
        non_default: list[str] = []
        for field in forbidden:
            val = self._get_field_value(field)
            default = _FIELD_DEFAULTS.get(field)
            if val is not None and val != default and val != "" and val != 22:
                non_default.append(field)
        return non_default

    def _get_field_value(self, field: str) -> Any:
        """Return the current widget value for a field."""
        mapping: dict[str, Callable[[], Any]] = {
            "host": lambda: self._edit_host.text(),
            "port": lambda: self._spin_port.value(),
            "user": lambda: self._edit_user.text(),
            "identity_file_ref": lambda: self._combo_identity_file_ref.currentText(),
            "jump_host": lambda: self._combo_jump_host.currentText(),
            "claude_options": lambda: self._edit_claude_options.text(),
            "keepalive_interval_s": lambda: self._spin_keepalive_interval_s.value(),
            "connection_timeout_s": lambda: self._spin_connection_timeout_s.value(),
            "custom_template_id": lambda: self._combo_custom_template_id.currentText(),
            "project_folder": lambda: self._edit_project_folder.text(),
            "sudo_user": lambda: self._edit_sudo_user.text(),
        }
        fn = mapping.get(field)
        return fn() if fn else None

    def _clear_fields(self, fields: list[str]) -> None:
        """Reset the given fields to their default/empty values."""
        for field in fields:
            if field == "host":
                self._edit_host.clear()
            elif field == "port":
                self._spin_port.setValue(22)
            elif field == "user":
                self._edit_user.clear()
            elif field == "identity_file_ref":
                self._combo_identity_file_ref.setCurrentIndex(-1)
                self._combo_identity_file_ref.clearEditText()
            elif field == "jump_host":
                self._combo_jump_host.setCurrentIndex(0)
            elif field == "claude_options":
                self._edit_claude_options.clear()
            elif field == "keepalive_interval_s":
                self._spin_keepalive_interval_s.setValue(0)
            elif field == "connection_timeout_s":
                self._spin_connection_timeout_s.setValue(0)
            elif field == "custom_template_id":
                self._combo_custom_template_id.setCurrentIndex(-1)
                self._combo_custom_template_id.clearEditText()

    def _apply_profile(self, profile: str) -> None:
        """Toggle field visibility for *profile* and update state."""
        self._current_profile = profile

        # Network group: visible for remote profiles
        net_visible = _field_visible("host", profile)
        self._grp_network.setVisible(net_visible)

        # Auth group: visible if identity_file_ref or sudo_user visible
        auth_visible = (
            _field_visible("identity_file_ref", profile)
            or _field_visible("sudo_user", profile)
            or _field_visible("jump_host", profile)
        )
        self._grp_auth.setVisible(auth_visible)

        # Individual auth fields
        identity_visible = _field_visible("identity_file_ref", profile)
        # auth_method tracks the same set of profiles as identity_file_ref
        self._lbl_auth_method.setVisible(identity_visible)
        self._combo_auth_method.setVisible(identity_visible)
        self._lbl_identity_file_ref.setVisible(identity_visible)
        self._combo_identity_file_ref.setVisible(identity_visible)
        self._btn_new_key.setVisible(identity_visible)
        self._err_identity_file_ref.setVisible(False)

        jump_visible = _field_visible("jump_host", profile)
        self._lbl_jump_host.setVisible(jump_visible)
        self._combo_jump_host.setVisible(jump_visible)
        self._err_jump_host.setVisible(False)

        sudo_visible = _field_visible("sudo_user", profile)
        self._lbl_sudo_user.setVisible(sudo_visible)
        self._edit_sudo_user.setVisible(sudo_visible)
        self._err_sudo_user.setVisible(False)

        # Project group
        project_folder_visible = _field_visible("project_folder", profile)
        self._lbl_project_folder.setVisible(project_folder_visible)
        self._edit_project_folder.setVisible(project_folder_visible)
        self._err_project_folder.setVisible(False)

        claude_opts_visible = _field_visible("claude_options", profile)
        self._lbl_claude_options.setVisible(claude_opts_visible)
        self._edit_claude_options.setVisible(claude_opts_visible)
        self._err_claude_options.setVisible(False)

        template_visible = _field_visible("custom_template_id", profile)
        self._lbl_custom_template_id.setVisible(template_visible)
        self._combo_custom_template_id.setVisible(template_visible)
        self._err_custom_template_id.setVisible(False)

        # Keepalive / timeout (advanced group)
        keepalive_visible = _field_visible("keepalive_interval_s", profile)
        self._lbl_keepalive_interval_s.setVisible(keepalive_visible)
        self._spin_keepalive_interval_s.setVisible(keepalive_visible)
        timeout_visible = _field_visible("connection_timeout_s", profile)
        self._lbl_connection_timeout_s.setVisible(timeout_visible)
        self._spin_connection_timeout_s.setVisible(timeout_visible)

        # Clear existing errors for now-hidden fields
        self._errors.clear()
        self._run_validation()

        self.profile_changed.emit(profile)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_field(self, field: str) -> None:
        """Validate a single field and update its error label."""
        profile = self._current_profile
        error = ""

        if field == "host" and _field_visible("host", profile):
            if not self._edit_host.text().strip():
                error = "Host is required"
            self._set_field_error("host", self._edit_host, self._err_host, error)

        elif field == "user" and _field_visible("user", profile):
            if not self._edit_user.text().strip():
                error = "User is required"
            self._set_field_error("user", self._edit_user, self._err_user, error)

        elif field == "identity_file_ref" and _field_visible("identity_file_ref", profile):
            val = self._combo_identity_file_ref.currentText().strip()
            required = profile in {"claude-remote", "ssh-shell"}
            if required and not val:
                error = "Identity file is required"
            self._set_field_error(
                "identity_file_ref",
                self._combo_identity_file_ref,
                self._err_identity_file_ref,
                error,
            )

        elif field == "project_folder" and _field_visible("project_folder", profile):
            val = self._edit_project_folder.text().strip()
            required = profile in {"claude-remote", "claude-local", "local-shell"}
            if required and not val:
                error = "Project folder is required"
            self._set_field_error(
                "project_folder",
                self._edit_project_folder,
                self._err_project_folder,
                error,
            )

        elif field == "claude_options" and _field_visible("claude_options", profile):
            val = self._edit_claude_options.text().strip()
            required = profile in {"claude-remote", "claude-local"}
            if required and not val:
                error = "Claude options is required"
            self._set_field_error(
                "claude_options",
                self._edit_claude_options,
                self._err_claude_options,
                error,
            )

        elif field == "custom_template_id" and _field_visible("custom_template_id", profile):
            val = self._combo_custom_template_id.currentText().strip()
            if not val:
                error = "Template is required for custom profile"
            self._set_field_error(
                "custom_template_id",
                self._combo_custom_template_id,
                self._err_custom_template_id,
                error,
            )

        self._emit_validation()

    def _set_field_error(
        self,
        field: str,
        widget: QWidget,
        err_label: QLabel,
        error: str,
    ) -> None:
        """Apply or clear the error state for a field."""
        if error:
            self._errors[field] = error
            widget.setStyleSheet("border: 1px solid red;")
            err_label.setText(error)
            err_label.setVisible(True)
        else:
            self._errors.pop(field, None)
            widget.setStyleSheet("")
            err_label.setText("")
            err_label.setVisible(False)

    def _run_validation(self) -> None:
        """Re-validate all visible required fields silently."""
        profile = self._current_profile
        # Clear errors and re-run for fields that are required and visible
        required_checks = [
            ("host", _field_visible("host", profile) and profile in {"claude-remote", "ssh-shell"}),
            ("user", _field_visible("user", profile) and profile in {"claude-remote", "ssh-shell"}),
            (
                "identity_file_ref",
                _field_visible("identity_file_ref", profile)
                and profile in {"claude-remote", "ssh-shell"},
            ),
            (
                "project_folder",
                _field_visible("project_folder", profile)
                and profile in {"claude-remote", "claude-local", "local-shell"},
            ),
            (
                "claude_options",
                _field_visible("claude_options", profile)
                and profile in {"claude-remote", "claude-local"},
            ),
            ("custom_template_id", _field_visible("custom_template_id", profile)),
        ]
        for field, is_required in required_checks:
            if not is_required:
                self._errors.pop(field, None)
        self._emit_validation()

    def _emit_validation(self) -> None:
        """Emit validation_changed with current validity."""
        self.validation_changed.emit(len(self._errors) == 0)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def current_profile(self) -> str:
        """Currently selected launch_profile string."""
        return self._current_profile

    def is_valid(self) -> bool:
        """Return True if no validation errors are present."""
        return len(self._errors) == 0

    def errors(self) -> dict[str, str]:
        """Return a copy of current validation errors."""
        return dict(self._errors)

    def populate_keys(self, key_ids: list[str]) -> None:
        """Populate the identity_file_ref combo with available key IDs."""
        self._combo_identity_file_ref.clear()
        for kid in key_ids:
            self._combo_identity_file_ref.addItem(kid)

    def populate_templates(self, template_ids: list[str]) -> None:
        """Populate the custom_template_id combo with available template IDs."""
        self._combo_custom_template_id.clear()
        for tid in template_ids:
            self._combo_custom_template_id.addItem(tid)

    def populate_connections(self, connection_ids: list[str]) -> None:
        """Populate the jump_host combo with available connection IDs."""
        current = self._combo_jump_host.currentText()
        self._combo_jump_host.clear()
        self._combo_jump_host.addItem("")
        for cid in connection_ids:
            self._combo_jump_host.addItem(cid)
        self._combo_jump_host.setCurrentText(current)

    def load_data(self, data: dict[str, Any]) -> None:
        """Populate all fields from a flat dict of connection attributes.

        Sets _suppress_confirm=True so no confirmation dialog fires during
        programmatic population.
        """
        self._suppress_confirm = True
        try:
            profile = str(data.get("launch_profile", "claude-remote"))
            if profile in self._radios:
                self._radios[profile].setChecked(True)
                self._apply_profile(profile)

            self._edit_host.setText(str(data.get("host", "") or ""))
            self._spin_port.setValue(int(data.get("port", 22) or 22))
            self._edit_user.setText(str(data.get("user", "") or ""))
            self._edit_sudo_user.setText(str(data.get("sudo_user", "") or ""))
            self._edit_project_folder.setText(str(data.get("project_folder", "") or ""))
            self._edit_claude_options.setText(str(data.get("claude_options", "") or ""))

            identity_ref = str(data.get("identity_file_ref", "") or "")
            self._combo_identity_file_ref.setCurrentText(identity_ref)

            # auth_method (Round C): default "ask"
            auth_method = str(data.get("auth_method", "ask") or "ask")
            for i in range(self._combo_auth_method.count()):
                if self._combo_auth_method.itemData(i) == auth_method:
                    self._combo_auth_method.setCurrentIndex(i)
                    break

            jump = str(data.get("jump_host", "") or "")
            self._combo_jump_host.setCurrentText(jump)

            template_id = str(data.get("custom_template_id", "") or "")
            self._combo_custom_template_id.setCurrentText(template_id)

            self._spin_keepalive_interval_s.setValue(int(data.get("keepalive_interval_s", 0) or 0))
            self._spin_connection_timeout_s.setValue(int(data.get("connection_timeout_s", 0) or 0))

            self._chk_auto_reconnect.setChecked(bool(data.get("auto_reconnect", False)))
            self._chk_auto_reconnect_on_clean_exit.setChecked(
                bool(data.get("auto_reconnect_on_clean_exit", False))
            )

            backoff = data.get("reconnect_backoff_ms", [1000, 3000, 10000, 30000])
            if isinstance(backoff, list):
                self._edit_reconnect_backoff_ms.setText(",".join(str(v) for v in backoff))
            self._spin_reconnect_max_attempts.setValue(
                int(data.get("reconnect_max_attempts", 0) or 0)
            )

            env = data.get("env", {})
            if isinstance(env, dict):
                self._edit_env.setPlainText("\n".join(f"{k}={v}" for k, v in env.items()))
            pre = data.get("pre_commands", [])
            if isinstance(pre, list):
                self._edit_pre_commands.setPlainText("\n".join(pre))
            post = data.get("post_commands", [])
            if isinstance(post, list):
                self._edit_post_commands.setPlainText("\n".join(post))

            tags = data.get("tags", [])
            if isinstance(tags, list):
                self._edit_tags.setText(", ".join(tags))
            notes = data.get("notes", "")
            self._edit_notes.setPlainText(str(notes or ""))

        finally:
            self._suppress_confirm = False

        self._run_validation()

    def collect_data(self) -> dict[str, Any]:
        """Collect all field values into a flat dict.

        Returns only fields that are relevant to the current profile.
        """
        profile = self._current_profile
        data: dict[str, Any] = {"launch_profile": profile}

        if _field_visible("host", profile):
            data["host"] = self._edit_host.text().strip() or None
        if _field_visible("port", profile):
            data["port"] = self._spin_port.value()
        if _field_visible("user", profile):
            data["user"] = self._edit_user.text().strip() or None
        if _field_visible("sudo_user", profile):
            val = self._edit_sudo_user.text().strip()
            data["sudo_user"] = val or None
        if _field_visible("identity_file_ref", profile):
            val = self._combo_identity_file_ref.currentText().strip()
            data["identity_file_ref"] = val or None
            # auth_method ships alongside identity_file_ref since both
            # belong to SSH-using profiles (claude-remote, ssh-shell, custom).
            data["auth_method"] = self._combo_auth_method.currentData() or "ask"
        if _field_visible("jump_host", profile):
            val = self._combo_jump_host.currentText().strip()
            data["jump_host"] = val or None
        if _field_visible("project_folder", profile):
            val = self._edit_project_folder.text().strip()
            data["project_folder"] = val or None
        if _field_visible("claude_options", profile):
            val = self._edit_claude_options.text().strip()
            data["claude_options"] = val or None
        if _field_visible("custom_template_id", profile):
            val = self._combo_custom_template_id.currentText().strip()
            data["custom_template_id"] = val or None
        if _field_visible("keepalive_interval_s", profile):
            v = self._spin_keepalive_interval_s.value()
            data["keepalive_interval_s"] = v if v > 0 else None
        if _field_visible("connection_timeout_s", profile):
            v = self._spin_connection_timeout_s.value()
            data["connection_timeout_s"] = v if v > 0 else None

        # Always-present fields
        data["auto_reconnect"] = self._chk_auto_reconnect.isChecked()
        data["auto_reconnect_on_clean_exit"] = self._chk_auto_reconnect_on_clean_exit.isChecked()
        backoff_text = self._edit_reconnect_backoff_ms.text().strip()
        if backoff_text:
            try:
                data["reconnect_backoff_ms"] = [
                    int(v.strip()) for v in backoff_text.split(",") if v.strip()
                ]
            except ValueError:
                data["reconnect_backoff_ms"] = [1000, 3000, 10000, 30000]
        else:
            data["reconnect_backoff_ms"] = [1000, 3000, 10000, 30000]
        data["reconnect_max_attempts"] = self._spin_reconnect_max_attempts.value()

        # Env
        env_text = self._edit_env.toPlainText().strip()
        env: dict[str, str] = {}
        for line in env_text.splitlines():
            eq_pos = line.find("=")
            if eq_pos >= 0:
                env_k = line[:eq_pos].strip()
                env_v = line[eq_pos + 1 :]
                if env_k:
                    env[env_k] = env_v
        data["env"] = env

        # Commands
        pre_text = self._edit_pre_commands.toPlainText().strip()
        data["pre_commands"] = (
            [ln for ln in pre_text.splitlines() if ln.strip()] if pre_text else []
        )
        post_text = self._edit_post_commands.toPlainText().strip()
        data["post_commands"] = (
            [ln for ln in post_text.splitlines() if ln.strip()] if post_text else []
        )

        tags_text = self._edit_tags.text().strip()
        data["tags"] = [t.strip() for t in tags_text.split(",") if t.strip()] if tags_text else []
        notes = self._edit_notes.toPlainText().strip()
        data["notes"] = notes or None

        return data
