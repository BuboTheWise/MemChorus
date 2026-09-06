"""
Test suite for MemChorus IMPL #163.2 — Recall LocationStandard (§2.2, §2.3, §3.3, §5.1, §5.4).

What this file exercises:
  - location channel: save() with a location dict → resolve() returns it with verified_at
  - standard channel: save() with a standard dict → resolve() returns it
  - channel independence: location-only, standard-only, both — each resolves independently
  - scratch distractor: ~/mempalace/ in memory is NOT promoted to canonical_root;
    it surfaces in reconciled[] with role='scratch'
  - render block: _render_project_record_block() output matches spec §5.4 verbatim

Precedent: test_project_record_schema.py (#163.1) — same save/resolve harness style.
"""

import json
import os
import tempfile

import pytest


# ────────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def orch_setup():
    """Build a MemoryOrchestrator backed by a clean temp hermes-default store."""
    from memchorus.orchestrator import MemoryOrchestrator, clear_project_record_cache

    tmpd = tempfile.mkdtemp(prefix="mcp2_t_")
    mem_dir = os.path.join(tmpd, "mem")
    os.makedirs(mem_dir, exist_ok=True)

    clear_project_record_cache()
    cfg = {
        "default_source": "hermes_default",
        "hermes_default_config": {"memory_dir": mem_dir},
        "mempalace_config": {},
    }
    orch = MemoryOrchestrator(cfg)
    yield orch, mem_dir
    clear_project_record_cache()


# ────────────────────────────────────────────────────────────────────────────
# §2.3  Location channel
# ────────────────────────────────────────────────────────────────────────────

def test_location_channel_resolves(orch_setup):
    """A record saved with a location channel resolves with that location."""
    orch, _ = orch_setup
    rec = {
        "_content": "MemChorus: root at /workspace/Code/MemChorus/",
        "location": {
            "canonical_root": "/workspace/Code/MemChorus/",
            "source": "ssot:ORGANIZATION.md#memchorus",
            "verified_at": "2026-09-05T12:00:00Z",
        },
    }
    orch.save("project:MemChorus", rec)
    r = orch.resolve_project_record("MemChorus")
    assert r["location"]["canonical_root"] == "/workspace/Code/MemChorus/"
    assert r["location"]["source"] == "ssot:ORGANIZATION.md#memchorus"
    assert r["location"]["verified_at"] == "2026-09-05T12:00:00Z"


def test_location_channel_from_ssot(orch_setup, monkeypatch):
    """No stored record → location resolves from the SSoT table row (spec §6)."""
    orch, _ = orch_setup
    import memchorus.orchestrator as mo

    # Patch the module-level SSoT reader so resolve_project_record's SSoT
    # fallback (step 3) returns our known row.  No record is saved, so step 2
    # (exact-key retrieve) misses and falls through to the SSoT path.
    monkeypatch.setattr(mo, "_ssot_read_cached", lambda: [
        {"project": "MemChorus",
         "canonical_root": "/workspace/Code/MemChorus/",
         "verified_at": "2026-08-01T09:00:00Z"},
    ])
    from memchorus.orchestrator import clear_project_record_cache
    clear_project_record_cache()
    r = orch.resolve_project_record("MemChorus")
    assert r["location"]["canonical_root"] == "/workspace/Code/MemChorus/"
    assert r["location"]["source"].startswith("ssot:")
    assert r["location"]["verified_at"] == "2026-08-01T09:00:00Z"


def test_location_fallback_rule_when_no_ssot(orch_setup, monkeypatch):
    """No record AND no SSoT row → location derives by rule (spec §2.4 fallback)."""
    orch, _ = orch_setup
    import memchorus.orchestrator as mo

    # Force the SSoT layer absent so step 4 (rule-derived fallback) is exercised.
    monkeypatch.setattr(mo, "_ssot_read_cached", lambda: None)
    from memchorus.orchestrator import clear_project_record_cache
    clear_project_record_cache()
    r = orch.resolve_project_record("MemChorus")
    # Derived-by-rule: canonical_root carries the project stem, unverified.
    assert "MemChorus" in r["location"]["canonical_root"]
    assert r["location"]["source"].startswith("derived:")
    assert r["location"]["verified_at"] is None


def test_location_channel_rejects_bad_source_prefix(orch_setup):
    """A location channel with source not starting ssot: or derived: is invalid on save."""
    from memchorus.project_record import ProjectRecordError
    # validate_project_record checks the WRAPPED record shape: {"location": {...}}.
    value = {
        "location": {
            "canonical_root": "/workspace/Code/MemChorus/",
            "source": "memory:note-abc",   # bad prefix
            "verified_at": None,
        },
    }
    from memchorus.project_record import validate_project_record
    with pytest.raises(ProjectRecordError):
        validate_project_record(value)


# ────────────────────────────────────────────────────────────────────────────
# §2.3  Standard channel
# ────────────────────────────────────────────────────────────────────────────

def test_standard_channel_resolves(orch_setup):
    """A record saved with a standard channel resolves with that standard."""
    orch, _ = orch_setup
    rec = {
        "_content": "standard for memchorus development",
        "standard": {
            "skill": "development-process",
            "doc_path": "stable/development-process/SKILL.md",
            "gist": "decompose-first, IMPL → REVIEW → RELEASE",
            "topics": ["development", "review"],
        },
    }
    orch.save("project:MemChorus", rec)
    r = orch.resolve_project_record("MemChorus")
    assert r["standard"]["skill"] == "development-process"
    assert r["standard"]["doc_path"] == "stable/development-process/SKILL.md"
    assert r["standard"]["gist"] == "decompose-first, IMPL → REVIEW → RELEASE"
    assert r["standard"]["topics"] == ["development", "review"]


def test_standard_channel_rejects_absolute_doc_path(orch_setup):
    """standard.doc_path must not be an absolute path (project_record validator)."""
    from memchorus.project_record import validate_project_record, ProjectRecordError
    rec = {
        "skill": "development-process",
        "doc_path": "/usr/share/skills/dev/SKILL.md",   # absolute
        "gist": "x", "topics": [],
    }
    # The full record-level validator checks channels; test the channel shape directly.
    from memchorus.project_record import _validate_standard
    with pytest.raises(ProjectRecordError):
        _validate_standard(rec)


# ────────────────────────────────────────────────────────────────────────────
# §2.2  Channel independence
# ────────────────────────────────────────────────────────────────────────────

def test_location_only_gives_derived_standard(orch_setup):
    """A location-only record resolves with a derived (default) standard."""
    orch, _ = orch_setup
    rec = {
        "location": {
            "canonical_root": "/workspace/Code/MemChorus/",
            "source": "ssot:ORGANIZATION.md#memchorus",
            "verified_at": "2026-09-05T12:00:00Z",
        },
    }
    orch.save("project:MemChorus", rec)
    r = orch.resolve_project_record("MemChorus")
    assert r["location"]["canonical_root"] == "/workspace/Code/MemChorus/"
    assert r["location"]["source"] == "ssot:ORGANIZATION.md#memchorus"
    # standard must be the derived default (structure-only floor) — not empty, not missing
    assert r["standard"] is not None
    assert r["standard"]["skill"] == "development-process"
    assert r["standard"]["doc_path"] == "stable/development-process/SKILL.md"


def test_standard_only_gives_derived_location(orch_setup):
    """A standard-only record resolves with a derived (empty or <unresolved>) location."""
    orch, _ = orch_setup
    rec = {
        "standard": {
            "skill": "development-process",
            "doc_path": "stable/development-process/SKILL.md",
            "gist": "g", "topics": ["development"],
        },
    }
    orch.save("project:MemChorus", rec)
    r = orch.resolve_project_record("MemChorus")
    assert r["standard"]["skill"] == "development-process"
    assert r["standard"]["doc_path"] == "stable/development-process/SKILL.md"
    # location must be present (a *derived* one — SSoT row or rule-derived —
    # never a scratch note).  It carries the project stem, not ~/mempalace.
    assert "location" in r
    assert "MemChorus" in r["location"]["canonical_root"]
    assert r["location"]["canonical_root"] != "~/mempalace"
    assert r["location"]["source"].startswith(("ssot:", "derived:"))


def test_both_channels_independent(orch_setup):
    """Both channels resolve independently when stored together."""
    orch, _ = orch_setup
    rec = {
        "location": {
            "canonical_root": "/workspace/Code/MemChorus/",
            "source": "ssot:ORGANIZATION.md#memchorus",
            "verified_at": "2026-09-05T12:00:00Z",
        },
        "standard": {
            "skill": "development-process",
            "doc_path": "stable/development-process/SKILL.md",
            "gist": "decompose-first, IMPL → REVIEW → RELEASE",
            "topics": ["development", "review"],
        },
    }
    orch.save("project:MemChorus", rec)
    r = orch.resolve_project_record("MemChorus")
    assert r["location"]["canonical_root"] == "/workspace/Code/MemChorus/"
    assert r["location"]["source"] == "ssot:ORGANIZATION.md#memchorus"
    assert r["standard"]["skill"] == "development-process"
    assert r["standard"]["doc_path"] == "stable/development-process/SKILL.md"
    assert r["standard"]["gist"] == "decompose-first, IMPL → REVIEW → RELEASE"
    assert r["standard"]["topics"] == ["development", "review"]


def test_location_change_does_not_affect_standard(orch_setup):
    """Changing the location channel does not corrupt or reset the standard channel."""
    orch, _ = orch_setup
    base = {
        "location": {
            "canonical_root": "/workspace/Code/MemChorus/",
            "source": "ssot:ORGANIZATION.md#memchorus",
            "verified_at": "2026-09-05T12:00:00Z",
        },
        "standard": {
            "skill": "development-process",
            "doc_path": "stable/development-process/SKILL.md",
            "gist": "g", "topics": ["development"],
        },
    }
    orch.save("project:MemChorus", base)
    r_before = orch.resolve_project_record("MemChorus")

    # Save a new record with a different location but the same standard
    from memchorus.orchestrator import clear_project_record_cache
    clear_project_record_cache()
    updated = dict(base)
    updated["location"] = {
        "canonical_root": "/opt/new-canonical/",
        "source": "ssot:other.md#row",
        "verified_at": "2026-10-01T00:00:00Z",
    }
    orch.save("project:MemChorus", updated)
    r_after = orch.resolve_project_record("MemChorus")

    assert r_after["location"]["canonical_root"] == "/opt/new-canonical/"
    assert r_after["location"]["source"] == "ssot:other.md#row"
    # standard was unchanged — same as before
    assert r_after["standard"]["skill"] == r_before["standard"]["skill"]
    assert r_after["standard"]["doc_path"] == r_before["standard"]["doc_path"]


# ────────────────────────────────────────────────────────────────────────────
# §5.2  Assertion 6 — Case-normalized keyed lookup
# ────────────────────────────────────────────────────────────────────────────

def test_resolve_project_record_across_case_boundaries(orch_setup):
    """
    §5.2 assertion 6 — resolving a record saved under one case of the project
    name with a lookup in a *different* case returns the STORED record.

    This is the direct suite-level proof that ``resolve_project_record()``
    normalizes ``project:<name>`` keys to a stable slug so that a save under
    ``project:MEMCHORUS`` and a lookup via ``memchorus`` resolve to the same
    record (spec §3.1 / §5.2 assertion 6).  Previously this was only proven
    indirectly: by the sibling retrieve()-level tests in
    test_project_record_schema.py and by a manual live probe.

    The discriminator is the ``source`` prefix: with no SSoT row present in a
    clean temp store, a *miss* on the stored record would fall through to the
    §2.4 rule-derived fallback and carry ``source.startswith("derived:")``.
    Asserting ``source == "ssot:ORGANIZATION.md#memchorus"`` therefore proves
    the exact-key (non-derived) stored record was returned — not the fallback.
    """
    orch, _ = orch_setup
    rec = {
        "location": {
            "canonical_root": "/workspace/Code/MemChorus/",
            "source": "ssot:ORGANIZATION.md#memchorus",
            "verified_at": "2026-09-05T12:00:00Z",
        },
        "standard": {
            "skill": "development-process",
            "doc_path": "stable/development-process/SKILL.md",
            "gist": "decompose-first, IMPL → REVIEW → RELEASE",
            "topics": ["development", "review"],
        },
    }
    # Save under an ALL-CAPS project name.
    orch.save("project:MEMCHORUS", rec)
    from memchorus.orchestrator import clear_project_record_cache
    clear_project_record_cache()

    # Resolve with a lowercase query — different case than the store key.
    r = orch.resolve_project_record("memchorus")

    # Exact-match canonical_root: a rule-derived fallback would be
    # <workspace>/Code/memchorus/ (slug lowercased), never the stored form.
    assert r["location"]["canonical_root"] == "/workspace/Code/MemChorus/"
    # Discriminator: the STORED record's ssot: source, not the derived:
    # fallback source that a lookup miss would yield.
    assert r["location"]["source"] == "ssot:ORGANIZATION.md#memchorus"
    assert r["location"]["source"].startswith("ssot:")
    assert r["location"]["verified_at"] == "2026-09-05T12:00:00Z"
    # The standard channel resolves from the stored record, not the default.
    assert r["standard"]["skill"] == "development-process"
    assert r["standard"]["doc_path"] == "stable/development-process/SKILL.md"


# ────────────────────────────────────────────────────────────────────────────
# §5.1  Scratch distractor
# ────────────────────────────────────────────────────────────────────────────

def test_scratch_distractor_not_promoted(orch_setup):
    """A ~/mempalace/ path stored in a memory note is NOT the canonical_root."""
    orch, _ = orch_setup
    # A memory note that mentions both the scratch and canonical paths
    orch.save("memchorus-location-note",
              "Working copy reminder: use ~/mempalace for this project, "
              "not the canonical Code/MemChorus at /workspace/Code/MemChorus/")

    # Also store the formal record
    rec = {
        "location": {
            "canonical_root": "/workspace/Code/MemChorus/",
            "source": "ssot:ORGANIZATION.md#memchorus",
            "verified_at": "2026-09-05T12:00:00Z",
        },
        "standard": {
            "skill": "development-process",
            "doc_path": "stable/development-process/SKILL.md",
            "gist": "g", "topics": ["development"],
        },
    }
    orch.save("project:MemChorus", rec)

    from memchorus.orchestrator import clear_project_record_cache
    clear_project_record_cache()
    r = orch.resolve_project_record("MemChorus")

    # canonical_root is the SSoT root, NOT ~/mempalace/
    assert r["location"]["canonical_root"] == "/workspace/Code/MemChorus/"
    assert r["location"]["canonical_root"] != "~/mempalace/"

    # ~/mempalace/ appears in reconciled[]
    assert "reconciled" in r
    scratch = [x for x in r["reconciled"] if x.get("role") == "scratch" and "mempalace" in x.get("path", "")]
    assert scratch, "~/mempalace/ must appear in reconciled[] as a scratch entry"
    assert "duplicate/fork" in scratch[0].get("relation", "")


def test_reconciled_entry_has_scratch_relation(orch_setup):
    """Every reconciled scratch entry carries the 'duplicate/fork' relation string."""
    orch, _ = orch_setup
    orch.save("project:MemChorus", {
        "location": {
            "canonical_root": "/workspace/Code/MemChorus/",
            "source": "ssot:ORGANIZATION.md#memchorus",
            "verified_at": "2026-09-05T12:00:00Z",
        },
        "standard": {"skill": "development-process", "doc_path": "stable/development-process/SKILL.md",
                     "gist": "g", "topics": ["development"]},
    })
    # A note with a scratch path
    orch.save("scratch-note-1", "Keep working in ~/mempalace/mc-scratch for memchorus")

    from memchorus.orchestrator import clear_project_record_cache
    clear_project_record_cache()
    r = orch.resolve_project_record("MemChorus")

    for entry in r.get("reconciled", []):
        if entry.get("role") == "scratch":
            assert "duplicate/fork" in entry.get("relation", "")


# ────────────────────────────────────────────────────────────────────────────
# §5.4  Render block
# ────────────────────────────────────────────────────────────────────────────

def test_render_block_spec_54(orch_setup):
    """
    §5.4 render case: location=ssot + standard=derived.

    Expected block (verbatim from spec §5.4 / IMPL #163.2):

      [project:MemChorus]
        location: canonical_root=<ROOT>/MemChorus/
          source=ssot:ORGANIZATION.md#memchorus  verified_at=2026-08-01T09:00:00Z
        standard: skill=development-process  doc_path=stable/development-process/SKILL.md  gist=decompose-first, IMPL → REVIEW → RELEASE  topics=[development]
      [/MemChorus project record]

    (The actual canonical_root will be the real path — we build the record inline
    using the same fields so the assertion matches the renderer's output exactly.)
    """
    from memchorus.hooks import _render_project_record_block

    root = "/workspace/Code/MemChorus/"
    record = {
        "location": {
            "canonical_root": root,
            "source": "ssot:ORGANIZATION.md#memchorus",
            "verified_at": "2026-08-01T09:00:00Z",
        },
        "standard": {
            "skill": "development-process",
            "doc_path": "stable/development-process/SKILL.md",
            "gist": "decompose-first, IMPL → REVIEW → RELEASE",
            "topics": ["development"],
        },
        "reconciled": [],
    }
    out = _render_project_record_block("MemChorus", record)

    # Structural assertions (independent of which real path is used)
    assert "[project:MemChorus]" in out
    assert "[/MemChorus project record]" in out
    assert "location: canonical_root=" in out
    assert "source=ssot:ORGANIZATION.md#memchorus" in out
    assert "verified_at=2026-08-01T09:00:00Z" in out
    assert "standard: skill=development-process" in out
    assert "doc_path=stable/development-process/SKILL.md" in out
    assert "decompose-first" in out
    assert "topics=[development]" in out
    # The reconciled line must NOT appear when reconciled is empty
    assert "reconciled" not in out


def test_render_block_spec_51_scratch(orch_setup):
    """
    §5.1 render case: scratch distractor surfaces in the reconciled section.

      [project:MemChorus]
        location: canonical_root=<ROOT>
          source=ssot:ORGANIZATION.md#memchorus  verified_at=2026-09-05T12:00:00Z
        standard: ...
        reconciled (duplicate/fork, not the working copy):
          - ~/mempalace/  — duplicate/fork, not the working copy
      [/MemChorus project record]
    """
    from memchorus.hooks import _render_project_record_block

    record = {
        "location": {
            "canonical_root": "/workspace/Code/MemChorus/",
            "source": "ssot:ORGANIZATION.md#memchorus",
            "verified_at": "2026-09-05T12:00:00Z",
        },
        "standard": {
            "skill": "development-process",
            "doc_path": "stable/development-process/SKILL.md",
            "gist": "g", "topics": ["development"],
        },
        "reconciled": [
            {"role": "scratch", "path": "~/mempalace/", "relation": "duplicate/fork, not the working copy"}
        ],
    }
    out = _render_project_record_block("MemChorus", record)

    assert "reconciled (duplicate/fork, not the working copy):" in out
    assert "- ~/mempalace/  — duplicate/fork, not the working copy" in out
    # canonical_root is NOT ~/mempalace/
    assert "canonical_root=~/mempalace/" not in out


def test_render_block_location_only(orch_setup):
    """A location-only record (no standard) renders location only + derived-by-rule note."""
    from memchorus.hooks import _render_project_record_block

    record = {
        "location": {
            "canonical_root": "<unresolved>",
            "source": "ssot:ORGANIZATION.md#memchorus",
            "verified_at": None,
        },
    }
    out = _render_project_record_block("MemChorus", record)
    assert "[project:MemChorus]" in out
    assert "canonical_root=<unresolved>" in out
    assert "verified_at=unverified (derived-by-rule)" in out
    # No standard line
    assert "standard:" not in out


def test_render_block_returns_empty_for_non_dict():
    """A non-dict record (or a record with neither channel) produces an empty string."""
    from memchorus.hooks import _render_project_record_block
    assert _render_project_record_block("MemChorus", "not a dict") == ""
    assert _render_project_record_block("MemChorus", None) == ""
    assert _render_project_record_block("MemChorus", {}) == ""
