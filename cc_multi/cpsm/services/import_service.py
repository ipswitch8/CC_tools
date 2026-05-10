# -*- coding: utf-8 -*-
"""
ImportService — façade for the UI/CLI import flow.

Spec sections: §4.2, §4.3

Provides two public entry points:

* ``import_legacy(source, *, dry_run=True) -> ImportPreview``
  Reads the legacy file, runs the converter, and **never** touches the
  source for write.  When *dry_run* is ``True`` the resulting document is
  returned inside the preview but is not persisted anywhere.

* ``import_legacy_to(source, target) -> ImportPreview``
  Converts and then writes the resulting ``.cpsm.yaml`` via
  ``CpsmRepository.save``, using an atomic rename so the write is safe
  even if *target* already exists.

Neither function ever opens *source* for write.
"""

from __future__ import annotations

from pathlib import Path

from cpsm.data.importer import ImportPreview, load_and_convert
from cpsm.data.repository import CpsmRepository

__all__ = [
    "ImportService",
    "import_legacy",
    "import_legacy_to",
]


class ImportService:
    """Stateless service wrapping the importer for the UI and CLI.

    All methods are thin wrappers around the pure ``cpsm.data.importer``
    functions.  The service exists so higher-level layers can depend on
    an injectable object rather than module-level functions.
    """

    # ------------------------------------------------------------------
    # Module-level convenience wrappers keep this injectable *and* allow
    # direct function-style calls from the CLI.
    # ------------------------------------------------------------------

    @staticmethod
    def import_legacy(source: Path, *, dry_run: bool = True) -> ImportPreview:
        """Convert *source* (a ``.claude-projects.yaml``) and return a preview.

        The source file is **never** opened for write.  When *dry_run* is
        ``True`` (the default) nothing is persisted; the caller receives the
        ``ImportPreview`` and decides whether to call ``import_legacy_to``.

        Parameters
        ----------
        source:
            Path to the legacy ``.claude-projects.yaml`` file.
        dry_run:
            When ``True`` (default) do not write any file.

        Returns
        -------
        ImportPreview
            The preview including the fully-validated ``CpsmDocument``.

        Raises
        ------
        FileNotFoundError
            If *source* does not exist.
        pydantic.ValidationError
            If the converted document fails schema validation (should not
            happen with well-formed legacy data, but surfaced here for safety).
        """
        return import_legacy(source, dry_run=dry_run)

    @staticmethod
    def import_legacy_to(source: Path, target: Path) -> ImportPreview:
        """Convert *source* and save the result to *target*.

        Equivalent to calling ``import_legacy(source, dry_run=False)`` and
        then persisting the document.  The source file is **never** opened
        for write.

        Parameters
        ----------
        source:
            Path to the legacy ``.claude-projects.yaml`` file.
        target:
            Destination path for the ``.cpsm.yaml`` output.

        Returns
        -------
        ImportPreview
            Same preview as ``import_legacy``, now with the document also
            written to *target*.
        """
        return import_legacy_to(source, target)


# ---------------------------------------------------------------------------
# Module-level functions (usable without instantiating ImportService)
# ---------------------------------------------------------------------------


def import_legacy(source: Path, *, dry_run: bool = True) -> ImportPreview:
    """Convert a legacy ``.claude-projects.yaml`` file.

    The *source* file is opened **read-only**.  When *dry_run* is ``True``
    (default) no output file is written; the returned ``ImportPreview``
    contains the document but it is not persisted.

    Parameters
    ----------
    source:
        Path to the legacy file.
    dry_run:
        When ``True`` (default) do not write any file.

    Returns
    -------
    ImportPreview
    """
    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Legacy file not found: {source}")
    preview = load_and_convert(source)
    # dry_run=True: nothing to do — document lives in the preview
    # dry_run=False is just a signal; actual writing needs a target path,
    # so callers that want to persist should use import_legacy_to instead.
    return preview


def import_legacy_to(source: Path, target: Path) -> ImportPreview:
    """Convert *source* and write the result to *target*.

    Uses ``CpsmRepository.save`` (atomic rename, 0600 perms on Linux).
    The *source* file is opened **read-only** — this invariant is tested by
    the test suite.

    Parameters
    ----------
    source:
        Path to the legacy ``.claude-projects.yaml`` file.
    target:
        Destination path for the ``.cpsm.yaml`` output.

    Returns
    -------
    ImportPreview
    """
    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Legacy file not found: {source}")

    preview = load_and_convert(source)

    repo = CpsmRepository()
    repo.save(preview.document, target.expanduser().resolve())

    return preview
