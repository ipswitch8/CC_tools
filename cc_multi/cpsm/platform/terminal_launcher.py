# -*- coding: utf-8 -*-
"""
TerminalLauncher ABC and registry of concrete launcher stubs.

Spec: §8

Phase 5 ships:
- ``TerminalLauncher`` ABC
- ``LocalShellLauncher`` — fully implemented (no geometry, inherits tty)
- Stub launchers for wezterm, alacritty, kitty, konsole, gnome-terminal, xterm,
  wt.exe — ``spawn`` raises ``NotImplementedError("Phase 8 / Phase 19")``
- ``discover_launchers()`` — returns detected launchers in priority order (§8.3)

Concrete ``spawn`` implementations for the geometry-aware launchers land in
Phase 8 (Linux) and Phase 19 (Windows).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path


class TerminalLauncher(ABC):
    """Abstract terminal launcher.

    Subclasses represent a specific terminal emulator and know how to spawn
    it with the desired title, geometry, and working directory.
    """

    name: str
    """Short name used in the registry and config (e.g. ``"wezterm"``)."""

    supports_geometry: bool
    """True when the launcher can place the window at an explicit geometry."""

    @abstractmethod
    def spawn(
        self,
        argv: list[str],
        *,
        title: str,
        geometry: tuple[int, int, int, int] | None = None,
        monitor_index: int | None = None,
        cwd: Path | None = None,
    ) -> int:
        """Spawn a terminal window running *argv*.

        Args:
            argv: Command + args to run inside the terminal.
            title: Window title.
            geometry: ``(x, y, width, height)`` in pixels.  Ignored when
                ``supports_geometry`` is False or when None.
            monitor_index: 0-based index of the monitor to open on.
            cwd: Working directory for the child process.

        Returns:
            OS process ID of the spawned terminal.
        """
        ...

    def move(self, handle: int, geometry: tuple[int, int, int, int]) -> bool:
        """Attempt to move an already-open terminal window.

        Default implementation is a no-op returning False.  Override in
        launchers that support live window repositioning (§6.7).
        """
        return False

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"


# ---------------------------------------------------------------------------
# LocalShellLauncher — fully implemented, no geometry
# ---------------------------------------------------------------------------


class LocalShellLauncher(TerminalLauncher):
    """Launch a command in the current shell session (no new window).

    This launcher does **not** open a new terminal window; instead it spawns
    the command as a subprocess inheriting the current tty (or detached, for
    test scenarios using pipes).  It is the only Phase-5 launcher with a real
    implementation and is used in tests.
    """

    name = "local-shell"
    supports_geometry = False

    def spawn(
        self,
        argv: list[str],
        *,
        title: str,
        geometry: tuple[int, int, int, int] | None = None,
        monitor_index: int | None = None,
        cwd: Path | None = None,
    ) -> int:
        """Spawn *argv* as a subprocess and return its PID.

        The process is started with ``Popen`` and is not waited on.  Callers
        that need to wait should retain the returned PID and use
        ``os.waitpid`` or pass it to StatusPoller.
        """
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            close_fds=True,
        )
        return proc.pid


# ---------------------------------------------------------------------------
# Stub launchers — spawn raises NotImplementedError
# ---------------------------------------------------------------------------


class _StubLauncher(TerminalLauncher):
    """Base for stub launchers that have not yet been fully implemented."""

    name = ""
    supports_geometry = False

    def spawn(
        self,
        argv: list[str],
        *,
        title: str,
        geometry: tuple[int, int, int, int] | None = None,
        monitor_index: int | None = None,
        cwd: Path | None = None,
    ) -> int:
        raise NotImplementedError("Phase 8 / Phase 19")


# ---------------------------------------------------------------------------
# Shared wmctrl-based placement helper (Round 6)
# ---------------------------------------------------------------------------


def _wmctrl_place_async(
    match_token: str,
    geometry: tuple[int, int, int, int],
    field_index: int = 3,
) -> None:
    """Poll ``wmctrl -l`` until a window's listing contains *match_token*,
    then move/resize it via ``wmctrl -i -r <wid> -e 0,x,y,w,h``. Runs in a
    daemon thread so the caller never blocks.

    Used by launchers (gnome-terminal, kitty, wezterm) that have no
    reliable command-line geometry flag and need post-spawn placement.
    """
    if shutil.which("wmctrl") is None:
        return
    import threading
    x, y, w, h = geometry

    def _place() -> None:
        import time as _time
        wid: str | None = None
        for _ in range(20):  # up to ~2 s total
            _time.sleep(0.1)
            try:
                out = subprocess.run(
                    ["wmctrl", "-l"],
                    capture_output=True, text=True, timeout=2,
                ).stdout
            except Exception:
                return
            for line in out.splitlines():
                parts = line.split(maxsplit=field_index)
                if len(parts) > field_index and match_token in parts[field_index]:
                    wid = parts[0]
                    break
            if wid:
                break
        if wid is None:
            return
        import contextlib
        with contextlib.suppress(Exception):
            subprocess.run(
                ["wmctrl", "-i", "-r", wid, "-e", f"0,{x},{y},{w},{h}"],
                timeout=2,
                check=False,
            )

    threading.Thread(target=_place, daemon=True).start()


def _unique_token(prefix: str) -> str:
    """Return a short unique ASCII token used to identify a freshly-spawned
    window in ``wmctrl -l`` output (or via WM_CLASS instance)."""
    import uuid as _uuid
    return f"{prefix}-{_uuid.uuid4().hex[:8]}"


class WezTermLauncher(TerminalLauncher):
    """WezTerm terminal emulator (Linux/Windows). Geometry placement via
    post-spawn ``wmctrl`` (no reliable native CLI flag for X/Y position)."""

    name = "wezterm"
    supports_geometry = True

    def spawn(
        self,
        argv: list[str],
        *,
        title: str,
        geometry: tuple[int, int, int, int] | None = None,
        monitor_index: int | None = None,
        cwd: Path | None = None,
    ) -> int:
        token = _unique_token("cpsm-wezterm")
        cmd: list[str] = ["wezterm", "start", "--class", token, "--", *argv]
        proc = subprocess.Popen(cmd, cwd=cwd, close_fds=True)
        if geometry is not None:
            _wmctrl_place_async(token, geometry)
        return proc.pid


class AlacrittyLauncher(TerminalLauncher):
    """Alacritty terminal emulator (Linux). Native position+dimensions via
    ``--option`` (modern alacritty)."""

    name = "alacritty"
    supports_geometry = True

    def spawn(
        self,
        argv: list[str],
        *,
        title: str,
        geometry: tuple[int, int, int, int] | None = None,
        monitor_index: int | None = None,
        cwd: Path | None = None,
    ) -> int:
        cmd: list[str] = ["alacritty"]
        if title:
            cmd += ["--title", title]
        if geometry is not None:
            x, y, w, h = geometry
            # Approximate pixel→cell conversion (8 px/col, 16 px/row),
            # matching xterm. window.dimensions is in cells, position in
            # pixels.
            cols = max(20, int(w / 8))
            rows = max(5, int(h / 16))
            cmd += [
                "--option", f"window.position.x={x}",
                "--option", f"window.position.y={y}",
                "--option", f"window.dimensions.columns={cols}",
                "--option", f"window.dimensions.lines={rows}",
            ]
        cmd += ["-e", *argv]
        proc = subprocess.Popen(cmd, cwd=cwd, close_fds=True)
        return proc.pid


class KittyLauncher(TerminalLauncher):
    """Kitty terminal emulator (Linux). Geometry via post-spawn ``wmctrl``
    matched by ``--class``."""

    name = "kitty"
    supports_geometry = True

    def spawn(
        self,
        argv: list[str],
        *,
        title: str,
        geometry: tuple[int, int, int, int] | None = None,
        monitor_index: int | None = None,
        cwd: Path | None = None,
    ) -> int:
        token = _unique_token("cpsm-kitty")
        cmd: list[str] = ["kitty", "--class", token]
        if title:
            cmd += ["--title", title]
        cmd += [*argv]
        proc = subprocess.Popen(cmd, cwd=cwd, close_fds=True)
        if geometry is not None:
            _wmctrl_place_async(token, geometry)
        return proc.pid


class KonsoleLauncher(TerminalLauncher):
    """Konsole terminal emulator (KDE, Linux). Uses Qt's ``--geometry``
    flag, which expects PIXEL dimensions (not cell counts like xterm).
    """

    name = "konsole"
    supports_geometry = True

    def spawn(
        self,
        argv: list[str],
        *,
        title: str,
        geometry: tuple[int, int, int, int] | None = None,
        monitor_index: int | None = None,
        cwd: Path | None = None,
    ) -> int:
        cmd: list[str] = ["konsole"]
        if title:
            cmd += ["-p", f"tabtitle={title}"]
        if geometry is not None:
            x, y, w, h = geometry
            # Qt geometry is in pixels.
            cmd += ["--geometry", f"{w}x{h}+{x}+{y}"]
        cmd += ["-e", *argv]
        proc = subprocess.Popen(cmd, cwd=cwd, close_fds=True)
        return proc.pid


class GnomeTerminalLauncher(TerminalLauncher):
    """GNOME Terminal (Linux). Geometry placement via post-spawn ``wmctrl``
    poll-and-move (gnome-terminal removed ``--geometry`` years ago)."""

    name = "gnome-terminal"
    supports_geometry = True

    def spawn(
        self,
        argv: list[str],
        *,
        title: str,
        geometry: tuple[int, int, int, int] | None = None,
        monitor_index: int | None = None,
        cwd: Path | None = None,
    ) -> int:
        # Use a unique title suffix so wmctrl can pick our window out of
        # the list. The user's title remains as a prefix.
        import uuid as _uuid
        unique = f"{title} #{_uuid.uuid4().hex[:6]}" if title else (
            f"cpsm-{_uuid.uuid4().hex[:8]}"
        )
        cmd: list[str] = ["gnome-terminal", "--title", unique, "--", *argv]
        proc = subprocess.Popen(cmd, cwd=cwd, close_fds=True)
        if geometry is not None:
            _wmctrl_place_async(unique, geometry)
        return proc.pid


class XtermLauncher(TerminalLauncher):
    """xterm terminal emulator (Linux). Quick implementation: passes
    ``-geometry`` for sizing if pixel-to-cell conversion is feasible (uses
    a default 8x16 cell estimate); Full implementation will measure
    actual font metrics."""

    name = "xterm"
    supports_geometry = True

    def spawn(
        self,
        argv: list[str],
        *,
        title: str,
        geometry: tuple[int, int, int, int] | None = None,
        monitor_index: int | None = None,
        cwd: Path | None = None,
    ) -> int:
        cmd: list[str] = ["xterm"]
        if title:
            cmd += ["-title", title]
        if geometry is not None:
            x, y, w, h = geometry
            # Approximate pixel→cell conversion (8 px/col, 16 px/row)
            cols = max(20, int(w / 8))
            rows = max(5, int(h / 16))
            cmd += ["-geometry", f"{cols}x{rows}+{x}+{y}"]
        cmd += ["-e", *argv]
        proc = subprocess.Popen(cmd, cwd=cwd, close_fds=True)
        return proc.pid


class WindowsTerminalLauncher(_StubLauncher):
    """Windows Terminal (wt.exe)."""

    name = "wt"
    supports_geometry = True


# ---------------------------------------------------------------------------
# Registry / discovery
# ---------------------------------------------------------------------------

# Priority-ordered list of (binary_name, launcher_class) for each platform.
# Launchers are tried in order; the first one whose binary is on PATH wins.

_LINUX_LAUNCHERS: list[tuple[str, type[TerminalLauncher]]] = [
    ("wezterm", WezTermLauncher),
    ("alacritty", AlacrittyLauncher),
    ("kitty", KittyLauncher),
    ("konsole", KonsoleLauncher),
    ("gnome-terminal", GnomeTerminalLauncher),
    ("xterm", XtermLauncher),
]

_WINDOWS_LAUNCHERS: list[tuple[str, type[TerminalLauncher]]] = [
    ("wt", WindowsTerminalLauncher),
    ("wezterm", WezTermLauncher),
    ("alacritty", AlacrittyLauncher),
]


def discover_launchers() -> list[TerminalLauncher]:
    """Return available terminal launchers in platform-priority order (§8.3).

    Checks ``shutil.which`` for each candidate binary.  The
    ``LocalShellLauncher`` is always appended last as a guaranteed fallback.

    Returns:
        List of :class:`TerminalLauncher` instances in priority order.  May be
        empty (except for the ``LocalShellLauncher`` tail element) if no
        terminal emulator is found on PATH.
    """
    registry = _WINDOWS_LAUNCHERS if sys.platform == "win32" else _LINUX_LAUNCHERS

    found: list[TerminalLauncher] = []
    for binary_name, launcher_cls in registry:
        if shutil.which(binary_name) is not None:
            found.append(launcher_cls())

    # LocalShellLauncher is always available
    found.append(LocalShellLauncher())
    return found
