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
holdout.py
==========
Central holdout helper - the incremental-update simulation.

a2_build_cohort marks the latest admission per patient is_holdout=True (and writes
cohort_holdout.csv). The ESTABLISHED-view stages (b2 ledger, d2 TL;DR, coordinator)
build the record over the NON-holdout span; the held-out admission is the "incoming"
episode surfaced for physician review. Gated by config.HOLDOUT_LAST_ADMISSION so the
behaviour is a single switch, applied identically everywhere.

Holding out the LATEST admission never touches the compacted Prior Admissions tier
(compaction targets the OLDEST span) or the cached per-admission paragraphs, so
enabling it only requires rerunning the cheap stages: b2, d2, the coordinator, d3.

PROMOTION (physician approval -> fold-in): when a physician approves an incoming
admission, the background fold-in records it in the `promotions` TABLE (the source of
truth) and projects promoted_admissions.csv FROM the table as the cache the file-based
readers consume. A promoted admission joins the ESTABLISHED span, so re-running the 
established stages for that subject folds the promoted admission into the record and 
(it was the patient's last admission) leaves them with no incoming. 
The DB is authoritative; the CSV is derived (rebuild via db_persist_promotions.py --rebuild-csv, 
or holdout.project_csv_from_db()).

is_holdout round-trips through CSV as a string ("True"/"False") or bool depending on
pandas inference, so coercion is explicit here - never astype(bool) on a "False"
string (that is truthy).
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

import config

COHORT_CSV = config.INTERIM_DIR / "cohort_admissions.csv"
PROMOTED_CSV = config.INTERIM_DIR / "promoted_admissions.csv"
_ENV_PATH = config.PROJECT_ROOT / ".env"


def _as_bool(s: pd.Series) -> pd.Series:
    """Robust truthiness for an is_holdout column read back from CSV."""
    if s.dtype == bool:
        return s
    return s.astype(str).str.strip().str.lower().isin({"true", "1", "t", "yes"})


def _enabled(df: pd.DataFrame) -> bool:
    return bool(config.HOLDOUT_LAST_ADMISSION) and "is_holdout" in df.columns


# ── Promotion (approval -> fold-in) ──────────────────────────────────────────────

def promoted_hadms() -> set[int]:
    """hadm_ids that have been approved+promoted out of holdout. Empty if none.
    A corrupt/unreadable CSV cache is rebuilt from the authoritative `promotions`
    table."""
    if not PROMOTED_CSV.exists():
        return set()
    try:
        df = pd.read_csv(PROMOTED_CSV, usecols=lambda c: c == "hadm_id")
        return {int(h) for h in df["hadm_id"]}
    except Exception as exc:
        print(f"WARN: {PROMOTED_CSV.name} unreadable ({exc}); rebuilding from the "
              f"authoritative promotions table")
        try:
            project_csv_from_db()
            df = pd.read_csv(PROMOTED_CSV, usecols=lambda c: c == "hadm_id")
            return {int(h) for h in df["hadm_id"]}
        except Exception as exc2:
            print(f"WARN: rebuild failed ({exc2}); treating promotions as EMPTY this run -- "
                  f"repair via db_persist_promotions.py --rebuild-csv")
            return set()


def promote(subject_id: int, hadm_id: int, dry_run: bool = False) -> None:
    """Record an admission as promoted (idempotent). Called by the fold-in on approval.

    DB-AUTHORITATIVE: the `promotions` table is the source of truth. This writes the
    table first (ON CONFLICT DO NOTHING), then projects promoted_admissions.csv FROM the
    table. The CSV is a derived cache the file-based readers consume. Serialized by the fold-in advisory lock; 
    the projection is a full deterministic rewrite, so the CSV is self-consistent with the table.

    dry_run: keep the `promotions` table CLEAN (no DB write) but still append to the CSV
    cache so the downstream dry-run cascade sees the promotion - mirrors the dry-run
    contract (intermediate files mutate; Postgres does not). The next real promote()
    reconciles the CSV back to the table."""
    if dry_run:
        _append_promoted_csv(subject_id, hadm_id)
        return
    conn = _pg_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO promotions (hadm_id, subject_id, promoted_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (hadm_id) DO NOTHING;",
            (int(hadm_id), int(subject_id), datetime.now()))
        conn.commit()
        cur.execute("SELECT subject_id, hadm_id, promoted_at FROM promotions "
                    "ORDER BY promoted_at NULLS FIRST, hadm_id;")
        rows = cur.fetchall()
    finally:
        conn.close()
    _write_promoted_csv(rows)


def project_csv_from_db() -> int:
    """Rebuild promoted_admissions.csv from the authoritative `promotions` table
    (repair/backfill, e.g. CSV lost or stale). Returns the row count."""
    conn = _pg_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT subject_id, hadm_id, promoted_at FROM promotions "
                    "ORDER BY promoted_at NULLS FIRST, hadm_id;")
        rows = cur.fetchall()
    finally:
        conn.close()
    _write_promoted_csv(rows)
    return len(rows)


def _pg_conn():
    """Lazy DB connection via the shared database.pg helper. Kept INSIDE the write path
    so the file-based read path (promoted_hadms/holdout_hadms/established/split --
    imported by b2/b4/c1/d2/d_coordinator/d3) never pulls in psycopg2 or needs .env."""
    from database.pg import get_conn
    return get_conn()


def _write_promoted_csv(rows) -> None:
    """Project the CSV cache (subject_id,hadm_id,promoted_at) from `promotions` rows."""
    import csv
    PROMOTED_CSV.parent.mkdir(parents=True, exist_ok=True)
    with PROMOTED_CSV.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["subject_id", "hadm_id", "promoted_at"])
        for sid, hid, ts in rows:
            w.writerow([int(sid), int(hid),
                        ts.isoformat() if hasattr(ts, "isoformat") else (ts or "")])


def _append_promoted_csv(subject_id: int, hadm_id: int) -> None:
    """Dry-run only: append a single promotion to the CSV cache without touching the DB
    (file-based readers in the dry-run cascade still see it)."""
    if int(hadm_id) in promoted_hadms():
        return
    PROMOTED_CSV.parent.mkdir(parents=True, exist_ok=True)
    write_header = not PROMOTED_CSV.exists()
    with PROMOTED_CSV.open("a", encoding="utf-8") as fh:
        if write_header:
            fh.write("subject_id,hadm_id,promoted_at\n")
        fh.write(f"{int(subject_id)},{int(hadm_id)},{datetime.now().isoformat()}\n")


def _still_held(df: pd.DataFrame) -> pd.Series:
    """Boolean mask: is_holdout AND not yet promoted."""
    mask = _as_bool(df["is_holdout"])
    prom = promoted_hadms()
    if prom:
        mask = mask & ~df["hadm_id"].astype(int).isin(prom)
    return mask


# ── Public API ───────────────────────────────────────────────────────────────────

def holdout_hadms(df: pd.DataFrame | None = None) -> set[int]:
    """Set of still-held-out hadm_ids (holdout minus promoted). Empty if switch off."""
    if not config.HOLDOUT_LAST_ADMISSION:
        return set()
    if df is None:
        if not COHORT_CSV.exists():
            return set()
        df = pd.read_csv(COHORT_CSV, usecols=lambda c: c in {"hadm_id", "is_holdout"})
    if "is_holdout" not in df.columns:
        return set()
    return {int(h) for h, k in zip(df["hadm_id"], _still_held(df)) if k}


def established(df: pd.DataFrame) -> pd.DataFrame:
    """df with still-held-out admissions removed; promoted admissions are kept."""
    if not _enabled(df):
        return df
    return df[~_still_held(df)].copy()


def split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(established_df, holdout_df). Promoted admissions count as established."""
    if not _enabled(df):
        return df, df.iloc[0:0].copy()
    held = _still_held(df)
    return df[~held].copy(), df[held].copy()
