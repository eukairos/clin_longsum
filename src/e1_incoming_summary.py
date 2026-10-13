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
e1_incoming_summary.py
======================
Single_summary (Tier 1) for the INCOMING (held-out) admission.

The coordinator builds the ESTABLISHED record over the non-holdout span; the
held-out admission (a2's is_holdout=True, also in cohort_holdout.csv) is the
incoming episode a physician reviews against that record. This step synthesises
the incoming admission's BHC into ONE faithful single-episode summary - the
editable Tier 1 draft surfaced in the review UI (raw BHC is shown alongside as
the source of truth).

In production the incoming summary would be synthesised from notes, 
not from a discharge BHC; here the holdout BHC stands in.

Input:  data/interim/cohort_admissions.csv (via cohort.load+holdout.split; promoted rows excluded) 
        (subject_id / hadm_id / admit_seq / admittime / service / bhc_text)
Output: data/interim/incoming_summary.jsonl  (one record per held-out admission)
        appends one AuditRecord per summary to generation_audit.jsonl

    cd C:~/Summarization/clin_longsum/src
    python e1_incoming_summary.py                       # this skips already-summarized rows
    python e1_incoming_summary.py --preview 90000029
    python e1_incoming_summary.py --limit 5
    python e1_incoming_summary.py --fresh
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

import pandas as pd

import config
import io_utils
from roles.contracts import AuditRecord
import cohort
import holdout

sys.path.insert(0, str(config.SYNTH_DIR))
import ollama_utils as ou                # noqa: E402  (shared chat retry / unload)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("e1_incoming_summary")

SPEC = config.model_for("single_summary")
PROMPT_VERSION = config.prompt_version("single_summary")
TEMP = SPEC.params.get("temperature", 0.2)
NUM_CTX = SPEC.params.get("num_ctx", 32768)

#HOLDOUT_CSV = config.INTERIM_DIR / "cohort_holdout.csv"
OUT_JSONL = config.INTERIM_DIR / "incoming_summary.jsonl"
AUDIT_JSONL = config.AUDIT_JSONL


# Shared VRAM / unload helpers (vendor/ollama_utils).
_gpu_cleanup = ou.gpu_cleanup


def _ollama_unload() -> None:
    ou.ollama_unload(SPEC.model_id)


# ── prompt (single_summary.v1) ──────────────────────────────────────────────────

_SYS = (
    "You are a clinical summarisation assistant. You write a faithful single-episode "
    "summary of ONE hospital admission for physician review. You invent nothing."
)

_TMPL = (
    "TASK: Write a concise single-episode summary of THIS admission for a physician to "
    "review. Cover the reason for admission, the key diagnoses/findings, major "
    "interventions or procedures, and the outcome/disposition.\n\n"
    "RULES:\n"
    "- Use ONLY the source below. Add no fact, date, medication, or finding not present.\n"
    "- Plain clinical prose, past tense. NO markdown headers, NO bullets, NO '#'/'*' "
    "characters - a reviewer edits this as plain text.\n"
    "- 1-2 short paragraphs. Summarise this admission only; do not re-narrate the "
    "patient's entire chronic history.\n\n"
    "ADMISSION {seq}{when} -- SOURCE:\n{bhc}\n\n"
    "Write only the summary."
)


def _chat(messages: list[dict]) -> str:
    # ou.chat_text retries once with think=False on empty output (thinking budget blowout).
    return ou.chat_text(SPEC.model_id, messages, think=SPEC.think,
                        options={"temperature": TEMP, "num_ctx": NUM_CTX}, keep_alive=-1)


def summarise_one(rec: dict) -> dict:
    when = ""
    if rec.get("admit_date"):
        when = f" ({rec['admit_date']}{', ' + rec['service'] if rec.get('service') else ''})"
    elif rec.get("service"):
        when = f" ({rec['service']})"
    user = _TMPL.format(seq=rec["admit_seq"], when=when, bhc=(rec["bhc_text"] or "").strip())
    msgs = [{"role": "system", "content": _SYS}, {"role": "user", "content": user}]
    summary = _chat(msgs)
    status = "ok" if summary.strip() else "flagged"
    audit = AuditRecord.from_spec(
        SPEC, PROMPT_VERSION, subject_id=rec["subject_id"], input_hadm_ids=[rec["hadm_id"]],
        output_text=summary,
        scores={"chars_in": len(rec["bhc_text"] or ""), "chars_out": len(summary)},
        status=status,
    )
    return {
        "subject_id": rec["subject_id"],
        "hadm_id": rec["hadm_id"],
        "admit_seq": rec["admit_seq"],
        "admit_date": rec.get("admit_date"),
        "service": rec.get("service"),
        "bhc_text": rec["bhc_text"],
        "summary_md": summary,
        "audit": json.loads(audit.model_dump_json()),
    }


_read_jsonl = io_utils.read_jsonl        # shared tolerant reader


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", type=int, default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--fresh", action="store_true", help="overwrite incoming_summary.jsonl")
    args = ap.parse_args()
    config.check_paths()

    if not config.HOLDOUT_LAST_ADMISSION:
        raise SystemExit("HOLDOUT_LAST_ADMISSION is False -- there is no incoming admission.")
    _, df = holdout.split(cohort.load())      # dayfirst-parsed once; promoted rows excluded
    df = df.rename(columns={"admittime": "_admit"})
    if df.empty:
        raise SystemExit("no held-out admissions -- check is_holdout in cohort_admissions.csv")
    
    recs = []
    for _, r in df.iterrows():
        recs.append({
            "subject_id": int(r["subject_id"]),
            "hadm_id": int(r["hadm_id"]),
            "admit_seq": int(r["admit_seq"]) if pd.notna(r.get("admit_seq")) else None,
            "admit_date": None if pd.isna(r["_admit"]) else r["_admit"].strftime("%Y-%m-%d"),
            "service": str(r["service"]) if pd.notna(r.get("service")) else None,
            "bhc_text": str(r["bhc_text"]) if pd.notna(r.get("bhc_text")) else "",
        })

    if args.preview is not None:
        recs = [x for x in recs if x["subject_id"] == args.preview]
    if args.limit:
        recs = recs[:args.limit]

    existing = {} if args.fresh else {
        (int(r["subject_id"]), int(r["hadm_id"])): r for r in _read_jsonl(OUT_JSONL)}
    done = {k for k, r in existing.items() if (r.get("summary_md") or "").strip()}

    results, audits = [], []
    try:
        for rec in recs:
            key = (rec["subject_id"], rec["hadm_id"])
            if key in done:
                continue
            if not rec["bhc_text"].strip():
                continue
            out = summarise_one(rec)
            results.append(out)
            audits.append(out["audit"])
            logger.info("subject=%s seq=%s hadm=%s  chars_out=%d", rec["subject_id"],
                        rec["admit_seq"], rec["hadm_id"], len(out["summary_md"]))
    finally:
        _ollama_unload()

    if args.preview is not None and results:
        print("\n## Incoming summary\n" + results[0]["summary_md"])
        return

    # Merge over existing, dedup by (subject_id, hadm_id).
    merged = dict(existing)
    for out in results:
        merged[(out["subject_id"], out["hadm_id"])] = out
    with OUT_JSONL.open("w", encoding="utf-8") as fh:
        for k in sorted(merged):
            fh.write(json.dumps(merged[k]) + "\n")
    with AUDIT_JSONL.open("a", encoding="utf-8") as fh:
        for a in audits:
            fh.write(json.dumps(a) + "\n")

    print("\n" + "=" * 55)
    print("E1 INCOMING SUMMARY (single_summary)")
    print(f"  summaries written : {len(results)}")
    if results:
        print(f"  mean length       : {sum(len(o['summary_md']) for o in results)//len(results)} chars")
    print(f"  results -> {OUT_JSONL}")
    print("\nNEXT: e2_embed_incoming.py.")


if __name__ == "__main__":
    main()
