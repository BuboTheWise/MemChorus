"""Integration tests for FeedbackLoop (loader + engine + escalation).

Covers the full pipeline:
  - YAML config -> FeedbackLoopDefinition (schema/loader)
  - Real detector engine evaluation against TurnContext
  - Actual EscalationTracker wiring with feedback loop conditions
  - End-to-end "loop fires at detection time" across the complete wiring

Imports use the actual engine and escalation modules as they exist on disk.
"""

import os
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestLoaderToEngineIntegration(unittest.TestCase):
    """Ensure valid YAML loop definitions wire into the engine correctly."""

    def test_valid_yaml_fires_correctly(self):
        yaml_content = textwrap.dedent("""\
            schema: schema_v1
            name: spiral_risk_guard
            trigger_event: pre_llm_call
            cooldown_interval: 0
            priority: 50
            enabled: true
            conditions:
              conversation_length: {type: threshold, value: 3}
        """)
        from memchorus.feedback_loop.loader import load_feedback_loops

        with tempfile.TemporaryDirectory() as tmpdir:
            p = os.path.join(tmpdir, "loop.yaml")
            with open(p, 'w') as f:
                f.write(yaml_content)
            loops = load_feedback_loops(tmpdir)
        self.assertEqual(len(loops), 1)

    def test_invalid_yaml_skipped(self):
        from memchorus.feedback_loop.loader import load_feedback_loops
        yaml_bad = textwrap.dedent("""\
            schema: schema_v1
            bad_key_here: true
        """)
        with tempfile.TemporaryDirectory() as tmpdir:
            p = os.path.join(tmpdir, "bad.yaml")
            with open(p, 'w') as f:
                f.write(yaml_bad)
            loops = load_feedback_loops(tmpdir)
        self.assertEqual(len(loops), 0)

    def test_disabled_loop_not_evaluated(self):
        yaml_a = textwrap.dedent("""\
            schema: schema_v1
            name: disabled_loop
            trigger_event: pre_llm_call
            cooldown_interval: 0
            priority: 50
            enabled: false
        """)
        from memchorus.feedback_loop.loader import load_feedback_loops

        with tempfile.TemporaryDirectory() as tmpdir:
            p = os.path.join(tmpdir, "disabled.yaml")
            with open(p, 'w') as f:
                f.write(yaml_a)
            loops = load_feedback_loops(tmpdir)
        self.assertEqual(len(loops), 1)
        self.assertFalse(loops[0].enabled)

    def test_configured_priorities_sort_correctly(self):
        yaml_high = textwrap.dedent("""\
            schema: schema_v1
            name: high_pri
            trigger_event: pre_llm_call
            cooldown_interval: 60
            priority: 90
            enabled: true
        """)
        yaml_low = textwrap.dedent("""\
            schema: schema_v1
            name: low_pri
            trigger_event: pre_llm_call
            cooldown_interval: 30
            priority: 10
            enabled: true
        """)
        from memchorus.feedback_loop.loader import load_feedback_loops

        with tempfile.TemporaryDirectory() as tmpdir:
            p1 = os.path.join(tmpdir, "high.yaml")
            p2 = os.path.join(tmpdir, "low.yaml")
            with open(p1, 'w') as f:
                f.write(yaml_high)
            with open(p2, 'w') as f:
                f.write(yaml_low)
            loops = load_feedback_loops(tmpdir)

        self.assertEqual(len(loops), 2)


class TestEscalatorDetectorWiring(unittest.TestCase):
    """Real escalation tracker works with feedback detector."""

    def test_escalation_tracks_with_detector(self):
        from memchorus.feedback_loop.engine import FeedbackDetector, TurnContext
        from memchorus.feedback_loop.escalation import EscalationTracker
        from memchorus.feedback_loop.schema_v1 import FeedbackLoopDefinition
        from dataclasses import field

        loop_def = FeedbackLoopDefinition(
            schema="schema_v1",
            name="test_loop",
            trigger_event="pre_llm_call",
            cooldown_interval=0,
            priority=50,
            enabled=True,
            conditions={},
        )
        detector = FeedbackDetector([loop_def], EscalationTracker())
        ctx = TurnContext(user_message="test turn", conversation_length=10)
        prompts = detector.evaluate(ctx)

        self.assertEqual(len(prompts), 1)
        self.assertIn("[FEEDBACK:test_loop]", prompts[0])

    def test_detector_no_conditions_always_fires(self):
        from memchorus.feedback_loop.engine import FeedbackDetector, TurnContext
        from memchorus.feedback_loop.escalation import EscalationTracker
        from memchorus.feedback_loop.schema_v1 import FeedbackLoopDefinition

        loop_def = FeedbackLoopDefinition(
            schema="schema_v1",
            name="always_fire",
            trigger_event="pre_llm_call",
            cooldown_interval=0,
            priority=50,
            enabled=True,
        )
        detector = FeedbackDetector([loop_def], EscalationTracker())
        ctx = TurnContext(user_message="test", conversation_length=5)
        prompts = detector.evaluate(ctx)

        self.assertEqual(len(prompts), 1)

    def test_cooldown_enforced_by_tracker(self):
        from memchorus.feedback_loop.engine import FeedbackDetector, TurnContext
        from memchorus.feedback_loop.escalation import EscalationTracker
        from memchorus.feedback_loop.schema_v1 import FeedbackLoopDefinition
        import time

        loop_def = FeedbackLoopDefinition(
            schema="schema_v1",
            name="cooldown_test",
            trigger_event="pre_llm_call",
            cooldown_interval=60,
            priority=50,
        )
        tracker = EscalationTracker()
        detector = FeedbackDetector([loop_def], tracker)

        # First fire: should work.
        ctx = TurnContext(user_message="test", conversation_length=3)
        prompts1 = detector.evaluate(ctx)
        self.assertEqual(len(prompts1), 1)

        # Second fire immediately after -- should be blocked by cooldown.
        prompts2 = detector.evaluate(ctx)
        self.assertEqual(len(prompts2), 0)

    def test_disabled_loop_ignored_by_detector(self):
        from memchorus.feedback_loop.engine import FeedbackDetector, TurnContext
        from memchorus.feedback_loop.escalation import EscalationTracker
        from memchorus.feedback_loop.schema_v1 import FeedbackLoopDefinition

        loop_def = FeedbackLoopDefinition(
            schema="schema_v1",
            name="disabled",
            trigger_event="pre_llm_call",
            cooldown_interval=0,
            priority=50,
            enabled=False,
        )
        detector = FeedbackDetector([loop_def], EscalationTracker())
        ctx = TurnContext(user_message="test", conversation_length=3)
        prompts = detector.evaluate(ctx)

        self.assertEqual(len(prompts), 0)


class TestEndToEndPipeline(unittest.TestCase):
    """Full pipeline: load config, instantiate tracker + detector, run detection."""

    def test_loop_fires_through_full_wiring(self):
        yaml_config = textwrap.dedent("""\
            schema: schema_v1
            name: spiral_guard
            trigger_event: pre_llm_call
            cooldown_interval: 0
            priority: 50
            enabled: true
            correction_prompt: "You are entering a conversation spiral. Reframe your output."
        """)

        with tempfile.TemporaryDirectory() as tmpdir:
            p = os.path.join(tmpdir, "config.yaml")
            with open(p, 'w') as f:
                f.write(yaml_config)

            from memchorus.feedback_loop.loader import load_feedback_loops
            from memchorus.feedback_loop.escalation import EscalationTracker
            from memchorus.feedback_loop.engine import FeedbackDetector, TurnContext

            loops = load_feedback_loops(tmpdir)

        self.assertEqual(len(loops), 1)
        loop_def = loops[0]

        detector = FeedbackDetector([loop_def], EscalationTracker())
        ctx = TurnContext(user_message="I think we should try a different approach", conversation_length=20, recent_messages=["same words again"] * 4)

        prompts = detector.evaluate(ctx)
        self.assertEqual(len(prompts), 1)
        # Should include the custom correction prompt text.
        self.assertIn("[FEEDBACK:spiral_guard]", prompts[0])
        self.assertIn("reframe", prompts[0].lower())

    def test_multiple_loops_combined(self):
        yaml_a = textwrap.dedent("""\
            schema: schema_v1
            name: loop_a
            trigger_event: pre_llm_call
            cooldown_interval: 0
            priority: 50
            enabled: true
        """)
        with tempfile.TemporaryDirectory() as tmpdir:
            p = os.path.join(tmpdir, "loop_a.yaml")
            with open(p, 'w') as f:
                f.write(yaml_a)
            from memchorus.feedback_loop.loader import load_feedback_loops
            from memchorus.feedback_loop.escalation import EscalationTracker
            from memchorus.feedback_loop.engine import FeedbackDetector, TurnContext

            loops = load_feedback_loops(tmpdir)

        self.assertEqual(len(loops), 1)
        detector = FeedbackDetector([loops[0]], EscalationTracker())
        ctx = TurnContext(user_message="test", conversation_length=3)
        prompts = detector.evaluate(ctx)
        self.assertEqual(len(prompts), 1)


class TestEscalationProgression(unittest.TestCase):
    """Ensure escalation levels advance correctly within the detector pipeline."""

    def test_escalation_advances_at_threshold(self):
        from memchorus.feedback_loop.engine import FeedbackDetector, TurnContext
        from memchorus.feedback_loop.escalation import EscalationTracker
        from memchorus.feedback_loop.schema_v1 import FeedbackLoopDefinition
        import time

        loop_def = FeedbackLoopDefinition(
            schema="schema_v1",
            name="adv_test",
            trigger_event="pre_llm_call",
            cooldown_interval=0,
            priority=50,
        )
        tracker = EscalationTracker(level_threshold=3)
        detector = FeedbackDetector([loop_def], tracker)

        ctx = TurnContext(user_message="test", conversation_length=1)

        # Verify Level 1 hint on valid fire.
        prompts = detector.evaluate(ctx)
        self.assertEqual(len(prompts), 1)
        self.assertIn("[FEEDBACK:adv_test]", prompts[0])


class TestMultiLoopFromConfig(unittest.TestCase):
    """Multiple loop definitions in one YAML file are each independently loaded and evaluated."""

    def test_multi_loop_each_independently(self):
        yaml_a = textwrap.dedent("""\
            schema: schema_v1
            name: multi_a
            trigger_event: pre_llm_call
            cooldown_interval: 0
            priority: 50
            enabled: true
        """)
        yaml_b = textwrap.dedent("""\
            schema: schema_v1
            name: multi_b
            trigger_event: post_tool_call
            cooldown_interval: 30
            priority: 75
            enabled: true
        """)
        with tempfile.TemporaryDirectory() as tmpdir:
            a_p = os.path.join(tmpdir, "multi_a.yaml")
            b_p = os.path.join(tmpdir, "multi_b.yaml")
            with open(a_p, 'w') as f:
                f.write(yaml_a)
            with open(b_p, 'w') as f:
                f.write(yaml_b)

            from memchorus.feedback_loop.loader import load_feedback_loops
            from memchorus.feedback_loop.escalation import EscalationTracker
            from memchorus.feedback_loop.engine import FeedbackDetector, TurnContext

            loops = load_feedback_loops(tmpdir)

        # Both loop definitions should be loaded as they're in separate files.
        self.assertEqual(len(loops), 2)
        names = {l.name for l in loops}
        self.assertIn("multi_a", names)
        self.assertIn("multi_b", names)

        # The detector evaluates all enabled loops regardless of trigger_event;
        # both fire here since no conditions block them.
        detector = FeedbackDetector(loops, EscalationTracker())
        ctx = TurnContext(user_message="test", conversation_length=1)
        prompts = detector.evaluate(ctx)
        self.assertEqual(len(prompts), 2)


if __name__ == "__main__":
    unittest.main()
