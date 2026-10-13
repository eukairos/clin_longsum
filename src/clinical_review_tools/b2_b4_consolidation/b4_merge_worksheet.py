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
b4_merge_worksheet.py
=====================
Clinician adjudication worksheet for b4 LEDGER CONSOLIDATION (the LLM T3/T4 pass).

WHY THIS EXISTS
---------------
Consolidation has three quality axes. Two already have metrics:

  recall     -- was a salient fact lost?            f1_salience_recall.py (SWR)
  precision  -- did the merged line INVENT anything? f9_merge_faithfulness.py
  GROUPING   -- were two DISTINCT conditions merged into one line?   <- no metric

The third axis is unmeasured (EVALUATION.md section 11) because no automatic
signal separates a good merge from a bad one: on the dev cohort every b4 merge
is single-class, single-tier, single-lifecycle and single-certainty, so metadata
cannot rank them. It needs per-merge clinician verdicts. This script builds the
sheet those verdicts go on.

READ-ONLY over the ledger artifacts. It writes ONE csv + ONE json under
OUTPUT_DIR/review_tools/merge_grouping/ and touches no pipeline artifact.

MEMBER RECOVERY (no LLM re-run, identical to f9_merge_faithfulness._members_of)
------------------------------------------------------------------------------
b4's _merge_node unions its members' fact_ids into the merged node, and b4 keeps
the pre-consolidation ledger at retention_ledger.raw.jsonl. So a consolidated
node C's members are exactly the RAW nodes whose fact_ids are a subset of C's.
Only nodes with >= 2 members are real merges; singletons are pass-throughs.

The script asserts the recovery is exact - every consolidated node's fact_ids
fully covered by its recovered members, no orphan ids, no zero-member nodes -
and reports any shortfall in the summary as `recovery_incomplete`. A shortfall
means the sheet is missing merges, so check it before adjudicating.

OUTPUT (long format: one row per MEMBER, merge-level columns repeated, so a
reviewer ticks each member against the consolidated line)

  merge_grouping_adjudication.csv
    merge_id subject_id tier n_members n_distinct_member_classes flags
    consolidated_class consolidated_lifecycle consolidated_text
    member_idx member_class member_tier member_lifecycle member_certainty
    member_n_mentions member_n_admissions member_text
    verdict_same_condition reviewer_note        <- blank, for the reviewer

  merge_grouping_summary.json   counts, size distribution, flag tally

Rows sort suspicious-first (multi_lifecycle, then multi_class, then multi_tier,
then largest merges). 

FLAGS  (each is a REVIEW HINT, not a defect)
  multi_class      members disagree on retention_class
  multi_lifecycle  members disagree on current_lifecycle -- b4 guarantees it never
                   merges across current_lifecycle, so this firing means that
                   guardrail did not hold. Investigate before adjudicating.
  multi_tier       members disagree on salience_tier
  multi_certainty  members disagree on certainty
  large_merge      >= 5 members

SUGGESTED VERDICT CODING for verdict_same_condition
  Y    one condition, the consolidated line represents it fairly
  N    two or more distinct conditions merged -- a grouping defect
  P    same condition but the line loses a clinically needed qualifier
  ?    cannot tell from the text alone

    cd ~/clin_longsum
    python src/clinical_review_tools/b2_b4_consolidation/b4_merge_worksheet.py
    python src/clinical_review_tools/b2_b4_consolidation/b4_merge_worksheet.py --subject 90000035
    python src/clinical_review_tools/b2_b4_consolidation/b4_merge_worksheet.py --out-dir /tmp/x
    python src/clinical_review_tools/b2_b4_consolidation/b4_merge_worksheet.py --limit 20        # first N subjects only
    python src/clinical_review_tools/b2_b4_consolidation/b4_merge_worksheet.py --json-only       # files still written; skips the console report
"""

from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
import sys
from pathlib import Path

import _bootstrap  # noqa: F401  (puts src/ on sys.path)
import config
import io_utils

LEDGER = config.OUTPUT_DIR / "retention_ledger.jsonl"
RAW = config.OUTPUT_DIR / "retention_ledger.raw.jsonl"
DEFAULT_OUT = config.OUTPUT_DIR / "review_tools" / "merge_grouping"

COLS = ["merge_id", "subject_id", "tier", "n_members", "n_distinct_member_classes", "flags",
        "consolidated_class", "consolidated_lifecycle", "consolidated_text",
        "member_idx", "member_class", "member_tier", "member_lifecycle", "member_certainty",
        "member_n_mentions", "member_n_admissions", "member_text",
        "verdict_same_condition", "reviewer_note"]

_FLAG_ORDER = {"multi_lifecycle": 0, "multi_class": 1, "multi_tier": 2}


def _node_key(pid: int, fact_ids: list) -> str:
    """Stable id for a merge. Same hash f9 uses, so a row here joins to an f9 record."""
    h = hashlib.sha1((str(pid) + "|" + ",".join(map(str, sorted(fact_ids)))).encode()).hexdigest()
    return h[:16]


def _members_of(c_node: dict, raw_nodes: list[dict]) -> list[dict]:
    """Raw nodes whose fact_ids are a subset of the consolidated node's. Byte-identical
    to f9_merge_faithfulness._members_of - keep them in step if either changes."""
    fc = set(c_node.get("fact_ids") or [])
    if not fc:
        return []
    out = []
    for r in raw_nodes:
        rf = set(r.get("fact_ids") or [])
        if rf and rf <= fc:
            out.append(r)
    return out


def _flags(members: list[dict]) -> list[str]:
    f = []
    if len({m.get("retention_class") for m in members}) > 1:
        f.append("multi_class")
    if len({m.get("current_lifecycle") for m in members}) > 1:
        f.append("multi_lifecycle")       # b4 guardrail says this cannot happen
    if len({m.get("salience_tier") for m in members}) > 1:
        f.append("multi_tier")
    if len({m.get("certainty") for m in members}) > 1:
        f.append("multi_certainty")
    if len(members) >= 5:
        f.append("large_merge")
    return f


def build(pids: list[int], con: dict, raw: dict) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    merges: list[dict] = []
    n_partial = n_orphan = n_zero_member = 0

    for pid in pids:
        c_nodes = con.get(pid, {}).get("nodes") or []
        r_nodes = raw.get(pid, {}).get("nodes") or []
        for c in c_nodes:
            mem = _members_of(c, r_nodes)
            if not mem:
                n_zero_member += 1
                continue
            fc = set(c.get("fact_ids") or [])
            union = set().union(*[set(m.get("fact_ids") or []) for m in mem])
            if union != fc:                      # recovery shortfall -- sheet would be incomplete
                n_partial += 1
                n_orphan += len(fc - union)
            if len(mem) < 2:
                continue                         # pass-through, not a merge
            flags = _flags(mem)
            merge = {
                "merge_id": f"{pid}-{_node_key(pid, c.get('fact_ids') or [])}",
                "subject_id": pid,
                "tier": c.get("salience_tier") or "",
                "n_members": len(mem),
                "n_distinct_member_classes": len({m.get("retention_class") for m in mem}),
                "flags": "|".join(flags),
                "consolidated_class": c.get("retention_class") or "",
                "consolidated_lifecycle": c.get("current_lifecycle") or "",
                "consolidated_text": (c.get("text") or "").strip(),
            }
            merges.append({**merge, "_classes": [m.get("retention_class") or "" for m in mem]})
            for i, m in enumerate(sorted(mem, key=lambda x: -(x.get("n_mentions") or 0)), 1):
                rows.append({
                    **merge,
                    "member_idx": i,
                    "member_class": m.get("retention_class") or "",
                    "member_tier": m.get("salience_tier") or "",
                    "member_lifecycle": m.get("current_lifecycle") or "",
                    "member_certainty": m.get("certainty") or "",
                    "member_n_mentions": m.get("n_mentions") or 0,
                    "member_n_admissions": len(m.get("source_hadm_ids") or []),
                    "member_text": (m.get("text") or "").strip(),
                    "verdict_same_condition": "",
                    "reviewer_note": "",
                })

    def _sort_key(r):
        first = r["flags"].split("|")[0] if r["flags"] else "zz"
        return (_FLAG_ORDER.get(first, 9), -r["n_members"], r["subject_id"],
                r["merge_id"], r["member_idx"])
    rows.sort(key=_sort_key)

    pairs = collections.Counter()
    for m in merges:
        if len(set(m["_classes"])) > 1:
            pairs["+".join(sorted(set(m["_classes"])))] += 1
    summary = {
        "worksheet": "b4_ledger_consolidation_grouping",
        "source_taxonomy_version": dict(collections.Counter(
            r.get("taxonomy_version") for r in con.values())),
        "ledger_generated_at": sorted({(r.get("generated_at") or "")[:10] for r in con.values()}),
        "patients_scanned": len(pids),
        "patients_with_merges": len({m["subject_id"] for m in merges}),
        "merges": len(merges),
        "member_rows": len(rows),
        "size_distribution": {str(k): v for k, v in sorted(
            collections.Counter(m["n_members"] for m in merges).items())},
        "by_tier": dict(collections.Counter(m["tier"] for m in merges)),
        "by_flag": dict(collections.Counter(
            f for m in merges for f in (m["flags"].split("|") if m["flags"] else ["clean"]))),
        "cross_class_pairs": dict(pairs.most_common()),
        "recovery_incomplete": {
            "merges_not_fully_covered": n_partial,
            "orphan_fact_ids": n_orphan,
            "consolidated_nodes_with_no_member": n_zero_member,
        },
    }
    return rows, summary


def main() -> int:
    ap = argparse.ArgumentParser(description="B4 consolidation grouping-adjudication worksheet.")
    ap.add_argument("--subject", type=int, default=None, help="one subject_id only")
    ap.add_argument("--limit", type=int, default=None, help="first N subjects")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()

    if not LEDGER.exists():
        raise SystemExit(f"{LEDGER} not found. Run b2 (and b4) first.")
    if not RAW.exists():
        raise SystemExit(f"{RAW} not found. The worksheet needs the pre-consolidation ledger; "
                         f"run b4 with CONSOLIDATE_LEDGER on (it writes the .raw backup).")

    con = {int(r["subject_id"]): r for r in io_utils.read_jsonl(LEDGER)}
    raw = {int(r["subject_id"]): r for r in io_utils.read_jsonl(RAW)}
    pids = [args.subject] if args.subject is not None else sorted(con)
    if args.limit:
        pids = pids[:args.limit]

    rows, summary = build(pids, con, raw)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.out_dir / "merge_grouping_adjudication.csv"
    json_path = args.out_dir / "merge_grouping_summary.json"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if not args.json_only:
        rec = summary["recovery_incomplete"]
        print("\n" + "=" * 60)
        print("B4 CONSOLIDATION GROUPING WORKSHEET")
        print(f"  patients / merges  : {summary['patients_with_merges']} / {summary['merges']}")
        print(f"  member rows        : {summary['member_rows']}")
        print(f"  by tier            : {summary['by_tier']}")
        print(f"  by flag            : {summary['by_flag']}")
        if summary["cross_class_pairs"]:
            print(f"  cross-class pairs  : {summary['cross_class_pairs']}")
        if any(rec.values()):
            print(f"  WARNING recovery incomplete: {rec} - the sheet is missing merges")
        else:
            print("  member recovery    : exact (every consolidated node fully covered)")
        print(f"\n  csv  -> {csv_path}")
        print(f"  json -> {json_path}")
        print("\n  Contains patient text. Keep it out of the repo; never commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
