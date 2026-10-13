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
run_ingest.py
=============
Production EHR ingest - the daily poll that feeds new signed discharge summaries into
clin_longsum without changing any existing stage.

    EHR (signed discharges) --fetch--> admissions (Postgres, is_holdout=TRUE, no embedding)
        --project--> cohort_admissions.csv (+ holdout / patients files)
        --> first-ever admission : foldin_patient.py (auto fold-in, no review)
        --> otherwise            : oldest held-out surfaced for review (e1 + d3), newer queued
    physician approves in the review UI --> existing fold-in (b1 ... e2 embed ... c5)

Steps, in order:
  0. guards      HOLDOUT_LAST_ADMISSION on; pandas parses ISO safely; DB is not
                 in LONGSUM_PROTECTED_DBS (unless LONGSUM_ALLOW_PROTECTED_DB=1); tables exist
  1. heal        project any previously ingested row missing from the CSV (a run that
                 committed to the DB and died before projecting)
  2. fetch       records signed after (last ok watermark - overlap); rejects logged
  3. plan+write  per patient (ingest_derive): insert | duplicate | amendment |
                 out_of_order | after_death. Inserts -> admissions/patients; the rest ->
                 ingest_amendments (MANUAL REVIEW). One DB transaction.
  4. project     DB -> cohort files (append-only, byte-preserving)
  5. auto fold   patients whose ONLY admissions are ingested and held out: fold the
                 oldest in via foldin_patient.py (it takes the fold-in lock itself)
  6. surface     ingest_surface: oldest held-out reviewable, newer hidden, review reset
Steps 1-4 hold the fold-in advisory lock, so no fold-in reads a half-updated cohort.

Amendments / anomalies are NOT shown in the review UI. Review them in Postgres:
    SELECT * FROM ingest_amendments WHERE review_status = 'pending_review' ORDER BY detected_at;

    cd C:~/clin_longsum/src
    python ingest/run_ingest.py --dry-run                  # plan only, no writes, no LLM
    python ingest/run_ingest.py                            # daily run
    python ingest/run_ingest.py --since 2026-09-01T00:00   # override the watermark
    python ingest/run_ingest.py --skip-surface             # ingest + project only
    python ingest/run_ingest.py --overlap-days 7           # re-read N days before the watermark (default 3)
    python ingest/run_ingest.py --source csv_drop          # EHR adapter (default LONGSUM_EHR_SOURCE or csv_drop)
    python ingest/run_ingest.py --skip-foldin              # skip step 5: first-ever admissions are not auto folded in

Environment: PGDATABASE (production DB), LONGSUM_PROTECTED_DBS (databases to refuse;
override with LONGSUM_ALLOW_PROTECTED_DB=1), LONGSUM_EHR_SOURCE (default csv_drop),
LONGSUM_EHR_DROP_DIR (default <data>/ehr_drop). Run under the project env
(ollama_HF_py313); pandas 3.x is refused (see ingest_project.assert_iso_parse_safe).
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  (puts src/ on sys.path)

import argparse
import subprocess
import sys
import traceback
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

import config
import ingest_db
import ingest_derive
import ingest_project
import ingest_source
import ingest_surface

SRC = Path(__file__).resolve().parents[1]
FOLDIN = SRC / "foldin_patient.py"
ANOMALY_KINDS = {"amendment", "out_of_order", "after_death"}


def _since(args, conn) -> datetime | None:
    if args.since:
        return pd.to_datetime(args.since, format="ISO8601").to_pydatetime()
    wm = ingest_db.last_watermark(conn)
    return None if wm is None else wm - timedelta(days=args.overlap_days)


def _auto_foldin_candidates(conn) -> list[tuple[int, int]]:
    """(subject, hadm) to auto fold-in:
      - patients with NO established admission whose held-out admissions were all
        inserted by the ingest (never touches a bootstrap cohort patient): the oldest;
      - RETRIES: admissions whose latest auto fold-in attempt failed. fold-in promotes
        before b2..d3 run, so a failed attempt leaves the patient 'established' and the
        first rule alone would never pick them up again. Fold-in stages are idempotent.
    A permanently failing admission is retried on every run (logged each time)."""
    import cohort
    cur = conn.cursor()
    cur.execute("SELECT hadm_id FROM ingest_log WHERE action = 'inserted';")
    ingested = {int(r[0]) for r in cur.fetchall()}
    cur.execute("""
        SELECT DISTINCT ON (hadm_id) subject_id, hadm_id, action FROM ingest_log
        WHERE action IN ('auto_folded', 'auto_foldin_failed')
        ORDER BY hadm_id, id DESC;""")
    failed = sorted((int(s), int(h)) for s, h, a in cur.fetchall() if a == "auto_foldin_failed")
    cur.close()
    conn.rollback()
    df = cohort.load()
    out, seen = [], set()
    for sid, hadm in failed:                    # retry first: they are the older admissions
        if sid not in seen:
            out.append((sid, hadm))
            seen.add(sid)
    for sid, st in sorted(ingest_surface.queue_state(df).items()):
        hadms = [st["oldest"]["hadm_id"], *st["queued"]]
        if sid not in seen and st["n_established"] == 0 and all(h in ingested for h in hadms):
            out.append((sid, st["oldest"]["hadm_id"]))
            seen.add(sid)
    return out


def _foldin(sid: int, hadm: int) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(FOLDIN), "--subject", str(sid), "--hadm", str(hadm)],
                       cwd=str(SRC), capture_output=True, text=True)
    tail = "\n".join((r.stdout or "").strip().splitlines()[-3:])
    if r.returncode != 0:
        tail += "\n" + (r.stderr or "")[-800:]
    return r.returncode, tail


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily EHR -> clin_longsum ingest.")
    ap.add_argument("--dry-run", action="store_true",
                    help="plan only: no DB/CSV writes, no fold-in, no LLM")
    ap.add_argument("--since", default=None, help="ISO 8601 signed_at lower bound (overrides watermark)")
    ap.add_argument("--overlap-days", type=int, default=3,
                    help="re-read this many days before the last watermark (default 3)")
    ap.add_argument("--source", default=None, help="EHR adapter (default LONGSUM_EHR_SOURCE or csv_drop)")
    ap.add_argument("--skip-foldin", action="store_true", help="do not auto fold-in first admissions")
    ap.add_argument("--skip-surface", action="store_true", help="do not run the surface step")
    args = ap.parse_args()

    # 0. guards
    config.check_paths()
    if not config.HOLDOUT_LAST_ADMISSION:
        raise SystemExit("config.HOLDOUT_LAST_ADMISSION is False -- the ingest relies on the "
                         "holdout mechanism to keep new admissions out of the established record.")
    ingest_project.assert_iso_parse_safe()
    conn, db = ingest_db.connect()
    ingest_db.check_schema(conn)
    source = ingest_source.get_source(args.source)
    since = _since(args, conn)

    import foldin_patient                    # advisory-lock helper shared with fold-in

    # 2. fetch (before the lock: may be slow, touches no cohort file)
    recs, rejects = source.fetch(since)
    print(f"\n[ingest] DB {db} | source {source.name} | since {since} | "
          f"fetched {len(recs)} | rejected {len(rejects)}" + ("  [DRY RUN]" if args.dry_run else ""))

    run_id = None if args.dry_run else ingest_db.start_run(conn, source.name, since)
    counts: Counter = Counter(fetched=len(recs), rejected=len(rejects))
    status, error = "ok", None
    try:
        with foldin_patient._foldin_advisory_lock(args.dry_run):
            # 1. heal
            healed = ingest_project.project(conn, dry_run=args.dry_run)
            if healed["appended_admissions"]:
                print(f"[ingest] heal: {healed['appended_admissions']} earlier ingested row(s) "
                      f"{'would be ' if args.dry_run else ''}projected")
            if healed["mismatches"]:
                raise RuntimeError(f"cohort CSV and DB disagree before ingest: {healed['mismatches']}")

            # 3. plan + write
            by_sid: dict[int, list] = {}
            for rec in recs:
                by_sid.setdefault(rec.subject_id, []).append(rec)
            existing = ingest_db.existing_by_subject(conn, sorted(by_sid))
            bad = ingest_project.inconsistent_subjects(conn, sorted(existing))
            cur = None if args.dry_run else conn.cursor()
            for sid in sorted(by_sid):
                if sid in bad:
                    counts["inconsistent"] += len(by_sid[sid])
                    print(f"[ingest] subject {sid}: DB/CSV admission sets differ {bad[sid]} -- skipped")
                    if cur:
                        for rec in by_sid[sid]:
                            ingest_db.log_action(cur, run_id, "inconsistent", rec=rec, detail=bad[sid])
                    continue
                for act in ingest_derive.plan_subject(by_sid[sid], existing.get(sid, [])):
                    counts[act.kind] += 1
                    if args.dry_run:
                        extra = f" seq {act.row['admit_seq']}" if act.row else ""
                        extra += " (first admission -> auto fold-in)" if act.first_admission else ""
                        print(f"   {act.kind:<12} subject {sid} hadm {act.rec.hadm_id}{extra}")
                        continue
                    if act.kind == "insert":
                        if ingest_db.insert_admission(cur, act.row):
                            counts["inserted"] += 1
                            ingest_db.log_action(cur, run_id, "inserted", rec=act.rec, detail={
                                **act.detail, "first_admission": act.first_admission})
                        else:
                            counts["insert_raced"] += 1
                    elif act.kind == "duplicate":
                        ingest_db.log_action(cur, run_id, "duplicate", rec=act.rec)
                    else:
                        new = ingest_db.log_amendment(cur, run_id, act)
                        counts[f"{act.kind}_new" if new else f"{act.kind}_seen"] += 1
                        ingest_db.log_action(cur, run_id, act.kind, rec=act.rec, detail=act.detail)
            if cur:
                for rj in rejects:
                    ingest_db.log_action(cur, run_id, "rejected", rj.get("subject_id"),
                                         rj.get("hadm_id"), detail=rj)
                conn.commit()
                cur.close()

            # 4. project
            proj = ingest_project.project(conn, dry_run=args.dry_run)
            counts.update({k: v for k, v in proj.items() if isinstance(v, int)})
            if proj["mismatches"]:
                raise RuntimeError(f"projection left DB and CSV disagreeing: {proj['mismatches']}")
    except Exception as exc:
        conn.rollback()
        status, error = "error", f"{type(exc).__name__}: {exc}"
        traceback.print_exc()

    # 5. auto fold-in of first-ever admissions (fold-in takes the lock itself)
    if status == "ok" and not args.dry_run and not args.skip_foldin:
        cur = conn.cursor()
        for sid, hadm in _auto_foldin_candidates(conn):
            print(f"[ingest] auto fold-in subject {sid} hadm {hadm} (first admission) ...")
            rc, tail = _foldin(sid, hadm)
            ok = rc == 0
            counts["auto_folded" if ok else "auto_foldin_failed"] += 1
            ingest_db.log_action(cur, run_id, "auto_folded" if ok else "auto_foldin_failed",
                                 sid, hadm, detail={"rc": rc, "tail": tail})
            conn.commit()
            print(f"[ingest]   {'OK' if ok else 'FAILED'}: {tail}")
            if not ok:
                status = "partial"
        cur.close()

    # 6. surface
    if status != "error" and not args.skip_surface:
        try:
            s = ingest_surface.surface(conn, run_id, dry_run=args.dry_run)
            counts.update(s)
            if s.get("d3_failed"):
                status = "partial"
        except Exception as exc:
            conn.rollback()
            status, error = "partial", f"surface: {type(exc).__name__}: {exc}"
            traceback.print_exc()

    signed = [r.signed_at for r in recs]
    prev_wm = None if args.dry_run else ingest_db.last_watermark(conn)
    wm = max([d for d in [prev_wm, *signed] if d is not None], default=None)
    if not args.dry_run:
        ingest_db.finish_run(conn, run_id, status, dict(counts), len(recs), wm, error)
    conn.close()

    print("\n" + "=" * 62)
    print(f"INGEST {status.upper()}" + ("  [DRY RUN]" if args.dry_run else f"  (run_id {run_id})"))
    for k in sorted(counts):
        if counts[k]:
            print(f"  {k:<22}: {counts[k]}")
    if args.dry_run:
        n_anom = sum(counts.get(k, 0) for k in ANOMALY_KINDS)
        label = "record(s) would go to MANUAL REVIEW (if not already recorded)"
    else:
        n_anom = sum(v for k, v in counts.items() if k.split('_')[0] in {"amendment", "out", "after"}
                     and k.endswith("_new"))
        label = "new record(s) for MANUAL REVIEW"
    if n_anom or any(counts.get(k) for k in ANOMALY_KINDS):
        print(f"\n  {n_anom} {label} in {db}.ingest_amendments:\n"
              f"    SELECT * FROM ingest_amendments WHERE review_status = 'pending_review' "
              f"ORDER BY detected_at;")
    if error:
        print(f"\n  error: {error}")
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
