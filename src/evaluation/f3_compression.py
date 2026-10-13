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
f3_compression.py
=================
Phase F intrinsic metric #3 - COMPRESSION vs RETENTION curve (Tier 0, deterministic).

Characterises the trade-off the compaction stage exists to make: how much the
Prior Admissions paragraph shrinks the aged-out span (x) against how much salient
content survives (y). No LLM, no GPU.

  x = compression ratio per patient = chars(prior_admissions_md)
                                      / chars(source of the compacted span)
      Primary denominator: bhc_delta (the deduped novel content c1 had to condense).
      Secondary: bhc_text (the full BHC). Lower ratio = more compression.
      Chars (not tokens) to match the pipeline convention - d1's audit ratio is
      chars_out/chars_in; deterministic and dependency-free.
  y = salience-weighted recall over the compacted span (REUSES f1 - single source
      for the weighting and the c2 representation-homing).

The defensible operating point is high compression (low ratio) at SWR ~ 1.0 with
zero T1/T2 loss. f1 owns the retention gates; f3 owns the compression sanity bound
and emits the paired (compression, SWR) scatter for the curve.

HARD GATE (Tier 0, sanity bound - tune from the first run's distribution):
  gate_compression_band : COMP_LO <= median(primary compression ratio) <= COMP_HI
      catches degenerate compaction -- ~1.0 means compaction did nothing,
      ~0 means it nuked the content.
ADVISORY (surfaced, never gates):
  a patient with aggressive compression AND swr_lenient < 1.0 (loss concentrated
  where compression is hardest) - worth an eyeball; f1 already gates real loss.

OUTPUTS:
  data/output/eval/compression_retention.csv   per-patient scatter (plot source)
  data/output/eval/compression_retention.json  medians/quantiles + gate
EXIT CODE: 0 if the band gate passes, 1 otherwise.

    cd ~/clin_longsum/src/evaluation
    python f3_compression.py
    python f3_compression.py --subject 90000029
    python f3_compression.py --lo 0.02 --hi 0.5      # tune the band
    python f3_compression.py --json-only

"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from statistics import median, mean, quantiles

import pandas as pd

import _bootstrap  # noqa: F401  (puts src/ on sys.path)
import config
import c1_compactor as c1
import c2_retention_validator as c2v
import f1_salience_recall as f1

OUT_DIR = config.OUTPUT_DIR / "eval"
OUT_JSON = OUT_DIR / "compression_retention.json"
OUT_CSV = OUT_DIR / "compression_retention.csv"
COHORT_CSV = config.INTERIM_DIR / "cohort_admissions.csv"

# Median compression sanity band (primary ratio = out_chars / in_chars over the
# compacted span). PROVISIONAL - tune from the first run's distribution, like the
# Tier-1 tolerance bands. Wide by default so it only blocks egregious regressions.
COMP_LO, COMP_HI = 0.05, 0.45

# Advisory: flag patients compressing below this AND losing salient content.
AGGRESSIVE = 0.05


# ── per-patient salience-weighted recall (reuses f1 weighting + c2 homing) ───────

def _swr_for(source, led) -> tuple[float, float, int]:
    """(swr_strict, swr_lenient, n_pinned) over a patient's compacted-span pinned facts."""
    fact_ids, by_cls, excluded, episodes = c2v._ledger_index(led)
    w_total = w_repr = w_len = 0.0
    for fct in source:
        home = c2v._where_represented(fct, fact_ids, by_cls, excluded, episodes)
        w = f1.salience_weight(fct)
        w_total += w
        if home in f1.REPRESENTED_HOMES:
            w_repr += w
            w_len += w
        elif home == "episode":
            w_len += w
    strict = round(w_repr / w_total, 4) if w_total else 1.0
    lenient = round(w_len / w_total, 4) if w_total else 1.0
    return strict, lenient, len(source)


def _q(data: list[float]) -> dict:
    data = [x for x in data if x is not None]
    if not data:
        return {"n": 0, "median": None, "mean": None, "p25": None, "p75": None}
    p25 = p75 = median(data)
    if len(data) >= 2:
        qs = quantiles(data, n=4)          # [p25, p50, p75]
        p25, p75 = round(qs[0], 4), round(qs[2], 4)
    return {"n": len(data), "median": round(median(data), 4),
            "mean": round(mean(data), 4), "p25": p25, "p75": p75}


def main() -> int:
    ap = argparse.ArgumentParser(description="Compression vs retention curve (deterministic).")
    ap.add_argument("--subject", type=int, default=None)
    ap.add_argument("--lo", type=float, default=COMP_LO, help="min median compression (band gate)")
    ap.add_argument("--hi", type=float, default=COMP_HI, help="max median compression (band gate)")
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()
    config.check_paths()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    comp = c1._read_jsonl(config.COMPACTION_JSONL)
    if not comp:
        raise SystemExit(f"{config.COMPACTION_JSONL} not found/empty. Run c1_compactor.py first.")
    if args.subject is not None:
        comp = [c for c in comp if int(c["subject_id"]) == args.subject]

    ledger_src, _ = f1._resolve_ledger_src()
    ledgers = {int(l["subject_id"]): l for l in c1._read_jsonl(ledger_src)}
    facts_by_hadm = c1._facts_by_hadm()

    # cohort source text per hadm (defensive about which bhc columns exist)
    cols = {"hadm_id", "bhc_delta", "bhc_text"}
    cdf = pd.read_csv(COHORT_CSV, usecols=lambda c: c in cols)
    have_delta = "bhc_delta" in cdf.columns
    have_text = "bhc_text" in cdf.columns
    delta_by_hadm, text_by_hadm = {}, {}
    for r in cdf.itertuples(index=False):
        h = int(getattr(r, "hadm_id"))
        if have_delta:
            d = getattr(r, "bhc_delta")
            delta_by_hadm[h] = str(d) if pd.notna(d) else ""
        if have_text:
            t = getattr(r, "bhc_text")
            text_by_hadm[h] = str(t) if pd.notna(t) else ""
    primary = "delta" if have_delta else "text"

    rows: list[dict] = []
    for c in comp:
        pid = int(c["subject_id"])
        covered = [int(h) for h in c.get("covered_hadm_ids", [])]
        led = ledgers.get(pid)
        if led is None or not covered:
            continue

        out_chars = len(c.get("prior_admissions_md") or "")
        in_delta = sum(len(delta_by_hadm.get(h, "")) for h in covered) if have_delta else 0
        in_text = sum(len(text_by_hadm.get(h, "")) for h in covered) if have_text else 0
        comp_delta = round(out_chars / in_delta, 4) if in_delta else None
        comp_text = round(out_chars / in_text, 4) if in_text else None

        # pinned source facts in the compacted span (same dedup as f1/c2)
        seen, source = set(), []
        for h in covered:
            for fct in facts_by_hadm.get(h, []):
                if not fct.in_ledger:
                    continue
                key = (fct.retention_class, fct.text.strip().lower())
                if key not in seen:
                    seen.add(key)
                    source.append(fct)
        swr_strict, swr_lenient, n_pinned = _swr_for(source, led)

        comp_primary = comp_delta if primary == "delta" else comp_text
        flag = ""
        if comp_primary is not None and comp_primary < AGGRESSIVE and swr_lenient < 1.0:
            flag = "aggressive_lossy"
        rows.append({
            "subject_id": pid, "n_covered": len(covered), "out_chars": out_chars,
            "in_chars_delta": in_delta, "in_chars_text": in_text,
            "compression_delta": comp_delta, "compression_text": comp_text,
            "n_pinned": n_pinned, "swr_strict": swr_strict, "swr_lenient": swr_lenient,
            "flag": flag,
        })

    if not rows:
        raise SystemExit("no compacted patients to score (compaction_results empty?).")

    primary_ratios = [r[f"compression_{primary}"] for r in rows]
    med_primary = median([x for x in primary_ratios if x is not None])
    gate_band = args.lo <= med_primary <= args.hi
    lossy = [r for r in rows if r["flag"]]

    report = {
        "metric": "compression_vs_retention",
        "taxonomy_version": __import__("retention_taxonomy").TAXONOMY_VERSION,
        "patients": len(rows),
        "primary_denominator": primary,
        "compression": {
            "delta": _q([r["compression_delta"] for r in rows]),
            "text": _q([r["compression_text"] for r in rows]),
        },
        "retention": {
            "swr_strict": _q([r["swr_strict"] for r in rows]),
            "swr_lenient": _q([r["swr_lenient"] for r in rows]),
            "min_swr_lenient": min(r["swr_lenient"] for r in rows),
        },
        "band": [args.lo, args.hi],
        "median_primary_compression": round(med_primary, 4),
        "gates": {"gate_compression_band": gate_band},
        "all_gates_pass": gate_band,
        "n_aggressive_lossy_advisory": len(lossy),
    }

    with OUT_JSON.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    cols_out = ["subject_id", "n_covered", "out_chars", "in_chars_delta", "in_chars_text",
                "compression_delta", "compression_text", "n_pinned",
                "swr_strict", "swr_lenient", "flag"]
    with OUT_CSV.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols_out)
        w.writeheader()
        w.writerows(rows)

    if not args.json_only:
        cd = report["compression"]["delta"]
        ct = report["compression"]["text"]
        sl = report["retention"]["swr_lenient"]
        print("\n" + "=" * 60)
        print("F3 COMPRESSION vs RETENTION (deterministic)")
        print(f"  compacted patients      : {len(rows)}")
        print(f"  compression (vs delta)  : median {cd['median']}  "
              f"(p25 {cd['p25']}, p75 {cd['p75']})")
        if have_text:
            print(f"  compression (vs bhc_text): median {ct['median']}  "
                  f"(p25 {ct['p25']}, p75 {ct['p75']})")
        print(f"  SWR lenient (retention) : median {sl['median']}  "
              f"(min {report['retention']['min_swr_lenient']})")
        print(f"  band gate [{args.lo}, {args.hi}] on median({primary}) "
              f"= {med_primary:.4f}: {'PASS' if gate_band else 'FAIL'}")
        if lossy:
            print(f"\n  {len(lossy)} advisory (aggressive compression + SWR<1):")
            for r in lossy[:15]:
                print(f"    subj {r['subject_id']}: comp_{primary}="
                      f"{r['compression_' + primary]}  swr_lenient={r['swr_lenient']}")
        print(f"\n  {'GATE PASS' if gate_band else 'GATE FAIL -- band out of range; inspect or retune'}")
        print(f"  json -> {OUT_JSON}")
        print(f"  csv  -> {OUT_CSV}  (compression-vs-SWR scatter for plotting)")

    return 0 if gate_band else 1


if __name__ == "__main__":
    sys.exit(main())
