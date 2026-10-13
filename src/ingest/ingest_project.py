# clin_longsum - tiered longitudinal clinical summarization
# Copyright (C) 2026 Eukairos
# SPDX-License-Identifier: AGPL-3.0-only
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of version 3 of the GNU Affero General Public License as published
# by the Free Software Foundation. This program is distributed WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE. See the GNU Affero General Public License for details.

"""
ingest_project.py
=================
Project ingested admissions from Postgres into the file cohort every stage reads
(cohort.py -> cohort_admissions.csv). Same DB-as-truth / CSV-as-cache split as
holdout.promote() -> promoted_admissions.csv.

APPEND-ONLY, byte-preserving: existing rows are never parsed and rewritten (a pandas
round-trip would reformat day-first dates, 0 vs 0.0 flags, TRUE vs True). New rows
are appended in the file's own column order, newline style and is_holdout casing,
via a temp copy + os.replace, so a reader never sees a half-written file.
Idempotent: a hadm_id already in the file is skipped, so re-running heals a run that
committed to the DB but crashed before projecting.

Files:
  cohort_admissions.csv  append ingested rows                  (all stages)
  cohort_holdout.csv     append ingested rows, if the file exists (replay tooling)
  cohort_patients.csv    refresh n_admissions, add new subjects  (a4 only)

Dates are written ISO 8601. cohort.py parses with format="mixed", dayfirst=True;
pandas 2.x leaves ISO untouched under that call, pandas 3.x swaps day and month.
assert_iso_parse_safe() refuses to project on a pandas that would mis-read them.

Caller holds the fold-in advisory lock (fold-in read-modify-writes cohort files).
"""

from __future__ import annotations

import csv
import os
import shutil
from datetime import date, datetime
from pathlib import Path

import pandas as pd

import config

ADMISSIONS_CSV = config.INTERIM_DIR / "cohort_admissions.csv"
HOLDOUT_CSV = config.INTERIM_DIR / "cohort_holdout.csv"
PATIENTS_CSV = config.INTERIM_DIR / "cohort_patients.csv"
# Read by a2_build_cohort._refuse_if_ingest_active(): once present, a2 will not rebuild
# the cohort from SOURCE_CSV (that would drop every projected EHR admission).
INGEST_MARKER = config.INTERIM_DIR / "INGEST_ACTIVE"
# a2's column order, used only if a file must be created from scratch.
ADMISSION_COLS = [
    "hadm_id", "subject_id", "admit_seq", "admittime", "dischtime",
    "service", "admission_type", "discharge_location", "hospital_expire_flag",
    "is_holdout", "bhc_text", "bhc_delta", "dedup_jaccard_to_prev",
]
PATIENT_COLS = ["subject_id", "n_admissions", "conditions_json"]


def assert_iso_parse_safe() -> None:
    probe = pd.to_datetime(pd.Series(["2175-06-05 10:30:00"]), format="mixed",
                           dayfirst=True, errors="coerce").iloc[0]
    if pd.isna(probe) or (probe.month, probe.day) != (6, 5):
        raise SystemExit(
            f"pandas {pd.__version__} parses ISO dates with day and month swapped under "
            f"cohort.py's to_datetime(format='mixed', dayfirst=True) -> got {probe}. Ingested "
            f"rows would be mis-dated. Run under the project env (pandas 2.x) or fix cohort.py first.")


def _key(v) -> int | None:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


def _sniff(path: Path, key: str = "hadm_id") -> dict:
    """Header, newline, trailing-newline flag, is_holdout casing and key set."""
    with path.open("rb") as fb:
        head = fb.read(65536)
        fb.seek(0, os.SEEK_END)
        size = fb.tell()
        ends_nl = True
        if size:
            fb.seek(-1, os.SEEK_END)
            ends_nl = fb.read(1) == b"\n"
    newline = "\r\n" if b"\r\n" in head else "\n"
    keys: set[int] = set()
    true_tok = "True"
    with path.open("r", encoding="utf-8", newline="") as fh:
        rd = csv.DictReader(fh)
        header = list(rd.fieldnames or [])
        seen_tok = False
        for r in rd:
            k = _key(r.get(key))
            if k is not None:
                keys.add(k)
            if not seen_tok and "is_holdout" in r and (r["is_holdout"] or "").strip():
                sample = r["is_holdout"].strip()
                true_tok = "TRUE" if sample.isupper() else ("true" if sample.islower() else "True")
                seen_tok = True
    return {"header": header, "newline": newline, "ends_nl": ends_nl,
            "true_tok": true_tok, "keys": keys}


def _fmt(col: str, v, true_tok: str) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if col == "is_holdout":
        return true_tok if bool(v) else {"TRUE": "FALSE", "true": "false"}.get(true_tok, "False")
    if col == "hospital_expire_flag":
        return "1" if bool(v) else "0"
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    return str(v)


def append_rows(path: Path, rows: list[dict], default_cols: list[str],
                key: str = "hadm_id", create: bool = True, dry_run: bool = False) -> int:
    """Append rows whose `key` is not yet in the file. Returns the number appended."""
    if not rows:
        return 0
    if path.exists():
        s = _sniff(path, key)
        if key not in s["header"]:
            raise SystemExit(f"{path} has no {key!r} column -- refusing to append.")
    elif create:
        s = {"header": list(default_cols), "newline": "\n", "ends_nl": True,
             "true_tok": "True", "keys": set()}
    else:
        return 0
    new = [r for r in rows if _key(r.get(key)) not in s["keys"]]
    if not new or dry_run:
        return len(new)
    missing = [c for c in s["header"] if c not in new[0]]
    if missing:
        print(f"[ingest] note: {path.name} columns not supplied by the ingest, written empty: {missing}")
    tmp = path.with_name(path.name + ".ingest_tmp")
    if path.exists():
        shutil.copyfile(path, tmp)
    with tmp.open("a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator=s["newline"])
        if not path.exists():
            w.writerow(s["header"])
        elif not s["ends_nl"]:
            fh.write(s["newline"])
        for r in new:
            w.writerow([_fmt(c, r.get(c), s["true_tok"]) for c in s["header"]])
    os.replace(tmp, path)
    return len(new)


def refresh_patients(path: Path, counts: dict[int, int], dry_run: bool = False) -> tuple[int, int]:
    """Set n_admissions for the given subjects and add missing ones (conditions_json '[]').
    Other rows and columns are copied through as strings. Returns (updated, added)."""
    if not counts:
        return 0, 0
    if path.exists():
        with path.open("rb") as fb:
            newline = "\r\n" if b"\r\n" in fb.read(65536) else "\n"
        with path.open("r", encoding="utf-8", newline="") as fh:
            rd = csv.DictReader(fh)
            header = list(rd.fieldnames or PATIENT_COLS)
            rows = list(rd)
    else:
        header, rows, newline = list(PATIENT_COLS), [], "\n"
    seen, updated = set(), 0
    for r in rows:
        k = _key(r.get("subject_id"))
        if k in counts:
            seen.add(k)
            if str(r.get("n_admissions", "")).strip() != str(counts[k]):
                r["n_admissions"] = str(counts[k])
                updated += 1
    added = [k for k in sorted(counts) if k not in seen]
    for k in added:
        r = {c: "" for c in header}
        r.update({"subject_id": str(k), "n_admissions": str(counts[k])})
        if "conditions_json" in header:
            r["conditions_json"] = "[]"
        rows.append(r)
    if dry_run or (not updated and not added):
        return updated, len(added)
    tmp = path.with_name(path.name + ".ingest_tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=header, lineterminator=newline)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)
    return updated, len(added)


def csv_hadms_by_subject(path: Path, subject_ids) -> dict[int, set[int]]:
    want = {int(s) for s in subject_ids}
    out: dict[int, set[int]] = {}
    if not path.exists() or not want:
        return out
    with path.open("r", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            sid, hadm = _key(r.get("subject_id")), _key(r.get("hadm_id"))
            if sid in want and hadm is not None:
                out.setdefault(sid, set()).add(hadm)
    return out


def inconsistent_subjects(conn, subject_ids, path: Path = ADMISSIONS_CSV) -> dict[int, dict]:
    """Subjects whose admission set differs between the DB and the CSV. Planning on
    them would compute admit_seq / bhc_delta from a record the pipeline does not see."""
    import ingest_db
    db = ingest_db.hadms_by_subject(conn, subject_ids)
    fs = csv_hadms_by_subject(path, subject_ids)
    bad = {}
    for sid in {int(s) for s in subject_ids}:
        d, f = db.get(sid, set()), fs.get(sid, set())
        if d != f:
            bad[sid] = {"db_only": sorted(d - f), "csv_only": sorted(f - d)}
    return bad


def project(conn, dry_run: bool = False, admissions_csv: Path = ADMISSIONS_CSV,
            holdout_csv: Path = HOLDOUT_CSV, patients_csv: Path = PATIENTS_CSV) -> dict:
    """DB -> cohort files for every ingested admission. Returns counts + mismatches."""
    import ingest_db
    assert_iso_parse_safe()
    rows = ingest_db.ingested_rows(conn)
    marker = admissions_csv.parent / INGEST_MARKER.name
    if rows and not dry_run and not marker.exists():
        marker.write_text("clin_longsum production ingest has projected EHR admissions into "
                          "cohort_admissions.csv. a2_build_cohort.py refuses to rebuild while "
                          "this file exists.\n", encoding="utf-8")
    n_adm = append_rows(admissions_csv, rows, ADMISSION_COLS, dry_run=dry_run)
    n_hold = append_rows(holdout_csv, [r for r in rows if r.get("is_holdout")],
                         ADMISSION_COLS, create=False, dry_run=dry_run)
    sids = {int(r["subject_id"]) for r in rows}
    upd, add = refresh_patients(patients_csv, ingest_db.patient_counts(conn, sids), dry_run)
    mism = {} if dry_run else inconsistent_subjects(conn, sids, admissions_csv)
    return {"appended_admissions": n_adm, "appended_holdout": n_hold,
            "patients_updated": upd, "patients_added": add, "mismatches": mism}
