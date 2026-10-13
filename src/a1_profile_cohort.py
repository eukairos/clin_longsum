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
a1_profile_cohort.py
====================
Phase A1 -- profile the admission-count distribution of the source CSV, focused
on the >7-admission tail that the tiered compaction pipeline needs.
READ-ONLY, writes a single profile artifact. 
What it does:
  - load longsum_multiple_admissions.csv, rename target -> bhc_text
  - dedup hadm_id keeping the longest bhc_text (same row may appear twice)
  - count distinct admissions per subject_id

Run on the workstation (reads a Windows path):
    cd ~/Summarization/clin_longsum/src (or wherever your clin_longsum folder sits)
    python a1_profile_cohort.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

import config

# Admissions strictly greater than this go through compaction.
COMPACT_THRESHOLD = config.N_COMPACT_THRESHOLD   # 7

# Proposed A2 stratification bands (right-open except the last).
# Edit here if A1 shows the tail is thinner/fatter than expected.
BANDS = [(8, 10), (11, 15), (16, 9999)]

PROFILE_OUT = config.OUTPUT_DIR / "a1_cohort_profile.json"


def band_label(lo: int, hi: int) -> str:
    return f"{lo}+" if hi >= 9999 else f"{lo}-{hi}"


def main() -> None:
    config.check_paths()
    print()

    # ── Load + hadm_id dedup ──────────────────────────────
    df = pd.read_csv(
        config.SOURCE_CSV,
        parse_dates=["admittime", "dischtime"],
        low_memory=False,
    )
    df = df.rename(columns={config.SOURCE_TEXT_FIELD: "bhc_text"})

    n_raw = len(df)
    df = (
        df.assign(_wc=df["bhc_text"].str.split().str.len())
        .sort_values("_wc", ascending=False)
        .drop_duplicates(subset="hadm_id", keep="first")
        .drop(columns="_wc")
        .reset_index(drop=True)
    )
    print(f"Rows: {n_raw:,} raw -> {len(df):,} after hadm_id dedup "
          f"| {df['subject_id'].nunique():,} patients")

    # ── Admissions per patient ─────────────────────────────────────────────────
    n_admits = df.groupby("subject_id")["hadm_id"].nunique()

    # Full distribution, capped tail for readability
    print("\nAdmission-count distribution (all patients):")
    dist = n_admits.value_counts().sort_index()
    for k, v in dist.items():
        bar = "#" * min(int(v / max(dist.max(), 1) * 50), 50)
        marker = "  <- compaction starts" if k == COMPACT_THRESHOLD + 1 else ""
        print(f"  {k:>3} admissions : {v:>6,} {bar}{marker}")

    # ── The >threshold tail ────────────────────────────────────────────────────
    tail = n_admits[n_admits > COMPACT_THRESHOLD]
    print(f"\nPatients with > {COMPACT_THRESHOLD} admissions: {len(tail):,}")
    if len(tail):
        print(f"  range  : {int(tail.min())}-{int(tail.max())} admissions")
        print(f"  median : {tail.median():.0f}   mean : {tail.mean():.1f}")

    # ── Band counts for A2 ─────────────────────────────────────────────────────
    print("\nProposed A2 bands (compaction folds per patient ~ n - 7):")
    band_counts = {}
    for lo, hi in BANDS:
        n = int(((n_admits >= lo) & (n_admits <= hi)).sum())
        band_counts[band_label(lo, hi)] = n
        folds_lo = max(lo - COMPACT_THRESHOLD, 0)
        folds_hi = "many" if hi >= 9999 else hi - COMPACT_THRESHOLD
        print(f"  {band_label(lo, hi):>6} admissions : {n:>6,} patients "
              f"(~{folds_lo}-{folds_hi} folds each)")

    # ── Save profile ───────────────────────────────────────────────────────────
    profile = {
        "source_csv": str(config.SOURCE_CSV),
        "n_rows_raw": int(n_raw),
        "n_rows_dedup": int(len(df)),
        "n_patients": int(df["subject_id"].nunique()),
        "compact_threshold": COMPACT_THRESHOLD,
        "distribution": {int(k): int(v) for k, v in dist.items()},
        "tail_gt_threshold": {
            "n_patients": int(len(tail)),
            "min": int(tail.min()) if len(tail) else None,
            "max": int(tail.max()) if len(tail) else None,
            "median": float(tail.median()) if len(tail) else None,
            "mean": float(tail.mean()) if len(tail) else None,
        },
        "proposed_bands": band_counts,
    }
    PROFILE_OUT.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    print(f"\nSaved profile -> {PROFILE_OUT}")
    print("\nA1 done. Use the band counts above to set A2 cohort sizes.")


if __name__ == "__main__":
    main()
