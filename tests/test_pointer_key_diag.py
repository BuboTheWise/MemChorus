"""Tests for the pointer-key vs drawer_id mismatch diagnostic (issue #331).

Covers two layers, both hermetic (no dependency on the local profile's store):

* :mod:`memchorus.pointer_key_diag` — the pure pointer-key classification,
  canonicalisation, payload detection, store discovery, cross-form resolution
  and the census report (GH-330 invariant).
* :mod:`memchorus.install_doctor` — the ``--pointer-keys`` doctor wiring:
  the check-builder (PASS / WARN / FAIL + exit code), the ``--keys`` /
  ``--keys-file`` collector, and the ``main()`` dispatch in both human and
  ``--json`` form.

All store fixtures are built in temp dirs and passed in directly, so no test
touches the real profile store.
"""
import json

import pytest

from memchorus import pointer_key_diag as diag
from memchorus import install_doctor as doctor


# --------------------------------------------------------------------------- #
# fixtures / helpers                                                            #
# --------------------------------------------------------------------------- #
def _write(path, payload, raw=None):
    """Write a cache file.  ``raw`` overrides (for corrupt-JSON cases)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# _is_pointer_key / _canonical_flat                                            #
# --------------------------------------------------------------------------- #
class TestPointerKeyShape:
    @pytest.mark.parametrize(
        "key",
        [
            "learning-db203b243798a941",
            "decision-88e86e5790c4c3ec",
            "DECISION_88e86e5790c4c3ec",
            "Learning-db203b243798a941",
            "learning-deadbeef1234",
        ],
    )
    def test_pointer_keys(self, key):
        assert diag._is_pointer_key(key) is True

    @pytest.mark.parametrize(
        "key",
        [
            "deploy-checklist",
            "project-memchorus",
            "auth-flow",
            "v3",
            "general",
            "memchorus_general",
            "agent_LEARNING",
            "drawer_wing_room_0123456789abcdef",
            "",
            "1234567890abcdef",
            "learning-",
        ],
    )
    def test_non_pointer_keys(self, key):
        assert diag._is_pointer_key(key) is False

    def test_canonical_flat_flat(self):
        assert diag._canonical_flat("learning-db203b243798a941") == (
            "learning-db203b243798a941"
        )

    def test_canonical_flat_drawer_form(self):
        assert diag._canonical_flat("DECISION_88e86e5790c4c3ec") == (
            "decision-88e86e5790c4c3ec"
        )

    def test_canonical_flat_mixed_case_tag(self):
        assert diag._canonical_flat("Learning-deadbeef1234") == (
            "learning-deadbeef1234"
        )

    def test_canonical_flat_non_pointer_passthrough(self):
        assert diag._canonical_flat("deploy-checklist") == "deploy-checklist"


# --------------------------------------------------------------------------- #
# _has_payload                                                                 #
# --------------------------------------------------------------------------- #
class TestHasPayload:
    def test_content_string(self, tmp_path):
        p = _write(tmp_path / "k.json", {"content": "hello"})
        assert diag._has_payload(p) is True

    def test_title_text(self, tmp_path):
        p = _write(tmp_path / "k.json", {"title": "t", "text": "x"})
        assert diag._has_payload(p) is True

    def test_empty_dict(self, tmp_path):
        p = _write(tmp_path / "k.json", {})
        assert diag._has_payload(p) is False

    def test_all_null_values(self, tmp_path):
        p = _write(tmp_path / "k.json", {"content": None, "title": None})
        assert diag._has_payload(p) is False

    def test_scalar_body(self, tmp_path):
        p = _write(tmp_path / "k.json", "just-a-string")
        assert diag._has_payload(p) is True

    def test_corrupt_json(self, tmp_path):
        p = _write(tmp_path / "k.json", None, raw="{not json")
        assert diag._has_payload(p) is False

    def test_missing_file(self, tmp_path):
        assert diag._has_payload(tmp_path / "nope.json") is False


# --------------------------------------------------------------------------- #
# discover_pointer_keys                                                        #
# --------------------------------------------------------------------------- #
class TestDiscover:
    def test_finds_only_pointer_keys(self, tmp_path):
        _write(tmp_path / "learning-aaaaaaaaaaaaaa.json", {"content": "x"})
        _write(tmp_path / "decision-bbbbbbbbbbbbbb.json", {"content": "y"})
        _write(tmp_path / "deploy-checklist.json", {"content": "z"})
        _write(tmp_path / "notes.txt", None, raw="not json")
        found = diag.discover_pointer_keys([tmp_path])
        assert set(found) == {"learning-aaaaaaaaaaaaaa", "decision-bbbbbbbbbbbbbb"}

    def test_dedups_flat_and_drawer_forms(self, tmp_path):
        _write(tmp_path / "decision-88e86e5790c4c3ec.json", {"content": "x"})
        _write(tmp_path / "DECISION_88e86e5790c4c3ec.json", {"content": "y"})
        found = diag.discover_pointer_keys([tmp_path])
        assert set(found) == {"decision-88e86e5790c4c3ec"}
        assert len(found["decision-88e86e5790c4c3ec"]) == 2

    def test_skips_missing_root(self, tmp_path):
        assert diag.discover_pointer_keys([tmp_path / "no-such-dir"]) == {}

    def test_skips_non_directory(self, tmp_path):
        f = _write(tmp_path / "learning-aaaaaaaaaaaaaa.json", {"content": "x"})
        assert diag.discover_pointer_keys([f]) == {}


# --------------------------------------------------------------------------- #
# resolve_pointer_key                                                          #
# --------------------------------------------------------------------------- #
class TestResolve:
    def test_flat_hit(self, tmp_path):
        _write(tmp_path / "learning-aaaaaaaaaaaaaa.json", {"content": "x"})
        hit = diag.resolve_pointer_key("learning-aaaaaaaaaaaaaa", [tmp_path], [])
        assert hit is not None and hit.stem == "learning-aaaaaaaaaaaaaa"

    def test_cross_form_hit(self, tmp_path):
        _write(tmp_path / "DECISION_88e86e5790c4c3ec.json", {"content": "x"})
        hit = diag.resolve_pointer_key("decision-88e86e5790c4c3ec", [tmp_path], [])
        assert hit is not None and hit.stem == "DECISION_88e86e5790c4c3ec"

    def test_missing_key(self, tmp_path):
        _write(tmp_path / "learning-aaaaaaaaaaaaaa.json", {"content": "x"})
        assert diag.resolve_pointer_key("learning-ffffffffffff", [tmp_path], []) is None

    def test_null_payload_is_a_miss(self, tmp_path):
        _write(tmp_path / "learning-aaaaaaaaaaaaaa.json", None, raw="null")
        assert diag.resolve_pointer_key("learning-aaaaaaaaaaaaaa", [tmp_path], []) is None

    def test_resolves_in_memory_dir(self, tmp_path):
        mem = tmp_path / "mem"
        _write(mem / "decision-bbbbbbbbbbbbbb.json", {"content": "x"})
        hit = diag.resolve_pointer_key("decision-bbbbbbbbbbbbbb", [], [mem])
        assert hit is not None

    def test_no_store(self, tmp_path):
        assert diag.resolve_pointer_key("learning-aaaaaaaaaaaaaa", [tmp_path], []) is None


# --------------------------------------------------------------------------- #
# pointer_key_report (census)                                                 #
# --------------------------------------------------------------------------- #
class TestReport:
    def test_no_data(self, tmp_path):
        r = diag.pointer_key_report([tmp_path], [])
        assert r.no_data is True
        assert r.total_keys == 0
        assert r.ok is True

    def test_all_resolved(self, tmp_path):
        _write(tmp_path / "learning-aaaaaaaaaaaaaa.json", {"content": "x"})
        _write(tmp_path / "decision-bbbbbbbbbbbbbb.json", {"content": "y"})
        r = diag.pointer_key_report([tmp_path], [])
        assert r.no_data is False
        assert r.total_keys == 2
        assert r.resolved == 2
        assert r.unresolved == 0
        assert r.ok is True
        assert r.cross_form_hits == 0

    def test_unresolved_reported(self, tmp_path):
        _write(tmp_path / "learning-aaaaaaaaaaaaaa.json", {"content": "x"})
        _write(tmp_path / "decision-bbbbbbbbbbbbbb.json", None, raw="null")
        r = diag.pointer_key_report([tmp_path], [])
        assert r.total_keys == 2
        assert r.unresolved == 1
        assert r.resolved == 1
        assert r.ok is False
        assert r.unresolved_keys == ["decision-bbbbbbbbbbbbbb"]

    def test_extra_dangling_key(self, tmp_path):
        _write(tmp_path / "learning-aaaaaaaaaaaaaa.json", {"content": "x"})
        r = diag.pointer_key_report([tmp_path], [], extra_keys=["decision-ffffffffffff"])
        assert r.total_keys == 2
        assert r.unresolved == 1
        assert "decision-ffffffffffff" in r.unresolved_keys

    def test_cross_form_counted(self, tmp_path):
        _write(tmp_path / "DECISION_88e86e5790c4c3ec.json", {"content": "x"})
        r = diag.pointer_key_report([tmp_path], [])
        assert r.resolved == 1
        assert r.cross_form_hits == 1

    def test_to_dict_fields(self, tmp_path):
        _write(tmp_path / "learning-aaaaaaaaaaaaaa.json", {"content": "x"})
        d = diag.pointer_key_report([tmp_path], []).to_dict()
        for field in (
            "total_keys", "resolved", "unresolved", "cross_form_hits",
            "unresolved_keys", "scanned_roots", "no_data", "ok",
        ):
            assert field in d

    def test_non_pointer_extra_keys_ignored(self, tmp_path):
        _write(tmp_path / "learning-aaaaaaaaaaaaaa.json", {"content": "x"})
        r = diag.pointer_key_report([tmp_path], [], extra_keys=["deploy-checklist"])
        assert r.total_keys == 1
        assert r.unresolved == 0


# --------------------------------------------------------------------------- #
# install_doctor.run_pointer_key_probe                                         #
# --------------------------------------------------------------------------- #
class TestDoctorProbe:
    def _patch_dirs(self, monkeypatch, store):
        monkeypatch.setattr(
            doctor, "_pointer_key_store_dirs", lambda: ([store], [store / "mem"])
        )
        monkeypatch.setattr(doctor, "active_profile_name", lambda: "testprofile")

    def test_clean_pass(self, tmp_path, monkeypatch):
        _write(tmp_path / "learning-aaaaaaaaaaaaaa.json", {"content": "x"})
        self._patch_dirs(monkeypatch, tmp_path)
        code, check, report = doctor.run_pointer_key_probe()
        assert code == 0
        assert check.status == doctor.PASS
        assert report.ok is True

    def test_unresolved_warn_non_strict(self, tmp_path, monkeypatch):
        _write(tmp_path / "learning-aaaaaaaaaaaaaa.json", {"content": "x"})
        self._patch_dirs(monkeypatch, tmp_path)
        code, check, report = doctor.run_pointer_key_probe(
            extra_keys=["decision-ffffffffffff"]
        )
        assert code == 1
        assert check.status == doctor.WARN
        assert report.unresolved == 1
        assert "decision-ffffffffffff" in check.message

    def test_unresolved_fail_strict(self, tmp_path, monkeypatch):
        _write(tmp_path / "learning-aaaaaaaaaaaaaa.json", {"content": "x"})
        self._patch_dirs(monkeypatch, tmp_path)
        code, check, report = doctor.run_pointer_key_probe(
            strict=True, extra_keys=["decision-ffffffffffff"]
        )
        assert code == 1
        assert check.status == doctor.FAIL

    def test_no_data_pass(self, tmp_path, monkeypatch):
        self._patch_dirs(monkeypatch, tmp_path / "empty")
        code, check, report = doctor.run_pointer_key_probe()
        assert code == 0
        assert check.status == doctor.PASS
        assert report.no_data is True

    def test_examples_capped(self, tmp_path, monkeypatch):
        _write(tmp_path / "learning-aaaaaaaaaaaaaa.json", {"content": "x"})
        self._patch_dirs(monkeypatch, tmp_path)
        extras = [f"decision-{i:012x}" for i in range(7)]
        code, check, report = doctor.run_pointer_key_probe(extra_keys=extras)
        assert code == 1
        assert check.message.count("decision-") <= 5
        assert "+2 more" in check.message


# --------------------------------------------------------------------------- #
# _collect_pointer_keys                                                        #
# --------------------------------------------------------------------------- #
class TestCollectKeys:
    def test_flags(self):
        args = [
            "--pointer-keys", "--keys",
            "learning-aaaaaaaaaaaaaa", "decision-bbbbbbbbbbbbbb", "--strict",
        ]
        keys = doctor._collect_pointer_keys(args)
        assert keys == ["learning-aaaaaaaaaaaaaa", "decision-bbbbbbbbbbbbbb"]

    def test_keys_file(self, tmp_path):
        p = tmp_path / "keys.txt"
        p.write_text(
            "# a comment\n\nlearning-aaaaaaaaaaaaaa\ndecision-bbbbbbbbbbbbbb\n",
            encoding="utf-8",
        )
        args = ["--pointer-keys", "--keys-file", str(p)]
        keys = doctor._collect_pointer_keys(args)
        assert keys == ["learning-aaaaaaaaaaaaaa", "decision-bbbbbbbbbbbbbb"]

    def test_empty(self):
        assert doctor._collect_pointer_keys(["--pointer-keys"]) == []


# --------------------------------------------------------------------------- #
# main() dispatch                                                              #
# --------------------------------------------------------------------------- #
class TestMainDispatch:
    def _patch(self, monkeypatch, store):
        monkeypatch.setattr(
            doctor, "_pointer_key_store_dirs", lambda: ([store], [store / "mem"])
        )
        monkeypatch.setattr(doctor, "active_profile_name", lambda: "testprofile")

    def test_human_unresolved(self, tmp_path, monkeypatch, capsys):
        _write(tmp_path / "learning-aaaaaaaaaaaaaa.json", {"content": "x"})
        self._patch(monkeypatch, tmp_path)
        code = doctor.main(["--pointer-keys", "--keys", "decision-ffffffffffff"])
        out = capsys.readouterr().out
        assert code == 1
        assert "pointer_keys" in out
        assert "decision-ffffffffffff" in out

    def test_json_clean(self, tmp_path, monkeypatch, capsys):
        _write(tmp_path / "learning-aaaaaaaaaaaaaa.json", {"content": "x"})
        self._patch(monkeypatch, tmp_path)
        code = doctor.main(["--pointer-keys", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["ok"] is True
        assert payload["unresolved"] == 0
        assert "check" in payload

    def test_json_unresolved(self, tmp_path, monkeypatch, capsys):
        _write(tmp_path / "learning-aaaaaaaaaaaaaa.json", {"content": "x"})
        self._patch(monkeypatch, tmp_path)
        code = doctor.main(
            ["--pointer-keys", "--json", "--keys", "decision-ffffffffffff"]
        )
        payload = json.loads(capsys.readouterr().out)
        assert code == 1
        assert payload["ok"] is False
        assert payload["unresolved"] == 1
        assert payload["unresolved_keys"] == ["decision-ffffffffffff"]
