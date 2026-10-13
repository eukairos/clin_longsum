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
dump_clusters.py - scratch, READ-ONLY. Show b2's clustering for one subject.
For each cluster: the member fact texts + the node text _build_node chose (latest by
admit_seq), then the node list after _consolidate. Reveals relabel-collisions (distinct
conditions clustered, node text taken from the wrong member) and folded compounds.

--variant previews a candidate _cluster metric WITHOUT touching b2:
  current       len(shared) >= _MIN_SHARED  AND overlap  >= CLUSTER_THRESH   (b2 default)
  jaccard       len(shared) >= _MIN_SHARED  AND jaccard  >= CLUSTER_THRESH
  overlap_gen1  >=1 shared SPECIFIC token   AND overlap  >= CLUSTER_THRESH   
  overlap_gen2  >=2 shared SPECIFIC tokens  AND overlap  >= CLUSTER_THRESH
"specific" = shared tokens minus a generic set (chronic/acute/disease/...). 
Inputs come from b2, which reads config, so the tool follows the RUNNING namespace
(LONGSUM_DATA_ROOT + LONGSUM_NAMESPACE).

    cd src/clinical_review_tools/b1_b4
    python dump_clusters.py --subject 90000055
    python dump_clusters.py --subject 90000055 --variant overlap_gen1
    python dump_clusters.py --subject 90000033 --variant overlap_gen1 --class chronic_disease_state
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # src/ on the path (tool is 2 dirs deep)

import config
import b2_build_ledger as b2

GENERIC = frozenset({"chronic", "acute", "disease", "disorder", "syndrome",
                     "condition", "history", "stage", "recurrent"})


def _specific(shared: set) -> set:
    return shared - GENERIC


def _shares_specific(a: str, b: str) -> bool:
    return bool(_specific(b2._content_tokens(a) & b2._content_tokens(b)))


def _pred_current(toks, ct):
    return len(toks & ct) >= b2._MIN_SHARED and b2._overlap(toks, ct) >= b2.CLUSTER_THRESH


def _pred_jaccard(toks, ct):
    return len(toks & ct) >= b2._MIN_SHARED and b2._jaccard(toks, ct) >= b2.CLUSTER_THRESH


def _pred_ov_gen1(toks, ct):
    return len(_specific(toks & ct)) >= 1 and b2._overlap(toks, ct) >= b2.CLUSTER_THRESH


def _pred_ov_gen2(toks, ct):
    return len(_specific(toks & ct)) >= 2 and b2._overlap(toks, ct) >= b2.CLUSTER_THRESH


PRED = {"current": _pred_current, "jaccard": _pred_jaccard,
        "overlap_gen1": _pred_ov_gen1, "overlap_gen2": _pred_ov_gen2}


def _cluster_with(facts, predicate):
    """b2._cluster's accumulate-into-union logic with a pluggable predicate."""
    clusters, ctoks = [], []
    for f in facts:
        toks = b2._content_tokens(f.text)
        placed = False
        for i, ct in enumerate(ctoks):
            if predicate(toks, ct):
                clusters[i].append(f)
                ctoks[i] = ct | toks
                placed = True
                break
        if not placed:
            clusters.append([f])
            ctoks.append(set(toks))
    return clusters


def main() -> int:
    ap = argparse.ArgumentParser(description="Dump b2 clustering for one subject.")
    ap.add_argument("--subject", type=int, required=True)
    ap.add_argument("--class", dest="cls", default=None, help="restrict to one retention_class")
    ap.add_argument("--variant", choices=list(PRED), default="current",
                    help="cluster metric to preview (default current = b2 today)")
    args = ap.parse_args()
    predicate = PRED[args.variant]

    print(f"namespace: {config.RUN_NAMESPACE or '(default)'}  |  data: {config.DATA_DIR}")
    facts = b2._read_facts().get(args.subject, [])
    if not facts:
        raise SystemExit(f"no facts for subject {args.subject}")
    dates = b2._hadm_dates()
    cands = [f for f in facts if f.certainty != "ruled_out"
             and f.retention_class != "recurrent_episode" and b2._is_candidate(f)]
    by_class: dict = {}
    for f in cands:
        by_class.setdefault(f.retention_class, []).append(f)

    raw_nodes = []
    print(f"\nsubject {args.subject}: {len(cands)} candidates  [variant={args.variant}]\n" + "=" * 72)
    for cls, fs in sorted(by_class.items()):
        clusters = _cluster_with(fs, predicate)
        nodes = [b2._build_node(c, dates) for c in clusters]
        raw_nodes += nodes
        if args.cls and cls != args.cls:
            continue
        print(f"\n## {cls}  ({len(fs)} facts -> {len(clusters)} clusters)")
        for c, node in zip(clusters, nodes):
            members = sorted({f.text for f in c})
            mixed = [m for m in members if m != node.text and not _shares_specific(m, node.text)]
            tag = "   <-- RELABEL-COLLISION" if mixed else ""
            print(f"  NODE: {node.text}{tag}")
            for m in members:
                if m != node.text:
                    mark = " [no shared specific token]" if m in mixed else ""
                    print(f"      <- {m}{mark}")

    cons = b2._consolidate(raw_nodes)
    print("\n" + "=" * 72 + f"\nAFTER _consolidate (served nodes)  [variant={args.variant}]:")
    for n in sorted(cons, key=lambda n: (n.retention_class, n.text)):
        if not args.cls or n.retention_class == args.cls:
            print(f"  [{n.salience_tier}] {n.retention_class}: {n.text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
