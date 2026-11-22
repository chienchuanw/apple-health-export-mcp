"""Re-ingest rebuilds the DB and swaps it in, so edits and deletions on the phone show up."""

import os
import stat
import zipfile
from xml.etree import ElementTree as ET

import pytest
from fixtures.synthetic import write_export

from apple_health_export_mcp.cli import main
from apple_health_export_mcp.db import connect_readonly
from apple_health_export_mcp.ingest import ShrinkRefused, ingest

STEPS = "HKQuantityTypeIdentifierStepCount"
WEIGHT = "HKQuantityTypeIdentifierBodyMass"


def _rec(type_, value, day, unit="count"):
    return (
        f' <Record type="{type_}" sourceName="Phone" unit="{unit}" '
        f'startDate="2026-06-{day:02d} 08:00:00 +0300" endDate="2026-06-{day:02d} 08:05:00 +0300" value="{value}"/>\n'
    )


def _export(path, records):
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<HealthData locale="en_US">\n' + "".join(records) + "</HealthData>\n"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("apple_health_export/export.xml", xml)
    return path


BASE = [_rec(STEPS, 1000 + d, d) for d in range(1, 21)]  # 20 records kept by both exports
# Export A also has a weight entry that is later corrected and a bogus one that is deleted.
A = BASE + [_rec(WEIGHT, "80", 21, "kg"), _rec(WEIGHT, "800", 22, "kg")]
B = BASE + [_rec(WEIGHT, "79.5", 21, "kg")]  # 21 of 22 records: above the shrink guard


def _quiet(*_):
    pass


def _weights(db):
    conn = connect_readonly(db)
    try:
        return sorted(r[0] for r in conn.execute("SELECT value FROM record WHERE type=?", (WEIGHT,)))
    finally:
        conn.close()


def _count(db):
    conn = connect_readonly(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM record").fetchone()[0]
    finally:
        conn.close()


def test_reingest_replaces_rows_edited_or_deleted_on_the_phone(tmp_path):
    db = tmp_path / "h.db"
    ingest(_export(tmp_path / "a.zip", A), db, progress=_quiet)
    ingest(_export(tmp_path / "b.zip", B), db, progress=_quiet)
    assert _weights(db) == ["79.5"]  # the bogus 800 is gone, the corrected value is there once
    assert _count(db) == 21


def test_merge_keeps_the_old_accumulating_behaviour(tmp_path):
    db = tmp_path / "h.db"
    ingest(_export(tmp_path / "a.zip", A), db, progress=_quiet)
    ingest(_export(tmp_path / "b.zip", B), db, progress=_quiet, merge=True)
    assert _weights(db) == ["79.5", "80", "800"]


def test_failed_build_leaves_the_existing_db_untouched(tmp_path):
    db = tmp_path / "h.db"
    ingest(_export(tmp_path / "a.zip", A), db, progress=_quiet)
    before = db.read_bytes()
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:  # XML cut off mid-record: fails during the parse
        zf.writestr(
            "apple_health_export/export.xml", '<HealthData locale="en_US">\n' + "".join(B) + ' <Record type="X"'
        )
    with pytest.raises(ET.ParseError):
        ingest(bad, db, progress=_quiet)
    assert db.read_bytes() == before
    assert _count(db) == 22
    assert not list(tmp_path.glob("h.db.building*"))


def test_much_smaller_export_is_not_swapped_in(tmp_path):
    db = tmp_path / "h.db"
    ingest(write_export(tmp_path / "big.zip", 4), db, progress=_quiet)  # 40 records
    before = db.read_bytes()
    with pytest.raises(ShrinkRefused, match="--merge"):
        ingest(write_export(tmp_path / "small.zip", 1), db, progress=_quiet)  # 10 records
    assert db.read_bytes() == before
    assert (tmp_path / "h.db.building").exists()  # kept for inspection


def test_force_swaps_in_a_much_smaller_export(tmp_path):
    db = tmp_path / "h.db"
    ingest(write_export(tmp_path / "big.zip", 4), db, progress=_quiet)
    ingest(write_export(tmp_path / "small.zip", 1), db, progress=_quiet, force=True)
    assert _count(db) == 10
    assert not (tmp_path / "h.db.building").exists()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_old_sidecar_files_do_not_survive_the_swap(tmp_path, suffix):
    db = tmp_path / "h.db"
    ingest(_export(tmp_path / "a.zip", A), db, progress=_quiet)
    stale = db.with_name(db.name + suffix)
    stale.write_bytes(b"stale sidecar from an older DB")
    ingest(_export(tmp_path / "b.zip", B), db, progress=_quiet)
    assert not stale.exists()
    assert _weights(db) == ["79.5"]


def test_no_temp_files_are_left_after_a_rebuild(tmp_path):
    db = tmp_path / "h.db"
    ingest(_export(tmp_path / "a.zip", A), db, progress=_quiet)
    ingest(_export(tmp_path / "b.zip", B), db, progress=_quiet)
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("h.db")) in (["h.db"], ["h.db", "h.db-shm"])
    assert not list(tmp_path.glob("h.db.building*"))


def test_swapped_db_is_owner_only(tmp_path):
    db = tmp_path / "h.db"
    ingest(_export(tmp_path / "a.zip", A), db, progress=_quiet)
    ingest(_export(tmp_path / "b.zip", B), db, progress=_quiet)
    assert stat.S_IMODE(db.stat().st_mode) == 0o600


def test_stale_building_file_from_a_crash_is_replaced(tmp_path):
    db = tmp_path / "h.db"
    (tmp_path / "h.db.building").write_bytes(b"half-written DB from a crash")
    ingest(_export(tmp_path / "a.zip", A), db, progress=_quiet)
    assert _count(db) == 22
    assert not (tmp_path / "h.db.building").exists()


def test_open_reader_keeps_its_snapshot_and_new_readers_see_the_new_db(tmp_path):
    db = tmp_path / "h.db"
    ingest(_export(tmp_path / "a.zip", A), db, progress=_quiet)
    old = connect_readonly(db)
    try:
        ingest(_export(tmp_path / "b.zip", B), db, progress=_quiet)
        assert old.execute("SELECT COUNT(*) FROM record").fetchone()[0] == 22
    finally:
        old.close()
    assert _count(db) == 21


def test_rebuild_of_a_missing_db_creates_it(tmp_path):
    db = tmp_path / "new" / "h.db"
    ingest(_export(tmp_path / "a.zip", A), db, progress=_quiet)
    assert _count(db) == 22
    assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700


# --- CLI -----------------------------------------------------------------------------


def test_cli_shrink_refusal_is_a_one_line_error_naming_the_way_out(tmp_path, capsys):
    db = tmp_path / "h.db"
    assert main([str(write_export(tmp_path / "big.zip", 4)), "--db", str(db)]) == 0
    assert main([str(write_export(tmp_path / "small.zip", 1)), "--db", str(db)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and "--merge" in err and "--force" in err


def test_cli_merge_and_force_flags(tmp_path):
    db = tmp_path / "h.db"
    assert main([str(_export(tmp_path / "a.zip", A)), "--db", str(db)]) == 0
    assert main([str(_export(tmp_path / "b.zip", B)), "--db", str(db), "--merge"]) == 0
    assert _weights(db) == ["79.5", "80", "800"]
    assert main([str(write_export(tmp_path / "small.zip", 1)), "--db", str(db), "--force"]) == 0
    assert _count(db) == 10


def test_cli_merge_with_force_is_a_usage_error(tmp_path):
    with pytest.raises(SystemExit) as e:
        main(["x.zip", "--merge", "--force"])
    assert e.value.code == 2


def test_inbox_rebuilds_too(tmp_path):
    box = tmp_path / "box"
    box.mkdir()
    db = tmp_path / "h.db"
    _export(box / "a.zip", A)
    os.utime(box / "a.zip", (1_000, 1_000))
    assert main(["--inbox", str(box), "--db", str(db), "--settle", "0"]) == 0
    _export(box / "b.zip", B)
    os.utime(box / "b.zip", (2_000, 2_000))
    assert main(["--inbox", str(box), "--db", str(db), "--settle", "0"]) == 0
    assert _weights(db) == ["79.5"]


def test_record_count_baseline_ignores_an_unreadable_old_db(tmp_path):
    """A corrupt old DB is what a rebuild is for: it is replaced, not used as a baseline."""
    db = tmp_path / "h.db"
    db.write_bytes(b"this is not an SQLite database" * 100)
    ingest(_export(tmp_path / "a.zip", A), db, progress=_quiet)
    assert _count(db) == 22
