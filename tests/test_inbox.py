"""Inbox mode: pick the newest complete export from a drop folder, skip what was ingested."""

import fcntl
import os
import sqlite3
import zipfile
from xml.etree import ElementTree as ET

import pytest
from fixtures.synthetic import write_export

from apple_health_export_mcp import inbox
from apple_health_export_mcp.cli import main
from apple_health_export_mcp.ingest import ingest


def _meta(db, k):
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _zip(path, days, mtime):
    write_export(path, days)
    os.utime(path, (mtime, mtime))
    return path


def _run(box, db, *extra):
    return main(["--inbox", str(box), "--db", str(db), "--settle", "0", *extra])


@pytest.fixture
def box(tmp_path):
    d = tmp_path / "HealthInbox"
    d.mkdir()
    return d


# --- ingest records which file it ingested -------------------------------------------


def test_ingest_records_the_zip_digest(tmp_path):
    z = write_export(tmp_path / "export.zip", 1)
    ingest(z, tmp_path / "h.db", progress=lambda *_: None)
    assert _meta(tmp_path / "h.db", "source_sha256") == inbox.sha256(z)


def test_failed_ingest_leaves_no_digest(tmp_path):
    db = tmp_path / "h.db"
    ingest(write_export(tmp_path / "ok.zip", 1), db, progress=lambda *_: None)
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:  # valid zip, XML cut off mid-record
        zf.writestr("apple_health_export/export.xml", '<HealthData locale="en_US">\n <Record type="X"')
    with pytest.raises(ET.ParseError):
        ingest(bad, db, progress=lambda *_: None)
    # The previous digest is gone too: a retry of either file must ingest again.
    assert _meta(db, "source_sha256") is None


# --- picking a candidate -------------------------------------------------------------


def test_newest_zip_is_picked(box):
    _zip(box / "old.zip", 1, 1_000)
    new = _zip(box / "new.zip", 1, 2_000)
    assert inbox.pick(box, settle=0) == (new, None)


def test_empty_inbox_is_not_ready(box):
    path, why = inbox.pick(box, settle=0)
    assert path is None and "no export" in why


def test_icloud_placeholder_is_not_downloaded_yet(box):
    (box / ".export.zip.icloud").write_bytes(b"bplist")
    path, why = inbox.pick(box, settle=0)
    assert path is None and "not downloaded yet" in why


def test_placeholder_newer_than_the_local_zip_wins(box):
    _zip(box / "old.zip", 1, 1_000)
    ph = box / ".new.zip.icloud"
    ph.write_bytes(b"bplist")
    os.utime(ph, (2_000, 2_000))
    path, why = inbox.pick(box, settle=0)
    assert path is None and "not downloaded yet" in why


def test_hidden_partial_copies_are_ignored(box):
    z = _zip(box / "export.zip", 1, 1_000)
    tmp = box / ".export.zip.sb-1234"
    tmp.write_bytes(b"partial")
    os.utime(tmp, (2_000, 2_000))
    assert inbox.pick(box, settle=0) == (z, None)


def test_file_still_growing_is_not_ready(box):
    z = _zip(box / "export.zip", 1, 1_000)

    def copying(_seconds):  # the copy writes more bytes while we wait
        with open(z, "ab") as f:
            f.write(b"more")

    path, why = inbox.pick(box, settle=2, sleep=copying)
    assert path is None and "still being written" in why


def test_non_zip_is_not_ready(box):
    (box / "export.zip").write_bytes(b"not a zip")
    path, why = inbox.pick(box, settle=0)
    assert path is None and "not a complete zip" in why


# --- the CLI -------------------------------------------------------------------------


def test_inbox_ingests_only_the_newest_zip(box, tmp_path, capsys):
    _zip(box / "old.zip", 1, 1_000)
    new = _zip(box / "new.zip", 2, 2_000)
    db = tmp_path / "h.db"
    assert _run(box, db) == 0
    assert _meta(db, "source_zip") == str(new)
    assert "20 records" in capsys.readouterr().out  # 2 days x 10 records, not 3 days


def test_second_run_on_the_same_zip_does_nothing(box, tmp_path, capsys):
    _zip(box / "export.zip", 1, 1_000)
    db = tmp_path / "h.db"
    assert _run(box, db) == 0
    os.utime(db, (5, 5))
    assert _run(box, db) == 0
    assert "nothing new" in capsys.readouterr().out.lower()
    assert db.stat().st_mtime == 5


def test_manual_ingest_counts_as_ingested(box, tmp_path, capsys):
    z = _zip(box / "export.zip", 1, 1_000)
    db = tmp_path / "h.db"
    assert main([str(z), "--db", str(db)]) == 0
    assert _run(box, db) == 0
    assert "nothing new" in capsys.readouterr().out.lower()


def test_not_ready_exits_zero_without_creating_a_db(box, tmp_path, capsys):
    (box / ".export.zip.icloud").write_bytes(b"bplist")
    db = tmp_path / "h.db"
    assert _run(box, db) == 0
    assert "not downloaded yet" in capsys.readouterr().out
    assert not db.exists()


def test_broken_export_exits_nonzero_with_a_one_line_error(box, tmp_path, capsys):
    with zipfile.ZipFile(box / "export.zip", "w") as zf:
        zf.writestr("readme.txt", "no health data here")
    assert _run(box, tmp_path / "h.db") == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and "Traceback" not in err


def test_keep_prunes_older_zips_after_success(box, tmp_path):
    _zip(box / "a.zip", 1, 1_000)
    _zip(box / "b.zip", 1, 2_000)
    new = _zip(box / "c.zip", 1, 3_000)
    assert _run(box, tmp_path / "h.db", "--keep", "1") == 0
    assert sorted(box.glob("*.zip")) == [new]


def test_without_keep_nothing_is_deleted(box, tmp_path):
    _zip(box / "a.zip", 1, 1_000)
    _zip(box / "b.zip", 1, 2_000)
    assert _run(box, tmp_path / "h.db") == 0
    assert len(list(box.glob("*.zip"))) == 2


def test_keep_does_not_prune_when_the_ingest_fails(box, tmp_path):
    _zip(box / "a.zip", 1, 1_000)
    bad = box / "b.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("readme.txt", "no health data here")
    os.utime(bad, (2_000, 2_000))
    assert _run(box, tmp_path / "h.db", "--keep", "1") == 1
    assert len(list(box.glob("*.zip"))) == 2


def test_concurrent_run_exits_zero_without_ingesting(box, tmp_path, capsys):
    _zip(box / "export.zip", 1, 1_000)
    db = tmp_path / "h.db"
    with open(tmp_path / "h.db.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert _run(box, db) == 0
    assert "already running" in capsys.readouterr().out.lower()
    assert not db.exists()


def test_manual_ingest_refuses_to_run_concurrently(tmp_path, capsys):
    z = write_export(tmp_path / "export.zip", 1)
    with open(tmp_path / "h.db.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert main([str(z), "--db", str(tmp_path / "h.db")]) == 1
    assert "already running" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        [],  # neither a zip nor --inbox
        ["export.zip", "--inbox", "dir"],  # both
        ["export.zip", "--keep", "1"],  # --keep needs --inbox
        ["--inbox", "dir", "--keep", "0"],  # keep at least the newest
    ],
)
def test_bad_argument_combinations_are_usage_errors(argv):
    with pytest.raises(SystemExit) as e:
        main(argv)
    assert e.value.code == 2


def test_missing_inbox_folder_is_an_error(tmp_path, capsys):
    assert _run(tmp_path / "nope", tmp_path / "h.db") == 1
    assert capsys.readouterr().err.startswith("error:")
