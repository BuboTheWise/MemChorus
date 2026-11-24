#!/usr/bin/env python3
"""Tests for Issue #330 — transparent pointer-key resolution in the recall path."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus import mempalace_memory_source as mms  # noqa: E402
from memchorus.mempalace_memory_source import MemPalaceMemorySource  # noqa: E402


def _src(tmp_path):
    """Local-only source (no MCP) with an isolated cache dir."""
    return MemPalaceMemorySource(
        config={"skip_mcp": True, "cache_dir": str(tmp_path)}
    )


class TestCacheKeyVariants:
    def test_flat_pointer_expands_to_drawer_form(self):
        assert mms._cache_key_variants("decision-88e86e5790c4c3ec") == [
            "decision-88e86e5790c4c3ec",
            "DECISION_88e86e5790c4c3ec",
        ]

    def test_drawer_form_expands_back_to_flat(self):
        assert mms._cache_key_variants("DECISION_88e86e5790c4c3ec") == [
            "DECISION_88e86e5790c4c3ec",
            "decision-88e86e5790c4c3ec",
        ]

    def test_learning_category(self):
        assert mms._cache_key_variants("learning-6b65db6ba588431f") == [
            "learning-6b65db6ba588431f",
            "LEARNING_6b65db6ba588431f",
        ]

    def test_non_pointer_key_is_single_variant(self):
        assert mms._cache_key_variants("k1") == ["k1"]

    def test_genuine_drawer_id_is_untouched(self):
        kid = "drawer_alpha_bravo_abc123def4567890123456"
        assert mms._cache_key_variants(kid) == [kid]

    def test_short_or_non_hex_suffix_is_single_variant(self):
        # _POINTER_KEY_RE requires an 8+ hex suffix; anything shorter is a
        # non-pointer key and stays a single variant.
        assert mms._cache_key_variants("decision-abc") == ["decision-abc"]
        assert mms._cache_key_variants("decision-1234567") == ["decision-1234567"]
        assert mms._cache_key_variants("") == [""]

    def test_variants_never_dup_or_empty(self):
        for k in ("decision-88e86e5790c4c3ec", "k1", "drawer_a_b_cdef0123456789abcdef0123"):
            out = mms._cache_key_variants(k)
            assert len(out) == len(set(out))
            assert all(isinstance(v, str) and v for v in out)


class TestPointerKeyVariantResolution:
    def test_body_saved_as_drawer_form_resolves_via_flat_key(self, tmp_path):
        src = _src(tmp_path)
        src.save("DECISION_88e86e5790c4c3ec", {"note": "the pointer body"})
        out = src.retrieve("decision-88e86e5790c4c3ec")
        assert out == {"note": "the pointer body"}

    def test_body_saved_as_flat_key_resolves_via_drawer_form(self, tmp_path):
        src = _src(tmp_path)
        src.save("decision-88e86e5790c4c3ec", {"note": "the pointer body"})
        out = src.retrieve("DECISION_88e86e5790c4c3ec")
        assert out == {"note": "the pointer body"}

    def test_learning_key_resolves_both_directions(self, tmp_path):
        src = _src(tmp_path)
        src.save("LEARNING_6b65db6ba588431f", "lesson body")
        assert src.retrieve("learning-6b65db6ba588431f") == "lesson body"
        src2 = _src(tmp_path)
        src2.save("learning-70497e2829d583de", "lesson body 2")
        assert src2.retrieve("LEARNING_70497e2829d583de") == "lesson body 2"

    def test_non_pointer_key_roundtrip_unregressed(self, tmp_path):
        src = _src(tmp_path)
        src.save("k1", {"x": 1})
        assert src.retrieve("k1") == {"x": 1}

    def test_missing_pointer_key_still_misses(self, tmp_path):
        assert hasattr(mms, "RETRIEVE_MISS")
        src = _src(tmp_path)
        out = src.retrieve("decision-deadbeef00000000")
        assert out is mms.RETRIEVE_MISS or isinstance(out, type(mms.RETRIEVE_MISS))


class TestProfileRoutedFlatStore:
    def test_flat_body_on_named_leaf_is_found_by_that_leaf(self, tmp_path, monkeypatch):
        import memchorus.hermes_home as hh
        leaf = tmp_path / "leafB"
        mem_dir = leaf / "memories"
        mem_dir.mkdir(parents=True)
        # HermesDefaultMemorySource._safe_key lowercases the key, so the flat
        # body file is the *flat* form (category-<hex>), not the drawer form.
        (mem_dir / "decision-88e86e5790c4c3ec.json").write_text(
            '{"note": "leafB flat body"}'
        )
        monkeypatch.setattr(hh, "profile_home", lambda name=None: tmp_path / (name or "default"))
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": str(tmp_path / "cache")}
        )
        src._profile_name = "leafB"
        src._refetch_live = None
        out = src.retrieve("decision-88e86e5790c4c3ec", fallback="live")
        assert out == {"note": "leafB flat body"}

    def test_flat_store_hit_is_not_wrapped_as_sentinel(self, tmp_path, monkeypatch):
        import memchorus.hermes_home as hh
        leaf = tmp_path / "leafB"
        mem_dir = leaf / "memories"
        mem_dir.mkdir(parents=True)
        (mem_dir / "learning-6b65db6ba588431f.json").write_text('{"t": "body"}')
        monkeypatch.setattr(hh, "profile_home", lambda name=None: tmp_path / (name or "default"))
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": str(tmp_path / "cache")}
        )
        src._profile_name = "leafB"
        src._refetch_live = None
        out = src.retrieve("learning-6b65db6ba588431f", fallback="live")
        assert out == {"t": "body"}


class TestCandidateProfiles:
    @staticmethod
    def _fake_base(monkeypatch, base, on_disk):
        # _on_disk_profile_names does a *local* import of _base_home from
        # memchorus.hermes_home, so patch it on the defining module, not the
        # consumer (recall_cli) which has no such module attribute.
        import memchorus.hermes_home as hh
        for name in on_disk:
            (base / "profiles" / name).mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(hh, "_base_home", lambda: base)

    def test_explicit_profile_is_first_and_deduped(self, tmp_path, monkeypatch):
        import memchorus.recall_cli as rc
        self._fake_base(monkeypatch, tmp_path, ["cthugha", "bubo", "nyarlathotep"])
        cands = rc._candidate_profiles("cthugha")
        assert cands[0] == "cthugha"
        assert cands.count("cthugha") == 1
        assert "default" in cands

    def test_no_explicit_still_offers_default_first(self, tmp_path, monkeypatch):
        import memchorus.recall_cli as rc
        self._fake_base(monkeypatch, tmp_path, ["cthugha", "grok"])
        # No ambient active profile in this test — neutralise the real one
        # so the named candidates are base-level (None) then "default".
        monkeypatch.setattr(rc, "active_profile", lambda: None)
        cands = rc._candidate_profiles(None)
        # Base-level cache (None) is first (legacy location), then "default"
        assert cands[0] is None
        assert "default" in cands
        assert "cthugha" in cands
        assert "grok" in cands
        # "default" must come before any on-disk profile leaves
        assert cands.index("default") < cands.index("cthugha")

    def test_ambient_profile_prefers_it_over_default(self, tmp_path, monkeypatch):
        import memchorus.recall_cli as rc
        self._fake_base(monkeypatch, tmp_path, ["cthugha"])
        monkeypatch.setattr(rc, "active_profile", lambda: "cthugha")
        cands = rc._candidate_profiles(None)
        assert cands.index("cthugha") < cands.index("default")

    def test_no_duplicates_in_list(self, tmp_path, monkeypatch):
        import memchorus.recall_cli as rc
        self._fake_base(monkeypatch, tmp_path, ["a", "b", "c"])
        cands = rc._candidate_profiles("a")
        assert len(cands) == len(set(cands))


class TestCliCrossLeafResolution:
    def test_body_on_second_leaf_resolves_via_candidate_loop(self, tmp_path, monkeypatch):
        import memchorus.recall_cli as rc
        import memchorus.hermes_home as hh
        import memchorus.mempalace_memory_source as mms

        base = tmp_path / "base"
        primary = base / "profiles" / "primary"
        other = base / "profiles" / "other"
        (primary / "memories").mkdir(parents=True)
        (other / "memories").mkdir(parents=True)
        (other / "memories" / "decision-88e86e5790c4c3ec.json").write_text(
            '{"note": "cross-leaf body"}'
        )
        monkeypatch.setattr(hh, "profile_home", lambda name=None: base / "profiles" / (name or "default"))
        # _on_disk_profile_names does a local import of _base_home from
        # memchorus.hermes_home — patch it on the defining module, not the
        # consumer (recall_cli) which has no such module attribute.
        monkeypatch.setattr(hh, "_base_home", lambda: base)
        sources = []

        def _fake_cls(name, config):
            s = MemPalaceMemorySource(config={"skip_mcp": True, **config})
            s._refetch_live = None
            sources.append(config.get("profile_name"))
            return s

        # run_retrieve does a local import of MemPalaceMemorySource from
        # memchorus.mempalace_memory_source, so patch it on the defining
        # module (the recall_cli local var _bare_source_cls is not patchable).
        monkeypatch.setattr(mms, "MemPalaceMemorySource", _fake_cls)
        # run_retrieve also does `from memchorus import get_orchestrator`
        # locally; in the test env a real orchestrator exists, which would
        # take the orchestrator branch and skip the candidate loop entirely.
        # Neutralise it so the bare-source path is exercised.
        import memchorus as _mc
        monkeypatch.setattr(_mc, "get_orchestrator", lambda *a, **kw: None)
        cands = rc._candidate_profiles("primary")
        assert "other" in cands
        rc.run_retrieve("decision-88e86e5790c4c3ec", profile="primary", as_json=False)
        assert sources[0] == cands[0]
        assert "other" in sources
