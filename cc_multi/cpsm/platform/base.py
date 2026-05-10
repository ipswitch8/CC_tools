# -*- coding: utf-8 -*-
"""
MultiplexerBackend ABC, Session/Window/Pane dataclasses, and BackendCapabilities.

Spec: §7.1, §7.5
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

# ---------------------------------------------------------------------------
# Data transfer objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Session:
    """Represents a multiplexer session (e.g. tmux session)."""

    id: str  # tmux session_id (e.g. "$0")
    name: str
    attached: bool
    created_at: datetime


@dataclass(frozen=True)
class Window:
    """Represents a multiplexer window inside a session."""

    id: str
    session: str
    index: int
    name: str
    layout: str


@dataclass(frozen=True)
class Pane:
    """Represents a multiplexer pane inside a window."""

    id: str  # tmux pane_id (e.g. "%0")
    session: str
    window_index: int
    pane_index: int
    pid: int | None
    dead: bool
    current_command: str
    width: int
    height: int
    dead_status: int | None = (
        None  # exit status of dead pane (#{pane_dead_status}); None if unknown
    )
    title: str = ""  # pane_title — set via ``select-pane -T``. CPSM tags
    # panes with ``cpsm:<connection_id>`` for user-visible labeling, but
    # running processes (claude, ssh) overwrite this via terminal escape
    # sequences, so it is NOT reliable for ownership identification.
    start_command: str = ""  # pane_start_command — argv used to launch
    # the pane (e.g. ``bash /tmp/cpsm-launcher-<conn_id>-<rand>.sh``).
    # Stable across the pane's lifetime; cannot be overwritten by the
    # running process. CPSM uses this to identify owned panes.


@dataclass(frozen=True)
class BackendCapabilities:
    """Probed capabilities of a multiplexer backend (§7.5)."""

    name: str  # "tmux", "itmux", "psmux"
    version: str
    supports_split_before: bool  # tmux split-window -b flag
    supports_remain_on_exit: bool
    supports_capture_pane: bool
    supports_format_pane_dead: bool  # `#{pane_dead}` format token
    supports_initial_size_in_new_session: bool = True  # new-session -x/-y flags


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------


class MultiplexerBackend(ABC):
    """Abstract multiplexer backend.

    Concrete implementations (TmuxBackend, ItmuxBackend, PsmuxBackend) must
    implement every abstract method.  The *capabilities* attribute is set by
    the concrete class after probing the binary at construction time.
    """

    capabilities: BackendCapabilities

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    @abstractmethod
    def list_sessions(self) -> list[Session]:
        """Return all active sessions."""
        ...

    @abstractmethod
    def new_session(
        self,
        name: str,
        width: int,
        height: int,
        detached: bool = True,
    ) -> Session:
        """Create and return a new session."""
        ...

    @abstractmethod
    def attach_session(self, name: str) -> None:
        """Attach the current terminal to *name*."""
        ...

    @abstractmethod
    def kill_session(self, name: str) -> None:
        """Destroy the named session and all its windows/panes."""
        ...

    # ------------------------------------------------------------------
    # Window management
    # ------------------------------------------------------------------

    @abstractmethod
    def list_windows(self, session: str) -> list[Window]:
        """Return all windows in *session*."""
        ...

    @abstractmethod
    def new_window(self, session: str, name: str | None = None) -> Window:
        """Create a new window in *session* and return it."""
        ...

    @abstractmethod
    def kill_window(self, target: str) -> None:
        """Kill the window identified by *target* (e.g. ``session:index``)."""
        ...

    @abstractmethod
    def select_layout(self, window: str, layout: str) -> None:
        """Apply a named layout (tiled, even-h, …, custom) to *window*."""
        ...

    @abstractmethod
    def capture_layout(self, window: str) -> str:
        """Return the current layout string for *window* (``#{window_layout}``)."""
        ...

    @abstractmethod
    def set_window_option(self, target: str, name: str, value: str) -> None:
        """Set a window option on *target* (e.g. ``remain-on-exit on``)."""
        ...

    # ------------------------------------------------------------------
    # Pane management
    # ------------------------------------------------------------------

    @abstractmethod
    def list_panes(self, target: str | None = None) -> list[Pane]:
        """Return panes.  If *target* is None, return panes across all sessions."""
        ...

    @abstractmethod
    def split_pane(
        self,
        target: str,
        direction: Literal["h", "v"],
        before: bool = False,
    ) -> Pane:
        """Split *target* pane.

        *direction* ``"h"`` → horizontal (side-by-side), ``"v"`` → vertical
        (stacked).  *before* maps to the ``-b`` flag in tmux.
        """
        ...

    @abstractmethod
    def select_pane(self, target: str) -> None:
        """Make *target* the active pane."""
        ...

    def set_pane_title(self, target: str, title: str) -> None:  # noqa: B027
        """Set the displayed title of *target* pane.

        Default no-op so non-tmux backends don't have to implement it.
        Tmux uses pane titles to tag CPSM-owned panes for cross-session
        move identification.
        """

    def join_pane(
        self,
        src: str,
        target: str,
        direction: Literal["h", "v"],
        before: bool = False,
    ) -> Pane:
        """Move *src* pane into *target*'s window as a split. Default
        implementation raises NotImplementedError; tmux backend
        overrides for live process-preserving cross-session moves.
        """
        raise NotImplementedError("join_pane not supported by this backend")

    @abstractmethod
    def swap_panes(self, src: str, dst: str) -> None:
        """Swap the positions of *src* and *dst* panes."""
        ...

    @abstractmethod
    def break_pane(self, src: str, detached: bool = True) -> Window:
        """Break *src* pane into its own window.  Returns the new window."""
        ...

    @abstractmethod
    def move_pane(self, src: str, dst_window: str) -> None:
        """Move *src* pane into *dst_window*."""
        ...

    @abstractmethod
    def resize_pane(self, target: str, width: int, height: int) -> None:
        """Resize *target* pane to *width* x *height* cells."""
        ...

    @abstractmethod
    def send_keys(self, target: str, keys: str, enter: bool = True) -> None:
        """Send *keys* to *target* pane.  Append ``Enter`` when *enter* is True."""
        ...

    @abstractmethod
    def kill_pane(self, target: str) -> None:
        """Kill *target* pane."""
        ...

    @abstractmethod
    def respawn_pane(
        self,
        target: str,
        command: str,
        kill_existing: bool = True,
    ) -> None:
        """Respawn *target* pane running *command*.

        When *kill_existing* is True pass ``-k`` to respawn-pane.
        """
        ...

    @abstractmethod
    def capture_pane(self, target: str, lines: int = 200) -> str:
        """Return the last *lines* lines of output from *target* pane."""
        ...
