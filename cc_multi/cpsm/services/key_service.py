# -*- coding: utf-8 -*-
"""
KeyService — ed25519 key generation, SSH deployment, and keyring passphrase management.

Spec sections: §9.2, §7.6
Security constraints:
  - Private key paths only in YAML; no plaintext passphrases.
  - Passphrase stored ONLY via OS keyring (keyring://cpsm/<key_id>).
  - Local password/passphrase buffers zeroed after use (best-effort).
  - No passphrase/password in any log line, command argv, or YAML.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    BestAvailableEncryption,
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from cpsm.data.schema import SshKey
from cpsm.platform.process_runner import ProcessRunner

__all__ = [
    "DeployResult",
    "KeyExistsError",
    "KeyService",
]

logger = logging.getLogger(__name__)

# Sentinel for keyring service name
_KEYRING_SERVICE = "cpsm"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class KeyExistsError(FileExistsError):
    """Raised when the target private key path already exists."""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class DeployResult:
    """Result of a key deployment operation."""

    success: bool
    method: str  # "ssh-copy-id" or "manual"
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# KeyService
# ---------------------------------------------------------------------------


class KeyService:
    """Generate ed25519 keys, deploy via ssh-copy-id, and manage passphrases.

    Parameters
    ----------
    runner:
        ``ProcessRunner`` instance for subprocess calls.  If None a new one
        is created.  Injected for testability.
    keyring_module:
        The ``keyring`` module (or a compatible mock).  If None, the real
        ``keyring`` module is imported lazily.
    """

    def __init__(
        self,
        *,
        runner: ProcessRunner | None = None,
        keyring_module: Any | None = None,
    ) -> None:
        self._runner = runner or ProcessRunner()
        self._keyring = keyring_module

    # ------------------------------------------------------------------
    # Keyring helpers
    # ------------------------------------------------------------------

    def _get_keyring(self) -> Any:
        """Return the keyring module (lazy import if not injected)."""
        if self._keyring is None:
            import keyring

            self._keyring = keyring
        return self._keyring

    def get_passphrase(self, key: SshKey) -> str | None:
        """Retrieve the passphrase for *key* from the OS keyring.

        Returns None when no passphrase is stored.
        """
        kr = self._get_keyring()
        result: str | None = kr.get_password(_KEYRING_SERVICE, key.id)
        return result

    def store_passphrase(self, key_id: str, passphrase: str) -> None:
        """Store *passphrase* for *key_id* in the OS keyring.

        The passphrase is NEVER written to disk or logged.
        """
        kr = self._get_keyring()
        kr.set_password(_KEYRING_SERVICE, key_id, passphrase)
        # Do not log the passphrase.
        logger.debug("Stored passphrase for key '%s' in keyring", key_id)

    # ------------------------------------------------------------------
    # Key generation
    # ------------------------------------------------------------------

    def generate_ed25519(
        self,
        *,
        key_id: str,
        private_path: Path,
        comment: str = "",
        passphrase: str | None = None,
    ) -> SshKey:
        """Generate a new ed25519 key pair and write to disk.

        Parameters
        ----------
        key_id:
            The CPSM key id (slug).  Used as keyring key identifier.
        private_path:
            Destination for the private key file.  Public key is written to
            ``private_path.with_suffix('.pub')``.
        comment:
            Optional comment embedded in the public key.
        passphrase:
            Optional passphrase for private key encryption.  When supplied it
            is encrypted into the private key and stored in the OS keyring;
            the plaintext reference is zeroed as soon as possible.

        Raises
        ------
        KeyExistsError:
            If *private_path* already exists.
        """
        if private_path.exists():
            raise KeyExistsError(
                f"Private key path already exists: {private_path}. "
                "Remove the existing key or use a different path."
            )

        public_path = private_path.with_suffix(".pub")

        # Generate key material
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key()

        # Determine encryption
        if passphrase is not None:
            passphrase_bytes = passphrase.encode("utf-8")
            encryption: BestAvailableEncryption | NoEncryption = BestAvailableEncryption(
                passphrase_bytes
            )
        else:
            passphrase_bytes = b""  # placeholder; not used when encryption=NoEncryption()
            encryption = NoEncryption()

        # Serialize private key (OpenSSH format)
        private_pem = private_key.private_bytes(
            encoding=Encoding.PEM,
            format=PrivateFormat.OpenSSH,
            encryption_algorithm=encryption,
        )

        # Serialize public key (OpenSSH format with comment)
        public_raw = public_key.public_bytes(
            encoding=Encoding.OpenSSH,
            format=PublicFormat.OpenSSH,
        )
        if comment:
            public_bytes = public_raw + b" " + comment.encode("utf-8") + b"\n"
        else:
            public_bytes = public_raw + b"\n"

        # Write private key
        private_path.parent.mkdir(parents=True, exist_ok=True)
        private_path.write_bytes(private_pem)

        # Restrict private key permissions on Linux (0600)
        if sys.platform != "win32":
            os.chmod(private_path, stat.S_IRUSR | stat.S_IWUSR)

        # Write public key
        public_path.write_bytes(public_bytes)

        # Store passphrase in keyring; zero local buffer (best-effort)
        passphrase_ref: str | None = None
        if passphrase is not None:
            self.store_passphrase(key_id, passphrase)
            passphrase_ref = f"keyring://{_KEYRING_SERVICE}/{key_id}"
            # Zero the bytes buffer we created (only meaningful bytes, not placeholder)
            if passphrase_bytes:
                buf = bytearray(passphrase_bytes)
                for i in range(len(buf)):
                    buf[i] = 0
                del buf
            del passphrase_bytes
            # We cannot zero a Python str, but we release our reference
            del passphrase

        logger.info(
            "Generated ed25519 key '%s' at %s (passphrase: %s)",
            key_id,
            private_path,
            "keyring" if passphrase_ref else "none",
        )

        return SshKey(
            id=key_id,
            name=comment or key_id,
            type="ed25519",
            private_path=str(private_path),
            public_path=str(public_path),
            passphrase_ref=passphrase_ref,
            created_at=datetime.now(tz=UTC),
        )

    # ------------------------------------------------------------------
    # Deployment
    # ------------------------------------------------------------------

    def deploy(
        self,
        *,
        key: SshKey,
        connection: Any,
        password: str | None = None,
    ) -> DeployResult:
        """Deploy *key* to *connection* via ssh-copy-id (with manual fallback).

        The *password* for remote authentication is used for one connection
        attempt then zeroed.  It is NEVER logged or included in command argv.

        Parameters
        ----------
        key:
            The SshKey whose public key is to be deployed.
        connection:
            A connection model (must have .host, .user, and optionally .port).
        password:
            Optional password for SSH authentication (e.g. when key-based
            auth is not yet set up).  Zeroed after use.
        """
        host = getattr(connection, "host", None)
        user = getattr(connection, "user", None)
        port = getattr(connection, "port", 22) or 22

        if not host or not user:
            return DeployResult(
                success=False,
                method="none",
                errors=["Connection must have host and user for key deployment."],
            )

        pub_key_path = Path(key.public_path).expanduser()
        if not pub_key_path.exists():
            # Auto-recover: derive the .pub from the private key if the
            # private key exists. Common when a config was imported from
            # another machine and only one of the two files survives.
            priv_key_path = Path(key.private_path).expanduser()
            if priv_key_path.exists():
                try:
                    self._derive_public_from_private(priv_key_path, pub_key_path, key)
                    logger.info(
                        "Derived missing public key %s from private key",
                        pub_key_path,
                    )
                except Exception as exc:
                    return DeployResult(
                        success=False,
                        method="none",
                        errors=[
                            f"Public key file not found: {pub_key_path}; "
                            f"could not derive from private key {priv_key_path}: {exc}"
                        ],
                    )
            else:
                return DeployResult(
                    success=False,
                    method="none",
                    errors=[
                        f"Public key file not found: {pub_key_path} "
                        f"(and private key {priv_key_path} also missing). "
                        "Generate or import a key via Tools → SSH Keys."
                    ],
                )

        # If a password was provided, use sshpass so ssh / ssh-copy-id can
        # auth non-interactively (subprocess has no TTY, so OpenSSH's prompt
        # would never appear). Pass via SSHPASS env var so it never lands in
        # argv (which would be visible in `ps`).
        import shutil as _shutil
        env: dict[str, str] | None = None
        sshpass_prefix: list[str] = []
        # When deploying with a password we want pubkey auth completely out
        # of the picture: any pre-existing agent key would either authenticate
        # us (defeating the point of deploying a new key) or, if the agent
        # refuses to sign (confirm-each key, locked agent, missing askpass),
        # fail loudly with "agent refused operation".  These ssh -o flags +
        # the SSH_AUTH_SOCK strip below force password-only auth.
        password_auth_opts: list[str] = []
        if password:
            if _shutil.which("sshpass") is not None:
                sshpass_prefix = ["sshpass", "-e"]
                env = {k: v for k, v in os.environ.items() if k != "SSH_AUTH_SOCK"}
                env["SSHPASS"] = password
                password_auth_opts = [
                    "-o", "PreferredAuthentications=password,keyboard-interactive",
                    "-o", "PubkeyAuthentication=no",
                    "-o", "IdentityAgent=none",
                ]
            else:
                _zero_password(password)
                return DeployResult(
                    success=False,
                    method="none",
                    errors=[
                        "Password authentication for key deployment requires "
                        "the 'sshpass' tool. Install it (e.g. `apt install "
                        "sshpass`) or set up an existing SSH key first."
                    ],
                )

        # Public key body used for verification (and manual fallback).
        pub_key_content = pub_key_path.read_text(encoding="utf-8").strip()

        # --- Primary: ssh-copy-id ---
        argv = [
            *sshpass_prefix,
            "ssh-copy-id",
            "-i",
            str(pub_key_path),
            "-p",
            str(port),
            "-o",
            "StrictHostKeyChecking=accept-new",
            *password_auth_opts,
            f"{user}@{host}",
        ]

        attempts: list[str] = []
        try:
            # Argv contains no password (sshpass reads SSHPASS env), so
            # logging is safe.
            logger.debug("Deploying key '%s' to %s@%s:%s via ssh-copy-id", key.id, user, host, port)
            self._runner.run(argv, timeout=30, env=env)
            # ssh-copy-id can return 0 even when the key wasn't actually
            # appended (e.g. its filter step decided "already there" via
            # an unrelated agent key).  Verify by reading remote
            # authorized_keys and checking the pubkey is actually present.
            if self._verify_pubkey_in_remote(
                pub_key_content=pub_key_content,
                user=user, host=host, port=port,
                sshpass_prefix=sshpass_prefix,
                password_auth_opts=password_auth_opts,
                env=env,
            ):
                _zero_password(password)
                return DeployResult(success=True, method="ssh-copy-id")
            attempts.append(
                "ssh-copy-id returned 0 but the public key is not in remote "
                "~/.ssh/authorized_keys"
            )
            logger.warning(
                "ssh-copy-id reported success for key '%s' but pubkey not "
                "found in remote authorized_keys — falling through to manual",
                key.id,
            )
        except FileNotFoundError:
            # ssh-copy-id not available — fall through to manual
            attempts.append("ssh-copy-id not installed")
        except Exception as exc:
            logger.warning("ssh-copy-id failed for key '%s': %s", key.id, exc)
            attempts.append(f"ssh-copy-id error: {exc}")
            # Fall through to manual

        # --- Fallback: pipe public key via stdin ---
        # chmod 700 ~/.ssh AND 600 authorized_keys: sshd silently refuses
        # key auth when either is group/world-writable, so a half-fixed
        # permissions state would let verification pass (key body present)
        # while every real ssh attempt fell back to password.
        # ``{ echo; cat; }`` prepends a newline so our key always starts
        # on a fresh line, even when the existing authorized_keys file
        # doesn't end in \n — otherwise the two would concatenate into a
        # single malformed entry that sshd silently skips while substring
        # verification still passes.
        remote_cmd = (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            "{ echo; cat; } >> ~/.ssh/authorized_keys && "
            "chmod 600 ~/.ssh/authorized_keys"
        )
        ssh_argv = [
            *sshpass_prefix,
            "ssh",
            "-p",
            str(port),
            "-o",
            "StrictHostKeyChecking=accept-new",
            *password_auth_opts,
            f"{user}@{host}",
            remote_cmd,
        ]
        try:
            logger.debug(
                "Deploying key '%s' to %s@%s:%s via manual stdin", key.id, user, host, port
            )
            self._runner.run(ssh_argv, input=pub_key_content, timeout=30, env=env)
        except Exception as exc:
            _zero_password(password)
            attempts.append(f"manual ssh error: {exc}")
            return DeployResult(
                success=False,
                method="manual",
                errors=attempts or [f"Manual deployment failed: {exc}"],
            )
        # Verify the manual deployment too — same reason as above.
        if not self._verify_pubkey_in_remote(
            pub_key_content=pub_key_content,
            user=user, host=host, port=port,
            sshpass_prefix=sshpass_prefix,
            password_auth_opts=password_auth_opts,
            env=env,
        ):
            _zero_password(password)
            attempts.append(
                "manual ssh succeeded but the public key is not in remote "
                "~/.ssh/authorized_keys (selinux, quota, or chrooted home?)"
            )
            return DeployResult(success=False, method="manual", errors=attempts)
        _zero_password(password)
        return DeployResult(success=True, method="manual")

    def _verify_pubkey_in_remote(
        self,
        *,
        pub_key_content: str,
        user: str,
        host: str,
        port: int,
        sshpass_prefix: list[str],
        password_auth_opts: list[str],
        env: dict[str, str] | None,
    ) -> bool:
        """Read the remote ``~/.ssh/authorized_keys`` (using the same
        password the user just provided) and confirm *pub_key_content*
        is present.  Decoupled from local key passphrase / agent state —
        we only care whether the deployment landed on the server.
        """
        deployed = pub_key_content.strip()
        if not deployed:
            return False
        # Compare against the base64 body (middle field), which is the
        # cryptographically meaningful part — comments may differ.
        parts = deployed.split()
        body = parts[1] if len(parts) >= 2 else deployed

        argv = [
            *sshpass_prefix,
            "ssh",
            "-p", str(port),
            "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new",
            *password_auth_opts,
            f"{user}@{host}",
            "cat ~/.ssh/authorized_keys 2>/dev/null || true",
        ]
        try:
            result = self._runner.run(
                argv, timeout=15, check=False, env=env,
            )
        except Exception as exc:
            logger.warning("Pubkey-presence verification raised: %s", exc)
            return False
        if result.returncode != 0:
            logger.warning(
                "Pubkey-presence verification ssh failed for %s@%s:%s — "
                "rc=%d, stderr=%s",
                user, host, port, result.returncode, result.stderr.strip(),
            )
            return False
        # Parse line-by-line so a previous entry without a trailing newline
        # (which would concatenate ours onto its line) doesn't pass via
        # substring match.  sshd ignores such malformed lines, so we should
        # too — otherwise we'd report "deployed" on a line that won't auth.
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if body in line.split():
                return True
        return False


    def _derive_public_from_private(
        self,
        priv_path: Path,
        pub_path: Path,
        key: SshKey,
    ) -> None:
        """Recreate *pub_path* by deriving the public half from the private
        key at *priv_path*. Used as a fallback when an imported config
        references a .pub file that no longer exists locally.

        The passphrase (if any) is read from the OS keyring via
        ``passphrase_ref``. Does not modify the private key file.
        """
        from cryptography.hazmat.primitives.serialization import load_ssh_private_key

        passphrase_bytes: bytes | None = None
        if key.passphrase_ref is not None:
            try:
                import keyring  # local import — keyring is optional
                pw = keyring.get_password(_KEYRING_SERVICE, key.id)
                if pw:
                    passphrase_bytes = pw.encode("utf-8")
            except Exception:
                # Keyring may not be available in this environment; the
                # load_ssh_private_key call below will raise a clearer error.
                pass

        priv_data = priv_path.read_bytes()
        private_key = load_ssh_private_key(priv_data, password=passphrase_bytes)
        public_key = private_key.public_key()
        public_raw = public_key.public_bytes(
            encoding=Encoding.OpenSSH,
            format=PublicFormat.OpenSSH,
        )
        comment = (key.name or key.id).encode("utf-8")
        public_bytes = public_raw + b" " + comment + b"\n"
        pub_path.parent.mkdir(parents=True, exist_ok=True)
        pub_path.write_bytes(public_bytes)
        if sys.platform != "win32":
            os.chmod(pub_path, 0o644)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _zero_password(password: str | None) -> None:
    """Best-effort zero of the password string.

    Python strings are immutable so we cannot guarantee zeroing, but we
    release our reference and attempt to zero a bytearray copy.
    """
    if password is None:
        return
    try:
        buf = bytearray(password.encode("utf-8", errors="replace"))
        for i in range(len(buf)):
            buf[i] = 0
        del buf
    except Exception:
        pass
    del password
