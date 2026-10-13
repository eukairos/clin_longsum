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
f12_frontier.py
===============
Phase F CHARACTERISATION metric - COMPRESSION-RETENTION FRONTIER + operating-point gate.

Publishes "here is our operating point and it degrades gracefully," and gates regressions on that point.

x = median compression ratio (chars(prior_admissions_md) / chars(covered bhc_delta), f3-style)
y = cohort salience-weighted recall, lenient (f1 weighting + c2 homing)

TWO MODES:
  default (GATE, deterministic, NO GPU): scores the EXISTING production compaction
    (compaction_results.jsonl) at the production N_COMPACT_THRESHOLD and evaluates the
    operating-point gate. Two parts :
      (a) FLOOR        -- SWR_lenient at the production compression >= the floor. The floor
                          is PINNED to f1's measured recall (`swr_lenient_weighted` in
                          salience_recall.json), not to a literal 1.0: f1 and f12 compute
                          the same quantity over the same ledger, so the honest bar is
                          "f12 must not report worse retention than f1 just measured".
                          A hardcoded 1.0 can never pass while any genuine loss exists and
                          turns the gate into a permanent red light. --floor overrides.
      (b) NO-DOMINANCE -- the release's (compression, SWR) point is NOT Pareto-dominated
                          by the previous accepted release's point (stored baseline):
                          dominated iff prev had compression <= and SWR >= , strictly
                          better on at least one. A dominated point bought compression by
                          sacrificing salient retention (or bought nothing) -> FAIL.
    On pass the current point becomes the new baseline (unless --no-update-baseline).
  --sweep (STUDY, GPU): re-compacts at each --thresholds boundary (re-runs c1 only; NOT
    d2/d_coordinator/d3), scores compression + SWR + PROSE metrics, emits the frontier
    curve. Sample-scoped.

SWEEP DESIGN (three things that make the curve readable):
  1. PROSE y-axis. Ledger-homed SWR is computed by looking each pinned fact up in the
     LEDGER, which the compaction threshold cannot change - so SWR is flat (~1.0) at every
     threshold and cannot express a tradeoff. The sweep therefore also scores the PROSE:
     per-fact presence and contradiction against `prior_admissions_md` (eval_fact_presence,
     deepseek). Those move. SWR is still reported per row as an INVARIANT CHECK - on a
     fixed fact set it should be identical across rows, and a row that differs signals a
     bug, not a tradeoff. Skip with --no-prose-score.
  2. FIXED DENOMINATOR. A lower threshold ages out more admissions, so the naive sweep
     scores 1275 facts at threshold 3 and 282 at threshold 11 - different exams, not
     different scores. Every row is now graded on the SAME fact set: the pinned facts of
     the span that ages out at the HIGHEST threshold tested, which is a subset of every
     lower threshold's span. Patients that do not age out at the highest threshold are
     dropped so the row population is constant too.
  3. ONE GENERATION MODE. The production row used to be reused from compaction_results
     (generated in `incremental`) while every other row was regenerated in `full`, putting
     one point on a different scale from its neighbours. All rows are now regenerated in
     --mode. --reuse-production-row restores the old cheaper behaviour, with the caveat.

CAVEAT ON THE X-AXIS: compression = paragraph chars / covered input chars, and a lower
threshold ages out more admissions, so the denominator grows mechanically. Falling
compression at low thresholds is largely arithmetic, not the compactor working harder.
The curve characterises the operating point; it does not by itself identify an optimal N,
and how many admissions a physician wants displayed in full is a clinical decision.

GOTCHA: N_COMPACT_THRESHOLD is a config invariant; the sweep overrides it IN-PROCESS
only (never writes config.py). VRAM: ornith compactor (pass A) then deepseek judge
(pass B); never co-resident. Both unloaded.

OUTPUTS:
  data/output/eval/ablations/frontier.json   operating point + gate verdict (+ sweep rows if --sweep)
  data/output/eval/ablations/frontier.csv    per-threshold (compression, SWR) points  (--sweep)
  data/output/eval/ablations/frontier_baseline.json   last accepted release point (gate)
EXIT CODE: 0 if the gate passes (or --sweep / --soft), 1 if the gate fails.

    cd ~/clin_longsum/src/evaluation
    python f12_frontier.py                                   # gate (no GPU)
    python f12_frontier.py --sweep --thresholds 3 5 7 9 11 --sample 20   # frontier (GPU)
    python f12_frontier.py --mode gate --subjects 90000029 90000062 --json-only
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from statistics import median

import pandas as pd

import _bootstrap  # noqa: F401  (puts src/ on sys.path)
import config
import retention_taxonomy as tax
import c1_compactor as c1
import c2_retention_validator as c2v
import f1_salience_recall as f1
import eval_fact_presence as fp
from roles.contracts import CompactorInput

OUT_DIR = config.OUTPUT_DIR / "eval" / "ablations"
OUT_JSON = OUT_DIR / "frontier.json"
OUT_CSV = OUT_DIR / "frontier.csv"
BASELINE_JSON = OUT_DIR / "frontier_baseline.json"
F1_JSON = config.OUTPUT_DIR / "eval" / "salience_recall.json"
COHORT_CSV = config.INTERIM_DIR / "cohort_admissions.csv"


def _f1_floor() -> tuple[float | None, str]:
    """Floor pinned to f1's MEASURED recall, not to a literal 1.0.

    f1 and f12 compute salience-weighted lenient recall over the same ledger, so the
    meaningful bar is "f12 must not report worse retention than f1 just measured". f1's own
    `threshold_swr_lenient` is an aspiration (1.0 = zero genuine loss) and is deliberately
    NOT used here -- borrowing it makes f12 fail whenever f1 fails, reporting one problem
    twice.
    """
    if not F1_JSON.exists():
        return None, f"{F1_JSON.name} not found"
    try:
        d = json.loads(F1_JSON.read_text(encoding="utf-8"))
        v = d.get("swr_lenient_weighted")
        return (float(v), f"{F1_JSON.name}:swr_lenient_weighted") if v is not None else (
            None, f"{F1_JSON.name} has no swr_lenient_weighted")
    except Exception as exc:                                   # noqa: BLE001
        return None, f"could not read {F1_JSON.name}: {exc}"


def _delta_by_hadm() -> dict[int, str]:
    cdf = pd.read_csv(COHORT_CSV, usecols=lambda c: c in {"hadm_id", "bhc_delta", "bhc_text"})
    has_d = "bhc_delta" in cdf.columns
    out = {}
    for r in cdf.itertuples(index=False):
        v = getattr(r, "bhc_delta") if has_d else getattr(r, "bhc_text", None)
        out[int(r.hadm_id)] = str(v) if pd.notna(v) else ""
    return out


def _median_compression(comp_records, delta_by_hadm) -> float | None:
    ratios = []
    for c in comp_records:
        out = len(c.get("prior_admissions_md") or "")
        ind = sum(len(delta_by_hadm.get(int(h), "")) for h in c.get("covered_hadm_ids", []))
        if ind:
            ratios.append(out / ind)
    return round(median(ratios), 4) if ratios else None


def _cohort_swr(comp_records, ledgers, facts_by_hadm) -> tuple[float, float, int]:
    wt = wr = wl = 0.0
    npin = 0
    for c in comp_records:
        led = ledgers.get(int(c["subject_id"]))
        if led is None:
            continue
        idx = c2v._ledger_index(led)
        seen = set()
        for h in c.get("covered_hadm_ids", []):
            for fct in facts_by_hadm.get(int(h), []):
                if not fct.in_ledger:
                    continue
                key = (fct.retention_class, fct.text.strip().lower())
                if key in seen:
                    continue
                seen.add(key)
                home = c2v._where_represented(fct, *idx)
                w = f1.salience_weight(fct)
                wt += w
                npin += 1
                if home in f1.REPRESENTED_HOMES:
                    wr += w
                    wl += w
                elif home == "episode":
                    wl += w
    strict = round(wr / wt, 4) if wt else 1.0
    lenient = round(wl / wt, 4) if wt else 1.0
    return strict, lenient, npin


def _point(comp_records, ledgers, facts_by_hadm, delta_by_hadm) -> dict:
    strict, lenient, npin = _cohort_swr(comp_records, ledgers, facts_by_hadm)
    return {"compression": _median_compression(comp_records, delta_by_hadm),
            "swr_lenient": lenient, "swr_strict": strict,
            "n_patients": len(comp_records), "n_pinned": npin}


def _dominated(point: dict, baseline: dict) -> bool:
    """point dominated by baseline: baseline compresses at least as much (ratio <=) AND
    retains at least as much (swr >=), strictly better on at least one.

    Callers MUST check the baseline's taxonomy_version first - this comparison is only
    meaningful within a single taxonomy version."""
    pc, ps = point.get("compression"), point.get("swr_lenient")
    bc, bs = baseline.get("compression"), baseline.get("swr_lenient")
    if None in (pc, ps, bc, bs):
        return False
    return (bc <= pc and bs >= ps) and (bc < pc or bs > ps)


def _recompact_at(t, pids, ledgers, cohort, facts_by_hadm, hadms_by_pid, mode) -> list[dict]:
    old = config.N_COMPACT_THRESHOLD
    config.N_COMPACT_THRESHOLD = t
    out = []
    try:
        for pid in pids:
            led = ledgers.get(pid)
            if led is None:
                continue
            n_adm = int(led.get("n_admissions") or 0)
            if n_adm <= t:
                continue
            aged = c1._aged_out_for(hadms_by_pid.get(pid, []), cohort, facts_by_hadm, n_adm)
            if not aged:
                continue
            ledger_facts = [c1._node_to_fact(n) for n in (led.get("nodes", []) + led.get("flagged", []))]
            o = c1.compact(CompactorInput(
                subject_id=pid, aged_out=aged, ledger_facts=ledger_facts,
                source_hadm_ids=[a.hadm_id for a in aged], n_prior_admissions=len(aged), mode=mode))
            out.append({"subject_id": pid, "covered_hadm_ids": o.covered_hadm_ids,
                        "prior_admissions_md": o.prior_admissions_md})
    finally:
        config.N_COMPACT_THRESHOLD = old
    return out


def _aged_at(t: int, pid: int, ledgers, cohort, facts_by_hadm, hadms_by_pid) -> list:
    """Aged-out span for a patient at threshold t (config override is in-process only)."""
    led = ledgers.get(pid)
    if led is None:
        return []
    n_adm = int(led.get("n_admissions") or 0)
    if n_adm <= t:
        return []
    old = config.N_COMPACT_THRESHOLD
    config.N_COMPACT_THRESHOLD = t
    try:
        return c1._aged_out_for(hadms_by_pid.get(pid, []), cohort, facts_by_hadm, n_adm)
    finally:
        config.N_COMPACT_THRESHOLD = old


def _fixed_facts(pids, t_max, ledgers, cohort, facts_by_hadm, hadms_by_pid) -> dict:
    """The COMMON fact set every sweep row is graded on: pinned facts of the span that ages
    out at the HIGHEST threshold tested. That span is a subset of every lower threshold's
    span, so the exam is identical at every row instead of shrinking as the threshold rises.
    Patients with no aged span at t_max are excluded entirely (constant row population)."""
    out: dict[int, list] = {}
    for pid in pids:
        aged = _aged_at(t_max, pid, ledgers, cohort, facts_by_hadm, hadms_by_pid)
        if not aged:
            continue
        seen, facts = set(), []
        for a in aged:
            for fct in facts_by_hadm.get(int(a.hadm_id), []):
                if not fct.in_ledger:
                    continue
                key = (fct.retention_class, fct.text.strip().lower())
                if key in seen:
                    continue
                seen.add(key)
                facts.append(fct)
        if facts:
            out[pid] = facts
    return out


def _point_fixed(recs, ledgers, fixed, delta_by_hadm) -> dict:
    """Compression for this row's own span; SWR over the FIXED fact set.

    SWR here should be identical at every threshold: `_where_represented` homes a fact in
    the LEDGER, which the compaction threshold does not touch. It is reported as an
    invariant check, not as a tradeoff axis - a row that differs is a bug signal.
    """
    wt = wr = wl = 0.0
    npin = 0
    for c in recs:
        led = ledgers.get(int(c["subject_id"]))
        if led is None:
            continue
        idx = c2v._ledger_index(led)
        for fct in fixed.get(int(c["subject_id"]), []):
            home = c2v._where_represented(fct, *idx)
            w = f1.salience_weight(fct)
            wt += w
            npin += 1
            if home in f1.REPRESENTED_HOMES:
                wr += w
                wl += w
            elif home == "episode":
                wl += w
    return {"compression": _median_compression(recs, delta_by_hadm),
            "swr_lenient": round(wl / wt, 4) if wt else 1.0,
            "swr_strict": round(wr / wt, 4) if wt else 1.0,
            "n_patients": len(recs), "n_pinned": npin}


def _prose_scores(recs, fixed) -> dict:
    """The y-axis that can actually degrade: does the PROSE carry the fact, and does it
    contradict it? Judged per fact against `prior_admissions_md` (deepseek)."""
    n_present = n_judged = n_contra = 0
    per_patient = []
    for c in recs:
        pid = int(c["subject_id"])
        texts = [f.text for f in fixed.get(pid, [])]
        prose = c.get("prior_admissions_md") or ""
        if not texts or not prose.strip():
            continue
        pres, _ = fp.fact_presence_bools(prose, texts)
        con, _ = fp.fact_contradiction_bools(prose, texts)
        judged = [b for b in pres if b is not None]
        n_judged += len(judged)
        n_present += sum(1 for b in judged if b)
        n_contra += sum(1 for b in con if b)
        per_patient.append({"subject_id": pid, "n_facts": len(texts),
                            "n_present": sum(1 for b in judged if b),
                            "n_contradicted": sum(1 for b in con if b)})
    return {"prose_present": round(n_present / n_judged, 4) if n_judged else None,
            "n_prose_judged": n_judged, "n_contradicted": n_contra,
            "per_patient_prose": per_patient}


def main() -> int:
    ap = argparse.ArgumentParser(description="Compression-retention frontier + operating-point gate.")
    ap.add_argument("--floor", type=float, default=None,
                    help="gate floor: min SWR_lenient at the production compression. "
                         "Default: pinned to f1's measured swr_lenient_weighted from "
                         "salience_recall.json (falls back to 1.0 if that file is absent)")
    ap.add_argument("--sweep", action="store_true", help="run the GPU sweep over --thresholds")
    ap.add_argument("--thresholds", type=int, nargs="*", default=[3, 5, 7, 9, 11])
    ap.add_argument("--sample", type=int, default=20, help="patients per sweep point (deepest spans)")
    ap.add_argument("--subjects", type=int, nargs="*", default=None)
    ap.add_argument("--mode", choices=["full", "incremental"], default="full",
                    help="compaction mode for the sweep (default full = stable/audit-true)")
    ap.add_argument("--reuse-production-row", action="store_true",
                    help="reuse compaction_results for the production-threshold row instead of "
                         "regenerating it. Cheaper, but that row was generated in "
                         f"'{config.COMPACTION_MODE}' mode and the rest in --mode, so the point "
                         "is not on the same scale as its neighbours")
    ap.add_argument("--no-prose-score", action="store_true",
                    help="skip the deepseek prose pass (presence + contradiction). Leaves only "
                         "the ledger-homed SWR, which is threshold-invariant and flat")
    ap.add_argument("--no-update-baseline", action="store_true",
                    help="do not write the current point as the new gate baseline")
    ap.add_argument("--soft", action="store_true", help="always exit 0")
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()
    config.check_paths()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    prod_threshold = config.N_COMPACT_THRESHOLD

    # Floor: pinned to f1's MEASURED recall unless the caller overrides it.
    if args.floor is not None:
        floor, floor_source = args.floor, "--floor"
    else:
        floor, floor_source = _f1_floor()
        if floor is None:
            floor, floor_source = 1.0, f"fallback 1.0 ({floor_source})"

    comp = c1._read_jsonl(config.COMPACTION_JSONL)
    if not comp:
        raise SystemExit(f"{config.COMPACTION_JSONL} not found/empty. Run c1_compactor.py first.")
    ledger_src, _ = f1._resolve_ledger_src()
    ledgers = {int(l["subject_id"]): l for l in c1._read_jsonl(ledger_src)}
    facts_by_hadm = c1._facts_by_hadm()
    delta_by_hadm = _delta_by_hadm()

    # ── operating point at the production threshold (existing compaction; no GPU) ──
    op = _point(comp, ledgers, facts_by_hadm, delta_by_hadm)
    op["threshold"] = prod_threshold

    floor_ok = (op["swr_lenient"] is not None and op["swr_lenient"] >= floor)
    baseline = json.loads(BASELINE_JSON.read_text(encoding="utf-8")) if BASELINE_JSON.exists() else None
    # The stored baseline has always carried taxonomy_version, but nothing read it.
    # A taxonomy bump can change WHAT IS SERVED, which shifts both compression and SWR for
    # reasons that have nothing to do with a regression. Comparing across versions is not a
    # weaker signal, it is a meaningless one - so the dominance gate is SKIPPED, loudly, and
    # the absolute floor gate carries the release on its own until a same-version baseline
    # exists. On pass the current point is written back, which re-baselines automatically.
    baseline_stale = bool(baseline) and baseline.get("taxonomy_version") != tax.TAXONOMY_VERSION
    dominated = _dominated(op, baseline) if (baseline and not baseline_stale) else False
    no_regression_ok = not dominated
    gate_pass = floor_ok and no_regression_ok

    sweep_rows = []
    if args.sweep:
        cohort = c1._cohort_by_hadm()
        cdf = pd.read_csv(COHORT_CSV, usecols=["subject_id", "hadm_id"])
        hadms_by_pid: dict[int, list[int]] = {}
        for _, r in cdf.iterrows():
            hadms_by_pid.setdefault(int(r["subject_id"]), []).append(int(r["hadm_id"]))
        if args.subjects:
            pids = list(args.subjects)
        else:
            pids = [int(c["subject_id"]) for c in
                    sorted(comp, key=lambda c: len(c.get("covered_hadm_ids", [])), reverse=True)[:args.sample]]
        thresholds = sorted(set(args.thresholds))
        t_max = max(thresholds)
        # FIXED DENOMINATOR: grade every row on the span that ages out at the highest
        # threshold, and drop patients that have no such span so the population is constant.
        fixed = _fixed_facts(pids, t_max, ledgers, cohort, facts_by_hadm, hadms_by_pid)
        pids = [p for p in pids if p in fixed]
        n_fixed = sum(len(v) for v in fixed.values())
        if not args.json_only:
            print(f"  fixed fact set: {n_fixed} pinned facts over {len(pids)} patients "
                  f"(the span that ages out at threshold {t_max})")

        # ── Pass A: ornith compaction for every row ──────────────────────────────
        staged: dict[int, tuple[list, bool]] = {}
        try:
            for t in thresholds:
                if t == prod_threshold and args.reuse_production_row:
                    recs = [c for c in comp if int(c["subject_id"]) in set(pids)]
                    regen = False
                else:
                    recs = _recompact_at(t, pids, ledgers, cohort, facts_by_hadm,
                                         hadms_by_pid, args.mode)
                    regen = True
                staged[t] = (recs, regen)
                if not args.json_only:
                    print(f"  compacted at threshold={t:2d}  patients={len(recs)}  regen={regen}")
        finally:
            c1._ollama_unload()          # free ornith before the deepseek judge

        # ── Pass B: deepseek prose scoring + fixed-set SWR ───────────────────────
        try:
            for t in thresholds:
                recs, regen = staged[t]
                pt = _point_fixed(recs, ledgers, fixed, delta_by_hadm)
                pt["threshold"] = t
                pt["regenerated"] = regen
                pt["mode"] = args.mode if regen else config.COMPACTION_MODE
                if not args.no_prose_score:
                    pt.update(_prose_scores(recs, fixed))
                sweep_rows.append(pt)
                if not args.json_only:
                    print(f"  threshold={t:2d}  compression={pt['compression']}  "
                          f"swr_lenient={pt['swr_lenient']}  "
                          f"prose_present={pt.get('prose_present')}  "
                          f"contradicted={pt.get('n_contradicted')}  "
                          f"(n={pt['n_patients']}, regen={regen})")
        finally:
            if not args.no_prose_score:
                fp.ollama_unload()       # free deepseek

    report = {
        "metric": "compression_retention_frontier",
        "taxonomy_version": tax.TAXONOMY_VERSION,
        "production_threshold": prod_threshold,
        "operating_point": op,
        "floor": floor,
        "floor_source": floor_source,
        "sweep_mode": args.mode if args.sweep else None,
        "sweep_fixed_fact_set": (
            {"anchor_threshold": max(set(args.thresholds)),
             "n_facts": sum(len(v) for v in (fixed or {}).values()) if args.sweep else None,
             "note": "every row graded on the same facts; SWR is threshold-invariant by "
                     "construction and is an invariant check, not a tradeoff axis"}
            if args.sweep else None),
        "baseline_point": baseline,
        "baseline_stale": baseline_stale,
        "baseline_taxonomy_version": (baseline or {}).get("taxonomy_version"),
        "taxonomy_version": tax.TAXONOMY_VERSION,
        "gates": {"gate_floor": floor_ok, "gate_no_dominance": no_regression_ok},
        "all_gates_pass": gate_pass,
        "dominated_by_baseline": dominated,
        "sweep": sweep_rows or None,
    }
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if sweep_rows:
        with OUT_CSV.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["threshold", "compression", "swr_lenient",
                                               "swr_strict", "prose_present", "n_contradicted",
                                               "n_prose_judged", "n_patients", "n_pinned",
                                               "regenerated", "mode"])
            w.writeheader()
            for r in sweep_rows:
                w.writerow({k: r.get(k) for k in w.fieldnames})

    if gate_pass and not args.no_update_baseline:
        BASELINE_JSON.write_text(json.dumps(
            {"compression": op["compression"], "swr_lenient": op["swr_lenient"],
             "swr_strict": op["swr_strict"], "threshold": prod_threshold,
             "taxonomy_version": tax.TAXONOMY_VERSION}, indent=2), encoding="utf-8")

    if not args.json_only:
        print("\n" + "=" * 60)
        print("F12 COMPRESSION-RETENTION FRONTIER")
        print(f"  operating point (thr {prod_threshold}) : compression={op['compression']}  "
              f"swr_lenient={op['swr_lenient']}  (n={op['n_patients']})")
        if baseline:
            print(f"  baseline point          : compression={baseline.get('compression')}  "
                  f"swr_lenient={baseline.get('swr_lenient')}")
            if baseline_stale:
                print(f"  ** BASELINE STALE: stored at {baseline.get('taxonomy_version')}, "
                      f"running {tax.TAXONOMY_VERSION}. Dominance gate SKIPPED -- "
                      f"cross-version comparison is not meaningful. The floor gate still "
                      f"applies; on pass this run re-baselines. **")
        print(f"  gate_floor (>= {floor})   : {'PASS' if floor_ok else 'FAIL'}"
              f"   [floor from {floor_source}]")
        print(f"  gate_no_dominance       : {'SKIPPED (stale baseline)' if baseline_stale else ('PASS' if no_regression_ok else 'FAIL')}"
              + ("  (dominated by baseline)" if dominated else ""))
        print(f"  {'GATE PASS' if gate_pass else 'GATE FAIL'}")
        if args.sweep:
            print(f"  sweep points -> {OUT_CSV}")
        print(f"  json -> {OUT_JSON}")

    return 0 if (args.soft or gate_pass) else 1


if __name__ == "__main__":
    sys.exit(main())
