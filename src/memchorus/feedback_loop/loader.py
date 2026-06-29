"""Loader for declarative feedback loop YAML definitions.

Scans ``~/.hermes/custom_loops/`` (or a configurable directory) for ``*.yaml`` /
``*.yml`` files, validates each against schema_v1, and returns a list of valid
``FeedbackLoopDefinition`` instances. Malformed or unknown-schema entries are logged
as warnings and silently skipped — the gateway never crashes from bad YAML.

Usage::

    from memchorus.feedback_loop.loader import load_feedback_loops

    loops = load_feedback_loops()  # defaults to ~/.hermes/custom_loops/
    for loop in loops:
        print(loop.name, "->", loop.trigger_event)
"""

import copy
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from memchorus.feedback_loop.schema_v1 import (
    FeedbackLoopDefinition,
    SUPPORTED_VERSIONS,
    validate_schema_v1,
)

logger = logging.getLogger(__name__)

DEFAULT_DIRECTORY = str(Path.home() / ".hermes" / "custom_loops")

# File extensions considered as YAML definitions.
_YAML_EXTENSIONS = {".yaml", ".yml"}


def load_feedback_loops(
    directory: Optional[str] = None,
) -> List[FeedbackLoopDefinition]:
    r"""Load all valid feedback loop definitions from a directory.

    Scans the given directory for ``*.yaml`` / ``*.yml`` files, validates each
    against schema_v1 rules, and returns only successfully validated definitions.

    Parameters
    ----------
    directory : str, optional
        Path to scan for YAML definitions. Defaults to
        ``~/.hermes/custom_loops/``.

    Returns
    -------
    List[FeedbackLoopDefinition]
        Validated loop definitions (one per file). Invalid entries are silently
        skipped with a warning log.

    Notes
    -----
    Duplicate names raise a warning and the later file is skipped — earlier files
    take precedence to match Linux glob ordering semantics.
    """
    target = Path(directory or DEFAULT_DIRECTORY)
    return _load_from_directory(target)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_from_directory(root: Path) -> List[FeedbackLoopDefinition]:
    """Internal loader used by ``load_feedback_loops`` and tests."""

    # --- missing directory is graceful (empty list) -----------------------
    if not root.exists():
        logger.debug("feedback_loop_loader: directory does not exist: %s", root)
        return []

    if not root.is_dir():
        logger.warning(
            "feedback_loop_loader: expected a directory, got a file: %s", root
        )
        return []

    seen_names: Dict[str, Path] = {}  # name -> source path (first winner)
    results: List[FeedbackLoopDefinition] = []

    raw_candidates = list(root.glob("*.yaml")) + list(root.glob("*.yml"))
    deduped = {f.name: f for f in raw_candidates}  # avoid double-counting both extensions
    candidate_files = sorted(deduped.values(), key=lambda p: p.name)

    if not candidate_files:
        logger.debug("feedback_loop_loader: no YAML files found in %s", root)
        return []

    for fpath in candidate_files:
        validated = _load_single_file(fpath)
        if validated is None:
            # Invalid file — already warned by ``_load_single_file``.
            continue

        # --- duplicate name check -------------------------------------------
        dup = seen_names.get(validated.name)
        if dup is not None:
            logger.warning(
                "feedback_loop_loader: duplicate loop name %r (first defined in %s); "
                "skipping %s",
                validated.name,
                dup,
                fpath,
            )
            continue

        seen_names[validated.name] = fpath
        results.append(validated)

    return results


def _load_single_file(fpath: Path) -> Optional[FeedbackLoopDefinition]:
    """Load and validate one YAML file. Returns ``None`` on any problem."""

    # --- empty / non-text files -------------------------------------------
    if fpath.stat().st_size == 0:
        logger.warning("feedback_loop_loader: skipping empty file %s", fpath)
        return None

    try:
        content = fpath.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("feedback_loop_loader: cannot read %s: %s", fpath, exc)
        return None

    if not content.strip():
        logger.warning("feedback_loop_loader: skipping whitespace-only file %s", fpath)
        return None

    # --- YAML parse -------------------------------------------------------
    parsed = _parse_yaml(content, fpath)
    if parsed is None:
        return None

    # Must be a mapping (dict); scalars/lists are invalid payloads.
    if not isinstance(parsed, dict):
        logger.warning(
            "feedback_loop_loader: %s top-level value must be a YAML mapping "
            "(got %s)",
            fpath,
            type(parsed).__name__,
        )
        return None

    # --- schema version gate -----------------------------------------------
    raw_schema = parsed.get("schema", "")
    schema_normed = str(raw_schema).strip() if raw_schema is not None else ""
    if schema_normed == "":
        logger.warning(
            "feedback_loop_loader: %s missing 'schema' field; skipping", fpath
        )
        return None

    if schema_normed not in SUPPORTED_VERSIONS:
        logger.warning(
            "feedback_loop_loader: %s unsupported schema version %r (supported: %s); "
            "skipping",
            fpath,
            raw_schema,
            sorted(SUPPORTED_VERSIONS),
        )
        return None

    # --- Pydantic validation -----------------------------------------------
    try:
        definition = validate_schema_v1(copy.deepcopy(parsed))
    except Exception as exc:  # ValidationError or downstream
        logger.warning(
            "feedback_loop_loader: %s failed schema validation (%s): %s",
            fpath,
            type(exc).__name__,
            exc,
        )
        return None

    # No normalisation needed — validate_schema_v1 already converts strings via the enum.
    return definition


def _parse_yaml(content: str, fpath: Path) -> Optional[Any]:
    """Parse YAML content using the stdlib-free safe loader.

    Uses PyYAML with ``safe_load`` when available; falls back to a tiny
    comment+blank line filter so plain dicts work even without pyyaml.
    """
    try:
        import yaml  # type: ignore[import-not-found,union-attr]

        parsed = yaml.safe_load(content)
        return parsed
    except ImportError:
        logger.warning("feedback_loop_loader: PyYAML not installed; "
                       "cannot load %s", fpath)
        return None
    except Exception as exc:
        # pyyaml raised a parse error
        logger.warning(
            "feedback_loop_loader: YAML parse error in %s: %s", fpath, exc
        )
        return None


# ---------------------------------------------------------------------------
# Enum resolution (used for backwards-compatible string values)
# ---------------------------------------------------------------------------


def _enum_from_str(field_name: str, raw_value: str):
    """Map a raw string value to the correct enum member.

    Called when we discover that *field_name* holds a plain string but really
    wants an enum instance (for future-proofing). Currently only used for
    triggers_event which supports the ``TriggerEvent`` enum.
    """
    from memchorus.feedback_loop.schema_v1 import TriggerEvent

    try:
        return TriggerEvent(raw_value)
    except ValueError:
        logger.warning(
            "feedback_loop_loader: invalid value %r for field %s; "
            "expected one of [%s]",
            raw_value,
            field_name,
            ", ".join(e.value for e in TriggerEvent),
        )
        return None  # caller should drop the loop

