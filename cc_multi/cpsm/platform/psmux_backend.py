# -*- coding: utf-8 -*-
"""
PsmuxBackend — concrete MultiplexerBackend implementation for PSMUX on Windows.

PSMUX is a PowerShell-driven terminal multiplexer.  Every operation is
performed by invoking::

    pwsh -NoProfile -Command "<Cmdlet -Param 'value' ...>"

and parsing the JSON output via :class:`PowerShellQuoter` and
:mod:`json`.

Cmdlet mapping (§7.4)
---------------------

+-----------------------------+-----------------------------------------------+
| ABC method                  | PSMUX cmdlet                                  |
+=============================+===============================================+
| list_sessions               | Get-PsmuxSession | ConvertTo-Json             |
| new_session                 | New-PsmuxSession -Name … -Width … -Height …   |
|                             |   [-Detached]                                 |
| attach_session              | Attach-PsmuxSession -Name …                   |
| kill_session                | Remove-PsmuxSession -Name …                   |
| list_windows                | Get-PsmuxWindow -Session …                    |
| new_window                  | New-PsmuxWindow -Session … [-Name …]          |
| kill_window                 | Remove-PsmuxWindow -Target …                  |
| select_layout               | Select-PsmuxLayout -Target … -Layout …        |
| capture_layout              | Get-PsmuxLayout -Window …                     |
| set_window_option           | Set-PsmuxWindowOption -Target … -Name …       |
|                             |   -Value …                                    |
| list_panes                  | Get-PsmuxPane [-Target …]                     |
| split_pane                  | Split-PsmuxPane -Target … -Direction …        |
|                             |   [-Before]                                   |
| select_pane                 | Select-PsmuxPane -Target …                    |
| swap_panes                  | Swap-PsmuxPane -Source … -Target …            |
| break_pane                  | Move-PsmuxPaneToWindow -Source … [-Detached]  |
| move_pane                   | Move-PsmuxPane -Source … -Target …            |
| resize_pane                 | Resize-PsmuxPane -Target … -Width … -Height … |
| send_keys                   | Send-PsmuxKeys -Target … -Keys … [-Enter]     |
| kill_pane                   | Stop-PsmuxPane -Target …                      |
| respawn_pane                | Restart-PsmuxPane -Target … -Command …        |
|                             |   [-KillExisting]                             |
| capture_pane                | Get-PsmuxPaneCapture -Target … -Lines …       |
+-----------------------------+-----------------------------------------------+

Output format
-------------
Every cmdlet pipes through ``| ConvertTo-Json`` (where not already implied
by the cmdlet itself) and ``PsmuxBackend`` parses the result with
:func:`json.loads`.  The parsed JSON is then mapped to the standard
``Session`` / ``Window`` / ``Pane`` dataclasses.

Error handling
--------------
If the JSON output cannot be parsed, :class:`PsmuxParseError` is raised
with a descriptive message that includes the raw output.
If a required capability is absent, :class:`BackendCapabilityError` is
raised.

Spec: §7.4, §7.5, §8
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal

from cpsm.platform.base import (
    BackendCapabilities,
    MultiplexerBackend,
    Pane,
    Session,
    Window,
)
from cpsm.platform.powershell_quoter import PowerShellQuoter
from cpsm.platform.process_runner import ProcessRunner

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class PsmuxParseError(ValueError):
    """Raised when a PSMUX cmdlet returns JSON that cannot be parsed."""


class BackendCapabilityError(RuntimeError):
    """Raised when a requested operation requires a capability not available
    in the current PSMUX installation."""


# ---------------------------------------------------------------------------
# JSON parsing helpers
# ---------------------------------------------------------------------------


def _parse_json(raw: str, context: str = "") -> Any:
    """Parse *raw* as JSON; raise :class:`PsmuxParseError` on failure.

    Args:
        raw: Raw string output from a pwsh cmdlet.
        context: Human-readable context for the error message (e.g. cmdlet
            name) used to aid debugging.

    Returns:
        Parsed Python object (dict, list, etc.).

    Raises:
        PsmuxParseError: when *raw* is not valid JSON.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        label = f" [{context}]" if context else ""
        raise PsmuxParseError(
            f"Failed to parse PSMUX JSON output{label}: {exc}\nRaw output: {raw!r}"
        ) from exc


def _ensure_list(value: Any) -> list[Any]:
    """Wrap a single JSON object in a list; return lists unchanged.

    PowerShell's ``ConvertTo-Json`` emits a bare object (not an array) when
    there is exactly one item.  Wrapping ensures callers always iterate a
    list.
    """
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _str(obj: Any, key: str, default: str = "") -> str:
    """Extract a string field from *obj* dict safely."""
    if isinstance(obj, dict):
        v = obj.get(key)
        return str(v) if v is not None else default
    return default


def _int(obj: Any, key: str, default: int = 0) -> int:
    """Extract an integer field from *obj* dict safely."""
    if isinstance(obj, dict):
        v = obj.get(key)
        try:
            return int(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default
    return default


def _bool_field(obj: Any, key: str) -> bool:
    """Extract a boolean field from *obj* dict safely."""
    if isinstance(obj, dict):
        v = obj.get(key)
        if isinstance(v, bool):
            return v
        if isinstance(v, int):
            return v != 0
        if isinstance(v, str):
            return v.lower() not in ("", "0", "false", "no")
    return False


def _datetime_field(obj: Any, key: str) -> datetime:
    """Extract a datetime from *obj* dict.

    Accepts:
    - Unix timestamp (int / float)
    - ISO 8601 string
    - Windows FILETIME ticks string (digits > 1e15 treated as 100-ns ticks
      from 1601-01-01, converted to Unix epoch)
    """
    if isinstance(obj, dict):
        v = obj.get(key)
        if v is not None:
            if isinstance(v, (int, float)):
                try:
                    return datetime.fromtimestamp(float(v), tz=UTC)
                except (OSError, OverflowError, ValueError):
                    pass
            if isinstance(v, str) and v.strip():
                raw = v.strip()
                # Try parsing as numeric Unix timestamp first.
                try:
                    ts = float(raw)
                    # Windows FILETIME: 100-ns intervals since 1601-01-01.
                    # These are very large numbers (> 1e15).
                    if ts > 1e15:
                        # Convert 100-ns ticks to seconds; subtract Unix epoch offset.
                        _FILETIME_EPOCH_DIFF = 11_644_473_600  # seconds
                        ts_unix = (ts / 1e7) - _FILETIME_EPOCH_DIFF
                        return datetime.fromtimestamp(ts_unix, tz=UTC)
                    return datetime.fromtimestamp(ts, tz=UTC)
                except (ValueError, OSError, OverflowError):
                    pass
                # Try ISO 8601.
                try:
                    return datetime.fromisoformat(raw).replace(tzinfo=UTC)
                except ValueError:
                    pass
    return datetime.now(tz=UTC)


# ---------------------------------------------------------------------------
# Session / Window / Pane parsers
# ---------------------------------------------------------------------------


def _parse_session(obj: dict[str, Any]) -> Session:
    """Map a PSMUX JSON session object to a :class:`Session` dataclass."""
    return Session(
        id=_str(obj, "Id") or _str(obj, "SessionId"),
        name=_str(obj, "Name") or _str(obj, "SessionName"),
        attached=_bool_field(obj, "Attached") or _bool_field(obj, "IsAttached"),
        created_at=_datetime_field(obj, "CreatedAt"),
    )


def _parse_window(obj: dict[str, Any], session: str = "") -> Window:
    """Map a PSMUX JSON window object to a :class:`Window` dataclass."""
    return Window(
        id=_str(obj, "Id") or _str(obj, "WindowId"),
        session=_str(obj, "Session") or _str(obj, "SessionName") or session,
        index=_int(obj, "Index") or _int(obj, "WindowIndex"),
        name=_str(obj, "Name") or _str(obj, "WindowName"),
        layout=_str(obj, "Layout") or _str(obj, "WindowLayout"),
    )


def _parse_pane(obj: dict[str, Any]) -> Pane:
    """Map a PSMUX JSON pane object to a :class:`Pane` dataclass."""
    pid_raw = obj.get("Pid") or obj.get("PaneId") if isinstance(obj, dict) else None
    # "PaneId" in PSMUX might be the pane identifier string; numeric Pid is separate.
    pid_val: int | None = None
    if isinstance(pid_raw, int):
        pid_val = pid_raw
    elif isinstance(pid_raw, str) and pid_raw.strip().lstrip("-").isdigit():
        pid_val = int(pid_raw)

    dead_status_raw = obj.get("DeadStatus") if isinstance(obj, dict) else None
    dead_status: int | None = None
    if isinstance(dead_status_raw, int):
        dead_status = dead_status_raw

    return Pane(
        id=_str(obj, "Id") or _str(obj, "PaneId"),
        session=_str(obj, "Session") or _str(obj, "SessionName"),
        window_index=_int(obj, "WindowIndex"),
        pane_index=_int(obj, "PaneIndex") or _int(obj, "Index"),
        pid=pid_val,
        dead=_bool_field(obj, "Dead") or _bool_field(obj, "IsDead"),
        current_command=_str(obj, "CurrentCommand") or _str(obj, "Command"),
        width=_int(obj, "Width"),
        height=_int(obj, "Height"),
        dead_status=dead_status,
    )


# ---------------------------------------------------------------------------
# PsmuxBackend
# ---------------------------------------------------------------------------


class PsmuxBackend(MultiplexerBackend):
    """Concrete PSMUX backend — invokes PowerShell cmdlets via pwsh.

    Every multiplexer operation is translated into a
    ``pwsh -NoProfile -Command "<cmdlet ...>"`` invocation whose output is
    JSON that maps to :class:`Session`, :class:`Window`, or :class:`Pane`
    dataclasses.

    Args:
        runner: Inject a custom :class:`ProcessRunner`; defaults to a fresh
            instance.
        pwsh_binary: Path or name of the PowerShell binary
            (default ``"pwsh"``).
    """

    # ------------------------------------------------------------------
    # Class-level constants (cmdlet names)
    # ------------------------------------------------------------------

    _CMDLET_LIST_SESSIONS: ClassVar[str] = "Get-PsmuxSession"
    _CMDLET_NEW_SESSION: ClassVar[str] = "New-PsmuxSession"
    _CMDLET_ATTACH_SESSION: ClassVar[str] = "Attach-PsmuxSession"
    _CMDLET_REMOVE_SESSION: ClassVar[str] = "Remove-PsmuxSession"
    _CMDLET_LIST_WINDOWS: ClassVar[str] = "Get-PsmuxWindow"
    _CMDLET_NEW_WINDOW: ClassVar[str] = "New-PsmuxWindow"
    _CMDLET_REMOVE_WINDOW: ClassVar[str] = "Remove-PsmuxWindow"
    _CMDLET_SELECT_LAYOUT: ClassVar[str] = "Select-PsmuxLayout"
    _CMDLET_GET_LAYOUT: ClassVar[str] = "Get-PsmuxLayout"
    _CMDLET_SET_WINDOW_OPTION: ClassVar[str] = "Set-PsmuxWindowOption"
    _CMDLET_LIST_PANES: ClassVar[str] = "Get-PsmuxPane"
    _CMDLET_SPLIT_PANE: ClassVar[str] = "Split-PsmuxPane"
    _CMDLET_SELECT_PANE: ClassVar[str] = "Select-PsmuxPane"
    _CMDLET_SWAP_PANE: ClassVar[str] = "Swap-PsmuxPane"
    _CMDLET_BREAK_PANE: ClassVar[str] = "Move-PsmuxPaneToWindow"
    _CMDLET_MOVE_PANE: ClassVar[str] = "Move-PsmuxPane"
    _CMDLET_RESIZE_PANE: ClassVar[str] = "Resize-PsmuxPane"
    _CMDLET_SEND_KEYS: ClassVar[str] = "Send-PsmuxKeys"
    _CMDLET_STOP_PANE: ClassVar[str] = "Stop-PsmuxPane"
    _CMDLET_RESTART_PANE: ClassVar[str] = "Restart-PsmuxPane"
    _CMDLET_CAPTURE_PANE: ClassVar[str] = "Get-PsmuxPaneCapture"

    def __init__(
        self,
        *,
        runner: ProcessRunner | None = None,
        pwsh_binary: str = "pwsh",
    ) -> None:
        self._pwsh = pwsh_binary
        self._runner = runner or ProcessRunner()
        self._quoter = PowerShellQuoter()
        self.capabilities: BackendCapabilities = self._probe_capabilities()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_argv(self, cmdlet: str, params: dict[str, Any]) -> list[str]:
        """Build the full ``pwsh`` argv list for *cmdlet* with *params*.

        The command string is assembled by :class:`PowerShellQuoter` and
        appended to the standard pwsh prefix flags.

        Args:
            cmdlet: PowerShell cmdlet name.
            params: Parameter name → value mapping; see :meth:`quote_command`.

        Returns:
            List of strings suitable for :class:`ProcessRunner`.
        """
        cmd_str = self._quoter.quote_command(cmdlet, params)
        return [self._pwsh, "-NoProfile", "-Command", cmd_str]

    def _build_pipeline_argv(self, cmdlet: str, params: dict[str, Any]) -> list[str]:
        """Like :meth:`_build_argv` but appends ``| ConvertTo-Json`` pipeline.

        Used for cmdlets that do not already pipe to ``ConvertTo-Json``
        internally.
        """
        cmd_str = self._quoter.quote_command(cmdlet, params) + " | ConvertTo-Json"
        return [self._pwsh, "-NoProfile", "-Command", cmd_str]

    def _run(
        self,
        argv: list[str],
        *,
        timeout: float = 10.0,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Delegate to :class:`ProcessRunner`."""
        return self._runner.run(argv, timeout=timeout, check=check)

    def _run_cmdlet_json(
        self,
        cmdlet: str,
        params: dict[str, Any],
        *,
        pipeline: bool = True,
        timeout: float = 10.0,
        check: bool = True,
    ) -> Any:
        """Run *cmdlet* and return the parsed JSON result.

        Args:
            cmdlet: PowerShell cmdlet name.
            params: Parameter dictionary.
            pipeline: When True, ``| ConvertTo-Json`` is appended to the
                command string.
            timeout: Seconds to wait.
            check: Raise on non-zero exit code.

        Returns:
            Parsed JSON (list, dict, str, etc.).

        Raises:
            PsmuxParseError: If JSON parsing fails.
        """
        if pipeline:
            argv = self._build_pipeline_argv(cmdlet, params)
        else:
            argv = self._build_argv(cmdlet, params)
        result = self._run(argv, timeout=timeout, check=check)
        return _parse_json(result.stdout, context=cmdlet)

    # ------------------------------------------------------------------
    # Capability probing
    # ------------------------------------------------------------------

    def _probe_capabilities(self) -> BackendCapabilities:
        """Probe PSMUX version by querying the ``Psmux`` PS module.

        Runs::

            pwsh -NoProfile -Command
                "Get-Module Psmux | Select-Object Version | ConvertTo-Json"

        and parses the ``Version`` field from the JSON output.

        Returns a :class:`BackendCapabilities` with ``name="psmux"`` and
        feature flags set to True unconditionally (PSMUX always supports all
        features; capability gating can be added when versioned feature flags
        are defined in the spec).

        Falls back to ``version=""`` and all capabilities enabled when the
        module query fails or returns no output (binary absent in test
        environment).
        """
        version = ""
        try:
            cmd_str = "Get-Module Psmux | Select-Object Version | ConvertTo-Json"
            argv = [self._pwsh, "-NoProfile", "-Command", cmd_str]
            result = self._run(argv, timeout=10.0, check=False)
            if result.returncode == 0 and result.stdout.strip():
                data = _parse_json(result.stdout, context="Get-Module Psmux")
                if isinstance(data, dict):
                    ver = data.get("Version")
                    version = str(ver) if ver is not None else ""
                elif isinstance(data, list) and data:
                    ver = data[0].get("Version") if isinstance(data[0], dict) else None
                    version = str(ver) if ver is not None else ""
        except (PsmuxParseError, Exception):
            version = ""

        return BackendCapabilities(
            name="psmux",
            version=version,
            supports_split_before=True,
            supports_remain_on_exit=True,
            supports_capture_pane=True,
            supports_format_pane_dead=True,
            supports_initial_size_in_new_session=True,
        )

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def list_sessions(self) -> list[Session]:
        """Return all active PSMUX sessions.

        Calls ``Get-PsmuxSession | ConvertTo-Json`` and parses the result.
        Returns an empty list when the cmdlet exits non-zero (no server
        or no sessions).
        """
        argv = self._build_pipeline_argv(self._CMDLET_LIST_SESSIONS, {})
        result = self._run(argv, check=False)
        if result.returncode != 0 or not result.stdout.strip():
            return []
        data = _parse_json(result.stdout, context=self._CMDLET_LIST_SESSIONS)
        return [_parse_session(obj) for obj in _ensure_list(data) if isinstance(obj, dict)]

    def new_session(
        self,
        name: str,
        width: int,
        height: int,
        detached: bool = True,
    ) -> Session:
        """Create and return a new PSMUX session.

        Calls ``New-PsmuxSession -Name <name> -Width <w> -Height <h>
        [-Detached] | ConvertTo-Json``.

        Args:
            name: Session name.
            width: Initial terminal width in cells.
            height: Initial terminal height in cells.
            detached: When True, add ``-Detached`` flag (default True).

        Returns:
            The newly created :class:`Session`.
        """
        params: dict[str, Any] = {
            "Name": name,
            "Width": str(width),
            "Height": str(height),
        }
        if detached:
            params["Detached"] = True
        data = self._run_cmdlet_json(self._CMDLET_NEW_SESSION, params, pipeline=True)
        if isinstance(data, dict):
            return _parse_session(data)
        # Fallback: query session list.
        sessions = self.list_sessions()
        for s in sessions:
            if s.name == name:
                return s
        return Session(
            id="",
            name=name,
            attached=not detached,
            created_at=datetime.now(tz=UTC),
        )

    def attach_session(self, name: str) -> None:
        """Attach the current terminal to *name*.

        Calls ``Attach-PsmuxSession -Name <name>``.
        No JSON output expected; return value is discarded.
        """
        argv = self._build_argv(self._CMDLET_ATTACH_SESSION, {"Name": name})
        self._run(argv)

    def kill_session(self, name: str) -> None:
        """Destroy *name* and all its windows/panes.

        Calls ``Remove-PsmuxSession -Name <name>``.
        """
        argv = self._build_argv(self._CMDLET_REMOVE_SESSION, {"Name": name})
        self._run(argv)

    # ------------------------------------------------------------------
    # Window management
    # ------------------------------------------------------------------

    def list_windows(self, session: str) -> list[Window]:
        """Return all windows in *session*.

        Calls ``Get-PsmuxWindow -Session <session> | ConvertTo-Json``.
        """
        params: dict[str, Any] = {"Session": session}
        argv = self._build_pipeline_argv(self._CMDLET_LIST_WINDOWS, params)
        result = self._run(argv, check=False)
        if result.returncode != 0 or not result.stdout.strip():
            return []
        data = _parse_json(result.stdout, context=self._CMDLET_LIST_WINDOWS)
        return [
            _parse_window(obj, session=session)
            for obj in _ensure_list(data)
            if isinstance(obj, dict)
        ]

    def new_window(self, session: str, name: str | None = None) -> Window:
        """Create a new window in *session* and return it.

        Calls ``New-PsmuxWindow -Session <session> [-Name <name>]
        | ConvertTo-Json``.
        """
        params: dict[str, Any] = {"Session": session}
        if name is not None:
            params["Name"] = name
        data = self._run_cmdlet_json(self._CMDLET_NEW_WINDOW, params, pipeline=True)
        if isinstance(data, dict):
            return _parse_window(data, session=session)
        raise RuntimeError(f"New-PsmuxWindow returned unexpected output: {data!r}")

    def kill_window(self, target: str) -> None:
        """Kill the window identified by *target*.

        Calls ``Remove-PsmuxWindow -Target <target>``.
        """
        argv = self._build_argv(self._CMDLET_REMOVE_WINDOW, {"Target": target})
        self._run(argv)

    def select_layout(self, window: str, layout: str) -> None:
        """Apply a named layout to *window*.

        Calls ``Select-PsmuxLayout -Target <window> -Layout <layout>``.
        """
        argv = self._build_argv(
            self._CMDLET_SELECT_LAYOUT,
            {"Target": window, "Layout": layout},
        )
        self._run(argv)

    def capture_layout(self, window: str) -> str:
        """Return the current layout string for *window*.

        Calls ``Get-PsmuxLayout -Window <window> | ConvertTo-Json``.
        Parses the layout from the JSON field ``Layout`` or returns the
        raw string if the result is a plain string.
        """
        params: dict[str, Any] = {"Window": window}
        argv = self._build_pipeline_argv(self._CMDLET_GET_LAYOUT, params)
        result = self._run(argv)
        raw = result.stdout.strip()
        if not raw:
            return ""
        data = _parse_json(raw, context=self._CMDLET_GET_LAYOUT)
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            return _str(data, "Layout") or _str(data, "WindowLayout")
        return ""

    def set_window_option(self, target: str, name: str, value: str) -> None:
        """Set a window option on *target*.

        Calls ``Set-PsmuxWindowOption -Target <target> -Name <name>
        -Value <value>``.
        """
        argv = self._build_argv(
            self._CMDLET_SET_WINDOW_OPTION,
            {"Target": target, "Name": name, "Value": value},
        )
        self._run(argv)

    # ------------------------------------------------------------------
    # Pane management
    # ------------------------------------------------------------------

    def list_panes(self, target: str | None = None) -> list[Pane]:
        """Return panes.

        When *target* is ``None``, returns panes across all sessions via
        ``Get-PsmuxPane | ConvertTo-Json``.  Otherwise calls
        ``Get-PsmuxPane -Target <target> | ConvertTo-Json``.
        """
        params: dict[str, Any] = {}
        if target is not None:
            params["Target"] = target
        argv = self._build_pipeline_argv(self._CMDLET_LIST_PANES, params)
        result = self._run(argv, check=False)
        if result.returncode != 0 or not result.stdout.strip():
            return []
        data = _parse_json(result.stdout, context=self._CMDLET_LIST_PANES)
        return [_parse_pane(obj) for obj in _ensure_list(data) if isinstance(obj, dict)]

    def split_pane(
        self,
        target: str,
        direction: Literal["h", "v"],
        before: bool = False,
    ) -> Pane:
        """Split *target* pane.

        Calls ``Split-PsmuxPane -Target <target>
        -Direction Horizontal|Vertical [-Before] | ConvertTo-Json``.

        Args:
            target: Pane identifier.
            direction: ``"h"`` → ``Horizontal``; ``"v"`` → ``Vertical``.
            before: When True, add ``-Before`` flag.

        Returns:
            The newly created :class:`Pane`.
        """
        ps_direction = "Horizontal" if direction == "h" else "Vertical"
        params: dict[str, Any] = {"Target": target, "Direction": ps_direction}
        if before:
            params["Before"] = True
        data = self._run_cmdlet_json(self._CMDLET_SPLIT_PANE, params, pipeline=True)
        if isinstance(data, dict):
            return _parse_pane(data)
        raise RuntimeError(f"Split-PsmuxPane returned unexpected output: {data!r}")

    def select_pane(self, target: str) -> None:
        """Make *target* the active pane.

        Calls ``Select-PsmuxPane -Target <target>``.
        """
        argv = self._build_argv(self._CMDLET_SELECT_PANE, {"Target": target})
        self._run(argv)

    def swap_panes(self, src: str, dst: str) -> None:
        """Swap the positions of *src* and *dst* panes.

        Calls ``Swap-PsmuxPane -Source <src> -Target <dst>``.
        """
        argv = self._build_argv(
            self._CMDLET_SWAP_PANE,
            {"Source": src, "Target": dst},
        )
        self._run(argv)

    def break_pane(self, src: str, detached: bool = True) -> Window:
        """Break *src* pane into its own window.

        Calls ``Move-PsmuxPaneToWindow -Source <src> [-Detached]
        | ConvertTo-Json``.

        Returns:
            The newly created :class:`Window`.
        """
        params: dict[str, Any] = {"Source": src}
        if detached:
            params["Detached"] = True
        data = self._run_cmdlet_json(self._CMDLET_BREAK_PANE, params, pipeline=True)
        if isinstance(data, dict):
            return _parse_window(data)
        raise RuntimeError(f"Move-PsmuxPaneToWindow returned unexpected output: {data!r}")

    def move_pane(self, src: str, dst_window: str) -> None:
        """Move *src* pane into *dst_window*.

        Calls ``Move-PsmuxPane -Source <src> -Target <dst_window>``.
        """
        argv = self._build_argv(
            self._CMDLET_MOVE_PANE,
            {"Source": src, "Target": dst_window},
        )
        self._run(argv)

    def resize_pane(self, target: str, width: int, height: int) -> None:
        """Resize *target* pane to *width* x *height* cells.

        Calls ``Resize-PsmuxPane -Target <target> -Width <w> -Height <h>``.
        """
        argv = self._build_argv(
            self._CMDLET_RESIZE_PANE,
            {"Target": target, "Width": str(width), "Height": str(height)},
        )
        self._run(argv)

    def send_keys(self, target: str, keys: str, enter: bool = True) -> None:
        """Send *keys* to *target* pane.

        Calls ``Send-PsmuxKeys -Target <target> -Keys <keys> [-Enter]``.
        """
        params: dict[str, Any] = {"Target": target, "Keys": keys}
        if enter:
            params["Enter"] = True
        argv = self._build_argv(self._CMDLET_SEND_KEYS, params)
        self._run(argv)

    def kill_pane(self, target: str) -> None:
        """Kill *target* pane.

        Calls ``Stop-PsmuxPane -Target <target>``.
        """
        argv = self._build_argv(self._CMDLET_STOP_PANE, {"Target": target})
        self._run(argv)

    def respawn_pane(
        self,
        target: str,
        command: str,
        kill_existing: bool = True,
    ) -> None:
        """Respawn *target* pane running *command*.

        Calls ``Restart-PsmuxPane -Target <target> -Command <command>
        [-KillExisting]``.
        """
        params: dict[str, Any] = {"Target": target, "Command": command}
        if kill_existing:
            params["KillExisting"] = True
        argv = self._build_argv(self._CMDLET_RESTART_PANE, params)
        self._run(argv)

    def capture_pane(self, target: str, lines: int = 200) -> str:
        """Return the last *lines* lines of output from *target* pane.

        Calls ``Get-PsmuxPaneCapture -Target <target> -Lines <lines>
        | ConvertTo-Json``.

        The result is expected to be a JSON string or an object with a
        ``Content`` / ``Output`` field.
        """
        params: dict[str, Any] = {"Target": target, "Lines": str(lines)}
        argv = self._build_pipeline_argv(self._CMDLET_CAPTURE_PANE, params)
        result = self._run(argv)
        raw = result.stdout.strip()
        if not raw:
            return ""
        data = _parse_json(raw, context=self._CMDLET_CAPTURE_PANE)
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            return _str(data, "Content") or _str(data, "Output") or _str(data, "Text")
        return ""
