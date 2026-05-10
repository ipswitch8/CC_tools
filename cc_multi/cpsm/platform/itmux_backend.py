# -*- coding: utf-8 -*-
"""
ItmuxBackend — concrete MultiplexerBackend implementation for itmux on Windows.

itmux is a tmux-compatible terminal multiplexer for Windows.  Most commands
mirror tmux exactly; the differences documented inline are:

- **Pane geometry in pixels, not cells.**  ``resize-pane`` accepts pixel
  dimensions on builds where pixel-mode is active.  When ``capabilities
  .pixel_geometry`` is True the backend queries the current cell dimensions
  via ``display-message -p "#{client_cell_width}|#{client_cell_height}"``
  and multiplies the requested *width* / *height* (in cells) before passing
  pixel values to itmux.

- **``new-session -x/-y`` may be silently ignored** on older itmux builds.
  ``BackendCapabilities.supports_initial_size_in_new_session`` is probed
  at startup; when False the backend creates the session first and then
  issues a ``resize-pane`` on the initial pane.

- **``split-window -b`` may be absent** on older builds.
  ``BackendCapabilities.supports_split_before`` gates the flag; when False
  the backend simulates "before" by doing a normal split and then swapping
  panes.

- **Sockets are named pipes** on Windows (``\\\\.\\pipe\\itmux-<user>``).
  Pass the named-pipe path as *socket_path*; the backend passes it with
  ``-S`` just like tmux sockets.  The caller is responsible for providing
  a valid pipe name on Windows.

Spec: §7.3, §7.5, §8
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from typing import ClassVar, Literal

from cpsm.platform.base import (
    BackendCapabilities,
    MultiplexerBackend,
    Pane,
    Session,
    Window,
)
from cpsm.platform.process_runner import ProcessRunner

# Parsing helpers shared with TmuxBackend (imported, not duplicated).
# Attribution: originally written for TmuxBackend (cpsm/platform/tmux_backend.py).
from cpsm.platform.tmux_backend import _parse_format, _parse_int, _version_at_least

# ---------------------------------------------------------------------------
# ItmuxBackend
# ---------------------------------------------------------------------------


class ItmuxBackend(MultiplexerBackend):
    """Concrete itmux backend (Windows terminal multiplexer).

    Runs every operation via :class:`ProcessRunner` using the itmux binary.
    Capabilities are probed at construction time by running ``itmux -V``.

    Args:
        socket_path: Named-pipe path for the itmux server on Windows
            (e.g. ``\\\\.\\pipe\\itmux-alice``).  When provided every
            invocation adds ``-S <path>``.  On POSIX the same flag works
            with UNIX sockets for cross-platform testing.
        runner: Inject a custom :class:`ProcessRunner`; defaults to a
            fresh instance.
        itmux_binary: Path or name of the itmux binary (default ``"itmux"``).
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
    ]
    _PANE_FMT: ClassVar[str] = (
        "#{session_name}|#{window_index}|#{pane_index}|#{pane_id}"
        "|#{pane_pid}|#{pane_dead}|#{pane_current_command}"
        "|#{pane_width}|#{pane_height}|#{pane_dead_status}"
    )

    # Pane fields returned by split-window / break-pane -P
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
    ]
    _SPLIT_PANE_FMT: ClassVar[str] = (
        "#{pane_id}|#{session_name}|#{window_index}|#{pane_index}"
        "|#{pane_pid}|#{pane_dead}|#{pane_current_command}"
        "|#{pane_width}|#{pane_height}|#{pane_dead_status}"
    )

    # Cell-dimension query format (used in pixel↔cell conversion)
    _CELL_DIM_FMT: ClassVar[str] = "#{client_cell_width}|#{client_cell_height}"

    def __init__(
        self,
        *,
        socket_path: str | None = None,
        runner: ProcessRunner | None = None,
        itmux_binary: str = "itmux",
    ) -> None:
        # Difference from TmuxBackend: socket_path is a str (named pipe on
        # Windows, e.g. \\.\pipe\itmux-alice) rather than a pathlib.Path, so
        # we can pass Windows named-pipe strings that pathlib normalises away.
        self._itmux = itmux_binary
        self._socket_path = socket_path
        self._runner = runner or ProcessRunner()
        # pixel_geometry is probed separately after base capabilities are known.
        self._pixel_geometry: bool = False
        self.capabilities: BackendCapabilities = self._probe_capabilities()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _prefix(self) -> list[str]:
        """Return the argv prefix for every itmux invocation.

        Difference vs tmux: socket_path is a plain str so we do not call
        ``str()`` on a Path object — the raw string (including Windows UNC
        pipe names) is used verbatim.
        """
        if self._socket_path is not None:
            return [self._itmux, "-S", self._socket_path]
        return [self._itmux]

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
        """Probe itmux version and populate :class:`BackendCapabilities`.

        itmux outputs a version string on stdout when invoked with ``-V``.
        Expected format: ``"itmux <major>.<minor>"`` (e.g. ``"itmux 1.2"``).

        Feature flags compared to tmux thresholds:

        - ``supports_split_before``: itmux >= 1.2 (older builds lack ``-b``).
        - ``supports_remain_on_exit``: itmux >= 1.0.
        - ``supports_capture_pane``: itmux >= 1.0.
        - ``supports_format_pane_dead``: itmux >= 1.1.
        - ``supports_initial_size_in_new_session``: itmux >= 1.1
          (older itmux ignores ``new-session -x/-y``).

        After populating capabilities the method also probes for pixel-geometry
        mode by checking if the version is earlier than a known pixel-mode
        introduction point.  (Pixel mode was introduced in itmux 1.0; versions
        < 1.0 use cell geometry like tmux.)

        Note: ``check=False`` is intentional — the binary may not be present in
        test environments; we fall back to minimal capabilities rather than
        raising.
        """
        result = self._runner.run(
            [*self._prefix(), "-V"],
            timeout=5.0,
            check=False,
        )
        version = result.stdout.strip()  # e.g. "itmux 1.2"

        caps = BackendCapabilities(
            name="itmux",
            version=version,
            # split-window -b: itmux >= 1.2
            supports_split_before=_version_at_least(version, "1.2"),
            # remain-on-exit option: itmux >= 1.0
            supports_remain_on_exit=_version_at_least(version, "1.0"),
            # capture-pane: itmux >= 1.0
            supports_capture_pane=_version_at_least(version, "1.0"),
            # #{pane_dead} format token: itmux >= 1.1
            supports_format_pane_dead=_version_at_least(version, "1.1"),
            # new-session -x/-y: itmux >= 1.1
            supports_initial_size_in_new_session=_version_at_least(version, "1.1"),
        )

        # Pixel-geometry mode: itmux >= 1.0 uses pixel dimensions for panes.
        # Store the flag so resize_pane can apply the cell→pixel conversion.
        self._pixel_geometry = _version_at_least(version, "1.0")

        return caps

    def _query_cell_dimensions(self) -> tuple[int, int]:
        """Return ``(cell_width_px, cell_height_px)`` from the active client.

        Runs ``itmux display-message -p "#{client_cell_width}|#{client_cell_height}"``
        and parses the two integers.

        Difference vs tmux: only called when pixel_geometry is active because
        standard tmux panes are already measured in cells.

        Returns (1, 1) as a safe fallback if the query fails or returns
        non-numeric output so callers never divide by zero.
        """
        try:
            result = self._run(
                ["display-message", "-p", self._CELL_DIM_FMT],
                check=False,
            )
            parts = result.stdout.strip().split("|")
            if len(parts) >= 2:
                cw = _parse_int(parts[0]) or 1
                ch = _parse_int(parts[1]) or 1
                return cw, ch
        except Exception:  # pragma: no cover
            pass
        return 1, 1

    # ------------------------------------------------------------------
    # Parsing helpers (same logic as TmuxBackend)
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
                )
            )
        return panes

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def list_sessions(self) -> list[Session]:
        """Return all active itmux sessions.

        Identical argv to tmux; returns empty list on non-zero exit (no server
        or no sessions active).
        """
        result = self._run(
            ["list-sessions", "-F", self._SESSION_FMT],
            check=False,
        )
        if result.returncode != 0:
            return []
        return self._parse_sessions(result.stdout)

    def new_session(
        self,
        name: str,
        width: int,
        height: int,
        detached: bool = True,
    ) -> Session:
        """Create a new itmux session and return it.

        Difference vs tmux:
        Older itmux builds silently ignore ``new-session -x/-y``.  When
        ``capabilities.supports_initial_size_in_new_session`` is False the
        backend creates the session without size flags and then calls
        :meth:`resize_pane` on the initial pane to apply the requested
        geometry.
        """
        args = ["new-session"]
        if detached:
            args.append("-d")
        args += ["-s", name]

        if self.capabilities.supports_initial_size_in_new_session:
            args += ["-x", str(width), "-y", str(height)]

        self._run(args)

        if not self.capabilities.supports_initial_size_in_new_session:
            # Resize the initial pane of the newly created session.
            panes = self.list_panes(target=name)
            if panes:
                self.resize_pane(panes[0].id, width, height)

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
        """Attach the current terminal to *name*.  Identical to tmux."""
        self._run(["attach-session", "-t", name])

    def kill_session(self, name: str) -> None:
        """Kill the named session.  Identical to tmux."""
        self._run(["kill-session", "-t", name])

    # ------------------------------------------------------------------
    # Window management
    # ------------------------------------------------------------------

    def list_windows(self, session: str) -> list[Window]:
        """Return all windows in *session*.  Identical argv to tmux."""
        result = self._run(
            ["list-windows", "-t", session, "-F", self._WINDOW_FMT],
        )
        return self._parse_windows(session, result.stdout)

    def new_window(self, session: str, name: str | None = None) -> Window:
        """Create a new window in *session* and return it.  Identical to tmux."""
        args = ["new-window", "-t", session, "-P", "-F", self._WINDOW_FMT]
        if name is not None:
            args += ["-n", name]
        result = self._run(args)
        windows = self._parse_windows(session, result.stdout)
        if windows:
            return windows[0]
        raise RuntimeError(f"new-window returned no parseable output: {result.stdout!r}")

    def kill_window(self, target: str) -> None:
        """Kill the window identified by *target*.  Identical to tmux."""
        self._run(["kill-window", "-t", target])

    def select_layout(self, window: str, layout: str) -> None:
        """Apply a layout preset or custom string to *window*.  Identical to tmux."""
        self._run(["select-layout", "-t", window, layout])

    def capture_layout(self, window: str) -> str:
        """Return the current ``#{window_layout}`` string for *window*.  Identical to tmux."""
        result = self._run(
            ["display-message", "-p", "-t", window, "#{window_layout}"],
        )
        return result.stdout.strip()

    def set_window_option(self, target: str, name: str, value: str) -> None:
        """Set a window option on *target*.  Identical to tmux."""
        self._run(["set-window-option", "-t", target, name, value])

    # ------------------------------------------------------------------
    # Pane management
    # ------------------------------------------------------------------

    def list_panes(self, target: str | None = None) -> list[Pane]:
        """Return panes.  If *target* is None, return panes across all sessions.

        Identical argv to tmux.
        """
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
        ``before=True`` → adds ``-b`` flag **when supported**.

        Difference vs tmux:
        Older itmux builds do not support ``split-window -b``.  When
        ``capabilities.supports_split_before`` is False and *before* is True
        the backend simulates the behaviour by:

        1. Splitting without ``-b`` to get a new pane *after* the target.
        2. Calling ``swap-pane`` to swap the new pane into the target's
           position, effectively making the new pane appear *before* the
           target in the layout.

        The returned :class:`Pane` is the newly created pane in both cases.
        """
        args = ["split-window"]
        if direction == "h":
            args.append("-h")
        else:
            args.append("-v")

        if before and self.capabilities.supports_split_before:
            # Native -b flag supported.
            args.append("-b")
        args += ["-t", target, "-P", "-F", self._SPLIT_PANE_FMT]

        result = self._run(args)
        panes = self._parse_panes_from_fields(result.stdout, self._SPLIT_PANE_FIELDS)
        if not panes:
            raise RuntimeError(f"split-window returned no parseable output: {result.stdout!r}")
        new_pane = panes[0]

        if before and not self.capabilities.supports_split_before:
            # Simulate -b: swap the new pane with the target so the new pane
            # occupies the target's former position (i.e. "before" it).
            self.swap_panes(new_pane.id, target)

        return new_pane

    def select_pane(self, target: str) -> None:
        """Make *target* the active pane.  Identical to tmux."""
        self._run(["select-pane", "-t", target])

    def swap_panes(self, src: str, dst: str) -> None:
        """Swap the positions of *src* and *dst* panes.  Identical to tmux."""
        self._run(["swap-pane", "-s", src, "-t", dst])

    def break_pane(self, src: str, detached: bool = True) -> Window:
        """Break *src* pane into its own window.  Identical to tmux."""
        args = ["break-pane", "-s", src]
        if detached:
            args.append("-d")
        args += ["-P", "-F", self._WINDOW_FMT]
        result = self._run(args)
        windows = self._parse_windows("", result.stdout)
        if windows:
            return windows[0]
        raise RuntimeError(f"break-pane returned no parseable output: {result.stdout!r}")

    def move_pane(self, src: str, dst_window: str) -> None:
        """Move *src* pane into *dst_window*.  Identical to tmux."""
        self._run(["move-pane", "-s", src, "-t", dst_window])

    def resize_pane(self, target: str, width: int, height: int) -> None:
        """Resize *target* pane to *width* x *height* cells.

        Difference vs tmux:
        When ``_pixel_geometry`` is True (itmux >= 1.0) the backend first
        queries the current client cell dimensions in pixels via
        ``display-message -p "#{client_cell_width}|#{client_cell_height}"``
        and multiplies *width* and *height* by the respective pixel sizes
        before passing them to ``resize-pane``.

        When ``_pixel_geometry`` is False (legacy or test mode) the integers
        are passed unchanged, matching standard tmux behaviour.
        """
        if self._pixel_geometry:
            cell_w, cell_h = self._query_cell_dimensions()
            px_width = width * cell_w
            px_height = height * cell_h
        else:
            px_width = width
            px_height = height

        self._run(["resize-pane", "-t", target, "-x", str(px_width), "-y", str(px_height)])

    def send_keys(self, target: str, keys: str, enter: bool = True) -> None:
        """Send *keys* to *target* pane.

        Identical to tmux.  *keys* is passed as a single positional argument.
        When *enter* is True the literal string ``"Enter"`` is appended.
        """
        args = ["send-keys", "-t", target, keys]
        if enter:
            args.append("Enter")
        self._run(args)

    def kill_pane(self, target: str) -> None:
        """Kill *target* pane.  Identical to tmux."""
        self._run(["kill-pane", "-t", target])

    def respawn_pane(
        self,
        target: str,
        command: str,
        kill_existing: bool = True,
    ) -> None:
        """Respawn *target* pane running *command*.  Identical to tmux.

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

        Identical to tmux: uses ``capture-pane -p -S -<lines>``.
        """
        result = self._run(
            ["capture-pane", "-p", "-t", target, "-S", f"-{lines}"],
        )
        return result.stdout
