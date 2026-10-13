# clin_longsum - tiered longitudinal clinical summarization
# Copyright (C) 2026 Eukairos
# SPDX-License-Identifier: AGPL-3.0-only
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of version 3 of the GNU Affero General Public License as published
# by the Free Software Foundation. This program is distributed WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE. See the GNU Affero General Public License for details.

"""b4_consolidate_ledger.py
========================
LLM-assisted, tier-aware consolidation of the retention ledger.
Runs AFTER b2 (build) and b3 (loss audit), BEFORE c1/d_coordinator/d3.

This is the ONE isolated LLM step over the ledger.
It rewrites only the confirmed 'nodes' bucket per patient (T3 roll-up, T4 metric,
T5 prune via ledger_consolidate); flagged/episodes/amended/considered_excluded are
left untouched.

Idempotency / reversibility:
  - b2's raw ledger is backed up to retention_ledger.raw.jsonl on first run (and
    refreshed whenever a fresh, unmarked b2 ledger is detected).
  - b4 always consolidates FROM the raw backup, so reruns are reproducible and never
    double-consolidate. Output records carry "consolidated": true.

Input/Output: data/output/retention_ledger.jsonl (overwritten with consolidated)
              data/output/retention_ledger.raw.jsonl (raw b2 backup)
              appends AuditRecords to generation_audit.jsonl

    cd C:~/Summarization/clin_longsum/src
    python b4_consolidate_ledger.py                        # whole cohort (writes consolidated ledger)
    python b4_consolidate_ledger.py --preview 90000043     # consolidate ONE subject, print only (no write)
    python b4_consolidate_ledger.py --subject 90000043     # re-consolidate ONE subject, MERGE (fold-in)
"""

from __future__ import annotations

import argparse
import json
import shutil

import pandas as pd

import config
import holdout
import io_utils
import ledger_consolidate as lc

LEDGER = config.OUTPUT_DIR / "retention_ledger.jsonl"
RAW = config.OUTPUT_DIR / "retention_ledger.raw.jsonl"
COHORT_CSV = config.INTERIM_DIR / "cohort_admissions.csv"
AUDIT_JSONL = config.AUDIT_JSONL


_read_jsonl = io_utils.read_jsonl        # shared tolerant reader


def _ref_dates() -> dict[int, str]:
    """Established last_date per patient (holdout removed) -- the 'now' for T5 prune."""
    if not COHORT_CSV.exists():
        return {}
    df = pd.read_csv(COHORT_CSV)
    df = holdout.established(df)
    df["_d"] = pd.to_datetime(df["dischtime"], format="mixed", dayfirst=True, errors="coerce")
    out = {}
    for pid, g in df.groupby("subject_id"):
        m = g["_d"].max()
        out[int(pid)] = None if pd.isna(m) else m.strftime("%Y-%m-%d")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", type=int, default=None, metavar="SUBJECT_ID",
                    help="consolidate ONE subject and PRINT before/after (no write); use --subject to persist")
    ap.add_argument("--subject", type=int, default=None,
                    help="consolidate ONE subject and MERGE into the ledger file (fold-in; writes)")
    args = ap.parse_args()
    config.check_paths()

    if not config.CONSOLIDATE_LEDGER:
        raise SystemExit("CONSOLIDATE_LEDGER is False -- nothing to do.")

    current = _read_jsonl(LEDGER)
    if not current:
        raise SystemExit(f"{LEDGER} not found/empty. Run b2_build_ledger.py first.")

    # Fold-in: consolidate ONE subject and merge into the served ledger. b2 --subject has
    # just rewritten this subject's RAW record into LEDGER, so take it from there, refresh
    # the raw backup for this subject, consolidate it, and merge the consolidated record back.
    if args.subject is not None:
        rec = next((r for r in current if int(r["subject_id"]) == args.subject), None)
        if rec is None:
            raise SystemExit(f"subject {args.subject} not in ledger.")
        raw_rec = dict(rec); raw_rec.pop("consolidated", None)
        if RAW.exists():          # guard: never CREATE a partial raw backup from one subject
            io_utils.merge_jsonl(RAW, [raw_rec], lambda r: int(r["subject_id"]))
        refs = _ref_dates()
        try:
            new_nodes, audits, report = lc.consolidate_nodes(
                args.subject, raw_rec.get("nodes", []), refs.get(args.subject))
        finally:
            lc.ollama_unload()
        out_rec = dict(raw_rec); out_rec["nodes"] = new_nodes; out_rec["consolidated"] = True
        io_utils.merge_jsonl(LEDGER, [out_rec], lambda r: int(r["subject_id"]))
        with AUDIT_JSONL.open("a", encoding="utf-8") as fh:
            for a in audits:
                fh.write(json.dumps(a) + "\n")
        print(f"B4 subject {args.subject}: T3 {report.get('t3','-')}  T4 {report.get('t4','-')}  "
              f"T5 {report.get('t5','-')}  -> merged into {LEDGER.name}")
        return

    # Pick the raw source. A fresh b2 ledger has no 'consolidated' marker -> (re)back it
    # up. An already-consolidated ledger -> reconsolidate from the raw backup.
    if not current[0].get("consolidated"):
        shutil.copy(LEDGER, RAW)
        source = current
        print(f"backed up raw b2 ledger -> {RAW.name}")
    elif RAW.exists():
        source = _read_jsonl(RAW)
        print(f"ledger already consolidated; reconsolidating from {RAW.name}")
    else:
        raise SystemExit("Ledger is marked consolidated but no raw backup exists. "
                         "Re-run b2_build_ledger.py to regenerate the raw ledger.")

    refs = _ref_dates()
    if args.preview is not None:
        source = [r for r in source if int(r["subject_id"]) == args.preview]
        if not source:
            raise SystemExit(f"subject {args.preview} not in ledger.")

    out_records, all_audits = [], []
    agg = {"t3": [0, 0], "t4": [0, 0], "t5_before": 0, "t5_kept": 0, "t5_pruned": 0}
    try:
        for rec in source:
            pid = int(rec["subject_id"])
            new_nodes, audits, report = lc.consolidate_nodes(
                pid, rec.get("nodes", []), refs.get(pid))
            rec = dict(rec)
            rec["nodes"] = new_nodes
            rec["consolidated"] = True
            out_records.append(rec)
            all_audits += audits
            if "t3" in report:
                agg["t3"][0] += report["t3"][0]; agg["t3"][1] += report["t3"][1]
            if "t4" in report:
                agg["t4"][0] += report["t4"][0]; agg["t4"][1] += report["t4"][1]
            if "t5" in report:
                agg["t5_before"] += report["t5"]["before"]
                agg["t5_kept"] += report["t5"]["kept"]
                agg["t5_pruned"] += report["t5"]["pruned"]
            if args.preview is None:
                print(f"subject {pid}: T3 {report.get('t3','-')}  T4 {report.get('t4','-')}  "
                      f"T5 {report.get('t5','-')}")
    finally:
        lc.ollama_unload()

    if args.preview is not None:
        print(json.dumps(out_records[0], indent=2))
        print("\n(--preview: printed only, ledger NOT written)")
        return

    LEDGER.write_text("".join(json.dumps(r) + "\n" for r in out_records), encoding="utf-8")
    with AUDIT_JSONL.open("a", encoding="utf-8") as fh:
        for a in all_audits:
            fh.write(json.dumps(a) + "\n")

    print("\n" + "=" * 55)
    print(f"B4 LEDGER CONSOLIDATION  ({len(out_records)} patients)")
    print(f"  T3 chronic nodes   : {agg['t3'][0]} -> {agg['t3'][1]}")
    print(f"  T4 recurrent nodes : {agg['t4'][0]} -> {agg['t4'][1]}")
    print(f"  T5 transient       : {agg['t5_before']} -> keep {agg['t5_kept']} / "
          f"prune {agg['t5_pruned']} (>{config.CONSOLIDATE_T5_PRUNE_DAYS}d)")
    print(f"  raw backup         : {RAW}")
    print(f"  saved              : {LEDGER}")
    print("\nNEXT: c1 / d_coordinator / d3 now read the consolidated ledger.")


if __name__ == "__main__":
    main()
