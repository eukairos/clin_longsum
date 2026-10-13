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
a4_load_and_embed.py
====================
Phase A4 -- load the cohort into Postgres and embed non-holdout bhc_delta.

TARGET DATABASE: taken from the environment (PGDATABASE, else the repo-root .env).
A shell PGDATABASE overrides .env, because load_dotenv is called with the default
override=False - this keeps test and generalisation cohorts in separate databases without editing config or .env.

Cohorts with distinct subject_ids coexist safely in the shared patients/
admissions tables, so several cohorts may share one database if that is what you
want (the original 100-patient 3-7 admission control cohort was loaded this way,
and remains usable as the <=7 no-compaction branch). Isolation is a choice of
PGDATABASE. 

SCOPE: base tables only (patients, admissions) + the vector extension + HNSW
index. THIS SCRIPT IS THE PRECONDITION FOR schema.sql - schema.sql's derived
tables FK to patients(subject_id) and admissions(hadm_id), so run a4 FIRST and
apply schema.sql afterwards.

Idempotent + resumable:
  - DDL uses IF NOT EXISTS (safe to rerun; will not disturb the control cohort).
  - upserts ON CONFLICT (safe to rerun).
  - embed loop skips rows already embedded (safe to resume after interruption).

Run on the workstation (needs DB + GPU). Reads cohort_admissions.csv and
cohort_patients.csv from config.INTERIM_DIR, so LONGSUM_DATA_ROOT /
LONGSUM_NAMESPACE select the cohort:
    python src/database/a4_load_and_embed.py

Optional flags:
    --skip-embed   load only; leave embeddings for a later run
    --no-index     skip HNSW index build/refresh
"""

from __future__ import annotations

import argparse
import gc
import json
import os
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

import _bootstrap  # noqa: F401  (puts src/ on sys.path)
import config
from database.pg import get_conn   # shared helper: protected-DB guard + banner

# .env lives at the repo root (config.PROJECT_ROOT); relocatable, no absolute path.
ENV_PATH = config.PROJECT_ROOT / ".env"
load_dotenv(ENV_PATH)

INTERIM = config.INTERIM_DIR
ADMISSIONS_CSV = INTERIM / "cohort_admissions.csv"
PATIENTS_CSV = INTERIM / "cohort_patients.csv"

EMBED_MODEL_NAME = "Snowflake/snowflake-arctic-embed-l-v2.0"
BATCH_SIZE = 32
EMBED_DIM = 1024

# Base-table DDL only. 
DDL_BASE = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS patients (
    subject_id               INT PRIMARY KEY,
    n_admissions             INT,
    conditions_json          JSONB,
    snomed_total_occurrences INT,
    snomed_unique_codes      INT,
    snomed_dedup_ratio       FLOAT
);

CREATE TABLE IF NOT EXISTS admissions (
    hadm_id               INT PRIMARY KEY,
    subject_id            INT REFERENCES patients(subject_id),
    admit_seq             INT,
    admit_date            DATE,
    discharge_date        DATE,
    service               TEXT,
    admission_type        TEXT,
    discharge_location    TEXT,
    hospital_expire_flag  BOOLEAN,
    is_holdout            BOOLEAN DEFAULT FALSE,
    bhc_text              TEXT,
    bhc_delta             TEXT,
    dedup_jaccard_to_prev FLOAT,
    embedding             vector(1024)
);

CREATE INDEX IF NOT EXISTS idx_admissions_subject ON admissions (subject_id);
"""

UPSERT_PATIENTS = """
INSERT INTO patients
    (subject_id, n_admissions, conditions_json,
     snomed_total_occurrences, snomed_unique_codes, snomed_dedup_ratio)
VALUES %s
ON CONFLICT (subject_id) DO UPDATE SET
    n_admissions             = EXCLUDED.n_admissions,
    conditions_json          = EXCLUDED.conditions_json,
    snomed_total_occurrences = EXCLUDED.snomed_total_occurrences,
    snomed_unique_codes      = EXCLUDED.snomed_unique_codes,
    snomed_dedup_ratio       = EXCLUDED.snomed_dedup_ratio;
"""

UPSERT_ADMISSIONS = """
INSERT INTO admissions
    (hadm_id, subject_id, admit_seq, admit_date, discharge_date,
     service, admission_type, discharge_location,
     hospital_expire_flag, is_holdout,
     bhc_text, bhc_delta, dedup_jaccard_to_prev)
VALUES %s
ON CONFLICT (hadm_id) DO UPDATE SET
    admit_seq             = EXCLUDED.admit_seq,
    admit_date            = EXCLUDED.admit_date,
    discharge_date        = EXCLUDED.discharge_date,
    service               = EXCLUDED.service,
    admission_type        = EXCLUDED.admission_type,
    discharge_location    = EXCLUDED.discharge_location,
    hospital_expire_flag  = EXCLUDED.hospital_expire_flag,
    is_holdout            = EXCLUDED.is_holdout,
    bhc_text              = EXCLUDED.bhc_text,
    bhc_delta             = EXCLUDED.bhc_delta,
    dedup_jaccard_to_prev = EXCLUDED.dedup_jaccard_to_prev;
"""


# ── coercion helpers  ─────────────────────────────────────────────

def coerce_int(x):
    try:
        return int(x)
    except Exception:
        return None


def coerce_float(x):
    try:
        return float(x)
    except Exception:
        return None


def coerce_bool(x):
    if isinstance(x, bool):
        return x
    if isinstance(x, float):
        return bool(int(x)) if not pd.isna(x) else None
    if isinstance(x, str):
        return x.strip().lower() in ("true", "1", "yes")
    return None


def coerce_date(x):
    try:
        return pd.Timestamp(x).date()
    except Exception:
        return None


def parse_conditions(x):
    if isinstance(x, list):
        return json.dumps(x)
    if isinstance(x, str):
        try:
            json.loads(x)
            return x
        except Exception:
            return "[]"
    return "[]"



# ── load ──────────────────────────────────────────────────────────────────────

def load_cohort(conn) -> None:
    cur = conn.cursor()

    patients = pd.read_csv(PATIENTS_CSV)
    patient_rows = [
        (
            int(r["subject_id"]),
            coerce_int(r.get("n_admissions")),
            parse_conditions(r.get("conditions_json")),
            coerce_int(r.get("snomed_total_occurrences")),
            coerce_int(r.get("snomed_unique_codes")),
            coerce_float(r.get("snomed_dedup_ratio")),
        )
        for _, r in patients.iterrows()
    ]
    execute_values(cur, UPSERT_PATIENTS, patient_rows,
                   template="(%s, %s, %s::jsonb, %s, %s, %s)")
    conn.commit()
    print(f"patients upserted: {len(patient_rows)}")

    admissions = pd.read_csv(ADMISSIONS_CSV, parse_dates=["admittime", "dischtime"])
    admission_rows = [
        (
            int(r["hadm_id"]),
            int(r["subject_id"]),
            coerce_int(r.get("admit_seq")),
            coerce_date(r.get("admittime")),
            coerce_date(r.get("dischtime")),
            str(r["service"]) if pd.notna(r.get("service")) else None,
            str(r["admission_type"]) if pd.notna(r.get("admission_type")) else None,
            str(r["discharge_location"]) if pd.notna(r.get("discharge_location")) else None,
            coerce_bool(r.get("hospital_expire_flag")),
            coerce_bool(r.get("is_holdout")),
            str(r["bhc_text"]) if pd.notna(r.get("bhc_text")) else None,
            str(r["bhc_delta"]) if pd.notna(r.get("bhc_delta")) else None,
            coerce_float(r.get("dedup_jaccard_to_prev")),
        )
        for _, r in admissions.iterrows()
    ]
    execute_values(cur, UPSERT_ADMISSIONS, admission_rows)
    conn.commit()
    print(f"admissions upserted: {len(admission_rows)}")
    cur.close()


# ── embed  ────────────────────────────────────────────────────────

def embed_cohort(conn, build_index: bool = True) -> None:
    import torch
    from pgvector.psycopg2 import register_vector
    from sentence_transformers import SentenceTransformer

    register_vector(conn)
    cur = conn.cursor()

    # Only non-holdout cohort rows that are not yet embedded. Restrict to THIS
    # cohort's subject_ids so a rerun never re-embeds the control cohort.
    cohort_pids = tuple(int(s) for s in pd.read_csv(PATIENTS_CSV)["subject_id"].tolist())
    cur.execute(
        """
        SELECT hadm_id, bhc_delta
        FROM admissions
        WHERE NOT is_holdout
          AND embedding IS NULL
          AND subject_id IN %s
        ORDER BY hadm_id;
        """,
        (cohort_pids,),
    )
    rows = cur.fetchall()
    print(f"rows to embed: {len(rows)}")
    if not rows:
        print("nothing to embed (already done or cohort empty)")
    else:
        hadm_ids = [r[0] for r in rows]
        texts = [r[1] if r[1] else "" for r in rows]

        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = SentenceTransformer(EMBED_MODEL_NAME, device=device)
        model.eval()
        try:
            with torch.no_grad():
                embeddings = model.encode(
                    texts,
                    batch_size=BATCH_SIZE,
                    normalize_embeddings=True,
                    show_progress_bar=True,
                )
            update_rows = [
                (embeddings[i].astype(np.float32), hadm_ids[i])
                for i in range(len(hadm_ids))
            ]
            cur.executemany(
                "UPDATE admissions SET embedding = %s WHERE hadm_id = %s;",
                update_rows,
            )
            conn.commit()
            print(f"embeddings written: {len(update_rows)}")
        finally:
            # clear VRAM after GPU use.
            del model
            gc.collect()
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
                torch.cuda.synchronize()
                free, total = torch.cuda.mem_get_info()
                print(f"VRAM after cleanup: {free/1024**3:.1f} / {total/1024**3:.1f} GB free")

    if build_index:
        # Idempotent; spans the whole admissions table (control + new cohort).
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_admissions_embedding
            ON admissions
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);
            """
        )
        conn.commit()
        print("HNSW index present (created or already existed)")
    cur.close()


# ── verify ────────────────────────────────────────────────────────────────────

def verify(conn) -> None:
    cur = conn.cursor()
    cohort_pids = tuple(int(s) for s in pd.read_csv(PATIENTS_CSV)["subject_id"].tolist())
    cur.execute("SELECT COUNT(*) FROM patients;")
    print(f"\ntotal patients in DB     : {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM admissions;")
    print(f"total admissions in DB   : {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM admissions WHERE subject_id IN %s;", (cohort_pids,))
    print(f"this cohort's admissions : {cur.fetchone()[0]}")
    cur.execute(
        "SELECT COUNT(*) FROM admissions WHERE subject_id IN %s AND embedding IS NOT NULL;",
        (cohort_pids,),
    )
    print(f"  embedded (non-holdout) : {cur.fetchone()[0]}")
    cur.execute(
        "SELECT COUNT(*) FROM admissions WHERE subject_id IN %s AND is_holdout;",
        (cohort_pids,),
    )
    print(f"  holdout (embed deferred): {cur.fetchone()[0]}")
    cur.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-embed", action="store_true", help="load only")
    ap.add_argument("--no-index", action="store_true", help="skip HNSW index")
    args = ap.parse_args()

    if not ADMISSIONS_CSV.exists() or not PATIENTS_CSV.exists():
        raise SystemExit(f"Cohort CSVs not found in {INTERIM}. Run a2_build_cohort.py first.")

    conn = get_conn()
    conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute(DDL_BASE)
        conn.commit()
        cur.close()
        print("base schema present (patients, admissions, vector ext, subject index)")

        load_cohort(conn)
        if not args.skip_embed:
            embed_cohort(conn, build_index=not args.no_index)
        verify(conn)
        print("\nA4 done. Base cohort loaded + embedded. Tier tables deferred to D3.")
        print("Next: Phase B (retention_extractor).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
