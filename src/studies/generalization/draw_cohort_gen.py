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
draw_cohort_gen.py  --  EVAL S11 redux, item 5 (GENERALISATION): draw the unseen cohort
=======================================================================================
Draws a DISJOINT cohort of NEW patients (subject_ids not in the dev cohort) from the
same >7-admission stratified tail, with a DIFFERENT seed, into the generalisation
namespace. The frozen pipeline is then run end-to-end over this cohort (no tuning) and
scored, and dev-vs-unseen is compared (compare_generalization.py) - a large gap is
overfitting to the dev cohort. This is a BETWEEN-patient holdout (distinct from e1's
within-patient incoming admission).

Reuses a2_build_cohort's dedup + SNOMED machinery VERBATIM (import, no fork). The only
differences from a2: (1) exclude the dev subject_ids, (2) a new seed, (3) a smaller N.

SAFETY: refuses to run UNLESS the LONGSUM_NAMESPACE env var is set (e.g. "gen"), so it
can never overwrite the dev cohort_admissions.csv. Under the namespace, config.INTERIM_DIR
already points at data_<ns>/interim, and the files are written with the STANDARD names the
frozen pipeline reads (cohort_admissions.csv, ...), so no stage needs editing.

    cd C:~/clin_longsum/src
    set LONGSUM_NAMESPACE=gen            # Windows (PowerShell: $env:LONGSUM_NAMESPACE="gen")
    python studies/generalization/draw_cohort_gen.py --n 16 --seed 2026
    python studies/generalization/draw_cohort_gen.py --n 16 --k 2     # hold out k trailing admissions per patient (k>=2 = production-replay pool; k=1 default)
    python studies/generalization/draw_cohort_gen.py --exclude-cohort <path>/cohort_patients.csv --exclude-cohort <path2>/cohort_patients.csv
                                                                      # repeatable; exclude every SEEN cohort (dev, test, earlier gen draws).
                                                                      # default: <LONGSUM_DEV_DATA_ROOT>/data/interim/cohort_patients.csv only
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # tiered_longsum/ on the path

import config
import a2_build_cohort as a2

DEV_PATIENTS = config.data_dir(root=config.DEV_DATA_ROOT) / "interim" / "cohort_patients.csv"
ADMISSION_COLS = [
    "hadm_id", "subject_id", "admit_seq", "admittime", "dischtime",
    "service", "admission_type", "discharge_location", "hospital_expire_flag",
    "is_holdout", "bhc_text", "bhc_delta", "dedup_jaccard_to_prev",
]


def main() -> int:
    ap = argparse.ArgumentParser(description="Draw the disjoint unseen generalisation cohort.")
    ap.add_argument("--n", type=int, default=16, help="total patients (split 50/50 across bands)")
    ap.add_argument("--seed", type=int, default=2026, help="NEW seed (must differ from a2's 42)")
    ap.add_argument("--k", type=int, default=1,
                    help="trailing admissions held out per patient. k=1 = the original single-"
                         "admission holdout (overfitting eval). k>=2 = the production-replay pool "
                         "(needed to surface the d1 demotion gap). Use a SEPARATE namespace for a "
                         "k>=2 draw so it doesn't clobber the k=1 eval cohort.")
    ap.add_argument("--exclude-cohort", type=Path, action="append", default=None,
                    metavar="COHORT_PATIENTS_CSV",
                    help="path to a cohort_patients.csv whose subject_ids must be EXCLUDED from "
                         "the draw. Repeatable. Default: the single file resolved from "
                         "LONGSUM_DEV_DATA_ROOT. Pass this explicitly when the seen cohorts live "
                         "under namespaced data dirs (data_test/, data_gen_prod/), which the "
                         "DEV_DATA_ROOT default cannot reach -- it only looks in <root>/data/.")
    args = ap.parse_args()

    if not config.RUN_NAMESPACE:
        raise SystemExit(
            "REFUSING to run in the default namespace -- this would overwrite the dev cohort.\n"
            "Set LONGSUM_NAMESPACE first, e.g.  set LONGSUM_NAMESPACE=gen  (PowerShell: "
            '$env:LONGSUM_NAMESPACE="gen")  then re-run.')
    if args.seed == a2.SEED:
        raise SystemExit(f"--seed must differ from a2's SEED ({a2.SEED}) so the draw is independent.")
    config.check_paths()
    print()

    excl_paths = list(args.exclude_cohort) if args.exclude_cohort else [DEV_PATIENTS]
    dev_ids: set[int] = set()
    for p in excl_paths:
        if not Path(p).exists():
            raise SystemExit(
                f"exclusion cohort not found at {p}.\n"
                "Pass --exclude-cohort <path to cohort_patients.csv> for every cohort the "
                "pipeline has already SEEN (dev, test, any earlier gen draw), or set "
                "LONGSUM_DEV_DATA_ROOT so <root>/data/interim/cohort_patients.csv resolves.")
        ids = set(pd.read_csv(p)["subject_id"].astype(int).tolist())
        print(f"excluding {len(ids):>4} subject_ids from {p}")
        dev_ids |= ids
    print(f"excluding {len(dev_ids)} distinct subject_ids in total")

    df = pd.read_csv(config.SOURCE_CSV, parse_dates=["admittime", "dischtime"], low_memory=False)
    df = df.rename(columns={config.SOURCE_TEXT_FIELD: "bhc_text"})
    df = (df.assign(_wc=df["bhc_text"].str.split().str.len())
            .sort_values("_wc", ascending=False)
            .drop_duplicates(subset="hadm_id", keep="first")
            .drop(columns="_wc").reset_index(drop=True))
    print(f"Loaded {len(df):,} admissions | {df['subject_id'].nunique():,} patients")

    n_admits = df.groupby("subject_id")["hadm_id"].nunique()
    per_band = max(args.n // 2, 1)
    bands = [(8, 10, per_band), (11, 15, args.n - per_band)]
    rng = np.random.default_rng(args.seed)
    sampled = []
    for lo, hi, want in bands:
        pool = n_admits[(n_admits >= lo) & (n_admits <= hi)].index.to_numpy()
        pool = np.array([p for p in pool if int(p) not in dev_ids])   # DISJOINT from dev
        take = min(want, len(pool))
        if take < want:
            print(f"  WARNING: band {lo}-{hi} has only {len(pool)} unseen patients (<{want})")
        sampled.extend(rng.choice(pool, size=take, replace=False).tolist())
        print(f"  band {lo}-{hi}: sampled {take} of {len(pool)} unseen")
    sampled = sorted(sampled)
    assert not (set(map(int, sampled)) & dev_ids), "draw overlaps the dev cohort"

    cohort = df[df["subject_id"].isin(sampled)].copy()
    cohort = cohort.sort_values(["subject_id", "admittime"]).reset_index(drop=True)
    cohort["admit_seq"] = cohort.groupby("subject_id").cumcount() + 1
    if args.k < 1:
        raise SystemExit("--k must be >= 1")
    max_seq = cohort.groupby("subject_id")["admit_seq"].transform("max")
    cohort["is_holdout"] = cohort["admit_seq"] > (max_seq - args.k)
    # keep >= 2 established admissions per patient (a hot-window admission + >=1 aged one),
    # else the held-out span leaves nothing meaningful to fold into.
    too_small = sorted(cohort.loc[(max_seq - args.k) < 2, "subject_id"].unique().tolist())
    if too_small:
        print(f"  WARNING: {len(too_small)} patient(s) keep <2 established admissions at "
              f"k={args.k}: {too_small[:8]}{'...' if len(too_small) > 8 else ''}")

    # SNOMED per patient (non-holdout), parity with a2
    conditions_json_by_pid = {}
    if a2.DIAGNOSES_CSV.exists():
        dx = pd.read_csv(a2.DIAGNOSES_CSV)
        noh = set(cohort[~cohort["is_holdout"]]["hadm_id"].unique())
        pids = set(cohort["subject_id"].unique())
        dxc = dx[dx["hadm_id"].isin(noh) & dx["subject_id"].isin(pids)].copy()
        dxc["snomed"] = pd.to_numeric(dxc["snomed"], errors="coerce")
        dxc = dxc.dropna(subset=["snomed"])
        dxc["snomed"] = dxc["snomed"].astype(int)
        for sid, grp in dxc.groupby("subject_id"):
            conditions_json_by_pid[int(sid)] = sorted(grp["snomed"].unique().tolist())

    # bhc_delta (reuse a2's verbatim dedup)
    all_dedup = []
    for sid, grp in cohort.sort_values(["subject_id", "admittime"]).groupby("subject_id"):
        all_dedup.extend(a2.dedup_patient_bhc(grp[["hadm_id", "bhc_text"]].to_dict("records")))
    dedup_df = pd.DataFrame(all_dedup)

    out = cohort.merge(
        dedup_df[["hadm_id", "bhc_delta", "dedup_jaccard_to_prev", "n_sent_in", "n_sent_kept"]],
        on="hadm_id", how="left").rename(columns={"curr_service": "service"})
    admissions_out = out[[c for c in ADMISSION_COLS if c in out.columns]].copy()

    pat = cohort.groupby("subject_id").agg(n_admissions=("hadm_id", "count")).reset_index()
    pat["conditions_json"] = pat["subject_id"].apply(
        lambda s: json.dumps(conditions_json_by_pid.get(int(s), [])))
    patients_out = pat[["subject_id", "n_admissions", "conditions_json"]].copy()

    config.INTERIM_DIR.mkdir(parents=True, exist_ok=True)
    admissions_out.to_csv(config.INTERIM_DIR / "cohort_admissions.csv", index=False)
    patients_out.to_csv(config.INTERIM_DIR / "cohort_patients.csv", index=False)
    admissions_out[admissions_out["is_holdout"]].to_csv(
        config.INTERIM_DIR / "cohort_holdout.csv", index=False)

    print("\n" + "=" * 55)
    print(f"UNSEEN GENERALISATION COHORT (namespace '{config.RUN_NAMESPACE}')")
    print(f"  patients   : {patients_out['subject_id'].nunique()}  (disjoint from dev)")
    print(f"  admissions : {len(admissions_out)}")
    print(f"  holdout    : k={args.k}  ({int(admissions_out['is_holdout'].sum())} discharge(s) "
          f"across {admissions_out[admissions_out['is_holdout']]['subject_id'].nunique()} patients)")
    print(f"  per patient: min={pat['n_admissions'].min()} "
          f"median={pat['n_admissions'].median():.0f} max={pat['n_admissions'].max()}")
    print(f"  -> {config.INTERIM_DIR / 'cohort_admissions.csv'}")
    if args.k == 1:
        print("\nNEXT (overfitting eval): run the FILE-ONLY frozen pipeline under this namespace "
              "(b1 -> b2 -> b4 -> c1 -> c3 -> c4 -> c2 -> d1 -> d2 -> d_coordinator -> f1/f4/f5), "
              "then compare_generalization.py. Do NOT run a4 / db_persist_* / d3 / e2 here.")
    else:
        print(f"\nNEXT (production replay, k={args.k}): setup the ESTABLISHED span into an ISOLATED "
              "DB (b1..d3 + a4 embed-established + db_persist_*; NOT e2/fold-in), then "
              "studies/generalization/replay_production.py to stream the held-out discharges "
              "through foldin_patient. Point .env at longitudinal_bhc_gen, NOT the dev DB.")
    print("See studies/generalization/README.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
