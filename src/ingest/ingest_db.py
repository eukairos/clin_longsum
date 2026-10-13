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
ingest_db.py
============
Postgres access for the production ingest. The DB is the source of truth for
admissions; cohort_admissions.csv is projected from it (ingest_project.py).

Writes only:  patients (new subject rows, n_admissions), admissions (INSERT ...
ON CONFLICT DO NOTHING - a stored admission is never overwritten), and the ingest_*
tables. Never writes admissions.embedding (fold-in's e2 owns that).
"""

from __future__ import annotations

import json
from datetime import datetime

import psycopg2.extras

from ingest_derive import Action, Existing
from ingest_source import EHRDischarge

BASE_TABLES = ("patients", "admissions", "promotions", "tiered_view")
INGEST_TABLES = ("ingest_runs", "ingest_log", "ingest_amendments", "ingest_review_history")


def connect():
    """Shared connection via database.pg. Returns (conn, dbname).

    Protected databases are declared per deployment, not in code: get_conn() refuses any
    database listed in LONGSUM_PROTECTED_DBS (comma-separated; .env or shell) unless
    LONGSUM_ALLOW_PROTECTED_DB=1 - the same guard every pipeline stage uses."""
    from database.pg import get_conn
    try:
        conn = get_conn()
    except RuntimeError as exc:                 # pg.py's protected-DB / unset-PGDATABASE refusal
        raise SystemExit(str(exc))
    cur = conn.cursor()
    cur.execute("SELECT current_database();")
    db = cur.fetchone()[0]
    cur.close()
    conn.rollback()
    return conn, db


def check_schema(conn) -> None:
    cur = conn.cursor()
    missing = []
    for t in BASE_TABLES + INGEST_TABLES:
        cur.execute("SELECT to_regclass(%s);", (f"public.{t}",))
        if cur.fetchone()[0] is None:
            missing.append(t)
    cur.close()
    conn.rollback()
    if missing:
        raise SystemExit(f"missing tables {missing}. Apply src/database/schema.sql and "
                         f"src/ingest/ingest_schema.sql in PgAdmin first (no auto-migration).")


# -- runs ------------------------------------------------------------------------

def last_watermark(conn) -> datetime | None:
    cur = conn.cursor()
    cur.execute("SELECT MAX(watermark_to) FROM ingest_runs WHERE status = 'ok';")
    wm = cur.fetchone()[0]
    cur.close()
    conn.rollback()
    return wm


def start_run(conn, source: str, since: datetime | None) -> int:
    cur = conn.cursor()
    cur.execute("INSERT INTO ingest_runs (source, since_ts) VALUES (%s, %s) RETURNING run_id;",
                (source, since))
    run_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    return run_id


def finish_run(conn, run_id: int, status: str, counts: dict, n_fetched: int,
               watermark_to: datetime | None, error: str | None = None) -> None:
    conn.rollback()
    cur = conn.cursor()
    cur.execute("""
        UPDATE ingest_runs SET finished_at = now(), status = %s, watermark_to = %s,
               n_fetched = %s, n_rejected = %s, n_inserted = %s, counts = %s, error = %s
        WHERE run_id = %s;""",
                (status, watermark_to, n_fetched, int(counts.get("rejected", 0)),
                 int(counts.get("inserted", 0)), json.dumps(counts), error, run_id))
    conn.commit()
    cur.close()


# -- reads ------------------------------------------------------------------------

def existing_by_subject(conn, subject_ids: list[int]) -> dict[int, list[Existing]]:
    out: dict[int, list[Existing]] = {}
    if not subject_ids:
        return out
    cur = conn.cursor()
    cur.execute("""
        SELECT a.subject_id, a.hadm_id, a.admit_seq, a.admit_date, COALESCE(a.bhc_text, ''),
               COALESCE(a.hospital_expire_flag, FALSE),
               (p.hadm_id IS NOT NULL OR NOT COALESCE(a.is_holdout, FALSE)) AS folded_in
        FROM admissions a LEFT JOIN promotions p ON p.hadm_id = a.hadm_id
        WHERE a.subject_id = ANY(%s)
        ORDER BY a.subject_id, a.admit_seq;""", (list(subject_ids),))
    for sid, hadm, seq, adate, text, expired, folded in cur.fetchall():
        out.setdefault(int(sid), []).append(
            Existing(int(hadm), int(seq), adate, text, bool(expired), bool(folded)))
    cur.close()
    conn.rollback()
    return out


def hadms_by_subject(conn, subject_ids: list[int]) -> dict[int, set[int]]:
    out: dict[int, set[int]] = {}
    if not subject_ids:
        return out
    cur = conn.cursor()
    cur.execute("SELECT subject_id, hadm_id FROM admissions WHERE subject_id = ANY(%s);",
                (list(subject_ids),))
    for sid, hadm in cur.fetchall():
        out.setdefault(int(sid), set()).add(int(hadm))
    cur.close()
    conn.rollback()
    return out


def ingested_rows(conn) -> list[dict]:
    """Every admission the ingest inserted, in cohort_admissions.csv shape. The full EHR
    timestamps come from ingest_log (admissions keeps DATE only)."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT a.hadm_id, a.subject_id, a.admit_seq,
               COALESCE(l.admittime, a.admit_date::timestamp)     AS admittime,
               COALESCE(l.dischtime, a.discharge_date::timestamp) AS dischtime,
               a.service, a.admission_type, a.discharge_location, a.hospital_expire_flag,
               a.is_holdout, a.bhc_text, a.bhc_delta, a.dedup_jaccard_to_prev
        FROM ingest_log l JOIN admissions a ON a.hadm_id = l.hadm_id
        WHERE l.action = 'inserted'
        ORDER BY a.subject_id, a.admit_seq;""")
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.rollback()
    return rows


def patient_counts(conn, subject_ids) -> dict[int, int]:
    ids = sorted({int(s) for s in subject_ids})
    if not ids:
        return {}
    cur = conn.cursor()
    cur.execute("SELECT subject_id, COUNT(*) FROM admissions WHERE subject_id = ANY(%s) "
                "GROUP BY subject_id;", (ids,))
    out = {int(s): int(n) for s, n in cur.fetchall()}
    cur.close()
    conn.rollback()
    return out


# -- writes (caller commits) ------------------------------------------------------

def insert_admission(cur, row: dict) -> bool:
    """Insert one new admission (+ its patient). False if the hadm_id already existed."""
    sid = int(row["subject_id"])
    cur.execute("INSERT INTO patients (subject_id, n_admissions, conditions_json) "
                "VALUES (%s, 0, '[]'::jsonb) ON CONFLICT (subject_id) DO NOTHING;", (sid,))
    cur.execute("""
        INSERT INTO admissions
            (hadm_id, subject_id, admit_seq, admit_date, discharge_date, service,
             admission_type, discharge_location, hospital_expire_flag, is_holdout,
             bhc_text, bhc_delta, dedup_jaccard_to_prev)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s, %s, %s)
        ON CONFLICT (hadm_id) DO NOTHING
        RETURNING hadm_id;""",
                (int(row["hadm_id"]), sid, int(row["admit_seq"]),
                 row["admittime"].date(),
                 row["dischtime"].date() if row.get("dischtime") else None,
                 row.get("service"), row.get("admission_type"), row.get("discharge_location"),
                 bool(row.get("hospital_expire_flag")), row["bhc_text"], row.get("bhc_delta"),
                 row.get("dedup_jaccard_to_prev")))
    inserted = cur.fetchone() is not None
    cur.execute("UPDATE patients SET n_admissions = (SELECT COUNT(*) FROM admissions "
                "WHERE subject_id = %s) WHERE subject_id = %s;", (sid, sid))
    return inserted


def log_action(cur, run_id: int, action: str, subject_id=None, hadm_id=None,
               rec: EHRDischarge | None = None, detail: dict | None = None) -> None:
    cur.execute("""
        INSERT INTO ingest_log (run_id, subject_id, hadm_id, action, admittime, dischtime,
                                signed_at, bhc_sha256, detail)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING;""",
                (run_id,
                 rec.subject_id if rec else _int_or_none(subject_id),
                 rec.hadm_id if rec else _int_or_none(hadm_id),
                 action,
                 rec.admittime if rec else None, rec.dischtime if rec else None,
                 rec.signed_at if rec else None, rec.sha256 if rec else None,
                 json.dumps(detail, default=str) if detail else None))


def log_amendment(cur, run_id: int, act: Action) -> bool:
    """Record an amendment / anomaly for manual review. False if already recorded."""
    kind = {"amendment": "text_changed"}.get(act.kind, act.kind)
    rec = act.rec
    cur.execute("""
        INSERT INTO ingest_amendments
            (run_id, subject_id, hadm_id, kind, stored_sha256, incoming_sha256,
             incoming_bhc_text, incoming_admittime, incoming_dischtime, signed_at,
             admission_folded_in, detail)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (hadm_id, kind, incoming_sha256) DO NOTHING
        RETURNING id;""",
                (run_id, rec.subject_id, rec.hadm_id, kind,
                 act.detail.get("stored_sha256"), rec.sha256, rec.bhc_text,
                 rec.admittime, rec.dischtime, rec.signed_at,
                 act.detail.get("admission_folded_in"),
                 json.dumps(act.detail, default=str)))
    return cur.fetchone() is not None


def _int_or_none(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None
