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
c2_retention_validator.py
=========================
Retention completeness (the critical-OMISSION guard; opposite of c3
faithfulness). DETERMINISTIC: no LLM. Retention is never left to a
generative prompt - the ledger is the lossless layer, so this is an independent
check that the guarantee held.

For each compacted patient, every PINNED fact (is_ledger_member, after the same
surveillance + disease-course promotion b2 applies) from the aged-out (covered)
admissions must be REPRESENTED somewhere in the served ledger. Representation
homes, in order of fidelity:
    node      nodes / flagged / amended   - full retention (exact fact_id, or
              same-class cluster by b2's own rule)
    excluded  considered_excluded         - ruled-out significant negatives
    episode   EpisodeRollup.parent_condition - a pinned recurrent_episode retained
              LOSSILY as a count. Not a loss, but a pinned fact landing ONLY here is
              a mis-tag candidate (a sentinel-grade event filed as a recurrence),
              surfaced for review (issue=PINNED_IN_ROLLUP), not corrected.
    (none)    genuine loss - a pinned fact in a compacted span absent from the
              ledger entirely. A b2/extraction bug; surfaced (issue=MISSING).

recall = represented / total pinned. complete = no genuine losses (rollup-only
still counts as represented).

Input:  data/interim/compaction_results.jsonl, data/output/retention_ledger.jsonl,
        data/interim/retention_facts.jsonl (via c1 loader)
Output: data/interim/retention_completeness.jsonl  (subject_id is a TOP-LEVEL field;
        older rows carry it only inside `audit` - readers should fall back)
        data/output/retention_review.csv   (MISSING + PINNED_IN_ROLLUP rows, if any)
        appends a deterministic AuditRecord per patient to generation_audit.jsonl

    cd C:~/Summarization/clin_longsum/src
    python c2_retention_validator.py
    python c2_retention_validator.py --preview 90000029 # print only, no write
    python c2_retention_validator.py --subject 90000029 
    python c2_retention_validator.py --fresh           # overwrite outputs
"""

from __future__ import annotations

import argparse
import csv
import json
from typing import Optional

import config
import io_utils
import retention_taxonomy as tax
import b2_build_ledger as b2
import c1_compactor as c1
from roles.contracts import AuditRecord, RetentionCompletenessOutput, RetentionFact

COMPACTION_JSONL = config.COMPACTION_JSONL
LEDGER_JSONL = config.OUTPUT_DIR / "retention_ledger.jsonl"
OUT_JSONL = config.INTERIM_DIR / "retention_completeness.jsonl"
CSV_OUT = config.OUTPUT_DIR / "retention_review.csv"
AUDIT_JSONL = config.AUDIT_JSONL


def _ledger_index(led: dict):
    """Representation homes: (member fact_ids, {class:[token-sets]} for nodes/
    flagged/amended, [excluded token-sets], [episode parent_condition token-sets])."""
    entries = led.get("nodes", []) + led.get("flagged", []) + led.get("amended", [])
    fact_ids: set[str] = set()
    by_class: dict[str, list[set]] = {}
    for e in entries:
        fact_ids.update(e.get("fact_ids", []) or [])
        by_class.setdefault(e["retention_class"], []).append(b2._content_tokens(e["text"]))
    excluded = [b2._content_tokens(t) for t in led.get("considered_excluded", [])]
    episodes = [b2._content_tokens(ep.get("parent_condition", "")) for ep in led.get("episodes", [])]
    return fact_ids, by_class, excluded, episodes


def _clusters(a: set, b: set) -> bool:
    return len(a & b) >= b2._MIN_SHARED and b2._overlap(a, b) >= b2.CLUSTER_THRESH


def _where_represented(f: RetentionFact, fact_ids: set[str], by_class: dict[str, list[set]],
                       excluded: list[set], episodes: list[set]) -> Optional[str]:
    if (f.fact_id or f.assign_fact_id()) in fact_ids:
        return "node"
    ft = b2._content_tokens(f.text)
    if any(_clusters(ft, ct) for ct in by_class.get(f.retention_class, [])):
        return "node"
    if any(_clusters(ft, ct) for ct in excluded):
        return "excluded"
    if any(_clusters(ft, ct) for ct in episodes):
        return "episode"
    return None


def _audit(pid: int, covered: list[int], score: float, n_total: int,
           n_missing: int, n_rollup: int, complete: bool) -> AuditRecord:
    # Deterministic rule-based check - recorded as such, pinned to the taxonomy
    # version that governs is_ledger_member + the promotion nets.
    return AuditRecord(
        role="retention_validator", provider="deterministic", model_id="(rule-based)",
        model_version=tax.TAXONOMY_VERSION, prompt_version="retention_completeness.v1",
        taxonomy_version=tax.TAXONOMY_VERSION, subject_id=pid, input_hadm_ids=covered,
        scores={"recall": round(score, 4), "n_pinned": n_total,
                "n_missing": n_missing, "n_pinned_in_rollup": n_rollup},
        status="ok" if complete else "flagged",
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", type=int, default=None) # print only, no write
    ap.add_argument("--subject", type=int, default=None,
                    help="fold-in write-mode: validate one subject and MERGE its completeness "
                         "+ review rows in place (preserving other subjects). Unlike --preview.")
    ap.add_argument("--fresh", action="store_true", help="overwrite outputs")
    args = ap.parse_args()
    config.check_paths()

    comp = c1._read_jsonl(COMPACTION_JSONL)
    if not comp:
        raise SystemExit(f"{COMPACTION_JSONL} not found/empty. Run c1_compactor.py first.")
    # Validate the LOSSLESS ledger. b4 consolidation (esp. T5 prune) is a serving-layer
    # reduction; recall must be checked against b2's raw ledger. If the served ledger is
    # marked consolidated, read the raw backup b4 preserved instead.
    ledger_src = LEDGER_JSONL
    _primary = c1._read_jsonl(LEDGER_JSONL)
    if _primary and _primary[0].get("consolidated"):
        _raw = config.OUTPUT_DIR / "retention_ledger.raw.jsonl"
        if _raw.exists():
            ledger_src = _raw
            print(f"served ledger is consolidated; validating raw ledger -> {_raw.name}")
        else:
            print("WARN: ledger marked consolidated but no raw backup; recall may under-report "
                  "by the count of pruned T5 nodes.")
    ledgers = {int(l["subject_id"]): l for l in c1._read_jsonl(ledger_src)}
    facts_by_hadm = c1._facts_by_hadm()

    target = args.subject if args.subject is not None else args.preview
    if target is not None:
        comp = [c for c in comp if int(c["subject_id"]) == target]

    results, audits, rows = [], [], []
    tot_pinned, tot_repr, n_incomplete, tot_rollup = 0, 0, 0, 0
    for c in comp:
        pid = int(c["subject_id"])
        covered = c.get("covered_hadm_ids", [])
        led = ledgers.get(pid)
        if led is None:
            print(f"no ledger for subject_id={pid} -- skipping")
            continue

        seen, source = set(), []
        for h in covered:
            for f in facts_by_hadm.get(h, []):
                if not f.in_ledger:
                    continue
                key = (f.retention_class, f.text.strip().lower())
                if key not in seen:
                    seen.add(key)
                    source.append(f)

        fact_ids, by_class, excluded, episodes = _ledger_index(led)
        missing, rollup_only = [], []
        for f in source:
            w = _where_represented(f, fact_ids, by_class, excluded, episodes)
            if w is None:
                missing.append(f)
            elif w == "episode":
                rollup_only.append(f)

        n_total = len(source)
        score = (n_total - len(missing)) / n_total if n_total else 1.0
        complete = not missing
        tot_pinned += n_total
        tot_repr += n_total - len(missing)
        tot_rollup += len(rollup_only)
        if not complete:
            n_incomplete += 1

        for f in missing:
            rows.append({"issue": "MISSING", "subject_id": pid, "salience_tier": f.salience_tier,
                         "retention_class": f.retention_class, "certainty": f.certainty,
                         "text": f.text, "source_hadm_id": f.source_hadm_id})
        for f in rollup_only:
            rows.append({"issue": "PINNED_IN_ROLLUP", "subject_id": pid, "salience_tier": f.salience_tier,
                         "retention_class": f.retention_class, "certainty": f.certainty,
                         "text": f.text, "source_hadm_id": f.source_hadm_id})

        audit = _audit(pid, covered, score, n_total, len(missing), len(rollup_only), complete)
        audits.append(audit)
        results.append(RetentionCompletenessOutput(
            subject_id=pid, complete=complete, score=round(score, 4),
            missing_facts=missing, audit=audit))
        if not complete:
            print(f"subject={pid}  recall={score:.3f}  MISSING {len(missing)}/{n_total}:")
            for f in missing:
                print(f"    [{f.salience_tier} {f.retention_class}] {f.text}  (hadm {f.source_hadm_id})")

    if args.preview is not None and args.subject is None and results:
        print(results[0].model_dump_json(indent=2))
        return

    if args.subject is not None:
        # Fold-in write-mode: replace THIS subject's completeness line + review rows in
        # place, preserving every other subject (io_utils.merge_jsonl). Key = the top-level
        # subject_id, falling back to audit.subject_id for rows written before the top-level field existed.
        io_utils.merge_jsonl(
            OUT_JSONL, [json.loads(r.model_dump_json()) for r in results],
            lambda r: int(r.get("subject_id")
                          if r.get("subject_id") is not None
                          else (r.get("audit") or {}).get("subject_id", -1)))
        with AUDIT_JSONL.open("a", encoding="utf-8") as fh:
            for a in audits:
                fh.write(a.model_dump_json() + "\n")
        cols = ["issue", "subject_id", "salience_tier", "retention_class", "certainty",
                "text", "source_hadm_id"]
        keep = []
        if CSV_OUT.exists():
            with CSV_OUT.open(encoding="utf-8", newline="") as fh:
                keep = [r for r in csv.DictReader(fh)
                        if str(r.get("subject_id")) != str(args.subject)]
        with CSV_OUT.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(keep)
            w.writerows(rows)
        recall = tot_repr / tot_pinned if tot_pinned else 1.0
        print(f"C2 (subject {args.subject}): recall={recall:.4f} pinned={tot_pinned} "
              f"missing={tot_pinned - tot_repr} rollup={tot_rollup} rows={len(rows)}")
        return

    mode_w = "w" if args.fresh else "a"
    with OUT_JSONL.open(mode_w, encoding="utf-8") as fh:
        for r in results:
            fh.write(r.model_dump_json() + "\n")
    with AUDIT_JSONL.open("a", encoding="utf-8") as fh:
        for a in audits:
            fh.write(a.model_dump_json() + "\n")
    if rows:
        cols = ["issue", "subject_id", "salience_tier", "retention_class", "certainty",
                "text", "source_hadm_id"]
        with CSV_OUT.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)

    recall = tot_repr / tot_pinned if tot_pinned else 1.0
    print("\n" + "=" * 55)
    print("C2 RETENTION COMPLETENESS (deterministic)")
    print(f"  patients checked     : {len(results)}")
    print(f"  distinct pinned facts in compacted spans : {tot_pinned}")
    print(f"  represented          : {tot_repr}")
    print(f"  recall               : {recall:.4f}")
    print(f"  genuine losses       : {tot_pinned - tot_repr}  (patients: {n_incomplete})")
    print(f"  pinned-in-rollup     : {tot_rollup}  (mis-tag candidates -- review, not loss)")
    if rows:
        print(f"  review CSV -> {CSV_OUT}")
        if tot_pinned - tot_repr:
            print("    MISSING rows are genuine losses -- investigate B2/extraction.")
        if tot_rollup:
            print("    PINNED_IN_ROLLUP rows = sentinel-grade events filed as recurrences;")
            print("    extend the disease-course/sentinel net to promote them to nodes.")
    else:
        print("  clean: every pinned fact represented in nodes/flagged/amended/excluded.")
    print(f"  results -> {OUT_JSONL}")
    print('\nNext: db_persist_retention.py if c3, c4 has been run; else run c3, c4')


if __name__ == "__main__":
    main()
