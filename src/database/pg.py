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
pg.py
=====
One Postgres connection helper for every DB-touching stage (a4, c5, e2,
holdout.promote, the foldin advisory lock, d3, db_persist_*). Credentials come from PGHOST/PGPORT/PGDATABASE/
PGUSER/PGPASSWORD in PROJECT_ROOT/.env.

Import from src-root scripts as `from database.pg import get_conn` - `database`
resolves as a namespace package because src/ is on sys.path for every stage
(script dir for root scripts; _bootstrap for database/ and evaluation/ scripts).
psycopg2/dotenv are imported lazily inside get_conn() so file-only read paths that
merely import a caller module (e.g. holdout's readers) never pull DB deps.

    get_conn()             plain connection
    get_conn(vector=True)  + pgvector registered (e2 embeddings)

PROTECTED DATABASES
-------------------
load_dotenv() is called with the default override=False, so a shell PGDATABASE
beats .env - that is how test and generalisation cohorts stay in separate
databases. The failure mode is a shell that FORGOT to set it: it falls through to
.env silently and the stage runs against the ledger-of-record. get_conn()
therefore refuses to connect to any database named in LONGSUM_PROTECTED_DBS
(comma-separated, default 'longitudinal_bhc') unless LONGSUM_ALLOW_PROTECTED_DB=1.
It also prints the resolved database once per process, so every stage announces
its target on first connect.
"""

from __future__ import annotations

import os

import config

ENV_PATH = config.PROJECT_ROOT / ".env"

# Databases a stage must never touch by accident. Override per deployment.
_PROTECTED = {d.strip() for d in
              os.environ.get("LONGSUM_PROTECTED_DBS", "longitudinal_bhc").split(",")
              if d.strip()}

_announced = False


def _resolve_db() -> str:
    """Resolve + vet PGDATABASE. Call only AFTER load_dotenv(), which supplies the
    fallback this guard exists to catch.

    Raises RuntimeError, not SystemExit: get_conn() is called per-request inside the
    FastAPI app, where a SystemExit can take uvicorn down. A RuntimeError surfaces as
    a logged 500 there, and as a traceback ending in this message at the CLI.
    """
    dbname = os.getenv("PGDATABASE")
    if not dbname:
        raise RuntimeError(
            "PGDATABASE is unset and .env supplies no default - refusing to connect "
            "to whatever database psycopg2 would fall back to. Set PGDATABASE.")
    if dbname in _PROTECTED and os.environ.get("LONGSUM_ALLOW_PROTECTED_DB") != "1":
        # load_dotenv(override=False) has already merged .env into os.environ, so the
        # source cannot be read off os.environ itself; compare with the file instead.
        # A shell that sets the same name as .env reads as ".env" -- the message covers it.
        from dotenv import dotenv_values
        in_env_file = (dotenv_values(ENV_PATH).get("PGDATABASE") or "").strip() == dbname
        if in_env_file:
            where = (f"PGDATABASE resolved to '{dbname}', the value in {ENV_PATH} -- if this "
                     f"shell did not set PGDATABASE, it fell through to that file")
        else:
            where = (f"PGDATABASE is set to '{dbname}' in this shell/process environment "
                     f"(not from {ENV_PATH.name})")
        raise RuntimeError(
            f"REFUSING to connect to protected database '{dbname}': {where}, and '{dbname}' "
            f"is listed in LONGSUM_PROTECTED_DBS ({', '.join(sorted(_PROTECTED))}). Point "
            f"PGDATABASE at the database you mean, or set LONGSUM_ALLOW_PROTECTED_DB=1 for "
            f"this run if you really mean to write to it.")
    return dbname


def get_conn(vector: bool = False):
    import psycopg2
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH)

    dbname = _resolve_db()

    global _announced
    if not _announced:
        print(f"[pg] database: {dbname}   namespace: "
              f"'{config.RUN_NAMESPACE or '(default)'}'")
        _announced = True

    conn = psycopg2.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=int(os.getenv("PGPORT", 5432)),
        dbname=dbname,
        user=os.getenv("PGUSER"),
        password=os.getenv("PGPASSWORD"),
    )
    if vector:
        from pgvector.psycopg2 import register_vector
        register_vector(conn)
        conn.rollback()          # clear register_vector's implicit txn
    return conn

def get_support_conn():
    """Connection to the SUPPORT database (ticket lifecycle, triage, staff identity).

    Reads PGDATABASE_SUPPORT, NOT PGDATABASE. That separation of env vars is the
    safety property: a shell that forgets PGDATABASE cannot write support rows
    into a clinical cohort, and support state never lands in a patient-oriented
    database. No _PROTECTED check applies for the same reason.
    """
    import psycopg2
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH)
    dbname = os.getenv("PGDATABASE_SUPPORT", "longsum_support")
    return psycopg2.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=int(os.getenv("PGPORT", 5432)),
        dbname=dbname,
        user=os.getenv("PGUSER"),
        password=os.getenv("PGPASSWORD"),
    )

# ---------------------------------------------------------------------------
# QA invariants
# ---------------------------------------------------------------------------
# Sited here rather than in a new module: it is pure SQL over tables both the
# writers and the API already reach through this helper.

_RETENTION_VISIBILITY_SQL = """
WITH latest AS (
  SELECT DISTINCT ON (subject_id)
         subject_id, created_at,
         COALESCE((scores->>'n_missing')::int, 0)          AS n_missing,
         COALESCE((scores->>'n_pinned_in_rollup')::int, 0) AS n_rollup
    FROM generation_audit
   WHERE role = 'retention_validator'{scope}
   ORDER BY subject_id, created_at DESC
)
SELECT l.subject_id, l.n_missing, l.n_rollup, l.created_at
  FROM latest l
  LEFT JOIN retention_review rr ON rr.subject_id = l.subject_id
 WHERE (l.n_missing + l.n_rollup) > 0
 GROUP BY l.subject_id, l.n_missing, l.n_rollup, l.created_at
HAVING COUNT(rr.id) FILTER (WHERE rr.checked_at >= l.created_at) = 0
 ORDER BY l.subject_id;
"""


def retention_visibility_gaps(conn, subjects=None) -> list[dict]:
    """Subjects whose c2 losses are recorded in generation_audit but are NOT visible
    in the RETENTION tab.

    The RETENTION tab is the ONLY surface for c2 fact-level losses. The chain is
    c2 -> db_persist_retention.py -> retention_review -> /tiered/review/retention-queue
    and if any link stops populating retention_review those losses become INVISIBLE to
    the physician rather than falling back to TRIAGE. This is the guard for that.

    Both sides live in Postgres (c2 records n_missing in generation_audit.scores; the
    persister writes retention_review), so no file access is needed. Catches all three
    failure modes: stage never ran, ran and wrote nothing, ran against stale c2 output.

    Returns [] when healthy. NEVER raises - callers decide whether a gap is fatal
    (db_persist_retention: yes) or advisory (the API: show a banner, still serve the
    queue). Degrading to 'no warning' is bad; degrading to 'no tab' is worse.
    """
    scope, params = "", []
    if subjects:
        scope, params = " AND subject_id = ANY(%s)", [list(subjects)]
    cur = conn.cursor()
    try:
        cur.execute(_RETENTION_VISIBILITY_SQL.format(scope=scope), params)
        return [
            {"subject_id": r[0], "n_missing": r[1], "n_rollup": r[2],
             "c2_at": str(r[3]) if r[3] else None}
            for r in cur.fetchall()
        ]
    except Exception as e:
        print(f"[pg] retention_visibility_gaps: check failed ({e})")
        return []
    finally:
        cur.close()
