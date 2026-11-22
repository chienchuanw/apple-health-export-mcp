"""Stream-parse an Apple Health export.zip into SQLite. See ADR-0001/0003/0005/0006.

Validated against a real tr_TR export (HealthKit Export Version 14, ~2.4M records):
- main XML is NOT always "export.xml" — a Turkish export names it "dışa aktarılan.xml".
  We locate it by content (root <HealthData>), not by filename.
- type identifiers and category value enums are English regardless of locale.
- decimals use "." even in tr_TR; sourceName may be localized/personal.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import zipfile
from collections.abc import Iterator
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import BinaryIO
from xml.etree import ElementTree as ET

from .db import connect, connect_readonly, init_schema

BATCH = 10_000


def _find_main_xml(zf: zipfile.ZipFile) -> str:
    """Return the zip entry name of the main HealthData XML (locale-independent)."""
    xmls = [n for n in zf.namelist() if n.lower().endswith(".xml")]
    # export_cda.xml is the clinical summary — never the main file.
    candidates = [n for n in xmls if not n.lower().endswith("export_cda.xml")]
    for name in candidates or xmls:
        with zf.open(name) as f:
            head = f.read(512)
        if b"<HealthData" in head or b"DOCTYPE HealthData" in head:
            return name
    raise ValueError("No HealthData XML found in archive — is this an Apple Health export?")


def _key(*parts: str | None) -> bytes:
    """Content hash → idempotent upsert key (records carry no UUID). ADR-0005."""
    return hashlib.blake2b("|".join(p or "" for p in parts).encode(), digest_size=16).digest()


_STAT_FIELDS = ("sum", "average", "minimum", "maximum")


def _num(v: str | None) -> float | None:
    """Parse a numeric attribute; a missing or malformed value is None, never an abort."""
    try:
        return float(v) if v else None
    except ValueError:
        return None


_TS = "%Y-%m-%d %H:%M:%S %z"


def _when(ts: str | None) -> datetime | None:
    try:
        return datetime.strptime(ts, _TS) if ts else None
    except ValueError:
        return None


def _stat(el) -> dict:
    """One <WorkoutStatistics> element as a dict (values as floats, unit raw)."""
    return {
        "unit": el.get("unit"),
        **{k: _num(el.get(k)) for k in _STAT_FIELDS},
        "start": el.get("startDate"),
        "end": el.get("endDate"),
    }


def _minutes(st: dict) -> float | None:
    start, end = _when(st["start"]), _when(st["end"])
    return (end - start).total_seconds() / 60 if start and end else None


def _span(stamps: list[str], pick) -> str | None:
    """Earliest/latest timestamp by actual time (offsets may differ), else by text."""
    if not stamps:
        return None
    parsed = [_when(t) for t in stamps]
    if all(parsed):
        return stamps[parsed.index(pick(parsed))]
    return pick(stamps)


def _merge(parts: list[dict]) -> dict:
    """Combine one type's statistics from several <WorkoutActivity> segments.

    Totals add, extremes take min/max, the average is weighted by segment time
    (plain mean if a segment's time is unknown). Only used when the Workout has no
    direct, whole-workout statistic for that type.
    """
    if len(parts) == 1:
        return parts[0]

    def vals(k):
        return [p[k] for p in parts if p[k] is not None]

    avgs = [(p["average"], _minutes(p)) for p in parts if p["average"] is not None]
    if avgs and all(m for _, m in avgs):
        average = sum(a * m for a, m in avgs) / sum(m for _, m in avgs)
    else:
        average = sum(a for a, _ in avgs) / len(avgs) if avgs else None
    sums = vals("sum")
    units = {p["unit"] for p in parts}
    return {
        # Mixed units can't be added raw; NULL normalizes to a NULL total (ADR-0011).
        "unit": units.pop() if len(units) == 1 else None,
        "sum": sum(sums) if sums else None,
        "average": average,
        "minimum": min(vals("minimum"), default=None),
        "maximum": max(vals("maximum"), default=None),
        "start": _span(vals("start"), min),
        "end": _span(vals("end"), max),
    }


def _workout_stats(elem, key: bytes) -> list[tuple]:
    """Statistics rows for one <Workout>; must run before elem.clear(). ADR-0011.

    Direct <WorkoutStatistics> children are whole-workout values and win over the
    per-segment ones nested in <WorkoutActivity> (iOS 16+). Older exports have no
    statistics, only totalDistance/totalEnergyBurned attributes — used as a fallback.
    """
    nested: dict[str, list[dict]] = {}
    for ws in elem.iterfind("WorkoutActivity/WorkoutStatistics"):
        nested.setdefault(ws.get("type", ""), []).append(_stat(ws))
    stats = {t: _merge(parts) for t, parts in nested.items()}
    for ws in elem.iterfind("WorkoutStatistics"):
        stats[ws.get("type", "")] = _stat(ws)

    a = elem.attrib
    dist, energy = _num(a.get("totalDistance")), _num(a.get("totalEnergyBurned"))
    if dist is not None and not any(t.startswith("HKQuantityTypeIdentifierDistance") for t in stats):
        stats["totalDistance"] = {"unit": a.get("totalDistanceUnit"), "sum": dist}
    if energy is not None and "HKQuantityTypeIdentifierActiveEnergyBurned" not in stats:
        stats["totalEnergyBurned"] = {"unit": a.get("totalEnergyBurnedUnit"), "sum": energy}

    start, end = a.get("startDate"), a.get("endDate")
    return [
        (key, t, st["unit"], *(st.get(k) for k in _STAT_FIELDS), st.get("start") or start, st.get("end") or end)
        for t, st in stats.items()
    ]


def _iter_rows(stream) -> Iterator[tuple[str, tuple]]:
    """Yield ('record'|'workout'|'workout_stat', row_tuple) per relevant element, freeing memory."""
    for _event, elem in ET.iterparse(stream, events=("end",)):
        tag = elem.tag
        if tag == "Record":
            a = elem.attrib
            start = a.get("startDate", "")
            row = (
                _key(a.get("type"), a.get("sourceName"), start, a.get("endDate"), a.get("value"), a.get("unit")),
                a.get("type", ""),
                a.get("sourceName"),
                a.get("unit"),
                a.get("value"),
                start,
                a.get("endDate"),
                a.get("creationDate"),
                start[:10],
            )
            yield "record", row
            elem.clear()
        elif tag == "Workout":
            a = elem.attrib
            start = a.get("startDate", "")
            key = _key(a.get("workoutActivityType"), a.get("sourceName"), start, a.get("endDate"), a.get("duration"))
            row = (
                key,
                a.get("workoutActivityType", ""),
                _num(a.get("duration")),
                a.get("durationUnit"),
                a.get("sourceName"),
                start,
                a.get("endDate"),
                a.get("creationDate"),
                start[:10],
            )
            yield "workout", row
            for stat_row in _workout_stats(elem, key):
                yield "workout_stat", stat_row
            elem.clear()
        elif tag in ("Correlation", "ActivitySummary", "Me", "ExportDate"):
            elem.clear()  # not stored in v1; free memory


_REC_SQL = (
    "INSERT OR IGNORE INTO record(key,type,source,unit,value,start,end,created,local_date) VALUES (?,?,?,?,?,?,?,?,?)"
)
_WK_SQL = (
    "INSERT OR IGNORE INTO workout"
    "(key,activity,duration,duration_unit,source,start,end,created,local_date)"
    " VALUES (?,?,?,?,?,?,?,?,?)"
)
_STAT_SQL = (
    "INSERT OR IGNORE INTO workout_stat"
    "(workout_key,type,unit,sum,average,minimum,maximum,start,end)"
    " VALUES (?,?,?,?,?,?,?,?,?)"
)
_SQL = {"record": _REC_SQL, "workout": _WK_SQL, "workout_stat": _STAT_SQL}


# What the last *successful* ingest read (ADR-0015). Both keys are cleared in the first
# commit and written after the last batch, so they only ever describe a complete run.
_DIGEST = "source_sha256"  # identity of the zip's content
_STAT = "source_stat"  # path|size|mtime_ns: a cache that spares re-hashing an unchanged file


def _hash(f: BinaryIO) -> str:
    """Hex SHA-256 of an open file from its current position, in 1 MB chunks."""
    h = hashlib.sha256()
    while chunk := f.read(1 << 20):
        h.update(chunk)
    return h.hexdigest()


def sha256(path: Path) -> str:
    """Hex SHA-256 of a file (an export can be gigabytes, so it is streamed)."""
    with open(path, "rb") as f:
        return _hash(f)


def _stat_key(path: Path, st: os.stat_result | None = None) -> str:
    st = st or path.stat()
    return f"{path.resolve()}|{st.st_size}|{st.st_mtime_ns}"


def is_ingested(zip_path: Path, db: Path) -> bool:
    """Was `zip_path` the last file fully ingested into `db`?

    An unchanged path, size and mtime answer without reading the file. Otherwise the
    content hash decides: iCloud can rewrite the mtime of a file it re-downloads.
    """
    if not db.exists():
        return False
    conn = connect_readonly(db)
    try:
        meta = dict(conn.execute("SELECT k, v FROM meta WHERE k IN (?, ?)", (_DIGEST, _STAT)).fetchall())
    except sqlite3.OperationalError:  # a DB older than the meta table
        meta = {}
    finally:
        conn.close()
    if _DIGEST not in meta:
        return False
    return meta.get(_STAT) == _stat_key(zip_path) or sha256(zip_path) == meta[_DIGEST]


def ingest(zip_path: Path, db: Path | None = None, *, progress=print) -> dict[str, int]:
    """Parse `zip_path` into the DB; return row counts per table.

    On success `meta` records the zip's digest and stat, so `--inbox` can skip a file
    that was already ingested (ADR-0015). Both are taken from the same open file that
    is parsed, so a file replaced mid-run is never marked as ingested.
    """
    zip_path = Path(zip_path).expanduser()
    # Opened before the DB is touched: a missing file leaves the previous record intact.
    with open(zip_path, "rb") as fh:
        # Identity of the bytes about to be parsed, not of whatever the path holds later.
        source = ((_DIGEST, _hash(fh)), (_STAT, _stat_key(zip_path, os.fstat(fh.fileno()))))
        fh.seek(0)
        conn = connect(db, create=True)
        init_schema(conn)
        # Ingest-only speedups; safe because it's a rebuildable derived store. ADR-0003.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=OFF")
        # Keys are content hashes (random order), so inserts hit B-tree pages all over;
        # a 32 MB page cache (default 2 MB) cuts ingest time ~15% at no peak-RSS cost.
        conn.execute("PRAGMA cache_size=-32768")
        # Commit right away so the -wal file exists for the whole ingest: connect_readonly
        # reads "no -wal" as "no writer" and may then open the DB immutable.
        conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('source_zip',?)", (str(zip_path),))
        conn.execute("DELETE FROM meta WHERE k IN (?, ?)", (_DIGEST, _STAT))
        conn.commit()
        counts = _parse(fh, conn, progress)
    conn.executemany("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", source)
    conn.commit()
    conn.execute("PRAGMA synchronous=NORMAL")
    # WAL was an ingest speedup only. Leave a single self-contained file: the read-only
    # server can't create -wal/-shm, and Linux SQLite deletes them on close. Best-effort:
    # with a server connection attached the switch is refused ("database is locked"),
    # but every row is already committed and connect_readonly copes with a WAL DB.
    with suppress(sqlite3.OperationalError):
        conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    return counts


def _parse(fh: BinaryIO, conn: sqlite3.Connection, progress) -> dict[str, int]:
    """Stream the export's main XML from open zip file `fh` into `conn` in batches."""
    counts = dict.fromkeys(_SQL, 0)
    batches: dict[str, list[tuple]] = {k: [] for k in _SQL}

    def flush() -> None:
        for kind, rows in batches.items():
            if rows:
                conn.executemany(_SQL[kind], rows)
                rows.clear()
        conn.commit()

    with zipfile.ZipFile(fh) as zf:
        main = _find_main_xml(zf)
        progress(f"Reading {main} ...")
        with zf.open(main) as stream:
            for kind, row in _iter_rows(stream):
                batches[kind].append(row)
                counts[kind] += 1
                if kind != "workout_stat" and (counts["record"] + counts["workout"]) % BATCH == 0:
                    flush()
                    progress(f"  {counts['record']:,} records, {counts['workout']:,} workouts")
    flush()
    return counts
