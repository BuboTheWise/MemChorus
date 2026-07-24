"""GAP023: Orchestrator facade coverage for MemorySource ABC methods.

Verifies that delete(), get_source_info(), proactive_check() and
proactive_save() are now reachable through the orchestrator and that
they correctly delegate to registered sources while respecting enable/
disable gating and availability checks."""

import pytest
from memchorus.orchestrator import MemoryOrchestrator


@pytest.fixture
def orchestrator():
    """Fresh orchestrator with default sources for facade method tests."""
    return MemoryOrchestrator(config={})


class TestGAP023DeleteFacade:
    """Facade: delete() removes from all sources + purges cache."""

    def test_delete_returns_dict(self, orchestrator):
        result = orchestrator.delete("nonexistent_key")
        assert isinstance(result, dict)

    def test_delete_attempts_registered_sources(self, orchestrator):
        result = orchestrator.delete("no_such_key")
        # At least hermes_default should be in results
        assert len(result) > 0

    def test_delete_purges_retrieve_cache(self, orchestrator):
        test_key = "cache_purge_test_gap023"
        orchestrator.save(test_key, "cached_value")
        _ = orchestrator.retrieve(test_key)
        assert test_key in orchestrator._retrieve_cache
        orchestrator.delete(test_key)
        assert test_key not in orchestrator._retrieve_cache

    def test_delete_skips_disabled_source(self, orchestrator):
        orchestrator.disable_source("hermes_default")
        result = orchestrator.delete("no_such_key")
        assert "hermes_default" not in result
        orchestrator.enable_source("hermes_default")

    def test_delete_after_save_retrieves_none(self, orchestrator):
        test_key = "gap023_del_then_retrieve"
        assert orchestrator.save(test_key, "temp_value")
        assert orchestrator.retrieve(test_key) == "temp_value"
        orchestrator.delete(test_key)
        assert orchestrator.retrieve(test_key) is None


class TestGAP023GetSourceInfoFacade:
    """Facade: get_source_info() delegates to all/one source."""

    def test_get_all_returns_dict_of_dicts(self, orchestrator):
        info = orchestrator.get_source_info()
        assert isinstance(info, dict)
        for val in info.values():
            assert isinstance(val, dict)

    def test_get_all_includes_registered_sources(self, orchestrator):
        info = orchestrator.get_source_info()
        assert "hermes_default" in info

    def test_get_single_returns_dict(self, orchestrator):
        single = orchestrator.get_source_info(source_name="hermes_default")
        assert isinstance(single, dict)

    def test_get_single_nonexistent_returns_empty(self, orchestrator):
        empty = orchestrator.get_source_info(source_name="no_such_source")
        assert empty == {}


class TestGAP023ProactiveCheckFacade:
    """Facade: proactive_check() aggregates results from sources."""

    def test_proactive_check_returns_dict(self, orchestrator):
        result = orchestrator.proactive_check()
        assert isinstance(result, dict)

    def test_proactive_check_includes_available_sources(self, orchestrator):
        result = orchestrator.proactive_check(context={"test": True})
        # hermes_default should respond (may have empty findings)
        assert len(result) > 0

    def test_proactive_check_skips_disabled_source(self, orchestrator):
        orchestrator.disable_source("hermes_default")
        result = orchestrator.proactive_check()
        assert "hermes_default" not in result
        orchestrator.enable_source("hermes_default")

    def test_proactive_check_passes_context_through(self, orchestrator):
        ctx = {"agent_goal": "test_retrieval"}
        result = orchestrator.proactive_check(context=ctx)
        # Should not crash and should return a dict keyed by source
        assert isinstance(result, dict)


class TestGAP023ProactiveSaveFacade:
    """Facade: proactive_save() delegates to all sources."""

    def test_proactive_save_returns_dict(self, orchestrator):
        result = orchestrator.proactive_save("test_key", "test_value")
        assert isinstance(result, dict)

    def test_proactive_save_includes_available_sources(self, orchestrator):
        result = orchestrator.proactive_save("gap023_proact", "value")
        assert len(result) > 0

    def test_proactive_save_skips_disabled_source(self, orchestrator):
        orchestrator.disable_source("hermes_default")
        result = orchestrator.proactive_save("key", "val")
        assert "hermes_default" not in result
        orchestrator.enable_source("hermes_default")

    def test_proactive_save_with_context(self, orchestrator):
        ctx = {"trigger": "user_decision"}
        result = orchestrator.proactive_save("ctx_key", "ctx_val", context=ctx)
        assert isinstance(result, dict)
        assert len(result) > 0


class TestGAP023AllMethodsCallable:
    """Ensure all four new facades exist on the orchestrator instance."""

    def test_delete_method_exists(self, orchestrator):
        assert callable(getattr(orchestrator, "delete", None))

    def test_get_source_info_method_exists(self, orchestrator):
        assert callable(getattr(orchestrator, "get_source_info", None))

    def test_proactive_check_method_exists(self, orchestrator):
        assert callable(getattr(orchestrator, "proactive_check", None))

    def test_proactive_save_method_exists(self, orchestrator):
        assert callable(getattr(orchestrator, "proactive_save", None))
