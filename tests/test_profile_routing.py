#!/usr/bin/env python3
"""
test_profile_routing.py - C-4: Profile-aware routing in Orchestrator.save()

Verifies that MemoryProfile-based smart placement actually works end-to-end:
1. Explicit profile parameter routes to the correct source(s) per _PROFILE_SOURCE_HINT
2. When no profile given, _infer_profile(value) classifies content and routes accordingly
3. Content is NOT written to sources outside the profile's preference list (dedup avoidance)
4. Explicit source_name still overrides everything

Scope: orchestrator.py save() + _infer_profile() only.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.orchestrator import (
    MemoryOrchestrator,
    MemoryProfile,
    _PROFILE_SOURCE_HINT,
)
from memchorus.hermes_memory_source import HermesDefaultMemorySource


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

def _make_orch(hermes_dir: str, **extra_config):
    """Create a pristine orchestrator pointing at a temporary hermes directory."""
    config = {
        "default_source": "hermes_default",
        "hermes_default_config": {"memory_dir": hermes_dir},
        "mempalace_config": {"skip_mcp": True},  # avoid live MCP in tests
        "enforce_on_read": False,   # routing tests don't exercise enforcement
        "enforce_on_write": False,  # prevent post-save capture calls polluting save counts
    }
    config["mempalace_config"].update(extra_config.pop("mempalace_config", {}))
    config.update(extra_config)
    return MemoryOrchestrator(config)


class _MockSource:
    """Minimal MemorySource stub that records every save call."""

    def __init__(self, name: str, available: bool = True):
        self.name = name
        self._available = available
        self._saves: list = []

    def save(self, key, value):
        self._saves.append((key, value))
        return True

    def retrieve(self, key):
        for k, v in self._saves:
            if k == key:
                return v
        return None

    def search(self, query, limit=10):
        return []

    def is_available(self):
        return self._available

    def get_source_info(self):
        return {"name": self.name, "type": "mock", "available": self._available}


class TestExplicitProfileRouting(unittest.TestCase):
    """When the caller passes an explicit MemoryProfile, save() honours it."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="memchorus_profile_")
        self.hermes_dir = os.path.join(self.tmpdir, "hermes_mem")
        self.mempalace_dir = os.path.join(self.tmpdir, "mempalace_cache")
        os.makedirs(self.hermes_dir, exist_ok=True)
        os.makedirs(self.mempalace_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- 1. EPHEMERAL profile (hermes_default, mempalace) ------------------
    def test_ephemeral_routes_to_hermes_first(self):
        """EPHEMERAL hints: [hermes_default, mempalace]. First match wins."""
        orch = _make_orch(self.hermes_dir)
        mock_hd = _MockSource("hermes_default")
        mock_mp = _MockSource("mempalace")
        orch.memory_sources["hermes_default"] = mock_hd
        orch.memory_sources["mempalace"] = mock_mp

        orch.save("e1", "short value", profile=MemoryProfile.EPHEMERAL)

        # hermes_default is first in the hint list — should get the save and stop
        assert len(mock_hd._saves) == 1, "hermes_default should receive the save"
        assert mock_hd._saves[0][0] == "e1"
        # Should NOT have reached mempalace (first target matched)
        assert len(mock_mp._saves) == 0, \
            f"Save should stop after first successful target; mempalace got {mock_mp._saves}"

    # -- 2. LONG_LIVED_KNOWLEDGE profile → mempalace only -------------------
    def test_long_lived_knowledge_routes_to_mempalace(self):
        """LONG_LIVED_KNOWLEDGE hints: [mempalace]."""
        orch = _make_orch(self.hermes_dir)
        mock_hd = _MockSource("hermes_default")
        mock_mp = _MockSource("mempalace")
        orch.memory_sources["hermes_default"] = mock_hd
        orch.memory_sources["mempalace"] = mock_mp

        orch.save("lk1", {"important": "knowledge"}, profile=MemoryProfile.LONG_LIVED_KNOWLEDGE)

        assert len(mock_mp._saves) == 1, "mempalace should receive long-lived knowledge"
        assert len(mock_hd._saves) == 0, \
            f"hermes_default should NOT get LONG_LIVED_KNOWLEDGE; got {mock_hd._saves}"

    # -- 3. RELATIONSHIP_GRAPH → mempalace only ----------------------------
    def test_relationship_graph_routes_to_mempalace(self):
        """RELATIONSHIP_GRAPH hints: [mempalace]."""
        orch = _make_orch(self.hermes_dir)
        mock_hd = _MockSource("hermes_default")
        mock_mp = _MockSource("mempalace")
        orch.memory_sources["hermes_default"] = mock_hd
        orch.memory_sources["mempalace"] = mock_mp

        edges = [["A", "B"], ["B", "C"]]
        orch.save("g1", edges, profile=MemoryProfile.RELATIONSHIP_GRAPH)

        assert len(mock_mp._saves) == 1, "Graph data should go to mempalace"
        assert len(mock_hd._saves) == 0, \
            f"hermes_default should NOT get RELATIONSHIP_GRAPH; got {mock_hd._saves}"

    # -- 4. USER_PREFERENCE → hermes_default only --------------------------
    def test_user_preference_routes_to_hermes(self):
        """USER_PREFERENCE hints: [hermes_default]."""
        orch = _make_orch(self.hermes_dir)
        mock_hd = _MockSource("hermes_default")
        mock_mp = _MockSource("mempalace")
        orch.memory_sources["hermes_default"] = mock_hd
        orch.memory_sources["mempalace"] = mock_mp

        orch.save("up1", {"lang": "en"}, profile=MemoryProfile.USER_PREFERENCE)

        assert len(mock_hd._saves) == 1, "Preference should go to hermes_default"
        assert len(mock_mp._saves) == 0, \
            f"mempalace should NOT get USER_PREFERENCE; got {mock_mp._saves}"

    # -- 5. CONTEXT_SENSITIVE_PREF → hermes_default ------------------------
    def test_context_sensitive_pref_routes_to_hermes(self):
        """CONTEXT_SENSITIVE_PREF hints: [hermes_default]."""
        orch = _make_orch(self.hermes_dir)
        mock_hd = _MockSource("hermes_default")
        mock_mp = _MockSource("mempalace")
        orch.memory_sources["hermes_default"] = mock_hd
        orch.memory_sources["mempalace"] = mock_mp

        orch.save("cs1", "context_note", profile=MemoryProfile.CONTEXT_SENSITIVE_PREF)

        assert len(mock_hd._saves) == 1
        assert len(mock_mp._saves) == 0


class TestAutoInferRouting(unittest.TestCase):
    """When no profile given, _infer_profile(value) classifies and routes."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="memchorus_infer_")
        self.hermes_dir = os.path.join(self.tmpdir, "hermes_mem")
        self.mempalace_dir = os.path.join(self.tmpdir, "mempalace_cache")
        os.makedirs(self.hermes_dir, exist_ok=True)
        os.makedirs(self.mempalace_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _mock_orch(self):
        orch = _make_orch(self.hermes_dir)
        mock_hd = _MockSource("hermes_default")
        mock_mp = _MockSource("mempalace")
        orch.memory_sources["hermes_default"] = mock_hd
        orch.memory_sources["mempalace"] = mock_mp
        return orch, mock_hd, mock_mp

    # -- 1. Short string → EPHEMERAL → hermes_default (first in hint list) ---
    def test_short_string_infers_ephemeral(self):
        orch, mock_hd, mock_mp = self._mock_orch()
        orch.save("s1", "Hello world")  # no profile → infer
        assert len(mock_hd._saves) == 1, "Short string (EPHEMERAL) → hermes_default first"

    # -- 2. Dict → USER_PREFERENCE → hermes_default only --------------------
    def test_dict_infers_user_preference(self):
        orch, mock_hd, mock_mp = self._mock_orch()
        orch.save("d1", {"theme": "dark"})
        assert len(mock_hd._saves) == 1, "Dict (USER_PREFERENCE) → hermes_default"
        assert len(mock_mp._saves) == 0, \
            f"mempalace should NOT get USER_PREFERENCE; got {mock_mp._saves}"

    # -- 3. Large string (>4500 bytes) → LARGE_DATA_BLOCK --------------------
    def test_large_string_infers_large_data(self):
        orch, mock_hd, mock_mp = self._mock_orch()
        big = "x" * 5000
        orch.save("big", big)
        # LARGE_DATA_BLOCK hints: [hermes_default, mempalace] → hermes first
        assert len(mock_hd._saves) == 1

    # -- 4. Large dict (>1000 keys) → LARGE_DATA_BLOCK ----------------------
    def test_large_dict_infers_large_data(self):
        orch, mock_hd, mock_mp = self._mock_orch()
        big = {str(i): i for i in range(2000)}
        orch.save("big", big)
        assert len(mock_hd._saves) == 1

    # -- 5. Edge list → RELATIONSHIP_GRAPH → mempalace ----------------------
    def test_edge_list_infers_relationship_graph(self):
        orch, mock_hd, mock_mp = self._mock_orch()
        edges = [("A", "loves"), ("B", "hates"), ("C", "knows")]
        orch.save("g1", edges)
        assert len(mock_mp._saves) == 1, "Edge list (RELATIONSHIP_GRAPH) → mempalace"
        assert len(mock_hd._saves) == 0, \
            f"hermes_default should NOT get RELATIONSHIP_GRAPH; got {mock_hd._saves}"


class TestExplicitSourceOverride(unittest.TestCase):
    """source_name always takes highest precedence, bypassing profile logic."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="memchorus_override_")
        self.hermes_dir = os.path.join(self.tmpdir, "hermes_mem")
        os.makedirs(self.hermes_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_source_name_bypasses_profile(self):
        """Even with USER_PREFERENCE profile, source_name forces the target."""
        orch = _make_orch(self.hermes_dir)
        mock_hd = _MockSource("hermes_default")
        mock_mp = _MockSource("mempalace")
        orch.memory_sources["hermes_default"] = mock_hd
        orch.memory_sources["mempalace"] = mock_mp

        orch.save(
            "x1", {"theme": "dark"},
            profile=MemoryProfile.USER_PREFERENCE,  # normally → hermes only
            source_name="mempalace",                  # but explicit source wins
        )
        assert len(mock_mp._saves) == 1, "source_name override should force mempalace"
        assert len(mock_hd._saves) == 0

    def test_invalid_source_name_returns_false(self):
        orch = _make_orch(self.hermes_dir)
        assert orch.save("k", "v", source_name="nonexistent") is False


class TestSafetyNetFallback(unittest.TestCase):
    """When preferred target is unavailable, fall back to any available source."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="memchorus_fallback_")
        self.hermes_dir = os.path.join(self.tmpdir, "hermes_mem")
        os.makedirs(self.hermes_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_mempalace_unavailable_falls_back_to_hermes(self):
        """LONG_LIVED_KNOWLEDGE prefers mempalace; if down, safety-net → hermes."""
        orch = _make_orch(self.hermes_dir)
        mock_hd = _MockSource("hermes_default", available=True)
        mock_mp = _MockSource("mempalace", available=False)  # DOWN
        orch.memory_sources["hermes_default"] = mock_hd
        orch.memory_sources["mempalace"] = mock_mp

        orch.save("lk1", {"important": "knowledge"}, profile=MemoryProfile.LONG_LIVED_KNOWLEDGE)

        # Preferred target (mempalace) unavailable → safety net tries all sources → hermes succeeds
        assert len(mock_hd._saves) == 1, "Fallback should write to hermes_default"

    def test_all_sources_down_returns_false(self):
        orch = _make_orch(self.hermes_dir)
        mock_hd = _MockSource("hermes_default", available=False)
        mock_mp = _MockSource("mempalace", available=False)
        orch.memory_sources["hermes_default"] = mock_hd
        orch.memory_sources["mempalace"] = mock_mp

        result = orch.save("x", "v")
        assert result is False


class TestInferProfileHeuristics(unittest.TestCase):
    """Unit tests for _infer_profile classification logic alone."""

    def test_plain_string_ephemeral(self):
        p = MemoryOrchestrator._infer_profile(None, "just a note")
        assert p == MemoryProfile.EPHEMERAL

    def test_dict_user_preference(self):
        p = MemoryOrchestrator._infer_profile(None, {"key": "value"})
        assert p == MemoryProfile.USER_PREFERENCE

    def test_large_string_data_block(self):
        p = MemoryOrchestrator._infer_profile(None, "A" * 5000)
        assert p == MemoryProfile.LARGE_DATA_BLOCK

    def test_large_dict_data_block(self):
        p = MemoryOrchestrator._infer_profile(None, {str(i): i for i in range(1500)})
        assert p == MemoryProfile.LARGE_DATA_BLOCK

    def test_edge_tuples_relationship_graph(self):
        edges = [("subj", "pred"), ("x", "y")]
        p = MemoryOrchestrator._infer_profile(None, edges)
        assert p == MemoryProfile.RELATIONSHIP_GRAPH

    def test_edge_lists_relationship_graph(self):
        edges = [["alpha", "beta"], ["gamma", "delta"]]
        p = MemoryOrchestrator._infer_profile(None, edges)
        assert p == MemoryProfile.RELATIONSHIP_GRAPH

    def test_plain_list_ephemeral(self):
        """A list without 2-element items falls through to EPHEMERAL."""
        p = MemoryOrchestrator._infer_profile(None, ["a", "b", "c"])
        assert p == MemoryProfile.EPHEMERAL


if __name__ == "__main__":
    unittest.main(verbosity=2)
