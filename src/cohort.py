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
cohort.py
=========
Single loader for data/interim/cohort_admissions.csv, so every stage sees the SAME
parsed dates. The cohort is a Singapore-locale DAY-FIRST export (D/M/YYYY); the old
str(admittime)[:10] slicing (formerly in c1/d1) left malformed day-first slop
("15/6/2175") in prompts and stored artifacts. Here admittime/dischtime are parsed
ONCE (dayfirst=True, format="mixed") and served as ISO 'YYYY-MM-DD' strings, so
lexical min/max ordering == chronological ordering everywhere.

Replaces: c1._cohort_by_hadm, c3._delta_by_hadm, b2._hadm_dates, and the ad-hoc
[:10] slices in d1. c1 keeps a thin back-compat alias (_cohort_by_hadm) for c4 and
the evaluation scripts.
"""

from __future__ import annotations

import pandas as pd

import config

COHORT_CSV = config.INTERIM_DIR / "cohort_admissions.csv"


def load() -> pd.DataFrame:
    """Full cohort DataFrame with admittime/dischtime parsed to Timestamps
    (dayfirst; ISO inputs unaffected). Raises SystemExit if the CSV is absent."""
    if not COHORT_CSV.exists():
        raise SystemExit(f"{COHORT_CSV} not found. Run a2_build_cohort.py first.")
    df = pd.read_csv(COHORT_CSV)
    for c in ("admittime", "dischtime"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], format="mixed", dayfirst=True, errors="coerce")
    return df


def _iso(ts):
    return None if pd.isna(ts) else pd.Timestamp(ts).strftime("%Y-%m-%d")


def by_hadm() -> dict[int, dict]:
    """hadm_id -> {subject_id, admit_seq, admit_date, discharge_date, service,
    bhc_delta, bhc_text} with ISO dates (or None)."""
    out: dict[int, dict] = {}
    for r in load().itertuples(index=False):
        out[int(r.hadm_id)] = {
            "subject_id": int(r.subject_id),
            "admit_seq": int(r.admit_seq),
            "admit_date": _iso(r.admittime),
            "discharge_date": _iso(getattr(r, "dischtime", None)),
            "service": str(r.service) if pd.notna(r.service) else None,
            "bhc_delta": str(r.bhc_delta) if pd.notna(getattr(r, "bhc_delta", None)) else None,
            "bhc_text": str(r.bhc_text) if pd.notna(getattr(r, "bhc_text", None)) else None,
        }
    return out


def hadm_dates() -> dict[int, str | None]:
    """hadm_id -> ISO admit date (b2 onset/last_confirmed derivation). Returns {}
    if the cohort CSV is absent (b2's original tolerance)."""
    if not COHORT_CSV.exists():
        return {}
    return {h: m["admit_date"] for h, m in by_hadm().items()}
