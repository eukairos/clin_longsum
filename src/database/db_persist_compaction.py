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
db_persist_compaction.py
========================
Persist the FAITHFULNESS GATE + CORRECTION provenance (C3 + C4) to Postgres, so the
clinical-audit record of why a Prior-Admissions claim was kept, rejected, dropped, or
corrected lives in the DB and is not held solely on disk.

  compaction_faithfulness.jsonl (c3)         -> compaction_review  (c3_* columns)  UPSERT per subject
  compaction_corrected.jsonl    (c4 summary) -> compaction_review  (c4_* columns)  UPDATE per subject
  compaction_corrections_audit.csv (c4 detail) -> compaction_change                DELETE-then-INSERT per subject

Why: tiered_view holds the SERVED corrected prose and generation_audit holds per-stage SUMMARY scores, 
but neither holds the evidence behind the decision - which claims the judge rejected (C3 entailment) or what the
compactor changed and whether it resolved (C4). Those are non-reproducible LLM
outputs and safety-relevant (a no-think judge can wave a claim through), so they
belong in the permanent record.

Run AS PART OF THE FULL REBUILD, right after c4 (c3 is loaded first in the same run
so the parent row exists before C4's child rows). Also IN THE FOLD-IN SUBSET:
foldin_patient.py runs `db_persist_compaction --subject` after c3/c4, so a folded
subject's compaction_review row is refreshed per fold-in and never goes stale.

    cd C:~/Summarization/clin_longsum/src/database
    python db_persist_compaction.py                  # load both (c3 then c4)
    python db_persist_compaction.py --c3             # c3 gate only
    python db_persist_compaction.py --c4             # c4 correction only
    python db_persist_compaction.py --subject S      # one subject
    python db_persist_compaction.py --fresh          # TRUNCATE then full reload
    python db_persist_compaction.py --dry-run

Tables: apply src/database/schema.sql in PgAdmin first.
Connection: the shared database.pg helper (.env at project root).
"""

from __future__ import annotations

import argparse
import csv
import logging

import psycopg2
import psycopg2.extras
from psycopg2.extras import Json

import _bootstrap  # noqa: F401  (puts src/ on sys.path)
import config
import io_utils
from database.pg import get_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("db_persist_compaction")

FAITH_JSONL = config.INTERIM_DIR / "compaction_faithfulness.jsonl"
CORRECTED_JSONL = config.INTERIM_DIR / "compaction_corrected.jsonl"
CHANGES_CSV = config.OUTPUT_DIR / "compaction_corrections_audit.csv"

_read_jsonl = io_utils.read_jsonl        # shared tolerant reader


def _ints(seq) -> list[int]:
    out = []
    for x in seq or []:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            pass
    return out


# ── C3: faithfulness gate -> compaction_review (c3_* columns) ────────────────────

_C3_SQL = """
INSERT INTO compaction_review
    (subject_id, covered_hadm_ids, c3_score, n_total_statements, n_supported, c3_pass,
     c3_judge_think_mode, c3_judge_degraded, c3_judge_attempts, c3_error,
     failed_statements, entailment_detail, c3_loaded_at)
VALUES %s
ON CONFLICT (subject_id) DO UPDATE SET
    covered_hadm_ids    = EXCLUDED.covered_hadm_ids,
    c3_score            = EXCLUDED.c3_score,
    n_total_statements  = EXCLUDED.n_total_statements,
    n_supported         = EXCLUDED.n_supported,
    c3_pass             = EXCLUDED.c3_pass,
    c3_judge_think_mode = EXCLUDED.c3_judge_think_mode,
    c3_judge_degraded   = EXCLUDED.c3_judge_degraded,
    c3_judge_attempts   = EXCLUDED.c3_judge_attempts,
    c3_error            = EXCLUDED.c3_error,
    failed_statements   = EXCLUDED.failed_statements,
    entailment_detail   = EXCLUDED.entailment_detail,
    c3_loaded_at        = NOW();
"""
_C3_TEMPLATE = "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())"


def _c3_rows(recs: list[dict]) -> list[tuple]:
    rows = []
    for r in recs:
        rows.append((
            int(r["subject_id"]),
            _ints(r.get("covered_hadm_ids")),
            r.get("faithfulness_score"),
            r.get("n_total_statements"),
            r.get("n_supported"),
            r.get("pass"),
            r.get("judge_think_mode"),
            r.get("judge_degraded"),
            Json(r.get("judge_attempts")) if r.get("judge_attempts") is not None else None,
            r.get("error"),
            Json(r.get("failed_statements") or []),
            Json(r.get("entailment_detail") or []),
        ))
    return rows


def load_c3(cur, subject=None, fresh=False, dry_run=False) -> int:
    recs = _read_jsonl(FAITH_JSONL)
    if subject is not None:
        recs = [r for r in recs if int(r.get("subject_id", -1)) == subject]
    rows = _c3_rows(recs)
    if dry_run:
        return len(rows)
    if fresh and subject is None:
        cur.execute("TRUNCATE compaction_review CASCADE;")   # cascades to compaction_change
    if rows:
        psycopg2.extras.execute_values(cur, _C3_SQL, rows, template=_C3_TEMPLATE)
    return len(rows)


# ── C4: correction outcome -> compaction_review (c4_*) + compaction_change ───────

_C4_STUB_SQL = """
INSERT INTO compaction_review (subject_id, covered_hadm_ids)
VALUES (%s, %s) ON CONFLICT (subject_id) DO NOTHING;
"""

_C4_UPDATE_SQL = """
UPDATE compaction_review SET
    corrected           = TRUE,
    c4_original_score   = %s,
    c4_corrected_score  = %s,
    c4_pass_after       = %s,
    c4_n_changes        = %s,
    c4_unresolved       = %s,
    c4_judge_think_mode = %s,
    c4_judge_degraded   = %s,
    c4_judge_attempts   = %s,
    c4_status           = %s,
    c4_loaded_at        = NOW()
WHERE subject_id = %s;
"""

_CHANGE_SQL = """
INSERT INTO compaction_change
    (subject_id, action, claim, correction, model_reason, validator_evidence,
     resolved_after, judge_degraded, source_hadm_ids)
VALUES %s;
"""


def _bool(v):
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    return None


def _changes_by_subject(subject=None) -> dict[int, list[tuple]]:
    """Per-subject change rows from the C4 per-claim CSV."""
    out: dict[int, list[tuple]] = {}
    if not CHANGES_CSV.exists():
        return out
    with CHANGES_CSV.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                sid = int(row.get("subject_id", ""))
            except (TypeError, ValueError):
                continue
            if subject is not None and sid != subject:
                continue
            src = _ints((row.get("source_hadm_ids") or "").split(";")) \
                if row.get("source_hadm_ids") else []
            out.setdefault(sid, []).append((
                sid, row.get("action"), row.get("claim"), row.get("correction"),
                row.get("model_reason"), row.get("validator_evidence"),
                _bool(row.get("resolved_after")), _bool(row.get("judge_degraded")), src))
    return out


def load_c4(cur, subject=None, dry_run=False) -> tuple[int, int]:
    recs = _read_jsonl(CORRECTED_JSONL)
    if subject is not None:
        recs = [r for r in recs if int(r.get("subject_id", -1)) == subject]
    changes = _changes_by_subject(subject)
    n_subj = len(recs)
    n_changes = sum(len(v) for v in changes.values())
    if dry_run:
        return n_subj, n_changes
    for r in recs:
        sid = int(r["subject_id"])
        sc = (r.get("audit") or {}).get("scores", {}) or {}
        status = (r.get("audit") or {}).get("status")
        cur.execute(_C4_STUB_SQL, (sid, _ints(r.get("covered_hadm_ids"))))
        cur.execute(_C4_UPDATE_SQL, (
            sc.get("original_score"), sc.get("corrected_score"), sc.get("pass_after"),
            sc.get("n_changes"), sc.get("unresolved"), sc.get("judge_think_mode"),
            sc.get("judge_degraded"),
            Json(sc.get("judge_attempts")) if sc.get("judge_attempts") is not None else None,
            status, sid))
        cur.execute("DELETE FROM compaction_change WHERE subject_id = %s;", (sid,))
        rows = changes.get(sid, [])
        if rows:
            psycopg2.extras.execute_values(cur, _CHANGE_SQL, rows)
    return n_subj, n_changes


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Persist the faithfulness gate + correction provenance (C3 + C4) to the DB.")
    ap.add_argument("--c3", action="store_true", help="load the C3 gate only")
    ap.add_argument("--c4", action="store_true", help="load the C4 correction only")
    ap.add_argument("--subject", type=int, default=None, help="restrict to one subject_id")
    ap.add_argument("--fresh", action="store_true",
                    help="TRUNCATE compaction_review (+ cascade) then full reload")
    ap.add_argument("--dry-run", action="store_true", help="parse + count, no DB writes")
    args = ap.parse_args()
    config.check_paths()

    do_c3 = args.c3 or not args.c4          # default: both (neither flag set)
    do_c4 = args.c4 or not args.c3

    conn = None if args.dry_run else get_conn()
    n_c3 = n_c4_subj = n_c4_changes = 0
    try:
        cur = None if args.dry_run else conn.cursor()
        try:
            if do_c3:
                n_c3 = load_c3(cur, args.subject, args.fresh, args.dry_run)
            if do_c4:
                n_c4_subj, n_c4_changes = load_c4(cur, args.subject, args.dry_run)
            if not args.dry_run:
                conn.commit()
        except psycopg2.errors.UndefinedTable as exc:
            if conn:
                conn.rollback()
            raise SystemExit(
                f"\nTable missing: {exc}\nApply src/database/schema.sql in PgAdmin first.")
    finally:
        if conn:
            conn.close()

    print("\n" + "=" * 55)
    print("DB PERSIST COMPACTION" + ("  (DRY RUN -- no writes)" if args.dry_run else ""))
    if do_c3:
        print(f"  compaction_review (C3) : {n_c3} subject(s)")
    if do_c4:
        print(f"  compaction_review (C4) : {n_c4_subj} subject(s) updated")
        print(f"  compaction_change      : {n_c4_changes} claim change(s)")
    if args.subject is not None:
        print(f"  scope                  : subject={args.subject}")
    if not args.dry_run:
        print("\nFaithfulness gate + correction provenance persisted to the DB.")


if __name__ == "__main__":
    main()
