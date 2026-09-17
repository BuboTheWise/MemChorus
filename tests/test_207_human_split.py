"""#207 — human ``--recall`` surface exposes the hot-injection split.

Per AC4 "Render (read-only simulation): N of M entries injected {inline |
locator+preview}" the doctor's human output must surface the per-entry mode
split so an operator can see at a glance which entries collapsed to the
locator+preview form and which inlined in full.
"""
from __future__ import annotations

import contextlib
import io
import unittest

from memchorus import install_doctor as doctor


def _report_with_split():
    return {
        "query": "deployment note",
        "limit": 5,
        "status": "ok",
        "reason": None,
        "results": [
            {
                "key": "learning-collapse-1",
                "source": "learning",
                "score": 0.95,
                "score_breakdown": None,
                "disposition": "injected",
                "content_preview": "line one of a long note",
            },
            {
                "key": "learning-inline-1",
                "source": "learning",
                "score": 0.85,
                "score_breakdown": None,
                "disposition": "injected",
                "content_preview": "short inline note",
            },
        ],
        "render": {
            "rendered": "…see simulated output…",
            "injected": [
                {"key": "learning-collapse-1", "score": 0.95,
                 "content": "…", "suppressed": False, "mode": "locator_preview"},
                {"key": "learning-inline-1", "score": 0.85,
                 "content": "short inline note", "suppressed": False,
                 "mode": "inline"},
            ],
            "dropped": [],
            "full_body_mark": [],
            "degraded": False,
            "mode_split": {"inline": 1, "locator_preview": 1, "suppressed": 0},
        },
        "suppression": None,
    }


class Test207HumanRecallSplit(unittest.TestCase):
    """The doctor's human ``--recall`` output exposes the hot-injection split."""

    def test_split_line_present(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            doctor._render_recall_human(_report_with_split())
        out = buf.getvalue()
        self.assertIn("hot-injection split", out, out)
        self.assertIn("1 inline", out, out)
        self.assertIn("1 locator+preview", out, out)

    def test_per_entry_tags(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            doctor._render_recall_human(_report_with_split())
        out = buf.getvalue()
        # Each injected entry line is tagged with its mode (AC4 visible output).
        self.assertIn("learning-collapse-1 score=0.95 locator+preview", out, out)
        self.assertIn("learning-inline-1 score=0.85 inline", out, out)

    def test_split_falls_back_to_zeroes_when_absent(self):
        rep = _report_with_split()
        rep["render"].pop("mode_split")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            doctor._render_recall_human(rep)
        out = buf.getvalue()
        self.assertIn("hot-injection split: 0 inline", out, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
