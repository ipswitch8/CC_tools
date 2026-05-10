# -*- coding: utf-8 -*-
"""
cpsm.platform.desktop_entry — generate and install a freedesktop.org .desktop
launcher entry for the CPSM GUI on Linux.

Spec: §9.7 (packaging).  Used by the ``cpsm install-desktop`` CLI subcommand
and by the AppImage / MSI builders during release.

Per-user install location:  ``$XDG_DATA_HOME/applications/cpsm.desktop``
                             (default ``~/.local/share/applications``)
System-wide:                ``/usr/local/share/applications/cpsm.desktop``

The icon is bundled at ``cpsm/resources/icons/cpsm.svg``.  When the launcher
is installed, the icon is also copied into the user's icon hicolor scalable
theme directory so app menus render it.
"""

from __future__ import annotations

import os
import shutil
import sys
from importlib import resources
from pathlib import Path
from typing import Final

__all__ = ["DESKTOP_ENTRY_NAME", "generate_desktop_file_text", "install_desktop_entry"]


DESKTOP_ENTRY_NAME: Final[str] = "cpsm.desktop"
ICON_NAME: Final[str] = "cpsm"
ICON_RESOURCE_FILE: Final[str] = "cpsm.svg"


def _xdg_data_home() -> Path:
    """Return $XDG_DATA_HOME or its default $HOME/.local/share."""
    explicit = os.environ.get("XDG_DATA_HOME")
    if explicit:
        return Path(explicit)
    return Path.home() / ".local" / "share"


def _resolve_executable(override: str | None) -> str:
    """Pick the executable path to embed in Exec=.

    Priority:
      1. Caller-supplied override (typically from ``--executable``).
      2. The current ``cpsm`` console script if we can find it on PATH.
      3. Fallback to ``sys.executable -m cpsm`` so the entry still works when
         CPSM is installed as a wheel without a console script.
    """
    if override:
        return override
    found = shutil.which("cpsm")
    if found:
        return found
    return f"{sys.executable} -m cpsm"


def generate_desktop_file_text(*, executable: str, icon_name: str = ICON_NAME) -> str:
    """Return the full text of the .desktop file.

    Parameters
    ----------
    executable:
        Command CPSM is launched with — *must* end with the ``cpsm`` invocation
        the user wants ``Exec=`` to call (e.g. ``/usr/local/bin/cpsm``).
    icon_name:
        Icon name as installed in a freedesktop icon theme (without extension).
    """
    # The Exec line invokes `gui` so the launcher opens the GUI directly.
    # %u is the optional URL/file token — if a future protocol handler is
    # registered, it will be passed here; we simply append it after the
    # subcommand and let argparse ignore unknown arguments gracefully.
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.5\n"
        "Name=CPSM\n"
        "GenericName=Session Manager\n"
        "Comment=Manage tmux/SSH sessions and Claude Code projects\n"
        f"Exec={executable} gui %u\n"
        f"Icon={icon_name}\n"
        "Terminal=false\n"
        "Categories=Development;System;TerminalEmulator;\n"
        "Keywords=tmux;ssh;claude;session;terminal;\n"
        "StartupNotify=true\n"
        "StartupWMClass=cpsm\n"
        "MimeType=application/x-cpsm-config;\n"
    )


def _copy_icon(target_dir: Path, *, force: bool) -> Path | None:
    """Copy the bundled SVG icon to the freedesktop icons hicolor scalable dir.

    Returns the path of the installed icon, or ``None`` if the bundled icon
    was missing (the .desktop file will still be installed but the launcher
    will show no icon until the user supplies one).
    """
    try:
        traversable = resources.files("cpsm.resources.icons").joinpath(ICON_RESOURCE_FILE)
    except (ModuleNotFoundError, FileNotFoundError):
        return None
    if not traversable.is_file():
        return None

    icon_dir = target_dir / "icons" / "hicolor" / "scalable" / "apps"
    icon_dir.mkdir(parents=True, exist_ok=True)
    dst = icon_dir / f"{ICON_NAME}.svg"
    if dst.exists() and not force:
        # Already present and we weren't asked to overwrite — leave it alone.
        return dst
    with resources.as_file(traversable) as src_path:
        shutil.copy2(src_path, dst)
    # Normalise to 0644 — shutil.copy2 preserves source perms, which can be 0664
    # under typical umasks; freedesktop convention is 0644 for icon files.
    dst.chmod(0o644)
    return dst


def install_desktop_entry(
    *,
    user_only: bool = True,
    force: bool = False,
    executable: str | None = None,
) -> Path:
    """Install the CPSM .desktop launcher.

    Parameters
    ----------
    user_only:
        Install under ``$XDG_DATA_HOME/applications`` (default ``True``).
        When ``False``, install to ``/usr/local/share/applications`` (requires
        root or matching write permission).
    force:
        Overwrite an existing .desktop file.  Default is to raise
        ``FileExistsError`` so re-runs don't silently clobber a customised file.
    executable:
        See :func:`_resolve_executable`.

    Returns
    -------
    Path
        The path of the installed .desktop file.
    """
    if sys.platform == "win32":
        raise RuntimeError(
            "install_desktop_entry is for Linux/Unix freedesktop systems. "
            "On Windows, use the WiX MSI installer (packaging/wix/cpsm.wxs)."
        )

    data_dir = _xdg_data_home() if user_only else Path("/usr/local/share")

    apps_dir = data_dir / "applications"
    apps_dir.mkdir(parents=True, exist_ok=True)
    target = apps_dir / DESKTOP_ENTRY_NAME

    if target.exists() and not force:
        raise FileExistsError(f"{target} already exists")

    exe = _resolve_executable(executable)
    text = generate_desktop_file_text(executable=exe)
    target.write_text(text, encoding="utf-8")
    target.chmod(0o644)

    # Best-effort icon install (won't block on failure).
    import contextlib

    with contextlib.suppress(OSError):
        _copy_icon(data_dir, force=force)

    # Trigger desktop-database refresh so the entry shows up promptly.
    # Failure is non-fatal — the file is in the right place either way.
    update_db = shutil.which("update-desktop-database")
    if update_db is not None:
        try:
            import subprocess

            subprocess.run(
                [update_db, str(apps_dir)],
                check=False,
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (subprocess.SubprocessError, OSError):
            pass

    return target
