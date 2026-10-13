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
a2_build_cohort.py
==================
Phase A2 + A3 - draw the >7-admission cohort and computes bhc_delta.

Draws a stratified 100-patient cohort capped at 15 admissions, assigns
admit_seq, flags the holdout (last) admission, collects per-patient SNOMED
codes, and computes bhc_delta (sentence-level cross-admission deduplication).

Stratified sample (50/50, not proportional): a POC must exercise BOTH fold
depths, so deep-fold cases are not thinned out. Per-band targets below.

Deduplication of clinical text before generating embeddings helps in future RAG uses, 
because clinical text often contains cut-and-paste material copied across multiple admissions.

READ-ONLY on source; writes cohort tables to tiered_longsum/data/interim.

Run on the workstation:
    cd C:~/Summarization/clin_longsum/src
    python a2_build_cohort.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import config

# datasketch + tqdm 
for pkg in ["datasketch", "tqdm"]:
    try:
        __import__(pkg)
    except ImportError:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", pkg, "--break-system-packages"],
            check=True,
        )
from datasketch import MinHash          # noqa: E402
from tqdm import tqdm                   # noqa: E402

# ── Constants ────────────────────────────────────────────────────────────────
SEED = 42
CAP_MAX = 15                            # cap admissions for the POC
# (band_lo, band_hi_inclusive, n_to_sample)
BANDS = [(8, 10, 50), (11, 15, 50)]     # 50/50 split, 100 total

# Dedup parameters -- copied from NB_LONG_01 (do not change independently).
MINHASH_THRESH = 0.85
MINHASH_PERM = 128
MINHASH_NGRAM = 3
DELTA_MIN_WORDS = 50
NEGATION_RE = re.compile(r"\b(no|without|denies|absent)\b", re.IGNORECASE)

SHARED_DATA = config.SOURCE_CSV.parent
DIAGNOSES_CSV = SHARED_DATA / "mimic_diagnoses_small.csv"

INTERIM = config.INTERIM_DIR
ADMISSIONS_OUT = INTERIM / "cohort_admissions.csv"
PATIENTS_OUT = INTERIM / "cohort_patients.csv"
HOLDOUT_OUT = INTERIM / "cohort_holdout.csv"
# Written by src/ingest/ingest_project.py the first time it projects EHR admissions into
# this cohort. From then on the CSV holds rows that are NOT in SOURCE_CSV, so a rebuild
# would silently drop them from every stage's view (the DB would still hold them).
INGEST_MARKER = INTERIM / "INGEST_ACTIVE"


def _refuse_if_ingest_active() -> None:
    if INGEST_MARKER.exists() and os.environ.get("LONGSUM_ALLOW_A2_REBUILD") != "1":
        raise SystemExit(
            f"REFUSING to rebuild {ADMISSIONS_OUT}: the production ingest has projected EHR "
            f"admissions into it ({INGEST_MARKER.name} present). A rebuild from SOURCE_CSV "
            f"would drop them. If you really mean to discard them, set "
            f"LONGSUM_ALLOW_A2_REBUILD=1 and re-run.")

# ── Dedup helpers  ──────────────────────────────────
_CP1252 = {
    "\x91": "'", "\x92": "'", "\x93": '"', "\x94": '"',
    "\x96": "-", "\x97": "-", "\xa0": " ", "\u200b": "",
}


def clean_bhc_text(text: str, artefacts: bool = True) -> str:
    if not text or not isinstance(text, str):
        return ""
    for old, new in _CP1252.items():
        text = text.replace(old, new)
    text = text.encode("ascii", errors="ignore").decode("ascii")
    if artefacts:
        text = re.sub(r"\[\*\*.*?\*\*\]", " ", text)
        text = re.sub(r"(\s|^)\*{1,2}([A-Za-z]+)",
                      lambda m: " " + m.group(2), text)
        text = re.sub(r"(?m)^[ \t]*[-=*_#~]{4,}[ \t]*$", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def sentence_tokenize(text: str) -> list:
    text = re.sub(r"(?<!\n)(?=#\s+[A-Z])", "\n", text)
    lines = [l.strip() for l in re.split(r"\n+", text) if l.strip()]
    sentences = []
    for line in lines:
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", line)
        sentences.extend(p.strip() for p in parts if p.strip())
    return sentences


def md5_of(text: str) -> str:
    return hashlib.md5(text.strip().lower().encode("utf-8")).hexdigest()


def make_minhash(text: str, num_perm: int = MINHASH_PERM, n: int = MINHASH_NGRAM) -> MinHash:
    m = MinHash(num_perm=num_perm)
    words = text.lower().split()
    if len(words) < n:
        for w in words:
            m.update(w.encode("utf-8"))
    else:
        for i in range(len(words) - n + 1):
            m.update(" ".join(words[i:i + n]).encode("utf-8"))
    return m


def word_jaccard(a: str, b: str) -> float:
    wA, wB = set(a.lower().split()), set(b.lower().split())
    union = wA | wB
    return len(wA & wB) / len(union) if union else 0.0


def dedup_patient_bhc(admissions_sorted: list) -> list:
    """Sentence-level BHC dedup for one patient, chronological order."""
    seen_md5 = set()
    prev_minhashes = []
    prev_cleaned = None
    results = []
    for adm in admissions_sorted:
        cleaned = clean_bhc_text(adm["bhc_text"])
        jaccard_to_prev = word_jaccard(cleaned, prev_cleaned) if prev_cleaned is not None else None
        sentences = sentence_tokenize(cleaned)
        kept = []
        for sent in sentences:
            if NEGATION_RE.search(sent):           # never suppress negations
                kept.append(sent)
                h = md5_of(sent)
                if h not in seen_md5:
                    seen_md5.add(h)
                    if len(sent.split()) >= 2:
                        prev_minhashes.append(make_minhash(sent))
                continue
            h = md5_of(sent)
            if h in seen_md5:                       # Type 1: verbatim
                continue
            if len(sent.split()) >= MINHASH_NGRAM and prev_minhashes:   # Type 2: near-dup
                mh = make_minhash(sent)
                best_j = max((mh.jaccard(pm) for pm in prev_minhashes), default=0.0)
                if best_j >= MINHASH_THRESH:
                    continue
            kept.append(sent)
            seen_md5.add(h)
            if len(sent.split()) >= 2:
                prev_minhashes.append(make_minhash(sent))
        bhc_delta = " ".join(kept).strip()
        if len(bhc_delta.split()) < DELTA_MIN_WORDS:
            bhc_delta = cleaned                     # fallback for thin deltas
        prev_cleaned = cleaned
        results.append({
            "hadm_id": adm["hadm_id"],
            "bhc_delta": bhc_delta,
            "dedup_jaccard_to_prev": jaccard_to_prev,
            "n_sent_in": len(sentences),
            "n_sent_kept": len(kept),
        })
    return results


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    config.check_paths()
    _refuse_if_ingest_active()
    print()

    # Load + hadm_id dedup 
    df = pd.read_csv(config.SOURCE_CSV, parse_dates=["admittime", "dischtime"],
                     low_memory=False)
    df = df.rename(columns={config.SOURCE_TEXT_FIELD: "bhc_text"})
    df = (
        df.assign(_wc=df["bhc_text"].str.split().str.len())
        .sort_values("_wc", ascending=False)
        .drop_duplicates(subset="hadm_id", keep="first")
        .drop(columns="_wc")
        .reset_index(drop=True)
    )
    print(f"Loaded {len(df):,} admissions | {df['subject_id'].nunique():,} patients")

    # Stratified sample per band
    n_admits = df.groupby("subject_id")["hadm_id"].nunique()
    rng = np.random.default_rng(SEED)
    sampled_ids = []
    for lo, hi, want in BANDS:
        pool = n_admits[(n_admits >= lo) & (n_admits <= hi)].index.to_numpy()
        take = min(want, len(pool))
        if take < want:
            print(f"  WARNING: band {lo}-{hi} has only {len(pool)} patients (<{want})")
        chosen = rng.choice(pool, size=take, replace=False)
        sampled_ids.extend(chosen.tolist())
        print(f"  band {lo}-{hi}: sampled {take} of {len(pool)}")
    sampled_ids = sorted(sampled_ids)

    cohort = df[df["subject_id"].isin(sampled_ids)].copy()
    print(f"Cohort: {cohort['subject_id'].nunique()} patients, {len(cohort)} admissions")

    # admit_seq + holdout (last admission), 
    cohort = cohort.sort_values(["subject_id", "admittime"]).reset_index(drop=True)
    cohort["admit_seq"] = cohort.groupby("subject_id").cumcount() + 1
    max_seq = cohort.groupby("subject_id")["admit_seq"].transform("max")
    cohort["is_holdout"] = cohort["admit_seq"] == max_seq
    assert cohort.groupby("subject_id")["is_holdout"].sum().eq(1).all(), \
        "Expected exactly 1 holdout admission per patient"

    # SNOMED per patient (non-holdout admissions), for conditions_json parity.
    # NOTE: mimic_diagnoses_small.csv already carries subject_id, so DO NOT merge
    # cohort[subject_id] in (that creates subject_id_x/_y and drops the plain
    # column). Filter by cohort hadm_ids and group on the file's own subject_id;
    # restrict to this cohort's patients afterwards.
    conditions_json_by_pid = {}
    if DIAGNOSES_CSV.exists():
        dx = pd.read_csv(DIAGNOSES_CSV)
        noh_hadm_ids = set(cohort[~cohort["is_holdout"]]["hadm_id"].unique())
        cohort_pids = set(cohort["subject_id"].unique())
        dxc = dx[dx["hadm_id"].isin(noh_hadm_ids) & dx["subject_id"].isin(cohort_pids)].copy()
        dxc["snomed"] = pd.to_numeric(dxc["snomed"], errors="coerce")
        dxc = dxc.dropna(subset=["snomed"])
        dxc["snomed"] = dxc["snomed"].astype(int)
        for sid, grp in dxc.groupby("subject_id"):
            conditions_json_by_pid[int(sid)] = sorted(grp["snomed"].unique().tolist())
        print(f"SNOMED: codes collected for {len(conditions_json_by_pid)} patients")
    else:
        print(f"  NOTE: {DIAGNOSES_CSV.name} not found -- conditions_json left empty")

    # bhc_delta per patient
    all_dedup = []
    for sid, grp in tqdm(cohort.sort_values(["subject_id", "admittime"]).groupby("subject_id"),
                         total=cohort["subject_id"].nunique(), desc="Computing bhc_delta"):
        recs = grp[["hadm_id", "bhc_text"]].to_dict("records")
        all_dedup.extend(dedup_patient_bhc(recs))
    dedup_df = pd.DataFrame(all_dedup)

    # Merge + finalise columns.
    # dedup_df keys on hadm_id only and shares no other column name with cohort,
    # so this merge cannot collide on subject_id.
    out = cohort.merge(
        dedup_df[["hadm_id", "bhc_delta", "dedup_jaccard_to_prev", "n_sent_in", "n_sent_kept"]],
        on="hadm_id", how="left",
    ).rename(columns={"curr_service": "service"})

    ADMISSION_COLS = [
        "hadm_id", "subject_id", "admit_seq", "admittime", "dischtime",
        "service", "admission_type", "discharge_location", "hospital_expire_flag",
        "is_holdout", "bhc_text", "bhc_delta", "dedup_jaccard_to_prev",
    ]
    admissions_out = out[ADMISSION_COLS].copy()

    pat = (cohort.groupby("subject_id").agg(n_admissions=("hadm_id", "count")).reset_index())
    pat["conditions_json"] = pat["subject_id"].apply(
        lambda s: json.dumps(conditions_json_by_pid.get(int(s), []))
    )
    patients_out = pat[["subject_id", "n_admissions", "conditions_json"]].copy()

    INTERIM.mkdir(parents=True, exist_ok=True)
    admissions_out.to_csv(ADMISSIONS_OUT, index=False)
    patients_out.to_csv(PATIENTS_OUT, index=False)
    admissions_out[admissions_out["is_holdout"]].to_csv(HOLDOUT_OUT, index=False)

    # QC
    print("\n" + "=" * 55)
    print("COHORT OVERVIEW")
    print(f"  Patients         : {patients_out['subject_id'].nunique()}")
    print(f"  Admissions       : {len(admissions_out)}")
    print(f"  Per patient      : min={pat['n_admissions'].min()} "
          f"median={pat['n_admissions'].median():.0f} max={pat['n_admissions'].max()}")
    print(f"  Compaction folds : "
          f"{int((pat['n_admissions'] - config.N_COMPACT_THRESHOLD).clip(lower=0).sum())} "
          f"total across cohort (~n-{config.N_COMPACT_THRESHOLD} per patient)")
    j = admissions_out["dedup_jaccard_to_prev"].dropna()
    print(f"  Dedup Jaccard    : median={j.median():.3f} "
          f"high(>=0.7)={int((j >= 0.7).sum())} admissions")
    print(f"\nSaved:\n  {ADMISSIONS_OUT}\n  {PATIENTS_OUT}\n  {HOLDOUT_OUT}")
    print("\nA2/A3 done. Ready for A4 (DB load + embed) or Phase B (retention extractor).")


if __name__ == "__main__":
    main()
