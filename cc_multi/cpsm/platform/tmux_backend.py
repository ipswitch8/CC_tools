# -*- coding: utf-8 -*-
"""
TmuxBackend — concrete MultiplexerBackend implementation for tmux.

Spec: §7.1, §7.2, §5.7, §5.3
"""

from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Literal

from cpsm.platform.base import (
    BackendCapabilities,
    MultiplexerBackend,
    Pane,
    Session,
    Window,
)
from cpsm.platform.process_runner import ProcessRunner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NUMERIC_FIELDS = frozenset(
    {"pane_pid", "pane_width", "pane_height", "window_index", "pane_index", "pane_dead_status"}
)
_BOOL_FIELDS = frozenset({"pane_dead", "session_attached"})


def _parse_int(value: str) -> int | None:
    """Return int from *value* or None for empty / non-numeric strings."""
    v = value.strip()
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def _parse_bool(value: str) -> bool:
    """Return False for empty / "0", True for any other non-empty value."""
    v = value.strip()
    return not (not v or v == "0")


def _parse_format(output: str, fields: list[str]) -> list[dict[str, str | int | bool | None]]:
    """Split each non-empty line of *output* on ``|`` and zip with *fields*.

    Numeric fields (pane_pid, pane_width, pane_height, window_index,
    pane_index) are coerced to ``int | None``.  Boolean fields
    (pane_dead, session_attached) are coerced to ``bool``.
    """
    records: list[dict[str, str | int | bool | None]] = []
    for raw_line in output.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            continue
        parts = line.split("|")
        # Pad or trim to match fields length so zip always covers all fields.
        while len(parts) < len(fields):
            parts.append("")
        row: dict[str, str | int | bool | None] = {}
        for field, part in zip(fields, parts, strict=False):
            if field in _NUMERIC_FIELDS:
                row[field] = _parse_int(part)
            elif field in _BOOL_FIELDS:
                row[field] = _parse_bool(part)
            else:
                row[field] = part
        records.append(row)
    return records


def _version_at_least(version_string: str, min_version: str) -> bool:
    """Return True if *version_string* (e.g. ``"tmux 3.4"``) >= *min_version*.

    Handles trailing alpha chars in the patch segment (e.g. ``"3.5a"``).
    """
    match = re.search(r"(\d+)\.(\d+)([a-zA-Z]?)", version_string)
    if not match:
        return False
    major, minor = int(match.group(1)), int(match.group(2))
    # Trailing alpha chars are ignored — they indicate release candidates /
    # patch builds that are >= the numeric version.

    min_match = re.search(r"(\d+)\.(\d+)", min_version)
    if not min_match:
        return False
    min_major, min_minor = int(min_match.group(1)), int(min_match.group(2))
    return (major, minor) >= (min_major, min_minor)


# ---------------------------------------------------------------------------
# TmuxBackend
# ---------------------------------------------------------------------------


class TmuxBackend(MultiplexerBackend):
    """Concrete tmux backend.

    Runs every operation via :class:`ProcessRunner` using the tmux binary.
    Capabilities are probed at construction time by running ``tmux -V``.

    Args:
        socket_path: If provided, every tmux invocation adds ``-S <path>``.
        runner: Inject a custom :class:`ProcessRunner`; defaults to a fresh
            instance.
        tmux_binary: Path or name of the tmux binary (default ``"tmux"``).
    """

    # ------------------------------------------------------------------
    # Format-string constants (ClassVar to satisfy ruff RUF012)
    # ------------------------------------------------------------------

    _SESSION_FIELDS: ClassVar[list[str]] = [
        "session_id",
        "session_name",
        "session_attached",
        "session_created",
    ]
    _SESSION_FMT: ClassVar[str] = (
        "#{session_id}|#{session_name}|#{session_attached}|#{session_created}"
    )

    _WINDOW_FIELDS: ClassVar[list[str]] = [
        "window_id",
        "window_index",
        "window_name",
        "window_layout",
    ]
    _WINDOW_FMT: ClassVar[str] = "#{window_id}|#{window_index}|#{window_name}|#{window_layout}"

    _PANE_FIELDS: ClassVar[list[str]] = [
        "session_name",
        "window_index",
        "pane_index",
        "pane_id",
        "pane_pid",
        "pane_dead",
        "pane_current_command",
        "pane_width",
        "pane_height",
        "pane_dead_status",
        "pane_title",
        "pane_start_command",
    ]
    _PANE_FMT: ClassVar[str] = (
        "#{session_name}|#{window_index}|#{pane_index}|#{pane_id}"
        "|#{pane_pid}|#{pane_dead}|#{pane_current_command}"
        "|#{pane_width}|#{pane_height}|#{pane_dead_status}|#{pane_title}"
        "|#{pane_start_command}"
    )

    # Pane fields returned by split-window / join-pane / break-pane -P
    _SPLIT_PANE_FIELDS: ClassVar[list[str]] = [
        "pane_id",
        "session_name",
        "window_index",
        "pane_index",
        "pane_pid",
        "pane_dead",
        "pane_current_command",
        "pane_width",
        "pane_height",
        "pane_dead_status",
        "pane_title",
        "pane_start_command",
    ]
    _SPLIT_PANE_FMT: ClassVar[str] = (
        "#{pane_id}|#{session_name}|#{window_index}|#{pane_index}"
        "|#{pane_pid}|#{pane_dead}|#{pane_current_command}"
        "|#{pane_width}|#{pane_height}|#{pane_dead_status}|#{pane_title}"
        "|#{pane_start_command}"
    )

    def __init__(
        self,
        *,
        socket_path: Path | None = None,
        runner: ProcessRunner | None = None,
        tmux_binary: str = "tmux",
    ) -> None:
        self._tmux = tmux_binary
        self._socket_path = socket_path
        self._runner = runner or ProcessRunner()
        self.capabilities: BackendCapabilities = self._probe_capabilities()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _prefix(self) -> list[str]:
        """Return the argv prefix for every tmux invocation."""
        if self._socket_path is not None:
            return [self._tmux, "-S", str(self._socket_path)]
        return [self._tmux]

    def _run(
        self,
        args: list[str],
        *,
        timeout: float = 5.0,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Build the full argv and delegate to :class:`ProcessRunner`."""
        return self._runner.run(
            [*self._prefix(), *args],
            timeout=timeout,
            check=check,
        )

    def _probe_capabilities(self) -> BackendCapabilities:
        """Probe tmux version and populate :class:`BackendCapabilities`."""
        result = self._runner.run(
            [*self._prefix(), "-V"],
            timeout=5.0,
            check=False,
        )
        version = result.stdout.strip()  # e.g. "tmux 3.4"

        return BackendCapabilities(
            name="tmux",
            version=version,
            supports_split_before=_version_at_least(version, "1.8"),
            supports_remain_on_exit=_version_at_least(version, "1.8"),
            supports_capture_pane=_version_at_least(version, "1.5"),
            supports_format_pane_dead=_version_at_least(version, "1.6"),
        )

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_sessions(self, output: str) -> list[Session]:
        sessions = []
        for row in _parse_format(output, self._SESSION_FIELDS):
            ts_raw = str(row.get("session_created") or "")
            try:
                created_at = datetime.fromtimestamp(int(ts_raw), tz=UTC)
            except (ValueError, TypeError):
                created_at = datetime.now(tz=UTC)
            sessions.append(
                Session(
                    id=str(row.get("session_id") or ""),
                    name=str(row.get("session_name") or ""),
                    attached=bool(row.get("session_attached")),
                    created_at=created_at,
                )
            )
        return sessions

    def _parse_windows(self, session: str, output: str) -> list[Window]:
        windows = []
        for row in _parse_format(output, self._WINDOW_FIELDS):
            windows.append(
                Window(
                    id=str(row.get("window_id") or ""),
                    session=session,
                    index=int(row.get("window_index") or 0),
                    name=str(row.get("window_name") or ""),
                    layout=str(row.get("window_layout") or ""),
                )
            )
        return windows

    def _parse_panes_from_fields(self, output: str, fields: list[str]) -> list[Pane]:
        panes = []
        for row in _parse_format(output, fields):
            pid_raw = row.get("pane_pid")
            dead_status_raw = row.get("pane_dead_status")
            panes.append(
                Pane(
                    id=str(row.get("pane_id") or ""),
                    session=str(row.get("session_name") or ""),
                    window_index=int(row.get("window_index") or 0),
                    pane_index=int(row.get("pane_index") or 0),
                    pid=pid_raw if isinstance(pid_raw, int) else None,
                    dead=bool(row.get("pane_dead")),
                    current_command=str(row.get("pane_current_command") or ""),
                    width=int(row.get("pane_width") or 0),
                    height=int(row.get("pane_height") or 0),
                    dead_status=dead_status_raw if isinstance(dead_status_raw, int) else None,
                    title=str(row.get("pane_title") or ""),
                    start_command=str(row.get("pane_start_command") or ""),
                )
            )
        return panes

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def list_sessions(self) -> list[Session]:
        """Return all active tmux sessions."""
        result = self._run(
            ["list-sessions", "-F", self._SESSION_FMT],
            check=False,
        )
        if result.returncode != 0:
            # No sessions exist — tmux exits non-zero when server has no sessions.
            return []
        return self._parse_sessions(result.stdout)

    def new_session(
        self,
        name: str,
        width: int,
        height: int,
        detached: bool = True,
    ) -> Session:
        """Create a new tmux session and return it."""
        args = ["new-session"]
        if detached:
            args.append("-d")
        args += ["-s", name, "-x", str(width), "-y", str(height)]
        self._run(args)
        # Re-query to obtain full session data.
        sessions = self.list_sessions()
        for s in sessions:
            if s.name == name:
                return s
        # Fallback — construct a minimal Session if re-query misses it.
        return Session(
            id="",
            name=name,
            attached=not detached,
            created_at=datetime.now(tz=UTC),
        )

    def attach_session(self, name: str) -> None:
        """Attach the current terminal to *name*."""
        self._run(["attach-session", "-t", name])

    def kill_session(self, name: str) -> None:
        """Kill the named session."""
        self._run(["kill-session", "-t", name])

    # ------------------------------------------------------------------
    # Window management
    # ------------------------------------------------------------------

    def list_windows(self, session: str) -> list[Window]:
        """Return all windows in *session*."""
        result = self._run(
            ["list-windows", "-t", session, "-F", self._WINDOW_FMT],
        )
        return self._parse_windows(session, result.stdout)

    def new_window(self, session: str, name: str | None = None) -> Window:
        """Create a new window in *session* and return it."""
        args = ["new-window", "-t", session, "-P", "-F", self._WINDOW_FMT]
        if name is not None:
            args += ["-n", name]
        result = self._run(args)
        windows = self._parse_windows(session, result.stdout)
        if windows:
            return windows[0]
        raise RuntimeError(f"new-window returned no parseable output: {result.stdout!r}")

    def kill_window(self, target: str) -> None:
        """Kill the window identified by *target*."""
        self._run(["kill-window", "-t", target])

    def select_layout(self, window: str, layout: str) -> None:
        """Apply a layout preset or custom string to *window*."""
        self._run(["select-layout", "-t", window, layout])

    def capture_layout(self, window: str) -> str:
        """Return the current ``#{window_layout}`` string for *window*."""
        result = self._run(
            ["display-message", "-p", "-t", window, "#{window_layout}"],
        )
        return result.stdout.strip()

    def set_window_option(self, target: str, name: str, value: str) -> None:
        """Set a window option on *target* (e.g. ``remain-on-exit on``)."""
        self._run(["set-window-option", "-t", target, name, value])

    # ------------------------------------------------------------------
    # Pane management
    # ------------------------------------------------------------------

    def list_panes(self, target: str | None = None) -> list[Pane]:
        """Return panes.  If *target* is None, return panes across all sessions."""
        if target is None:
            args = ["list-panes", "-a", "-F", self._PANE_FMT]
        else:
            args = ["list-panes", "-t", target, "-F", self._PANE_FMT]
        result = self._run(args)
        return self._parse_panes_from_fields(result.stdout, self._PANE_FIELDS)

    def split_pane(
        self,
        target: str,
        direction: Literal["h", "v"],
        before: bool = False,
    ) -> Pane:
        """Split *target* pane.

        ``direction="h"`` → ``-h`` (side-by-side).
        ``direction="v"`` → ``-v`` (stacked).
        ``before=True`` → adds ``-b`` flag.
        """
        args = ["split-window"]
        if direction == "h":
            args.append("-h")
        else:
            args.append("-v")
        if before:
            args.append("-b")
        args += ["-t", target, "-P", "-F", self._SPLIT_PANE_FMT]
        result = self._run(args)
        panes = self._parse_panes_from_fields(result.stdout, self._SPLIT_PANE_FIELDS)
        if panes:
            return panes[0]
        raise RuntimeError(f"split-window returned no parseable output: {result.stdout!r}")

    def select_pane(self, target: str) -> None:
        """Make *target* the active pane."""
        self._run(["select-pane", "-t", target])

    def set_pane_title(self, target: str, title: str) -> None:
        """Set the displayed title of *target* pane (``select-pane -T``).

        CPSM sets ``cpsm:<connection_id>`` titles for user-visible
        labeling.  Note that long-running processes (claude, ssh) can
        overwrite the title via terminal escape sequences, so this is
        NOT a reliable identification mechanism — ownership is
        determined from ``pane_start_command`` instead.
        """
        self._run(["select-pane", "-t", target, "-T", title])

    def join_pane(
        self,
        src: str,
        target: str,
        direction: Literal["h", "v"],
        before: bool = False,
    ) -> Pane:
        """Move *src* pane into *target*'s window as a split.

        ``direction="h"`` produces a horizontal split (side-by-side);
        ``direction="v"`` produces a vertical split (stacked).
        ``before=True`` puts the moved pane before the target rather
        than after.

        The src pane's process is preserved (no kill/respawn). After a
        successful join, *src* no longer exists in its original session.
        Returns the moved Pane in its new location.
        """
        args = ["join-pane", "-s", src, "-t", target]
        args.append("-h" if direction == "h" else "-v")
        if before:
            args.append("-b")
        args += ["-d", "-P", "-F", self._SPLIT_PANE_FMT]
        result = self._run(args)
        panes = self._parse_panes_from_fields(result.stdout, self._SPLIT_PANE_FIELDS)
        if panes:
            return panes[0]
        raise RuntimeError(f"join-pane returned no parseable output: {result.stdout!r}")

    def swap_panes(self, src: str, dst: str) -> None:
        """Swap the positions of *src* and *dst* panes."""
        self._run(["swap-pane", "-s", src, "-t", dst])

    def break_pane(self, src: str, detached: bool = True) -> Window:
        """Break *src* pane into its own window.  Returns the new window."""
        args = ["break-pane", "-s", src]
        if detached:
            args.append("-d")
        args += ["-P", "-F", self._WINDOW_FMT]
        result = self._run(args)
        # break-pane -P returns window info; session field is unknown here so
        # we use an empty string — callers should re-query if they need it.
        windows = self._parse_windows("", result.stdout)
        if windows:
            return windows[0]
        raise RuntimeError(f"break-pane returned no parseable output: {result.stdout!r}")

    def move_pane(self, src: str, dst_window: str) -> None:
        """Move *src* pane into *dst_window*."""
        self._run(["move-pane", "-s", src, "-t", dst_window])

    def resize_pane(self, target: str, width: int, height: int) -> None:
        """Resize *target* pane to *width* x *height* cells."""
        self._run(["resize-pane", "-t", target, "-x", str(width), "-y", str(height)])

    def send_keys(self, target: str, keys: str, enter: bool = True) -> None:
        """Send *keys* to *target* pane.

        *keys* is passed as a single positional argument to avoid shell
        interpretation by tmux's argument parser.  When *enter* is True the
        literal string ``"Enter"`` is appended as a separate argument.
        """
        args = ["send-keys", "-t", target, keys]
        if enter:
            args.append("Enter")
        self._run(args)

    def kill_pane(self, target: str) -> None:
        """Kill *target* pane."""
        self._run(["kill-pane", "-t", target])

    def respawn_pane(
        self,
        target: str,
        command: str,
        kill_existing: bool = True,
    ) -> None:
        """Respawn *target* pane running *command*.

        When *kill_existing* is True, ``-k`` is passed to ``respawn-pane``.
        *command* is passed as a single positional argument.
        """
        args = ["respawn-pane"]
        if kill_existing:
            args.append("-k")
        args += ["-t", target, command]
        self._run(args)

    def capture_pane(self, target: str, lines: int = 200) -> str:
        """Return the last *lines* lines of output from *target* pane.

        Uses ``capture-pane -p -S -<lines>`` per §7.2.
        """
        result = self._run(
            ["capture-pane", "-p", "-t", target, "-S", f"-{lines}"],
        )
        return result.stdout
