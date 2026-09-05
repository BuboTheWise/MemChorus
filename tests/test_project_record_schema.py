#!/usr/bin/env python3
"""
test_project_record_schema.py — IMPL #163 (Recall Location & Standard Channels)
schema card.

Covers the schema / contract deliverables from
``MemChorus-Recall-LocationStandard-Design-Spec.md`` §2–§3:

  * stable ``project:<name>`` key namespace + case normalization (§3.1)
  * §2.1 location shape (canonical_root / source / verified_at) and validation
  * §2.2 standard shape (skill / doc_path / gist / topics) and validation
  * §2.3 fail-loud on save for malformed channels
  * the two channels are INDEPENDENT optional fields (one/both/neither)
  * §3.2 channels stay at top level (out of the free-text body path)
  * §2.4 defaults + ``verified_at`` null = derived-by-rule/unverified
  * base-class ``resolve_project_record`` no-op on MemorySource (mirrors recall_kg)

Recall *dispatch*, session-start rendering, CLI, and doctor are #163.2–#163.4
and are intentionally out of scope here.
"""

import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from memchorus import project_record as pr                 # noqa: E402
from memchorus.memory_source import MemorySource            # noqa: E402
from memchorus.orchestrator import MemoryOrchestrator       # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures — valid channel objects (the spec's accepted shapes)
# ---------------------------------------------------------------------------

VALID_LOCATION = {
    "canonical_root": "<workspace>/Code/MemChorus/",
    "source": "ssot:ORGANIZATION.md#memchorus",
    "verified_at": None,   # null ⇒ derived-by-rule / unverified (§2.4)
}

VALID_STANDARD = {
    "skill": "development-process",
    "doc_path": "stable/development-process/SKILL.md",
    "gist": "Development workflow: branching, review, TDD gates",
    "topics": ["development", "review"],
}


@pytest.fixture
def fail():
    """The §2.3 fail-loud validator — a bad channel must raise, never raise."""
    return pr.validate_project_record


# ---------------------------------------------------------------------------
# §3.1 — stable key namespace + case normalization
# ---------------------------------------------------------------------------

def test_normalize_key_case_insensitive_name():
    assert pr.normalize_project_key(name="MemChorus") == "project:memchorus"
    assert pr.normalize_project_key(name="MEMCHORUS") == "project:memchorus"
    assert pr.normalize_project_key(name="memchorus") == "project:memchorus"


def test_normalize_key_from_existing_namespaced_key():
    # A store under project:MemChorus must resolve to the same stable key.
    assert pr.normalize_project_key(key="project:MemChorus") == "project:memchorus"
    assert pr.normalize_project_key(key="project:MEMCHORUS") == "project:memchorus"


def test_normalize_key_collapses_non_alphanumeric():
    # Spaces / punctuation collapse to '-' so a case-or-slug miss becomes
    # the byte-identical key instead of a silent lookup miss.
    assert pr.normalize_project_key(name="My Project") == "project:my-project"
    assert pr.normalize_project_key(name="My.Project") == "project:my-project"
    assert pr.normalize_project_key(name="My_Project") == "project:my-project"


def test_normalize_key_requires_some_input():
    with pytest.raises(pr.ProjectRecordError):
        pr.normalize_project_key()


def test_is_project_key():
    assert pr.is_project_key("project:memchorus") is True
    assert pr.is_project_key("project:MemChorus") is True
    assert pr.is_project_key("foo") is False
    assert pr.is_project_key(None) is False
    assert pr.is_project_key(123) is False


def test_all_case_variants_resolve_to_same_key():
    variants = [
        pr.normalize_project_key(name="MemChorus"),
        pr.normalize_project_key(name="memchorus"),
        pr.normalize_project_key(name="MEMCHORUS"),
        pr.normalize_project_key(key="project:MemChorus"),
    ]
    assert len(set(variants)) == 1, vars(variants) if False else variants


# ---------------------------------------------------------------------------
# §2.1 + §2.3 — location validation
# ---------------------------------------------------------------------------

def test_valid_location_passes():
    pr.validate_project_record({"location": VALID_LOCATION})  # must not raise


def test_location_verified_at_accepts_iso_instant():
    ok = {
        "canonical_root": "<workspace>/Code/X/",
        "source": "ssot:ORGANIZATION.md#x",
        "verified_at": "2026-09-05T12:30:00Z",
    }
    pr.validate_project_record({"location": ok})


@pytest.mark.parametrize("bad_location", [
    {"canonical_root": "   ", "source": "ssot:x"},                       # empty root
    {"canonical_root": "<ws>/X/", "source": "bad"},                      # bad source
    {"canonical_root": "<ws>/X/", "source": "raw note"},                 # bad source
    {"canonical_root": "<ws>/X/", "source": "ssot:x", "verified_at": "nope"},
    {"canonical_root": "<ws>/X/", "source": "ssot:x", "verified_at": 123},
    {"canonical_root": "<ws>/X/", "source": "ssot:x", "unknown_key": 1}, # unknown key
    "not-a-dict",                                                       # wrong type
])
def test_location_invalid_rejected(fail, bad_location):
    with pytest.raises(pr.ProjectRecordError):
        fail({"location": bad_location})


# ---------------------------------------------------------------------------
# §2.2 + §2.3 — standard validation
# ---------------------------------------------------------------------------

def test_valid_standard_passes():
    pr.validate_project_record({"standard": VALID_STANDARD})


@pytest.mark.parametrize("bad_standard", [
    {"skill": "Development-Process", "doc_path": "a.md"},   # skill must be lower slug
    {"skill": "development-process", "doc_path": "/home/x.md"},  # abs path
    {"skill": "development-process", "doc_path": "~/x.md"},      # home abs
    {"skill": "development-process", "doc_path": "a.md", "gist": "z" * 200},
    {"skill": "development-process", "doc_path": "a.md", "topics": ["a"] * 7},
    {"skill": "development-process", "doc_path": "a.md", "bad": 1},  # unknown key
    "not-a-dict",
])
def test_standard_invalid_rejected(fail, bad_standard):
    with pytest.raises(pr.ProjectRecordError):
        fail({"standard": bad_standard})


# ---------------------------------------------------------------------------
# Independence invariant — the two channels never gate each other
# ---------------------------------------------------------------------------

def test_independence_only_location():
    rec = {"location": VALID_LOCATION}
    pr.validate_project_record(rec)
    assert "standard" not in rec
    assert pr.strip_project_channels(rec) == {}  # no standard channel to strip


def test_independence_only_standard():
    rec = {"standard": VALID_STANDARD}
    pr.validate_project_record(rec)
    assert "location" not in rec


def test_independence_both():
    rec = {"location": VALID_LOCATION, "standard": VALID_STANDARD}
    pr.validate_project_record(rec)
    assert set(rec) == {"location", "standard"}
    assert pr.strip_project_channels(rec) == {}


def test_independence_neither():
    pr.validate_project_record({})       # both absent → valid
    pr.validate_project_record({})       # idempotent


def test_independence_bad_location_does_not_gate_standard():
    # An invalid location must NOT be accepted because standard is present,
    # and vice-versa — each is validated on its own path.
    with pytest.raises(pr.ProjectRecordError):
        pr.validate_project_record({"location": {"bad": 1}, "standard": VALID_STANDARD})
    with pytest.raises(pr.ProjectRecordError):
        pr.validate_project_record({"location": VALID_LOCATION, "standard": {"bad": 1}})


def test_non_dict_payload_is_not_structurally_validated():
    # A plain string body on a project: key has no channels to validate.
    pr.validate_project_record("just a note")  # must not raise


# ---------------------------------------------------------------------------
# §3.2 — channels stay at top level, OUT of the free-text body path
# ---------------------------------------------------------------------------

def test_channels_are_top_level_keys_not_serialized_into_body():
    rec = {"_content": "body text", "location": VALID_LOCATION, "standard": VALID_STANDARD}
    extracted = pr.extract_project_channels(rec)
    assert set(extracted) == {"location", "standard"}
    # The *body* (free-text path) still carries only the prose, not the channels:
    body = pr.strip_project_channels(rec)
    assert body == {"_content": "body text"}


def test_extract_returns_none_when_no_channels():
    assert pr.extract_project_channels({"_content": "x", "other": 1}) is None
    assert pr.extract_project_channels("plain") is None


def test_attach_is_byte_identical_passthrough_for_project_key():
    rec = {"location": VALID_LOCATION}
    out = pr.attach_project_channels(rec, "project:memchorus")
    assert out is rec  # identity — channels ride on the same top-level dict


def test_attach_plain_key_is_untouched():
    rec = {"location": VALID_LOCATION}
    out = pr.attach_project_channels(rec, "foo")
    assert out is rec


def test_build_record_composes_independently():
    assert pr.build_record() == {}
    assert pr.build_record(location=VALID_LOCATION) == {"location": VALID_LOCATION}
    assert pr.build_record(standard=VALID_STANDARD) == {"standard": VALID_STANDARD}
    both = pr.build_record(location=VALID_LOCATION, standard=VALID_STANDARD)
    assert both == {"location": VALID_LOCATION, "standard": VALID_STANDARD}


# ---------------------------------------------------------------------------
# §2.4 — defaults (versioned, from SSoT; never a free-text pointer)
# ---------------------------------------------------------------------------

def test_default_location_is_derived_rule_and_unverified():
    d = pr.default_location("MemChorus")
    assert d["canonical_root"] == "<workspace>/Code/MemChorus/"
    assert d["source"].startswith("derived:ORGANIZATION.md#")
    assert d["verified_at"] is None  # null ⇒ unverified, derived-by-rule
    # And it must be a valid location per §2.3 (no contradiction with §2.4).
    pr.validate_project_record({"location": d})


def test_default_standard_is_versioned_skill_pointer():
    d = pr.default_standard()
    assert d["skill"] == "development-process"
    assert d["doc_path"]  # non-empty
    pr.validate_project_record({"standard": d})


def test_default_standard_structure_only():
    d = pr.default_standard(structure_only=True)
    assert d["skill"] == "project-organization"
    pr.validate_project_record({"standard": d})


# ---------------------------------------------------------------------------
# Base-class no-op — MemorySource.resolve_project_record (mirrors recall_kg)
# ---------------------------------------------------------------------------

class _NoOpSource(MemorySource):
    """A concrete source that does NOT back the project-record channels —
    used to assert the base-class default is a no-op (like recall_kg)."""

    SUPPORTED_METHODS = ['save', 'retrieve', 'search', 'get_source_info']

    def __init__(self, name="noop_source", config=None):
        self.name = name
        self.config = config or {}

    def is_available(self):
        return True

    def save(self, key, value):
        return True

    def retrieve(self, key):
        return None

    def search(self, query, limit=10):
        return []

    def get_source_info(self):
        return {"name": self.name}

    def proactive_check(self, context=None):
        return {}

    def proactive_save(self, key, value, context=None):
        return True

    def delete(self, key):
        return False


def test_base_resolve_project_record_is_noop():
    src = _NoOpSource()
    assert src.resolve_project_record("project:memchorus") is None


def test_base_resolve_project_record_with_channels_arg_is_noop():
    src = _NoOpSource()
    assert src.resolve_project_record("memchorus", channels=["location", "standard"]) is None


def test_resolve_project_record_exists_on_base_class():
    # The hook must be a concrete base method (not abstract) so every existing
    # source keeps working unchanged — the same guarantee recall_kg provides.
    assert callable(getattr(MemorySource, "resolve_project_record", None))


# ---------------------------------------------------------------------------
# Integration — orchestrator save() enforces the contract & round-trips
# ---------------------------------------------------------------------------

@pytest.fixture
def orch(tmp_path):
    hermes_dir = os.path.join(str(tmp_path), 'hermes_mem')
    config = {
        'default_source': 'hermes_default',
        'hermes_default_config': {'memory_dir': hermes_dir},
        'mempalace_config': {},
    }
    o = MemoryOrchestrator(config)
    yield o


def test_orchestrator_save_round_trips_channels(orch):
    rec = {"_content": "body", "location": VALID_LOCATION, "standard": VALID_STANDARD}
    assert orch.save("project:MemChorus", rec) is True          # store (case M)
    out = orch.retrieve("project:memchorus")                     # retrieve (case m)
    assert out is not None
    assert out.get("location") == VALID_LOCATION
    assert out.get("standard") == VALID_STANDARD
    assert out.get("_content") == "body"


def test_orchestrator_case_variant_keys_resolve_together(orch):
    rec = {"location": VALID_LOCATION}
    assert orch.save("project:MEMCHORUS", rec) is True
    # A case-different retrieve must still find it (the §3.1 normalization).
    assert orch.retrieve("project:memchorus") is not None
    assert orch.retrieve("project:MemChorus") is not None


def test_orchestrator_rejects_invalid_location_on_save(orch):
    bad = {"location": {"canonical_root": "   ", "source": "ssot:x"}}
    with pytest.raises(pr.ProjectRecordError):
        orch.save("project:badproj", bad)


def test_orchestrator_rejects_invalid_standard_on_save(orch):
    bad = {"standard": {"skill": "Bad-Skill", "doc_path": "/abs/path.md"}}
    with pytest.raises(pr.ProjectRecordError):
        orch.save("project:badproj", bad)


def test_orchestrator_invalid_channels_are_not_persisted(orch):
    # Fail-loud: the save raises AND (because validation happens before any
    # source write) nothing was persisted under that key.
    with pytest.raises(pr.ProjectRecordError):
        orch.save("project:shouldnotexist", {"location": {"bogus": 1}})
    assert orch.retrieve("project:shouldnotexist") is None


def test_orchestrator_ordinary_save_is_unaffected(orch):
    # A plain non-project: dict and a plain string round-trip unchanged and do
    # NOT trigger the project-record validator.
    assert orch.save("plain_key", {"data": "x"}) is True
    assert orch.retrieve("plain_key") == {"data": "x"}
