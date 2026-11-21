"""Stream-parse an Apple Health export.zip into SQLite. See ADR-0001/0003/0005/0006.

Validated against a real tr_TR export (HealthKit Export Version 14, ~2.4M records):
- main XML is NOT always "export.xml" — a Turkish export names it "dışa aktarılan.xml".
  We locate it by content (root <HealthData>), not by filename.
- type identifiers and category value enums are English regardless of locale.
- decimals use "." even in tr_TR; sourceName may be localized/personal.
"""

from __future__ import annotations

import hashlib
import zipfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from .db import connect, init_schema

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
    return float(v) if v else None


def _stat(el) -> dict:
    """One <WorkoutStatistics> element as a dict (values as floats, unit raw)."""
    return {
        "unit": el.get("unit"),
        **{k: _num(el.get(k)) for k in _STAT_FIELDS},
        "start": el.get("startDate"),
        "end": el.get("endDate"),
    }


def _minutes(st: dict) -> float | None:
    try:
        fmt = "%Y-%m-%d %H:%M:%S %z"
        return (datetime.strptime(st["end"], fmt) - datetime.strptime(st["start"], fmt)).total_seconds() / 60
    except (TypeError, ValueError):
        return None


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
    sums, starts, ends = vals("sum"), vals("start"), vals("end")
    return {
        "unit": parts[0]["unit"],  # ponytail: segments of one workout share a unit in practice
        "sum": sum(sums) if sums else None,
        "average": average,
        "minimum": min(vals("minimum"), default=None),
        "maximum": max(vals("maximum"), default=None),
        "start": min(starts) if starts else None,
        "end": max(ends) if ends else None,
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
    if a.get("totalDistance") and not any(t.startswith("HKQuantityTypeIdentifierDistance") for t in stats):
        stats["totalDistance"] = {"unit": a.get("totalDistanceUnit"), "sum": float(a["totalDistance"])}
    if a.get("totalEnergyBurned") and "HKQuantityTypeIdentifierActiveEnergyBurned" not in stats:
        stats["totalEnergyBurned"] = {"unit": a.get("totalEnergyBurnedUnit"), "sum": float(a["totalEnergyBurned"])}

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
                float(a["duration"]) if a.get("duration") else None,
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


def ingest(zip_path: Path, db: Path | None = None, *, progress=print) -> dict[str, int]:
    zip_path = Path(zip_path).expanduser()
    conn = connect(db, create=True)
    init_schema(conn)
    # Ingest-only speedups; safe because it's a rebuildable derived store. ADR-0003.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")

    counts = dict.fromkeys(_SQL, 0)
    batches: dict[str, list[tuple]] = {k: [] for k in _SQL}

    def flush() -> None:
        for kind, rows in batches.items():
            if rows:
                conn.executemany(_SQL[kind], rows)
                rows.clear()
        conn.commit()

    with zipfile.ZipFile(zip_path) as zf:
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
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('source_zip',?)", (str(zip_path),))
    conn.commit()
    conn.close()
    return counts
