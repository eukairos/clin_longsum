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
f2_integrity.py
===============
Phase F Tier-0 deterministic STRUCTURAL + TEMPORAL INTEGRITY gate.

Distinct from f1 (retention recall) and f4 (faithfulness): f2 asks whether the
view is ASSEMBLED CORRECTLY - the right admissions in the right tiers, in order,
with the holdout excluded and the ledger well-formed. No LLM; reproducible
on the frozen cohort. Non-zero exit on any hard-gate failure (CI consumes it).

The coordinator's window math is the contract checked here (d_coordinator.assemble):
  n        = number of ESTABLISHED admissions (holdout removed, promoted kept)
  boundary = n - N_COMPACT_THRESHOLD
  compacted (Tier 3) : admit_seq <= boundary
  hot window (Tier 2a): admit_seq in (boundary, n-1], last N_HOT_DISPLAY
  current  (Tier 1)  : admit_seq == n
We derive the EXPECTED partition from cohort_admissions.csv (via holdout.py) and
check the actual artifacts (compaction_results, longitudinal_view) against it.

HARD GATES (block the build):
  gate_config      N_HOT_DISPLAY <= N_COMPACT_THRESHOLD
  gate_holdout     no still-held-out hadm appears in any compacted span.
                   CAVEAT: holdout.holdout_hadms() subtracts PROMOTED admissions, so after a
                   complete fold-in replay the still-held set is empty and this gate is
                   VACUOUS - it can only catch a future-leak while admissions remain held
                   out. Run it at a mid-replay checkpoint (replay_production --stop-after N)
                   if you want it to mean anything.
  gate_span        covered_hadm_ids == expected oldest span; established admit_seq
                   is contiguous 1..n (the window math assumes it)
  gate_consistency n_admissions_compacted == len(covered_hadm_ids), the field is PRESENT,
                   and generated_hadm_ids == covered_hadm_ids (the prose's actual
                   provenance matches the claimed span - a seeded fold-in builds the two
                   by different routes, so they can diverge)
  gate_current     the current (Tier 1) admission is not ALSO shown compacted or hot, and
                   current_episode_md is rendered. After a fold-in the current admission is
                   the newly promoted one, so this is fold-in sensitive.
  gate_ledger      served ledger entries well-formed (known class/tier, text present)
  gate_hotwindow   (view present) hot window == expected set, len <= N_HOT_DISPLAY,
                   no compacted admission shown hot; n_total_admissions == n; and no
                   expected-hot admission is ABSENT (a demoted admission that lost its d1
                   paragraph vanishes from the served record - the aging failure mode)
ADVISORY (surfaced, never gates):
  prose date-leak  a YYYY year in prior_admissions_md later than the established
                   span's last discharge year (admit_seq ordering can outrun dates)
  window gap       established admissions in neither the compacted span nor the hot window
                   because N_HOT_DISPLAY < N_COMPACT_THRESHOLD (a config choice, not a bug)

OUTPUTS:
  data/output/eval/integrity.json              gate booleans + counts
  data/output/eval/integrity_violations.csv    one row per violation (+ advisories)
EXIT CODE: 0 if all hard gates pass, 1 otherwise.

    cd ~/clin_longsum/src/evaluation
    python f2_integrity.py
    python f2_integrity.py --subject 90000029
    python f2_integrity.py --json-only

No GPU is used, so there is no VRAM to clear.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys

import pandas as pd

import _bootstrap  # noqa: F401  (puts src/ on sys.path)
import config
import retention_taxonomy as tax
import c1_compactor as c1
import holdout

OUT_DIR = config.OUTPUT_DIR / "eval"
OUT_JSON = OUT_DIR / "integrity.json"
OUT_CSV = OUT_DIR / "integrity_violations.csv"

COMPACTION_JSONL = config.COMPACTION_JSONL
LEDGER_JSONL = config.OUTPUT_DIR / "retention_ledger.jsonl"
VIEW_JSONL = config.OUTPUT_DIR / "longitudinal_view.jsonl"
COHORT_CSV = config.INTERIM_DIR / "cohort_admissions.csv"

_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


# ── cohort / established partition ────────────────────────────────────────────────

def _load_established():
    """Return {subject_id: [(admit_seq, hadm_id), ...] sorted}, the holdout hadm set,
    and {subject_id: last_established_year} -- all from the cohort via holdout.py."""
    cols = {"subject_id", "hadm_id", "admit_seq", "is_holdout", "dischtime"}
    df = pd.read_csv(COHORT_CSV, usecols=lambda c: c in cols)
    held = holdout.holdout_hadms(df)
    est = holdout.established(df).copy()
    est["admit_seq"] = est["admit_seq"].astype(int)
    est["hadm_id"] = est["hadm_id"].astype(int)

    by_subj: dict[int, list[tuple[int, int]]] = {}
    last_year: dict[int, int] = {}
    for sid, g in est.groupby("subject_id"):
        sid = int(sid)
        g = g.sort_values("admit_seq")
        by_subj[sid] = [(int(s), int(h)) for s, h in zip(g["admit_seq"], g["hadm_id"])]
        if "dischtime" in g.columns:
            yrs = pd.to_datetime(g["dischtime"], format="mixed", dayfirst=True, errors="coerce").dt.year.dropna()
            if not yrs.empty:
                last_year[sid] = int(yrs.max())
    return by_subj, held, last_year


def _expected_partition(seq_hadm: list[tuple[int, int]]):
    """From a patient's established (admit_seq, hadm) list, derive the expected
    compacted / hot / current hadm sets per the coordinator's window math. Returns
    None if admit_seq is not contiguous 1..n (window math invalid)."""
    n = len(seq_hadm)
    seqs = [s for s, _ in seq_hadm]
    if seqs != list(range(1, n + 1)):
        return None  # non-contiguous -> caller flags gate_span
    boundary = n - config.N_COMPACT_THRESHOLD
    by_seq = {s: h for s, h in seq_hadm}
    compacted = {h for s, h in seq_hadm if s <= boundary}
    hot_seqs = list(range(boundary + 1, n))[-config.N_HOT_DISPLAY:]
    hot = {by_seq[s] for s in hot_seqs}
    return {"n": n, "boundary": boundary, "compacted": compacted, "hot": hot,
            "current": by_seq.get(n)}


# ── ledger schema ─────────────────────────────────────────────────────────────────

def _ledger_violations(pid: int, led: dict) -> list[dict]:
    out = []

    def _row(detail):
        return {"check": "ledger_schema", "subject_id": pid, "detail": detail}

    for sect in ("nodes", "flagged", "amended"):
        for e in led.get(sect, []) or []:
            if not str(e.get("text", "")).strip():
                out.append(_row(f"{sect} entry with empty text"))
            rc = e.get("retention_class")
            if rc is not None and not tax.is_known_class(rc):
                out.append(_row(f"{sect} unknown retention_class '{rc}'"))
            st = e.get("salience_tier")
            if st is not None and not tax.is_known_tier(st):
                out.append(_row(f"{sect} unknown salience_tier '{st}'"))
    for t in led.get("considered_excluded", []) or []:
        if not str(t).strip():
            out.append(_row("considered_excluded entry empty"))
    for ep in led.get("episodes", []) or []:
        if not str(ep.get("parent_condition", "")).strip():
            out.append(_row("episode rollup with empty parent_condition"))
    return out


# ── main ──────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Structural/temporal integrity gate (deterministic).")
    ap.add_argument("--subject", type=int, default=None)
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()
    config.check_paths()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    comp = {int(c["subject_id"]): c for c in c1._read_jsonl(COMPACTION_JSONL)}
    if not comp:
        raise SystemExit(f"{COMPACTION_JSONL} not found/empty. Run c1_compactor.py first.")
    ledgers = {int(l["subject_id"]): l for l in c1._read_jsonl(LEDGER_JSONL)}
    views = {int(v["subject_id"]): v for v in c1._read_jsonl(VIEW_JSONL)}
    have_views = bool(views)
    by_subj, held, last_year = _load_established()

    if args.subject is not None:
        comp = {k: v for k, v in comp.items() if k == args.subject}

    violations: list[dict] = []
    advisories: list[dict] = []
    g = {"gate_config": True, "gate_holdout": True, "gate_span": True,
         "gate_consistency": True, "gate_current": True, "gate_ledger": True,
         "gate_hotwindow": True}
    n_patients = 0

    # gate_config (global)
    if config.N_HOT_DISPLAY > config.N_COMPACT_THRESHOLD:
        g["gate_config"] = False
        violations.append({"check": "config", "subject_id": -1,
                           "detail": f"N_HOT_DISPLAY ({config.N_HOT_DISPLAY}) > "
                                     f"N_COMPACT_THRESHOLD ({config.N_COMPACT_THRESHOLD})"})

    for pid, c in comp.items():
        n_patients += 1
        covered = set(int(h) for h in c.get("covered_hadm_ids", []))
        generated = set(int(h) for h in c.get("generated_hadm_ids", []))
        # No len(covered) fallback: a consistency check whose default is "assume consistent"
        # cannot detect the schema drift it exists to catch.
        n_compacted = c.get("n_admissions_compacted", None)

        # gate_holdout
        bleed = covered & held
        if bleed:
            g["gate_holdout"] = False
            violations.append({"check": "holdout_bleed", "subject_id": pid,
                               "detail": f"held-out hadm(s) in compacted span: {sorted(bleed)}"})

        seq_hadm = by_subj.get(pid)
        if not seq_hadm:
            g["gate_span"] = False
            violations.append({"check": "span", "subject_id": pid,
                               "detail": "no established admissions found in cohort"})
            continue

        exp = _expected_partition(seq_hadm)
        if exp is None:
            g["gate_span"] = False
            violations.append({"check": "span", "subject_id": pid,
                               "detail": "established admit_seq not contiguous 1..n "
                                         "(window math invalid)"})
            continue

        if exp["boundary"] < 1:
            g["gate_span"] = False
            violations.append({"check": "span", "subject_id": pid,
                               "detail": f"n={exp['n']} <= N_COMPACT_THRESHOLD; patient "
                                         "should have no compacted span"})

        # gate_span: covered must equal the expected oldest span
        extra = covered - exp["compacted"]     # too-recent admissions compacted (future-leak)
        missing = exp["compacted"] - covered   # old admissions not compacted (gap)
        if extra:
            g["gate_span"] = False
            violations.append({"check": "span_future_leak", "subject_id": pid,
                               "detail": f"compacted admissions newer than boundary: {sorted(extra)}"})
        if missing:
            g["gate_span"] = False
            violations.append({"check": "span_gap", "subject_id": pid,
                               "detail": f"expected-compacted admissions absent from covered: {sorted(missing)}"})

        # gate_consistency
        if n_compacted is None:
            g["gate_consistency"] = False
            violations.append({"check": "consistency_missing_field", "subject_id": pid,
                               "detail": "n_admissions_compacted absent from the compaction "
                                         "record (schema drift)"})
        elif n_compacted != len(covered):
            g["gate_consistency"] = False
            violations.append({"check": "consistency", "subject_id": pid,
                               "detail": f"n_admissions_compacted={n_compacted} != "
                                         f"len(covered)={len(covered)}"})
        # The prose's real provenance. In the seeded fold-in path generated_hadm_ids is built
        # as (prev_covered - pinned span) | delta while covered_hadm_ids is recomputed from the
        # current aged span - different routes, so they can silently diverge and leave the
        # paragraph not actually describing every admission the record claims it covers.
        #
        # TWO-REGION EXCEPTION: when a physician has pinned the Prior-Admissions tier, the
        # machine continuation deliberately covers ONLY the span below the pin, so generated is
        # a strict subset of covered and equality would be a false alarm. There we check the
        # weaker invariant (no admission generated that isn't covered) plus the region
        # invariant on the PERSISTED text, which nothing else re-checks after c1's in-process
        # assert.
        prefix_md = c.get("pinned_prefix_md") or ""
        served_md = c.get("prior_admissions_md") or ""
        if prefix_md:
            stray = generated - covered
            if stray:
                g["gate_consistency"] = False
                violations.append({"check": "provenance_stray", "subject_id": pid,
                                   "detail": f"generated_hadm_ids not in covered span: "
                                             f"{sorted(stray)}"})
            if served_md and not served_md.startswith(prefix_md):
                g["gate_consistency"] = False
                violations.append({"check": "pin_region_broken", "subject_id": pid,
                                   "detail": "prior_admissions_md does not start with "
                                             "pinned_prefix_md; the physician-edited region "
                                             "was mutated"})
        elif generated and generated != covered:
            g["gate_consistency"] = False
            violations.append({"check": "provenance_mismatch", "subject_id": pid,
                               "detail": f"generated_hadm_ids != covered_hadm_ids; "
                                         f"only-generated={sorted(generated - covered)}, "
                                         f"only-covered={sorted(covered - generated)}"})

        # gate_hotwindow (only if the assembled view is present)
        v = views.get(pid)
        if v is not None:
            hot = v.get("hot_window", []) or []
            hot_hadms = {int(p["hadm_id"]) for p in hot if p.get("hadm_id") is not None}
            if len(hot) > config.N_HOT_DISPLAY:
                g["gate_hotwindow"] = False
                violations.append({"check": "hotwindow_size", "subject_id": pid,
                                   "detail": f"hot_window len {len(hot)} > N_HOT_DISPLAY {config.N_HOT_DISPLAY}"})
            overlap = hot_hadms & covered
            if overlap:
                g["gate_hotwindow"] = False
                violations.append({"check": "hotwindow_overlap", "subject_id": pid,
                                   "detail": f"admission shown hot AND compacted: {sorted(overlap)}"})
            
            extra_hot = hot_hadms - exp["hot"]
            missing_hot = exp["hot"] - hot_hadms
            if extra_hot:   # hot showing an unexpected admission -> real misassembly
                g["gate_hotwindow"] = False
                violations.append({"check": "hotwindow_unexpected", "subject_id": pid,
                                   "detail": f"hot shows non-expected admission(s): {sorted(extra_hot)}"})
            if missing_hot:  # a demoted admission absent from the served record - usually a
                             # missing d1 paragraph. This is THE aging failure mode: unlike an
                             # unexpected extra admission, a missing one is invisible to any
                             # reader of the record, so it gates.
                g["gate_hotwindow"] = False
                violations.append({"check": "hotwindow_missing", "subject_id": pid,
                                   "detail": f"expected-hot admission(s) absent from view "
                                             f"(missing d1 paragraph?): {sorted(missing_hot)}"})

            # gate_current - the Tier 1 admission must be rendered and must not ALSO appear
            # in a lower tier. exp["current"] changes on every fold-in promotion.
            cur = exp["current"]
            if cur is not None and cur in (covered | hot_hadms):
                g["gate_current"] = False
                violations.append({"check": "current_duplicated", "subject_id": pid,
                                   "detail": f"current admission {cur} also shown "
                                             f"{'compacted' if cur in covered else 'hot'}"})
            if not str(v.get("current_episode_md") or "").strip():
                g["gate_current"] = False
                violations.append({"check": "current_missing", "subject_id": pid,
                                   "detail": f"current_episode_md empty; current admission "
                                             f"{cur} is not rendered"})

            # advisory: admissions in neither tier because N_HOT_DISPLAY caps the window
            all_est = {h for _, h in seq_hadm}
            shown = exp["compacted"] | exp["hot"] | ({cur} if cur is not None else set())
            gap = all_est - shown
            if gap:
                advisories.append({"check": "window_gap", "subject_id": pid,
                                   "detail": f"established admission(s) in neither tier "
                                             f"(N_HOT_DISPLAY={config.N_HOT_DISPLAY} < "
                                             f"N_COMPACT_THRESHOLD={config.N_COMPACT_THRESHOLD}): "
                                             f"{sorted(gap)}"})

            n_total = v.get("n_total_admissions")
            if n_total is not None and int(n_total) != exp["n"]:
                g["gate_hotwindow"] = False
                violations.append({"check": "n_total", "subject_id": pid,
                                   "detail": f"n_total_admissions={n_total} != established n={exp['n']}"})

        # advisory: prose date-leak in the served prior_admissions_md
        prior_md = (v or {}).get("prior_admissions_md") or c.get("prior_admissions_md") or ""
        ly = last_year.get(pid)
        if ly and prior_md:
            yrs = [int(y) for y in _YEAR_RE.findall(prior_md)]
            future = sorted({y for y in yrs if y > ly})
            if future:
                advisories.append({"check": "prose_date_leak", "subject_id": pid,
                                   "detail": f"year(s) {future} later than established last "
                                             f"discharge year {ly}"})

    # ledger schema (all patients present in the served ledger, or just the filtered one)
    for pid, led in ledgers.items():
        if args.subject is not None and pid != args.subject:
            continue
        rows = _ledger_violations(pid, led)
        if rows:
            g["gate_ledger"] = False
            violations.extend(rows)

    all_pass = all(g.values())
    report = {
        "metric": "structural_temporal_integrity",
        "taxonomy_version": tax.TAXONOMY_VERSION,
        "views_present": have_views,
        "patients_checked": n_patients,
        "n_compact_threshold": config.N_COMPACT_THRESHOLD,
        "n_hot_display": config.N_HOT_DISPLAY,
        "gates": g,
        "all_gates_pass": all_pass,
        "n_violations": len(violations),
        "n_advisories": len(advisories),
    }

    with OUT_JSON.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    rows = violations + [{**a, "advisory": True} for a in advisories]
    if rows:
        cols = ["check", "subject_id", "detail", "advisory"]
        with OUT_CSV.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({**{k: "" for k in cols}, **r})

    if not args.json_only:
        print("\n" + "=" * 60)
        print("F2 STRUCTURAL / TEMPORAL INTEGRITY (deterministic)")
        print(f"  patients checked : {n_patients}")
        print(f"  views present    : {have_views}"
              + ("" if have_views else "  (hot-window checks skipped)"))
        print("  gates:")
        for k, ok in g.items():
            print(f"    {'PASS' if ok else 'FAIL'}  {k}")
        if violations:
            print(f"\n  {len(violations)} VIOLATION(S):")
            for x in violations[:25]:
                print(f"    [{x['check']}] subj {x['subject_id']}: {x['detail']}")
            if len(violations) > 25:
                print(f"    ... +{len(violations) - 25} more (see {OUT_CSV.name})")
        if advisories:
            print(f"\n  {len(advisories)} advisory (non-gating):")
            for x in advisories[:10]:
                print(f"    [{x['check']}] subj {x['subject_id']}: {x['detail']}")
        print(f"\n  {'ALL GATES PASS' if all_pass else 'GATE FAILURE -- build should stop'}")
        print(f"  json  -> {OUT_JSON}")
        if rows:
            print(f"  rows  -> {OUT_CSV}")

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
