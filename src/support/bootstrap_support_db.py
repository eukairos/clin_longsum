# clin_longsum - physician feedback loop
# Copyright (C) 2026 Eukairos
# SPDX-License-Identifier: AGPL-3.0-only
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of version 3 of the GNU Affero General Public License as published
# by the Free Software Foundation. This program is distributed WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE. See the GNU Affero General Public License for details.

"""
bootstrap_support_db.py
=======================
Create and initialise the SUPPORT database (default: longsum_support).

Idempotent. Safe to re-run: CREATE DATABASE is skipped if present, the schema is
fresh-creation form (CREATE TABLE IF NOT EXISTS), and the symptom catalog is
replaced wholesale from the policy file.

    python src/support/bootstrap_support_db.py --create-db          # first run
    python src/support/bootstrap_support_db.py                      # schema + policy
    python src/support/bootstrap_support_db.py --seed-demo          # + synthetic staff
    python src/support/bootstrap_support_db.py --check              # report only

WHY THIS SCRIPT EXISTS RATHER THAN pg_execute / psql
----------------------------------------------------
CREATE DATABASE cannot run inside a transaction block, so it needs an autocommit
connection to the `postgres` maintenance database. psql is ruled out separately:
it prompts interactively for a password and hangs the shell MCP server.

CREDENTIALS come from PROJECT_ROOT/.env, same as database/pg.py. 
Expected keys, with fallbacks so an existing dev .env works unchanged:
    PGHOST PGPORT PGUSER PGPASSWORD          (shared)
    PGDATABASE_SUPPORT                       (default 'longsum_support')

DATABASE SEPARATION is key. Support state - ticket lifecycle, triage tags,
staff identity, correspondence - should never land in a patient-oriented clinical
database. So we deliberately do NOT import database.pg.get_conn(), which
resolves PGDATABASE and would connect to a clinical DB. A support connection is
reached only through get_support_conn(), which reads a different env var
entirely; that separation of variables IS the safety property. 
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# src/ on sys.path so `config` and `database.pg` resolve when run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402

SCHEMA_SQL = Path(__file__).resolve().parent / "schema_support.sql"
POLICY_YAML = Path(__file__).resolve().parent / "policy" / "triage_policy.yaml"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Columns the running code writes. Checked against the live table on every
# bootstrap and by --check.
#
# WHY THIS LIST EXISTS. schema_support.sql is fresh-creation form, so re-running
# it against an existing database is a no-op even for columns added to the file
# later. A support DB bootstrapped at any earlier point therefore stays silently
# behind, and the first symptom is an opaque HTTP 500 on a physician's send
# button - a failure that costs the loop far more trust than the defect they
# were trying to report. Drift is caught here instead, at the point where
# somebody is already looking at the database.
EXPECTED_COLUMNS = {
    "feedback": {
        "feedback_id", "reporter_id", "reporter_name_raw", "symptom", "report_text",
        "disputed_field", "clinical_action_flag", "seen_other_patients", "cohort",
        "namespace", "subject_id", "anchor_kind", "anchor_key", "fact_ids",
        "source_hadm_ids", "fact_text", "salience_tier", "retention_class",
        "certainty", "current_lifecycle", "negative_kind", "taxonomy_version",
        "prompt_version", "artifact_generated_at", "policy_version", "urgency",
        "urgency_rule_id", "s1_subclass", "defect_class", "owner_roles",
        "dedup_key", "state", "disposition", "duplicate_of", "fix_version",
        "reply_text", "acknowledged_by", "created_at", "triaged_at", "closed_at",
    },
    "feedback_events": {
        "event_id", "feedback_id", "event_type", "from_state", "to_state",
        "actor_kind", "actor_id", "rule_id", "note", "detail", "created_at",
    },
    "feedback_evidence": {
        "evidence_id", "feedback_id", "collector_version", "evidence", "collected_at",
    },
    "outbox": {
        "outbox_id", "channel", "feedback_id", "idempotency_key", "payload",
        "state", "attempts", "last_error", "external_ref", "created_at", "sent_at",
    },
    "physicians": {
        "physician_id", "directory_ref", "display_name", "email", "role",
        "notify_mode", "active", "created_at",
    },
    "subject_alias": {"cohort", "subject_id", "display_alias", "created_at"},
    "symptom_catalog": {
        "symptom_id", "physician_label", "owner_roles", "defect_class_hint",
        "notes", "sort_order", "policy_version", "loaded_at",
    },
}

DEFAULT_SUPPORT_DB = "longsum_support"

# Synthetic staff only. .invalid is reserved by RFC 2606 and can never resolve,
# so a misconfigured mailer cannot reach a real inbox from demo data.
DEMO_PHYSICIANS = [
    ("Dr Tan Wei Ming", "tan.weiming@example.invalid", "clinician", "immediate"),
    ("Dr Priya Nair",   "priya.nair@example.invalid",  "clinician", "digest"),
    ("Eng Support",     "eng.support@example.invalid", "engineer",  "immediate"),
]


# ── connections ────────────────────────────────────────────────────────────────

def _env():
    from dotenv import load_dotenv
    load_dotenv(config.PROJECT_ROOT / ".env")
    return {
        "host": os.getenv("PGHOST", "localhost"),
        "port": int(os.getenv("PGPORT", 5432)),
        "user": os.getenv("PGUSER"),
        "password": os.getenv("PGPASSWORD"),
        "dbname": os.getenv("PGDATABASE_SUPPORT", DEFAULT_SUPPORT_DB),
    }


def get_support_conn():
    """Plain connection to the support database.

    Mirrored into database/pg.py so application code has one import site; kept
    here too so bootstrap can run before that patch is applied.
    """
    import psycopg2
    e = _env()
    return psycopg2.connect(host=e["host"], port=e["port"], dbname=e["dbname"],
                            user=e["user"], password=e["password"])


def _maintenance_conn():
    """Autocommit connection to `postgres` - CREATE DATABASE needs both."""
    import psycopg2
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
    e = _env()
    conn = psycopg2.connect(host=e["host"], port=e["port"], dbname="postgres",
                            user=e["user"], password=e["password"])
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    return conn


# ── steps ──────────────────────────────────────────────────────────────────────

def create_database() -> bool:
    """Create the support DB if absent. Returns True if it created one."""
    dbname = _env()["dbname"]
    conn = _maintenance_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s;", (dbname,))
        if cur.fetchone():
            print(f"[bootstrap] database '{dbname}' already exists")
            return False
        # Identifier cannot be parameterised; dbname comes from .env, not user
        # input, and is quoted defensively.
        from psycopg2 import sql
        cur.execute(sql.SQL("CREATE DATABASE {} ENCODING 'UTF8'").format(
            sql.Identifier(dbname)))
        print(f"[bootstrap] CREATED database '{dbname}'")
        return True
    finally:
        conn.close()


def apply_schema(conn) -> None:
    if not SCHEMA_SQL.exists():
        raise FileNotFoundError(f"schema not found: {SCHEMA_SQL}")
    cur = conn.cursor()
    cur.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
    conn.commit()
    print(f"[bootstrap] applied {SCHEMA_SQL.name}")


def apply_migrations(conn) -> int:
    """Run un-applied .sql files from migrations/, in filename order.

    Tracked in `schema_migrations`, so each file runs exactly once and a re-run
    is a no-op. Migrations are the ONLY way an existing support database gains a
    column: schema_support.sql cannot do it, by design, because fresh-creation
    form keeps that file readable as a description of the current shape rather
    than an accreting history of patches.

    Each migration commits on its own. A later failure therefore leaves earlier
    ones applied and recorded, which is what makes a fix-and-rerun safe.
    """
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                       filename    TEXT PRIMARY KEY,
                       applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW());""")
    conn.commit()
    cur.execute("SELECT filename FROM schema_migrations;")
    done = {r[0] for r in cur.fetchall()}

    if not MIGRATIONS_DIR.exists():
        return 0
    n = 0
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in done:
            continue
        cur.execute(path.read_text(encoding="utf-8"))
        cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s);", (path.name,))
        conn.commit()
        print(f"[bootstrap] migration applied: {path.name}")
        n += 1
    if not n:
        print("[bootstrap] migrations: nothing to apply")
    return n


def verify_schema(conn, *, fatal: bool = True) -> list[str]:
    """Compare the live tables against what the code writes.

    Returns a list of human-readable problems; empty means healthy. Fatal by
    default during bootstrap, because shipping a known-behind schema is how a
    physician ends up seeing HTTP 500 instead of a reply.
    """
    cur = conn.cursor()
    problems: list[str] = []
    for table, expected in EXPECTED_COLUMNS.items():
        cur.execute("""SELECT column_name FROM information_schema.columns
                        WHERE table_schema='public' AND table_name=%s;""", (table,))
        actual = {r[0] for r in cur.fetchall()}
        if not actual:
            problems.append(f"table '{table}' is missing entirely")
            continue
        missing = expected - actual
        if missing:
            problems.append(f"table '{table}' is missing column(s): {sorted(missing)}")
    if problems:
        msg = "schema drift detected:\n  - " + "\n  - ".join(problems)
        if fatal:
            raise RuntimeError(
                msg + "\n\nAdd a migration under src/support/migrations/ and re-run "
                      "this script. schema_support.sql alone cannot add columns to "
                      "an existing database.")
        print(f"[bootstrap] WARNING: {msg}")
    else:
        print("[bootstrap] schema verified against expected columns")
    return problems


def load_policy() -> dict:
    import yaml
    if not POLICY_YAML.exists():
        raise FileNotFoundError(f"policy not found: {POLICY_YAML}")
    return yaml.safe_load(POLICY_YAML.read_text(encoding="utf-8"))


def project_symptom_catalog(conn, policy: dict) -> int:
    """Replace symptom_catalog from the policy file.

    The FILE is authoritative; this table is a UI-population cache stamped with
    policy_version. Same DB-as-truth / projection split as holdout.promote().
    Wholesale replace, never in-place edit: a symptom retired in the policy must
    disappear from the picker, and a row edited directly in the DB must not
    survive the next load.
    """
    version = policy["version"]
    rows = [
        (s["id"], s["physician_label"], s.get("owner_roles") or [],
         s.get("defect_class_hint"), s.get("notes"), i, version)
        for i, s in enumerate(policy.get("symptoms", []))
    ]
    cur = conn.cursor()
    cur.execute("DELETE FROM symptom_catalog;")
    cur.executemany(
        """INSERT INTO symptom_catalog
             (symptom_id, physician_label, owner_roles, defect_class_hint,
              notes, sort_order, policy_version)
           VALUES (%s,%s,%s,%s,%s,%s,%s);""", rows)
    conn.commit()
    print(f"[bootstrap] symptom_catalog <- {len(rows)} symptoms ({version})")
    return len(rows)


def seed_demo_physicians(conn) -> int:
    """Synthetic staff for the POC. Never overwrites an existing address."""
    cur = conn.cursor()
    n = 0
    for name, email, role, notify in DEMO_PHYSICIANS:
        cur.execute(
            """INSERT INTO physicians (display_name, email, role, notify_mode)
               SELECT %s,%s,%s,%s
               WHERE NOT EXISTS (SELECT 1 FROM physicians
                                  WHERE lower(email) = lower(%s) AND active);""",
            (name, email, role, notify, email))
        n += cur.rowcount
    conn.commit()
    print(f"[bootstrap] seeded {n} synthetic physician(s)")
    return n


def report(conn) -> None:
    cur = conn.cursor()
    cur.execute("""SELECT table_name FROM information_schema.tables
                    WHERE table_schema='public' ORDER BY table_name;""")
    tables = [r[0] for r in cur.fetchall()]
    print(f"[bootstrap] database: {_env()['dbname']}")
    print(f"[bootstrap] tables ({len(tables)}): {', '.join(tables) or '(none)'}")
    for t in ("physicians", "symptom_catalog", "feedback"):
        if t in tables:
            cur.execute(f"SELECT count(*) FROM {t};")
            print(f"[bootstrap]   {t}: {cur.fetchone()[0]} rows")


# ── entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Bootstrap the longsum support database.")
    ap.add_argument("--create-db", action="store_true",
                    help="CREATE DATABASE if absent (needs a role with CREATEDB).")
    ap.add_argument("--seed-demo", action="store_true",
                    help="Insert synthetic physicians for the POC.")
    ap.add_argument("--check", action="store_true",
                    help="Report current state and exit without writing.")
    args = ap.parse_args()

    if args.check:
        conn = get_support_conn()
        try:
            report(conn)
            verify_schema(conn, fatal=False)
        finally:
            conn.close()
        return

    if args.create_db:
        create_database()

    conn = get_support_conn()
    try:
        apply_schema(conn)
        apply_migrations(conn)
        verify_schema(conn)
        project_symptom_catalog(conn, load_policy())
        if args.seed_demo:
            seed_demo_physicians(conn)
        report(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
