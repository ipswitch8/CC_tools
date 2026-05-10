# -*- coding: utf-8 -*-
"""
ProcessRunner — subprocess wrapper with per-platform quoting and UTF-8 hygiene.

Spec: §8, §9.3
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path


class ProcessRunner:
    """Run subprocesses with consistent UTF-8 I/O and per-platform quoting.

    All invocations use ``text=True``, ``encoding="utf-8"``, and
    ``errors="replace"`` so that non-UTF-8 bytes in subprocess output are
    replaced rather than raising ``UnicodeDecodeError``.

    On Windows, ``subprocess.CREATE_NO_WINDOW`` is applied to background
    subprocess.run calls so no console window flashes on the screen.
    Foreground terminal-emulator launches go through ``TerminalLauncher``,
    not this class.
    """

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float = 5.0,
        check: bool = True,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run *argv* as a subprocess and return the completed process.

        Args:
            argv: Command and arguments.  Must be a non-empty sequence.
            timeout: Seconds to wait before raising ``subprocess.TimeoutExpired``.
            check: If True, raise ``subprocess.CalledProcessError`` on non-zero
                exit code.
            env: Environment mapping passed to the child process.  ``None``
                inherits the current process environment.
            cwd: Working directory for the child.  ``None`` inherits the caller's
                cwd.
            input: Optional text to pass to the child's stdin.

        Returns:
            ``subprocess.CompletedProcess`` with ``stdout`` and ``stderr`` as
            strings.

        Raises:
            subprocess.TimeoutExpired: if the process does not finish within
                *timeout* seconds.
            subprocess.CalledProcessError: if *check* is True and the exit code
                is non-zero.
        """
        if sys.platform == "win32":
            # Suppress console window flash for background subprocesses.
            # CREATE_NO_WINDOW is Windows-only; use getattr to avoid AttributeError
            # when this branch is exercised in tests on non-Windows platforms.
            no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            return subprocess.run(  # type: ignore[call-overload]
                list(argv),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=timeout,
                check=check,
                env=env,
                cwd=cwd,
                input=input,
                creationflags=no_window,
            )
        return subprocess.run(
            list(argv),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=check,
            env=env,
            cwd=cwd,
            input=input,
        )

    def quote_argv(self, argv: Sequence[str]) -> str:
        """Return *argv* as a quoted shell string.

        On POSIX (Linux / macOS) each argument is quoted with ``shlex.quote``
        and joined with spaces.  On Windows ``subprocess.list2cmdline`` is used
        instead (the Windows CreateProcess convention).
        """
        if sys.platform == "win32":
            return subprocess.list2cmdline(list(argv))
        return " ".join(shlex.quote(arg) for arg in argv)
