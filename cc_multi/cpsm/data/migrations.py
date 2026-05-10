# -*- coding: utf-8 -*-
"""
Schema migration framework for .cpsm.yaml.

Phase 2 ships schema_version 1; there is nothing to migrate yet.
Future phases register Migration subclasses in MIGRATIONS.

Spec section: §2.2 (schema_version field)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Migration(ABC):
    """Base class for schema migrations.

    Each concrete subclass handles upgrading a document from *from_version*
    to *from_version + 1*.

    Attributes:
        from_version: The schema version this migration upgrades **from**.
    """

    from_version: int

    @abstractmethod
    def apply(self, doc: dict[str, Any]) -> None:
        """Mutate *doc* in-place to upgrade it from *from_version* to *from_version + 1*.

        Args:
            doc: A plain-Python dict representation of the .cpsm.yaml document.
                 The caller is responsible for incrementing ``doc['schema_version']``
                 after a successful apply().
        """


# Registry of migrations sorted by from_version.
# Phase 2 is version 1 — no migrations needed yet.
MIGRATIONS: list[Migration] = []


def run_migrations(doc: dict[str, Any], target_version: int = 1) -> dict[str, Any]:
    """Apply all pending migrations to *doc* up to *target_version*.

    Args:
        doc:            Plain-Python dict of the loaded YAML document.
        target_version: The schema version to migrate to (default: 1).

    Returns:
        The mutated *doc* (same object, modified in-place, returned for
        convenience).
    """
    current = int(doc.get("schema_version", 1))
    sorted_migrations = sorted(MIGRATIONS, key=lambda m: m.from_version)

    for migration in sorted_migrations:
        if migration.from_version >= current and migration.from_version < target_version:
            migration.apply(doc)
            current = migration.from_version + 1
            doc["schema_version"] = current

    return doc


__all__ = ["MIGRATIONS", "Migration", "run_migrations"]
