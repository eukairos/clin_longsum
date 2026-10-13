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
b2_merge_worksheet.py
=====================
Clinician adjudication worksheet for B2 DETERMINISTIC MERGING 

WHY A SEPARATE SCRIPT FROM b4_merge_worksheet.py
------------------------------------------------
b4 is not where most merging happens. Every RAW ledger node is already a merge of
facts, produced by b2 at two distinct points with different failure modes:

  1. CLUSTERING      _cluster + _split_cluster
     Token-overlap grouping WITHIN one retention_class, then split by condition
     identity. _build_node then takes text/class/certainty/lifecycle from the
     LATEST member. This is where Failure Mode-7 lives: a compound fact naming two
     conditions bridges them into one cluster, one condition supplies the
     displayed text, and the other's fact_id survives inside the node, but isn't displayed - not a
     prune, so no dropped[] entry and no prune_reason fires.

  2. CONSOLIDATION   _consolidate + _merge_group
     Merges NODES within a compatibility group (_MERGE_GROUP: diseases together,
     procedures/devices together, every other class only with its own), when
     Jaccard >= JACCARD_THRESH, a curated condition key matches, or the shorter
     node's phrase is contained verbatim in the longer. _split_group re-applies
     the identity guard. This is the only stage in the whole pipeline that merges
     ACROSS retention_class, which makes its rows the highest-yield to adjudicate.

The stored ledger cannot tell the two apart: a node with n_mentions=5 may be five
facts in one cluster or 2+3 from consolidation. So this script RECOMPUTES both
stages by calling b2's own functions on retention_facts.jsonl, mirroring
_project_patient's routing exactly, and labels every row with the stage that
produced it.

CONSEQUENCE: this reflects the CURRENT code and CURRENT taxonomy, which may differ
from the ledger on disk. That is usually what you want for adjudication (you are
judging what b2 does now), but it means a verdict here does not necessarily
describe the served ledger. The summary records both taxonomy versions so the gap
is visible. Re-run b2 if you need them aligned.

READ-ONLY. Calls b2's pure functions; writes ONE csv + ONE json under
OUTPUT_DIR/review_tools/merge_grouping/ and no pipeline artifact. It does NOT run the
b2 stage - nothing is written to retention_ledger.jsonl.

OUTPUT (long format: one row per MEMBER, merge-level columns repeated)

  b2_merge_adjudication.csv
    stage merge_id subject_id merged_tier merged_class n_members flags merged_text
    member_idx member_text member_class member_tier member_certainty
    member_lifecycle member_admit_seq member_n_mentions
    verdict_same_condition reviewer_note      <- blank, for the reviewer

  b2_merge_summary.json   per-stage counts, size distributions, flag tally

  stage = b2_cluster       members are FACTS      (member_admit_seq populated)
  stage = b2_consolidate   members are NODES      (member_n_mentions populated)

Rows sort suspicious-first (multi_class, multi_tier, multi_lifecycle, then largest).

FLAGS  (each is a REVIEW HINT, not a defect)
  multi_class          members disagree on retention_class. IMPOSSIBLE at b2_cluster
                       (clustering partitions by class first), so it only ever fires
                       on b2_consolidate, where it is by design - see _MERGE_GROUP.
                       These are the rows the disease bucket licenses, and the same
                       classes Failure-Mode-9 shows are least separable, so they compound.
  multi_tier           members disagree on salience_tier. The merged node keeps the
                       representative's tier, so this merge CHANGED a node's tier.
  multi_lifecycle      members disagree on current_lifecycle. b2 has no guardrail
                       against this (b4 does), so it is expected, not an anomaly.
  multi_negative_kind  members disagree on negative_kind. _merge_group resolves via
                       tax.merge_negative_kind, which ranks None (present) highest,
                       so a surveillance/resolved qualifier can be dropped in favour
                       of a sibling asserting the condition is present. Correct in
                       general; check the direction on each one.
  identical_text       every member has the same text - a pure duplicate collapse,
                       safe to skip when sampling.
  large_merge          >= 5 members

WHERE THE FM-8 RESIDUAL WOULD SHOW
  The FM-8 fix (a retraction wins outright) lives in _build_node and has NO
  counterpart in _merge_group, which uses plain merge_negative_kind. So a
  safety-robust SUPERSEDED node absorbed into an active sibling could silently
  lose its de-label. The summary counts this directly as
  `superseded_absorbed_into_active`; it was 0 on the dev cohort, where only one
  safety-robust superseded node reaches consolidation at all. Re-check on any
  cohort with more de-labelling.

SUGGESTED VERDICT CODING for verdict_same_condition
  Y    one condition, the merged line represents it fairly
  N    two or more distinct conditions merged -- a grouping defect
  P    same condition but the line loses a clinically needed qualifier
  ?    cannot tell from the text alone

SAMPLING: the three merge surfaces (b2_cluster, b2_consolidate, b4) are different
mechanisms and should be estimated SEPARATELY, not pooled into one rate.

    cd ~/clin_longsum
    python src/clinical_review_tools/b2_b4_consolidation/b2_merge_worksheet.py
    python src/clinical_review_tools/b2_b4_consolidation/b2_merge_worksheet.py --subject 90000035
    python src/clinical_review_tools/b2_b4_consolidation/b2_merge_worksheet.py --stage consolidate
    python src/clinical_review_tools/b2_b4_consolidation/b2_merge_worksheet.py --limit 20        # first N subjects only
    python src/clinical_review_tools/b2_b4_consolidation/b2_merge_worksheet.py --out-dir /tmp/x  # default: OUTPUT_DIR/review_tools/merge_grouping/
    python src/clinical_review_tools/b2_b4_consolidation/b2_merge_worksheet.py --json-only       # files still written; skips the console report
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import sys
from pathlib import Path

import _bootstrap  # noqa: F401  (puts src/ on sys.path)
import config
import b2_build_ledger as b2
import retention_taxonomy as tax

DEFAULT_OUT = config.OUTPUT_DIR / "review_tools" / "merge_grouping"

COLS = ["stage", "merge_id", "subject_id", "merged_tier", "merged_class", "n_members", "flags",
        "merged_text", "member_idx", "member_text", "member_class", "member_tier",
        "member_certainty", "member_lifecycle", "member_admit_seq", "member_n_mentions",
        "verdict_same_condition", "reviewer_note"]

_FLAG_ORDER = {"multi_class": 0, "multi_tier": 1, "multi_lifecycle": 2, "multi_negative_kind": 3}


def _routed_nodes(facts, dates):
    """Reproduce _project_patient up to the point _consolidate is called.

    Returns (routed_nodes, cluster_parts) where cluster_parts is [(node, [facts])] for
    every cluster, including the ones routed to `amended`. Mirrors b2 exactly:
    candidates -> partition by retention_class -> _cluster -> _split_cluster ->
    _build_node -> drop non-safety superseded to `amended`.
    """
    candidates = [f for f in facts
                  if f.certainty != "ruled_out"
                  and f.retention_class != "recurrent_episode"
                  and b2._is_candidate(f)]
    by_class: dict[str, list] = {}
    for f in candidates:
        by_class.setdefault(f.retention_class, []).append(f)

    cluster_parts, routed = [], []
    for cls_facts in by_class.values():
        for cluster in b2._cluster(cls_facts):
            for part in b2._split_cluster(cluster):
                node = b2._build_node(part, dates)
                cluster_parts.append((node, part))
                if node.current_lifecycle == "superseded" and \
                        not tax.is_safety_robust(node.retention_class):
                    continue                      # -> amended; never reaches consolidation
                routed.append(node)
    return routed, cluster_parts


def _flags_cluster(part) -> list[str]:
    f = []
    if len({x.retention_class for x in part}) > 1:
        f.append("multi_class")                   # cannot happen: clustering is per-class
    if len({x.salience_tier for x in part}) > 1:
        f.append("multi_tier")
    if len({x.lifecycle for x in part}) > 1:
        f.append("multi_lifecycle")
    if len({(x.negative_kind or "") for x in part}) > 1:
        f.append("multi_negative_kind")
    if len({x.text.strip().lower() for x in part}) == 1:
        f.append("identical_text")
    if len(part) >= 5:
        f.append("large_merge")
    return f


def _flags_consol(mem) -> list[str]:
    f = []
    if len({m.retention_class for m in mem}) > 1:
        f.append("multi_class")
    if len({m.salience_tier for m in mem}) > 1:
        f.append("multi_tier")
    if len({m.current_lifecycle for m in mem}) > 1:
        f.append("multi_lifecycle")
    if len({(m.negative_kind or "") for m in mem}) > 1:
        f.append("multi_negative_kind")
    if len({m.text.strip().lower() for m in mem}) == 1:
        f.append("identical_text")
    if len(mem) >= 5:
        f.append("large_merge")
    return f


def build(by_pid: dict, dates: dict, pids: list[int], stages: set[str]):
    rows: list[dict] = []
    sizes = {"b2_cluster": [], "b2_consolidate": []}
    pairs = collections.Counter()
    flag_combos = {"b2_cluster": collections.Counter(), "b2_consolidate": collections.Counter()}
    n_superseded_routed = n_superseded_absorbed = 0
    nk_dropped = 0
    seq = 0

    for pid in pids:
        facts = by_pid.get(pid)
        if not facts:
            continue
        routed, cluster_parts = _routed_nodes(facts, dates)
        n_superseded_routed += sum(1 for n in routed if n.current_lifecycle == "superseded")

        if "cluster" in stages:
            for node, part in cluster_parts:
                if len(part) < 2:
                    continue
                seq += 1
                flags = _flags_cluster(part)
                sizes["b2_cluster"].append(len(part))
                flag_combos["b2_cluster"]["|".join(flags) or "clean"] += 1
                if len({x.retention_class for x in part}) > 1:
                    pairs["+".join(sorted({x.retention_class for x in part}))] += 1
                base = {
                    "stage": "b2_cluster", "merge_id": f"{pid}-C{seq}", "subject_id": pid,
                    "merged_tier": node.salience_tier, "merged_class": node.retention_class,
                    "n_members": len(part), "flags": "|".join(flags),
                    "merged_text": node.text.strip(),
                }
                for i, x in enumerate(sorted(part, key=b2._seq_key), 1):
                    rows.append({
                        **base, "member_idx": i, "member_text": x.text.strip(),
                        "member_class": x.retention_class, "member_tier": x.salience_tier,
                        "member_certainty": x.certainty, "member_lifecycle": x.lifecycle,
                        "member_admit_seq": x.admit_seq if x.admit_seq is not None else "",
                        "member_n_mentions": 1,
                        "verdict_same_condition": "", "reviewer_note": "",
                    })

        post = b2._consolidate(routed)
        for c in post:
            fc = set(c.fact_ids)
            mem = [n for n in routed if set(n.fact_ids) and set(n.fact_ids) <= fc]
            if len(mem) < 2:
                continue
            if any(m.current_lifecycle == "superseded" for m in mem) and \
                    c.current_lifecycle != "superseded":
                n_superseded_absorbed += 1        # the FM-8-shaped hole at _merge_group
            if any(m.negative_kind for m in mem) and not c.negative_kind:
                nk_dropped += 1
            if "consolidate" not in stages:
                continue
            seq += 1
            flags = _flags_consol(mem)
            sizes["b2_consolidate"].append(len(mem))
            flag_combos["b2_consolidate"]["|".join(flags) or "clean"] += 1
            if len({m.retention_class for m in mem}) > 1:
                pairs["+".join(sorted({m.retention_class for m in mem}))] += 1
            base = {
                "stage": "b2_consolidate", "merge_id": f"{pid}-M{seq}", "subject_id": pid,
                "merged_tier": c.salience_tier, "merged_class": c.retention_class,
                "n_members": len(mem), "flags": "|".join(flags),
                "merged_text": c.text.strip(),
            }
            for i, m in enumerate(sorted(mem, key=lambda x: -(x.n_mentions or 0)), 1):
                rows.append({
                    **base, "member_idx": i, "member_text": m.text.strip(),
                    "member_class": m.retention_class, "member_tier": m.salience_tier,
                    "member_certainty": m.certainty, "member_lifecycle": m.current_lifecycle,
                    "member_admit_seq": "", "member_n_mentions": m.n_mentions,
                    "verdict_same_condition": "", "reviewer_note": "",
                })

    def _sort_key(r):
        first = r["flags"].split("|")[0] if r["flags"] else "zz"
        return (_FLAG_ORDER.get(first, 9), -r["n_members"], r["subject_id"],
                r["merge_id"], r["member_idx"])
    rows.sort(key=_sort_key)

    def _dist(v):
        return {str(k): n for k, n in sorted(collections.Counter(v).items())}

    summary = {
        "worksheet": "b2_deterministic_merge_grouping",
        "recomputed_with_taxonomy_version": tax.TAXONOMY_VERSION,
        "note": ("recomputed from retention_facts.jsonl with the CURRENT b2 code; the ledger "
                 "on disk may have been built by an older taxonomy version - compare with "
                 "retention_ledger.jsonl's taxonomy_version before treating a verdict here "
                 "as describing the SERVED ledger"),
        "patients_scanned": len(pids),
        "stages": sorted(stages),
        "cluster_merges": len(sizes["b2_cluster"]),
        "consolidate_merges": len(sizes["b2_consolidate"]),
        "member_rows": len(rows),
        "cluster_size_distribution": _dist(sizes["b2_cluster"]),
        "consolidate_size_distribution": _dist(sizes["b2_consolidate"]),
        "cross_class_pairs": dict(pairs.most_common()),
        "flag_combinations": {k: dict(v.most_common()) for k, v in flag_combos.items()},
        "fm8_residual_check": {
            "safety_robust_superseded_nodes_reaching_consolidation": n_superseded_routed,
            "superseded_absorbed_into_active": n_superseded_absorbed,
            "merges_where_a_member_negative_kind_was_dropped": nk_dropped,
        },
    }
    return rows, summary


def main() -> int:
    ap = argparse.ArgumentParser(description="b2 deterministic-merge adjudication worksheet.")
    ap.add_argument("--subject", type=int, default=None, help="one subject_id only")
    ap.add_argument("--limit", type=int, default=None, help="first N subjects")
    ap.add_argument("--stage", choices=["cluster", "consolidate", "both"], default="both")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()

    by_pid = b2._read_facts()            # applies tax.normalize_fact on the way in
    dates = b2._hadm_dates()
    pids = [args.subject] if args.subject is not None else sorted(by_pid)
    if args.limit:
        pids = pids[:args.limit]
    stages = {"cluster", "consolidate"} if args.stage == "both" else {args.stage}

    rows, summary = build(by_pid, dates, pids, stages)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.out_dir / "b2_merge_adjudication.csv"
    json_path = args.out_dir / "b2_merge_summary.json"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if not args.json_only:
        fm8 = summary["fm8_residual_check"]
        print("\n" + "=" * 60)
        print("B2 DETERMINISTIC MERGE WORKSHEET")
        print(f"  recomputed at      : {summary['recomputed_with_taxonomy_version']}")
        print(f"  cluster merges     : {summary['cluster_merges']}")
        print(f"  consolidate merges : {summary['consolidate_merges']}")
        print(f"  member rows        : {summary['member_rows']}")
        if summary["cross_class_pairs"]:
            print("  cross-class merges (consolidation only, by design):")
            for k, v in summary["cross_class_pairs"].items():
                print(f"    {v:5d}  {k}")
        print(f"  FM-8 residual      : {fm8}")
        if fm8["superseded_absorbed_into_active"]:
            print("  WARNING a superseded node was absorbed into an active line -- "
                  "_merge_group has no retraction guard. Inspect these first.")
        print(f"\n  csv  -> {csv_path}")
        print(f"  json -> {json_path}")
        print("\n  Contains patient text. Keep it out of the repo; never commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
