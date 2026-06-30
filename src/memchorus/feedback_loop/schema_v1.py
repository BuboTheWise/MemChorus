"""Schema v1 for declarative feedback loop YAML definitions.

Defines the Pydantic models that represent a single custom feedback loop
as declared in a YAML file under ``~/.hermes/custom_loops/``.
"""

import re
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from pydantic import BaseModel as _BaseModel
    from pydantic import Field as _Field
    from pydantic import field_validator as _fv
    from pydantic import model_validator as _mv
else:
    from pydantic import BaseModel, Field, field_validator, model_validator

    _BaseModel = BaseModel
    _Field = Field
    _fv = field_validator
    _mv = model_validator

from pydantic import ValidationError  # noqa: E402

__all__ = [
    "ConditionSignal",
    "FeedbackLoopDefinition",
    "TriggerEvent",
    "SUPPORTED_VERSIONS",
    "validate_schema_v1",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_VERSIONS = {"schema_v1"}
DEFAULT_COOLDOWN_SECONDS = 60
MAX_COOLDOWN_SECONDS = 3_600  # 1 hour cap
MIN_COOLDOWN_SECONDS = 0      # zero allows immediate retry
_ALLOWED_SIGNAL_TYPES_PAT = re.compile(r"^[a-z][a-z0-9_]*$")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TriggerEvent(str, Enum):
    """When the feedback loop is triggered.

    Values:
        pre_llm_call: fire before an LLM call decision point.
        post_tool_call: fire after a tool call completes.
    """

    PRE_LLM_CALL = "pre_llm_call"
    POST_TOOL_CALL = "post_tool_call"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ConditionSignal(_BaseModel):  # type: ignore[name-defined]
    """A single signal condition inside ``conditions``.

    A signal is one key-value pair where the value describes a threshold
    or pattern to match against.
    """

    type: str
    value: Any

    @_fv("type")  # type: ignore[arg-type]
    @classmethod
    def _validate_type_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("signal.type must be a non-empty string")
        return v


class FeedbackLoopDefinition(_BaseModel):  # type: ignore[name-defined]
    """Declarative definition of one feedback loop from a YAML file.

    Fields
    ------
    schema: Always ``schema_v1`` for this model.
    name: Human-readable unique identifier for the loop.
    trigger_event: When it fires (pre_llm_call | post_tool_call).
    conditions: Dict mapping signal types to ConditionSignal objects (optional).
    correction_prompt: String template used to generate a correction prompt (optional).
    cooldown_interval: Minimum seconds between consecutive firings.
    priority: Integer priority within the same trigger_event; higher wins on conflicts.
    enabled: Toggle loop on/off.
    """

    model_config = {
        "populate_by_name": True,  # allow both 'schema' (alias) and 'schema_version' 
    }

    schema_version: str = _Field(
        ..., alias="schema", description="Schema version discriminator"
    )  # type: ignore[call-arg]
    name: str = _Field(..., description="Unique loop identifier")  # type: ignore[call-arg]
    trigger_event: TriggerEvent = _Field(
        ..., description="When the loop fires"  # type: ignore[call-arg]
    )
    conditions: Dict[str, ConditionSignal] = _Field(  # type: ignore[call-arg]
        default_factory=dict, description="Optional signal conditions"
    )
    correction_prompt: Optional[str] = _Field(  # type: ignore[call-arg]
        default=None, description="Correction prompt template (optional)"
    )
    cooldown_interval: int = _Field(  # type: ignore[call-arg]
        default=DEFAULT_COOLDOWN_SECONDS,
        description="Cooldown between firings in seconds",
    )
    priority: int = _Field(default=50, description="Loop priority (int)")  # type: ignore[call-arg]
    enabled: bool = _Field(default=True, description="Whether the loop is active")  # type: ignore[call-arg]

    # -- validators -----------------------------------------------------------

    @_fv("schema_version")  # type: ignore[arg-type]
    @classmethod
    def _validate_schema(cls, v: str) -> str:
        normed = v.strip()
        if normed not in SUPPORTED_VERSIONS:
            raise ValueError(
                f"unsupported schema version: {normed!r}; expected one of {SUPPORTED_VERSIONS}"
            )
        return normed

    @_fv("name")  # type: ignore[arg-type]
    @classmethod
    def _validate_name(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("name must be a non-empty string")
        return stripped

    @_fv("cooldown_interval")  # type: ignore[arg-type]
    @classmethod
    def _validate_cooldown(cls, v: int) -> int:
        if v < MIN_COOLDOWN_SECONDS:
            raise ValueError(
                f"cooldown_interval must be >= {MIN_COOLDOWN_SECONDS}, got {v}"
            )
        if v > MAX_COOLDOWN_SECONDS:
            raise ValueError(
                f"cooldown_interval must be <= {MAX_COOLDOWN_SECONDS}, got {v}"
            )
        return v

    @_fv("priority")  # type: ignore[arg-type]
    @classmethod
    def _validate_priority(cls, v: int) -> int:
        if abs(v) > 10_000:
            raise ValueError("priority must be within [-10000, +10000]")
        return v

    @_mv(mode="after")  # type: ignore[arg-type]
    def _validate_conditions(self):
        """Validate that all condition keys have valid structure."""
        for key, signal in self.conditions.items():
            key_clean = key.strip()
            if not key_clean or not _ALLOWED_SIGNAL_TYPES_PAT.match(key_clean):
                raise ValueError(
                    f"condition key {key!r} is invalid: must be non-empty "
                    "lowercase-alphanumeric-underscore"
                )
        return self

    # -- helpers ---------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the definition to a plain dict (JSON-safe)."""
        d = self.model_dump(mode="json")
        if isinstance(d.get("conditions"), dict):
            for k, v in d["conditions"].items():
                if hasattr(v, "model_dump"):
                    d["conditions"][k] = v.model_dump()
        return d

    # -- stdlib-only fallback -------------------------------------------------

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "FeedbackLoopDefinition":
        """Accept a plain dict and validate it against the schema.

        Used when Pydantic is unavailable or when callers want an explicit
        conversion path that always raises ValidationError on invalid input.
        """
        return cls(**raw)


def validate_schema_v1(raw: Dict[str, Any]) -> FeedbackLoopDefinition:
    r"""Validate a raw YAML-decoded dict against schema_v1 rules.

    Parameters
    ----------
    raw : dict
        Decoded YAML content as a plain dictionary.

    Returns
    -------
    FeedbackLoopDefinition
        Validated Pydantic model instance.

    Raises
    ------
    ValidationError
        If any field fails validation.
    """
    return FeedbackLoopDefinition(**raw)
