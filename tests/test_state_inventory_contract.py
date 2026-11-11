"""Writer/reader shape-contract regression tests (PR #291 review).

The canonical config writer (auto_init.generate_config) nests the data
directory under a 'mempalace_config' key (data_dir and
runtime.mempalace_home), NOT at the top level. The inventory reader must
honour that shape, otherwise the palace entry silently falls back to the
legacy location. Feeding a REAL generated config through the resolver
locks the contract to the writer's actual output, not a fixture.
"""
from __future__ import annotations

import memchorus.auto_init as ai
from memchorus import state_inventory as si


def test_resolve_data_dir_honours_canonical_nested_shape(tmp_path, monkeypatch):
    profile = "myagent"
    text = ai.generate_config(profile=profile)
    assert "mempalace_config:" in text
    assert ("data_dir:" in text) or ("mempalace_home:" in text)

    p_home = tmp_path / profile
    p_home.mkdir(parents=True, exist_ok=True)
    (p_home / "memchorus.yaml").write_text(text)

    monkeypatch.setenv("HERMES_PROFILE", profile)
    monkeypatch.setattr(si, "_profile_home", lambda p: p_home)
    monkeypatch.setattr(si, "_home", lambda: tmp_path)
    monkeypatch.setattr(si, "_hermes_home", lambda: tmp_path / "homedir")

    resolved = si._resolve_data_dir(profile)
    assert resolved is not None, "reader must honour the nested mempalace_config shape"
    assert "mempalace" in str(resolved)


def test_data_dir_from_cfg_prefers_nested_over_top_level():
    cfg = {
        "data_dir": "/top/level",
        "runtime": {"mempalace_home": "/top/rt"},
        "mempalace_config": {
            "data_dir": "/nested/data",
            "runtime": {"mempalace_home": "/nested/home"},
        },
    }
    assert si._data_dir_from_cfg(cfg) == "/nested/data"

    cfg2 = {
        "data_dir": "/top/level",
        "mempalace_config": {"runtime": {"mempalace_home": "/nested/home"}},
    }
    assert si._data_dir_from_cfg(cfg2) == "/nested/home"

    assert si._data_dir_from_cfg({"data_dir": "/top/level"}) == "/top/level"
    assert si._data_dir_from_cfg({"runtime": {"mempalace_home": "/top/rt"}}) == "/top/rt"
    assert si._data_dir_from_cfg({}) is None
    assert si._data_dir_from_cfg(None) is None
