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
f10_nothink_audit.py
====================
Phase F Tier-1 (track) + Tier-2 (human review) JUDGE-HEALTH audit of the C3
faithfulness gate. Deterministic (no LLM, no GPU): reads c3's output and surfaces
the faithfulness PASSES that were decided by a DEGRADED (no-think) deepseek judge,
so a clinician-MLOps specialist reviews them against source instead of trusting them.

Why this is an eval, not a one-off: the C3 validator's hang-containment cascade can
fall back to think=False, and a no-think judge rubber-stamps supported:true more
readily (f7/VeriFact calibration: Not-Supported PPV ~0.24 -> automated gating on the
judge is inappropriate; human-reviewed triage is). The RATE of no-think passes is a
judge-health signal (rising rate = the judge is degrading / the cohort is getting
harder), and each no-think PASS is a human-review item. This is the standing audit
for both.

NOT a hard gate. Tier-1 semantics: track + alert on a band breach, never block the
build (a no-think pass is a REVIEW flag, not a failure). `--strict` opts into a
non-zero exit when the step-4 no-think rate exceeds the band (for CI alerting).

Review focus depends on WHICH step degraded (judge_attempts; attempt>1 = no-think):
  step4 = ENTAIL  -> the supported/not VERDICTS were rubber-stamped. Verify each
                     accepted statement is genuinely supported by the source. (worst)
  step2/3 = classify/filter -> the final statement SELECTION used a weak step.
  step1 = extract -> the statement LIST came from a weak extraction (verdicts were
                     judged WITH reasoning); check the source for MISSED facts.

OUTPUTS:
  data/output/eval/nothink_audit.json     aggregate + rates + band verdict
  data/output/eval/nothink_audit.jsonl    the human-review worklist (one row per
                                           no-think PASS: focus + accepted/rejected)
EXIT CODE: 0 (track). With --strict: 1 if the step-4 no-think rate exceeds the band.

cd ~/clin_longsum/src/evaluation
    python f10_nothink_audit.py
    python f10_nothink_audit.py --source            # + the covered BHC notes (console)
    python f10_nothink_audit.py --subject 90000034 --source
    python f10_nothink_audit.py --max-step4-rate 0.05 --strict   # CI alert mode
    python f10_nothink_audit.py --json-only

The fold-in DOES re-run c3/c4 for the folded subject, so --subject here reports that subject's CURRENT c3 record; 
this is a build-time / periodic audit, not a run-time gate.)
"""

from __future__ import annotations

import argparse
import csv
import json
import sys

import _bootstrap  # noqa: F401  (puts src/ on sys.path)
import config

OUT_DIR = config.OUTPUT_DIR / "eval"
OUT_JSON = OUT_DIR / "nothink_audit.json"
OUT_JSONL = OUT_DIR / "nothink_audit.jsonl"

FAITH_JSONL = config.INTERIM_DIR / "compaction_faithfulness.jsonl"
COHORT_CSV = config.INTERIM_DIR / "cohort_admissions.csv"

# Default advisory band on the step-4 (ENTAIL) no-think PASS rate - the dangerous
# kind, where the verdicts themselves were rubber-stamped. Track-only; tune empirically.
DEFAULT_MAX_STEP4_RATE = 0.05

_STEP_LABEL = {"step1": "extract", "step2": "classify", "step3": "filter", "step4": "ENTAIL"}

csv.field_size_limit(10_000_000)   # bhc_text fields can be large


def _read_jsonl(path):
    if not path.exists():
        raise SystemExit(f"{path} not found. Run c3_faithfulness.py first.")
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _degraded_steps(rec) -> list[str]:
    """Which steps ran no-think (attempt > 1), most-critical first."""
    att = rec.get("judge_attempts") or {}
    out = []
    for step in ("step4", "step1", "step2", "step3"):
        a = att.get(step)
        if isinstance(a, int) and a > 1:
            out.append(f"{_STEP_LABEL[step]}(a{a})")
    return out


def _step4_degraded(rec) -> bool:
    a = (rec.get("judge_attempts") or {}).get("step4")
    return isinstance(a, int) and a > 1


def _review_focus(steps: list[str]) -> str:
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
    return "no-think step (unspecified) - review the accepted statements against the source."


def _load_source(path):
    src = {}
    if not path.exists():
        return src
    with path.open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                h = int(r["hadm_id"])
            except (TypeError, ValueError, KeyError):
                continue
            src[h] = (r.get("bhc_delta") or r.get("bhc_text") or "").strip()
    return src


def _worklist_record(rec) -> dict:
    ent = rec.get("entailment_detail", []) or []
    accepted = [{"statement": (e.get("statement", "") or "").strip(),
                 "evidence": (e.get("evidence", "") or e.get("note", "") or "").strip()}
                for e in ent if e.get("supported")]
    not_supported = [{"statement": (e.get("statement", "") or "").strip()}
                     for e in ent if not e.get("supported")]
    steps = _degraded_steps(rec)
    return {
        "subject_id": rec.get("subject_id"),
        "covered_hadm_ids": rec.get("covered_hadm_ids", []),
        "faithfulness_score": rec.get("faithfulness_score"),
        "n_supported": rec.get("n_supported"),
        "n_total_statements": rec.get("n_total_statements"),
        "degraded_steps": steps,
        "step4_degraded": _step4_degraded(rec),
        "review_focus": _review_focus(steps),
        "accepted": accepted,
        "not_supported": not_supported,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Judge-health audit of C3 no-think faithfulness passes (track).")
    ap.add_argument("--subject", type=int, default=None, help="restrict to one subject_id")
    ap.add_argument("--source", action="store_true",
                    help="print the covered admissions' BHC text to check against (console)")
    ap.add_argument("--max-step4-rate", type=float, default=DEFAULT_MAX_STEP4_RATE,
                    help="advisory band on the step-4 (ENTAIL) no-think PASS rate")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 if the step-4 no-think rate exceeds the band (CI alert)")
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()
    config.check_paths()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    recs = _read_jsonl(FAITH_JSONL)
    if args.subject is not None:
        recs = [r for r in recs if r.get("subject_id") == args.subject]

    passes = [r for r in recs if r.get("pass") is True]
    nothink = [r for r in passes if r.get("judge_degraded") is True]
    pre_instr = [r for r in passes if r.get("judge_degraded") is None]   # pre-instrumentation
    step4 = [r for r in nothink if _step4_degraded(r)]
    nothink.sort(key=lambda r: (r.get("faithfulness_score") or 0.0))     # worst first

    n_pass = len(passes)
    nothink_rate = (len(nothink) / n_pass) if n_pass else 0.0
    step4_rate = (len(step4) / n_pass) if n_pass else 0.0
    within_band = step4_rate <= args.max_step4_rate
    verdict = ("ok" if not nothink else ("review" if within_band else "alert"))

    worklist = [_worklist_record(r) for r in nothink]

    report = {
        "metric": "c3_nothink_pass_audit",
        "tier": "1 (track) + 2 (human review)",
        "input": FAITH_JSONL.name,
        "n_passes": n_pass,
        "n_nothink_passes": len(nothink),
        "n_step4_nothink_passes": len(step4),
        "n_pre_instrumentation": len(pre_instr),
        "nothink_pass_rate": round(nothink_rate, 4),
        "step4_nothink_pass_rate": round(step4_rate, 4),
        "max_step4_rate": args.max_step4_rate,
        "within_band": within_band,
        "verdict": verdict,
        "worklist": OUT_JSONL.name,
        "subject_filter": args.subject,
    }

    with OUT_JSON.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    with OUT_JSONL.open("w", encoding="utf-8") as fh:
        for w in worklist:
            fh.write(json.dumps(w) + "\n")

    if not args.json_only:
        src = _load_source(COHORT_CSV) if args.source else {}
        print("\n" + "=" * 70)
        print("F10 C3 NO-THINK PASS AUDIT  (Tier 1 track / Tier 2 human review)")
        print(f"  passes                 : {n_pass}")
        print(f"  no-think passes        : {len(nothink)}  (rate {nothink_rate:.3f})")
        print(f"  step-4 ENTAIL no-think : {len(step4)}  (rate {step4_rate:.3f}, "
              f"band <= {args.max_step4_rate})")
        if pre_instr:
            print(f"  pre-instrumentation    : {len(pre_instr)}  (no judge_degraded field; re-run c3)")
        print(f"  within band            : {within_band}")
        print(f"  VERDICT                : {verdict.upper()}"
              + ("" if within_band else "  -- step-4 no-think rate over band; review the judge"))
        print("=" * 70)

        for w in worklist:
            print(f"\nsubject={w['subject_id']}  hadm={w['covered_hadm_ids']}")
            sc = w["faithfulness_score"]
            sc_s = f"{sc:.3f}" if isinstance(sc, (int, float)) else str(sc)
            print(f"  score={sc_s}  supported={w['n_supported']}/{w['n_total_statements']}  "
                  f"degraded={', '.join(w['degraded_steps']) or 'unknown'}")
            print(f"  REVIEW FOCUS: {w['review_focus']}")
            print(f"  ACCEPTED ({len(w['accepted'])}):")
            for a in w["accepted"]:
                print(f"    [+] {a['statement']}")
                if a["evidence"]:
                    print(f"        evidence: {a['evidence']}")
            if w["not_supported"]:
                print(f"  NOT SUPPORTED ({len(w['not_supported'])}) -- tolerated within a PASS, verify:")
                for ns in w["not_supported"]:
                    print(f"    [-] {ns['statement']}")
            if args.source:
                print("  SOURCE BHC (check the accepted statements against this):")
                any_txt = False
                for h in w["covered_hadm_ids"]:
                    txt = src.get(h, "")
                    if txt:
                        any_txt = True
                        print(f"    --- hadm {h} ---")
                        for line in txt.splitlines():
                            print(f"      {line}")
                if not any_txt:
                    print("    (no source text for the covered hadm_ids in the cohort CSV)")

        print(f"\n  json     -> {OUT_JSON}")
        print(f"  worklist -> {OUT_JSONL}")
        if nothink:
            print(f"  re-judge with thinking: python c3_faithfulness.py --subjects "
                  f"{' '.join(str(w['subject_id']) for w in worklist)}")

    return 1 if (args.strict and not within_band) else 0


if __name__ == "__main__":
    sys.exit(main())
