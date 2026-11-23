"""Tests for memchorus.wing_migrate (issue #305 migration tool).

Builds a temporary chroma.sqlite3 in the live schema (``embedding_metadata``
key/value rows + an untouched ``embeddings`` table) and verifies the safety
properties the tool promises: dry-run default, metadata-only writes,
backup-before-apply, idempotency, and byte-identical vectors.
Hermetic: ``wing_orphans.valid_wings_for_profile`` is monkeypatched so no
test depends on the local profile config.
"""
import json
import sqlite3
import time

import pytest

from memchorus import wing_migrate, wing_orphans

VALID = frozenset(
    {"cthugha_LEARNING", "cthugha_DECISION", "memchorus_general", "general"}
)

#: (embedding_id, wing value) — mirrors the #305 orphan mix.
SEED_ROWS = [
    ("e1", "cthugha_LEARNING"),
    ("e2", "cthugha_LEARNING"),
    ("e3", "cthugha_LEARNING"),
    ("e4", "v3"),
    ("e5", "v3"),
    ("e6", "2026-11-20T10:17:03"),
    ("e7", None),          # <None> wing
    ("e8", "cthugha_learning"),  # case variant
]


def _read_wing_counts(path):
    con = sqlite3.connect(str(path))
    try:
        rows = con.execute(
            "SELECT COALESCE(string_value,''), COUNT(*) "
            "FROM embedding_metadata WHERE key='wing' GROUP BY string_value"
        ).fetchall()
    finally:
        con.close()
    return {w: n for w, n in rows}


def _read_vectors(path):
    con = sqlite3.connect(str(path))
    try:
        return con.execute(
            "SELECT embedding_id, data FROM embeddings ORDER BY embedding_id"
        ).fetchall()
    finally:
        con.close()


@pytest.fixture
def palace(tmp_path, monkeypatch):
    d = tmp_path / "palace"
    d.mkdir()
    con = sqlite3.connect(str(d / "chroma.sqlite3"))
    con.execute(
        "CREATE TABLE embedding_metadata ("
        " id INTEGER PRIMARY KEY, embedding_id TEXT, key TEXT,"
        " string_value TEXT, integer_value INTEGER,"
        " float_value REAL, bool_value INTEGER)"
    )
    con.execute(
        "CREATE TABLE embeddings (embedding_id TEXT PRIMARY KEY, data BLOB)"
    )
    for i, (eid, wing) in enumerate(SEED_ROWS):
        con.execute(
            "INSERT INTO embedding_metadata"
            " (id, embedding_id, key, string_value)"
            " VALUES (?,?,?,?)",
            (i + 1, eid, "wing", wing),
        )
        # distinct 2-byte vector per row
        con.execute(
            "INSERT INTO embeddings (embedding_id, data)"
            " VALUES (?, X'0102')",
            (eid,),
        )
    con.commit()
    con.close()
    # Hermetic valid-wing set (no local profile config needed).
    monkeypatch.setattr(wing_orphans, "valid_wings_for_profile", lambda p: VALID)
    return d


# ---------------------------------------------------------------------------
# census
# ---------------------------------------------------------------------------
class TestWingRowCounts:
    def test_counts_match_seed(self, palace):
        counts = wing_migrate.wing_row_counts(palace)
        assert counts == {
            "cthugha_LEARNING": 3,
            "v3": 2,
            "2026-11-20T10:17:03": 1,
            "": 1,
            "cthugha_learning": 1,
        }


# ---------------------------------------------------------------------------
# dry run
# ---------------------------------------------------------------------------
class TestDryRun:
    def test_reports_orphans_and_moves_nothing(self, palace):
        result = wing_migrate.apply_migration(
            palace_dir=palace, profile="cthugha", dry_run=True
        )
        assert result.dry_run is True
        assert result.error is None
        assert result.total_rows == 8
        assert result.orphan_rows_before == 5  # v3(2) + ts(1) + None(1) + case(1)
        assert result.rows_moved == 5
        # store is untouched
        assert _read_wing_counts(palace / "chroma.sqlite3") == {
            "cthugha_LEARNING": 3,
            "v3": 2,
            "2026-11-20T10:17:03": 1,
            "": 1,
            "cthugha_learning": 1,
        }
        # dry run makes no backup
        assert list(palace.glob("chroma.sqlite3.bak-*")) == []

    def test_plan_actions(self, palace):
        result = wing_migrate.apply_migration(
            palace_dir=palace, profile="cthugha", dry_run=True
        )
        by_src = {a.source_wing: a for a in result.actions}
        assert set(by_src) == {"v3", "2026-11-20T10:17:03", "", "cthugha_learning"}
        assert by_src["v3"].target_wing == "memchorus_general"
        assert by_src["v3"].category == wing_orphans.ORPHAN_UNRECOGNIZED
        assert by_src[""].target_wing == "memchorus_general"
        assert by_src["2026-11-20T10:17:03"].category == wing_orphans.ORPHAN_TIMESTAMP
        assert by_src["cthugha_learning"].target_wing == "cthugha_LEARNING"
        assert by_src["v3"].row_count == 2
        assert by_src[""].row_count == 1


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------
class TestApply:
    def test_repoints_metadata_only(self, palace):
        before = _read_vectors(palace / "chroma.sqlite3")
        result = wing_migrate.apply_migration(
            palace_dir=palace, profile="cthugha", dry_run=False
        )
        assert result.error is None
        assert result.dry_run is False
        assert result.orphan_rows_after == 0
        assert result.rows_moved == 5
        assert _read_vectors(palace / "chroma.sqlite3") == before  # vectors intact
        assert _read_wing_counts(palace / "chroma.sqlite3") == {
            "cthugha_LEARNING": 4,       # 3 + case-variant row
            "memchorus_general": 4,     # v3(2) + timestamp(1) + None(1)
        }

    def test_backup_created(self, palace):
        time.sleep(1.01)  # make the ts suffix distinct from a prior backup
        result = wing_migrate.apply_migration(
            palace_dir=palace, profile="cthugha", dry_run=False
        )
        assert result.backup_path
        backups = list(palace.glob("chroma.sqlite3.bak-*"))
        assert len(backups) == 1
        assert result.backup_path == str(backups[0])

    def test_no_backup_when_disabled(self, palace):
        wing_migrate.apply_migration(
            palace_dir=palace, profile="cthugha", dry_run=False, make_backup=False
        )
        assert list(palace.glob("chroma.sqlite3.bak-*")) == []

    def test_idempotent_second_apply(self, palace):
        first = wing_migrate.apply_migration(
            palace_dir=palace, profile="cthugha", dry_run=False
        )
        assert first.orphan_rows_after == 0
        second = wing_migrate.apply_migration(
            palace_dir=palace, profile="cthugha", dry_run=False
        )
        assert second.error is None
        assert second.actions == ()
        assert second.rows_moved == 0
        assert second.orphan_rows_before == 0

    def test_clean_store_is_noop(self, tmp_path, monkeypatch):
        d = tmp_path / "clean"
        d.mkdir()
        con = sqlite3.connect(str(d / "chroma.sqlite3"))
        con.execute(
            "CREATE TABLE embedding_metadata ("
            " id INTEGER PRIMARY KEY, embedding_id TEXT, key TEXT,"
            " string_value TEXT, integer_value INTEGER,"
            " float_value REAL, bool_value INTEGER)"
        )
        con.execute(
            "INSERT INTO embedding_metadata"
            " (id, embedding_id, key, string_value) VALUES (1,'e1','wing','general')"
        )
        con.commit()
        con.close()
        monkeypatch.setattr(wing_orphans, "valid_wings_for_profile", lambda p: VALID)
        result = wing_migrate.apply_migration(
            palace_dir=d, profile="cthugha", dry_run=False
        )
        assert result.error is None
        assert result.dry_run is True        # early return: nothing to do
        assert result.actions == ()

    def test_missing_db_is_treated_as_empty_store(self, tmp_path):
        # Documented behaviour (wing_row_counts docstring): a missing store is
        # reported as zero rows, not an error — same as a fresh install.
        missing = tmp_path / "nope"
        missing.mkdir()
        result = wing_migrate.apply_migration(
            palace_dir=missing, profile="cthugha", dry_run=True
        )
        assert result.error is None
        assert result.total_rows == 0
        assert result.actions == ()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
class TestCLI:
    def test_main_dry_run_json(self, palace, capsys):
        rc = wing_migrate.main(
            ["--palace-dir", str(palace), "--profile", "cthugha", "--json"]
        )
        assert rc == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["dry_run"] is True
        assert payload["orphan_rows_before"] == 5
        assert payload["error"] is None

    def test_main_apply(self, palace, capsys):
        rc = wing_migrate.main(
            ["--palace-dir", str(palace), "--profile", "cthugha", "--apply", "--json"]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["dry_run"] is False
        assert payload["orphan_rows_after"] == 0
