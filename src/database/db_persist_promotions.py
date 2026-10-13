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
db_persist_promotions.py
========================
Backfill / repair tool for the PROMOTION log. holdout.promote() is now the AUTHORITATIVE
writer - it writes the `promotions` table directly, then projects promoted_admissions.csv
FROM it - so this script is NO LONGER part of the fold-in. Two uses:

  default (CSV -> DB)       : ONE-TIME migration of a pre-existing promoted_admissions.csv
                              into the promotions table. INSERT ON CONFLICT(hadm_id) DO
                              NOTHING - idempotent; never duplicates or mutates a promotion.
  --rebuild-csv (DB -> CSV) : regenerate the promoted_admissions.csv CACHE from the
                              authoritative table (repair if the CSV is lost or stale).

    cd C:~/Summarization/clin_longsum/src/database
    python db_persist_promotions.py                # migrate the existing CSV into the table (once)
    python db_persist_promotions.py --subject S    # one subject
    python db_persist_promotions.py --subject S --hadm H    # restrict to one admission (fold-in)
    python db_persist_promotions.py --fresh        # TRUNCATE then full reload from the CSV
    python db_persist_promotions.py --rebuild-csv  # regenerate the CSV from the table
    python db_persist_promotions.py --dry-run

Tables: run promotions_table.sql in PgAdmin first.
Connection: same env vars as db_persist_source.py / d3_load_tiers.py (.env at project root).
"""

from __future__ import annotations

import argparse
import csv
import logging

import psycopg2
import psycopg2.extras

import _bootstrap  # noqa: F401  (puts src/ on sys.path)
import config
from database.pg import get_conn as _pg_get_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("db_persist_promotions")

PROMOTED_CSV = config.INTERIM_DIR / "promoted_admissions.csv"

_SQL = """
INSERT INTO promotions (hadm_id, subject_id, promoted_at)
VALUES %s
ON CONFLICT (hadm_id) DO NOTHING;
"""


def get_conn():
    """Shared connection (database.pg): protected-DB guard + one-line banner."""
    return _pg_get_conn()


def _rows(subject=None, hadm=None) -> list[tuple]:
    if not PROMOTED_CSV.exists():
        return []
    rows = []
    with PROMOTED_CSV.open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                sid, hid = int(r["subject_id"]), int(r["hadm_id"])
            except (TypeError, ValueError, KeyError):
                continue
            if subject is not None and sid != subject:
                continue
            if hadm is not None and hid != hadm:
                continue
            rows.append((hid, sid, (r.get("promoted_at") or None)))
    return rows


def load_promotions(cur, subject=None, hadm=None, fresh=False, dry_run=False) -> int:
    rows = _rows(subject, hadm)
    if dry_run:
        return len(rows)
    if fresh and subject is None and hadm is None:
        cur.execute("TRUNCATE promotions;")
    if rows:
        psycopg2.extras.execute_values(cur, _SQL, rows)
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="Persist the promotion log to the DB.")
    ap.add_argument("--subject", type=int, default=None, help="restrict to one subject_id")
    ap.add_argument("--hadm", type=int, default=None, help="restrict to one hadm_id (fold-in)")
    ap.add_argument("--fresh", action="store_true", help="TRUNCATE promotions then full reload")
    ap.add_argument("--rebuild-csv", action="store_true",
                    help="regenerate promoted_admissions.csv FROM the table (DB -> CSV)")
    ap.add_argument("--dry-run", action="store_true", help="parse + count, no DB writes")
    args = ap.parse_args()
    config.check_paths()

    if args.rebuild_csv:
        if args.dry_run:
            print("--rebuild-csv --dry-run: would regenerate promoted_admissions.csv from the table.")
            return
        import holdout
        n = holdout.project_csv_from_db()
        print("\n" + "=" * 55)
        print(f"REBUILT promoted_admissions.csv from the promotions table: {n} row(s).")
        return

    conn = None if args.dry_run else get_conn()
    n = 0
    try:
        cur = None if args.dry_run else conn.cursor()
        try:
            n = load_promotions(cur, args.subject, args.hadm, args.fresh, args.dry_run)
            if not args.dry_run:
                conn.commit()
        except psycopg2.errors.UndefinedTable as exc:
            if conn:
                conn.rollback()
            raise SystemExit(
                f"\nTable missing: {exc}\nRun promotions_table.sql in PgAdmin first.")
    finally:
        if conn:
            conn.close()

    print("\n" + "=" * 55)
    print("DB PERSIST PROMOTIONS" + ("  (DRY RUN -- no writes)" if args.dry_run else ""))
    print(f"  promotions offered : {n}  (duplicates skipped on conflict)")
    scope = []
    if args.subject is not None:
        scope.append(f"subject={args.subject}")
    if args.hadm is not None:
        scope.append(f"hadm={args.hadm}")
    if scope:
        print(f"  scope              : {', '.join(scope)}")
    if not args.dry_run:
        print("\nPromotion log persisted to the DB.")


if __name__ == "__main__":
    main()
