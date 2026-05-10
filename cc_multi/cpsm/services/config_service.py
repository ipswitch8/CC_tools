# -*- coding: utf-8 -*-
"""
ConfigService — load, save, validate, and search the CPSM document.

Spec sections: §2.1, §2.5, §4.1, §9.2
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from cpsm.data.repository import CpsmRepository, resolve_config_path
from cpsm.data.schema import (
    Connection,
    CpsmDocument,
    Group,
    Scene,
    ScreenLayout,
    SshKey,
)

__all__ = [
    "ConfigService",
    "ValidationIssue",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ValidationIssue
# ---------------------------------------------------------------------------


@dataclass
class ValidationIssue:
    """A single structured validation problem surfaced by ConfigService.validate()."""

    location: str
    message: str
    severity: Literal["error", "warning"] = "error"


# ---------------------------------------------------------------------------
# ConfigService
# ---------------------------------------------------------------------------


class ConfigService:
    """High-level config service wrapping the CpsmRepository.

    Responsibilities
    ----------------
    - Resolve and delegate load/save to the repository.
    - Re-run pydantic validation and surface structured ValidationIssue records.
    - Provide fast lookup helpers for connections, groups, scenes, layouts, and keys.
    """

    def __init__(self, repository: CpsmRepository) -> None:
        self._repo = repository
        self._path: Path | None = None

    # ------------------------------------------------------------------
    # Load / Save
    # ------------------------------------------------------------------

    def load(self, path: Path | None = None) -> CpsmDocument:
        """Load the document from *path* (or the resolved default path).

        Resolution order follows §2.1 via ``resolve_config_path``.
        """
        resolved = resolve_config_path(path)
        self._path = resolved
        doc = self._repo.load_or_create(resolved)
        logger.info("Loaded config from %s", resolved)
        return doc

    def save(self, doc: CpsmDocument, path: Path | None = None) -> None:
        """Save *doc* to *path* (or the path last loaded from).

        Delegates atomic write + 0600 chmod to the repository.
        """
        target = path or self._path
        self._repo.save(doc, target)
        logger.info("Saved config to %s", target or "<repo default>")

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self, doc: CpsmDocument) -> list[ValidationIssue]:
        """Re-run schema validation on *doc* and return a list of ValidationIssue records.

        Strategy: round-trip through model_dump → model_validate so that
        pydantic re-executes all field + model validators, including FK checks.

        Returns an empty list when the document is fully valid.
        """
        issues: list[ValidationIssue] = []
        try:
            CpsmDocument.model_validate(doc.model_dump(mode="python"))
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(p) for p in err["loc"]) if err["loc"] else "<root>"
                issues.append(
                    ValidationIssue(
                        location=loc,
                        message=err["msg"],
                        severity="error",
                    )
                )
        return issues

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------

    def find_connection(self, doc: CpsmDocument, connection_id: str) -> Connection | None:
        """Return the connection with *connection_id*, or None."""
        for conn in doc.connections:
            if conn.id == connection_id:
                return conn
        return None

    def find_group(self, doc: CpsmDocument, group_id: str) -> Group | None:
        """Return the group with *group_id*, or None."""
        for grp in doc.groups:
            if grp.id == group_id:
                return grp
        return None

    def find_scene(self, doc: CpsmDocument, scene_id: str) -> Scene | None:
        """Return the scene with *scene_id*, or None."""
        for scene in doc.scenes:
            if scene.id == scene_id:
                return scene
        return None

    def find_layout(self, doc: CpsmDocument, layout_id: str) -> ScreenLayout | None:
        """Return the screen_layout with *layout_id*, or None."""
        for layout in doc.screen_layouts:
            if layout.id == layout_id:
                return layout
        return None

    def find_key(self, doc: CpsmDocument, key_id: str) -> SshKey | None:
        """Return the ssh_key with *key_id*, or None."""
        for key in doc.ssh_keys:
            if key.id == key_id:
                return key
        return None
