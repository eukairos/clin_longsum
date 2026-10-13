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
weight_sensitivity.py  --  quasi-calibration of the f1 salience tier weights
=============================================================================
STUDY (one-off, not a standing gate). f1_salience_recall.py scores retention with
TIER_WEIGHT = {T1:5, T2:4, T3:3, T4:2, T5:1} -- an ASSERTED linear ramp, never
calibrated. This study asks the only question that matters operationally:

    DOES THE CHOICE OF WEIGHTS CHANGE ANY DECISION?

If the cohort SWR and the per-subject ranking are stable across a flat, a linear,
a geometric and a NOHARM-shaped superlinear scheme, then "the weights are
arbitrary" stops being a vulnerability and becomes a documented insensitivity.
That is the NOHARM criterion (Nature 2026, arXiv:2512.01241): their 1:8:24
mild:moderate:severe multipliers were selected by grid search optimising for
RANKING STABILITY, not by clinical fiat, and they report that the severity cap
reorders models only marginally.

STRUCTURAL FACT THAT FRAMES THE RESULT
--------------------------------------
In f1, the hard gates are WEIGHT-INDEPENDENT:
    gate_T1 / gate_T2     count-based (n_repr == n)
    gate_safety           membership test over miss_rows
    gate_signeg           membership test over miss_rows
    gate_swr              the ONLY weight-dependent gate
So the tier weights move exactly four things: swr_strict_weighted,
swr_lenient_weighted, the per-tier/per-class *_weighted figures, and the CSV
`critical` flag (via FLOOR_TOP inside is_critical()). Catastrophe detection is
already carried by the four count-based gates. The weighted scalar is therefore a
REPORTING AGGREGATE, not a harm model - which is the defensible reading of the
earlier finding that a quadratic (T1 = 16x T5) ramp let T1 swamp the aggregate.

DEGENERACY WARNING (read before believing a null result)
--------------------------------------------------------
At SWR ~0.996 the aggregate is near-saturated. If the misses are few, or sit in a
single tier, every scheme returns near-identical numbers and the invariance is
ARITHMETIC, NOT EVIDENCE. This script prints the weight-invariant by-tier miss
profile first and refuses to claim stability when the profile is degenerate. When
it is, re-run against f11's perturbed ledgers, which inject known losses across
tiers by construction:

    python studies/weight_sensitivity/weight_sensitivity.py \
        --ledger-path <f11 perturbed ledger.jsonl>

IMPLEMENTATION GOTCHA
---------------------
f1 binds FLOOR_TOP and FLOOR_SIGNEG at IMPORT time from TIER_WEIGHT. Patching
TIER_WEIGHT alone leaves them stale and silently wrong. All three are set together
in _apply(). salience_weight() reads module globals at call time, so no edit to
f1 itself is needed - this study is strictly read-only with respect to the
pipeline and the standing eval.

OUTPUT: <data root>/output/studies/weight_sensitivity/weight_sensitivity.json
        (+ FINDINGS.md, + _runs/<scheme>/ sandboxed f1 artefacts).
  Outputs go to the DATA tree, NEVER the repo: they are derived from credentialed
  patient data and the repo is not a DUA-compliant location. Namespace-aware via
  config. The canonical data/output/eval/salience_recall.json is NOT touched --
  every f1 invocation is sandboxed via --out-dir.

    cd ~/clin_longsum/src
    python studies/weight_sensitivity/weight_sensitivity.py
    python studies/weight_sensitivity/weight_sensitivity.py --subjects 90000029 90000055 90000008
    python studies/weight_sensitivity/weight_sensitivity.py --all-subjects
    python studies/weight_sensitivity/weight_sensitivity.py --ledger-path <perturbed>.jsonl
    python studies/weight_sensitivity/weight_sensitivity.py --comp-path <compaction_results>.jsonl   # forwarded to f1
    python studies/weight_sensitivity/weight_sensitivity.py --ledger-path <perturbed>.jsonl --tag perturbed_a
                                                                       # sandbox subfolder under _runs/ (default: cohort)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_SRC))                 # src/ on the path
sys.path.insert(0, str(_SRC / "evaluation"))  # f1_salience_recall lives in src/evaluation/

import config
import c1_compactor as c1
import f1_salience_recall as f1

OUT_DIR = config.OUTPUT_DIR / "studies" / "weight_sensitivity"
OUT_JSON = OUT_DIR / "weight_sensitivity.json"
OUT_MD = OUT_DIR / "FINDINGS.md"
RUNS_DIR = OUT_DIR / "_runs"

BASELINE = "linear_5_4_3_2_1"

# Four shapes spanning the plausible space. FLAT is the control that isolates how
# much the weighting does AT ALL; GEOMETRIC is the previously-rejected quadratic
# (T1 = 16x T5); NOHARM_SHAPED interpolates 1:8:24 onto five tiers.
SCHEMES: dict[str, dict[str, float]] = {
    "flat_1_1_1_1_1":       {"T1": 1.0,  "T2": 1.0,  "T3": 1.0, "T4": 1.0, "T5": 1.0},
    BASELINE:               {"T1": 5.0,  "T2": 4.0,  "T3": 3.0, "T4": 2.0, "T5": 1.0},
    "geometric_16_8_4_2_1": {"T1": 16.0, "T2": 8.0,  "T3": 4.0, "T4": 2.0, "T5": 1.0},
    "noharm_24_15_8_3_1":   {"T1": 24.0, "T2": 15.0, "T3": 8.0, "T4": 3.0, "T5": 1.0},
}

TIERS = ("T1", "T2", "T3", "T4", "T5")

# Stability thresholds. Spread is in SWR points; rho is Spearman on per-subject
# SWR_lenient. Both are reporting aids 
SPREAD_STABLE = 0.005     # <= 0.5 SWR points across all schemes
RHO_STABLE = 0.95


# -- f1 driver -------------------------------------------------------------------

def _apply(weights: dict[str, float]) -> None:
    """Patch f1's three coupled weight globals together. Patching TIER_WEIGHT alone
    leaves FLOOR_TOP / FLOOR_SIGNEG stale (they are bound at import)."""
    f1.TIER_WEIGHT = dict(weights)
    f1.FLOOR_TOP = weights["T1"]
    f1.FLOOR_SIGNEG = weights["T3"]


def _run(scheme: str, weights: dict[str, float], extra: list[str], tag: str) -> dict:
    """Invoke f1.main() under a weight scheme, sandboxed to its own out-dir."""
    _apply(weights)
    out = RUNS_DIR / tag / scheme
    argv = ["f1_salience_recall.py", "--json-only", "--out-dir", str(out)] + extra
    saved, sys.argv = sys.argv, argv
    try:
        f1.main()          # return code is the gate verdict; gates are weight-independent
    finally:
        sys.argv = saved
    return json.loads((out / "salience_recall.json").read_text(encoding="utf-8"))


# -- rank statistics (stdlib only; no scipy dependency) --------------------------

def _ranks(vals: list[float]) -> list[float]:
    """Average ranks, ties shared. Ties are the norm here (many subjects at 1.0)."""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    out = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        shared = (i + j) / 2.0
        for k in range(i, j + 1):
            out[order[k]] = shared
        i = j + 1
    return out


def _spearman(a: list[float], b: list[float]) -> float | None:
    """None when either series is constant (rho undefined, not 1.0)."""
    if len(a) < 2:
        return None
    ra, rb = _ranks(a), _ranks(b)
    n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    da = sum((x - ma) ** 2 for x in ra) ** 0.5
    db = sum((y - mb) ** 2 for y in rb) ** 0.5
    if da == 0 or db == 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(ra, rb)) / (da * db)


# -- degeneracy ------------------------------------------------------------------

def _miss_profile(report: dict) -> dict:
    """Weight-invariant: which tiers actually contain losses/rollups to weight."""
    prof = {}
    for t in TIERS:
        r = report.get("by_tier", {}).get(t)
        if r:
            prof[t] = {"n": r["n"], "rollup": r["n_rollup"], "missing": r["n_missing"]}
    tiers_with_loss = [t for t, v in prof.items() if v["rollup"] or v["missing"]]
    total_loss = report["n_genuine_losses"] + report["n_rollup_only"]
    return {
        "by_tier": prof,
        "tiers_with_loss": tiers_with_loss,
        "total_loss_events": total_loss,
        "degenerate": len(tiers_with_loss) <= 1 or total_loss < 5,
    }


def _subject_ids(extra: list[str]) -> list[int]:
    comp = c1._read_jsonl(Path(extra[extra.index("--comp-path") + 1])
                          if "--comp-path" in extra else config.COMPACTION_JSONL)
    return sorted({int(c["subject_id"]) for c in comp})


# -- main ------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Tier-weight sensitivity / quasi-calibration for f1 (deterministic).")
    ap.add_argument("--subjects", type=int, nargs="+", default=None,
                    help="per-subject rank-stability check over these subject_ids")
    ap.add_argument("--all-subjects", action="store_true",
                    help="per-subject check over every compacted patient")
    ap.add_argument("--ledger-path", default=None,
                    help="forwarded to f1; point at an f11 perturbed ledger to escape "
                         "the near-saturated cohort and get a non-degenerate test")
    ap.add_argument("--comp-path", default=None, help="forwarded to f1")
    ap.add_argument("--tag", default="cohort",
                    help="sandbox subfolder under _runs/ (label a perturbation run)")
    args = ap.parse_args()
    config.check_paths()

    extra: list[str] = []
    if args.ledger_path:
        extra += ["--ledger-path", args.ledger_path]
    if args.comp_path:
        extra += ["--comp-path", args.comp_path]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    # -- cohort sweep -------------------------------------------------------------
    cohort = {name: _run(name, w, extra, args.tag) for name, w in SCHEMES.items()}
    base = cohort[BASELINE]
    prof = _miss_profile(base)

    print("\n" + "=" * 72)
    print("WEIGHT SENSITIVITY -- f1 salience tier weights")
    print(f"  ledger source           : {base['ledger_source']}")
    print(f"  patients checked        : {base['patients_checked']}")
    print(f"  pinned facts (compacted): {base['n_pinned_facts']}")

    print("\n  miss profile (WEIGHT-INVARIANT -- this decides if the sweep can prove anything):")
    for t, v in prof["by_tier"].items():
        print(f"    {t}: n={v['n']:<5} rollup={v['rollup']:<4} missing={v['missing']}")
    print(f"    loss events: {prof['total_loss_events']}  "
          f"tiers with loss: {', '.join(prof['tiers_with_loss']) or 'NONE'}")
    if prof["degenerate"]:
        print("    *** DEGENERATE: losses absent or confined to one tier. Any invariance")
        print("    *** below is ARITHMETIC, NOT EVIDENCE. Re-run with --ledger-path")
        print("    *** pointed at an f11 perturbed ledger before claiming stability.")

    print(f"\n  {'scheme':<24} {'SWR_strict':>11} {'SWR_lenient':>12} {'n_crit':>7} {'gates':>7}")
    for name in SCHEMES:
        r = cohort[name]
        print(f"  {name:<24} {r['swr_strict_weighted']:>11.4f} "
              f"{r['swr_lenient_weighted']:>12.4f} {r['n_critical_violations']:>7} "
              f"{'PASS' if r['all_gates_pass'] else 'FAIL':>7}")

    lenient = [cohort[n]["swr_lenient_weighted"] for n in SCHEMES]
    strict = [cohort[n]["swr_strict_weighted"] for n in SCHEMES]
    spread_lenient = max(lenient) - min(lenient)
    spread_strict = max(strict) - min(strict)
    print(f"\n  SWR spread across schemes: strict {spread_strict:.4f}  "
          f"lenient {spread_lenient:.4f}")

    gate_sets = {tuple(sorted(cohort[n]["gates"].items())) for n in SCHEMES}
    gates_identical = len(gate_sets) == 1
    print(f"  gate verdicts identical across schemes: {gates_identical}"
          f"{'' if gates_identical else '  <-- gate_swr flipped; inspect'}")

    # -- per-subject rank stability ----------------------------------------------
    per_subject, rho = {}, {}
    subjects = args.subjects
    if args.all_subjects and not subjects:
        subjects = _subject_ids(extra)
    if subjects:
        print(f"\n  per-subject sweep over {len(subjects)} subject(s)...")
        for name, w in SCHEMES.items():
            per_subject[name] = [
                _run(name, w, extra + ["--subject", str(s)], f"{args.tag}_subj{s}")
                ["swr_lenient_weighted"] for s in subjects
            ]
        print(f"  rank stability vs {BASELINE} (Spearman on SWR_lenient):")
        for name in SCHEMES:
            r = _spearman(per_subject[BASELINE], per_subject[name])
            rho[name] = r
            print(f"    {name:<24} {'undefined (constant series)' if r is None else f'{r:>6.3f}'}")

    # -- verdict ------------------------------------------------------------------
    defined_rho = [v for k, v in rho.items() if v is not None and k != BASELINE]
    stable = (
        not prof["degenerate"]
        and gates_identical
        and spread_lenient <= SPREAD_STABLE
        and (not defined_rho or min(defined_rho) >= RHO_STABLE)
    )
    verdict = ("STABLE -- weight choice does not change the reported picture; the linear "
               "ramp is a defensible reporting aggregate and can be documented as such."
               if stable else
               "NOT ESTABLISHED -- see the miss profile and spread above before claiming "
               "insensitivity in the README.")
    print(f"\n  VERDICT: {verdict}")

    report = {
        "study": "weight_sensitivity",
        "taxonomy_version": base["taxonomy_version"],
        "tag": args.tag,
        "ledger_source": base["ledger_source"],
        "patients_checked": base["patients_checked"],
        "n_pinned_facts": base["n_pinned_facts"],
        "baseline_scheme": BASELINE,
        "schemes": SCHEMES,
        "miss_profile": prof,
        "cohort": {n: {"swr_strict_weighted": cohort[n]["swr_strict_weighted"],
                       "swr_lenient_weighted": cohort[n]["swr_lenient_weighted"],
                       "n_critical_violations": cohort[n]["n_critical_violations"],
                       "gates": cohort[n]["gates"],
                       "all_gates_pass": cohort[n]["all_gates_pass"]} for n in SCHEMES},
        "spread_swr_strict": round(spread_strict, 4),
        "spread_swr_lenient": round(spread_lenient, 4),
        "gates_identical_across_schemes": gates_identical,
        "subjects": subjects or [],
        "per_subject_swr_lenient": per_subject,
        "spearman_vs_baseline": rho,
        "thresholds": {"spread_stable": SPREAD_STABLE, "rho_stable": RHO_STABLE},
        "stable": stable,
        "verdict": verdict,
    }
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")

    md = [
        "# Weight sensitivity -- f1 salience tier weights", "",
        f"Taxonomy `{base['taxonomy_version']}` | ledger `{base['ledger_source']}` | "
        f"{base['patients_checked']} patients | {base['n_pinned_facts']} pinned facts | tag `{args.tag}`", "",
        "## Miss profile (weight-invariant)", "",
        "| tier | n | rollup | missing |", "| --- | --- | --- | --- |",
    ]
    md += [f"| {t} | {v['n']} | {v['rollup']} | {v['missing']} |" for t, v in prof["by_tier"].items()]
    md += ["", f"Loss events: {prof['total_loss_events']}; tiers with loss: "
               f"{', '.join(prof['tiers_with_loss']) or 'none'}.", ""]
    if prof["degenerate"]:
        md += ["> **Degenerate.** Losses are absent or confined to a single tier, so any "
               "invariance below is arithmetic rather than evidence. Re-run against an f11 "
               "perturbed ledger before citing this as calibration.", ""]
    md += ["## Cohort SWR by scheme", "",
           "| scheme | T1:T2:T3:T4:T5 | SWR strict | SWR lenient | critical | gates |",
           "| --- | --- | --- | --- | --- | --- |"]
    for n in SCHEMES:
        w = SCHEMES[n]
        ratio = ":".join(f"{w[t]:g}" for t in TIERS)
        r = cohort[n]
        md.append(f"| `{n}` | {ratio} | {r['swr_strict_weighted']:.4f} | "
                  f"{r['swr_lenient_weighted']:.4f} | {r['n_critical_violations']} | "
                  f"{'PASS' if r['all_gates_pass'] else 'FAIL'} |")
    md += ["", f"Spread: strict {spread_strict:.4f}, lenient {spread_lenient:.4f}. "
               f"Gate verdicts identical across schemes: {gates_identical}.", ""]
    if rho:
        md += ["## Per-subject rank stability", "",
               f"Spearman on SWR_lenient vs `{BASELINE}`, n={len(subjects)} subjects.", "",
               "| scheme | rho |", "| --- | --- |"]
        md += [f"| `{n}` | {'undefined' if rho[n] is None else f'{rho[n]:.3f}'} |" for n in SCHEMES]
        md += [""]
    md += ["## Verdict", "", verdict, "",
           "Note: f1's hard gates (`gate_T1`, `gate_T2`, `gate_safety`, `gate_signeg`) are "
           "count-based and weight-independent. Only `gate_swr` depends on the tier weights. "
           "Catastrophe detection therefore does not rest on the weighted scalar.", ""]
    OUT_MD.write_text("\n".join(md), encoding="utf-8")

    print(f"  json  -> {OUT_JSON}")
    print(f"  md    -> {OUT_MD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
