# -*- coding: utf-8 -*-
"""
SshBinary — detects OpenSSH vs plink, normalises argv differences.

Spec: §8, §9.2
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal


class SshBinary:
    """Detect and wrap an SSH binary (OpenSSH or PuTTY plink).

    Differences normalised here so callers never need to know which binary is
    in use:

    * Port flag: OpenSSH ``-p <port>``, plink ``-P <port>``
    * Force TTY: OpenSSH ``-tt``, plink ``-t``
    * Identity file: both use ``-i <path>``
    * Extra options: OpenSSH ``-o Key=Value``, plink ``-o Key=Value``
      (plink ≥ 0.73 supports -o, older versions silently ignore it)

    Plink does NOT support forwarding passphrases through stdin when invoked
    non-interactively; that path is guarded by Phase 7 KeyService, not here.
    """

    binary: str
    flavor: Literal["openssh", "plink"]

    # Names to search for, in preference order
    _OPENSSH_NAMES = ("ssh",)
    _PLINK_NAMES = ("plink",)

    def __init__(self, binary: str, flavor: Literal["openssh", "plink"]) -> None:
        self.binary = binary
        self.flavor = flavor

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def detect(
        cls,
        prefer: Literal["auto", "openssh", "plink"] = "auto",
    ) -> SshBinary:
        """Detect an SSH binary on PATH and return an ``SshBinary`` instance.

        *prefer* controls the search order:
        * ``"auto"`` — try OpenSSH first, fall back to plink.
        * ``"openssh"`` — only search for OpenSSH binaries.
        * ``"plink"`` — only search for plink.

        Raises:
            RuntimeError: if no suitable binary is found on PATH.
        """
        if prefer in ("auto", "openssh"):
            for name in cls._OPENSSH_NAMES:
                path = shutil.which(name)
                if path:
                    return cls(binary=path, flavor="openssh")

        if prefer in ("auto", "plink"):
            for name in cls._PLINK_NAMES:
                path = shutil.which(name)
                if path:
                    return cls(binary=path, flavor="plink")

        if prefer == "openssh":
            raise RuntimeError(
                f"No OpenSSH binary found on PATH (searched: {', '.join(cls._OPENSSH_NAMES)})"
            )
        if prefer == "plink":
            raise RuntimeError(
                f"No plink binary found on PATH (searched: {', '.join(cls._PLINK_NAMES)})"
            )

        raise RuntimeError("No SSH binary found (openssh or plink)")

    # ------------------------------------------------------------------
    # Argv builder
    # ------------------------------------------------------------------

    def build_argv(
        self,
        *,
        host: str,
        user: str,
        port: int = 22,
        identity_file: Path | None = None,
        ssh_options: list[str] | None = None,
        remote_command: list[str] | None = None,
        force_tty: bool = True,
    ) -> list[str]:
        """Build an argv list for the detected SSH binary.

        Args:
            host: Remote hostname or IP.
            user: Remote username.
            port: Remote port (default 22).  Omitted from argv when 22 so the
                command stays clean; the server default is 22 anyway.
            identity_file: Path to private key file (``-i``).
            ssh_options: Extra ``-o Key=Value`` strings (no leading ``-o``
                prefix needed — they are added by this method).
            remote_command: Command + args to execute on the remote host.
                When ``None`` an interactive session is opened.
            force_tty: Request pseudo-TTY allocation (``-tt`` / ``-t``).

        Returns:
            A list of strings ready to pass to :class:`ProcessRunner`.
        """
        argv: list[str] = [self.binary]

        # TTY allocation
        if force_tty:
            if self.flavor == "openssh":
                argv += ["-tt"]
            else:  # plink
                argv += ["-t"]

        # Port (only add when non-default)
        if port != 22:
            flag = "-P" if self.flavor == "plink" else "-p"
            argv += [flag, str(port)]

        # Identity file
        if identity_file is not None:
            argv += ["-i", str(identity_file)]

        # Extra -o options (OpenSSH and plink ≥ 0.73)
        for opt in ssh_options or []:
            argv += ["-o", opt]

        # User@host
        argv.append(f"{user}@{host}")

        # Optional remote command
        if remote_command:
            argv += ["--", *list(remote_command)]

        return argv
