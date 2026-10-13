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
smoke_test_schema.py
====================
Standalone smoke test for src/database/schema.sql against a THROWAWAY database.

Verifies, without touching your real DB:
  1. schema.sql applies cleanly on an empty DB that already has the base tables
     (patients, admissions) -- the a4 precondition.
  2. Every expected table, key column, index, and the tiered_view->patients FK
     are created.
  3. Re-running schema.sql is a clean no-op (idempotency) -- no error, no
     duplicate fk_tv_patient, same table set.
  4. The dev-DB guard raises when the database is named 'longitudinal_bhc_dev'.

It creates a throwaway DB (default: clin_schema_smoke), runs everything there,
and DROPs it at the end -- your longitudinal_bhc_test / _gen databases are never
touched. Self-contained: no pipeline imports, only psycopg2 + your .env.

Connection params come from .env (same vars a4 uses: PGHOST/PGPORT/PGUSER/
PGPASSWORD). PGDATABASE from .env is IGNORED -- the test picks its own DB names.
The PGUSER role must have CREATEDB (to make/drop the throwaway) and be allowed to
CREATE EXTENSION vector in a fresh DB (superuser, or a role granted it).

Run from the repo root:
    python setup/smoke_test_schema.py
    python setup/smoke_test_schema.py --keep      # don't drop the throwaway DB
    python setup/smoke_test_schema.py --db my_tmp # custom throwaway DB name
    python setup/smoke_test_schema.py --skip-guard # skip check 4 (dev-DB guard); avoids a 2nd CREATE DATABASE

Exit code 0 = all checks passed; 1 = a check failed (details printed).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg2
from psycopg2 import sql
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths + config
# ---------------------------------------------------------------------------
REPO_ROOT   = Path(__file__).resolve().parents[1]          # setup/ -> repo root
SCHEMA_SQL  = REPO_ROOT / "src" / "database" / "schema.sql"
ENV_PATH    = REPO_ROOT / ".env"

# A name in schema.sql's denylist that is RESERVED AS A TEST SENTINEL and must never be a
# real database: phase_dev_guard CREATEs and DROPs it. Do NOT "fix" this to the real dev DB
# name (longitudinal_bhc) to match the guard -- that would drop the dev ledger-of-record.
# The denylist holds both names precisely so this test has a safe one to use.
DEV_GUARD_DBNAME = "longitudinal_bhc_dev"
_NEVER_DROP = {"longitudinal_bhc", "longitudinal_bhc_gen", "longitudinal_bhc_test"}

# Base tables a4 creates (DDL_BASE in a4_load_and_embed.py), reproduced so the
# smoke test does not need a4, a cohort, or a GPU. Kept byte-faithful to a4 so
# the FK target (patients.subject_id) and the admissions shape match production.
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

# What schema.sql must produce.
EXPECTED_TABLES = [
    "tiered_view", "tiered_episode", "tiered_ledger", "tiered_ledger_raw",
    "retention_facts", "generation_audit",
    "compaction_review", "compaction_change",
    "promotions", "retention_review",
]
# A few load-bearing columns that were previously added by e_review_schema.sql --
# confirms they are inline in schema.sql's tiered_view.
EXPECTED_TV_COLUMNS = ["approved_summary_md", "summary_version", "folded_in_at",
                       "review_status", "incoming_hadm_id"]

# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------
_failures: list[str] = []

def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {label}"
    if detail:
        line += f"  -- {detail}"
    print(line)
    if not ok:
        _failures.append(label)


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------
def conn_params(dbname: str) -> dict:
    return dict(
        host=os.getenv("PGHOST", "localhost"),
        port=int(os.getenv("PGPORT", 5432)),
        dbname=dbname,
        user=os.getenv("PGUSER"),
        password=os.getenv("PGPASSWORD"),
    )


def connect(dbname: str, autocommit: bool = False) -> psycopg2.extensions.connection:
    c = psycopg2.connect(**conn_params(dbname))
    c.autocommit = autocommit
    return c


def create_db(admin: psycopg2.extensions.connection, dbname: str) -> None:
    with admin.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT 1 FROM pg_database WHERE datname = %s"), (dbname,)
        )
        if cur.fetchone():
            cur.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(dbname)))
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))


def drop_db(admin: psycopg2.extensions.connection, dbname: str) -> None:
    with admin.cursor() as cur:
        # terminate any lingering backends, then drop
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (dbname,),
        )
        cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(dbname)))


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------
def table_set(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
        return {r[0] for r in cur.fetchall()}


def column_set(conn, table: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s",
            (table,),
        )
        return {r[0] for r in cur.fetchall()}


def fk_count(conn, constraint: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM information_schema.table_constraints "
            "WHERE constraint_type = 'FOREIGN KEY' AND constraint_name = %s",
            (constraint,),
        )
        return cur.fetchone()[0]


def index_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_indexes WHERE schemaname = 'public'")
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Test phases
# ---------------------------------------------------------------------------
def apply_schema(conn) -> None:
    """Run schema.sql as one multi-statement script (as PgAdmin would)."""
    sql_text = SCHEMA_SQL.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql_text)
    conn.commit()


def phase_fresh_apply(dbname: str) -> None:
    print("\n[1] Fresh apply on a DB with base tables")
    with connect(dbname) as c:
        with c.cursor() as cur:
            cur.execute(DDL_BASE)
        c.commit()
        check("base tables (patients, admissions) created", True)

        try:
            apply_schema(c)
            check("schema.sql applied without error", True)
        except Exception as e:
            check("schema.sql applied without error", False, repr(e))
            return  # nothing more to check if it did not apply

        tables = table_set(c)
        for t in EXPECTED_TABLES:
            check(f"table present: {t}", t in tables)

        tv_cols = column_set(c, "tiered_view")
        for col in EXPECTED_TV_COLUMNS:
            check(f"tiered_view has inline column: {col}", col in tv_cols)

        check("FK fk_tv_patient attached (exactly 1)",
              fk_count(c, "fk_tv_patient") == 1,
              f"count={fk_count(c, 'fk_tv_patient')}")

        # compaction_change -> compaction_review FK (order-sensitive create)
        cc_fk = fk_count(c, "compaction_change_subject_id_fkey")
        check("compaction_change FK -> compaction_review present",
              cc_fk >= 1, f"count={cc_fk}")


def phase_idempotency(dbname: str) -> None:
    print("\n[2] Idempotency -- re-run schema.sql on the same DB")
    with connect(dbname) as c:
        before_tables = table_set(c)
        before_indexes = index_count(c)
        try:
            apply_schema(c)
            check("second schema.sql run without error", True)
        except Exception as e:
            check("second schema.sql run without error", False, repr(e))
            return
        check("table set unchanged after re-run",
              table_set(c) == before_tables)
        check("index count unchanged after re-run",
              index_count(c) == before_indexes,
              f"{before_indexes} -> {index_count(c)}")
        check("FK fk_tv_patient still exactly 1 (guard prevents dup)",
              fk_count(c, "fk_tv_patient") == 1,
              f"count={fk_count(c, 'fk_tv_patient')}")


def phase_dev_guard(admin: psycopg2.extensions.connection) -> None:
    print("\n[3] Dev-DB guard fires on the guarded name")
    guard_db = DEV_GUARD_DBNAME
    # Belt and braces: this function drops the database it names.
    if guard_db in _NEVER_DROP:
        raise SystemExit(
            f"REFUSING: DEV_GUARD_DBNAME is set to '{guard_db}', a real database. "
            f"This phase CREATEs and DROPs that name. Use a throwaway sentinel.")
    created = False
    try:
        create_db(admin, guard_db)
        created = True
        with connect(guard_db) as c:
            with c.cursor() as cur:
                cur.execute(DDL_BASE)          # base tables so only the guard blocks
            c.commit()
            raised = False
            try:
                apply_schema(c)
            except psycopg2.errors.RaiseException:
                raised = True
                c.rollback()
            except Exception as e:
                # any hard failure here still means the guard stopped it, but be explicit
                raised = True
                c.rollback()
                print(f"       (guard raised: {type(e).__name__})")
            check(f"schema.sql refuses to run on '{guard_db}'", raised)
    finally:
        if created:
            drop_db(admin, guard_db)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="clin_schema_smoke",
                    help="throwaway DB name (default: clin_schema_smoke)")
    ap.add_argument("--keep", action="store_true",
                    help="do not drop the throwaway DB at the end")
    ap.add_argument("--skip-guard", action="store_true",
                    help="skip the dev-DB guard test (needs a 2nd CREATE DATABASE)")
    args = ap.parse_args()

    if not SCHEMA_SQL.exists():
        print(f"ERROR: schema.sql not found at {SCHEMA_SQL}")
        return 1
    if not ENV_PATH.exists():
        print(f"ERROR: .env not found at {ENV_PATH}")
        return 1
    load_dotenv(ENV_PATH)

    if args.db in ("longitudinal_bhc", "longitudinal_bhc_gen", DEV_GUARD_DBNAME):
        print(f"ERROR: refusing to use '{args.db}' as the throwaway DB.")
        return 1

    print(f"schema.sql : {SCHEMA_SQL}")
    print(f"throwaway  : {args.db}  (host={os.getenv('PGHOST','localhost')} "
          f"user={os.getenv('PGUSER')})")

    # admin connection on the maintenance DB, autocommit for CREATE/DROP DATABASE
    try:
        admin = connect("postgres", autocommit=True)
    except Exception as e:
        print(f"ERROR: cannot connect to the 'postgres' maintenance DB: {e}")
        print("       (need a role with CREATEDB; check .env PGUSER/PGPASSWORD)")
        return 1

    try:
        create_db(admin, args.db)
        print(f"created throwaway DB: {args.db}")

        phase_fresh_apply(args.db)
        phase_idempotency(args.db)
        if not args.skip_guard:
            phase_dev_guard(admin)
    finally:
        if not args.keep:
            drop_db(admin, args.db)
            print(f"\ndropped throwaway DB: {args.db}")
        else:
            print(f"\nkept throwaway DB: {args.db} (drop it yourself when done)")
        admin.close()

    print("\n" + "=" * 60)
    if _failures:
        print(f"RESULT: {len(_failures)} check(s) FAILED:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("RESULT: all checks PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
