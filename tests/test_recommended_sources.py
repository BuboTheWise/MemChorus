"""Tests for MemoryOrchestrator.recommended_sources().

This method is greenfield (B-2 bug fix t_b9205369). It should return a ranked list of source
names suitable for saving a given write_type, honouring:

  AC1  storage_enabled gating — disabled sources never appear
  AC2  priority tiering — higher priority_tier sources come first
  AC3  write_restrictions — sources that refuse the requested write_type are excluded
"""

import os
import sys
import unittest
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.orchestrator import MemoryOrchestrator


# -----------------------------------------------------------------------
# Mock source implementing the MemorySource ABC
# -----------------------------------------------------------------------

@dataclass
class _MockMemorySource:
    """Minimal but complete MemorySource implementation."""

    name: str = "mock"
    is_available_flag: bool = True
    src_config: Dict[str, Any] = field(default_factory=dict)
    _saved: List[Any] = field(default_factory=list, compare=False)

    @property
    def config(self) -> Dict[str, Any]:
        return self.src_config.copy()

    def save(self, key: str, value: Any) -> bool:
        self._saved.append((key, value))
        return True

    def retrieve(self, key: str) -> Optional[Any]:
        for k, v in self._saved:
            if k == key:
                return v
        return None

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        results = []
        for k, v in self._saved:
            if query.lower() in k.lower():
                results.append({"key": k, "content": v})
        return results[:limit]

    def is_available(self) -> bool:
        return self.is_available_flag

    def get_source_info(self) -> Dict[str, Any]:
        return {"name": self.name, "type": "mock"}

    def proactive_check(self, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {}

    def proactive_save(
        self, key: str, value: Any, context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        return True


def _build_orch(sources: List[_MockMemorySource]) -> MemoryOrchestrator:
    """Construct an orchestrator seeded with the given sources (bypasses real init)."""
    orch = MemoryOrchestrator(config={"enforce_on_read": False, "enforce_on_write": False})
    orch.memory_sources.clear()
    orch._source_enabled.clear()
    for _ in sources:
        pass
    for s in sources:
        orch.memory_sources[s.name] = s  # type: ignore[index]
        orch._source_enabled[s.name] = True
    return orch


# -----------------------------------------------------------------------
# AC1  Enabled gating
# -----------------------------------------------------------------------

class TestAC1StorageEnabled(unittest.TestCase):

    def test_all_enabled_returns_all(self) -> None:
        orch = _build_orch([
            _MockMemorySource(name="a"),
            _MockMemorySource(name="b"),
        ])
        result = orch.recommended_sources()
        self.assertEqual(sorted(result), ["a", "b"])

    def test_disabled_source_excluded(self) -> None:
        orch = _build_orch([
            _MockMemorySource(name="good"),
            _MockMemorySource(name="bad"),
        ])
        orch.disable_source("bad")
        self.assertNotIn("bad", orch.recommended_sources())
        self.assertEqual(orch.recommended_sources(), ["good"])

    def test_re_enable_restores(self) -> None:
        orch = _build_orch([
            _MockMemorySource(name="x"),
            _MockMemorySource(name="y"),
        ])
        orch.disable_source("y")
        self.assertNotIn("y", orch.recommended_sources())
        orch.enable_source("y")
        self.assertIn("y", orch.recommended_sources())

    def test_all_disabled_returns_empty(self) -> None:
        orch = _build_orch([
            _MockMemorySource(name="a"),
            _MockMemorySource(name="b"),
        ])
        orch.disable_source("a")
        orch.disable_source("b")
        self.assertEqual(orch.recommended_sources(), [])

    def test_disable_nonexistent_returns_false(self) -> None:
        orch = _build_orch([_MockMemorySource(name="only")])
        self.assertFalse(orch.disable_source("ghost"))
        self.assertEqual(orch.recommended_sources(), ["only"])


# -----------------------------------------------------------------------
# AC2  Priority tiering
# -----------------------------------------------------------------------

class TestAC2PriorityTiering(unittest.TestCase):

    @staticmethod
    def _st(name: str, tier: int) -> _MockMemorySource:
        return _MockMemorySource(name=name, src_config={"priority_tier": tier})

    def test_descending_order(self) -> None:
        orch = _build_orch([
            self._st("low", 1),
            self._st("high", 10),
            self._st("mid", 5),
        ])
        result = orch.recommended_sources()
        self.assertEqual(result, ["high", "mid", "low"])

    def test_disabled_high_tier_does_not_block_lower(self) -> None:
        orch = _build_orch([self._st("top", 10), self._st("bottom", 1)])
        orch.disable_source("top")
        self.assertEqual(orch.recommended_sources(), ["bottom"])

    def test_zero_tier_default(self) -> None:
        """Sources without priority_tier key should default to tier 0."""
        orch = _build_orch([
            self._st("has_tier", 5),
            _MockMemorySource(name="no_tier"),
        ])
        result = orch.recommended_sources()
        self.assertEqual(result, ["has_tier", "no_tier"])

    def test_equal_tier_stable(self) -> None:
        """Equal-tier peers appear together; c (tier 10) comes first."""
        orch = _build_orch([
            self._st("a", 5), self._st("b", 5), self._st("c", 10),
        ])
        result = orch.recommended_sources()
        self.assertEqual(result[0], "c")
        self.assertIn("a", result[1:])


# -----------------------------------------------------------------------
# AC3  Write restrictions
# -----------------------------------------------------------------------

class TestAC3WriteRestrictions(unittest.TestCase):

    def test_excludes_non_matching_type(self) -> None:
        orch = _build_orch([_MockMemorySource(
            name="mem_only", src_config={"write_restrictions": ["memory"]}
        )])
        # memory IS in restrictions → source EXCLUDED for memory writes
        self.assertNotIn("mem_only", orch.recommended_sources(write_type="memory"))
        # decision is NOT restricted → source INCLUDED for decision writes
        self.assertIn("mem_only", orch.recommended_sources(write_type="decision"))

    def test_no_restriction_accepts_everything(self) -> None:
        orch = _build_orch([_MockMemorySource(name="open")])
        for wt in ["memory", "decision", "general", "graph"]:
            self.assertIn("open", orch.recommended_sources(write_type=wt))

    def test_empty_restriction_accepts_everything(self) -> None:
        orch = _build_orch([_MockMemorySource(
            name="e", src_config={"write_restrictions": []}
        )])
        self.assertIn("e", orch.recommended_sources(write_type="anything"))

    def test_multi_restriction(self) -> None:
        orch = _build_orch([_MockMemorySource(
            name="m", src_config={"write_restrictions": ["memory", "decision"]}
        )])
        # memory IS in restrictions → source EXCLUDED for memory writes
        self.assertNotIn("m", orch.recommended_sources(write_type="memory"))
        # decision IS in restrictions → source EXCLUDED for decision writes
        self.assertNotIn("m", orch.recommended_sources(write_type="decision"))
        # general is NOT restricted → source INCLUDED for general writes
        self.assertIn("m", orch.recommended_sources(write_type="general"))


# -----------------------------------------------------------------------
# Combined edge cases
# -----------------------------------------------------------------------

class TestEdgeCases(unittest.TestCase):

    def test_max_results_cap(self) -> None:
        orch = _build_orch([_MockMemorySource(name=f"s{i}") for i in range(10)])
        self.assertLessEqual(len(orch.recommended_sources(max_results=2)), 2)

    def test_no_sources_empty(self) -> None:
        self.assertEqual(_build_orch([]).recommended_sources(), [])

    def test_unavailable_excluded(self) -> None:
        orch = _build_orch([
            _MockMemorySource(name="alive", is_available_flag=True),
            _MockMemorySource(name="dead", is_available_flag=False),
        ])
        self.assertEqual(orch.recommended_sources(), ["alive"])

    def test_single_source(self) -> None:
        self.assertEqual(_build_orch([_MockMemorySource(name="solo")]).recommended_sources(), ["solo"])

    def test_combined_gating_priority_restriction(self) -> None:
        """All three acceptance criteria together."""
        orch = _build_orch([
            # high tier, refuses memory writes
            _MockMemorySource(
                name="hm", src_config={"priority_tier": 10, "write_restrictions": ["memory"]}
            ),
            # mid tier, accepts all
            _MockMemorySource(name="ma", src_config={"priority_tier": 5}),
            # low tier, refuses decision writes
            _MockMemorySource(
                name="ld", src_config={"priority_tier": 1, "write_restrictions": ["decision"]}
            ),
        ])
        # memory write: hm excluded (refuses), ma first (tier 5), ld second (tier 1)
        m = orch.recommended_sources(write_type="memory")
        self.assertNotIn("hm", m)
        self.assertIn("ma", m)
        self.assertIn("ld", m)
        self.assertEqual(m[0], "ma")

        # decision write: ld excluded (refuses), hm first (tier 10), ma second (tier 5)
        d = orch.recommended_sources(write_type="decision")
        self.assertIn("hm", d)
        self.assertIn("ma", d)
        self.assertNotIn("ld", d)

        # disable top source -> ma becomes first for decision
        orch.disable_source("hm")
        self.assertEqual(orch.recommended_sources(write_type="decision")[0], "ma")


if __name__ == "__main__":
    unittest.main(verbosity=2)
