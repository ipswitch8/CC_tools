# -*- coding: utf-8 -*-
"""
CorrelationWorker — runs CorrelationService.correlate() off the UI thread.

The probe spawns one ssh subprocess per ambiguous host with a 10 s timeout
each, so on a slow network this can take several seconds.  We keep that off
the GUI loop entirely; results come back via a Qt signal that MainWindow
maps onto the cached DiscoveredSession list.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QThread, Signal

from cpsm.services.correlation_service import CorrelationResult, CorrelationService

__all__ = ["CorrelationWorker"]

logger = logging.getLogger(__name__)


class CorrelationWorker(QThread):
    """One-shot worker that probes ambiguous hosts and reports results.

    Signals:
        finished_with_result(CorrelationResult): emitted after the probe
            completes, even if no mappings were found.  Empty result means
            "leave the discovered sessions as-is".
    """

    finished_with_result: Signal = Signal(object)

    def __init__(
        self,
        service: CorrelationService,
        doc: Any,
        sessions: list[Any],
        parent: object = None,
    ) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self._service = service
        self._doc = doc
        self._sessions = sessions

    def run(self) -> None:
        try:
            result = self._service.correlate(self._doc, self._sessions)
        except Exception:
            logger.exception("CorrelationService.correlate raised")
            result = CorrelationResult(by_pid={})
        self.finished_with_result.emit(result)
