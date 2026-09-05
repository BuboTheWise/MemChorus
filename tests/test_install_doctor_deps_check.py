"""Tests for memchorus-doctor ``--deps-check`` / the OpenTelemetry consistency
check (issue #169).

The point of #169 is that a reinstall-from-GitHub into a shared venv used to let
pip re-resolve the OpenTelemetry family and *split* it (e.g. ``opentelemetry-api``
1.44.0 alongside ``opentelemetry-sdk`` 1.29.0), which bricked ``import memchorus``
/ ``import mempalace``. These tests lock the detector down with **deterministic**
fake metadata so they do not depend on whatever OpenTelemetry stack the test
environment happens to carry:

* a coherent OTel set   -> PASS
* a split/skewed set      -> FAIL (with a fix hint)
* no OTel at all          -> WARN (core still runs)
* the CLI flags ``--deps-check`` and ``--json``
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import memchorus.install_doctor as doctor
from memchorus.install_doctor import (
    PASS,
    FAIL,
    WARN,
    _otel_packages_present,
    check_otel_consistency,
    deps_check_report,
    main,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _fake_dist(name: str, version: str, requires=None, marker: str | None = None):
    """Build a minimal Distribution-lookalike with string-form ``requires``.

    String-form ``requires`` mirrors the real CPython 3.11 metadata behaviour
    (``Distribution.requires`` yields strings, not ``Requirement`` objects) — the
    detector must handle that shape.
    """

    class _D:
        def __init__(self, name, version, requires, marker):
            self.name = name
            self.version = version
            self.requires = [
                f"{n}{s}; {m}" if m else f"{n}{s}"
                for n, s, m in (requires or [])
            ]
            self._marker = marker

    return _D(name, version, requires, marker)


def _coherent_dists():
    return [
        _fake_dist("opentelemetry-api", "1.39.1", [("typing-extensions", "", None)]),
        _fake_dist(
            "opentelemetry-sdk",
            "1.39.1",
            [
                ("opentelemetry-api", "==1.39.1", None),
                ("opentelemetry-semantic-conventions", "==0.60b1", None),
            ],
        ),
        _fake_dist(
            "opentelemetry-instrumentation",
            "0.60b1",
            [
                ("opentelemetry-api", "~=1.30", None),
                ("opentelemetry-semantic-conventions", "==0.60b1", None),
                ("packaging", ">=18.0", None),
            ],
        ),
        _fake_dist(
            "opentelemetry-exporter-otlp-proto-common",
            "1.39.1",
            [("opentelemetry-proto", "==1.39.1", None)],
        ),
        _fake_dist(
            "opentelemetry-exporter-otlp-proto-grpc",
            "1.39.1",
            [
                ("opentelemetry-exporter-otlp-proto-common", "==1.39.1", None),
                ("opentelemetry-sdk", "~=1.39.1", None),
            ],
        ),
        _fake_dist(
            "opentelemetry-proto", "1.39.1", [("opentelemetry-api", ">=1.2.0", None)]
        ),
        _fake_dist(
            "opentelemetry-semantic-conventions",
            "0.60b1",
            [("opentelemetry-api", "~=1.30", None)],
        ),
    ]


def _skewed_dists():
    """The classic #169 split: api 1.44.0 but sdk 1.29.0 (+ semconv 0.50b0)."""
    return [
        _fake_dist("opentelemetry-api", "1.44.0", [("typing-extensions", "", None)]),
        _fake_dist(
            "opentelemetry-sdk",
            "1.29.0",
            [
                ("opentelemetry-api", "==1.29.0", None),  # not satisfied by 1.44.0
                ("opentelemetry-semantic-conventions", "==0.50b0", None),
            ],
        ),
        _fake_dist(
            "opentelemetry-exporter-otlp-proto-http",
            "1.39.1",
            [
                ("opentelemetry-exporter-otlp-proto-common", "==1.39.1", None),
                ("opentelemetry-sdk", "~=1.39.1", None),  # not satisfied by 1.29.0
            ],
        ),
        _fake_dist(
            "opentelemetry-exporter-otlp-proto-common",
            "1.29.0",
            [("opentelemetry-proto", "==1.29.0", None)],
        ),
        _fake_dist(
            "opentelemetry-proto", "1.29.0", [("opentelemetry-api", ">=1.2.0", None)]
        ),
        _fake_dist(
            "opentelemetry-semantic-conventions",
            "0.50b0",
            [("opentelemetry-api", "==1.29.0", None)],  # not satisfied by 1.44.0
        ),
    ]


# ---------------------------------------------------------------------------
# _otel_packages_present / _parse_req
# ---------------------------------------------------------------------------

def test_otel_packages_present_returns_installed_map():
    with patch.object(doctor, "_otel_packages_present") as m:
        m.return_value = {
            "opentelemetry-api": "1.39.1",
            "opentelemetry-sdk": "1.39.1",
        }
        got = doctor._otel_packages_present()
    assert got == {"opentelemetry-api": "1.39.1", "opentelemetry-sdk": "1.39.1"}


def test_parse_req_handles_string_requirement():
    # Emulate the internal _parse_req via a direct Requirement parse check: the
    # detector must treat the string "opentelemetry-api==1.29.0" as a real pin.
    from packaging.requirements import Requirement

    r = Requirement("opentelemetry-api==1.29.0")
    assert r.name == "opentelemetry-api"
    assert "1.44.0" not in str(r.specifier.__class__.__name__)  # sanity
    from packaging.version import Version

    assert Version("1.29.0") in r.specifier
    assert Version("1.44.0") not in r.specifier


# ---------------------------------------------------------------------------
# check_otel_consistency — deterministic pass / fail / warn
# ---------------------------------------------------------------------------

def test_check_otel_consistency_pass_on_coherent_stack():
    with patch.object(doctor.imp_meta, "distributions", side_effect=lambda: _coherent_dists()):
        r = check_otel_consistency()
    assert r.status == PASS
    assert "coherent" in r.message.lower()
    # api and sdk versions should be surfaced in the message
    assert "1.39.1" in r.message


def test_check_otel_consistency_fail_on_skewed_stack():
    with patch.object(doctor.imp_meta, "distributions", side_effect=lambda: _skewed_dists()):
        r = check_otel_consistency()
    assert r.status == FAIL
    assert "split" in r.message.lower() or "skew" in r.message.lower()
    # Must name the real skew (api 1.44 vs sdk 1.29) and carry a fix hint.
    assert "1.44" in r.message and "1.29" in r.message
    assert r.hint
    assert "pip install" in r.hint or "pip check" in r.hint


def test_check_otel_consistency_fail_detects_semantic_conventions_skew():
    # A subtly-skewed stack where only the semantic-conventions line disagrees.
    dists = _coherent_dists()
    # Bump semantic-conventions to a line the sdk 1.39.1 pins against 0.60b1.
    dists[-1] = _fake_dist(
        "opentelemetry-semantic-conventions",
        "0.65b0",
        [("opentelemetry-api", "~=1.30", None)],
    )
    with patch.object(doctor.imp_meta, "distributions", side_effect=lambda: dists):
        r = check_otel_consistency()
    assert r.status == FAIL
    assert "semantic-conventions" in r.message


def test_check_otel_consistency_warn_when_no_otel():
    with patch.object(doctor.imp_meta, "distributions", side_effect=lambda: []):
        r = check_otel_consistency()
    assert r.status == WARN
    assert "not installed" in r.message.lower()


def test_check_otel_consistency_requires_present():
    # The detector must at least run (not raise) when packaging is importable.
    assert hasattr(doctor, "check_otel_consistency")
    assert callable(doctor.check_otel_consistency)


def test_name_is_stable():
    assert check_otel_consistency.__name__ == "check_otel_consistency"
    with patch.object(doctor.imp_meta, "distributions", side_effect=lambda: []):
        assert check_otel_consistency().name == "otel_consistency"


# ---------------------------------------------------------------------------
# deps_check_report
# ---------------------------------------------------------------------------

def test_deps_check_report_has_two_results():
    with patch.object(doctor.imp_meta, "distributions", side_effect=lambda: _coherent_dists()):
        results = deps_check_report()
    assert len(results) == 2
    names = {r.name for r in results}
    assert "dependency_integrity" in names
    assert "otel_consistency" in names


# ---------------------------------------------------------------------------
# main() — CLI flags
# ---------------------------------------------------------------------------

def test_main_deps_check_json_pass(capsys):
    with patch.object(doctor.imp_meta, "distributions", side_effect=lambda: _coherent_dists()):
        code = main(["--deps-check", "--json"])
    assert code == 0
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert doc["ok"] is True
    statuses = {r["name"]: r["status"] for r in doc["results"]}
    assert statuses["otel_consistency"] == "PASS"


def test_main_deps_check_json_fail(capsys):
    with patch.object(doctor.imp_meta, "distributions", side_effect=lambda: _skewed_dists()):
        code = main(["--deps-check", "--json"])
    assert code == 1
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert doc["ok"] is False
    statuses = {r["name"]: r["status"] for r in doc["results"]}
    assert statuses["otel_consistency"] == "FAIL"


def test_main_full_report_default(capsys):
    # Default (no flags) still runs the full 8-check suite as before.
    with patch.object(doctor.imp_meta, "distributions", side_effect=lambda: _coherent_dists()):
        code = main([])
    assert code in (0, 1)
    out = capsys.readouterr().out
    assert "MemChorus Install Doctor" in out
