# -*- coding: utf-8 -*-
"""
DiscoveryService — find outside-CPSM sessions that are candidates for adoption.

Walks ``/proc`` on Linux looking for ``claude`` / ``ssh`` / ``scp`` processes
that are NOT inside any tmux server's ancestor chain (i.e. not already
managed by CPSM, since CPSM always launches connections via tmux).

For each survivor we classify the launch profile (claude-local, claude-remote,
ssh-shell) and try to match it against an existing Connection in the document
by exact cwd (claude-local) or by host+user (ssh-based profiles).

The service is platform-aware: macOS and Windows currently return ``[]``. A
``ProcSource`` protocol makes it easy to inject fake process data in tests.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol

from cpsm.data.schema import CpsmDocument

__all__ = [
    "DiscoveredSession",
    "DiscoveryService",
    "ProcInfo",
    "ProcSource",
    "append_continue_flag",
    "send_sigterm",
    "wait_for_pid_exit",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data transfer objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcInfo:
    """Minimal per-process snapshot used for classification.

    Fields mirror what a single pass over ``/proc`` can collect cheaply.
    Tests construct these directly via the :class:`ProcSource` protocol.
    """

    pid: int
    ppid: int
    comm: str  # short process name (e.g. "claude", "ssh", "tmux: server")
    cmdline: tuple[str, ...]  # split argv; empty for kernel threads
    cwd: str  # process working directory; "" if unreadable
    tty: str  # /dev/pts/N path; "" if no controlling tty


@dataclass(frozen=True)
class DiscoveredSession:
    """One outside-CPSM session that may be adoptable.

    ``suggested_connection_id`` is non-empty when the discovered process
    matches an existing Connection by exact cwd or host+user; the UI uses
    it to enable the "Adopt as <name>" action.

    ``remote_cwd`` is populated only after a successful CorrelationService
    probe (D6) — it contains the working directory of the foreground
    process on the *remote* host paired with this local pid via
    source-port correlation.  Empty for local processes and for SSH
    sessions where probing didn't run or didn't yield a cwd.
    """

    pid: int
    kind: Literal["claude-local", "claude-remote", "ssh-shell", "unknown"]
    cmdline: str  # joined for display
    cwd: str  # local cwd of the discovered process
    host: str  # populated for ssh-based kinds
    user: str  # populated for ssh-based kinds
    tty: str
    suggested_connection_id: str = ""
    remote_cwd: str = ""  # populated by CorrelationService probe


# ---------------------------------------------------------------------------
# ProcSource protocol + Linux implementation
# ---------------------------------------------------------------------------


class ProcSource(Protocol):
    """Source of process snapshots. The Linux production implementation reads
    ``/proc``; tests substitute a fake source returning canned :class:`ProcInfo`
    objects.
    """

    def list_processes(self) -> list[ProcInfo]:  # pragma: no cover - protocol
        ...


# Maximum depth for ancestor walks. /proc cycles cannot occur (processes form
# a tree rooted at PID 1), but we cap defensively against pathological data
# from a fake ProcSource in tests.
_MAX_ANCESTOR_DEPTH = 100


# Comm field for the tmux server. /proc/<pid>/stat truncates ``comm`` to
# 16 chars; "tmux: server" fits within that. Match by prefix to be tolerant
# of variants like "tmux: client" or future suffixes.
_TMUX_COMM_PREFIX = "tmux"


# ssh option flags that take a value (so we know to skip the next argv slot
# during host extraction). Not exhaustive but covers the common cases.
_SSH_OPTS_WITH_VALUE = frozenset({
    "-o", "-i", "-p", "-l", "-J", "-c", "-F", "-W", "-D", "-L", "-R",
    "-S", "-Q", "-Y", "-B", "-b", "-E", "-e", "-I", "-m",
})


class _LinuxProcSource:
    """Reads ``/proc`` to enumerate live processes. Linux-only."""

    def __init__(self, proc_root: str = "/proc") -> None:
        self._root = Path(proc_root)

    def list_processes(self) -> list[ProcInfo]:
        if not self._root.is_dir():
            return []
        out: list[ProcInfo] = []
        try:
            entries = list(self._root.iterdir())
        except OSError:
            return []
        for entry in entries:
            name = entry.name
            if not name.isdigit():
                continue
            pid = int(name)
            info = self._read_one(pid)
            if info is not None:
                out.append(info)
        return out

    def _read_one(self, pid: int) -> ProcInfo | None:
        base = self._root / str(pid)
        # /proc/<pid>/stat has the canonical (comm) and ppid fields.
        try:
            stat_raw = (base / "stat").read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
            return None

        comm, ppid = _parse_stat(stat_raw)
        if comm is None:
            return None

        # cmdline is null-separated argv. Empty for kernel threads.
        try:
            cmdline_raw = (base / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, OSError):
            cmdline_raw = b""
        cmdline = tuple(
            p.decode("utf-8", errors="replace")
            for p in cmdline_raw.split(b"\x00")
            if p
        )

        # cwd is a symlink to the working directory. Other-user processes
        # return EACCES; we just record an empty cwd in that case.
        try:
            cwd = os.readlink(base / "cwd")
        except (FileNotFoundError, PermissionError, OSError):
            cwd = ""

        # Controlling tty: /proc/<pid>/fd/0 -> /dev/pts/N for interactive
        # processes, or empty for daemons. We use it for display only.
        tty = ""
        try:
            target = os.readlink(base / "fd" / "0")
            if target.startswith("/dev/"):
                tty = target
        except (FileNotFoundError, PermissionError, OSError):
            pass

        return ProcInfo(
            pid=pid, ppid=ppid, comm=comm, cmdline=cmdline, cwd=cwd, tty=tty,
        )


# Match the (comm) field in /proc/<pid>/stat. Comm can contain spaces and
# parens, so we parse from the LAST close-paren which terminates it.
_STAT_RE = re.compile(r"^\d+ \((.*)\) \S (\d+)")


def _parse_stat(stat_raw: str) -> tuple[str | None, int]:
    """Extract (comm, ppid) from /proc/<pid>/stat content.

    /proc/<pid>/stat layout: ``"<pid> (<comm>) <state> <ppid> ..."``. The
    ``comm`` field can contain spaces and parentheses, so we use the LAST
    ``)`` to terminate it. Returns ``(None, 0)`` for unparseable input.
    """
    open_paren = stat_raw.find("(")
    close_paren = stat_raw.rfind(")")
    if open_paren < 0 or close_paren <= open_paren:
        return (None, 0)
    comm = stat_raw[open_paren + 1 : close_paren]
    rest = stat_raw[close_paren + 1 :].strip().split()
    # rest[0] is state ('R', 'S', ...), rest[1] is ppid.
    if len(rest) < 2:
        return (comm, 0)
    try:
        ppid = int(rest[1])
    except ValueError:
        ppid = 0
    return (comm, ppid)


# ---------------------------------------------------------------------------
# DiscoveryService
# ---------------------------------------------------------------------------


class DiscoveryService:
    """Find adoptable outside-CPSM sessions and match them to Connections.

    Parameters
    ----------
    proc_source:
        Custom :class:`ProcSource`. Production callers pass ``None`` to use
        the platform default (Linux ``/proc`` reader; empty list elsewhere).
    """

    def __init__(self, proc_source: ProcSource | None = None) -> None:
        if proc_source is not None:
            self._proc_source: ProcSource = proc_source
        elif _is_linux():
            self._proc_source = _LinuxProcSource()
        else:
            self._proc_source = _EmptyProcSource()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def find_outside_sessions(self, doc: CpsmDocument) -> list[DiscoveredSession]:
        """Return all outside-CPSM sessions visible in the current process
        table, classified and (where possible) matched to a Connection.
        """
        try:
            procs = self._proc_source.list_processes()
        except Exception:
            logger.exception("ProcSource.list_processes failed")
            return []
        by_pid = {p.pid: p for p in procs}

        results: list[DiscoveredSession] = []
        for proc in procs:
            if not _is_candidate_command(proc):
                continue
            if _has_tmux_ancestor(proc, by_pid):
                continue
            session = _build_session(proc, doc)
            if session is None:
                continue
            results.append(session)

        # Stable sort: by kind then pid for deterministic UI ordering.
        results.sort(key=lambda s: (s.kind, s.pid))
        return results

    def find_for_connection(
        self, doc: CpsmDocument, connection_id: str
    ) -> DiscoveredSession | None:
        """Return the first discovered session that matches *connection_id*,
        or ``None`` if none.

        Used by the launch interceptor to decide whether to prompt the user
        before spawning a duplicate of an already-running connection.
        """
        if not connection_id:
            return None
        for s in self.find_outside_sessions(doc):
            if s.suggested_connection_id == connection_id:
                return s
        return None


class _EmptyProcSource:
    """Stub ProcSource for non-Linux platforms; always returns []."""

    def list_processes(self) -> list[ProcInfo]:
        return []


# ---------------------------------------------------------------------------
# Classification & matching
# ---------------------------------------------------------------------------


def _is_linux() -> bool:
    import sys
    return sys.platform.startswith("linux")


def _is_candidate_command(proc: ProcInfo) -> bool:
    """Coarse filter: only keep processes that look like potential adoption
    candidates. Saves us from walking parent chains for every kernel
    thread and shell on the system.

    Matches Claude only by the exact basename ``claude``; we deliberately
    do NOT prefix-match ``claude-*`` because that would surface unrelated
    binaries like ``claude-tools`` / ``claude-helper`` as adoption
    candidates.
    """
    if not proc.cmdline:
        return False
    name = _basename(proc.cmdline[0])
    if name == "claude":
        return True
    if name in ("ssh", "scp"):
        return True
    return False


def _basename(path: str) -> str:
    if not path:
        return ""
    return os.path.basename(path)


def _has_tmux_ancestor(proc: ProcInfo, by_pid: dict[int, ProcInfo]) -> bool:
    """True if any ancestor (including the process itself) has a comm
    starting with ``tmux``. Walks up to ``_MAX_ANCESTOR_DEPTH`` levels."""
    cur: ProcInfo | None = proc
    seen: set[int] = set()
    for _ in range(_MAX_ANCESTOR_DEPTH):
        if cur is None or cur.pid in seen:
            return False
        seen.add(cur.pid)
        if cur.comm.startswith(_TMUX_COMM_PREFIX):
            return True
        if cur.ppid <= 1:
            return False
        cur = by_pid.get(cur.ppid)
    return False


def _build_session(
    proc: ProcInfo, doc: CpsmDocument
) -> DiscoveredSession | None:
    """Classify *proc* and return a DiscoveredSession, or None to skip."""
    cmd0 = _basename(proc.cmdline[0]) if proc.cmdline else ""
    cmdline_display = " ".join(proc.cmdline)

    if cmd0 == "claude":
        suggested = _match_local_by_cwd(doc, proc.cwd)
        return DiscoveredSession(
            pid=proc.pid,
            kind="claude-local",
            cmdline=cmdline_display,
            cwd=proc.cwd,
            host="",
            user="",
            tty=proc.tty,
            suggested_connection_id=suggested,
        )

    if cmd0 in ("ssh", "scp"):
        host, user = _parse_ssh_args(proc.cmdline)
        if not host:
            return None
        kind: Literal["claude-remote", "ssh-shell"] = (
            "claude-remote" if _ssh_args_invoke_claude(proc.cmdline) else "ssh-shell"
        )
        suggested = _match_remote_by_host_user(doc, kind, host, user)
        return DiscoveredSession(
            pid=proc.pid,
            kind=kind,
            cmdline=cmdline_display,
            cwd=proc.cwd,
            host=host,
            user=user,
            tty=proc.tty,
            suggested_connection_id=suggested,
        )

    return None


def _parse_ssh_args(args: tuple[str, ...]) -> tuple[str, str]:
    """Return ``(host, user)`` extracted from an ssh-like argv. Best-effort —
    handles ``ssh user@host``, ``ssh -l user host``, and ``ssh host`` with
    intervening flags. Returns ``("", "")`` if no host is parseable."""
    user = ""
    host = ""
    i = 1  # skip argv[0] (the program name)
    while i < len(args):
        a = args[i]
        if a == "-l" and i + 1 < len(args):
            user = args[i + 1]
            i += 2
            continue
        if a in _SSH_OPTS_WITH_VALUE:
            i += 2  # skip flag and its value
            continue
        if a.startswith("-"):
            i += 1
            continue
        if "@" in a:
            user_part, host_part = a.split("@", 1)
            if not user:
                user = user_part
            host = host_part
        else:
            host = a
        # scp argv embeds remote paths as ``host:/remote/path``; strip the
        # ``:path`` suffix so we don't store a corrupt host string. ssh
        # hosts never contain ``:`` (port lives in the ``-p`` flag).
        if ":" in host:
            host = host.split(":", 1)[0]
        break
    return (host, user)


def _ssh_args_invoke_claude(args: tuple[str, ...]) -> bool:
    """Heuristic: returns True if any positional arg passed to ssh contains
    'claude', suggesting the remote command line invokes Claude.

    False positives are possible (a host literally named "claude.example.com"
    would trigger this). The cost of a false positive is just classifying
    as claude-remote vs ssh-shell, which only changes the suggested action
    label — both adopt via kill-and-relaunch.
    """
    # Skip argv[0] and the host token itself (best-effort: anything after
    # the first non-flag positional we treat as command-line content).
    seen_host = False
    i = 1
    while i < len(args):
        a = args[i]
        if a in _SSH_OPTS_WITH_VALUE:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        if not seen_host:
            seen_host = True
            i += 1
            continue
        if "claude" in a:
            return True
        i += 1
    return False


def _match_local_by_cwd(doc: CpsmDocument, cwd: str) -> str:
    """Return a matching claude-local Connection.id by exact cwd, else ""."""
    if not cwd:
        return ""
    target = _canonical(cwd)
    if not target:
        return ""
    for conn in doc.connections:
        if getattr(conn, "launch_profile", "") != "claude-local":
            continue
        pf = getattr(conn, "project_folder", "") or ""
        if _canonical(pf) == target:
            return conn.id
    return ""


def _match_remote_by_host_user(
    doc: CpsmDocument, kind: str, host: str, user: str
) -> str:
    """Return a matching Connection.id by host+user, else "".

    Matches across both SSH-based profiles (``claude-remote`` and
    ``ssh-shell``): a manually-opened ``ssh root@host`` is classified as
    ``ssh-shell`` because there's no ``claude`` in the local argv, but it
    could still correspond to a ``claude-remote`` Connection (the user
    just hasn't run claude yet, or ran it inside an interactive shell).
    Prefers a same-profile match when both exist.

    When the candidate set has **multiple** Connections that all match
    host+user (e.g. three Connections to ``10.10.10.44`` differing only
    by ``project_folder``), this is genuine local ambiguity — we cannot
    pick correctly without knowing the remote cwd.  Return ``""`` in
    that case and let CorrelationService disambiguate via remote probe.
    Picking one arbitrarily would mis-tag every session whose actual
    project is not the first match.
    """
    if not host:
        return ""
    preferred = "claude-remote" if kind == "claude-remote" else "ssh-shell"
    same_profile: list[str] = []
    other_profile: list[str] = []
    for conn in doc.connections:
        cprofile = getattr(conn, "launch_profile", "")
        if cprofile not in ("claude-remote", "ssh-shell"):
            continue
        if getattr(conn, "host", "") != host:
            continue
        # If the discovered ssh argv didn't set a user (bare host), accept
        # any matching connection. Otherwise require user equality.
        conn_user = getattr(conn, "user", "") or ""
        if user and conn_user and conn_user != user:
            continue
        if cprofile == preferred:
            same_profile.append(conn.id)
        else:
            other_profile.append(conn.id)
    bucket = same_profile or other_profile
    if len(bucket) == 1:
        return bucket[0]
    # 0 = no match, return ""; 2+ = ambiguous, defer to correlation
    return ""


def is_pid_alive(pid: int) -> bool:
    """Return True if *pid* is still a running process.

    Uses ``signal 0`` (no-op delivery) which raises ``ProcessLookupError``
    when the pid is gone and ``PermissionError`` when it's a foreign-user
    process that still exists.  We treat PermissionError as "alive" since
    the process is observably present.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def send_sigterm(pid: int) -> bool:
    """Send SIGTERM to *pid*. Returns True on success, False if the pid is
    already gone or we lack permission to signal it."""
    import signal
    if pid <= 0:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def send_sigkill(pid: int) -> bool:
    """Send SIGKILL to *pid*. Returns True on success, False otherwise."""
    import signal
    if pid <= 0:
        return False
    try:
        os.kill(pid, signal.SIGKILL)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def wait_for_pid_exit(
    pid: int,
    *,
    timeout_s: float = 10.0,
    poll_interval_s: float = 0.2,
    sleep: "Callable[[float], None] | None" = None,
) -> bool:
    """Poll until *pid* is gone or *timeout_s* elapses.

    Returns True if the pid exited within the timeout, False otherwise.
    The injectable *sleep* parameter exists so tests can drive the
    polling loop deterministically without real wall-clock waits.
    """
    import time
    if pid <= 0:
        return True
    sleeper = sleep if sleep is not None else time.sleep
    elapsed = 0.0
    while elapsed < timeout_s:
        if not is_pid_alive(pid):
            return True
        sleeper(poll_interval_s)
        elapsed += poll_interval_s
    return not is_pid_alive(pid)


def append_continue_flag(claude_options: str) -> str:
    """Return *claude_options* with ``--continue`` appended if not already
    present. Used during adoption so the relaunched Claude resumes the
    same conversation that was visible in the outside terminal."""
    parts = (claude_options or "").split()
    if "--continue" in parts:
        return claude_options or ""
    if claude_options:
        return f"{claude_options} --continue"
    return "--continue"


def _canonical(path: str) -> str:
    """Resolve ``~`` and symlinks for a stable equality test. Empty input
    returns "". Failures (non-existent path, permission errors) fall back
    to expanduser-only normalization."""
    if not path:
        return ""
    try:
        expanded = os.path.expanduser(path)
        return os.path.realpath(expanded)
    except Exception:
        try:
            return os.path.expanduser(path)
        except Exception:
            return path
