# -*- coding: utf-8 -*-
"""
PathResolver and config-path helpers.

Spec: §2.1, §8

``resolve_config_path`` lives canonically in ``cpsm.data.repository`` (where it
was first implemented in Phase 2).  This module re-exports it so callers in the
platform layer can import from a single location, and adds the ``PathResolver``
utility class for general path operations.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Re-export resolve_config_path from the data layer so it is accessible from
# both ``cpsm.data.repository`` (original location) and ``cpsm.platform.paths``
# (platform-layer home).  Existing tests that import from the original location
# continue to work unchanged.
from cpsm.data.repository import resolve_config_path as resolve_config_path

__all__ = ["PathResolver", "resolve_config_path"]


class PathResolver:
    """General-purpose path resolution helper for CPSM.

    Provides platform-aware lookups for config directories, log directories,
    and temporary launcher scripts.

    Resolution order for the config directory (§2.1, §8):
      1. $XDG_CONFIG_HOME/cpsm  (Linux when var is set)
         %APPDATA%\\cpsm         (Windows when var is set)
      2. ~/.config/cpsm          (Linux default)
         %USERPROFILE%\\AppData\\Roaming\\cpsm  (Windows default)
    """

    _CPSM_CONFIG_ENV = "CPSM_CONFIG"
    _XDG_CONFIG_HOME_ENV = "XDG_CONFIG_HOME"
    _APPDATA_ENV = "APPDATA"
    _CONFIG_FILENAME = ".cpsm.yaml"

    def config_dir(self) -> Path:
        """Return the platform-appropriate CPSM config directory.

        The directory is not created; callers are responsible for mkdir if
        needed.
        """
        if sys.platform == "win32":
            appdata = os.environ.get(self._APPDATA_ENV)
            if appdata:
                return Path(appdata) / "cpsm"
            return Path.home() / "AppData" / "Roaming" / "cpsm"
        # Linux / other POSIX
        xdg = os.environ.get(self._XDG_CONFIG_HOME_ENV)
        if xdg:
            return Path(xdg) / "cpsm"
        return Path.home() / ".config" / "cpsm"

    def log_dir(self) -> Path:
        """Return the path for rotating log files (§9.5).

        On Linux: ``<config_dir>/logs/``.
        On Windows: same pattern under %APPDATA%\\cpsm\\logs\\.
        """
        return self.config_dir() / "logs"

    def launcher_tmp_dir(self) -> Path:
        """Return the directory for temporary launcher scripts (§7.6).

        On Linux: ``/tmp/cpsm-launchers/``.
        On Windows: ``%TEMP%\\cpsm-launchers\\``.
        """
        if sys.platform == "win32":
            tmp = os.environ.get("TEMP") or os.environ.get("TMP") or "C:\\Temp"
            return Path(tmp) / "cpsm-launchers"
        return Path("/tmp") / "cpsm-launchers"

    def launcher_script_path(self, connection_id: str, pid: int) -> Path:
        """Return the path for a rendered launcher script (§7.6).

        Pattern: ``<launcher_tmp_dir>/cpsm-launcher-<connection_id>-<pid>.sh``
        """
        filename = f"cpsm-launcher-{connection_id}-{pid}.sh"
        return self.launcher_tmp_dir() / filename

    def expand(self, path: Path | str) -> Path:
        """Expand ``~`` and environment variables, then resolve symlinks."""
        return Path(os.path.expandvars(str(path))).expanduser().resolve()

    def is_safe_config_path(self, path: Path) -> bool:
        """Return True if *path* has owner-only permissions (Linux 0600).

        On Windows always returns True (ACL check handled by KeyService in
        Phase 7).  On non-existent paths returns False.
        """
        if sys.platform == "win32":
            return True
        try:
            mode = path.stat().st_mode & 0o777
            return mode <= 0o600
        except OSError:
            return False
