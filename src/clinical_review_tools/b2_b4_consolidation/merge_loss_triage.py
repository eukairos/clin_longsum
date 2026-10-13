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
merge_loss_triage.py
====================
Triage across ALL THREE merge surfaces: which merges silently DROPPED a
high-salience clinical concept, and of those, which were not recovered anywhere
else in what the clinician is served.

WHY THIS EXISTS
---------------
The b2/b4 adjudication worksheets list every merge, and a full read is expensive:
1546 b2 merges plus 213 b4 merges on the dev cohort. Hand-adjudicating the b4
sheet showed the most commonly encountered pattern is narrow and mechanical:

  a SHORT representative absorbs a LONGER, more specific member, and a concept
  the member carried is absent from the merged text.

`_rep_of` (b2) and b4's LLM roll-up pick a representative by tier, certainty and
recency - never by informativeness. When the winning mention is the terse one
("Coronary artery disease"), the specific member's content ("...status post stent
placement") is gone from the served text even though its fact_id survives inside
the node. No prune fires, dropped[] stays empty, and f1 counts the fact as
represented because it still homes to a node. Invisible to every gate.

MOST SUCH LOSSES ARE HARMLESS, and that is the point of this script. Hand review
of the b4 sheet found that nearly every flagged loss was rescued either by the
merged line naming the concept in other words or by the concept being pinned on a
DIFFERENT ledger node. Only one of 213 was a true loss. So the expensive question
is not "was a concept dropped at this merge" but:

  dropped at the merge?  AND  absent from the whole served ledger?
  AND absent from the served prose?  AND present in bhc_text?

Only the intersection is worth a clinician's time. On the dev cohort that took
1546 b2 merges down to a double-digit shortlist.

METHOD
------
1. Recompute b2's two merge stages by calling b2's own functions (see
   b2_merge_worksheet.py for why the stored ledger cannot distinguish them), and
   recover b4's merges from the artifacts by the fact_ids subset rule f9 uses.
2. For each merge, for each member: concepts matched in the MEMBER text but not in
   the MERGED text = loss events. Negated mentions are skipped.
3. Recovery check, per event, scoped to that subject:
     in_ledger  concept appears in ANY served node/flagged/amended/episode/excluded
     in_prose   concept appears in the TL;DR, current episode, hot window or the
                compacted Prior Admissions paragraph
     in_bhc     concept appears in the established-span bhc_text (GROUND TRUTH)
4. Priority:
     REVIEW        not in ledger, not in prose, IS in bhc_text  <- the shortlist
     CHECK_SOURCE  not in ledger, not in prose, NOT in bhc_text. b1 paraphrases,
                   so absence from source is NOT proof of fabrication -- the fact
                   may be a legitimate rewording. Read the note before judging.
     recovered_*   found elsewhere; logged for completeness, not for review

WHAT THIS IS NOT
----------------
A metric. It is a concept-keyword screen over a curated lexicon (devices,
surgeries, infection-control organisms, malignancy, immunosuppression, ADR/allergy,
code status, major bleeding). It will miss any loss whose concept is not in
CONCEPTS, and it says nothing about whether a merge was clinically sensible -- that
is the adjudication worksheets' job. Read it as a triage filter, never as coverage.

A REVIEW row is not a defect. Two things must still be checked by hand, and both
have already overturned findings on this cohort:
  - follow the patient's later course. A concept dropped at admission 3 and
    re-established at admission 6 was never lost from the record.
  - read the source. `code_status_directive` is _SPLIT_EXEMPT precisely because a
    directive legitimately MUTATES: four confirmed DNR/DNI mentions superseded by a
    later documented "Full" is correct latest-wins behaviour, not a loss. The
    split_exempt column flags those rows.

READ-ONLY. Calls b2's pure functions and reads artifacts; writes ONE csv + ONE json
under OUTPUT_DIR/review_tools/merge_grouping/ and no pipeline artifact.

OUTPUT  merge_loss_triage.csv
  priority stage subject_id domain concept split_exempt merged_class merged_tier
  n_members in_ledger in_prose in_bhc merged_text member_text
  verdict reviewer_note        <- blank, for the reviewer

  suggested verdict coding: L  genuine loss, concept gone from the served record
                            R  recovered - restated later in the patient's course
                            D  by design (directive supersession, correct latest-wins)
                            F  false positive of the keyword screen
                            ?  undecidable from the text

    cd ~/clin_longsum
    python src/clinical_review_tools/b2_b4_consolidation/merge_loss_triage.py
    python src/clinical_review_tools/b2_b4_consolidation/merge_loss_triage.py --stage b4
    python src/clinical_review_tools/b2_b4_consolidation/merge_loss_triage.py --all-events
    python src/clinical_review_tools/b2_b4_consolidation/merge_loss_triage.py --subject 90000035   # one subject_id only
    python src/clinical_review_tools/b2_b4_consolidation/merge_loss_triage.py --limit 20          # first N subjects only
    python src/clinical_review_tools/b2_b4_consolidation/merge_loss_triage.py --out-dir /tmp/x    # default: OUTPUT_DIR/review_tools/merge_grouping/
    python src/clinical_review_tools/b2_b4_consolidation/merge_loss_triage.py --json-only         # files still written; skips the console report
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import re
import sys
from pathlib import Path

import _bootstrap  # noqa: F401  (puts src/ on sys.path)
import config
import cohort
import holdout
import b2_build_ledger as b2
import retention_taxonomy as tax

DEFAULT_OUT = config.OUTPUT_DIR / "review_tools" / "merge_grouping"
STAGES = ("b2_cluster", "b2_consolidate", "b4")

# High-salience concepts only. A loss outside this lexicon is invisible here.
# Patterns allow a parenthetical or a couple of intervening words, because the
# merged line often expands an abbreviation ("IVC filter" -> "inferior vena cava
# (IVC) filter") and a naive \bivc filter\b would call that a loss.
CONCEPTS = [
    ("device", "stent", r"\bstent"),
    ("device", "pacemaker/ICD", r"\b(pacemaker|icd|defibrillator|ppm)\b"),
    ("device", "PICC/port", r"\b(picc|port-?a-?cath|mediport)\b"),
    ("device", "AV fistula/graft", r"\b(av fistula|arteriovenous fistula|avf)\b"),
    ("device", "drainage catheter", r"\b(pleurx|nephrostom|tunneled catheter|indwelling catheter)\b"),
    ("device", "shunt/TIPS", r"\b(shunt|tips)\b"),
    ("device", "IVC filter", r"\b(ivc|inferior vena cava)\b[^.;]{0,25}\bfilter\b"),
    ("device", "LVAD", r"\blvad\b"),
    ("device", "tracheostomy", r"\btracheostom"),
    ("device", "PEG/G-tube", r"\b(peg tube|g-?tube|gastrostom)\b"),
    ("device", "pessary", r"\bpessary\b"),
    ("device", "prosthesis/arthroplasty", r"\b(prosthesis|prosthetic|arthroplasty|joint replacement)\b"),
    ("surgery", "transplant", r"\btransplant"),
    ("surgery", "amputation", r"\bamputat"),
    ("surgery", "-ectomy", r"\b\w+ectom(y|ies|ised|ized)\b"),
    ("surgery", "-ostomy", r"\b(colostom|ileostom|urostom|jejunostom)"),
    ("surgery", "bypass/CABG", r"\b(bypass|cabg)\b"),
    ("surgery", "resection", r"\bresect"),
    ("surgery", "ORIF/fixation", r"\b(orif|open reduction|internal fixation)\b"),
    ("infection", "MRSA", r"\bmrsa\b"),
    ("infection", "VRE", r"\bvre\b"),
    ("infection", "C difficile", r"\b(c\.? ?diff|clostrid)"),
    ("infection", "ESBL/CRE", r"\b(esbl|carbapenem-resistant)\b"),
    ("infection", "colonisation", r"\bcoloni[sz]"),
    ("infection", "tuberculosis", r"\btuberculosis\b"),
    ("infection", "HIV", r"\bhiv\b"),
    ("infection", "hepatitis B/C", r"\bhepatitis [bc]\b|\bhbv\b|\bhcv\b"),
    ("malignancy", "carcinoma/cancer", r"\b(carcinoma|cancer|malignan)"),
    ("malignancy", "metastatic", r"\bmetasta"),
    ("malignancy", "heme malignancy", r"\b(lymphoma|leukemia|leukaemia|myeloma|sarcoma|melanoma)\b"),
    ("immune", "chemotherapy", r"\bchemotherap"),
    ("immune", "immunosuppression", r"\bimmunosuppress"),
    ("immune", "neutropenia", r"\bneutropeni"),
    ("immune", "asplenia", r"\basplen"),
    ("adr", "allergy/intolerance", r"\b(allerg|anaphyla|intoleran)"),
    ("code", "DNR/DNI", r"\b(dnr|dni|dnar)\b"),
    ("code", "comfort/hospice", r"\b(comfort measures|hospice|palliative)\b"),
    ("bleed", "major bleeding", r"\b(gi bleed|gastrointestinal bleed|h[ae]morrhag|variceal bleed)"),
]
CX = [(d, n, re.compile(p, re.I)) for d, n, p in CONCEPTS]
RX = {n: r for _, n, r in CX}
NEG = re.compile(r"\b(no|not|non|denies|denied|without|negative|ruled out|r/o)\b", re.I)

COLS = ["priority", "stage", "subject_id", "domain", "concept", "split_exempt",
        "merged_class", "merged_tier", "n_members", "in_ledger", "in_prose", "in_bhc",
        "merged_text", "member_text", "verdict", "reviewer_note"]


def concepts_in(text: str) -> set[tuple[str, str]]:
    return {(d, n) for d, n, rx in CX if rx.search(text or "")}


def negated(text: str, rx: re.Pattern) -> bool:
    """Negation window BEFORE the concept mention. Deliberately narrow: a wider
    window swallows unrelated clauses, and a false 'negated' hides a real loss."""
    m = rx.search(text or "")
    if not m:
        return False
    return bool(NEG.search(text[max(0, m.start() - 40):m.start()]))


def _read_jsonl(p: Path):
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def served_text(pid: int, led: dict, view: dict) -> tuple[str, str]:
    """(everything the ledger serves, everything the prose serves) for one subject."""
    r = led.get(pid, {})
    lparts = [n.get("text", "") for k in ("nodes", "flagged", "amended") for n in (r.get(k) or [])]
    lparts += [e.get("parent_condition", "") for e in (r.get("episodes") or [])]
    lparts += list(r.get("considered_excluded") or [])
    v = view.get(pid, {})
    pparts = [v.get("tldr_md", ""), v.get("current_episode_md", ""), v.get("prior_admissions_md", "")]
    for h in (v.get("hot_window") or []):
        pparts.append(h.get("paragraph_md") or h.get("text") or "")
    return " || ".join(p for p in lparts if p), " || ".join(p for p in pparts if p)


def b2_merges(pid: int, facts, dates, stages: set[str]):
    """(stage, node, [member texts]) for both b2 stages. Mirrors _project_patient."""
    cands = [f for f in facts if f.certainty != "ruled_out"
             and f.retention_class != "recurrent_episode" and b2._is_candidate(f)]
    by_class: dict[str, list] = {}
    for f in cands:
        by_class.setdefault(f.retention_class, []).append(f)
    out, routed = [], []
    for cls_facts in by_class.values():
        for cluster in b2._cluster(cls_facts):
            for part in b2._split_cluster(cluster):
                node = b2._build_node(part, dates)
                if len(part) >= 2 and "b2_cluster" in stages:
                    out.append(("b2_cluster", node, [x.text for x in part]))
                if node.current_lifecycle == "superseded" and \
                        not tax.is_safety_robust(node.retention_class):
                    continue
                routed.append(node)
    if "b2_consolidate" in stages:
        for c in b2._consolidate(routed):
            fc = set(c.fact_ids)
            mem = [n for n in routed if set(n.fact_ids) and set(n.fact_ids) <= fc]
            if len(mem) >= 2:
                out.append(("b2_consolidate", c, [m.text for m in mem]))
    return out


def b4_merges(pid: int, led: dict, raw: dict):
    """(stage, node-dict, [member texts]) for b4, via f9's fact_ids subset rule."""
    out = []
    r_nodes = raw.get(pid, {}).get("nodes") or []
    for c in led.get(pid, {}).get("nodes") or []:
        fc = set(c.get("fact_ids") or [])
        if not fc:
            continue
        mem = [n for n in r_nodes
               if set(n.get("fact_ids") or []) and set(n.get("fact_ids") or []) <= fc]
        if len(mem) >= 2:
            out.append(("b4", c, [m.get("text", "") for m in mem]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Merge concept-loss triage across b2 and b4.")
    ap.add_argument("--subject", type=int, default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--stage", choices=list(STAGES) + ["all"], default="all")
    ap.add_argument("--all-events", action="store_true",
                    help="keep recovered events in the csv (default: shortlist + CHECK_SOURCE)")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()
    stages = set(STAGES) if args.stage == "all" else {args.stage}

    led = {int(r["subject_id"]): r for r in _read_jsonl(config.OUTPUT_DIR / "retention_ledger.jsonl")}
    viewf = config.OUTPUT_DIR / "longitudinal_view.jsonl"
    view = {int(r["subject_id"]): r for r in _read_jsonl(viewf)} if viewf.exists() else {}
    if not view:
        print("WARNING: longitudinal_view.jsonl missing -- in_prose will be False everywhere, "
              "which INFLATES the shortlist. Run d_coordinator first.")
    rawf = config.OUTPUT_DIR / "retention_ledger.raw.jsonl"
    raw = {int(r["subject_id"]): r for r in _read_jsonl(rawf)} if rawf.exists() else {}
    if "b4" in stages and not raw:
        print("NOTE: retention_ledger.raw.jsonl missing -- skipping the b4 stage.")
        stages.discard("b4")

    by_pid = b2._read_facts()
    dates = b2._hadm_dates()
    df = holdout.established(cohort.load())          # established span = ground truth
    bhc = {int(pid): " || ".join(g["bhc_text"].astype(str)) for pid, g in df.groupby("subject_id")}

    pids = [args.subject] if args.subject is not None else sorted(by_pid)
    if args.limit:
        pids = pids[:args.limit]

    events, n_merges = [], collections.Counter()
    for pid in pids:
        merges = []
        if stages & {"b2_cluster", "b2_consolidate"}:
            merges += b2_merges(pid, by_pid.get(pid, []), dates, stages)
        if "b4" in stages:
            merges += b4_merges(pid, led, raw)
        if not merges:
            continue
        sl, sp = served_text(pid, led, view)
        gt = bhc.get(pid, "")
        for stage, node, members in merges:
            n_merges[stage] += 1
            get = (lambda k: node.get(k, "")) if isinstance(node, dict) else (lambda k: getattr(node, k, ""))
            mtext_merged = (get("text") or "").strip()
            merged_c = concepts_in(mtext_merged)
            for member in members:
                for (dom, name) in sorted(concepts_in(member) - merged_c):
                    rx = RX[name]
                    if negated(member, rx):
                        continue
                    in_l, in_p, in_b = bool(rx.search(sl)), bool(rx.search(sp)), bool(rx.search(gt))
                    if in_l:
                        prio = "recovered_ledger"
                    elif in_p:
                        prio = "recovered_prose"
                    elif in_b:
                        prio = "REVIEW"
                    else:
                        prio = "CHECK_SOURCE"
                    events.append({
                        "priority": prio, "stage": stage, "subject_id": pid,
                        "domain": dom, "concept": name,
                        "split_exempt": get("retention_class") in b2._SPLIT_EXEMPT,
                        "merged_class": get("retention_class"), "merged_tier": get("salience_tier"),
                        "n_members": len(members),
                        "in_ledger": in_l, "in_prose": in_p, "in_bhc": in_b,
                        "merged_text": mtext_merged, "member_text": (member or "").strip(),
                        "verdict": "", "reviewer_note": "",
                    })

    rank = {"REVIEW": 0, "CHECK_SOURCE": 1, "recovered_prose": 2, "recovered_ledger": 3}
    events.sort(key=lambda e: (rank[e["priority"]], e["domain"], e["subject_id"], e["concept"]))
    keep = events if args.all_events else [e for e in events
                                           if e["priority"] in ("REVIEW", "CHECK_SOURCE")]

    args.out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.out_dir / "merge_loss_triage.csv"
    json_path = args.out_dir / "merge_loss_triage_summary.json"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        w.writerows(keep)

    prio = collections.Counter(e["priority"] for e in events)
    shortlist = [e for e in events if e["priority"] == "REVIEW"]
    summary = {
        "tool": "merge_loss_triage",
        "taxonomy_version": tax.TAXONOMY_VERSION,
        "stages": sorted(stages),
        "patients": len(pids),
        "merges_scanned": dict(n_merges),
        "concept_loss_events": len(events),
        "by_priority": dict(prio),
        "shortlist_by_domain": dict(collections.Counter(e["domain"] for e in shortlist)),
        "shortlist_by_stage": dict(collections.Counter(e["stage"] for e in shortlist)),
        "shortlist_subjects": sorted({e["subject_id"] for e in shortlist}),
        "rows_written": len(keep),
        "prose_available": bool(view),
    }
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if not args.json_only:
        print("\n" + "=" * 60)
        print("MERGE CONCEPT-LOSS TRIAGE")
        print(f"  merges scanned     : {dict(n_merges)}  (total {sum(n_merges.values())})")
        print(f"  concept-loss events: {len(events)}")
        for k in ("REVIEW", "CHECK_SOURCE", "recovered_prose", "recovered_ledger"):
            print(f"    {k:18s} {prio.get(k, 0)}")
        if shortlist:
            print(f"  shortlist by domain: {summary['shortlist_by_domain']}")
        print(f"\n  csv  -> {csv_path}   ({len(keep)} rows)")
        print(f"  json -> {json_path}")
        print("\n  A REVIEW row is a candidate, not a defect: follow the patient's later")
        print("  course and read bhc_text before calling any of them a loss.")
        print("  Contains patient text. Keep it out of the repo; never commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
