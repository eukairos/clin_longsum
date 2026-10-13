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
b1_rerun_hadms.py
=================
Re-runs the retention_extractor on a SPECIFIC set of hadm_ids and splice the new
facts back into retention_facts.jsonl IN PLACE. For recovering the handful of
admissions that came back with zero facts (Step-A JSON parse failure) without
re-extracting whole patients (b1 --subject).

Faithful to b1's patient-sequential feed-forward: known_conditions for each
target are reconstructed from that patient's already-extracted EARLIER
admissions (admit_seq <) as currently stored, so the re-run sees the same prior
context the original pass had. Multiple targets in one patient are processed in
admit_seq order, and each target's known list reflects any earlier target
re-run in the same invocation.

Reuses b1_retention_extractor._extract_admission verbatim (same prompt, taxonomy, enum schema, 
Step-A retry guard).

Idempotent: re-running on the same hadm_ids replaces their records again.

Run (from clin_longsum/src/):
    python b1_rerun_hadms.py 80000039
    python b1_rerun_hadms.py 80000039 80000072
    python b1_rerun_hadms.py --from-audit          # every status=error hadm_id
    python b1_rerun_hadms.py 80000039 --dry-run     # plan only; no model, no write
"""

from __future__ import annotations

import argparse
import json
import logging

import pandas as pd

import config
import retention_taxonomy as tax
import b1_retention_extractor as b1
from roles.contracts import RetentionExtractorInput

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("b1_rerun_hadms")

FACTS = config.RETENTION_FACTS_JSONL
AUDIT = config.AUDIT_JSONL
COHORT = b1.COHORT_CSV


def _seed_known(fact_dicts: list[dict], known: list[str], seen: set[str]) -> None:
    """Mirror b1._update_known on stored fact DICTS (in_ledger recomputed via tax)."""
    for f in fact_dicts:
        in_ledger = tax.is_ledger_member(
            f["salience_tier"], f["certainty"], f["lifecycle"],
            f.get("negative_kind"), f.get("retention_class"))
        if in_ledger and f["salience_tier"] in ("T1", "T2", "T3"):
            t = f["text"]
            if t.lower() not in seen:
                known.append(t)
                seen.add(t.lower())


def _error_hadms() -> list[int]:
    out: list[int] = []
    for r in b1._read_jsonl_safe(AUDIT):
        if r.get("status") == "error":
            out.extend(int(h) for h in r.get("input_hadm_ids", []))
    return sorted(set(out))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("hadm_ids", type=int, nargs="*", help="hadm_ids to re-extract")
    ap.add_argument("--from-audit", action="store_true",
                    help="re-extract every hadm_id carrying a status=error audit record")
    ap.add_argument("--dry-run", action="store_true", help="plan only; no model, no write")
    args = ap.parse_args()

    targets = list(args.hadm_ids)
    if args.from_audit:
        targets += _error_hadms()
    targets = sorted(set(targets))
    if not targets:
        raise SystemExit("no hadm_ids given (pass ids or --from-audit).")
    if not FACTS.exists():
        raise SystemExit(f"{FACTS} not found.")
    if not COHORT.exists():
        raise SystemExit(f"{COHORT} not found.")

    df = pd.read_csv(COHORT).sort_values(["subject_id", "admit_seq"]).reset_index(drop=True)
    cohort_by_hadm = {int(r["hadm_id"]): r for _, r in df.iterrows()}

    records = b1._read_jsonl_safe(FACTS)            # list of RetentionExtractorOutput dumps
    pos_by_hadm = {int(r["hadm_id"]): i for i, r in enumerate(records)}

    # Resolve + order by (subject_id, admit_seq).
    plan = []
    for h in targets:
        if h not in cohort_by_hadm:
            logger.warning("hadm=%s not in cohort_admissions.csv -- skipping", h)
            continue
        row = cohort_by_hadm[h]
        plan.append((int(row["subject_id"]), int(row["admit_seq"]), h))
    plan.sort()
    if not plan:
        raise SystemExit("no resolvable targets.")

    logger.info("targets: %d resolved", len(plan))
    for sid, seq, h in plan:
        n_exist = len(records[pos_by_hadm[h]].get("facts", [])) if h in pos_by_hadm else "NEW"
        logger.info("  subject=%s admit_seq=%s hadm=%s (existing facts=%s)", sid, seq, h, n_exist)
    if args.dry_run:
        logger.info("dry-run: no model call, no write.")
        return

    new_audits: list[str] = []
    try:
        for sid, seq, h in plan:
            row = cohort_by_hadm[h]
            # Rebuild known_conditions from earlier admissions as currently stored.
            known: list[str] = []
            seen: set[str] = set()
            earlier = sorted(
                (r for r in records
                 if int(r["subject_id"]) == sid and int(r["admit_seq"]) < seq),
                key=lambda r: int(r["admit_seq"]))
            for r in earlier:
                _seed_known(r.get("facts", []), known, seen)

            inp = RetentionExtractorInput(
                subject_id=sid, hadm_id=h, admit_seq=seq,
                bhc_text=str(row["bhc_text"]) if pd.notna(row.get("bhc_text")) else "",
                admit_date=str(row.get("admittime")) if pd.notna(row.get("admittime")) else None,
                service=str(row.get("service")) if pd.notna(row.get("service")) else None,
                known_conditions=known,
            )
            try:
                out = b1._extract_admission(inp)
            except Exception as exc:
                logger.error("re-extract hadm=%s FAILED again: %s -- leaving existing record", h, exc)
                continue

            rec = json.loads(out.model_dump_json())
            if h in pos_by_hadm:
                records[pos_by_hadm[h]] = rec
            else:
                pos_by_hadm[h] = len(records)
                records.append(rec)
            new_audits.append(out.audit.model_dump_json())
            logger.info("re-extracted hadm=%s -> %d facts (%d pinned)",
                        h, len(out.facts), sum(f.in_ledger for f in out.facts))
    finally:
        b1.ollama_unload()

    if not new_audits:
        logger.info("nothing re-extracted successfully; facts file untouched.")
        return

    # Rewrite facts (temp + replace); append audits.
    tmp = FACTS.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    tmp.replace(FACTS)
    with AUDIT.open("a", encoding="utf-8") as fh:
        for line in new_audits:
            fh.write(line + "\n")
    logger.info("rewrote %s (%d records); appended %d audit rows.",
                FACTS.name, len(records), len(new_audits))
    logger.info("Next: re-run b2_build_ledger.py, then b3_loss_audit.py.")


if __name__ == "__main__":
    main()
