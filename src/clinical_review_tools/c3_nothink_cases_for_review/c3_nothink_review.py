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
scratch/c3_nothink_review.py
============================
SUPERSEDED by f10_nothink_audit.py (the productionised Phase-F eval: same review
logic, plus it emits eval/nothink_audit.json + .jsonl and tracks the no-think-pass
RATE as a judge-health metric). Kept as the quick self-contained ad-hoc variant;
prefer f10 for the standing audit.

Ad-hoc diagnostic - NOT part of the pipeline. Paths resolve through config, so the
tool follows the RUNNING namespace (LONGSUM_DATA_ROOT + LONGSUM_NAMESPACE) like every
pipeline stage. Pulls the C3 faithfulness PASSES decided by a DEGRADED (no-think) judge
so they can be human-reviewed against the source BHC notes.

Why: the validator's hang-containment cascade can fall back to think=False. The
review focus depends on WHICH step degraded (read from judge_attempts; attempt>1
= that step ran no-think):
  step4 = ENTAIL  -> the supported/not VERDICTS were rubber-stamped. Verify each
                     accepted statement is genuinely supported by the source.
  step2/3 = classify/filter -> the final statement SELECTION used a weak step.
                     Check no relevant claim was wrongly dropped or kept.
  step1 = extract -> the statement LIST came from a weak extraction (the verdicts
                     were still judged WITH reasoning). Check the source for facts
                     that may have been MISSED, and that the statements are well-formed.

A record is a no-think pass when pass==True and judge_degraded==True.

Default input (override with --path):
  data_<ns>/interim/compaction_faithfulness.jsonl
Source BHC for --source (override with --cohort):
  data_<ns>/interim/cohort_admissions.csv         (hadm_id -> bhc_delta/bhc_text)
Writes nothing to the pipeline; --dump writes the filtered records under
data_<ns>/output/review_tools/ (patient-derived -- kept out of the repo).

    cd src/clinical_review_tools/c3_nothink_cases_for_review
    python c3_nothink_review.py
    python c3_nothink_review.py --source            # + the covered BHC notes
    python c3_nothink_review.py --subjects 90000034 --source
    python c3_nothink_review.py --no-evidence       # statements only, terser
"""

import argparse
import csv
import json
import sys
from pathlib import Path

# config lives in src/ (this tool is 2 dirs under src/).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config

csv.field_size_limit(10_000_000)        # bhc_text fields can be large

DEFAULT_IN = config.INTERIM_DIR / "compaction_faithfulness.jsonl"
DEFAULT_COHORT = config.INTERIM_DIR / "cohort_admissions.csv"
DUMP_OUT = config.OUTPUT_DIR / "review_tools" / "nothink_passes_review.jsonl"

_STEP_LABEL = {"step1": "extract", "step2": "classify", "step3": "filter", "step4": "ENTAIL"}


def _read_jsonl(path):
    if not path.exists():
        raise SystemExit(f"{path} not found. Run c3_faithfulness.py first, or pass --path.")
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _degraded_steps(rec):
    """Which steps ran no-think (attempt > 1), most-critical first."""
    att = rec.get("judge_attempts") or {}
    out = []
    for step in ("step4", "step1", "step2", "step3"):
        a = att.get(step)
        if isinstance(a, int) and a > 1:
            out.append(f"{_STEP_LABEL[step]}(a{a})")
    return out


def _review_focus(steps):
    """One-line guidance for what to scrutinize, by the most-severe degraded step."""
    names = " ".join(steps)
    if "ENTAIL" in names:
        return ("ENTAIL no-think -> the supported/not VERDICTS were rubber-stamped. "
                "Verify EACH accepted statement is genuinely supported by the source.")
    if "classify" in names or "filter" in names:
        return ("classify/filter no-think -> the final statement SELECTION used a weak step. "
                "Check no relevant claim was wrongly dropped or kept.")
    if "extract" in names:
        return ("extract no-think -> the statement LIST came from a weak extraction "
                "(verdicts were judged WITH reasoning). Check the source for facts the "
                "extraction may have MISSED, and that the statements are well-formed.")
    return "no-think step (unspecified) -- review the accepted statements against the source."


def _load_source(cohort_path):
    """hadm_id -> source BHC text (bhc_delta preferred, else bhc_text)."""
    src = {}
    if not cohort_path.exists():
        print(f"  (--source: {cohort_path} not found; skipping BHC text)")
        return src
    with cohort_path.open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                h = int(r["hadm_id"])
            except (TypeError, ValueError, KeyError):
                continue
            src[h] = (r.get("bhc_delta") or r.get("bhc_text") or "").strip()
    return src


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", type=Path, default=DEFAULT_IN,
                    help=f"compaction_faithfulness.jsonl (default: {DEFAULT_IN})")
    ap.add_argument("--cohort", type=Path, default=DEFAULT_COHORT,
                    help=f"cohort_admissions.csv for --source (default: {DEFAULT_COHORT})")
    ap.add_argument("--subjects", type=int, nargs="*", default=[],
                    help="restrict to these subject_id(s)")
    ap.add_argument("--source", action="store_true",
                    help="print the covered admissions' BHC text to check statements against")
    ap.add_argument("--no-evidence", dest="evidence", action="store_false",
                    help="omit the judge's per-statement evidence (terser)")
    ap.add_argument("--dump", action="store_true",
                    help=f"write the filtered records to {DUMP_OUT}")
    args = ap.parse_args()

    print(f"namespace: {config.RUN_NAMESPACE or '(default)'}  |  data: {config.DATA_DIR}")
    recs = _read_jsonl(args.path)
    targets = set(args.subjects or [])
    if targets:
        recs = [r for r in recs if r.get("subject_id") in targets]

    passes = [r for r in recs if r.get("pass") is True]
    nothink = [r for r in passes if r.get("judge_degraded") is True]
    unknown = [r for r in passes if r.get("judge_degraded") is None]   # pre-instrumentation
    step4_nothink = [r for r in nothink
                     if isinstance((r.get("judge_attempts") or {}).get("step4"), int)
                     and r["judge_attempts"]["step4"] > 1]
    nothink.sort(key=lambda r: (r.get("faithfulness_score") or 0.0))   # worst first

    src = _load_source(args.cohort) if args.source else {}

    print("=" * 70)
    print("C3 NO-THINK PASS REVIEW  (degraded/weaker judge -> human-review)")
    print(f"  total passes        : {len(passes)}")
    print(f"  no-think passes     : {len(nothink)}   (step-4 ENTAIL degraded: {len(step4_nothink)})")
    if unknown:
        print(f"  pre-instrumentation : {len(unknown)}  (no judge_degraded field; re-run to classify)")
    print("=" * 70)

    if not nothink:
        print("\nNo no-think passes to review. Nothing flagged.")
        return

    for r in nothink:
        sid = r.get("subject_id")
        hadms = r.get("covered_hadm_ids", [])
        score = r.get("faithfulness_score")
        ns, nt = r.get("n_supported"), r.get("n_total_statements")
        steps = _degraded_steps(r)
        ent = r.get("entailment_detail", [])
        accepted = [e for e in ent if e.get("supported")]
        rejected = [e for e in ent if not e.get("supported")]

        print(f"\nsubject={sid}  hadm={hadms}")
        print(f"  score={score:.3f}  supported={ns}/{nt}  degraded={', '.join(steps) or 'unknown'}")
        print(f"  REVIEW FOCUS: {_review_focus(steps)}")

        print(f"  ACCEPTED ({len(accepted)}) -- the weak judge let these through:")
        for e in accepted:
            stmt = (e.get("statement", "") or "").strip()
            print(f"    [+] {stmt}")
            if args.evidence:
                ev = (e.get("evidence", "") or e.get("note", "") or "").strip()
                if ev:
                    print(f"        evidence: {ev}")
        if rejected:
            print(f"  NOT SUPPORTED ({len(rejected)}) -- tolerated within a PASS, but verify:")
            for e in rejected:
                stmt = (e.get("statement", "") or "").strip()
                print(f"    [-] {stmt}")

        if args.source:
            print(f"  SOURCE BHC (covered admissions -- check the accepted statements against this):")
            any_txt = False
            for h in hadms:
                txt = src.get(h, "")
                if txt:
                    any_txt = True
                    print(f"    --- hadm {h} ---")
                    for line in txt.splitlines():
                        print(f"      {line}")
            if not any_txt:
                print("    (no source text found for the covered hadm_ids in the cohort CSV)")

    print(f"\nTo re-judge any with thinking: cd .. && python c3_faithfulness.py --subjects "
          f"{' '.join(str(r.get('subject_id')) for r in nothink)}")
    print("(a fresh run may land thinking again; a persistent no-think = genuinely hard case.)")

    if args.dump:
        out = DUMP_OUT
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(json.dumps(r) for r in nothink) + "\n", encoding="utf-8")
        print(f"\nFiltered records -> {out}")


if __name__ == "__main__":
    main()
