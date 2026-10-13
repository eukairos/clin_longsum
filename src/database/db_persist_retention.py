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
db_persist_retention.py
=======================
Persist C2 retention-completeness review flags to Postgres, so the record of which
pinned facts went MISSING (genuine loss) or landed PINNED_IN_ROLLUP (mis-tag candidate)
lives in the DB and drives the physician re-review queue and is not held solely on disk.

  retention_completeness.jsonl (c2 per-subject) -> per-subject context (recall / counts)
  retention_review.csv         (c2 item rows)   -> retention_review  (one row per flag)

Why: generation_audit holds the c2 SUMMARY scores, but not the per-fact rows (which fact, which admission, which issue) 
the reviewer needs to triage. This is the retention analogue of db_persist_compaction (which persists the C3/C4 gate).

Post-commit flag : c2 runs INSIDE the fold-in cascade, after approval, so these
are re-review flags, not a pre-approval gate.

Idempotent per subject: UPSERT on the natural key (subject_id, issue, source_hadm_id,
fact_text) refreshes recall/counts/checked_at but PRESERVES review_status, so a physician's
triage survives a re-fold. Flags no longer present after the window shifts are cleared:
after upserting, this subject's still-'pending_review' rows with an older checked_at are
marked 'auto_cleared' (resolved / dismissed rows are kept).

Run AS PART OF THE FOLD-IN, after c2 (foldin_patient.py runs db_persist_retention --subject
after c2, before c5 reads the queue). Also runnable cohort-wide in the full rebuild after c2.

    cd C:~/Summarization/clin_longsum/src/database
    python db_persist_retention.py                 # whole cohort
    python db_persist_retention.py --subject S      # one subject (fold-in)
    python db_persist_retention.py --fresh          # TRUNCATE then full reload
    python db_persist_retention.py --dry-run

Tables: apply src/database/schema.sql in PgAdmin first.
Connection: the shared database.pg helper (.env at project root).
"""

from __future__ import annotations

import argparse
import csv
import logging

import psycopg2
import psycopg2.extras

import _bootstrap # noqa: F401
import config
import io_utils
from database.pg import get_conn, retention_visibility_gaps

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("db_persist_retention")

COMPLETENESS_JSONL = config.INTERIM_DIR / "retention_completeness.jsonl"
REVIEW_CSV = config.OUTPUT_DIR / "retention_review.csv"

_read_jsonl = io_utils.read_jsonl        # shared tolerant reader


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _context_by_subject(subject=None) -> dict[int, dict]:
    """Per-subject c2 context from retention_completeness.jsonl (recall / counts /
    versions). Also the authoritative set of PROCESSED subjects (a clean subject appears
    here with no CSV rows, and its stale flags must still be cleared)."""
    ctx: dict[int, dict] = {}
    for r in _read_jsonl(COMPLETENESS_JSONL):
        audit = r.get("audit") or {}
        sid = _int_or_none(r.get("subject_id") if r.get("subject_id") is not None
                           else audit.get("subject_id"))
        if sid is None or (subject is not None and sid != subject):
            continue
        sc = audit.get("scores") or {}
        ctx[sid] = {
            "recall": r.get("score"),
            "complete": r.get("complete"),
            "n_pinned": sc.get("n_pinned"),
            "n_missing": sc.get("n_missing"),
            "n_pinned_in_rollup": sc.get("n_pinned_in_rollup"),
            "taxonomy_version": audit.get("taxonomy_version"),
            "prompt_version": audit.get("prompt_version"),
        }
    return ctx


def _item_rows(ctx: dict[int, dict], run_ts, subject=None) -> list[tuple]:
    """Join each retention_review.csv flag row with its subject context -> insert tuple."""
    if not REVIEW_CSV.exists():
        return []
    rows: list[tuple] = []
    with REVIEW_CSV.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            sid = _int_or_none(row.get("subject_id"))
            if sid is None or (subject is not None and sid != subject):
                continue
            c = ctx.get(sid, {})
            rows.append((
                sid,
                row.get("issue"),
                (str(row.get("salience_tier")) if row.get("salience_tier") not in (None, "") else None),
                row.get("retention_class"),
                row.get("certainty"),
                row.get("text"),
                _int_or_none(row.get("source_hadm_id")),
                c.get("recall"), c.get("complete"),
                c.get("n_pinned"), c.get("n_missing"), c.get("n_pinned_in_rollup"),
                c.get("taxonomy_version"), c.get("prompt_version"),
                run_ts,
            ))
    return rows


_INSERT_SQL = """
INSERT INTO retention_review
    (subject_id, issue, salience_tier, retention_class, certainty, fact_text, source_hadm_id,
     recall, complete, n_pinned, n_missing, n_pinned_in_rollup, taxonomy_version, prompt_version,
     checked_at)
VALUES %s
ON CONFLICT (subject_id, issue, COALESCE(source_hadm_id, -1), fact_text) DO UPDATE SET
    salience_tier      = EXCLUDED.salience_tier,
    retention_class    = EXCLUDED.retention_class,
    certainty          = EXCLUDED.certainty,
    recall             = EXCLUDED.recall,
    complete           = EXCLUDED.complete,
    n_pinned           = EXCLUDED.n_pinned,
    n_missing          = EXCLUDED.n_missing,
    n_pinned_in_rollup = EXCLUDED.n_pinned_in_rollup,
    taxonomy_version   = EXCLUDED.taxonomy_version,
    prompt_version     = EXCLUDED.prompt_version,
    checked_at         = EXCLUDED.checked_at;
    -- review_status intentionally NOT overwritten: preserve physician triage across re-folds.
"""

# Stale-flag clear: a pending flag not re-affirmed this run (older checked_at) no longer
# applies after the window shifted - marked it auto-cleared. Resolved / dismissed rows are kept.
_STALE_SQL = """
UPDATE retention_review
   SET review_status = 'auto_cleared', checked_at = %s
 WHERE subject_id = ANY(%s)
   AND review_status = 'pending_review'
   AND checked_at < %s;
"""


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Persist C2 retention-completeness review flags to the DB.")
    ap.add_argument("--subject", type=int, default=None, help="restrict to one subject_id")
    ap.add_argument("--fresh", action="store_true",
                    help="TRUNCATE retention_review then full reload (drops all review_status)")
    ap.add_argument("--dry-run", action="store_true", help="parse + count, no DB writes")
    args = ap.parse_args()
    config.check_paths()

    ctx = _context_by_subject(args.subject)
    processed = sorted(ctx.keys())

    if args.dry_run:
        rows = _item_rows(ctx, None, args.subject)
        n_missing = sum(1 for r in rows if r[1] == "MISSING")
        n_rollup = sum(1 for r in rows if r[1] == "PINNED_IN_ROLLUP")
        print("\n" + "=" * 55)
        print("DB PERSIST RETENTION  (DRY RUN -- no writes)")
        print(f"  subjects processed : {len(processed)}")
        print(f"  flag rows          : {len(rows)}  (MISSING {n_missing}, PINNED_IN_ROLLUP {n_rollup})")
        if args.subject is not None:
            print(f"  scope              : subject={args.subject}")
        return

    conn = get_conn()
    n_rows = n_stale = 0
    gaps: list[dict] = []
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT now();")
            run_ts = cur.fetchone()[0]
            if args.fresh and args.subject is None:
                cur.execute("TRUNCATE retention_review;")
            rows = _item_rows(ctx, run_ts, args.subject)
            if rows:
                psycopg2.extras.execute_values(cur, _INSERT_SQL, rows)
            n_rows = len(rows)
            if processed and not args.fresh:
                cur.execute(_STALE_SQL, (run_ts, processed, run_ts))
                n_stale = cur.rowcount
            conn.commit()
            # Write-time half of the retention-visibility guard. The RETENTION tab is the
            # ONLY surface for c2 losses, so a subject flagged by c2 with no rows here is
            # invisible to the physician. Checked post-commit against what we just wrote.
            gaps = retention_visibility_gaps(conn, sorted(processed) if processed else None)
        except psycopg2.errors.UndefinedTable as exc:
            conn.rollback()
            raise SystemExit(
                f"\nTable missing: {exc}\nApply src/database/schema.sql in PgAdmin first.")
    finally:
        conn.close()

    print("\n" + "=" * 55)
    print("DB PERSIST RETENTION")
    print(f"  subjects processed  : {len(processed)}")
    print(f"  flag rows upserted  : {n_rows}")
    print(f"  stale flags cleared : {n_stale}  (pending, no longer present)")
    if args.subject is not None:
        print(f"  scope               : subject={args.subject}")
    if gaps:
        print("\n" + "!" * 55)
        print("RETENTION VISIBILITY GAP -- these subjects' c2 losses are NOT reviewable:")
        for g in gaps:
            print(f"  subject {g['subject_id']}: c2 reported "
                  f"{g['n_missing']} MISSING / {g['n_rollup']} PINNED_IN_ROLLUP at {g['c2_at']}, "
                  f"but retention_review has no rows at or after that run.")
        print("The RETENTION tab is the only surface for these -- they will not appear in")
        print("TRIAGE. Re-run c2_retention_validator.py then this stage for the subjects above.")
        raise SystemExit("\nAborting: retention losses persisted to the DB would be invisible.")

    print("\nRetention-review flags persisted to the DB.")


if __name__ == "__main__":
    main()
