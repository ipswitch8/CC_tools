# -*- coding: utf-8 -*-
"""
Legacy `.claude-projects.yaml` → `.cpsm.yaml` converter.

Spec sections: §4.2, §4.3

This module is pure conversion logic.  It accepts a parsed legacy dict
(from ``ruamel.yaml`` or ``yaml.safe_load``) and returns a ``(CpsmDocument,
ImportPreview)`` pair.  It **never** opens any file for write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from cpsm.data.schema import (
    ClaudeRemoteConnection,
    CpsmDocument,
    Group,
    Settings,
    SshKey,
)

# ---------------------------------------------------------------------------
# Public data structures (transient UI artefacts — plain dataclasses)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportTransform:
    """A single recorded transformation from the import run.

    ``kind`` values:
    - ``"added"``       — new connection or group synthesised from legacy entry.
    - ``"renamed"``     — connection id suffixed due to slug collision.
    - ``"skipped"``     — legacy entry could not be mapped (unknown/invalid field).
    - ``"warning"``     — legacy entry had a field the new schema cannot represent.
    - ``"synthesized"`` — placeholder resource created (e.g. the default SSH key).
    """

    kind: Literal["added", "renamed", "skipped", "warning", "synthesized"]
    target_path: str  # e.g. "connections[web01]" or "settings.default_claude_options"
    detail: str


@dataclass(frozen=True)
class ImportPreview:
    """Result of an import run.

    ``document`` is the fully-validated ``CpsmDocument`` ready to be saved.
    ``transforms`` is the ordered list of every change made during conversion.
    ``source_path`` is the path of the legacy file that was read.
    """

    source_path: Path
    transforms: list[ImportTransform]
    document: CpsmDocument


# ---------------------------------------------------------------------------
# Placeholder SSH key constants
# ---------------------------------------------------------------------------

_PLACEHOLDER_KEY_ID = "imported-default"
_PLACEHOLDER_KEY_NAME = "Imported default key"
_PLACEHOLDER_KEY_PRIVATE = "~/.ssh/id_ed25519"
_PLACEHOLDER_KEY_PUBLIC = "~/.ssh/id_ed25519.pub"

# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_NON_SLUG_CHAR_RE = re.compile(r"[^a-z0-9-]+")


def _slugify(name: str) -> str:
    """Convert *name* to a valid CPSM id slug.

    Rules (§2.5 + spec notes in the importer task):
    - Lowercase.
    - Replace runs of non-``[a-z0-9-]`` characters with ``-``.
    - Strip leading/trailing dashes.
    - Must match ``^[a-z0-9][a-z0-9-]{1,62}$``.

    Raises ``ValueError`` if the result is empty after stripping.
    """
    lowered = name.lower()
    slugged = _NON_SLUG_CHAR_RE.sub("-", lowered)
    stripped = slugged.strip("-")
    if not stripped:
        raise ValueError(
            f"Cannot derive a valid slug from name '{name}': result is empty after stripping."
        )
    # Enforce maximum length (spec allows up to 63 chars: [a-z0-9] + {1,62})
    truncated = stripped[:63]
    # Re-strip in case truncation lands on a dash
    truncated = truncated.strip("-")
    if not truncated:
        raise ValueError(
            f"Cannot derive a valid slug from name '{name}': truncated result is empty."
        )
    if not _SLUG_RE.match(truncated):
        raise ValueError(
            f"Derived slug '{truncated}' from name '{name}' does not match "
            r"^[a-z0-9][a-z0-9-]{1,62}$"
        )
    return truncated


def _unique_slug(base: str, seen: set[str]) -> tuple[str, bool]:
    """Return *(slug, collided)*.

    If *base* is already in *seen* the slug is suffixed with ``-2``, ``-3``, …
    until a free slot is found.  ``collided`` is ``True`` when a suffix was
    applied.
    """
    if base not in seen:
        return base, False
    counter = 2
    while True:
        candidate = f"{base}-{counter}"
        if candidate not in seen:
            return candidate, True
        counter += 1


# ---------------------------------------------------------------------------
# Group slug helper (preserve display name but slug the id)
# ---------------------------------------------------------------------------


def _group_slug(group_name: str) -> str:
    """Derive a stable group id slug from the display name."""
    return _slugify(group_name)


# ---------------------------------------------------------------------------
# Core conversion
# ---------------------------------------------------------------------------


def convert(
    legacy: dict[str, Any],
    source_path: Path,
) -> ImportPreview:
    """Convert a parsed legacy dict to a ``CpsmDocument`` + ``ImportPreview``.

    Parameters
    ----------
    legacy:
        A plain Python dict as returned by ``ruamel.yaml.YAML().load(...)``
        (after stripping ruamel comment wrappers, or from ``yaml.safe_load``).
    source_path:
        The path the legacy YAML was read from.  Stored in ``ImportPreview``
        so callers can surface it in the UI.  This function does **not** open
        or write to this path.

    Returns
    -------
    ImportPreview
        Contains the validated ``CpsmDocument`` and the full transform log.
    """
    transforms: list[ImportTransform] = []

    # ------------------------------------------------------------------
    # 1. Settings from defaults block
    # ------------------------------------------------------------------
    defaults: dict[str, Any] = legacy.get("defaults") or {}
    raw_claude_opts: str | None = defaults.get("claude_options")
    raw_ssh_opts: str | None = defaults.get("ssh_options")

    settings_kwargs: dict[str, Any] = {}
    if raw_claude_opts is not None:
        settings_kwargs["default_claude_options"] = raw_claude_opts
        transforms.append(
            ImportTransform(
                kind="added",
                target_path="settings.default_claude_options",
                detail=f"mapped from defaults.claude_options='{raw_claude_opts}'",
            )
        )
    if raw_ssh_opts is not None:
        settings_kwargs["default_ssh_options"] = raw_ssh_opts
        transforms.append(
            ImportTransform(
                kind="added",
                target_path="settings.default_ssh_options",
                detail=f"mapped from defaults.ssh_options='{raw_ssh_opts}'",
            )
        )

    settings = Settings(**settings_kwargs)

    # ------------------------------------------------------------------
    # 2. Placeholder SSH key (§4.3 SSH key synthesis note)
    # ------------------------------------------------------------------
    placeholder_key = SshKey(
        id=_PLACEHOLDER_KEY_ID,
        name=_PLACEHOLDER_KEY_NAME,
        type="ed25519",
        private_path=_PLACEHOLDER_KEY_PRIVATE,
        public_path=_PLACEHOLDER_KEY_PUBLIC,
        passphrase_ref=None,
    )
    transforms.append(
        ImportTransform(
            kind="synthesized",
            target_path=f"ssh_keys[{_PLACEHOLDER_KEY_ID}]",
            detail=("placeholder key — replace via SSH Key Manager before launching"),
        )
    )

    # ------------------------------------------------------------------
    # 3. Connections and groups
    # ------------------------------------------------------------------
    raw_projects: list[dict[str, Any]] = legacy.get("projects") or []

    connections: list[ClaudeRemoteConnection] = []
    conn_id_set: set[str] = set()

    # group_name → list of connection ids
    group_members: dict[str, list[str]] = {}

    _KNOWN_FIELDS = {
        "name",
        "group",
        "host",
        "ssh_user",
        "sudo_user",
        "project_folder",
        "claude_options",
    }

    for entry in raw_projects:
        if not isinstance(entry, dict):
            transforms.append(
                ImportTransform(
                    kind="skipped",
                    target_path="projects[]",
                    detail=f"entry is not a mapping: {entry!r}",
                )
            )
            continue

        name: str | None = entry.get("name")
        if not name:
            transforms.append(
                ImportTransform(
                    kind="skipped",
                    target_path="projects[]",
                    detail="entry has no 'name' field — skipped",
                )
            )
            continue

        # Warn about unknown/unsupported fields
        unknown_fields = set(entry.keys()) - _KNOWN_FIELDS
        for uf in sorted(unknown_fields):
            transforms.append(
                ImportTransform(
                    kind="warning",
                    target_path=f"projects[name='{name}']",
                    detail=(f"field '{uf}' is not representable in the new schema and was dropped"),
                )
            )

        # Derive id
        try:
            base_slug = _slugify(name)
        except ValueError as exc:
            transforms.append(
                ImportTransform(
                    kind="skipped",
                    target_path=f"projects[name='{name}']",
                    detail=f"cannot derive id slug: {exc}",
                )
            )
            continue

        conn_id, collided = _unique_slug(base_slug, conn_id_set)
        conn_id_set.add(conn_id)

        if collided:
            transforms.append(
                ImportTransform(
                    kind="renamed",
                    target_path=f"connections[{conn_id}]",
                    detail=(f"id derived as '{conn_id}' from name='{name}' (collision suffixed)"),
                )
            )

        # Build connection
        host: str = entry.get("host") or ""
        user: str = entry.get("ssh_user") or ""
        sudo_user: str | None = entry.get("sudo_user") or None
        project_folder: str = entry.get("project_folder") or ""

        # Per-entry claude_options; fall back to defaults
        per_entry_opts: str | None = entry.get("claude_options")
        if per_entry_opts is None:
            per_entry_opts = raw_claude_opts or "--resume"

        conn = ClaudeRemoteConnection(
            id=conn_id,
            name=name,
            launch_profile="claude-remote",
            host=host,
            user=user,
            sudo_user=sudo_user,
            identity_file_ref=_PLACEHOLDER_KEY_ID,
            project_folder=project_folder,
            claude_options=per_entry_opts,
        )
        connections.append(conn)

        transforms.append(
            ImportTransform(
                kind="added",
                target_path=f"connections[{conn_id}]",
                detail=f"imported from projects[].name='{name}'",
            )
        )

        # Group membership
        group_name: str | None = entry.get("group")
        if group_name:
            group_members.setdefault(group_name, []).append(conn_id)

    # ------------------------------------------------------------------
    # 4. Synthesize Group objects
    # ------------------------------------------------------------------
    groups: list[Group] = []
    group_id_set: set[str] = set()

    for group_display_name, member_ids in group_members.items():
        try:
            base_gslug = _group_slug(group_display_name)
        except ValueError as exc:
            transforms.append(
                ImportTransform(
                    kind="skipped",
                    target_path=f"groups[name='{group_display_name}']",
                    detail=f"cannot derive group id slug: {exc}",
                )
            )
            continue

        gid, g_collided = _unique_slug(base_gslug, group_id_set)
        group_id_set.add(gid)

        if g_collided:
            transforms.append(
                ImportTransform(
                    kind="renamed",
                    target_path=f"groups[{gid}]",
                    detail=(
                        f"id derived as '{gid}' from group='{group_display_name}' "
                        f"(collision suffixed)"
                    ),
                )
            )

        groups.append(
            Group(
                id=gid,
                name=group_display_name,
                members=member_ids,
            )
        )

        transforms.append(
            ImportTransform(
                kind="added",
                target_path=f"groups[{gid}]",
                detail=f"synthesized from group='{group_display_name}'",
            )
        )

    # ------------------------------------------------------------------
    # 5. Assemble and validate the document
    # ------------------------------------------------------------------
    document = CpsmDocument(
        schema_version=1,
        settings=settings,
        ssh_keys=[placeholder_key],
        connections=connections,
        groups=groups,
    )

    return ImportPreview(
        source_path=source_path,
        transforms=transforms,
        document=document,
    )


# ---------------------------------------------------------------------------
# Convenience: load a legacy file and convert
# ---------------------------------------------------------------------------


def load_and_convert(source_path: Path) -> ImportPreview:
    """Read the legacy YAML at *source_path* (read-only) and run ``convert``.

    The file is **never** opened for write.

    Parameters
    ----------
    source_path:
        Path to a ``.claude-projects.yaml`` file.

    Returns
    -------
    ImportPreview
    """
    from ruamel.yaml import YAML

    yaml = YAML(typ="safe")
    yaml.preserve_quotes = False  # safe loader, no round-trip needed

    with open(source_path, encoding="utf-8") as fh:
        raw = yaml.load(fh)

    if raw is None:
        raw = {}

    # ruamel safe loader already returns plain dicts/lists, but guard anyway
    plain = _to_plain(raw)
    return convert(plain, source_path)


def _to_plain(obj: Any) -> Any:
    """Recursively convert ruamel CommentedMap/Seq to plain Python types."""
    # Import here so the module doesn't hard-depend on ruamel internals at top level
    try:
        from ruamel.yaml.comments import CommentedMap, CommentedSeq

        if isinstance(obj, CommentedMap):
            return {k: _to_plain(v) for k, v in obj.items()}
        if isinstance(obj, CommentedSeq):
            return [_to_plain(v) for v in obj]
    except ImportError:
        pass
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_plain(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "ImportPreview",
    "ImportTransform",
    "convert",
    "load_and_convert",
]
