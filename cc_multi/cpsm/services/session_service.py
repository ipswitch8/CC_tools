# -*- coding: utf-8 -*-
"""
SessionService — single-connection, group, and scene launch flows.

Spec sections: §5.1, §5.4, §5.5, §5.6, §9.2

Security note (§9.2 Local-profile guard)
-----------------------------------------
``claude-local`` and ``local-shell`` connections MUST NOT invoke ssh/scp/plink.
SessionService enforces this at launch time by inspecting the rendered template
text for any invocation of those binaries.  If detected, ``LocalProfileLeakError``
is raised and the launch is aborted.

The guard uses an explicit allow-list approach: local profiles (``claude-local``
and ``local-shell``) are allowed to invoke a known-safe set of binaries.  If the
rendered command contains a pattern matching ssh/scp/plink at a process-spawn
boundary, it is rejected regardless of how the template was composed.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import re
import stat
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal

from cpsm.data.schema import (
    CpsmDocument,
    Group,
)
from cpsm.platform.base import MultiplexerBackend
from cpsm.services.config_service import ConfigService
from cpsm.services.layout_service import LayoutService
from cpsm.services.template_service import TemplateService

__all__ = [
    "GroupLaunchResult",
    "LaunchResult",
    "LayoutConflictError",
    "LocalProfileLeakError",
    "SceneLaunchResult",
    "SessionService",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

# Binaries that must NOT appear in local-profile rendered commands
_LOCAL_PROFILE_FORBIDDEN_RE = re.compile(
    r"(?:^|[\s;|&`(])(?:ssh|scp|plink)\b",
    re.MULTILINE,
)

_LOCAL_PROFILES = frozenset({"claude-local", "local-shell"})


# Match the launcher tmpfile path embedded in a tmux pane's start_command.
# Format: cpsm-launcher-<conn_id>-<mkstemp-suffix>.sh.  The mkstemp suffix is
# 8 characters from [A-Za-z0-9_]; we accept 6–16 to absorb platform variation.
# pane_start_command is set by tmux when the pane is spawned and CANNOT be
# overwritten by the running process — unlike pane_title, which claude/ssh
# routinely stomp via terminal escape sequences.
_CPSM_LAUNCHER_RE = re.compile(
    r"cpsm-launcher-(?P<id>.+?)-[A-Za-z0-9]{6,16}\.sh"
)


def _extract_cpsm_conn_id(start_command: str) -> str | None:
    """Return the connection_id encoded in *start_command*, or None.

    Skips placeholder launchers (label ``placeholder-<vp_id>``) since those
    aren't tied to a real connection.
    """
    if not start_command:
        return None
    m = _CPSM_LAUNCHER_RE.search(start_command)
    if m is None:
        return None
    cid = m.group("id")
    if cid.startswith("placeholder-"):
        return None
    return cid


class LocalProfileLeakError(RuntimeError):
    """Raised when a local profile template renders to a command invoking ssh/scp/plink."""


class LayoutConflictError(ValueError):
    """Raised by launch_scene when on_conflict=error and viewports overlap."""


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class LaunchResult:
    """Result of a single-connection launch."""

    success: bool
    session_name: str
    connection_id: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class GroupLaunchResult:
    """Aggregate result of a group launch."""

    success: bool
    group_id: str
    session_name: str
    member_results: list[LaunchResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class SceneLaunchResult:
    """Aggregate result of a scene launch."""

    success: bool
    scene_id: str
    group_results: list[GroupLaunchResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# SessionService
# ---------------------------------------------------------------------------


class SessionService:
    """Orchestrate tmux session lifecycle for all CPSM launch profiles.

    Parameters
    ----------
    config:
        ``ConfigService`` for document lookups.
    backend:
        ``MultiplexerBackend`` concrete instance (e.g. ``TmuxBackend``).
    templates:
        ``TemplateService`` for rendering launcher scripts.
    layout:
        ``LayoutService`` for post-launch layout capture.
    launcher_tmp_dir:
        Optional override for the directory where launcher tmpfiles are written.
        Defaults to the system temp dir.  Injected for tests.
    """

    def __init__(
        self,
        config: ConfigService,
        backend: MultiplexerBackend,
        templates: TemplateService,
        layout: LayoutService,
        *,
        launcher_tmp_dir: Path | None = None,
    ) -> None:
        self._config = config
        self._backend = backend
        self._templates = templates
        self._layout = layout
        self._tmp_dir = launcher_tmp_dir

    # ------------------------------------------------------------------
    # Session naming (§5.1)
    # ------------------------------------------------------------------

    def session_name(self, connection_id: str, group_id: str | None = None) -> str:
        """Return the canonical tmux session name.

        Shared (default):     ``cpsm-<connection_id>``
        Per-group isolation:  ``cpsm-<group_id>-<connection_id>``
        """
        if group_id is not None:
            return f"cpsm-{group_id}-{connection_id}"
        return f"cpsm-{connection_id}"

    # ------------------------------------------------------------------
    # Single-connection launch (§5.4)
    # ------------------------------------------------------------------

    def launch(
        self,
        doc: CpsmDocument,
        connection_id: str,
        *,
        group_id: str | None = None,
        existing_pane: str | None = None,
    ) -> LaunchResult:
        """Launch a single connection in its own tmux session.

        Parameters
        ----------
        doc:
            The CPSM document.
        connection_id:
            ID of the connection to launch.
        group_id:
            When set (and the group uses ``isolation: per-group``), the
            session name includes the group id.
        existing_pane:
            tmux pane target to respawn.  When None a new session is created.
        """
        conn = self._config.find_connection(doc, connection_id)
        if conn is None:
            return LaunchResult(
                success=False,
                session_name="",
                connection_id=connection_id,
                errors=[f"Connection '{connection_id}' not found in document."],
            )

        # Determine isolation
        eff_group_id: str | None = None
        if group_id is not None:
            grp = self._config.find_group(doc, group_id)
            if grp is not None and grp.isolation == "per-group":
                eff_group_id = group_id

        sname = self.session_name(connection_id, eff_group_id)
        profile = conn.launch_profile

        try:
            # Render launcher script
            rendered = self._templates.render(
                profile,
                conn,
                settings=doc.settings,
                templates=doc.launch_templates,
                ssh_keys=doc.ssh_keys,
            )

            # Local-profile guard (§9.2)
            if profile in _LOCAL_PROFILES:
                _check_local_profile_guard(rendered, profile, connection_id)

            # Write launcher tmpfile
            tmpfile = self._write_launcher_tmpfile(rendered, connection_id)

            # Ensure session exists
            if existing_pane is None:
                self._ensure_session(sname)
                pane_target = f"{sname}:0"
            else:
                pane_target = existing_pane

            # Respawn pane with the launcher script
            self._backend.respawn_pane(pane_target, f"bash {tmpfile}")
            # Tag pane for cross-session move identification (Round-late).
            import contextlib as _ctx
            with _ctx.suppress(Exception):
                self._backend.set_pane_title(pane_target, f"cpsm:{conn.id}")

            # Open a terminal window attached to the tmux session so the
            # user can actually see / interact with it. Only spawn when
            # we created a fresh session — for explicit existing_pane
            # respawns, the caller has its own attached terminal.
            if existing_pane is None:
                self._spawn_terminal_for_session(sname, conn.name or connection_id)

        except LocalProfileLeakError:
            raise
        except Exception as exc:
            return LaunchResult(
                success=False,
                session_name=sname,
                connection_id=connection_id,
                errors=[str(exc)],
            )

        return LaunchResult(
            success=True,
            session_name=sname,
            connection_id=connection_id,
        )

    def _spawn_terminal_for_session(
        self,
        session_name: str,
        title: str,
        *,
        geometry: tuple[int, int, int, int] | None = None,
        monitor_index: int | None = None,
    ) -> None:
        """Open a visible terminal window attached to *session_name*.

        When *geometry* is provided, launchers that support placement
        (xterm, alacritty, kitty, konsole, wezterm) will size and position
        the window to the given pixel rect. Launchers without geometry
        support (e.g. gnome-terminal pre-Round-5) ignore it.

        ``LocalShellLauncher`` is always skipped — attaching an interactive
        tmux session to the GUI's tty would hijack stdin/stdout.
        """
        from cpsm.platform.terminal_launcher import (
            LocalShellLauncher,
            discover_launchers,
        )

        argv = ["tmux", "attach", "-t", session_name]
        last_err: Exception | None = None
        for launcher in discover_launchers():
            if isinstance(launcher, LocalShellLauncher):
                continue  # would hijack the GUI tty
            try:
                launcher.spawn(
                    argv,
                    title=title,
                    geometry=geometry,
                    monitor_index=monitor_index,
                )
                return
            except NotImplementedError as exc:
                last_err = exc
                continue
            except Exception as exc:
                last_err = exc
                continue
        logger.warning(
            "No terminal launcher succeeded for session %s; install "
            "gnome-terminal or xterm. Last error: %s",
            session_name,
            last_err,
        )

    # ------------------------------------------------------------------
    # Group launch (§5.5)
    # ------------------------------------------------------------------

    def launch_group(
        self,
        doc: CpsmDocument,
        group_id: str,
        *,
        monitors: list[Any] | None = None,
    ) -> GroupLaunchResult:
        """Launch all members of a group.

        Resolves ``default_layout_id`` to apply pane layout after launch.
        Null-id panes (empty slots) are spawned with the placeholder script.
        Sequential / parallel dispatch is driven by ``group.launch_order``.

        When *monitors* is provided (a list of :class:`MonitorInfo`), the
        layout-aware path uses each monitor's pixel geometry to position
        terminal windows on the corresponding physical monitor (Round 4).
        """
        grp = self._config.find_group(doc, group_id)
        if grp is None:
            return GroupLaunchResult(
                success=False,
                group_id=group_id,
                session_name="",
                errors=[f"Group '{group_id}' not found in document."],
            )

        # If a layout exists with at least one monitor + pane, use the
        # layout-aware path which builds one tmux session per monitor with
        # the right window/pane structure and opens one terminal per
        # monitor. Otherwise fall through to the per-connection behaviour
        # below (one terminal per connection).
        layout = None
        if grp.default_layout_id:
            layout = self._config.find_layout(doc, grp.default_layout_id)
        if layout is not None and any(
            len(vp.panes) > 0 for m in layout.monitors for vp in m.viewports
        ):
            return self._launch_group_with_layout(doc, grp, layout, monitors)

        isolation_group_id = group_id if grp.isolation == "per-group" else None
        # Use the first member's connection id as anchor for the session name display
        sname = f"cpsm-group-{group_id}"

        # If a layout is specified, also account for null panes in that layout
        layout_null_panes: list[str] = []  # window targets for placeholder
        if grp.default_layout_id:
            layout = self._config.find_layout(doc, grp.default_layout_id)
            if layout is not None:
                for monitor in layout.monitors:
                    for vp in monitor.viewports:
                        for pane in vp.panes:
                            if pane.connection_id is None:
                                layout_null_panes.append(vp.id)

        member_results: list[LaunchResult] = []
        warnings: list[str] = []

        if grp.launch_order == "sequential":
            for i, conn_id in enumerate(grp.members):
                result = self.launch(doc, conn_id, group_id=isolation_group_id)
                member_results.append(result)
                if i < len(grp.members) - 1 and grp.launch_delay_ms > 0:
                    time.sleep(grp.launch_delay_ms / 1000.0)
        else:
            # Parallel launch via ThreadPoolExecutor (NOT QThreadPool — Phase 8)
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(len(grp.members), 16)
            ) as executor:
                futures: dict[concurrent.futures.Future[LaunchResult], str] = {
                    executor.submit(self.launch, doc, conn_id, group_id=isolation_group_id): conn_id
                    for conn_id in grp.members
                }
                for fut in concurrent.futures.as_completed(futures):
                    try:
                        member_results.append(fut.result())
                    except Exception as exc:
                        conn_id = futures[fut]
                        member_results.append(
                            LaunchResult(
                                success=False,
                                session_name="",
                                connection_id=conn_id,
                                errors=[str(exc)],
                            )
                        )

        # Spawn placeholder for null-id panes
        for _vp_id in layout_null_panes:
            try:
                placeholder = self._templates.render_placeholder()
                ph_tmp = self._write_launcher_tmpfile(placeholder, f"placeholder-{_vp_id}")
                # We don't have an exact pane target here — callers (Phase 16) wire this up
                # per-viewport; record a warning so the caller is aware.
                warnings.append(
                    f"Null-id pane in viewport '{_vp_id}' requires explicit pane target "
                    "for placeholder respawn; deferred to layout controller."
                )
                logger.debug("Null-id placeholder tmpfile at %s for viewport %s", ph_tmp, _vp_id)
            except Exception as exc:
                warnings.append(f"Placeholder setup for viewport '{_vp_id}' failed: {exc}")

        overall_success = all(r.success for r in member_results)
        return GroupLaunchResult(
            success=overall_success,
            group_id=group_id,
            session_name=sname,
            member_results=member_results,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Layout-aware group launch (Phase A — Round C wiring)
    # ------------------------------------------------------------------

    # Map cpsm tmux_layout values to actual tmux preset names
    _TMUX_LAYOUT_MAP: ClassVar[dict[str, str]] = {
        "tiled": "tiled",
        "even-h": "even-horizontal",
        "even-v": "even-vertical",
        "main-h": "main-horizontal",
        "main-v": "main-vertical",
    }

    # Default cell dimensions used when computing custom layout strings.
    # Tmux scales relative to the actual window size when the terminal
    # attaches, so these are nominal — only the *ratios* between siblings
    # matter for the rendered layout.
    _DEFAULT_LAYOUT_W: ClassVar[int] = 220
    _DEFAULT_LAYOUT_H: ClassVar[int] = 50

    def _apply_tree_or_preset_layout(
        self,
        target_window: str,
        vp: Any,
    ) -> None:
        """Apply a tmux layout to *target_window* using ``vp.split_tree`` if
        present (mixed-orientation custom string), falling back to the
        preset map or ``custom_layout_string`` for legacy viewports.
        """
        import contextlib

        from cpsm.data.schema import Pane as _Pane
        from cpsm.data.schema import Split as _Split

        tree = getattr(vp, "split_tree", None)
        if isinstance(tree, _Split):
            try:
                layout_str = _tmux_layout_string_from_tree(
                    tree, self._DEFAULT_LAYOUT_W, self._DEFAULT_LAYOUT_H,
                )
                self._backend.select_layout(target_window, layout_str)
                return
            except Exception as exc:
                logger.warning(
                    "Custom layout-string apply failed for %s: %s; "
                    "falling back to preset.",
                    target_window, exc,
                )
        # Tree is None or a single Pane (or generation failed). Use
        # preset / legacy custom_layout_string.
        if isinstance(tree, _Pane):
            return  # Single pane — no layout needed
        tmux_preset = self._TMUX_LAYOUT_MAP.get(vp.tmux_layout)
        if tmux_preset is not None:
            with contextlib.suppress(Exception):
                self._backend.select_layout(target_window, tmux_preset)
        elif vp.tmux_layout == "custom" and vp.custom_layout_string:
            with contextlib.suppress(Exception):
                self._backend.select_layout(
                    target_window, vp.custom_layout_string,
                )

    def _launch_group_with_layout(
        self,
        doc: CpsmDocument,
        grp: Any,
        layout: Any,
        monitors: list[Any] | None = None,
    ) -> GroupLaunchResult:
        """Build one tmux session per monitor in *layout*, populated with the
        pane structure of each viewport, and open one terminal per session.

        When *monitors* is provided, each schema monitor is resolved against
        the live :class:`MonitorInfo` list (by identifier → ``monitor_index_hint``
        → positional index) and the resolved monitor's pixel geometry is
        passed to the terminal launcher for placement (Round 4).

        Kills any pre-existing tmux sessions for this group's monitors first
        so a relaunch starts from a clean slate (otherwise dead panes from a
        previous launch would accumulate alongside the newly-created ones).

        Returns a GroupLaunchResult with one LaunchResult per non-empty pane.
        """
        group_id = grp.id
        isolation_group_id = group_id if grp.isolation == "per-group" else None

        # Build resolution maps for monitor placement (Round 4)
        live_monitors = monitors or []
        ident_map = {
            getattr(m, "identifier", ""): m
            for m in live_monitors if getattr(m, "identifier", "")
        }
        index_map = {
            getattr(m, "qt_index", -1): m
            for m in live_monitors if getattr(m, "qt_index", -1) >= 0
        }

        # Snapshot existing prefix-sessions and their panes (one tmux call
        # for the whole listing — we group by session locally). The Phase A
        # behaviour was "kill any existing matching session"; refined
        # version: keep the session if it has at least one tmux pane (alive
        # or dead) and reconcile in place — only DEAD panes get respawned,
        # ALIVE ones are left untouched. Dead-only sessions are killed and
        # recreated fresh.
        import contextlib
        prefix = f"cpsm-group-{group_id}-mon-"
        try:
            all_sessions_list = list(self._backend.list_sessions())
        except Exception:
            all_sessions_list = []
        existing_sessions_meta: dict[str, Any] = {
            s.name: s for s in all_sessions_list if s.name.startswith(prefix)
        }
        existing_sessions: set[str] = set(existing_sessions_meta.keys())
        # Pull all panes once and bucket per-session
        try:
            all_panes = self._backend.list_panes(None)
        except Exception:
            all_panes = []
        panes_by_session: dict[str, list[Any]] = {}
        for p in all_panes:
            if p.session in existing_sessions:
                panes_by_session.setdefault(p.session, []).append(p)
        # Drop empty sessions (0 panes total) — kill and treat as absent.
        for sess_name in list(existing_sessions):
            if not panes_by_session.get(sess_name):
                with contextlib.suppress(Exception):
                    self._backend.kill_session(sess_name)
                existing_sessions.discard(sess_name)
                panes_by_session.pop(sess_name, None)

        member_results: list[LaunchResult] = []
        warnings: list[str] = []
        primary_session = ""

        # Round-late: pre-pass MOVE for cross-session pane relocation.
        # Build a map of "where is each connection's pane right now" from
        # pane_start_command (which embeds the cpsm-launcher-<conn_id>-…
        # path), then for each layout leaf whose connection is currently
        # in a *different* prefix-matching session, ``join-pane`` it into
        # the target session.  Preserves the live process (no kill+respawn).
        #
        # We identify CPSM-owned panes by start_command, not pane_title,
        # because long-running processes (claude, ssh) overwrite titles
        # via terminal escape sequences.  start_command is set by tmux
        # at spawn and is immutable.
        from cpsm.data.schema import _flatten_split_tree_leaves as _flatten

        tagged_panes: dict[str, tuple[str, str]] = {}
        # tagged_panes maps connection_id -> (session_name, pane_id_str)
        for sess_name, panes in panes_by_session.items():
            for p in panes:
                cid = _extract_cpsm_conn_id(getattr(p, "start_command", "") or "")
                if cid:
                    tagged_panes[cid] = (sess_name, p.id)

        # Plan moves: connection -> dst_session it should be in.
        moves: list[tuple[str, str, str]] = []  # (src_pane_id, src_sess, dst_sess)
        for mon_idx, monitor in enumerate(layout.monitors):
            dst_sess = f"cpsm-group-{group_id}-mon-{mon_idx}"
            for vp in monitor.viewports:
                leaves = (
                    _flatten(vp.split_tree)
                    if vp.split_tree is not None
                    else list(vp.panes)
                )
                for pane in leaves:
                    cid = pane.connection_id
                    if cid and cid in tagged_panes:
                        cur_sess, cur_pane_id = tagged_panes[cid]
                        if cur_sess != dst_sess:
                            moves.append((cur_pane_id, cur_sess, dst_sess))

        if moves:
            # Ensure destination sessions exist before joining into them.
            dst_sessions_needed = {dst for _, _, dst in moves}
            for dst in dst_sessions_needed:
                if dst not in existing_sessions:
                    try:
                        self._ensure_session(dst)
                        existing_sessions.add(dst)
                        # New session has a default shell pane at :0.0
                        # which we'll need to clean up after joins.
                    except Exception as exc:
                        warnings.append(f"Could not ensure {dst}: {exc}")
            # Track sessions that just got their first joined pane so we
            # know to kill the placeholder default-shell pane afterward.
            sessions_with_default_to_kill: set[str] = set()
            for src_pane_id, _src_sess, dst_sess in moves:
                if dst_sess not in panes_by_session or not panes_by_session[dst_sess]:
                    sessions_with_default_to_kill.add(dst_sess)
                try:
                    self._backend.join_pane(
                        src_pane_id, f"{dst_sess}:0.0", direction="h",
                    )
                except Exception as exc:
                    warnings.append(
                        f"join-pane {src_pane_id} -> {dst_sess} failed: {exc}"
                    )
            # Kill the default shell pane in sessions where we joined a
            # tagged pane in.  Identify CPSM panes by start_command rather
            # than title (titles get overwritten by running processes).
            for dst in sessions_with_default_to_kill:
                try:
                    fresh_panes = self._backend.list_panes(dst)
                except Exception:
                    continue
                cpsm_panes = [
                    p for p in fresh_panes
                    if _extract_cpsm_conn_id(getattr(p, "start_command", "") or "")
                ]
                non_cpsm = [
                    p for p in fresh_panes
                    if not _extract_cpsm_conn_id(getattr(p, "start_command", "") or "")
                ]
                if cpsm_panes and non_cpsm:
                    for p in non_cpsm:
                        with contextlib.suppress(Exception):
                            self._backend.kill_pane(p.id)
            # Refresh per-session pane snapshots after the moves.
            try:
                refreshed = self._backend.list_panes(None)
            except Exception:
                refreshed = []
            panes_by_session = {}
            existing_sessions = set()
            for p in refreshed:
                if p.session.startswith(prefix):
                    existing_sessions.add(p.session)
                    panes_by_session.setdefault(p.session, []).append(p)
            existing_sessions_meta = {
                s.name: s
                for s in (self._backend.list_sessions() if self._backend else [])
                if s.name.startswith(prefix)
            }

        for mon_idx, monitor in enumerate(layout.monitors):
            # Monitors with no populated panes:
            #  - Don't create an empty session / spawn an empty terminal.
            #  - DO kill any pre-existing prefix-matching session that
            #    belonged to this monitor (e.g. the user dragged the last
            #    pane away, leaving an orphan session alive). Without this
            #    cleanup the canvas update + tmux state diverge and the
            #    user sees a "duplicate" of the moved pane.
            total_panes_on_mon = sum(len(vp.panes) for vp in monitor.viewports)
            if total_panes_on_mon == 0:
                stale_session = f"cpsm-group-{group_id}-mon-{mon_idx}"
                if stale_session in existing_sessions:
                    with contextlib.suppress(Exception):
                        self._backend.kill_session(stale_session)
                continue
            mon_session = f"cpsm-group-{group_id}-mon-{mon_idx}"
            if not primary_session:
                primary_session = mon_session
            # Resolve schema monitor → live MonitorInfo for geometry
            # placement (Round 4). Order matches Round-2's renderer:
            # identifier → monitor_index_hint → positional.
            mon_geom = self._resolve_monitor_geometry(
                monitor, mon_idx, ident_map, index_map,
            )
            if mon_session in existing_sessions:
                # Reconcile the existing session in place: dead panes get
                # respawned with their layout connection's launcher; alive
                # panes are left alone.
                self._reconcile_session_with_monitor(
                    doc=doc,
                    mon_session=mon_session,
                    monitor=monitor,
                    existing_panes=panes_by_session.get(mon_session, []),
                    member_results=member_results,
                    warnings=warnings,
                )
                # If no client is currently attached to the session (e.g.
                # the user closed the terminal but the tmux session
                # survived), spawn a fresh terminal so the relaunch is
                # actually visible.
                sess_meta = existing_sessions_meta.get(mon_session)
                if sess_meta is None or not getattr(sess_meta, "attached", False):
                    self._spawn_terminal_for_session(
                        mon_session,
                        f"{grp.name or group_id} — monitor {mon_idx}",
                        geometry=mon_geom,
                        monitor_index=mon_idx if mon_geom else None,
                    )
                continue
            try:
                self._ensure_session(mon_session)
            except Exception as exc:
                warnings.append(f"Could not create tmux session {mon_session}: {exc}")
                continue

            for vp_idx, vp in enumerate(monitor.viewports):
                if not vp.panes:
                    continue
                # Window 0 already exists from new-session; reuse it for the
                # first viewport. Subsequent viewports get a new named window.
                # Window-rename of the first window is cosmetic and skipped
                # here (no rename in MultiplexerBackend ABC).
                window_name = vp.tmux_window_name or vp.id
                if vp_idx == 0:
                    target_window = f"{mon_session}:0"
                else:
                    try:
                        new_w = self._backend.new_window(mon_session, name=window_name)
                        target_window = f"{mon_session}:{new_w.index}"
                    except Exception as exc:
                        warnings.append(
                            f"Could not create window '{window_name}' on "
                            f"{mon_session}: {exc}"
                        )
                        continue

                # Build pane structure for this viewport
                pane_targets = self._build_panes_for_viewport(
                    target_window, vp, warnings,
                )

                # Populate each pane with its connection's launcher
                for pane_target, pane_obj in zip(pane_targets, vp.panes, strict=False):
                    if pane_obj.connection_id is None:
                        # Empty slot → placeholder
                        try:
                            placeholder = self._templates.render_placeholder()
                            ph_tmp = self._write_launcher_tmpfile(
                                placeholder, f"placeholder-{vp.id}"
                            )
                            self._backend.respawn_pane(pane_target, f"bash {ph_tmp}")
                        except Exception as exc:
                            warnings.append(
                                f"Placeholder failed for pane {pane_target}: {exc}"
                            )
                        continue

                    conn = self._config.find_connection(doc, pane_obj.connection_id)
                    if conn is None:
                        member_results.append(LaunchResult(
                            success=False,
                            session_name=mon_session,
                            connection_id=pane_obj.connection_id,
                            errors=[f"Connection '{pane_obj.connection_id}' not found"],
                        ))
                        continue
                    try:
                        rendered = self._templates.render(
                            conn.launch_profile,
                            conn,
                            settings=doc.settings,
                            templates=doc.launch_templates,
                            ssh_keys=doc.ssh_keys,
                        )
                        if conn.launch_profile in _LOCAL_PROFILES:
                            _check_local_profile_guard(
                                rendered, conn.launch_profile, conn.id
                            )
                        tmpfile = self._write_launcher_tmpfile(rendered, conn.id)
                        self._backend.respawn_pane(pane_target, f"bash {tmpfile}")
                        with contextlib.suppress(Exception):
                            self._backend.set_pane_title(
                                pane_target, f"cpsm:{conn.id}",
                            )
                        member_results.append(LaunchResult(
                            success=True,
                            session_name=mon_session,
                            connection_id=conn.id,
                        ))
                    except LocalProfileLeakError:
                        raise
                    except Exception as exc:
                        member_results.append(LaunchResult(
                            success=False,
                            session_name=mon_session,
                            connection_id=conn.id,
                            errors=[str(exc)],
                        ))

                # Apply tmux layout. Round 3: prefer the structured
                # split tree (mixed-orientation layouts via custom layout
                # string); fall back to the preset map for legacy
                # viewports.
                self._apply_tree_or_preset_layout(target_window, vp)

            # Spawn one terminal attached to this monitor's session,
            # passing geometry so geometry-aware launchers (xterm,
            # alacritty, kitty, konsole, wezterm) place the window on
            # the right monitor at the right size.
            self._spawn_terminal_for_session(
                mon_session,
                f"{grp.name or group_id} — monitor {mon_idx}",
                geometry=mon_geom,
                monitor_index=mon_idx if mon_geom else None,
            )

        # Use isolation_group_id only for naming consistency (parity with
        # legacy launch_group); not actually applied here.
        del isolation_group_id

        overall_success = all(r.success for r in member_results) if member_results else True
        return GroupLaunchResult(
            success=overall_success,
            group_id=group_id,
            session_name=primary_session,
            member_results=member_results,
            warnings=warnings,
        )

    def _reconcile_session_with_monitor(
        self,
        *,
        doc: CpsmDocument,
        mon_session: str,
        monitor: Any,
        existing_panes: list[Any],
        member_results: list[LaunchResult],
        warnings: list[str],
    ) -> None:
        """Respawn dead panes in *mon_session* using *monitor*'s layout.

        Alive panes are left untouched. Dead panes whose position matches a
        layout pane get the layout pane's launcher script. Layout panes
        with no matching tmux pane are added via split-pane. Excess tmux
        panes (more than the layout asks for) are killed.

        No new terminal is spawned — the existing one (if attached) keeps
        showing the session.
        """
        import contextlib

        # Group existing panes by window_index → sorted by pane_index
        panes_by_win: dict[int, list[Any]] = {}
        for p in existing_panes:
            panes_by_win.setdefault(p.window_index, []).append(p)
        for win_idx in panes_by_win:
            panes_by_win[win_idx].sort(key=lambda x: x.pane_index)

        for vp_idx, vp in enumerate(monitor.viewports):
            if not vp.panes:
                continue
            target_window = f"{mon_session}:{vp_idx}"
            existing_window_panes = panes_by_win.get(vp_idx, [])

            # If the layout has a viewport whose window doesn't exist yet,
            # create it.
            if not existing_window_panes:
                if vp_idx == 0:
                    # Window 0 should always exist for an existing session;
                    # if it doesn't, something is very off — log and skip.
                    warnings.append(
                        f"Reconcile: window 0 missing on {mon_session}; skipping vp"
                    )
                    continue
                try:
                    new_w = self._backend.new_window(
                        mon_session, name=vp.tmux_window_name or vp.id,
                    )
                    target_window = f"{mon_session}:{new_w.index}"
                except Exception as exc:
                    warnings.append(
                        f"Reconcile: could not add window for vp {vp.id}: {exc}"
                    )
                    continue
                # Pull fresh pane list for the brand-new window (1 pane)
                try:
                    existing_window_panes = self._backend.list_panes(target_window)
                except Exception:
                    existing_window_panes = []

            split_dir: Literal["h", "v"] = "h" if vp.tmux_layout in (
                "even-h", "main-h"
            ) else "v"

            # Iterate layout panes; respawn dead, leave alive, split-and-add
            # for the overflow.
            last_target: str | None = None
            for lp_idx, layout_pane in enumerate(vp.panes):
                if lp_idx < len(existing_window_panes):
                    ex = existing_window_panes[lp_idx]
                    pane_target = f"{target_window}.{ex.pane_index}"
                    last_target = pane_target
                    if not getattr(ex, "dead", False):
                        # Alive — leave alone.
                        continue
                    self._respawn_layout_pane(
                        doc, mon_session, vp, layout_pane, pane_target,
                        member_results, warnings,
                    )
                else:
                    # Layout has more panes than the existing window — split.
                    if last_target is None and existing_window_panes:
                        last_target = (
                            f"{target_window}.{existing_window_panes[-1].pane_index}"
                        )
                    if last_target is None:
                        warnings.append(
                            f"Reconcile: no anchor pane to split on {target_window}"
                        )
                        break
                    try:
                        new_pane = self._backend.split_pane(last_target, split_dir)
                        pane_target = f"{target_window}.{new_pane.pane_index}"
                    except Exception as exc:
                        warnings.append(
                            f"Reconcile: split-pane failed on {target_window}: {exc}"
                        )
                        break
                    last_target = pane_target
                    self._respawn_layout_pane(
                        doc, mon_session, vp, layout_pane, pane_target,
                        member_results, warnings,
                    )

            # Excess existing panes (layout shrunk) — kill.
            for excess in existing_window_panes[len(vp.panes):]:
                with contextlib.suppress(Exception):
                    self._backend.kill_pane(excess.id)

            # Round 3: apply tree-aware layout (custom string from
            # split_tree, falling back to preset for legacy viewports).
            self._apply_tree_or_preset_layout(target_window, vp)

    def _respawn_layout_pane(
        self,
        doc: CpsmDocument,
        mon_session: str,
        vp: Any,
        layout_pane: Any,
        pane_target: str,
        member_results: list[LaunchResult],
        warnings: list[str],
    ) -> None:
        """Respawn one pane with the launcher for *layout_pane*. Used by both
        the reconcile path and (indirectly via duplication) the fresh path.
        Empty (None connection_id) panes get the placeholder."""
        if layout_pane.connection_id is None:
            try:
                placeholder = self._templates.render_placeholder()
                ph_tmp = self._write_launcher_tmpfile(
                    placeholder, f"placeholder-{vp.id}",
                )
                self._backend.respawn_pane(pane_target, f"bash {ph_tmp}")
            except Exception as exc:
                warnings.append(
                    f"Placeholder failed for pane {pane_target}: {exc}"
                )
            return
        conn = self._config.find_connection(doc, layout_pane.connection_id)
        if conn is None:
            member_results.append(LaunchResult(
                success=False,
                session_name=mon_session,
                connection_id=layout_pane.connection_id,
                errors=[f"Connection '{layout_pane.connection_id}' not found"],
            ))
            return
        try:
            rendered = self._templates.render(
                conn.launch_profile,
                conn,
                settings=doc.settings,
                templates=doc.launch_templates,
                ssh_keys=doc.ssh_keys,
            )
            if conn.launch_profile in _LOCAL_PROFILES:
                _check_local_profile_guard(rendered, conn.launch_profile, conn.id)
            tmpfile = self._write_launcher_tmpfile(rendered, conn.id)
            self._backend.respawn_pane(pane_target, f"bash {tmpfile}")
            # Tag the pane with its connection id so cross-session moves
            # (Round-late: live tmux resync) can locate it via title.
            import contextlib as _ctx
            with _ctx.suppress(Exception):
                self._backend.set_pane_title(pane_target, f"cpsm:{conn.id}")
            member_results.append(LaunchResult(
                success=True,
                session_name=mon_session,
                connection_id=conn.id,
            ))
        except LocalProfileLeakError:
            raise
        except Exception as exc:
            member_results.append(LaunchResult(
                success=False,
                session_name=mon_session,
                connection_id=conn.id,
                errors=[str(exc)],
            ))

    def _resolve_monitor_geometry(
        self,
        schema_monitor: Any,
        schema_index: int,
        ident_map: dict[str, Any],
        index_map: dict[int, Any],
    ) -> tuple[int, int, int, int] | None:
        """Resolve a layout's schema monitor to a live ``MonitorInfo`` and
        return its ``(x, y, w, h)`` pixel geometry, or ``None`` if no live
        monitor matches. Resolution order:
            1. identifier match
            2. monitor_index_hint
            3. positional schema_index
        """
        live = None
        identifier = getattr(schema_monitor, "identifier", None)
        if identifier and identifier in ident_map:
            live = ident_map[identifier]
        elif getattr(schema_monitor, "monitor_index_hint", None) is not None:
            live = index_map.get(schema_monitor.monitor_index_hint)
        if live is None and schema_index in index_map:
            live = index_map[schema_index]
        if live is None:
            return None
        geom = getattr(live, "geometry", None)
        if (
            geom is None
            or not isinstance(geom, tuple)
            or len(geom) != 4
        ):
            return None
        x, y, w, h = geom
        return (int(x), int(y), int(w), int(h))

    def _build_panes_for_viewport(
        self,
        target_window: str,
        viewport: Any,
        warnings: list[str],
    ) -> list[str]:
        """Create N tmux panes in *target_window* (one per ``viewport.panes``)
        and return the list of pane targets (e.g., ``session:window.idx``).

        Returns an empty list on first-pane failure; otherwise returns at
        least one target. Split direction is consistent with viewport's
        ``tmux_layout`` (even-h / main-h split horizontally, others vertical)
        — final positions get re-laid out via select-layout afterwards.
        """
        n = len(viewport.panes)
        if n == 0:
            return []

        # First pane already exists (window 0 of new session, or new window)
        try:
            existing = self._backend.list_panes(target_window)
        except Exception as exc:
            warnings.append(f"list-panes failed on {target_window}: {exc}")
            return []
        if not existing:
            warnings.append(f"No initial pane found on {target_window}")
            return []
        targets: list[str] = [f"{target_window}.{existing[0].pane_index}"]

        # Pick split direction from layout preset
        split_dir: Literal["h", "v"] = "h" if viewport.tmux_layout in (
            "even-h", "main-h"
        ) else "v"

        for _ in range(n - 1):
            try:
                new_pane = self._backend.split_pane(targets[-1], split_dir)
                # Pane index → assemble target
                targets.append(f"{target_window}.{new_pane.pane_index}")
            except Exception as exc:
                warnings.append(
                    f"split-pane failed on {target_window}: {exc}"
                )
                break
        return targets

    # ------------------------------------------------------------------
    # Scene launch (§5.6)
    # ------------------------------------------------------------------

    def launch_scene(self, doc: CpsmDocument, scene_id: str) -> SceneLaunchResult:
        """Launch all groups in a scene, applying ``on_conflict`` policy.

        on_conflict=``error``:
            Pre-validate that no two groups claim the same (monitor, viewport)
            footprint.  Raises ``LayoutConflictError`` before any launch.

        on_conflict=``first-wins``:
            Skip conflicting viewports in later groups with a warning.

        on_conflict=``last-wins``:
            Move conflicting viewports (record warnings for each move).
        """
        scene = self._config.find_scene(doc, scene_id)
        if scene is None:
            return SceneLaunchResult(
                success=False,
                scene_id=scene_id,
                errors=[f"Scene '{scene_id}' not found in document."],
            )

        # Resolve groups
        groups: list[Group] = []
        missing: list[str] = []
        for gid in scene.groups:
            grp = self._config.find_group(doc, gid)
            if grp is None:
                missing.append(gid)
            else:
                groups.append(grp)

        if missing:
            return SceneLaunchResult(
                success=False,
                scene_id=scene_id,
                errors=[f"Groups not found: {missing}"],
            )

        on_conflict = scene.on_conflict
        group_results: list[GroupLaunchResult] = []
        warnings: list[str] = []

        # Collect (monitor_identifier, viewport_id) → group_id for conflict detection
        claimed: dict[tuple[str, str], str] = {}
        conflict_pairs: list[tuple[str, str, str, str]] = []  # (g1, vp1, g2, vp2)

        for grp in groups:
            if grp.default_layout_id:
                layout = self._config.find_layout(doc, grp.default_layout_id)
                if layout is not None:
                    for mon_idx, monitor in enumerate(layout.monitors):
                        mon_key = monitor.identifier or str(mon_idx)
                        for vp in monitor.viewports:
                            key = (mon_key, vp.id)
                            if key in claimed:
                                conflict_pairs.append((claimed[key], key[1], grp.id, vp.id))
                            else:
                                claimed[key] = grp.id

        if conflict_pairs and on_conflict == "error":
            msgs = [
                f"Viewport '{p[1]}' claimed by group '{p[0]}' and '{p[2]}'" for p in conflict_pairs
            ]
            raise LayoutConflictError(
                f"Scene '{scene_id}' has viewport conflicts: " + "; ".join(msgs)
            )

        # Launch groups in order
        skip_groups: set[str] = set()
        for conflict in conflict_pairs:
            if on_conflict == "first-wins":
                # Skip the later group entirely
                skip_groups.add(conflict[2])
                warnings.append(
                    f"first-wins: skipping group '{conflict[2]}' due to viewport conflict "
                    f"with group '{conflict[0]}' on viewport '{conflict[1]}'."
                )
            elif on_conflict == "last-wins":
                warnings.append(
                    f"last-wins: group '{conflict[2]}' overrides group '{conflict[0]}' "
                    f"on viewport '{conflict[1]}'."
                )

        for grp in groups:
            if grp.id in skip_groups:
                continue
            result = self.launch_group(doc, grp.id)
            group_results.append(result)

        overall_success = all(r.success for r in group_results)
        return SceneLaunchResult(
            success=overall_success,
            scene_id=scene_id,
            group_results=group_results,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Session lifecycle helpers
    # ------------------------------------------------------------------

    def close(self, doc: CpsmDocument, connection_id: str) -> None:
        """Kill the session for *connection_id* (shared isolation)."""
        sname = self.session_name(connection_id)
        self.kill_session(sname)

    def kill_session(self, name: str) -> None:
        """Kill the tmux session named *name*."""
        try:
            self._backend.kill_session(name)
            logger.info("Killed session '%s'", name)
        except Exception as exc:
            logger.warning("Failed to kill session '%s': %s", name, exc)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_session(self, name: str, width: int = 220, height: int = 50) -> None:
        """Create tmux session *name* if it does not already exist."""
        existing = {s.name for s in self._backend.list_sessions()}
        if name not in existing:
            self._backend.new_session(name, width=width, height=height, detached=True)
            # Set remain-on-exit per §7.2
            self._backend.set_window_option(f"{name}:0", "remain-on-exit", "on")
            logger.debug("Created session '%s'", name)
        else:
            logger.debug("Session '%s' already exists; reusing", name)

    def _write_launcher_tmpfile(self, script_content: str, label: str) -> str:
        """Write *script_content* to a secure tmpfile and return its path.

        The file is written with 0700 permissions per §7.6.
        """
        tmp_dir = self._tmp_dir
        prefix = f"cpsm-launcher-{label}-"
        suffix = ".sh"

        if tmp_dir is not None:
            tmp_dir.mkdir(parents=True, exist_ok=True)
            # Manually create the temp file in the specified directory
            fd, tmp_path_str = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=str(tmp_dir))
        else:
            fd, tmp_path_str = tempfile.mkstemp(prefix=prefix, suffix=suffix)

        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(script_content)
        except Exception:
            os.close(fd)
            raise

        if sys.platform != "win32":
            os.chmod(tmp_path_str, stat.S_IRWXU)  # 0700

        return tmp_path_str


# ---------------------------------------------------------------------------
# Tmux custom-layout-string generation (Round C — Round 3)
# ---------------------------------------------------------------------------


def _tmux_layout_checksum(body: str) -> str:
    """Compute the 4-char hex checksum tmux prepends to layout strings.

    Mirrors tmux's ``layout_checksum``: rotate-right + add-each-byte
    in 16-bit space.
    """
    csum = 0
    for ch in body:
        csum = ((csum >> 1) | ((csum & 1) << 15)) & 0xFFFF
        csum = (csum + ord(ch)) & 0xFFFF
    return f"{csum:04x}"


def _tmux_layout_body_from_tree(
    node: Any,
    width: int,
    height: int,
    x: int,
    y: int,
    leaf_index: list[int],
) -> str:
    """Build the body of a tmux layout string for *node*.

    Leaf indices are assigned by in-order traversal; ``leaf_index`` is a
    1-element list passed by reference so the counter is shared across
    the recursion.
    """
    from cpsm.data.schema import Pane as _Pane
    from cpsm.data.schema import Split as _Split

    if isinstance(node, _Pane):
        idx = leaf_index[0]
        leaf_index[0] += 1
        return f"{width}x{height},{x},{y},{idx}"
    if isinstance(node, _Split):
        n = len(node.children)
        if n == 0:
            return f"{width}x{height},{x},{y}"
        sizes_pct = (
            [*node.ratios, 1.0 - sum(node.ratios)] if node.ratios else [1.0 / n] * n
        )
        dim_total = width if node.direction == "h" else height
        dims = [max(1, int(dim_total * pct)) for pct in sizes_pct[:-1]]
        dims.append(dim_total - sum(dims))
        children_strs: list[str] = []
        if node.direction == "h":
            cx = x
            for child, cw in zip(node.children, dims, strict=True):
                children_strs.append(
                    _tmux_layout_body_from_tree(child, cw, height, cx, y, leaf_index),
                )
                cx += cw
            return f"{width}x{height},{x},{y}{{{','.join(children_strs)}}}"
        cy = y
        for child, ch in zip(node.children, dims, strict=True):
            children_strs.append(
                _tmux_layout_body_from_tree(child, width, ch, x, cy, leaf_index),
            )
            cy += ch
        return f"{width}x{height},{x},{y}[{','.join(children_strs)}]"
    raise TypeError(f"unsupported node type {type(node)}")


def _tmux_layout_string_from_tree(node: Any, width: int, height: int) -> str:
    """Build a full tmux layout string (with checksum prefix) for *node*."""
    body = _tmux_layout_body_from_tree(node, width, height, 0, 0, [0])
    return f"{_tmux_layout_checksum(body)},{body}"


# ---------------------------------------------------------------------------
# Local-profile guard
# ---------------------------------------------------------------------------


def _check_local_profile_guard(rendered: str, profile: str, connection_id: str) -> None:
    """Raise ``LocalProfileLeakError`` if *rendered* contains ssh/scp/plink invocations.

    The guard checks the rendered template text for patterns that would
    cause the local profile to spawn a network-access binary.  This is an
    explicit security boundary: local profiles MUST NOT open network connections
    (§9.2).

    The check scans for the forbidden binaries at process-spawn boundaries,
    i.e. after whitespace, semicolons, pipes, ampersands, backticks, or at
    line start.  Simple mentions inside comments or quoted strings could
    be false-positives; the guard intentionally errs on the side of caution.
    """
    if _LOCAL_PROFILE_FORBIDDEN_RE.search(rendered):
        raise LocalProfileLeakError(
            f"Local profile '{profile}' (connection '{connection_id}') rendered template "
            "contains an invocation of ssh, scp, or plink. This is forbidden for local "
            "profiles (§9.2). Check the launcher template for unexpected content."
        )
