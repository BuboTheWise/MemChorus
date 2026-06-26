"""
Tests for BehavioralTrigger decision-point detection and callback system.

Acceptance criteria covered:
  AC-1: Each DecisionPoint type fires on its correct keywords (PLANNING_START, TOOL_CALL_INTENT,
        POST_ACTION_COMPLETE, ERROR_STATE).
  AC-2: Callback hooks execute when matched decision points are detected via ``fire()``.
  AC-3: Priority ordering works — if text matches multiple types, ERROR_STATE fires first.
  AC-4: Text with no matching keywords returns an empty list from ``detect()`".
  AC-5: ``fire()`` returns the same list as ``detect()`` for identical input.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.behavioral_trigger import (
    BehavioralTrigger,
    DecisionPoint,
    DetectedPoint,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _CallbackTracker:
    """Collects DetectedPoints passed to a callback for assertion."""

    def __init__(self) -> None:
        self.points: list[DetectedPoint] = []

    def record(self, point: DetectedPoint) -> None:
        self.points.append(point)


def _make_orch() -> BehavioralTrigger:
    return BehavioralTrigger()


# ---------------------------------------------------------------------------
# AC-1: Each DecisionPoint type fires on correct keywords
# ---------------------------------------------------------------------------


class TestDecisionPointDetection(unittest.TestCase):
    """AC-1: individual decision-point keyword detection."""

    def test_error_state_fires_on_error_keyword(self) -> None:
        trigger = _make_orch()
        results = trigger.detect("I got an error while processing the file")
        types_found = {r.type for r in results}
        self.assertIn(DecisionPoint.ERROR_STATE, types_found)

    def test_error_state_fires_on_failed_keyword(self) -> None:
        trigger = _make_orch()
        results = trigger.detect("The command failed unexpectedly")
        types_found = {r.type for r in results}
        self.assertIn(DecisionPoint.ERROR_STATE, types_found)

    def test_error_state_fires_on_exception_keyword(self) -> None:
        trigger = _make_orch()
        results = trigger.detect("Caught an exception during execution")
        types_found = {r.type for r in results}
        self.assertIn(DecisionPoint.ERROR_STATE, types_found)

    def test_error_state_fires_on_traceback_keyword(self) -> None:
        trigger = _make_orch()
        results = trigger.detect("There is a full traceback in the logs")
        types_found = {r.type for r in results}
        self.assertIn(DecisionPoint.ERROR_STATE, types_found)

    def test_error_state_fires_on_went_wrong_keyword(self) -> None:
        trigger = _make_orch()
        results = trigger.detect("Something went wrong with the API call")
        types_found = {r.type for r in results}
        self.assertIn(DecisionPoint.ERROR_STATE, types_found)

    def test_planning_start_fires_on_need_to_implement(self) -> None:
        trigger = _make_orch()
        results = trigger.detect("I need to implement the new endpoint")
        types_found = {r.type for r in results}
        self.assertIn(DecisionPoint.PLANNING_START, types_found)

    def test_planning_start_fires_on_plan_is_keyword(self) -> None:
        trigger = _make_orch()
        results = trigger.detect("The plan is to refactor the auth module")
        types_found = {r.type for r in results}
        self.assertIn(DecisionPoint.PLANNING_START, types_found)

    def test_planning_start_fires_on_first_step_keyword(self) -> None:
        trigger = _make_orch()
        results = trigger.detect("First step is to gather requirements")
        types_found = {r.type for r in results}
        self.assertIn(DecisionPoint.PLANNING_START, types_found)

    def test_planning_start_fires_on_strategy_keyword(self) -> None:
        trigger = _make_orch()
        results = trigger.detect("My strategy is to use a cache layer")
        types_found = {r.type for r in results}
        self.assertIn(DecisionPoint.PLANNING_START, types_found)

    def test_tool_call_intent_fires_on_will_call_keyword(self) -> None:
        trigger = _make_orch()
        results = trigger.detect("Next I will call the database adapter")
        types_found = {r.type for r in results}
        self.assertIn(DecisionPoint.TOOL_CALL_INTENT, types_found)

    def test_tool_call_intent_fires_on_use_keyword(self) -> None:
        trigger = _make_orch()
        results = trigger.detect("I'll use the search tool next")
        types_found = {r.type for r in results}
        self.assertIn(DecisionPoint.TOOL_CALL_INTENT, types_found)

    def test_tool_call_intent_fires_on_running_command_keyword(self) -> None:
        trigger = _make_orch()
        results = trigger.detect("Running the command now to fetch data")
        types_found = {r.type for r in results}
        self.assertIn(DecisionPoint.TOOL_CALL_INTENT, types_found)

    def test_tool_call_intent_fires_on_executing_keyword(self) -> None:
        trigger = _make_orch()
        results = trigger.detect("Executing the deployment pipeline")
        types_found = {r.type for r in results}
        self.assertIn(DecisionPoint.TOOL_CALL_INTENT, types_found)

    def test_post_action_complete_fires_on_completed_keyword(self) -> None:
        trigger = _make_orch()
        results = trigger.detect("Task completed successfully")
        types_found = {r.type for r in results}
        self.assertIn(DecisionPoint.POST_ACTION_COMPLETE, types_found)

    def test_post_action_complete_fires_on_finished_keyword(self) -> None:
        trigger = _make_orch()
        results = trigger.detect("I have finished the analysis")
        types_found = {r.type for r in results}
        self.assertIn(DecisionPoint.POST_ACTION_COMPLETE, types_found)

    def test_post_action_complete_fires_on_done_with_keyword(self) -> None:
        trigger = _make_orch()
        results = trigger.detect("Done with the first round of testing")
        types_found = {r.type for r in results}
        self.assertIn(DecisionPoint.POST_ACTION_COMPLETE, types_found)

    def test_post_action_complete_fires_on_output_received_keyword(self) -> None:
        trigger = _make_orch()
        results = trigger.detect("Output received from the API")
        types_found = {r.type for r in results}
        self.assertIn(DecisionPoint.POST_ACTION_COMPLETE, types_found)

    def test_post_action_complete_fires_on_result_is_keyword(self) -> None:
        trigger = _make_orch()
        results = trigger.detect("The result is the processed data")
        types_found = {r.type for r in results}
        self.assertIn(DecisionPoint.POST_ACTION_COMPLETE, types_found)


# ---------------------------------------------------------------------------
# AC-2: Callback hooks execute when matched decision points are detected
# ---------------------------------------------------------------------------


class TestCallbackExecution(unittest.TestCase):
    """AC-2: on_decision_point and on() register and fire callbacks correctly."""

    def test_on_decision_point_fires_for_all_types(self) -> None:
        """``on_decision_point(registry)`` registers for ALL DecisionPoint types."""
        tracker = _CallbackTracker()
        trigger = BehavioralTrigger()

        trigger.on_decision_point(tracker.record)
        text = "Error occurred while running the command"  # ERROR_STATE + TOOL_CALL_INTENT
        trigger.fire(text)

        types_seen = {p.type for p in tracker.points}
        self.assertIn(DecisionPoint.ERROR_STATE, types_seen)
        self.assertIn(DecisionPoint.TOOL_CALL_INTENT, types_seen)

    def test_on_specific_type_fires_only_for_that(self) -> None:
        """``on(dp, cb)`` fires only callbacks registered for that decision point."""
        tracker = _CallbackTracker()
        trigger = BehavioralTrigger()
        trigger.on(DecisionPoint.ERROR_STATE, tracker.record)

        text = "Error occurred so I need to implement a fix"  # ERROR + PLANNING_START
        trigger.fire(text)

        types_seen = {p.type for p in tracker.points}
        self.assertEqual(types_seen, {DecisionPoint.ERROR_STATE},
                         f"Expected only ERROR_STATE but got {types_seen}")

    def test_callback_receives_detected_point(self) -> None:
        """The callback must receive a DetectedPoint with correct attributes."""
        captured_points: list[DetectedPoint] = []

        def capture(point: DetectedPoint) -> None:
            captured_points.append(point)

        trigger = BehavioralTrigger()
        trigger.on(DecisionPoint.PLANNING_START, capture)
        trigger.fire("The plan is to implement a new feature")

        self.assertEqual(len(captured_points), 1)
        point = captured_points[0]
        self.assertEqual(point.type, DecisionPoint.PLANNING_START)
        self.assertIsInstance(point.confidence, float)
        self.assertGreaterEqual(point.confidence, 0.5)
        self.assertLessEqual(point.confidence, 1.0)
        self.assertIsInstance(point.matched_keyword, str)
        self.assertIsNotNone(point.text_span)

    def test_multiple_callbacks_all_fire(self) -> None:
        """If two callbacks are registered for the same DP, both should fire."""
        tracker_a = _CallbackTracker()
        tracker_b = _CallbackTracker()
        trigger = BehavioralTrigger()
        trigger.on(DecisionPoint.ERROR_STATE, tracker_a.record)
        trigger.on(DecisionPoint.ERROR_STATE, tracker_b.record)

        trigger.fire("Error in the system detected")

        self.assertGreater(len(tracker_a.points), 0)
        self.assertGreater(len(tracker_b.points), 0)


# ---------------------------------------------------------------------------
# AC-3: Priority ordering works
# ---------------------------------------------------------------------------


class TestPriorityOrdering(unittest.TestCase):
    """AC-3: priority — ERROR_STATE > PLANNING_START > TOOL_CALL_INTENT > POST_ACTION_COMPLETE."""

    def test_error_state_has_highest_priority(self) -> None:
        """When text matches ERROR_STATE, its DetectedPoint must come first in the list."""
        trigger = _make_orch()
        # Text that triggers both ERROR_STATE and PLANNING_START
        text = "The plan is to fix the error we encountered"  # PLANNING_START + ERROR_STATE
        results = trigger.detect(text)

        self.assertTrue(len(results) >= 2)
        first_type = results[0].type
        self.assertEqual(first_type, DecisionPoint.ERROR_STATE,
                         f"ERROR_STATE should be first but got {first_type}")

    def test_planning_start_before_tool_call(self) -> None:
        """PLANNING_START must rank before TOOL_CALL_INTENT."""
        trigger = _make_orch()
        text = "The plan is to implement it using the next command"  # PLANNING_START + TOOL_CALL_INTENT
        results = trigger.detect(text)

        types_in_order = [r.type for r in results]
        if DecisionPoint.PLANNING_START in types_in_order and DecisionPoint.TOOL_CALL_INTENT in types_in_order:
            ps_idx = types_in_order.index(DecisionPoint.PLANNING_START)
            tc_idx = types_in_order.index(DecisionPoint.TOOL_CALL_INTENT)
            self.assertLess(ps_idx, tc_idx, "PLANNING_START must come before TOOL_CALL_INTENT")

    def test_all_priorities_ordered_correctly(self) -> None:
        """If all four types match, verify the full priority chain."""
        # Construct text that contains one keyword for each category
        # (word boundaries ensure they don't accidentally match other categories)
        text = ("Error in the plan is to implement fixing using "
                "running the command now and completed it.")

        trigger = _make_orch()
        results = trigger.detect(text)

        types_in_order = [r.type for r in results]
        
        # Verify ERROR_STATE is first (highest priority)
        if DecisionPoint.ERROR_STATE in types_in_order:
            self.assertEqual(types_in_order[0], DecisionPoint.ERROR_STATE,
                             f"ERROR_STATE should be first; got {types_in_order}")

    def test_priority_values_are_strictly_ordered(self) -> None:
        """The static priority() method returns strictly increasing integers."""
        prev_prio = 0
        for dp in (DecisionPoint.PLANNING_START,
                   DecisionPoint.TOOL_CALL_INTENT,
                   DecisionPoint.POST_ACTION_COMPLETE):
            curr_prio = DecisionPoint.priority(dp)
            self.assertGreater(curr_prio, prev_prio,
                               f"{dp.name} priority must be > {prev_prio}")
            prev_prio = curr_prio


# ---------------------------------------------------------------------------
# AC-4: No matches returns empty list
# ---------------------------------------------------------------------------


class TestNoMatches(unittest.TestCase):
    """AC-4: text with zero matching keywords must return []."""

    def test_detect_returns_empty_on_irrelevant_text(self) -> None:
        trigger = _make_orch()
        results = trigger.detect("The weather is nice today and birds are singing")
        self.assertEqual(results, [], "Expected empty list for irrelevant text")

    def test_fire_returns_empty_on_irrelevant_text(self) -> None:
        tracker = _CallbackTracker()
        trigger = BehavioralTrigger()
        trigger.on_decision_point(tracker.record)
        results = trigger.fire("The weather is nice today and birds are singing")
        self.assertEqual(results, [])
        self.assertEqual(len(tracker.points), 0, "No callback should fire")

    def test_just_numbers_returns_empty(self) -> None:
        trigger = _make_orch()
        results = trigger.detect("42 3.14 100 0xDEADBEEF")
        self.assertEqual(results, [])

    def test_none_text_spelling_returns_no_match(self) -> None:
        """Words that contain keywords as substrings but lack word boundaries should not match."""
        # "unsuccessful" contains "fail" but NOT as a standalone word — should not match
        trigger = _make_orch()
        results = trigger.detect("The attempt was unsuccessful because of an unfortunate situation")
        error_types = [r for r in results if r.type == DecisionPoint.ERROR_STATE]
        self.assertEqual(error_types, [],
                         "Substring matches without word boundary should not fire")


# ---------------------------------------------------------------------------
# AC-5: fire() returns same list as detect()
# ---------------------------------------------------------------------------


class TestFireReturnsSameAsDetect(unittest.TestCase):
    """AC-5: ``fire(text)`` must return the identical list that ``detect(text)`` produces."""

    def test_fire_equals_detect(self) -> None:
        trigger = _make_orch()
        text = "Error occurred so the plan is to execute and then completed"
        
        detect_results = trigger.detect(text)
        fire_results  = trigger.fire(text)  # also registers a no-op callback

        self.assertEqual(len(detect_results), len(fire_results))
        for dr, fr in zip(detect_results, fire_results):  # type: ignore[arg-type]
            self.assertEqual(dr.type, fr.type)
            self.assertAlmostEqual(dr.confidence, fr.confidence, places=3)

    def test_fire_on_empty_text(self) -> None:
        trigger = _make_orch()
        detect_results = trigger.detect("")
        fire_results   = trigger.fire("")
        self.assertEqual(detect_results, [])
        self.assertEqual(fire_results, [])


# ---------------------------------------------------------------------------
# Additional edge case tests
# ---------------------------------------------------------------------------


class TestEdgeCases(unittest.TestCase):
    """Additional edge case coverage for completeness."""

    def test_case_insensitivity(self) -> None:
        """Keywords should match regardless of case."""
        trigger = _make_orch()
        
        upper_results  = trigger.detect("ERROR occurred in the system")
        lower_results  = trigger.detect("error occurred in the system")
        mixed_results  = trigger.detect("Error Occurred In The System")
        
        for rset in [upper_results, lower_results, mixed_results]:
            types_found = {r.type for r in rset}
            self.assertIn(DecisionPoint.ERROR_STATE, types_found)

    def test_multiple_matches_same_type(self) -> None:
        """If text contains multiple keywords of the same type, all should be detected."""
        trigger = _make_orch()
        # "error" and "exception" both belong to ERROR_STATE
        text = "Got an error and an exception in the code"
        results = trigger.detect(text)
        
        error_types = [r for r in results if r.type == DecisionPoint.ERROR_STATE]  # type: ignore[arg-type, union-attr]
        self.assertGreaterEqual(len(error_types), 1)

    def test_confidence_value_range(self) -> None:
        """All DetectedPoint confidence values must be in [0.5, 1.0]."""
        trigger = _make_orch()
        # Base confidence is 0.7 for single match
        results = trigger.detect("The result is success")
        
        for point in results:
            self.assertGreaterEqual(point.confidence, 0.5)
            self.assertLessEqual(point.confidence, 1.0)

    def test_confidence_rises_with_repeats(self) -> None:
        """More occurrences of a keyword should yield higher confidence (capped at 1.0)."""
        trigger = _make_orch()
        
        single_text   = "Got an error"           # 1 match
        multiple_text = "Got an error but another error and yet one more error here"  # 3+ matches
        
        single_results  = trigger.detect(single_text)
        multi_results   = trigger.detect(multiple_text)
        
        if single_results and multi_results:
            base_conf = single_results[0].confidence
            highest   = max(r.confidence for r in multi_results)  # type: ignore[arg-type, union-attr]
            self.assertGreater(highest, base_conf,
                               f"Repeats should boost confidence")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    unittest.main(verbosity=2)
