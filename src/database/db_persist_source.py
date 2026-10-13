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
db_persist_source.py
====================
Persist the pipeline's SOURCE-OF-TRUTH records to Postgres, so the high-fidelity
record lives in the DB and is not held solely on disk (production risk).

  retention_facts.jsonl   (b1)   -> retention_facts    DELETE-then-INSERT per admission
  generation_audit.jsonl  (all)  -> generation_audit   INSERT ON CONFLICT(audit_sha) DO NOTHING

AUDIT SPOOL + MONTHLY ROTATION: generation_audit.jsonl is a write-ahead SPOOL, not
the record. --audit atomically renames it to generation_audit.processing.jsonl,
upserts those records (audit_sha dedup), commits, then appends the processed lines
to audit_archive/generation_audit-YYYYMM.jsonl (next to the spool, so it follows
LONGSUM_NAMESPACE) and deletes the rotated file. Per-fold-in cost is O(records
since last persist); nothing is ever deleted - full provenance =
the DB + the archive segments. Crash-safe: a leftover .processing file is replayed
on the next run (dedup no-ops re-upserts); appends that land after the rename go
to a fresh spool, so no record is lost. In incremental mode --subject does NOT
filter audit records: the fold-in's spool is already subject-scoped, and replaying
crash leftovers from other subjects is free under dedup (filtering would strand
them). --fresh replays archive segments + pending. --dry-run never rotates or
mutates files.

Run AS PART OF THE CASCADE: facts right after b1 (so the fundamental record hits the
DB immediately), audit after the cascade (it is appended by every stage). The fold-in
loads facts for the single spliced admission, then loads audit at the end.

  fact_id   assigned at load if absent  (sha1(source_hadm_id|text); matches
            RetentionFact.assign_fact_id), so ledger fact_ids join to these rows.
  in_ledger DERIVED at load (tax.is_ledger_member) and persisted - the pin decision.
  normalize tax.normalize_fact is applied at load (surveillance promotion +
            disease-course reclassification), so the persisted retention_class /
            salience_tier / negative_kind match what every file reader (b2/c1/b3)
            derives.
  audit_sha sha256 of the canonical record JSON -- idempotent-reload dedup key (also
            collapses the error double-write when the two lines are byte-identical).

    cd src/database
    python db_persist_source.py                       # load both (full)
    python db_persist_source.py --facts               # facts only
    python db_persist_source.py --audit               # audit only
    python db_persist_source.py --subject S --hadm H  # facts for one admission (fold-in)
    python db_persist_source.py --subject S --audit   # that patient's audit rows
    python db_persist_source.py --fresh               # TRUNCATE then full reload
    python db_persist_source.py --dry-run

Tables: apply src/database/schema.sql in PgAdmin first.
Connection: the shared database.pg helper (.env at project root).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from psycopg2.extras import Json

import _bootstrap  # noqa: F401  (puts src/ on sys.path)
import config
import io_utils
import retention_taxonomy as tax
from database.pg import get_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("db_persist_source")

FACTS_JSONL = config.RETENTION_FACTS_JSONL
AUDIT_JSONL = config.AUDIT_JSONL                                    # write-ahead spool
PROCESSING = AUDIT_JSONL.with_suffix(".processing.jsonl")           # rotated, mid-persist
ARCHIVE_DIR = AUDIT_JSONL.parent / "audit_archive"                  # monthly segments

_read_jsonl = io_utils.read_jsonl        # shared tolerant reader


# ── facts ────────────────────────────────────────────────────────────────────────

_FACTS_SQL = """
INSERT INTO retention_facts
    (subject_id, hadm_id, admit_seq, source_hadm_id, fact_id, text, retention_class,
     salience_tier, certainty, lifecycle, occurrence, negative_kind, parent_condition,
     supersedes, rationale, in_ledger)
VALUES %s;
"""


def _fact_id(f: dict) -> str:
    fid = f.get("fact_id")
    if fid:
        return fid
    base = f"{f.get('source_hadm_id')}|{f.get('text', '')}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def _in_ledger(f: dict) -> bool:
    return tax.is_ledger_member(
        f.get("salience_tier"), f.get("certainty", "confirmed"),
        f.get("lifecycle", "active"), f.get("negative_kind"), f.get("retention_class"))


def _fact_rows(recs: list[dict]) -> tuple[list[tuple], set[tuple[int, int]]]:
    rows, adm_keys = [], set()
    for r in recs:
        sid, hid = int(r["subject_id"]), int(r["hadm_id"])
        seq = r.get("admit_seq")
        adm_keys.add((sid, hid))
        for f in r.get("facts", []):
            tax.normalize_fact(f)      # persist the SAME classes the file readers derive
            rows.append((
                sid, hid, seq, f.get("source_hadm_id"), _fact_id(f), f.get("text"),
                f.get("retention_class"), f.get("salience_tier"), f.get("certainty"),
                f.get("lifecycle"), f.get("occurrence"), f.get("negative_kind"),
                f.get("parent_condition"), f.get("supersedes"), f.get("rationale"),
                _in_ledger(f)))
    return rows, adm_keys


def load_facts(cur, subject=None, hadm=None, fresh=False, dry_run=False) -> tuple[int, int]:
    recs = _read_jsonl(FACTS_JSONL)
    if subject is not None:
        recs = [r for r in recs if int(r["subject_id"]) == subject]
    if hadm is not None:
        recs = [r for r in recs if int(r["hadm_id"]) == hadm]
    rows, adm_keys = _fact_rows(recs)
    if dry_run:
        return len(adm_keys), len(rows)
    if fresh and subject is None and hadm is None:
        cur.execute("TRUNCATE retention_facts;")
    else:
        for sid, hid in adm_keys:
            cur.execute("DELETE FROM retention_facts WHERE subject_id = %s AND hadm_id = %s;",
                        (sid, hid))
    if rows:
        psycopg2.extras.execute_values(cur, _FACTS_SQL, rows)
    return len(adm_keys), len(rows)


# ── audit ────────────────────────────────────────────────────────────────────────

_AUDIT_SQL = """
INSERT INTO generation_audit
    (audit_sha, role, provider, model_id, model_version, prompt_version, taxonomy_version,
     params, subject_id, input_hadm_ids, output_sha256, scores, status, error, created_at)
VALUES %s
ON CONFLICT (audit_sha) DO NOTHING;
"""


def _audit_sha(rec: dict) -> str:
    return hashlib.sha256(json.dumps(rec, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _audit_rows(recs: list[dict]) -> list[tuple]:
    rows = []
    for r in recs:
        sid = r.get("subject_id")
        rows.append((
            _audit_sha(r), r.get("role"), r.get("provider"), r.get("model_id"),
            r.get("model_version"), r.get("prompt_version"), r.get("taxonomy_version"),
            Json(r.get("params") or {}), (int(sid) if sid is not None else None),
            [int(h) for h in (r.get("input_hadm_ids") or [])], r.get("output_sha256"),
            Json(r.get("scores") or {}), r.get("status"), r.get("error"), r.get("created_at")))
    return rows


def _collect_pending(mutate: bool) -> list[dict]:
    """Rotate the spool aside (atomic rename) and return every pending record:
    crash-leftover .processing first, then the freshly rotated spool. mutate=False
    (dry-run) reads both in place without renaming or deleting anything."""
    if not mutate:
        return _read_jsonl(PROCESSING) + _read_jsonl(AUDIT_JSONL)
    if AUDIT_JSONL.exists():
        if PROCESSING.exists():
            # Crash leftover AND new spool lines: merge into .processing so one file
            # carries everything pending (replay + new), then clear the spool.
            with PROCESSING.open("a", encoding="utf-8") as out, \
                 AUDIT_JSONL.open("r", encoding="utf-8") as src:
                shutil.copyfileobj(src, out)
            AUDIT_JSONL.unlink()
        else:
            os.replace(AUDIT_JSONL, PROCESSING)   # atomic on NTFS; later appends open a fresh spool
    return _read_jsonl(PROCESSING)


def _archive_pending() -> None:
    """POST-COMMIT ONLY: append the processed lines to this month's archive segment,
    then drop the .processing file. If this step itself fails, .processing survives
    and the next run replays it (dedup) and re-archives -- worst case a duplicated
    archive line, never a lost one."""
    if not PROCESSING.exists():
        return
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    seg = ARCHIVE_DIR / f"generation_audit-{datetime.now(timezone.utc):%Y%m}.jsonl"
    with seg.open("a", encoding="utf-8") as out, PROCESSING.open("r", encoding="utf-8") as src:
        shutil.copyfileobj(src, out)
    PROCESSING.unlink()


def _archive_records() -> list[dict]:
    """All archived audit records, oldest segment first (--fresh replay only --
    never on the fold-in hot path)."""
    if not ARCHIVE_DIR.exists():
        return []
    recs: list[dict] = []
    for seg in sorted(ARCHIVE_DIR.glob("generation_audit-*.jsonl")):
        recs.extend(_read_jsonl(seg))
    return recs


def load_audit(cur, subject=None, fresh=False, dry_run=False) -> int:
    """Incremental (default): upsert ONLY the pending spool + any crash-leftover
    .processing -- O(records since last persist). --subject is intentionally NOT a
    filter here (see module docstring). --fresh: TRUNCATE, then replay archive
    segments + pending (full history)."""
    recs = _collect_pending(mutate=not dry_run)
    if fresh and subject is None:
        recs = _archive_records() + recs
    rows = _audit_rows(recs)
    if dry_run:
        return len(rows)
    if fresh and subject is None:
        cur.execute("TRUNCATE generation_audit;")
    if rows:
        psycopg2.extras.execute_values(cur, _AUDIT_SQL, rows)
    return len(rows)


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Persist source-of-truth records (facts + audit) to the DB.")
    ap.add_argument("--facts", action="store_true", help="load retention_facts only")
    ap.add_argument("--audit", action="store_true", help="load generation_audit only")
    ap.add_argument("--subject", type=int, default=None, help="restrict to one subject_id")
    ap.add_argument("--hadm", type=int, default=None,
                    help="restrict facts to one hadm_id (fold-in; requires --subject)")
    ap.add_argument("--fresh", action="store_true", help="TRUNCATE the target table(s) then full reload")
    ap.add_argument("--dry-run", action="store_true", help="parse + count, no DB writes")
    args = ap.parse_args()
    config.check_paths()

    do_facts = args.facts or not args.audit          # default: both (neither flag set)
    do_audit = args.audit or not args.facts
    if args.hadm is not None and args.subject is None:
        raise SystemExit("--hadm requires --subject")

    conn = None if args.dry_run else get_conn()
    n_adm = n_facts = n_audit = 0
    try:
        cur = None if args.dry_run else conn.cursor()
        try:
            if do_facts:
                n_adm, n_facts = load_facts(cur, args.subject, args.hadm, args.fresh, args.dry_run)
            if do_audit:
                n_audit = load_audit(cur, args.subject, args.fresh, args.dry_run)
            if not args.dry_run:
                conn.commit()
                if do_audit:
                    _archive_pending()   # POST-COMMIT: .processing -> monthly segment
        except psycopg2.errors.UndefinedTable as exc:
            if conn:
                conn.rollback()
            raise SystemExit(
                f"\nTable missing: {exc}\nApply src/database/schema.sql in PgAdmin first.")
    finally:
        if conn:
            conn.close()

    print("\n" + "=" * 55)
    print("DB PERSIST SOURCE" + ("  (DRY RUN -- no writes)" if args.dry_run else ""))
    if do_facts:
        print(f"  retention_facts  : {n_facts} facts across {n_adm} admission(s)")
    if do_audit:
        print(f"  generation_audit : {n_audit} pending record(s) offered (dedup on conflict; "
              f"spool rotated to monthly archive)")
    scope = []
    if args.subject is not None:
        scope.append(f"subject={args.subject}")
    if args.hadm is not None:
        scope.append(f"hadm={args.hadm}")
    if scope:
        print(f"  scope            : {', '.join(scope)}")
    if not args.dry_run:
        print("\nSource-of-truth records persisted to the DB.")


if __name__ == "__main__":
    main()
