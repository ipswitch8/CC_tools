# -*- coding: utf-8 -*-
"""
ruamel.yaml round-trip repository for .cpsm.yaml.

Spec sections: §2.1, §4.1, §9.2, §9.3
"""

from __future__ import annotations

import logging
import os
import sys
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from cpsm.data.schema import CpsmDocument

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path resolution (§2.1, §8)
# ---------------------------------------------------------------------------

_CPSM_CONFIG_ENV = "CPSM_CONFIG"
_XDG_CONFIG_HOME_ENV = "XDG_CONFIG_HOME"
_APPDATA_ENV = "APPDATA"
_CONFIG_FILENAME = ".cpsm.yaml"


def resolve_config_path(explicit: Path | None = None) -> Path:
    """Return the resolved path to .cpsm.yaml.

    Resolution order (§2.1):
      1. *explicit* (caller-supplied, e.g. from --config)
      2. $CPSM_CONFIG environment variable
      3. $XDG_CONFIG_HOME/cpsm/.cpsm.yaml  (Linux)
         %APPDATA%\\cpsm\\.cpsm.yaml         (Windows)
      4. ~/.cpsm.yaml
    """
    # 1. Explicit --config argument
    if explicit is not None:
        return explicit.expanduser().resolve()

    # 2. $CPSM_CONFIG environment variable
    env_config = os.environ.get(_CPSM_CONFIG_ENV)
    if env_config:
        return Path(env_config).expanduser().resolve()

    # 3. Platform config directory (only when its env var is set; otherwise fall through)
    if sys.platform == "win32":
        appdata = os.environ.get(_APPDATA_ENV)
        if appdata:
            return Path(appdata) / "cpsm" / _CONFIG_FILENAME
    else:
        xdg_config = os.environ.get(_XDG_CONFIG_HOME_ENV)
        if xdg_config:
            return Path(xdg_config) / "cpsm" / _CONFIG_FILENAME

    # 4. ~/.cpsm.yaml fallback (when neither env var nor explicit override is set)
    return Path.home() / _CONFIG_FILENAME


# ---------------------------------------------------------------------------
# YAML factory
# ---------------------------------------------------------------------------


def _make_yaml() -> YAML:
    """Create a configured ruamel.yaml round-trip parser (§4.1, §9.3)."""
    yaml = YAML(typ="rt")
    yaml.encoding = "utf-8"
    yaml.line_break = "\n"  # type: ignore[assignment]
    yaml.preserve_quotes = True
    return yaml


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class CpsmRepository:
    """Load and save .cpsm.yaml with round-trip comment preservation.

    The repository keeps the raw ``CommentedMap`` tree alongside the typed
    ``CpsmDocument`` so that saves can mutate the existing tree rather than
    regenerating it (preserving comments and key order).
    """

    def __init__(self) -> None:
        self._yaml: YAML = _make_yaml()
        self._raw: CommentedMap | None = None
        self._document: CpsmDocument | None = None
        self._path: Path | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self, path: Path) -> CpsmDocument:
        """Load and validate *path*.

        Returns the parsed ``CpsmDocument``.  The raw ``CommentedMap`` tree
        is cached internally for round-trip saves.

        Raises:
            FileNotFoundError: if *path* does not exist.
            pydantic.ValidationError: if the document fails schema validation.
        """
        path = path.expanduser().resolve()
        with open(path, encoding="utf-8") as fh:
            raw = self._yaml.load(fh)

        if raw is None:
            raw = CommentedMap()

        self._raw = raw
        self._path = path
        self._document = CpsmDocument.model_validate(_commented_map_to_plain(raw))
        return self._document

    def save(self, document: CpsmDocument, path: Path | None = None) -> None:
        """Save *document* to *path* (or the path last loaded from).

        The save strategy is:
          1. Merge model values back into the cached raw tree (preserving
             comments and key order).
          2. Serialize to a temp file alongside the target.
          3. fsync + atomic rename.
          4. Set 0600 permissions on Linux.

        Raises:
            RuntimeError: if no path is known (neither loaded nor supplied).
        """
        target = path or self._path
        if target is None:
            raise RuntimeError(
                "No path supplied and no path was previously loaded. "
                "Pass an explicit path to save()."
            )
        target = target.expanduser().resolve()

        # Ensure parent directory exists
        target.parent.mkdir(parents=True, exist_ok=True)

        # Build/update the raw tree from the model
        if self._raw is None:
            self._raw = CommentedMap()
        _merge_model_into_raw(document, self._raw)

        # Atomic write
        tmp_path = target.with_suffix(target.suffix + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
                self._yaml.dump(self._raw, fh)
                fh.flush()
                os.fsync(fh.fileno())

            os.replace(tmp_path, target)
        except Exception:
            # Clean up temp file on error
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise

        # Set 0600 permissions (Linux only; Windows handled by Phase 7 KeyService)
        if sys.platform != "win32":
            os.chmod(target, 0o600)
        else:
            try:
                import win32security  # type: ignore[import-untyped]

                _set_owner_only_acl_windows(target, win32security)
            except ImportError:
                logger.warning(
                    "pywin32 not available — cannot set owner-only ACL on %s. "
                    "Full ACL handling lands in Phase 7 KeyService.",
                    target,
                )

        self._path = target
        self._document = document

    @property
    def document(self) -> CpsmDocument | None:
        """Return the last loaded/saved document, or None."""
        return self._document

    @property
    def raw(self) -> CommentedMap | None:
        """Return the raw ruamel.yaml tree, or None if nothing loaded."""
        return self._raw

    def load_or_create(self, path: Path) -> CpsmDocument:
        """Load from *path* if it exists, otherwise return a default document."""
        if path.exists():
            return self.load(path)
        doc = CpsmDocument()
        self._path = path.expanduser().resolve()
        self._raw = CommentedMap()
        self._document = doc
        return doc

    def serialize_to_string(self, document: CpsmDocument | None = None) -> str:
        """Serialize *document* (or cached document) to a YAML string."""
        doc = document or self._document
        if doc is None:
            raise RuntimeError("No document to serialize.")
        raw: CommentedMap = CommentedMap()
        _merge_model_into_raw(doc, raw)
        buf = StringIO()
        self._yaml.dump(raw, buf)
        return buf.getvalue()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _commented_map_to_plain(obj: Any) -> Any:
    """Recursively convert ruamel.yaml CommentedMap/CommentedSeq to plain dicts/lists.

    Pydantic needs plain Python types for model_validate.
    """
    # Import here to avoid circular issues; ruamel types are only needed internally
    from ruamel.yaml.comments import CommentedMap as CM
    from ruamel.yaml.comments import CommentedSeq as CS

    if isinstance(obj, CM):
        return {k: _commented_map_to_plain(v) for k, v in obj.items()}
    if isinstance(obj, CS):
        return [_commented_map_to_plain(v) for v in obj]
    return obj


def _merge_model_into_raw(document: CpsmDocument, raw: CommentedMap) -> None:
    """Overwrite scalar values in *raw* from *document*, preserving structure.

    We serialize the model to a plain dict then merge it into the raw tree.
    Keys not present in the model are left untouched (e.g. unexpected fields
    that were loaded but not modelled — rare, but we don't want to silently drop
    user data).  Keys present in the model but not in the tree are added.
    """
    plain = _model_to_plain(document)
    _deep_merge(raw, plain)


def _model_to_plain(obj: Any) -> Any:
    """Recursively convert a pydantic model (or primitive) to plain Python types."""
    if hasattr(obj, "model_dump"):
        # Pydantic BaseModel — use model_dump to get the dict
        return obj.model_dump(mode="python")
    if isinstance(obj, dict):
        return {k: _model_to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_model_to_plain(v) for v in obj]
    return obj


def _deep_merge(raw: CommentedMap, plain: dict[str, Any]) -> None:
    """Merge plain dict values into raw CommentedMap in-place."""
    from ruamel.yaml.comments import CommentedMap as CM
    from ruamel.yaml.comments import CommentedSeq as CS

    for key, value in plain.items():
        if key not in raw:
            # New key: just assign
            raw[key] = _plain_to_commented(value)
        else:
            existing = raw[key]
            if isinstance(existing, CM) and isinstance(value, dict):
                _deep_merge(existing, value)
            elif isinstance(existing, CS) and isinstance(value, list):
                # Replace sequence content while keeping the CommentedSeq object
                existing.clear()
                for item in value:
                    existing.append(_plain_to_commented(item))
            else:
                raw[key] = _plain_to_commented(value)


def _plain_to_commented(value: Any) -> Any:
    """Convert a plain Python value to ruamel.yaml commented types."""
    from ruamel.yaml.comments import CommentedMap as CM
    from ruamel.yaml.comments import CommentedSeq as CS

    if isinstance(value, dict):
        cm = CM()
        for k, v in value.items():
            cm[k] = _plain_to_commented(v)
        return cm
    if isinstance(value, list):
        cs = CS()
        for v in value:
            cs.append(_plain_to_commented(v))
        return cs
    return value


def _set_owner_only_acl_windows(path: Path, win32security: Any) -> None:
    """Set owner-only DACL on *path* via pywin32 (Windows, §9.2)."""
    # Get current process token owner SID
    token = win32security.OpenProcessToken(
        win32security.GetCurrentProcess(),
        win32security.TOKEN_QUERY,
    )
    owner_sid = win32security.GetTokenInformation(token, win32security.TokenOwner)

    # Build a DACL granting full control to owner only
    dacl = win32security.ACL()
    dacl.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        win32security.GENERIC_ALL,
        owner_sid,
    )

    # Apply to file
    sd = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
    sd.SetSecurityDescriptorDacl(True, dacl, False)
    win32security.SetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION, sd)
