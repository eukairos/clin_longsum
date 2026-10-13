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
f1_salience_recall.py
=====================
Phase F intrinsic metric #2 - SALIENCE-WEIGHTED RETENTION RECALL.

Deterministic (no LLM), CI/CD-gateable. The salience-aware,
tier-stratified, hard-gated generalisation of c2. c2 answers "was every pinned
fact represented?" with a flat count (recall == 1.0). f1 adds the clinical-
severity dimension the compaction stage demands:

  - every pinned fact is weighted by salience (a dropped Tier-1 SJS is
    catastrophic; a dropped Tier-5 screening negative is minor),
  - recall is reported STRATIFIED by tier and by retention_class,
  - HARD GATES a build can block on: no T1 / T2 / safety-class / significant-
    negative fact may be lost (absent) or collapsed to a frequency rollup.

WEIGHTS use retention_class IDS only (no content detection). The floor classes
are the CLOSEST taxonomy ids to the clinical categories of interest:
  active malignancies   -> chronic_disease_state   (T3; floors ALL chronic disease)
  major organ transplant -> anatomy_altering_surgery (T2; floors all anatomy-altering surgery)
Neither category has a dedicated class id; flooring the closest class is broader
than the category itself (see FLOOR_TOP_CLASSES note). To narrow, edit the tuple.

SCOPE: the COMPACTED (aged-out) span per patient - the only place narrative
compaction can drop a fact. Mirrors c2's covered_hadm_ids exactly and REUSES
c2's settled representation-homing logic (single source of truth: if c2's homing
rule changes, this metric follows automatically - no divergence).

TARGET: the LOSSLESS ledger (raw backup when the served ledger is consolidated),
identical to c2 - b4's T5 prune is a serving reduction, not a loss.

OUTPUTS (machine-readable for CI + human-readable for review):
  data/output/eval/salience_recall.json         gate booleans + stratified scores
  data/output/eval/salience_recall_misses.csv   weighted miss / rollup rows
EXIT CODE: 0 if all hard gates pass, 1 otherwise (CI consumes this).

    cd ~/clin_longsum/src/evaluation
    python f1_salience_recall.py
    python f1_salience_recall.py --subject 90000008
    python f1_salience_recall.py --threshold 0.995
    python f1_salience_recall.py --json-only      # suppress the text report
    python f1_salience_recall.py --ledger-path P --comp-path P --out-dir D   # override input/output paths


"""

from __future__ import annotations

# repo path bootstrap: eval scripts live in src/evaluation/. Add this dir (sibling
# f*) and src/ (flat pipeline modules: config, retention_taxonomy, c1, c2, roles)
# to sys.path so imports resolve regardless of the current working directory.
import sys as _sys
from pathlib import Path as _Path
_evd = _Path(__file__).resolve().parent
_sys.path[:0] = [str(_evd), str(_evd.parent)]

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

import _bootstrap  # noqa: F401  (puts src/ on sys.path)
import config
import retention_taxonomy as tax
import c1_compactor as c1
import c2_retention_validator as c2v


# ── SALIENCE WEIGHTS (retention_class ids only; no content detection) ─────────────
# Base weights asymmetric by clinical consequence of LOSS, mirroring the
# taxonomy's temporal-action ordering (pin > carry_forward > state_track >
# graph_collapse > prune). Self-contained here (not in config.py) so the eval
# module never perturbs the pipeline; promote to config if a stage needs them.
TIER_WEIGHT = {"T1": 5.0, "T2": 4.0, "T3": 3.0, "T4": 2.0, "T5": 1.0}
FLOOR_TOP = TIER_WEIGHT["T1"]      # 5
FLOOR_SIGNEG = TIER_WEIGHT["T3"]   # 3

SWR_THRESHOLD_DEFAULT = 0.995

# Classes floored to FLOOR_TOP. These are the CLOSEST taxonomy ids to the target
# clinical categories - each is BROADER than the category it stands in for:
#   severe_adr_allergy        - the category itself (already T1).
#   chronic_disease_state     - closest id for ACTIVE MALIGNANCY; also floors
#                                COPD/CHF/CKD/AFib/cirrhosis/IBD/epilepsy/bipolar.
#   anatomy_altering_surgery  - closest id for MAJOR ORGAN TRANSPLANT; also
#                                floors splenectomy/bowel resection/amputation.
# No malignancy-only or transplant-only class exists; narrowing requires content
# detection (deliberately not used). Edit this tuple to retune.
FLOOR_TOP_CLASSES = ("severe_adr_allergy", "anatomy_altering_surgery")

# Hard-gate safety classes: loss is catastrophic regardless of tier (mirrors the
# taxonomy's _SAFETY_ROBUST_CLASSES + infection control; retained per review).
SAFETY_CLASSES = ("severe_adr_allergy", "code_status_directive", "infection_control_status")

# Tiers whose loss OR rollup-collapse is a hard CI failure.
GATE_TIERS = ("T1", "T2")

# Representation homes (from c2) that count as full retention.
REPRESENTED_HOMES = ("node", "excluded")

OUT_DIR = config.OUTPUT_DIR / "eval"
OUT_JSON = OUT_DIR / "salience_recall.json"
OUT_CSV = OUT_DIR / "salience_recall_misses.csv"

LEDGER_JSONL = config.OUTPUT_DIR / "retention_ledger.jsonl"
RAW_LEDGER_JSONL = config.OUTPUT_DIR / "retention_ledger.raw.jsonl"


# ── WEIGHT / CRITICALITY ─────────────────────────────────────────────────────────

def salience_weight(f) -> float:
    """Clinical-consequence weight for losing fact f (class-id floors only)."""
    w = TIER_WEIGHT.get(f.salience_tier, 1.0)
    if f.retention_class in FLOOR_TOP_CLASSES:
        w = max(w, FLOOR_TOP)
    if getattr(f, "negative_kind", None) is not None:
        w = max(w, FLOOR_SIGNEG)
    return w


def is_critical(f) -> bool:
    """High-consequence-to-lose: floored-to-top OR T1/T2 OR a negative_kind OR safety
    class. Drives the CSV `critical` flag and the printed violations list. Note
    the hard GATES are narrower (see gates below) - a floored chronic_disease_state
    loss is flagged critical and trips gate_swr, but no per-class gate."""
    return (
        salience_weight(f) >= FLOOR_TOP
        or f.salience_tier in GATE_TIERS
        or getattr(f, "negative_kind", None) is not None
        or f.retention_class in SAFETY_CLASSES
    )


# ── LEDGER SOURCE (mirror c2: validate the lossless raw ledger) ──────────────────

def _resolve_ledger_src():
    primary = c1._read_jsonl(LEDGER_JSONL)
    if primary and primary[0].get("consolidated"):
        if RAW_LEDGER_JSONL.exists():
            return RAW_LEDGER_JSONL, True
        print("WARN: served ledger consolidated but no raw backup; recall may "
              "under-report by the count of pruned T5 nodes.")
    return LEDGER_JSONL, False


# ── ACCUMULATOR ──────────────────────────────────────────────────────────────────

def _blank_stratum() -> dict:
    return {"n": 0, "w": 0.0,
            "n_repr": 0, "w_repr": 0.0,        # node / excluded
            "n_rollup": 0, "w_rollup": 0.0,    # episode-only
            "n_missing": 0, "w_missing": 0.0}  # absent


def _add(stratum: dict, w: float, home) -> None:
    stratum["n"] += 1
    stratum["w"] += w
    if home in REPRESENTED_HOMES:
        stratum["n_repr"] += 1
        stratum["w_repr"] += w
    elif home == "episode":
        stratum["n_rollup"] += 1
        stratum["w_rollup"] += w
    else:
        stratum["n_missing"] += 1
        stratum["w_missing"] += w


def _recall(stratum: dict) -> dict:
    """Strict (node/excluded only) and lenient (incl rollup) recall, weighted +
    unweighted. Empty stratum -> 1.0 (nothing to retain)."""
    n, w = stratum["n"], stratum["w"]
    n_len = stratum["n_repr"] + stratum["n_rollup"]
    w_len = stratum["w_repr"] + stratum["w_rollup"]
    return {
        "n": n,
        "strict_unweighted": round(stratum["n_repr"] / n, 4) if n else 1.0,
        "strict_weighted": round(stratum["w_repr"] / w, 4) if w else 1.0,
        "lenient_unweighted": round(n_len / n, 4) if n else 1.0,
        "lenient_weighted": round(w_len / w, 4) if w else 1.0,
        "n_rollup": stratum["n_rollup"],
        "n_missing": stratum["n_missing"],
    }


# ── MAIN ─────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Salience-weighted retention recall (deterministic).")
    ap.add_argument("--subject", type=int, default=None, help="restrict to one subject_id")
    ap.add_argument("--threshold", type=float, default=SWR_THRESHOLD_DEFAULT,
                    help=f"gate_swr: minimum SWR_lenient (default {SWR_THRESHOLD_DEFAULT} -- an "
                         "FM-4 tolerance, NOT a target; pass 1.0 for zero-genuine-loss)")
    ap.add_argument("--ledger-path", default=None,
                    help="override the ledger file scored (f11 points this at a perturbed "
                         "copy); bypasses the consolidated->raw resolution")
    ap.add_argument("--comp-path", default=None,
                    help="override compaction_results.jsonl (perturbation harness)")
    ap.add_argument("--out-dir", default=None,
                    help="override the eval output dir (sandbox the JSON/CSV; default data/output/eval)")
    ap.add_argument("--json-only", action="store_true", help="suppress the text report")
    args = ap.parse_args()
    config.check_paths()

    global OUT_DIR, OUT_JSON, OUT_CSV
    if args.out_dir:
        OUT_DIR = Path(args.out_dir)
        OUT_JSON = OUT_DIR / "salience_recall.json"
        OUT_CSV = OUT_DIR / "salience_recall_misses.csv"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    comp_path = Path(args.comp_path) if args.comp_path else config.COMPACTION_JSONL

    comp = c1._read_jsonl(comp_path)
    if not comp:
        raise SystemExit(f"{comp_path} not found/empty. Run c1_compactor.py first.")
    if args.subject is not None:
        comp = [c for c in comp if int(c["subject_id"]) == args.subject]

    if args.ledger_path:
        ledger_src, consolidated = Path(args.ledger_path), False
    else:
        ledger_src, consolidated = _resolve_ledger_src()
    if not args.json_only and consolidated:
        print(f"served ledger consolidated; validating raw ledger -> {ledger_src.name}")
    ledgers = {int(l["subject_id"]): l for l in c1._read_jsonl(ledger_src)}
    facts_by_hadm = c1._facts_by_hadm()

    overall = _blank_stratum()
    by_tier: dict[str, dict] = defaultdict(_blank_stratum)
    by_class: dict[str, dict] = defaultdict(_blank_stratum)
    miss_rows: list[dict] = []
    n_patients = 0

    for c in comp:
        pid = int(c["subject_id"])
        covered = c.get("covered_hadm_ids", [])
        led = ledgers.get(pid)
        if led is None:
            if not args.json_only:
                print(f"no ledger for subject_id={pid} -- skipping")
            continue
        n_patients += 1

        # Distinct pinned facts in the compacted span (same dedup as c2).
        seen, source = set(), []
        for h in covered:
            for f in facts_by_hadm.get(h, []):
                if not f.in_ledger:
                    continue
                key = (f.retention_class, f.text.strip().lower())
                if key not in seen:
                    seen.add(key)
                    source.append(f)

        fact_ids, by_cls, excluded, episodes = c2v._ledger_index(led)
        for f in source:
            home = c2v._where_represented(f, fact_ids, by_cls, excluded, episodes)
            w = salience_weight(f)
            _add(overall, w, home)
            _add(by_tier[f.salience_tier], w, home)
            _add(by_class[f.retention_class], w, home)

            if home in REPRESENTED_HOMES:
                continue
            miss_rows.append({
                "issue": "ROLLUP_ONLY" if home == "episode" else "MISSING",
                "subject_id": pid, "salience_tier": f.salience_tier,
                "retention_class": f.retention_class, "certainty": f.certainty,
                "negative_kind": getattr(f, "negative_kind", None),
                "weight": w, "critical": is_critical(f),
                "text": f.text, "source_hadm_id": f.source_hadm_id,
            })

    # ── gates ────────────────────────────────────────────────────────────────────
    def _tier_strict_ok(tier: str) -> bool:
        s = by_tier.get(tier)
        return True if not s or s["n"] == 0 else (s["n_repr"] == s["n"])

    swr_strict = round(overall["w_repr"] / overall["w"], 4) if overall["w"] else 1.0
    swr_lenient = round((overall["w_repr"] + overall["w_rollup"]) / overall["w"], 4) if overall["w"] else 1.0

    gates = {
        "gate_T1": _tier_strict_ok("T1"),
        "gate_T2": _tier_strict_ok("T2"),  # transplant (anatomy_altering_surgery) is gated here
        "gate_safety": not any(r["retention_class"] in SAFETY_CLASSES for r in miss_rows),
        "gate_signeg": not any(r["negative_kind"] is not None for r in miss_rows),
        "gate_swr": swr_lenient >= args.threshold,
    }
    all_pass = all(gates.values())

    critical_violations = [r for r in miss_rows if r["critical"]]
    t3_rollup = [r for r in miss_rows if r["issue"] == "ROLLUP_ONLY" and r["salience_tier"] == "T3"]

    report = {
        "metric": "salience_weighted_recall",
        "taxonomy_version": tax.TAXONOMY_VERSION,
        "span": "compacted",
        "ledger_source": ledger_src.name,
        "threshold_swr_lenient": args.threshold,
        "patients_checked": n_patients,
        "n_pinned_facts": overall["n"],
        "swr_strict_weighted": swr_strict,
        "swr_lenient_weighted": swr_lenient,
        "weights": {"tier": TIER_WEIGHT, "floor_top": FLOOR_TOP,
                    "floor_top_classes": list(FLOOR_TOP_CLASSES), "floor_signeg": FLOOR_SIGNEG},
        "by_tier": {t: _recall(by_tier[t]) for t in sorted(by_tier)},
        "by_class": {k: _recall(by_class[k]) for k in sorted(by_class)},
        "gates": gates,
        "all_gates_pass": all_pass,
        "n_genuine_losses": overall["n_missing"],
        "n_rollup_only": overall["n_rollup"],
        "n_critical_violations": len(critical_violations),
        "n_t3_rollup_mistag_candidates": len(t3_rollup),
    }

    with OUT_JSON.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    if miss_rows:
        cols = ["issue", "subject_id", "salience_tier", "retention_class", "certainty",
                "negative_kind", "weight", "critical", "text", "source_hadm_id"]
        with OUT_CSV.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(miss_rows)

    if not args.json_only:
        print("\n" + "=" * 60)
        print("F1 SALIENCE-WEIGHTED RETENTION RECALL (deterministic)")
        print(f"  patients checked        : {n_patients}")
        print(f"  pinned facts (compacted): {overall['n']}")
        print(f"  SWR strict  (weighted)  : {swr_strict:.4f}   (node/excluded only)")
        print(f"  SWR lenient (weighted)  : {swr_lenient:.4f}   (incl. rollup; c2-comparable)")
        print(f"  genuine losses          : {overall['n_missing']}")
        print(f"  rollup-only             : {overall['n_rollup']}  "
              f"(T3 mis-tag candidates: {len(t3_rollup)})")
        print("  per-tier strict recall (weighted):")
        for t in ("T1", "T2", "T3", "T4", "T5"):
            if by_tier.get(t, {}).get("n"):
                r = _recall(by_tier[t])
                print(f"    {t}: {r['strict_weighted']:.4f}  (n={r['n']}, "
                      f"rollup={r['n_rollup']}, missing={r['n_missing']})")
        print("  gates:")
        for g, ok in gates.items():
            print(f"    {'PASS' if ok else 'FAIL'}  {g}")
        if critical_violations:
            print(f"\n  {len(critical_violations)} CRITICAL VIOLATION(S) "
                  f"(weight>={FLOOR_TOP:.0f} / T1-T2 / safety / sig-neg lost or rollup-collapsed):")
            for v in critical_violations[:20]:
                print(f"    [{v['salience_tier']} {v['retention_class']} w{v['weight']:.0f} "
                      f"{v['issue']}] {v['text']}  (subj {v['subject_id']}, hadm {v['source_hadm_id']})")
            if len(critical_violations) > 20:
                print(f"    ... +{len(critical_violations) - 20} more (see {OUT_CSV.name})")
        print(f"\n  {'ALL GATES PASS' if all_pass else 'GATE FAILURE -- build should stop'}")
        print(f"  json  -> {OUT_JSON}")
        if miss_rows:
            print(f"  misses-> {OUT_CSV}")

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
