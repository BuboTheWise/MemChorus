"""Test suite for lifecycle_merge.py -- MergeEngine strategies and edge cases."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

src = Path(__file__).resolve().parent.parent
if str(src) not in sys.path:
    sys.path.insert(0, str(src))

import pytest
from memchorus.lifecycle_merge import (
    AuditAction, MergeEngine, _jaccard_similarity,
    _strategy_append, _strategy_overwrite, _strategy_union, create_merge_engine,
)


class TestTokenHelpers:
    def test_jaccard_identical(self):
        assert _jaccard_similarity("hello world", "hello world") == 1.0

    def test_jaccard_disjoint(self):
        assert _jaccard_similarity("hello world", "foo bar baz") < 0.01

    def test_jaccard_partial(self):
        assert _jaccard_similarity("hello world", "world foo") == pytest.approx(1/3, abs=1e-9)

    def test_jaccard_dicts(self):
        a = {"a": 1, "b": 2, "c": 3}
        b = {"b": 2, "c": 3, "d": 4}
        assert _jaccard_similarity(a, b) == pytest.approx(0.5, abs=1e-9)

    def test_jaccard_empty(self):
        assert _jaccard_similarity({}, {}) == 1.0

    def test_jaccard_case_insensitive(self):
        assert _jaccard_similarity("Hello WORLD", "hello world") == 1.0


class TestStandaloneStrategies:
    def test_overwrite(self):
        result, action = _strategy_overwrite("old", "new")
        assert result == "new" and action == AuditAction.MERGE_OVERWRITE

    def test_append_list(self):
        result, _ = _strategy_append(["v1","v2"], "v3")
        assert result == ["v1","v2","v3"]

    def test_append_scalar(self):
        result, _ = _strategy_append("solo", "extra")
        assert result == ["solo", "extra"]

    def test_union_dicts(self):
        result, _ = _strategy_union({"a": 1}, {"b": 2, "a": 99})
        assert result == {"a": 99, "b": 2}

    def test_union_fallback(self):
        result, action = _strategy_union("text", 42)
        assert result == 42 and action == AuditAction.MERGE_UNION


class TestInit:
    @staticmethod
    def _orch():
        o = MagicMock(); o.memory_sources = {}; return o

    def test_defaults(self):
        me = MergeEngine(self._orch(), {"merge_at_write": {"enabled": True}})
        assert me._enabled and me._strategy == "overwrite"
        assert me._similarity_min == 0.75 and me._cluster_max == 3

    def test_custom_strategy(self):
        me = MergeEngine(self._orch(), {"merge_at_write": {"enabled": True, "strategy": "union"}})
        assert me._strategy == "union"

    def test_disabled(self):
        me = MergeEngine(self._orch(), {"merge_at_write": {"enabled": False}})
        assert not me._enabled

    def test_custom_thresholds(self):
        me = MergeEngine(self._orch(), {
            "merge_at_write": {"enabled": True},
            "eviction": {"similarity_min": 0.5, "duplicate_cluster_max": 5}
        })
        assert me._similarity_min == 0.5 and me._cluster_max == 5

    def test_strategies_list(self):
        assert set(MergeEngine.STRATEGIES) == {"overwrite", "append", "union"}


# Two values sharing enough tokens to exceed the default 0.75 jaccard threshold:
# {alpha,beta,gamma,delta,epsilon,zeta,eta,theta,iota,kappa,lam} = 11 unique tokens
# intersection = 10, union = 12 -> jaccard = 0.833 > 0.75
_VAL_A = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda"
_VAL_B = "alpha beta gamma delta epsilon zeta eta theta iota kappa MU"


class TestPreSaveCheck:
    @staticmethod
    def _engine(enabled=True, strategy="overwrite"):
        return MergeEngine(MagicMock(memory_sources={}), {
            "merge_at_write": {"enabled": enabled, "strategy": strategy}
        })

    # --- passthrough paths ---

    def test_disabled_passthrough(self):
        me = self._engine(enabled=False)
        r = me.pre_save_check("k", "v")
        assert r.should_proceed and r.final_value == "v" and r.similar_found == 0

    def test_no_orchestrator_passthrough(self):
        me = MergeEngine(None, {"merge_at_write": {"enabled": True}})
        r = me.pre_save_check("k", "v")
        assert r.should_proceed and r.final_value == "v"

    def test_no_hits_passthrough(self):
        me = self._engine()
        src = MagicMock(); src.search.return_value = []
        me._orchestrator.memory_sources = {"s": src}
        r = me.pre_save_check("uk", "uv")
        assert r.should_proceed and r.merge_action == AuditAction.PASSTHROUGH_NEW_KEY

    def test_below_cluster_max_passthrough(self):
        me = self._engine(); me._cluster_max = 5
        src = MagicMock()
        src.search.return_value = [{"key": "k", "value": _VAL_A}] * 4
        me._orchestrator.memory_sources = {"s": src}
        r = me.pre_save_check("k", _VAL_B)
        assert r.should_proceed and r.merge_action == AuditAction.PASSTHROUGH_NEW_KEY

    # --- merge triggered (per strategy) ---

    def test_merge_overwrite(self):
        """Write path: cluster threshold met with overwrite -> existing replaced."""
        me = self._engine(strategy="overwrite"); me._cluster_max = 1
        src = MagicMock()
        src.search.return_value = [{"key": "k", "value": _VAL_A}]
        me._orchestrator.memory_sources = {"s": src}
        # _retrieve_existing calls orchestrator.retrieve first -- return the existing value there.
        me._orchestrator.retrieve.return_value = _VAL_A

        r = me.pre_save_check("k", _VAL_B)
        assert not r.should_proceed and r.final_value == _VAL_B
        assert r.merge_action == AuditAction.MERGE_OVERWRITE

    def test_merge_append(self):
        """Append strategy combines existing and new values into a list."""
        me = self._engine(strategy="append"); me._cluster_max = 1
        src = MagicMock()
        src.search.return_value = [{"key": "k", "value": _VAL_A}]
        me._orchestrator.memory_sources = {"s": src}
        me._orchestrator.retrieve.return_value = _VAL_A

        r = me.pre_save_check("k", _VAL_B)
        assert not r.should_proceed and r.final_value == [_VAL_A, _VAL_B]
        assert r.merge_action == AuditAction.MERGE_APPEND

    def test_merge_union(self):
        """Union strategy merges dicts (new overrides common keys)."""
        me = self._engine(strategy="union"); me._cluster_max = 1
        existing = {"a": 1, "b": 2}
        # Mock _find_similar to return high-similarity hits with dict values
        me._find_similar = MagicMock(return_value=[{"key": "k", "similarity": 0.9, "existing_value": existing}])
        # _retrieve_existing returns `orchestrator.retrieve(key)` -- set it to the dict so union actually merges dicts
        me._orchestrator.retrieve.return_value = existing

        r = me.pre_save_check("k", {"b": 99, "c": 3})
        assert not r.should_proceed and r.final_value == {"a": 1, "b": 99, "c": 3}
        assert r.merge_action == AuditAction.MERGE_UNION

    # --- profile overrides strategy resolution ---

    def test_profile_overrides_strategy(self):
        """PROFILE_STRATEGY_MAP overrides global default when set."""
        me = self._engine(strategy="overwrite"); me._cluster_max = 2
        # Bypass similarity computation -- the point is profile -> strategy resolution.
        me._find_similar = MagicMock(return_value=[
            {"key": "k", "similarity": 0.9, "existing_value": {}}] * 2)
        me._orchestrator.retrieve.return_value = {}

        class MP:
            value = "relationship_graph"   # maps to 'union' in PROFILE_STRATEGY_MAP
        r = me.pre_save_check("k", {"x": 1}, profile=MP())
        assert not r.should_proceed and r.merge_action == AuditAction.MERGE_UNION

    def test_safety_net_degradation(self):
        """When merge strategy function raises, result falls back to passthrough."""
        me = self._engine(strategy="overwrite"); me._cluster_max = 1
        src = MagicMock()
        src.search.return_value = [{"key": "k", "value": _VAL_A}]
        me._orchestrator.memory_sources = {"s": src}
        # Make retrieve succeed so we get past _retrieve_existing, then make the strategy dispatch blow up
        me._orchestrator.retrieve.return_value = _VAL_A
        me._apply_strategy = MagicMock(side_effect=Exception("boom"))
        r = me.pre_save_check("k", _VAL_B)
        assert r.merge_action == AuditAction.DEGRADE_SAFETY_NET


class TestAudit:
    def test_initially_empty(self):
        me = MergeEngine(MagicMock(memory_sources={}), {"merge_at_write": {"enabled": True}})
        assert me.get_audit_trail() == []

    def test_entry_recorded(self):
        me = MergeEngine(MagicMock(memory_sources={}), {"merge_at_write": {"enabled": True}})
        me.pre_save_check("k", "v")
        trail = me.get_audit_trail()
        assert len(trail) >= 1 and trail[0]["key"] == "k"


class TestFactory:
    def test_creates_when_enabled(self):
        e = create_merge_engine(MagicMock(), {"merge_at_write": {"enabled": True, "strategy": "append"}})
        assert e is not None and e._enabled and e._strategy == "append"

    def test_none_when_disabled(self):
        assert create_merge_engine(MagicMock(), {"merge_at_write": {"enabled": False}}) is None


class TestOrchestratorIntegration:
    """Smoke-test the orchestrator actually wires MergeEngine.pre_save_check on saves."""

    def test_orchestrator_has_merge_engine_when_configured(self):
        from memchorus.orchestrator import MemoryOrchestrator
        orch = MemoryOrchestrator(config={
            "lifecycle_config": {
                "enabled": True,
                "merge_at_write": {"enabled": True},
            }
        })
        assert getattr(orch, "_merge_engine", None) is not None

    def test_orchestrator_merge_result_used_on_save(self):
        """When merge engine blocks a save, orchestrator uses final_value."""
        from memchorus.orchestrator import MemoryOrchestrator
        orch = MemoryOrchestrator(config={
            "enforce_on_read": False,   # avoid enforcement noise
            "enforce_on_write": False,
            "lifecycle_config": {
                "enabled": True,
                "merge_at_write": {"enabled": True, "strategy": "overwrite"},
            }
        })
        mock_result = MagicMock()
        mock_result.should_proceed = False
        mock_result.final_value = "_merged_"
        # Make pre_save_check return the mock result (callable returning it)
        orch._merge_engine.pre_save_check = lambda *a, **k: mock_result

        saved_keys = {}
        def fake_save(k, v):
            saved_keys[k] = v; return True
        for src in orch.memory_sources.values():
            src.save = fake_save

        orch.save("test_merge_key", "original_value")
        assert saved_keys.get("test_merge_key") == "_merged_"