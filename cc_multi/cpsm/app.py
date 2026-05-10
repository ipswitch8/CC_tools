# -*- coding: utf-8 -*-
"""
cpsm.app — QApplication setup and GUI entry point.

Spec sections: §1.3, §3.1, §4.2

This module is the boundary between the headless service layer and the Qt
event loop.  It must NOT be imported at the top level of any headless module
so that PySide6 is never imported in a headless context.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

__all__ = ["run_gui"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Service factory
# ---------------------------------------------------------------------------


def _make_services() -> SimpleNamespace:
    """Build the headless service stack (no Qt required)."""
    import cpsm.platform.tmux_backend as _tmux_mod
    from cpsm.data.repository import CpsmRepository
    from cpsm.services.config_service import ConfigService
    from cpsm.services.layout_service import LayoutService
    from cpsm.services.session_service import SessionService
    from cpsm.services.template_service import TemplateService

    repository = CpsmRepository()
    config = ConfigService(repository)
    backend = _tmux_mod.TmuxBackend()
    templates = TemplateService()
    layout = LayoutService(backend)
    session = SessionService(config, backend, templates, layout)
    from cpsm.services.key_service import KeyService
    from cpsm.workers.status_poller import StatusPoller

    key_service = KeyService()
    status_poller = StatusPoller(backend, interval_ms=3000)
    return SimpleNamespace(
        config=config,
        session=session,
        layout=layout,
        templates=templates,
        repository=repository,
        key_service=key_service,
        monitor_service=None,  # populated by run_gui after QApplication exists
        config_path=None,  # populated by run_gui after config is resolved
        status_poller=status_poller,
    )


def _attach_monitor_service(services: SimpleNamespace) -> None:
    """Wire a MonitorService to the services namespace.

    Must be called *after* a QApplication exists (MonitorService binds to Qt's
    screen list).  Headless callers (CLI) skip this step.
    """
    try:
        from PySide6.QtGui import QGuiApplication

        from cpsm.services.monitor_service import MonitorService
    except ImportError:
        return
    app = QGuiApplication.instance()
    if app is None or not isinstance(app, QGuiApplication):
        return
    try:
        services.monitor_service = MonitorService(app)
    except Exception:
        logger.exception("Failed to attach MonitorService")


# ---------------------------------------------------------------------------
# First-run welcome dispatch
# ---------------------------------------------------------------------------


def _resolve_initial_config(services: SimpleNamespace, explicit_config: Path | None) -> Path:
    """Return the path that should be opened on startup.

    If ``--config`` was supplied, that always wins.  Otherwise we run the §2.1
    fallback ladder.  This *does not* require the file to exist — the caller
    decides what to do when it doesn't.
    """
    from cpsm.data.repository import resolve_config_path

    return resolve_config_path(explicit_config)


def _run_first_run_flow(services: SimpleNamespace, explicit_config: Path | None) -> Path | None:
    """Show the welcome dialog and act on the user's choice.

    Returns
    -------
    Path | None
        Resolved `.cpsm.yaml` path to open in the main window, or ``None`` if
        the user cancelled (caller should exit without showing the main window).
    """
    from PySide6.QtWidgets import QMessageBox

    from cpsm.services.import_service import ImportService
    from cpsm.ui.dialogs.welcome import WelcomeDialog

    target = _resolve_initial_config(services, explicit_config)

    dialog = WelcomeDialog()
    dialog.exec()

    choice = dialog.choice
    source = dialog.source_path

    if choice is WelcomeDialog.Choice.CANCEL:
        return None

    if choice is WelcomeDialog.Choice.IMPORT:
        if source is None:
            return None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            ImportService().import_legacy_to(source, target)
        except Exception as exc:  # pragma: no cover - error dialog path
            logger.exception("Import failed")
            QMessageBox.critical(
                None,
                "Import failed",
                f"Could not import {source}:\n\n{exc}",
            )
            return None
        return target

    if choice is WelcomeDialog.Choice.EMPTY:
        from cpsm.data.schema import CpsmDocument, Settings

        doc = CpsmDocument(
            schema_version=1,
            settings=Settings(),
            ssh_keys=[],
            connections=[],
            groups=[],
            screen_layouts=[],
            scenes=[],
            launch_templates=[],
        )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            services.repository.save(doc, target)
        except Exception as exc:  # pragma: no cover
            logger.exception("Failed to write empty config")
            QMessageBox.critical(
                None,
                "Couldn't create config",
                f"Could not write {target}:\n\n{exc}",
            )
            return None
        return target

    if choice is WelcomeDialog.Choice.OPEN:
        return source

    return None


# ---------------------------------------------------------------------------
# GUI entry point
# ---------------------------------------------------------------------------


def run_gui(argv: list[str] | None = None, *, config_path: Path | None = None) -> int:
    """Create the QApplication, build the main window, and start the event loop.

    Parameters
    ----------
    argv:
        Argument vector to pass to QApplication.  Defaults to ``sys.argv``.
    config_path:
        Optional explicit `.cpsm.yaml` path supplied via ``--config``.  When
        ``None``, the §2.1 fallback ladder is used; if the resolved path
        doesn't exist, the welcome dialog runs.
    """
    from PySide6.QtWidgets import QApplication

    from cpsm.ui.main_window import MainWindow

    effective_argv: list[str] = sys.argv if argv is None else argv

    app: QApplication = QApplication.instance() or QApplication(effective_argv)  # type: ignore[assignment]
    app.setApplicationName("CPSM")
    app.setApplicationDisplayName("CPSM — Cross-Platform Session Manager")
    app.setOrganizationName("CPSM")
    app.setOrganizationDomain("cpsm.local")

    services = _make_services()
    _attach_monitor_service(services)  # GUI-only — needs QApplication

    # Fix #2 — start the status poller after QApplication exists
    status_poller = getattr(services, "status_poller", None)
    if status_poller is not None:
        status_poller.start()

        def _stop_poller() -> None:
            """Stop the poller and wait for the thread to exit cleanly."""
            status_poller.stop()
            # Give the thread up to 2 s to finish its current poll cycle.
            status_poller.quit()
            if not status_poller.wait(2000):
                # Thread still running after 2 s — forcefully terminate so Qt
                # doesn't abort with "QThread: Destroyed while thread is still running".
                status_poller.terminate()
                status_poller.wait(500)

        app.aboutToQuit.connect(_stop_poller)

    target = _resolve_initial_config(services, config_path)
    document = None

    if target.exists():
        # Existing config — just load and go
        try:
            document = services.config.load(target)
            services.config_path = target  # Fix #8
        except Exception:
            logger.exception("Failed to load %s; falling through to welcome", target)
            target = None  # type: ignore[assignment]

    if document is None:
        # First-run / unreadable / cancelled path: welcome dialog
        chosen = _run_first_run_flow(services, config_path)
        if chosen is None:
            return 0  # User cancelled — don't open the main window
        services.config_path = chosen  # Fix #8
        try:
            document = services.config.load(chosen)
        except Exception as exc:
            logger.exception("Failed to load %s after welcome flow", chosen)
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.critical(
                None,
                "Could not load config",
                f"{chosen}\n\n{exc}",
            )
            return 1

    window = MainWindow(services=services, document=document)
    window.show()

    return app.exec()
