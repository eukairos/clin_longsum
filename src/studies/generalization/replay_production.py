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
replay_production.py  --  GENERALISATION, production-stage simulation
=====================================================================
Replays the held-out admissions of the generalisation cohort as a STREAM of actualised
discharges, one physician-approved fold-in at a time, to exercise the production path
that setup does not: d1 (paragraph the just-folded admission), c2 (retention re-check),
e2 (embed the incoming), and the db_persist_* + c5 re-review notify inside foldin_patient.

This is the SECOND of the two generalisation activities and is DISTINCT from the file-only
overfitting eval (draw_cohort_gen.py + compare_generalization.py):

  overfitting eval : frozen pipeline over the ESTABLISHED span, file-only, namespaced, NO DB.
  production replay : setup + fold-in WITH DB writes (e2 / d3 / db_persist_*), so it needs an
                      ISOLATED DB -- never the dev ledger-of-record.

Why k >= 2 trailing admissions (draw_cohort_gen.py --k K): the d1 demotion gap only appears
on the SECOND consecutive discharge. Fold-in N makes admission N the current episode (rendered
raw, no paragraph needed); fold-in N+1 demotes N into the hot window, where it now needs a d1
paragraph. A single-admission holdout can never surface that - so each patient must contribute
>= 2 discharges, replayed OLDEST-FIRST.

PREREQUISITES (all under the SAME namespace + isolated DB):
  1. LONGSUM_NAMESPACE set (e.g. gen_prod); .env PGDATABASE -> longitudinal_bhc_gen (NOT the dev DB).
  2. src/database/schema.sql applied in that DB (it creates every table incl. retention_review
     and review_queue; the old per-table *_tables.sql files are consolidated into it).
  3. draw_cohort_gen.py --k K  ->  cohort_holdout.csv holds the k discharges/patient.
  4. SETUP done: full pipeline over the ESTABLISHED span into the gen DB
     (b1 -> ... -> d3, a4 embed-established, db_persist_*). Do NOT run e1 / e2 in setup --
     the incoming preview + embedding are what this replay triggers.

GOTCHA -- A FULL REPLAY PERMANENTLY LOSES f6 FOR THIS RUN (read before running without
--stop-after). e1_incoming_summary.py is NOT in foldin_patient.CHAIN; only e2_embed_incoming
runs per fold-in (a DB write for similarity search; it does not populate incoming_summary.jsonl
or tiered_view.incoming_*). Once every discharge in a patient's holdout is promoted,
holdout.holdout_hadms() is empty and NOTHING remains for e1 to preview - and there is no
un-fold operation anywhere in this pipeline, so this is not recoverable after the fact for the
same cohort. f6_incoming_ref.py will SystemExit ("incoming_summary.jsonl not found/empty") and
stay that way. f6 is track-only (never a hard gate), so nothing else in the eval sequence is
blocked -- but if an UNSEEN-DATA f6 number matters for this run, checkpoint BEFORE the final
fold-in per patient:

    python studies/generalization/replay_production.py --stop-after 1     # k=2: parks 1/patient
    python e1_incoming_summary.py --fresh                                 # preview the survivor
    python database/d3_load_tiers.py                                      # populate incoming_*
    python evaluation/f6_incoming_ref.py --fresh                          # score it HERE
    python studies/generalization/replay_production.py --resume           # THEN finish the aging test

Running f6 after a full (--resume with nothing parked, or no --stop-after ever used) replay
measures nothing: 0 patients scored, not a failure of the pipeline. (First hit 2026-08-04 on
the n=10/k=2 gen cohort -- replayed in one shot with no checkpoint, so f6 was unobtainable for
that run; documented in handover.md rather than redone, since f6 is track-only and every other
gen metric was already in hand.)

    cd C:~/Summarization/clin_longsum/src
    $env:LONGSUM_NAMESPACE="gen_prod"
    python studies/generalization/replay_production.py --dry-run     # rehearse, no DB writes
    python studies/generalization/replay_production.py               # real replay (loses f6 -- see GOTCHA)
    python studies/generalization/replay_production.py --stop-after 1 # checkpoint for f6 (k=2, see GOTCHA)
    python studies/generalization/replay_production.py --subject S    # one patient
    python studies/generalization/replay_production.py --resume       # skip already-folded
    python studies/generalization/replay_production.py --limit 5      # cap number of subjects
    python studies/generalization/replay_production.py --allow-dev-db # DANGER: overrides the guard against replaying into the dev
                                                                      # ledger-of-record DB; only for a throwaway/test DB

Each fold-in is serialized by foldin_patient's own advisory lock, so this driver runs them
strictly sequentially (subprocess, wait) -- one patient's model calls at a time.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # tiered_longsum/ on the path

import config
import holdout

ROOT = Path(__file__).resolve().parents[2]        # src/
FOLDIN = ROOT / "foldin_patient.py"
HOLDOUT_CSV = config.INTERIM_DIR / "cohort_holdout.csv"
# Ledger-of-record databases the replay must never write into. `longitudinal_bhc` is the
# original dev DB; `longitudinal_bhc_test` is the CURRENT dev/test ledger-of-record, and
# omitting it left the guard protecting a database nobody uses any more.
DEV_DBS = {"longitudinal_bhc", "longitudinal_bhc_test"}


def _holdout_admissions(subject: int | None) -> pd.DataFrame:
    if not HOLDOUT_CSV.exists():
        raise SystemExit(
            f"holdout file not found: {HOLDOUT_CSV}\nRun draw_cohort_gen.py --k K first "
            f"(under this namespace).")
    df = pd.read_csv(HOLDOUT_CSV)
    if subject is not None:
        df = df[df["subject_id"].astype(int) == subject]
        if df.empty:
            raise SystemExit(f"subject {subject} has no held-out admissions in {HOLDOUT_CSV.name}.")
    # OLDEST-FIRST within patient is mandatory (each fold-in promotes the admission the next
    # one is demoted past). Across patients the order is irrelevant.
    return df.sort_values(["subject_id", "admit_seq"]).reset_index(drop=True)


def _foldin(subject_id: int, hadm_id: int, dry_run: bool) -> int:
    cmd = [sys.executable, str(FOLDIN), "--subject", str(subject_id), "--hadm", str(hadm_id)]
    if dry_run:
        cmd.append("--dry-run")
    return subprocess.run(cmd, cwd=str(ROOT)).returncode


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Replay held-out admissions as a production discharge stream (fold-in each).")
    ap.add_argument("--subject", type=int, default=None, help="replay one subject only")
    ap.add_argument("--limit", type=int, default=None, help="cap number of subjects")
    ap.add_argument("--stop-after", type=int, default=None, metavar="N",
                    help="fold in at most N discharges PER PATIENT (oldest-first), leaving the "
                         "rest held out. The cap counts discharges already folded, so it is "
                         "idempotent: --stop-after 1 twice is a no-op, and --stop-after 2 "
                         "--resume continues. With k=2, --stop-after 1 parks the run at the "
                         "checkpoint where each patient's LAST discharge is the sole remaining "
                         "incoming -- the state f6 and the review UI's incoming panel are "
                         "designed for. Run e1_incoming_summary.py + d3_load_tiers.py there to "
                         "populate tiered_view.incoming_*.")
    ap.add_argument("--resume", action="store_true",
                    help="skip admissions already promoted (per promoted_admissions.csv)")
    ap.add_argument("--dry-run", action="store_true", help="pass --dry-run to each fold-in")
    ap.add_argument("--allow-dev-db", action="store_true",
                    help="override the guard refusing to replay into the dev ledger-of-record DB")
    args = ap.parse_args()

    if not config.RUN_NAMESPACE:
        raise SystemExit(
            "REFUSING to run in the default namespace -- set LONGSUM_NAMESPACE (e.g. gen_prod) so "
            "the file artifacts stay isolated from the dev tree.")
    # The production replay WRITES to the DB (e2 / d3 / db_persist_*). Refuse the dev
    # ledger-of-record unless explicitly overridden, so the sim can never contaminate it.
    from dotenv import load_dotenv
    load_dotenv(config.PROJECT_ROOT / ".env")
    pgdb = os.getenv("PGDATABASE")
    if pgdb in DEV_DBS and not args.allow_dev_db:
        raise SystemExit(
            f"REFUSING: PGDATABASE={pgdb!r} is a ledger-of-record DB ({sorted(DEV_DBS)}). Point "
            f"PGDATABASE at a fresh DB (e.g. longitudinal_bhc_gen) for the production sim, or "
            f"pass --allow-dev-db.")
    config.check_paths()

    df = _holdout_admissions(args.subject)
    subjects = list(dict.fromkeys(df["subject_id"].astype(int).tolist()))   # unique, order-preserving
    if args.limit is not None:
        subjects = subjects[:args.limit]
        df = df[df["subject_id"].astype(int).isin(subjects)].reset_index(drop=True)

    already = holdout.promoted_hadms() if args.resume else set()

    if args.stop_after is not None:
        if args.stop_after < 1:
            raise SystemExit("--stop-after must be >= 1")
        # Oldest-first per patient, so head(N) keeps the EARLIEST N discharges -- the cap is
        # on the patient's folded total, not on this run, which keeps re-runs idempotent.
        before = len(df)
        df = df.groupby("subject_id", sort=False).head(args.stop_after).reset_index(drop=True)
        parked = before - len(df)

    print("\n" + "=" * 62)
    print(f"PRODUCTION REPLAY (namespace '{config.RUN_NAMESPACE}', DB '{pgdb}')"
          + ("  [DRY RUN]" if args.dry_run else ""))
    print(f"  subjects              : {len(subjects)}")
    print(f"  discharges to replay  : {len(df)}")
    if args.stop_after is not None:
        print(f"  stop-after            : {args.stop_after} per patient "
              f"({parked} discharge(s) left held out)")
    if args.resume:
        print(f"  already folded (skip) : {sum(int(h) in already for h in df['hadm_id'])}")
    print("=" * 62)

    ok = failed = skipped = 0
    failed_subjects: set[int] = set()
    t0 = time.time()
    for i, row in enumerate(df.itertuples(index=False), 1):
        sid, hadm, seq = int(row.subject_id), int(row.hadm_id), int(row.admit_seq)
        tag = f"[{i}/{len(df)}] subject {sid} hadm {hadm} (seq {seq})"
        if args.resume and hadm in already:
            print(f"{tag}: already folded -- skip")
            skipped += 1
            continue
        if sid in failed_subjects:
            # a patient's later discharge depends on the earlier one having folded in
            print(f"{tag}: SKIP (an earlier discharge for this patient failed)")
            skipped += 1
            continue
        print(f"\n{tag}: folding in ...")
        ts = time.time()
        rc = _foldin(sid, hadm, args.dry_run)
        if rc == 0:
            ok += 1
            print(f"{tag}: OK ({time.time() - ts:.0f}s)")
        else:
            failed += 1
            failed_subjects.add(sid)
            print(f"{tag}: FAILED (rc={rc}) -- skipping this patient's later discharges")

    print("\n" + "=" * 62)
    print("PRODUCTION REPLAY COMPLETE" + ("  [DRY RUN]" if args.dry_run else ""))
    print(f"  folded in : {ok}")
    print(f"  failed    : {failed}  (subjects: {sorted(failed_subjects) or '-'})")
    print(f"  skipped   : {skipped}")
    print(f"  elapsed   : {(time.time() - t0) / 60:.1f} min")
    if args.stop_after is not None and parked and not args.dry_run:
        print(f"\nCHECKPOINT: {parked} discharge(s) remain held out. Each affected patient now has\n"
              "exactly one unfolded discharge, so it is their sole INCOMING admission. To make it\n"
              "visible to f6 and the review UI / export_review_html:\n"
              "    python e1_incoming_summary.py --fresh\n"
              "    python database/d3_load_tiers.py\n"
              "Then continue with:  replay_production.py --resume  (drop --stop-after)")
    if failed:
        print("\nFold-ins are idempotent. Fix the cause, then re-run with --resume to continue "
              "from where each patient stopped (already-folded discharges are skipped).")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
