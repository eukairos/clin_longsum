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
e2_embed_incoming.py - embeds the incoming admission's bhc_delta for future RAG retrieval.

a2_build_cohort.py already computes bhc_delta (sentence-level cross-admission dedup) 
for every cohort admission and writes it to cohort_admissions.csv. 
This stage reads the incoming (holdout) admission's PRECOMPUTED
bhc_delta, embeds it with Snowflake arctic-embed, and writes the vector to
admissions.embedding (pgvector) in the longitudinal_bhc DB.

One vector per incoming admission (incremental, not bulk). Skips already-embedded
admissions unless --force. The dedup makes the embedding reflect what is new this
admission, which improves RAG precision.

    cd C:~/Summarization/clin_longsum/src
    python e2_embed_incoming.py
    python e2_embed_incoming.py --subject 90000123        # one patient
    python e2_embed_incoming.py --force                   # re-embed
    python e2_embed_incoming.py --hadm 80000123           # one admission only (fold-in: the just-approved discharge)
"""

from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd

import config
from database.pg import get_conn as _pg_get_conn

sys.path.insert(0, str(config.SYNTH_DIR))
import ollama_utils as ou                # noqa: E402  (shared VRAM cleanup)

COHORT_CSV = config.INTERIM_DIR / "cohort_admissions.csv"
EMBED_MODEL = "Snowflake/snowflake-arctic-embed-l-v2.0"


def get_conn():
    """Shared connection (database.pg) with pgvector registered."""
    return _pg_get_conn(vector=True)


_gpu_cleanup = ou.gpu_cleanup


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("true", "1", "t", "yes")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", type=int, default=None)
    ap.add_argument("--hadm", type=int, default=None,
                    help="embed only this admission (fold-in: the just-approved discharge)")
    ap.add_argument("--force", action="store_true", help="re-embed even if already present")
    args = ap.parse_args()

    df = pd.read_csv(COHORT_CSV)
    if "bhc_delta" not in df.columns:
        raise SystemExit(f"{COHORT_CSV} has no bhc_delta column -- run a2_build_cohort.py first.")
    df["is_holdout"] = df["is_holdout"].map(_truthy)
    holdout = df[df["is_holdout"]].copy()
    if args.subject is not None:
        holdout = holdout[holdout["subject_id"] == args.subject]
        if holdout.empty:
            raise SystemExit(f"subject {args.subject} has no holdout admission in cohort.")
    if args.hadm is not None:
        holdout = holdout[holdout["hadm_id"].astype(int) == args.hadm]
        if holdout.empty:
            raise SystemExit(f"hadm {args.hadm} is not a holdout admission in the cohort.")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT hadm_id FROM admissions WHERE embedding IS NOT NULL "
                "AND hadm_id = ANY(%s);", (holdout["hadm_id"].astype(int).tolist(),))
    already = {r[0] for r in cur.fetchall()}
    cur.close()

    todo = holdout if args.force else holdout[~holdout["hadm_id"].astype(int).isin(already)]
    print(f"incoming admissions: {len(holdout)}  already embedded: {len(already)}  "
          f"to embed: {len(todo)}")
    if todo.empty:
        conn.close()
        print("nothing to embed.")
        return

    try:
        from sentence_transformers import SentenceTransformer
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = SentenceTransformer(EMBED_MODEL, device=device)   # no trust_remote_code
        model.eval()
        n = 0
        for _, row in todo.iterrows():
            hadm_id = int(row["hadm_id"])
            text = row["bhc_delta"]
            if not isinstance(text, str) or not text.strip():     # fallback to full BHC
                text = str(row.get("bhc_text") or "")
            with torch.no_grad():
                vec = model.encode(text, normalize_embeddings=True).astype(np.float32)
            conn.rollback()
            cur = conn.cursor()
            cur.execute("UPDATE admissions SET embedding = %s WHERE hadm_id = %s;",
                        (vec, hadm_id))
            if cur.rowcount == 0:
                print(f"  [WARN] hadm {hadm_id} not in admissions table -- not written")
            else:
                n += 1
                print(f"  embedded hadm {hadm_id}")
            conn.commit()
            cur.close()
        del model
    finally:
        _gpu_cleanup()

    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM admissions WHERE embedding IS NOT NULL;")
    total = cur.fetchone()[0]
    cur.close()
    conn.close()
    print(f"embedded {n} incoming admission(s). total embedded rows in admissions: {total}")
    print('\nNext - d_coordinator.py')
    print("VRAM released.")


if __name__ == "__main__":
    main()
