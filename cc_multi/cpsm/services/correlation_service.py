# -*- coding: utf-8 -*-
"""
CorrelationService — disambiguate which Connection a discovered SSH session
corresponds to by inspecting the remote host.

Background
----------

Local discovery (DiscoveryService) sees only the *local* ssh process and its
argv.  When a single host has several Connections defined (e.g. three
Connections all going to ``10.10.10.44`` differing only in
``project_folder``), local matching can't tell which session is which.

This service runs a small probe over SSH on each ambiguous host: enumerate
inbound established sshd sessions, capture each one's source port (the
client port chosen by the local ssh — uniquely identifies the connection by
TCP 4-tuple) and the foreground process's working directory (where the
launcher cd'd to).

We then match local ``ssh`` PIDs to remote sessions by source port, and
match each remote cwd to a Connection by ``project_folder``.

Linux-only for the local source-port lookup (uses ``/proc/<pid>/fd`` and
``/proc/net/tcp``).  Other platforms gracefully return empty results.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from cpsm.data.schema import CpsmDocument
from cpsm.services.discovery_service import DiscoveredSession

__all__ = [
    "CorrelationResult",
    "CorrelationService",
    "RemoteSession",
    "_PROBE_SCRIPT",
    "get_local_source_ports",
    "parse_probe_output",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Probe script — runs on the remote host
# ---------------------------------------------------------------------------

# POSIX sh, no bashisms. Outputs:
#   CPSM_PROBE_HOME|<remote $HOME>           — once, used to expand ``~/`` paths
#   CPSM_PROBE|<sshd_pid>|<source_port>|<fg_pid>|<cwd>|<cmd>  — one per sshd
#
# `ss -tnHp state established` lists established TCP connections with their
# owning processes; we filter rows that belong to an ``sshd`` process. From
# each such row we read the foreign (client) port — that's the source port
# we'll match against the local ssh process's local port. Then we walk the
# sshd's process tree to the deepest descendant and read its cwd via /proc.
_PROBE_SCRIPT = r"""
echo CPSM_PROBE_START
printf 'CPSM_PROBE_HOME|%s\n' "$HOME"
ss -tnHp state established 2>/dev/null | while IFS= read -r line; do
    case "$line" in
        *'"sshd"'*)
            foreign=$(printf '%s' "$line" | awk '{print $5}')
            src_port=${foreign##*:}
            sshd_pid=$(printf '%s' "$line" | sed -n 's/.*pid=\([0-9]\{1,\}\).*/\1/p' | head -n1)
            [ -z "$sshd_pid" ] && continue
            [ -z "$src_port" ] && continue
            # Walk down the process tree to the leaf descendant.
            fg_pid=$sshd_pid
            for _ in 1 2 3 4 5 6 7 8 9 10; do
                child=$(pgrep -P "$fg_pid" 2>/dev/null | head -n1)
                [ -z "$child" ] && break
                fg_pid=$child
            done
            cwd=$(readlink "/proc/$fg_pid/cwd" 2>/dev/null)
            cmd=$(tr '\0' ' ' < "/proc/$fg_pid/cmdline" 2>/dev/null | sed 's/[[:space:]]*$//')
            printf 'CPSM_PROBE|%s|%s|%s|%s|%s\n' "$sshd_pid" "$src_port" "$fg_pid" "$cwd" "$cmd"
        ;;
    esac
done
echo CPSM_PROBE_END
"""


# ---------------------------------------------------------------------------
# Data transfer objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RemoteSession:
    """One inbound sshd session on a remote host."""

    sshd_pid: int
    source_port: int  # client (local end) port, matches local ssh's local port
    fg_pid: int       # foreground process under sshd
    cwd: str          # foreground process's working directory
    cmd: str          # foreground process's argv (joined)


@dataclass(frozen=True)
class CorrelationResult:
    """Output of a correlation pass.

    Empty ``by_pid`` means correlation produced no useful matches (probe
    failed, cwd mismatch, etc.) — callers should leave the discovered
    sessions' suggested_connection_id as-is.

    ``cwds_by_pid`` is reported even when matching to a Connection
    failed: it lets the UI show the *remote* cwd in tooltips for
    diagnostic purposes ("the probe saw X, your project_folder is Y").
    """

    by_pid: dict[int, str]  # local pid → connection_id (only confident matches)
    cwds_by_pid: dict[int, str] = field(default_factory=dict)  # local pid → remote cwd


# ---------------------------------------------------------------------------
# Local source-port lookup (Linux /proc)
# ---------------------------------------------------------------------------


def get_local_source_ports(
    pids: list[int], *, proc_root: str = "/proc"
) -> dict[int, int]:
    """Return ``{pid: source_port}`` for each local ssh process.

    Walks ``/proc/<pid>/fd`` to find socket inodes, then matches them against
    ``/proc/net/tcp`` to extract the local-end (source) port.  PIDs whose
    socket inode is not found (already closed) or whose TCP entry is not in
    state ESTABLISHED are silently skipped.
    """
    if not pids:
        return {}
    proc = Path(proc_root)
    if not proc.is_dir():
        return {}

    inode_by_pid: dict[int, set[int]] = {}
    for pid in pids:
        fd_dir = proc / str(pid) / "fd"
        try:
            fds = os.listdir(fd_dir)
        except (FileNotFoundError, PermissionError, OSError):
            continue
        inodes: set[int] = set()
        for fd in fds:
            try:
                target = os.readlink(fd_dir / fd)
            except (FileNotFoundError, PermissionError, OSError):
                continue
            # Sockets show up as e.g. "socket:[1234567]"
            m = re.match(r"socket:\[(\d+)\]", target)
            if m:
                inodes.add(int(m.group(1)))
        if inodes:
            inode_by_pid[pid] = inodes

    if not inode_by_pid:
        return {}

    # Build {inode: source_port} from /proc/net/tcp (and tcp6).
    inode_to_port: dict[int, int] = {}
    for tcp_path in (proc / "net" / "tcp", proc / "net" / "tcp6"):
        try:
            content = tcp_path.read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, PermissionError, OSError):
            continue
        for line in content.splitlines()[1:]:  # skip header
            inode, port = _parse_tcp_row(line)
            if inode is not None and port is not None:
                inode_to_port[inode] = port

    result: dict[int, int] = {}
    for pid, inodes in inode_by_pid.items():
        for inode in inodes:
            if inode in inode_to_port:
                result[pid] = inode_to_port[inode]
                break
    return result


def _parse_tcp_row(line: str) -> tuple[int | None, int | None]:
    """Extract (inode, local_port) from one ``/proc/net/tcp`` line.

    Returns ``(None, None)`` for unparseable rows or rows whose state is
    not ESTABLISHED (state code ``01`` in hex).
    """
    parts = line.split()
    # Layout from kernel net/ipv4/tcp_ipv4.c:
    #   sl  local  rem  st  tx_queue:rx_queue  tr:tm_when  retrnsmt  uid  timeout  inode  ...
    # We only need the first 10 fields; real /proc/net/tcp has 17.
    if len(parts) < 10:
        return (None, None)
    local_addr = parts[1]  # "ADDR:PORT" with hex port
    state = parts[3]
    inode_str = parts[9]
    if state != "01":  # only ESTABLISHED
        return (None, None)
    if ":" not in local_addr:
        return (None, None)
    port_hex = local_addr.rsplit(":", 1)[1]
    try:
        port = int(port_hex, 16)
        inode = int(inode_str)
    except ValueError:
        return (None, None)
    return (inode, port)


# ---------------------------------------------------------------------------
# Probe output parser
# ---------------------------------------------------------------------------


def parse_probe_output(text: str) -> list[RemoteSession]:
    """Parse the remote probe's stdout into :class:`RemoteSession` objects.

    Tolerates extra noise (login banners, sudo prompts) by ignoring lines
    that don't start with ``CPSM_PROBE|``.  Returns ``[]`` for empty or
    malformed input.
    """
    out: list[RemoteSession] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("CPSM_PROBE|"):
            continue
        parts = line.split("|", 5)
        if len(parts) != 6:
            continue
        _, sshd_pid_s, src_port_s, fg_pid_s, cwd, cmd = parts
        try:
            sshd_pid = int(sshd_pid_s)
            src_port = int(src_port_s)
            fg_pid = int(fg_pid_s)
        except ValueError:
            continue
        out.append(RemoteSession(
            sshd_pid=sshd_pid,
            source_port=src_port,
            fg_pid=fg_pid,
            cwd=cwd,
            cmd=cmd,
        ))
    return out


def parse_probe_home(text: str) -> str:
    """Extract the remote $HOME directory from the probe output.

    Returns ``""`` when the probe didn't emit a HOME line (older probe
    script, no shell, etc.). Callers fall back to looser tilde-suffix
    matching in that case.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("CPSM_PROBE_HOME|"):
            return line.split("|", 1)[1] if "|" in line else ""
    return ""


# ---------------------------------------------------------------------------
# CorrelationService
# ---------------------------------------------------------------------------


# Type for the SSH probe runner: takes (host, user, key_path, port) and
# returns the probe output (or "" / raises on failure).  Injectable so tests
# can substitute fake transports.
SshProber = Callable[[str, str, str, int], str]


class CorrelationService:
    """Cross-correlate local ssh processes with remote sshd sessions.

    Parameters
    ----------
    ssh_prober:
        Callable that runs the probe on the remote and returns its stdout.
        The default uses :func:`_run_probe_via_subprocess` which spawns
        ``ssh user@host -- sh -c <probe>``.  Tests inject fakes.
    proc_root:
        Override ``/proc`` for tests that want to drive a fake filesystem.
    """

    def __init__(
        self,
        ssh_prober: SshProber | None = None,
        *,
        proc_root: str = "/proc",
    ) -> None:
        self._prober = ssh_prober or _run_probe_via_subprocess
        self._proc_root = proc_root

    def correlate(
        self, doc: CpsmDocument, sessions: list[DiscoveredSession]
    ) -> CorrelationResult:
        """Return an updated PID → connection_id mapping for *sessions*.

        Only SSH-based discovered sessions on hosts with ≥ 2 sibling
        Connections are probed; everything else is omitted from the result
        (the caller keeps the existing local match for those).
        """
        # Bucket sessions by (host, user). Only buckets with ≥ 2 candidate
        # Connections are worth correlating.
        by_host: dict[tuple[str, str], list[DiscoveredSession]] = {}
        for s in sessions:
            if s.kind not in ("claude-remote", "ssh-shell"):
                continue
            if not s.host:
                continue
            by_host.setdefault((s.host, s.user), []).append(s)

        # Index ssh_keys by id so we can dereference identity_file_ref.
        keys_by_id = {k.id: k for k in doc.ssh_keys}

        out: dict[int, str] = {}
        cwds: dict[int, str] = {}
        logger.info(
            "Correlation: %d host bucket(s) to consider",
            len(by_host),
        )
        for (host, user), bucket in by_host.items():
            candidates = [
                c for c in doc.connections
                if getattr(c, "host", "") == host
                and getattr(c, "launch_profile", "") in ("claude-remote", "ssh-shell")
            ]
            logger.info(
                "Correlation: host=%s user=%s sessions=%d candidates=%d",
                host, user, len(bucket), len(candidates),
            )
            if len(candidates) < 2:
                logger.info(
                    "Correlation: skipping %s — fewer than 2 candidates", host,
                )
                continue  # no ambiguity to resolve
            mapping, cwd_map = self._correlate_one_host(
                host, user, bucket, candidates, keys_by_id,
            )
            logger.info(
                "Correlation: host=%s probe produced %d mapping(s), %d cwd(s)",
                host, len(mapping), len(cwd_map),
            )
            out.update(mapping)
            cwds.update(cwd_map)
        logger.info("Correlation: total mappings=%d", len(out))
        return CorrelationResult(by_pid=out, cwds_by_pid=cwds)

    def _correlate_one_host(
        self,
        host: str,
        user: str,
        sessions: list[DiscoveredSession],
        candidates: list[Any],
        keys_by_id: dict[str, Any],
    ) -> tuple[dict[int, str], dict[int, str]]:
        """Probe one host and produce ``(pid → conn_id, pid → remote_cwd)``.

        The cwd map is returned even for pids that didn't get matched to
        a Connection — surfaces useful diagnostic info in tooltips."""
        # Collect local source ports for these PIDs.
        port_by_pid = get_local_source_ports(
            [s.pid for s in sessions], proc_root=self._proc_root,
        )
        logger.info(
            "Correlation %s: %d/%d local source-port lookups succeeded",
            host, len(port_by_pid), len(sessions),
        )
        if not port_by_pid:
            logger.warning(
                "Correlation %s: no local source ports captured; skipping probe",
                host,
            )
            return {}, {}

        # Pick a candidate to use for SSH auth. Any one with a valid key
        # will do — they're all to the same (host, user, port).
        prober_user = user or _resolve_user(candidates)
        port = _resolve_port(candidates)
        key_path = _resolve_key_path(candidates, keys_by_id)
        if not prober_user:
            logger.warning("Correlation %s: no user available; skipping probe", host)
            return {}, {}
        logger.info(
            "Correlation %s: probing as user=%s port=%d key_path=%r",
            host, prober_user, port, key_path or "(default)",
        )

        try:
            output = self._prober(host, prober_user, key_path, port)
        except Exception:
            logger.exception("Correlation %s: probe raised", host)
            return {}, {}
        logger.info(
            "Correlation %s: probe returned %d byte(s) of output",
            host, len(output),
        )
        remote_home = parse_probe_home(output)
        if remote_home:
            logger.info("Correlation %s: remote $HOME = %r", host, remote_home)
        remote = parse_probe_output(output)
        logger.info(
            "Correlation %s: parsed %d remote session(s) from probe",
            host, len(remote),
        )
        if not remote:
            if output:
                # Useful for diagnosing: if the probe produced output but no
                # CPSM_PROBE lines, log the first 200 chars at debug.
                logger.debug(
                    "Correlation %s: probe output preview: %r",
                    host, output[:200],
                )
            return {}, {}

        # Build remote_session: local_pid → cwd.
        cwd_by_port = {r.source_port: r.cwd for r in remote}
        cwd_by_pid: dict[int, str] = {}
        for pid, src_port in port_by_pid.items():
            if src_port in cwd_by_port:
                cwd_by_pid[pid] = cwd_by_port[src_port]
        logger.info(
            "Correlation %s: %d/%d pids paired with remote cwds via source-port",
            host, len(cwd_by_pid), len(port_by_pid),
        )

        # Match cwd → Connection.project_folder.
        result: dict[int, str] = {}
        for pid, cwd in cwd_by_pid.items():
            for c in candidates:
                pf = getattr(c, "project_folder", "") or ""
                if _paths_equal(pf, cwd, remote_home=remote_home):
                    result[pid] = c.id
                    break
            else:
                # Log the candidate project_folders so the user can see
                # WHY no match was made (typically a ~/ vs absolute mismatch
                # or a trailing-segment difference).
                logger.info(
                    "Correlation %s: pid=%d cwd=%r did not match any of: %s",
                    host, pid, cwd,
                    ", ".join(
                        f"{c.id}=>{getattr(c, 'project_folder', '') or ''!r}"
                        for c in candidates
                    ),
                )
        # Always return the cwd map even when no Connection matched —
        # downstream UI shows it in tooltips for diagnosis.
        return result, dict(cwd_by_pid)


# ---------------------------------------------------------------------------
# SSH probe runner (default subprocess implementation)
# ---------------------------------------------------------------------------


def _run_probe_via_subprocess(
    host: str, user: str, key_path: str, port: int,
) -> str:
    """Execute the probe script on *host* via ssh.  Returns stdout text.

    Times out at 10 seconds (probe itself should finish in well under 1s;
    the budget is mostly for connection setup).  Logs stderr and the
    return code at WARNING when stdout is empty, so the user can see
    auth/permission/missing-tool failures via ``CPSM_LOG_LEVEL=INFO``.
    """
    from cpsm.platform.ssh_binary import SshBinary

    binary = SshBinary.detect()
    argv = binary.build_argv(
        host=host,
        user=user,
        port=port,
        identity_file=Path(key_path) if key_path else None,
        ssh_options=[
            "BatchMode=yes",
            "ConnectTimeout=5",
            "StrictHostKeyChecking=accept-new",
        ],
        remote_command=["sh", "-c", _PROBE_SCRIPT],
        force_tty=False,  # batch mode; no PTY needed
    )
    logger.info(
        "Correlation %s: spawning ssh probe (user=%s port=%d): %s",
        host, user, port, " ".join(argv),
    )
    result = subprocess.run(  # noqa: S603 - controlled argv from SshBinary
        argv,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if result.returncode != 0 or not stdout.strip():
        logger.warning(
            "Correlation %s: ssh probe exited rc=%d stdout=%dB stderr=%r",
            host, result.returncode, len(stdout), stderr[:500],
        )
    elif stderr.strip():
        # Some hosts emit MOTD/banners on stderr even with BatchMode; log at
        # debug so it's available without polluting INFO output.
        logger.debug("Correlation %s: ssh probe stderr=%r", host, stderr[:500])
    return stdout


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_user(candidates: list[Any]) -> str:
    for c in candidates:
        u = getattr(c, "user", "") or ""
        if u:
            return u
    return ""


def _resolve_port(candidates: list[Any]) -> int:
    for c in candidates:
        p = getattr(c, "port", 0) or 0
        if p:
            return int(p)
    return 22


def _resolve_key_path(candidates: list[Any], keys_by_id: dict[str, Any]) -> str:
    """Return a usable identity_file path from one of the candidates' keys.

    Resolves Connection.identity_file_ref via the document's ssh_keys list
    to get the actual on-disk path.  Returns ``""`` if no key is found —
    the probe will fall back to ssh-agent / ssh-config defaults, and
    BatchMode=yes will fail cleanly if neither is available.
    """
    for c in candidates:
        ref = getattr(c, "identity_file_ref", None)
        if not ref:
            continue
        key = keys_by_id.get(ref)
        if key is None:
            continue
        path = getattr(key, "private_path", "") or ""
        if path:
            return os.path.expanduser(path)
    return ""


def _paths_equal(a: str, b: str, *, remote_home: str = "") -> bool:
    """Compare two paths, expanding ``~`` against the *remote* home when
    known.

    Two strategies, in order:

    1. Exact equality after realpath/expanduser. Handles trailing slashes
       and local symlinks. (Note: ``expanduser`` here uses the *local*
       user's home, which is wrong for a remote ``~/...`` path — Strategy
       2 covers that case.)
    2. Tilde-against-remote-home expansion. When ``remote_home`` is
       provided and one side starts with ``~/`` (or is ``~``), expand it
       to ``remote_home + path[1:]`` and compare. This is the deterministic
       case: we know the remote user's $HOME from the probe's
       ``CPSM_PROBE_HOME`` line. When ``remote_home`` is empty (older
       probe, missing shell), fall back to a lenient suffix match that
       requires the suffix to start at a path component boundary AND the
       prefix before it to look home-shaped (``/home/<x>``, ``/root``,
       ``/var/lib/<x>``, etc.) so that ``~/work`` doesn't match
       ``/usr/local/work``.
    """
    if not a or not b:
        return False

    # Strategy 1 — local exact match.
    try:
        ra = os.path.realpath(os.path.expanduser(a))
        rb = os.path.realpath(os.path.expanduser(b))
        if ra == rb:
            return True
    except Exception:
        pass

    norm_a = a.strip().rstrip("/")
    norm_b = b.strip().rstrip("/")

    # Strategy 2a — explicit remote-home expansion.
    if remote_home:
        rh = remote_home.rstrip("/")
        ea = _expand_tilde_against(norm_a, rh)
        eb = _expand_tilde_against(norm_b, rh)
        if ea == eb:
            return True

    # Strategy 2b — heuristic tilde-suffix matching with home-shape guard.
    a_tail = _post_tilde(norm_a)
    b_tail = _post_tilde(norm_b)
    if a_tail and _is_home_rooted_path_with_suffix(norm_b, a_tail):
        return True
    if b_tail and _is_home_rooted_path_with_suffix(norm_a, b_tail):
        return True

    return False


def _post_tilde(path: str) -> str | None:
    """Return the part of *path* after ``~/``, or ``None`` if it doesn't
    start with ``~/``.  Empty result for plain ``~`` is treated as
    ``""`` (caller decides)."""
    if path == "~":
        return ""
    if path.startswith("~/"):
        return path[2:]
    return None


def _expand_tilde_against(path: str, home: str) -> str:
    """Replace a leading ``~/`` (or bare ``~``) in *path* with *home*."""
    if not path:
        return path
    if path == "~":
        return home
    if path.startswith("~/"):
        return home + "/" + path[2:]
    return path


# Path prefixes that "look like" a home directory.  We require the
# tilde-suffix match to occur immediately AFTER one of these, so that
# ``~/work`` matches ``/root/work`` or ``/home/ubuntu/work`` but NOT
# ``/usr/local/work`` or ``/var/log/work``.
_HOME_LIKE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/root/"),                    # root user
    re.compile(r"^/home/[^/]+/"),              # /home/<user>/
    re.compile(r"^/var/lib/[^/]+/"),           # daemon-style homes
    re.compile(r"^/Users/[^/]+/"),             # macOS
)


def _is_home_rooted_path_with_suffix(path: str, suffix: str) -> bool:
    """Return True if *path* is a home-rooted path ending in ``/<suffix>``.

    Used as a fallback when ``remote_home`` isn't known: prevents
    ``~/work`` from over-matching arbitrary directories named ``work``.
    """
    if not path or suffix is None:
        return False
    if not path.startswith("/"):
        return False
    suffix = suffix.lstrip("/")
    if not suffix:
        # ``~/`` or ``~`` — only match the bare home directories.
        return any(rx.match(path + "/") for rx in _HOME_LIKE_RES) and (
            path.count("/") <= 3
        )
    if not path.endswith("/" + suffix):
        return False
    return any(rx.match(path) for rx in _HOME_LIKE_RES)
