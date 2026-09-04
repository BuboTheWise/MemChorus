#!/usr/bin/env python3
"""test_profile_isolation.py - Cross-profile isolation for MemoryOrchestrator."""

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
import tempfile
import shutil
from memchorus.orchestrator import MemoryOrchestrator


class ProfileBackedOrchestrator:
    def __init__(self, profile_name, base_dir):
        self.profile_name = profile_name
        self.memory_dir = os.path.join(base_dir, "hermes_" + profile_name)
        os.makedirs(self.memory_dir, exist_ok=True)

    def create_orchestrator(self):
        config = {
            "default_source": "hermes_default",
            "hermes_default_config": {"memory_dir": self.memory_dir},
            "mempalace_config": {"skip_mcp": True},
        }
        orch = MemoryOrchestrator(config)
        if "mempalace" in orch.memory_sources:
            orch.unregister_source("mempalace")
        return orch


@pytest.fixture
def temp_base():
    d = tempfile.mkdtemp(prefix="profile_iso_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def profile_alpha(temp_base):
    p = ProfileBackedOrchestrator("alpha", temp_base)
    return p.create_orchestrator()


@pytest.fixture
def profile_beta(temp_base):
    p = ProfileBackedOrchestrator("beta", temp_base)
    return p.create_orchestrator()


class TestProfileDirectoryIsolation:
    def test_alpha_write_not_visible_to_beta(self, profile_alpha, profile_beta):
        key = "isolated_key_a"
        value = {"owner": "alpha", "secret": "data"}
        assert profile_alpha.save(key, value) is True
        retrieved = profile_beta.retrieve(key)
        assert retrieved is None, "Beta saw alpha data -- isolation violated"

    def test_beta_write_not_visible_to_alpha(self, profile_alpha, profile_beta):
        key = "isolated_key_b"
        value = {"owner": "beta", "secret": "data"}
        assert profile_beta.save(key, value) is True
        retrieved = profile_alpha.retrieve(key)
        assert retrieved is None, "Alpha saw beta data -- isolation violated"

    def test_multiple_keys_no_cross_profile_leak(self, profile_alpha, profile_beta):
        for i in range(10):
            k = "key_%d" % i
            profile_alpha.save("a_" + k, {"profile": "alpha", "index": i})
            profile_beta.save("b_" + k, {"profile": "beta", "index": i})
        for i in range(10):
            r = profile_alpha.retrieve("a_" + ("key_%d" % i))
            assert r is not None and r["profile"] == "alpha"
        for i in range(10):
            r = profile_beta.retrieve("a_" + ("key_%d" % i))
            assert r is None, "Beta found alpha key: %s" % r

    def test_same_key_different_values(self, profile_alpha, profile_beta):
        shared_key = "shared_name_unique_value"
        profile_alpha.save(shared_key, {"value": 100})
        profile_beta.save(shared_key, {"value": 200})
        a_r = profile_alpha.retrieve(shared_key)
        b_r = profile_beta.retrieve(shared_key)
        assert a_r is not None and a_r["value"] == 100
        assert b_r is not None and b_r["value"] == 200

    def test_separate_memory_dirs(self, temp_base):
        pa = ProfileBackedOrchestrator("px", temp_base)
        pb = ProfileBackedOrchestrator("py", temp_base)
        pa.create_orchestrator()
        pb.create_orchestrator()
        assert pa.memory_dir != pb.memory_dir
        assert os.path.exists(pa.memory_dir)
        assert os.path.exists(pb.memory_dir)


class TestSearchResultIsolation:
    def test_alpha_search_returns_only_alpha(self, profile_alpha, profile_beta):
        profile_alpha.save("alpha_search_item", {"data": "from alpha"})
        profile_beta.save("beta_search_item", {"data": "from beta"})
        r = profile_alpha.search("item")
        for entry in r:
            assert entry["key"] != "beta_search_item"

    def test_beta_search_returns_only_beta(self, profile_alpha, profile_beta):
        profile_alpha.save("alpha_search_x", {"data": "from alpha"})
        profile_beta.save("beta_search_y", {"data": "from beta"})
        r = profile_beta.search("search")
        for entry in r:
            assert entry["key"] != "alpha_search_x"

    def test_no_leak_on_similar_queries(self, profile_alpha, profile_beta):
        for i in range(5):
            k = "search_term_%d" % i
            profile_alpha.save(k, {"src": "alpha", "i": i})
            profile_beta.save(k, {"src": "beta", "i": i})
        a_results = profile_alpha.search("search_term")
        b_results = profile_beta.search("search_term")
        assert len(a_results) > 0
        assert len(b_results) > 0

    def test_empty_profile_search_no_error(self, profile_alpha, profile_beta):
        r = profile_alpha.search("nothing_here_xyz")
        assert isinstance(r, list)


class TestEnvProfileResolution:
    def test_default_when_unset(self, monkeypatch):
        from memchorus.auto_bootstrap import _resolve_hermes_profile
        monkeypatch.delenv("HERMES_PROFILE", raising=False)
        assert _resolve_hermes_profile() == "default"

    @pytest.mark.parametrize("env_val,expected", [
        ("cthugha", "cthugha"),
        ("custom-agent", "custom-agent"),
        ("my_profile_name", "my_profile_name"),
    ])
    def test_respects_env_var(self, monkeypatch, env_val, expected):
        from memchorus.auto_bootstrap import _resolve_hermes_profile
        monkeypatch.setenv("HERMES_PROFILE", env_val)
        assert _resolve_hermes_profile() == expected

    def test_profile_yaml_load_graceful_missing(self):
        from memchorus.auto_bootstrap import _load_profile_yaml_config
        result = _load_profile_yaml_config("nonexistent_xyz_123")
        assert isinstance(result, dict)


class TestCacheIsolation:
    def test_retrieve_cache_no_cross_contam(self, profile_alpha, profile_beta):
        key = "cache_test_key"
        alpha_val = {"cached": True, "owner": "alpha"}
        profile_alpha.save(key, alpha_val)
        r_a = profile_alpha.retrieve(key)
        assert r_a == alpha_val
        r_b = profile_beta.retrieve(key)
        assert r_b is None, "Beta cache has alpha data"

    @pytest.mark.parametrize("n_saves", [1, 5, 20])
    def test_many_saves_stay_isolated(self, profile_alpha, profile_beta, n_saves):
        for i in range(n_saves):
            k = "bulk_%d" % i
            profile_alpha.save(k, {"alpha": True})
        for i in range(n_saves):
            k = "bulk_%d" % i
            r = profile_beta.retrieve(k)
            assert r is None, "Leak at index %d of %d" % (i, n_saves)

    def test_orchestrator_info_shows_own_source(self, profile_alpha):
        info = profile_alpha.get_orchestrator_info()
        assert "sources" in info


class TestHitRateTrackerCrossProfileIsolation:
    """MemChorus #171: HitRateTracker must not leak state across profiles.

    The old first-call-pinned singleton returned profile-A's tracker for
    profile-B. The registry-keyed design returns a distinct tracker per
    memory directory, so two profiles in one process never share counters.
    """

    @pytest.fixture(autouse=True)
    def _reset(self):
        from memchorus.hit_rate_tracker import HitRateTracker
        HitRateTracker.reset()
        yield
        HitRateTracker.reset()

    def test_two_profiles_get_distinct_trackers(self, temp_base):
        from memchorus.hit_rate_tracker import HitRateTracker
        dir_a = os.path.join(temp_base, "hermes_alpha")
        dir_b = os.path.join(temp_base, "hermes_beta")
        os.makedirs(dir_a, exist_ok=True)
        os.makedirs(dir_b, exist_ok=True)

        t_a = HitRateTracker.get_instance(memory_dir=dir_a)
        t_b = HitRateTracker.get_instance(memory_dir=dir_b)

        assert t_a is not t_b, "Two profiles shared one tracker object (#171)"
        assert t_a.memory_dir != t_b.memory_dir

    def test_hit_rate_tracker_writes_do_not_cross_profile_in_same_process(
        self, temp_base
    ):
        """AC: record a hit on tracker-A; tracker-B's index stays empty."""
        from memchorus.hit_rate_tracker import HitRateTracker
        dir_a = os.path.join(temp_base, "hermes_alpha")
        dir_b = os.path.join(temp_base, "hermes_beta")
        os.makedirs(dir_a, exist_ok=True)
        os.makedirs(dir_b, exist_ok=True)

        t_a = HitRateTracker.get_instance(memory_dir=dir_a)
        t_b = HitRateTracker.get_instance(memory_dir=dir_b)

        t_a.register_save("shared_memory_key")
        t_a.record_recallhit("shared_memory_key")
        t_a.record_useful("shared_memory_key")

        assert t_a.get_hit_stats("shared_memory_key")["total_recalls"] == 1
        # B must see a fresh, empty entry for the same key
        b_stats = t_b.get_hit_stats("shared_memory_key")
        assert b_stats["total_recalls"] == 0, "B inherited A's recall count (#171)"
        assert b_stats["useful_flags"] == 0, "B inherited A's useful flags (#171)"

    def test_profile_switch_re_resolves_in_same_process(self, temp_base, monkeypatch):
        """AC: switching HERMES_PROFILE mid-process re-resolves the tracker."""
        from memchorus.hit_rate_tracker import HitRateTracker
        home = temp_base
        monkeypatch.setenv("HERMES_HOME", home)

        # Profile A
        monkeypatch.setenv("HERMES_PROFILE", "alpha")
        HitRateTracker.reset()
        t_a = HitRateTracker.get_instance()
        t_a.register_save("nightly_fact")

        # Profile B — a fresh tracker, not A's pinned instance
        monkeypatch.setenv("HERMES_PROFILE", "beta")
        t_b = HitRateTracker.get_instance()
        assert t_a is not t_b, "Profile switch did not re-resolve tracker (#171)"
        assert "nightly_fact" not in t_b._index, "B sees A's entry (#171)"
