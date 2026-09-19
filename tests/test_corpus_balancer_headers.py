"""(#209 working-state surface (b)) render tests for the two display surfaces.

``render_header_line`` — the one-line note injected INSIDE
``[MemChorus Memory Recall]`` (AC-S2).

``render_status_block`` — the fuller "why + what to do" block for
``mempalace_status``.

These tests pin the exact contract of each surface so a viewer can see:
  * OK → header None, status a terse "OK" block (not lost, but not loud)
  * WARN → header one-line nudge, status block includes remediation guide
  * CRIT → header one-line nudge (stronger wording), status block
    includes the "no current work" sub-line and the two remediation options

All inputs are canned report dicts exactly in the shape ``compute()``
returns — no live dependency.
"""

from __future__ import annotations

from memchorus.corpus_balancer import (
    render_header_line,
    render_status_block,
)


# --------------------------------------------------------------------------- #
# canned reports — exactly the shape compute() returns                        #
# --------------------------------------------------------------------------- #

def _report(
    level: str,
    m1_total: int = 0,
    m1_ratio: float = 0.0,
    m2_total: int = 0,
    m2_ratio: float = 0.0,
    total_corpus: int = 100,
    closets_present=None,
    slug: str = "memchorus",
) -> dict:
    return {
        "level": level,
        "reason": "reason text",
        "active_project": slug,
        "mode": "active",
        "partial": False,
        "errors": [],
        "thresholds_hit": ["m1_zero_active"],
        "m1": {
            "total": m1_total,
            "ratio": m1_ratio,
            "project_min_ratio": 0.05,
            "active_rooms_total": m1_total,
            "project_wings": {},
            "room_census": {},
        },
        "m2": {
            "total": m2_total,
            "ratio": m2_ratio,
            "settled_threshold": 0.5,
            "wing_census": {},
            "room_census": {},
        },
        "m3": {
            "closets_present": closets_present,
            "closet_collection": "mempalace_closets",
            "closet_stats": {
                "queries_seen": 42,
                "queries_with_active_project": 12,
                "bound_results_surfaced": 8,
            },
        },
        "total_corpus": total_corpus,
    }


# --------------------------------------------------------------------------- #
# render_header_line                                                           #
# --------------------------------------------------------------------------- #

def test_header_line_none_for_ok():
    assert render_header_line(_report("ok")) is None


def test_header_line_none_for_none_or_garbage():
    assert render_header_line(None) is None
    assert render_header_line("not-a-report") is None  # type: ignore[arg-type]
    assert render_header_line(42) is None  # type: ignore[arg-type]
    assert render_header_line([]) is None  # type: ignore[arg-type]


def test_header_line_warn_contains_all_key_facts():
    rpt = _report(level="warn", m1_total=10, m1_ratio=10/578,
                  m2_total=470, m2_ratio=470/578, total_corpus=578,
                  closets_present=True)
    line = render_header_line(rpt)
    assert isinstance(line, str)
    # The header must surface the two key numbers — reviewer can
    # visually confirm the imbalance from the prompt without opening a log.
    assert "10/578" in line
    assert "578" in line
    # The WARN level is named explicitly so the model knows to act gently.
    assert "WARN" in line
    # The remediation pointer tells the model where the action block lives.
    assert "mempalace_status" in line
    assert "Corpus Balance" in line
    # Single line — the nudge should never be multi-paragraph.
    assert "\n" not in line


def test_header_line_crit_contains_m1zero_closet_state_and_remediation():
    rpt = _report(level="crit", m1_total=0, m1_ratio=0.0,
                  m2_total=212, m2_ratio=212/216, total_corpus=216,
                  closets_present=False, slug="profile_b")
    line = render_header_line(rpt)
    assert isinstance(line, str)
    assert "CRIT" in line
    # Closet collection state is named — MISSING is the CRIT amplifier.
    assert "MISSING" in line
    assert "mempalace_closets" not in line  # collection name not in header
    # Remediation options are named directly — two explicit paths.
    assert "working-state drawer" in line
    assert "balance.mode=archive" in line
    # The model-facing action pointer is present.
    assert "mempalace_status" in line
    # Single-line discipline.
    assert "\n" not in line


def test_header_line_crit_with_closets_present_names_present():
    rpt = _report(level="crit", m1_total=0, m2_total=200, m2_ratio=0.95,
                  total_corpus=211, closets_present=True)
    line = render_header_line(rpt)
    assert isinstance(line, str)
    assert "PRESENT" in line


def test_header_line_never_leaks_pii_or_local_paths():
    # OPSEC: no "/home/<name>/..." or "bubo@..." style strings in the line.
    rpt = _report(level="crit", total_corpus=216, slug="profile_b")
    line = render_header_line(rpt)
    assert isinstance(line, str)
    assert "/home/" not in line
    assert "@gmail.com" not in line
    # Only the profile slug (which the user configured) may appear — never
    # a local filesystem path.
    assert "profile_b" not in line.split("balance")[0]  # slug is not in the banner


# --------------------------------------------------------------------------- #
# render_status_block                                                          #
# --------------------------------------------------------------------------- #

def test_status_block_ok_is_healthy_marker():
    rpt = _report(level="ok", m1_total=60, m1_ratio=0.6, m2_total=30, m2_ratio=0.3,
                  total_corpus=100, closets_present=True)
    block = render_status_block(rpt)
    assert isinstance(block, str)
    assert "Corpus Balance: OK" in block
    assert "healthy" in block.lower()
    # The two numbers are shown for audit clarity.
    assert "60/100" in block
    assert "30/100" in block


def test_status_block_warn_names_both_remediation_options():
    rpt = _report(level="warn", m1_total=10, m1_ratio=10/578,
                  m2_total=470, m2_ratio=470/578, total_corpus=578,
                  closets_present=True)
    block = render_status_block(rpt)
    assert "CORPUS BALANCE WARN" in block
    # Both remediation options are named (a + b).
    assert "working-state" in block
    assert "mode=archive" in block
    assert "mempalace_add_drawer" in block
    assert "memchorus_workspace" in block
    # The sibling work reference is present so the model can cross-link.
    assert "#206" in block
    assert "#209" in block


def test_status_block_crit_is_strongest_and_names_no_current_work():
    rpt = _report(level="crit", m1_total=0, m2_total=212,
                  m2_ratio=212/216, total_corpus=216,
                  closets_present=False, slug="profile_b")
    block = render_status_block(rpt)
    assert "CORPUS BALANCE CRIT" in block
    # CRIT specifically names "no current work" — distinct from WARN.
    assert "no current work" in block
    # Closet collection absence is named with the structural impact.
    assert "mempalace_closets" in block or "closet" in block.lower()
    assert "0.0" in block  # closet_boost structurally 0.0 note
    assert "MISSING" in block
    # Both remediation options are present.
    assert "working-state" in block
    assert "mode=archive" in block


def test_status_block_handles_unknown_report_gracefully():
    block = render_status_block(None)
    assert "(report unavailable)" in block


def test_status_block_closet_unknown_is_unknown_not_missing():
    rpt = _report(level="crit", m1_total=0, m2_total=100,
                  total_corpus=120, closets_present=None)
    block = render_status_block(rpt)
    # UNKNOWN (not present, not missing) — the classifier couldn't measure.
    assert "UNKNOWN" in block
    assert "MISSING" not in block


def test_render_roundtrip_warn_and_crit_are_visually_distinct():
    """A reviewer reading the two blocks side-by-side should be able to
    tell them apart by wording, not just the level token.
    """
    warn = render_status_block(_report(level="warn", total_corpus=578))
    crit = render_status_block(_report(level="crit", total_corpus=216))
    assert "WARN" in warn and "CRIT" not in warn
    assert "CRIT" in crit and "no current work" in crit
    # The WARN wording does NOT say "no current work" (it's a nudge).
    assert "no current work" not in warn


def test_pct_formatter_handles_none():
    # Renderers must not NPE when a sub-metric is missing from the report.
    from memchorus.corpus_balancer import _fmt_pct
    assert _fmt_pct(None) == "?"  # type: ignore[arg-type]
    assert _fmt_pct(0.0) == "0.0%"
    assert _fmt_pct(0.5) == "50.0%"
    assert _fmt_pct(1.0) == "100.0%"
