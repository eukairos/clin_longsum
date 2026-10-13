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
foldin_patient.py
=================
Background per-patient fold-in: promotes a physician-approved incoming admission into
the established record, then rebuild that ONE subject through the established stages.

Triggered by the tiered review submit endpoint (FastAPI BackgroundTask) after approval.
Steps:

  b1_retention_extractor.py --subject S --hadm H   # extract the incoming admission's facts
                                                   # and splice into retention_facts.jsonl
  db_persist_source.py --subject S --hadm H --facts # [DB WRITE] persist those facts to the DB
                                                    # (source of truth in DB, not disk-only)
  holdout.promote(subject, hadm)          # [DB WRITE] promotions table = source of truth;
                                          # projects promoted_admissions.csv from it
  b2_build_ledger.py       --subject S    # ledger incl. the promoted admission's facts
  b4_consolidate_ledger.py --subject S    # re-consolidate (only if CONSOLIDATE_LEDGER)
  c1_compactor.py          --subject S    # recompute compaction (hot-window boundary shifts)
  d1_paragrapher.py        --subject S    # paragraph the just-promoted admission (so the NEXT
                                          # discharge finds a paragraph when it demotes this one
                                          # out of the current-episode slot - the demotion gap)
  c2_retention_validator.py --subject S   # re-check pinned-fact retention over the span the
                                          # fold-in just aged into compaction (post-commit flag)
  c3_faithfulness.py       --subject S    # re-judge the fresh Prior Admissions paragraph (deepseek)
  c4_correct.py            --subject S    # correct it if C3 failed (served via d_coordinator)
  d2_tldr.py               --subject S    # re-synthesize TL;DR
  d_coordinator.py         --subject S    # reassemble the view (incoming now cleared)
  d3_load_tiers.py --subject S --set-folded-in   # reload tier tables (incl. tiered_ledger_raw)
                                                 # + set folded_in_at, one transaction
  e2_embed_incoming.py --subject S --hadm H  # [DB WRITE] embed the incoming admission's
                                             # bhc_delta for RAG (skipped in dry-run)
  db_persist_compaction.py --subject S    # [DB WRITE] persist C3/C4 faithfulness + correction provenance
  db_persist_retention.py  --subject S    # [DB WRITE] persist C2 MISSING / PINNED_IN_ROLLUP flags
  db_persist_source.py --subject S --audit  # persist this patient's audit trail to the DB
  c5_review_queue.py --subject S          # notify the physician re-review queue from the
                                          # PERSISTED flags (best-effort; runs last)

Chain order groups same-model stages: b4/c1/d1 are contiguous ornith:35b runs (d1 and
c2 depend only on the cohort CSV / c1 outputs, not on c3/c4), so the deepseek judge
loads once instead of interleaving. foldin sets LONGSUM_SKIP_UNLOAD=1 for b4 and c1,
whose successor uses the same model, so the shared unload helper (vendor/ollama_utils)
leaves ornith resident across those subprocess boundaries.

The b1 step is REQUIRED under the holdout-clean model: b1 extracts the
ESTABLISHED span only, so the incoming admission's facts are NOT in retention_facts.jsonl
until this fold-in extracts them. This mirrors production, where a brand-new admission
arrives, is extracted, then folds in. extract_one_admission rebuilds the feed-forward
known_conditions from the patient's earlier stored admissions, so the one-off extraction
sees the same prior context a full sequential pass would.

The promoted admission was the patient's latest, so after fold-in it has no incoming
and drops off the EHR's discharge tasklist. d3 preserves the review_* columns, so the physician's
approval survives the reload, and folded_in_at is committed atomically with the tier rows.

Each stage runs as its own subprocess (clean imports, and the LLM stages unload VRAM on
exit), sequentially - one patient's worth of model calls, off the request path. The
stages are individually atomic and idempotent, so a failed fold-in can simply be re-run.

    python foldin_patient.py --subject 90000123 --hadm 80000123
    python foldin_patient.py --subject 90000123 --hadm 80000123 --dry-run
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import config
import holdout

HERE = Path(__file__).resolve().parent
DB_DIR = HERE / "database"          # d3 / db_persist_* live under src/database/

# Stages whose script lives under src/database/ rather than src/ root. _stage_path()
# resolves each spawned stage to the correct directory (root stages b1/b2/b4/c1/c3/c4/
# d2/d_coordinator are unchanged; d3 + the db_persist_* helpers are under database/).
_DB_STAGES = {"db_persist_source.py", "d3_load_tiers.py", "db_persist_compaction.py",
              "db_persist_retention.py"}

# Stages whose SUCCESSOR in the chain uses the same generation model (ornith:35b):
# spawn these with LONGSUM_SKIP_UNLOAD=1 so vendor/ollama_utils.ollama_unload leaves
# the model resident instead of evicting at subprocess exit. d1 is NOT here - its
# next LLM stage is the deepseek judge (c3), and two 32-35b models do not co-reside
# in the 5090's VRAM.
_SKIP_UNLOAD_STAGES = {"b4_consolidate_ledger.py", "c1_compactor.py"}


def _stage_path(stage: str) -> Path:
    return (DB_DIR if stage in _DB_STAGES else HERE) / stage


# Order groups same-model runs: ornith (b4, c1, d1) -> deterministic (c2) -> deepseek
# (c3) -> ornith+deepseek (c4) -> ornith (d2). d1 needs only the cohort CSV and c2
# only c1's output + the raw ledger, so both legally precede c3/c4.
CHAIN = ["b2_build_ledger.py", "b4_consolidate_ledger.py", "c1_compactor.py",
         "d1_paragrapher.py",            # paragraphs the just-promoted admission (closes the
                                         # demotion gap: it needs a paragraph before the NEXT
                                         # discharge demotes it out of the current-episode slot)
         "c2_retention_validator.py",    # re-check retention over the newly-compacted span
         "c3_faithfulness.py", "c4_correct.py",
         "d2_tldr.py", "d_coordinator.py", "d3_load_tiers.py"]


# Single global advisory-lock key. The fold-in critical section read-modify-writes
# COHORT-WIDE shared artifacts (retention_facts/ledger/compaction/view JSONL +
# promoted_admissions.csv), so only ONE fold-in may run at a time across ALL processes
# and uvicorn workers. A session-level Postgres advisory lock gives that cross-process
# guarantee - the in-process threading.Lock in longsum_api.py only covers a single
# worker, and a manual CLI fold-in bypasses that lock entirely. The lock auto-releases
# if the holder crashes (connection drop), so there is no stale-lock risk.
_FOLDIN_LOCK_KEY = 7704140419
_ENV_PATH = config.PROJECT_ROOT / ".env"


@contextmanager
def _foldin_advisory_lock(dry_run: bool):
    """Hold the cross-process fold-in lock for the whole cascade; block until a
    concurrent fold-in finishes. A real run REQUIRES the lock (and the DB is up anyway,
    since d3 writes to it); a dry-run proceeds lockless if the DB is unreachable
    (it withholds DB writes regardless)."""
    from database.pg import get_conn
    try:
        conn = get_conn()
    except Exception as exc:
        if dry_run:
            print(f"[foldin] advisory lock skipped (DB unreachable in dry-run): {exc}")
            yield
            return
        raise SystemExit(f"[foldin] cannot acquire fold-in lock - DB unreachable: {exc}")
    conn.autocommit = True
    cur = conn.cursor()
    try:
        cur.execute("SELECT pg_try_advisory_lock(%s);", (_FOLDIN_LOCK_KEY,))
        if not cur.fetchone()[0]:
            print("[foldin] another fold-in holds the lock - waiting for it to finish...")
            cur.execute("SELECT pg_advisory_lock(%s);", (_FOLDIN_LOCK_KEY,))   # blocks
        yield
    finally:
        try:
            cur.execute("SELECT pg_advisory_unlock(%s);", (_FOLDIN_LOCK_KEY,))
        except Exception:
            pass
        cur.close()
        conn.close()   # closing the session also releases the lock


def _stage_env(stage: str) -> dict | None:
    """Per-stage subprocess env: same-model successor stages get LONGSUM_SKIP_UNLOAD=1
    (see _SKIP_UNLOAD_STAGES); everything else inherits unchanged (None)."""
    if stage in _SKIP_UNLOAD_STAGES:
        return {**os.environ, "LONGSUM_SKIP_UNLOAD": "1"}
    return None


def _run(stage: str, subject_id: int, extra: list[str] | None = None) -> None:
    cmd = [sys.executable, str(_stage_path(stage)), "--subject", str(subject_id), *(extra or [])]
    r = subprocess.run(cmd, cwd=str(HERE), capture_output=True, text=True, env=_stage_env(stage))
    last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    print(f"[foldin {subject_id}] {stage}: {last}")
    if r.returncode != 0:
        print(f"[foldin {subject_id}] {stage} FAILED (rc={r.returncode})\n{r.stderr}")
        raise SystemExit(f"{stage} failed for subject {subject_id}")


def _run_soft(stage: str, subject_id: int, extra: list[str] | None = None) -> None:
    """Best-effort post-commit stage (notification). Logs a failure but does NOT abort:
    the fold-in's DB writes have already committed, so a failed notify must not fail it."""
    cmd = [sys.executable, str(_stage_path(stage)), "--subject", str(subject_id), *(extra or [])]
    r = subprocess.run(cmd, cwd=str(HERE), capture_output=True, text=True)
    last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    print(f"[foldin {subject_id}] {stage}: {last}")
    if r.returncode != 0:
        print(f"[foldin {subject_id}] {stage} soft-failed (rc={r.returncode}) -- fold-in "
              f"already committed; continuing.\n{r.stderr[:500]}")

def _fold_in_steps(subject_id: int, hadm_id: int, dry_run: bool = False) -> None:
    # Step 0: extract the incoming admission's facts and splice into retention_facts.jsonl.
    # Under the holdout-clean model b1 extracts the established span only, so the incoming
    # admission is not yet in the fact store; the ledger cannot project it until this runs.
    # Runs as a subprocess (loads + unloads the generation model itself). Faithful in
    # dry-run too - the chain already mutates intermediate JSONL; only the Postgres commit
    # and folded_in_at are withheld.
    _run("b1_retention_extractor.py", subject_id, ["--hadm", str(hadm_id)])
    # Step 0b: persist the new facts to the DB immediately -- the fundamental source of
    # truth (retention_facts) must live in the DB, not solely on disk.
    _run("db_persist_source.py", subject_id,
         ["--facts", "--hadm", str(hadm_id)] + (["--dry-run"] if dry_run else []))
    # Step 1: promote: writes the `promotions` table (DB = source of truth) and projects
    # promoted_admissions.csv from it; the admission rejoins the established span for the
    # downstream stages. promote() is the authoritative writer, so there is no separate
    # DB-persist step. In dry-run it appends only to the CSV cache (table stays clean).
    holdout.promote(subject_id, hadm_id, dry_run=dry_run)
    for stage in CHAIN:
        if stage == "b4_consolidate_ledger.py" and not config.CONSOLIDATE_LEDGER:
            continue
        if stage == "d3_load_tiers.py":
            extra = ["--dry-run"] if dry_run else ["--set-folded-in"]
        else:
            extra = None
        _run(stage, subject_id, extra)
    # Embed the just-folded admission's bhc_delta (RAG). DB write, scoped to this hadm so
    # not-yet-approved holdout admissions are never pre-embedded. e2 has no --dry-run, so
    # skip it in dry-run (the a2-owned bhc_delta is already on disk regardless).
    if dry_run:
        print(f"[foldin {subject_id}] e2_embed_incoming.py: skipped (dry-run, DB write)")
    else:
        _run("e2_embed_incoming.py", subject_id, ["--hadm", str(hadm_id)])
    # Persist provenance BEFORE the notifier reads it (c5 reads the persisted flags):
    _run("db_persist_compaction.py", subject_id, ["--dry-run"] if dry_run else [])   # c3/c4 gate
    _run("db_persist_retention.py", subject_id, ["--dry-run"] if dry_run else [])     # c2 flags
    # Persist this patient's audit trail (every stage above appended to generation_audit.jsonl).
    _run("db_persist_source.py", subject_id, ["--audit"] + (["--dry-run"] if dry_run else []))
    # Notify the physician re-review queue from the PERSISTED flags. Best-effort, last, and
    # skipped in dry-run (nothing was persisted to read).
    if dry_run:
        print(f"[foldin {subject_id}] c5_review_queue.py: skipped (dry-run)")
    else:
        _run_soft("c5_review_queue.py", subject_id, [])
    if dry_run:
        print(f"[foldin {subject_id}] DRY RUN complete - d3 parsed only (no Postgres writes), "
              f"folded_in_at NOT set.")
        print(f"[foldin {subject_id}] note: intermediate JSONL (incl. retention_facts.jsonl, "
              f"now carrying the incoming admission's facts) and promoted_admissions.csv WERE "
              f"updated. To fully revert, remove subject {subject_id} from "
              f"promoted_admissions.csv, re-run b1 --fresh (or b1_rerun_hadms for the spliced "
              f"hadm), and re-run the full cohort pipeline.")
        return
    print(f"[foldin {subject_id}] done -- admission {hadm_id} folded into the record "
          f"(folded_in_at set atomically by d3).")


def fold_in_patient(subject_id: int, hadm_id: int, dry_run: bool = False) -> None:
    """Public entry: serialize the entire cascade behind the cross-process advisory lock
    so concurrent fold-ins (multiple uvicorn workers, or a manual CLI run alongside an
    API-triggered one) cannot corrupt the shared read-modify-write artifacts."""
    with _foldin_advisory_lock(dry_run):
        _fold_in_steps(subject_id, hadm_id, dry_run)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", type=int, required=True)
    ap.add_argument("--hadm", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="run the chain with d3 in dry-run (no Postgres writes); skip folded_in_at")
    args = ap.parse_args()
    fold_in_patient(args.subject, args.hadm, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
