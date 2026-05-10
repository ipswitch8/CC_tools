# -*- coding: utf-8 -*-
"""
MonitorService — wraps QGuiApplication.screens() and exposes MonitorInfo.

Spec: §6.1
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QGuiApplication, QScreen

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Data transfer object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MonitorInfo:
    """Snapshot of a single monitor's properties.

    The *identifier* is a persistent string derived from manufacturer, model,
    and serial number (EDID data via Qt 6 API) so it survives monitor
    hot-plug cycles and survives application restarts.  When EDID data is
    unavailable the index-based fallback is used.
    """

    identifier: str
    """Persistent identifier: ``"<manufacturer>-<model>-<serial>"`` or ``"index-<n>"``.

    Empty components are omitted; the whole string is stripped of leading/
    trailing hyphens (e.g. if only model is available → ``"model"``).
    """

    name: str
    """``QScreen.name()`` — OS-assigned name (e.g. ``"eDP-1"``, ``"HDMI-A-1"``)."""

    geometry: tuple[int, int, int, int]
    """``(x, y, width, height)`` in device-independent pixels."""

    available_geometry: tuple[int, int, int, int]
    """Available geometry (excludes task-bars / docks)."""

    physical_size_mm: tuple[float, float]
    """Physical dimensions ``(width_mm, height_mm)``."""

    device_pixel_ratio: float
    """HiDPI scaling ratio (e.g. 2.0 on Retina displays)."""

    orientation: str
    """One of ``"landscape"``, ``"portrait"``, ``"landscape-flipped"``,
    ``"portrait-flipped"``, ``"unknown"``."""

    manufacturer: str
    """Monitor manufacturer string from EDID (empty string if unavailable)."""

    model: str
    """Monitor model string from EDID (empty string if unavailable)."""

    serial: str
    """Monitor serial number from EDID (empty string if unavailable)."""

    qt_index: int
    """0-based position of this screen in ``QGuiApplication.screens()``."""


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

_ORIENTATION_MAP = {
    0: "unknown",
    1: "portrait",
    2: "landscape",
    4: "portrait-flipped",
    8: "landscape-flipped",
}


def _screen_to_info(screen: QScreen, index: int) -> MonitorInfo:
    """Convert a ``QScreen`` to a ``MonitorInfo`` dataclass."""
    geo = screen.geometry()
    avail = screen.availableGeometry()
    phys = screen.physicalSize()

    # Qt 6 EDID fields (may return empty strings on older drivers or VMs)
    manufacturer = screen.manufacturer() if hasattr(screen, "manufacturer") else ""
    model = screen.model() if hasattr(screen, "model") else ""
    serial = screen.serialNumber() if hasattr(screen, "serialNumber") else ""

    # Build persistent identifier
    parts = [p for p in (manufacturer, model, serial) if p]
    identifier = "-".join(parts) if parts else f"index-{index}"

    # Orientation value.
    # QScreen.orientation() returns Qt.ScreenOrientation (a PySide6 Flag/IntEnum).
    # .value gives the underlying int for the real Qt type; tests return plain ints.
    orient_raw = screen.orientation()
    if isinstance(orient_raw, int):
        orient_val: int = orient_raw
    else:
        # Real Qt ScreenOrientation: .value is an int attribute
        orient_val_attr = getattr(orient_raw, "value", None)
        orient_val = orient_val_attr if isinstance(orient_val_attr, int) else 0
    orientation = _ORIENTATION_MAP.get(orient_val, "unknown")

    return MonitorInfo(
        identifier=identifier,
        name=screen.name(),
        geometry=(geo.x(), geo.y(), geo.width(), geo.height()),
        available_geometry=(avail.x(), avail.y(), avail.width(), avail.height()),
        physical_size_mm=(phys.width(), phys.height()),
        device_pixel_ratio=screen.devicePixelRatio(),
        orientation=orientation,
        manufacturer=manufacturer,
        model=model,
        serial=serial,
        qt_index=index,
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class MonitorService(QObject):
    """Service that tracks connected monitors via Qt screen signals.

    Emit ``monitor_added`` when a new screen is detected and
    ``monitor_removed`` when an existing screen is disconnected.  Use
    :meth:`snapshot` to get the current list of monitors at any time.

    Usage::

        app = QGuiApplication(sys.argv)
        svc = MonitorService(app)
        svc.monitor_added.connect(lambda info: print("Added:", info.identifier))
        svc.monitor_removed.connect(lambda ident: print("Removed:", ident))
        print(svc.snapshot())
    """

    monitor_added = Signal(MonitorInfo)
    """Emitted with the :class:`MonitorInfo` for the newly connected monitor."""

    monitor_removed = Signal(str)
    """Emitted with the *identifier* string of the disconnected monitor."""

    def __init__(self, app: QGuiApplication) -> None:
        super().__init__()
        self._app = app
        # Track known screens by QScreen pointer so we can match on removal
        self._known: dict[int, MonitorInfo] = {}

        # Populate initial state
        for idx, screen in enumerate(app.screens()):
            info = _screen_to_info(screen, idx)
            self._known[id(screen)] = info
            logger.debug("MonitorService: initial screen %s (%s)", idx, info.identifier)

        # Connect Qt signals
        app.screenAdded.connect(self._on_screen_added)
        app.screenRemoved.connect(self._on_screen_removed)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def snapshot(self) -> list[MonitorInfo]:
        """Return the current list of connected monitors.

        The list is rebuilt from ``QGuiApplication.screens()`` on each call so
        it always reflects the live state, even if signals were missed.
        """
        screens = self._app.screens()
        result: list[MonitorInfo] = []
        for idx, screen in enumerate(screens):
            info = _screen_to_info(screen, idx)
            result.append(info)
        return result

    # ------------------------------------------------------------------
    # Private signal handlers
    # ------------------------------------------------------------------

    def _on_screen_added(self, screen: QScreen) -> None:
        screens = self._app.screens()
        try:
            idx = screens.index(screen)
        except ValueError:
            idx = len(screens)
        info = _screen_to_info(screen, idx)
        self._known[id(screen)] = info
        logger.info("MonitorService: screen added %s (%s)", idx, info.identifier)
        self.monitor_added.emit(info)

    def _on_screen_removed(self, screen: QScreen) -> None:
        screen_id = id(screen)
        info = self._known.pop(screen_id, None)
        if info is not None:
            identifier = info.identifier
        else:
            # Fallback: try to derive identifier from what Qt still knows
            identifier = screen.name() or f"unknown-{screen_id}"
        logger.info("MonitorService: screen removed %s", identifier)
        self.monitor_removed.emit(identifier)
