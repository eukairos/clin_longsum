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
d2_tldr.py
==========
Tldr_synthesizer (Tier 2b). Re-synthesize the TL;DR over the whole
current picture: the compacted Prior Admissions paragraph (C1) PLUS the
hot-window per-admission paragraphs (D1). Inputs are PARAGRAPHS, not raw facts. 
Runs per patient (TLDR_REGENERATE_EVERY_ADMISSION).

Input:  data/interim/compaction_results.jsonl (c1; compaction_corrected.jsonl
        preferred when present), data/interim/episode_paragraphs.jsonl (d1),
        data/interim/cohort_admissions.csv (n_total + date span)
Output: data/interim/tldr_results.jsonl  (one TldrSynthesizerOutput per patient)
        appends one AuditRecord per TL;DR to generation_audit.jsonl

    cd ~/Summarization/clin_longsum/src
    python d2_tldr.py
    python d2_tldr.py --preview 90000029
    python d2_tldr.py --subject 90000029          # fold-in: recompute ONE subject, MERGE in place
    python d2_tldr.py --limit 5 --fresh
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

import pandas as pd

import cohort
import config
import holdout
import io_utils
from roles.contracts import (AuditRecord, EpisodeParagraph, TldrSynthesizerInput,
                             TldrSynthesizerOutput)

sys.path.insert(0, str(config.SYNTH_DIR))
import ollama_utils as ou                # noqa: E402  (shared chat retry / unload)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("d2_tldr")

SPEC = config.model_for("tldr_synthesizer")
PROMPT_VERSION = config.prompt_version("tldr_synthesizer")
TEMP = SPEC.params.get("temperature", 0.2)
NUM_CTX = SPEC.params.get("num_ctx", 32768)

COMPACTION_JSONL = config.COMPACTION_JSONL
CORRECTED_JSONL = config.INTERIM_DIR / "compaction_corrected.jsonl"
PARAGRAPHS_JSONL = config.EPISODE_PARAGRAPHS_JSONL
COHORT_CSV = config.INTERIM_DIR / "cohort_admissions.csv"
OUT_JSONL = config.TLDR_JSONL
AUDIT_JSONL = config.AUDIT_JSONL


# Shared VRAM / unload helpers (vendor/ollama_utils).
_gpu_cleanup = ou.gpu_cleanup


def _ollama_unload() -> None:
    ou.ollama_unload(SPEC.model_id)


# ── prompt (tldr_synthesizer.v1 -- NB_LONG_02 TL;DR instruction verbatim) ────────

_SYS = "You are a clinical summarization assistant. Write the TL;DR of a longitudinal patient summary."

# Instruction + rule + example.
_TMPL = (
    "Write the TL;DR for this patient's longitudinal record.\n\n"
    "## TL;DR\n"
    "[short, succinct paragraph: overall clinical story, dominant condition pattern, "
    "and trajectory]\n\n"
    "Rules:\n"
    "- Use only information present in the provided material; do not invent any details.\n"
    "- One short, succinct paragraph. Plain prose -- no headers, no bullets.\n\n"
    "Example TL;DR (style reference only):\n"
    "Patient with known Type 2 diabetes mellitus, hypertension, and CKD stage 3 had 2 "
    "admissions over 18 days, initially for hyperglycaemia with acute-on-chronic kidney "
    "injury and subsequently for community-acquired pneumonia. Renal function returned to "
    "near baseline after the first admission; the second admission was uncomplicated with "
    "good response to antibiotics. Overall trajectory is stable with ongoing metabolic and "
    "renal comorbidity burden.\n\n"
    "--- PATIENT MATERIAL ---\n"
    "{n} admissions ({first} to {last})\n\n"
    "PRIOR ADMISSIONS (older, compacted):\n{prior}\n\n"
    "RECENT ADMISSIONS (chronological):\n{hot}\n\n"
    "Write only the TL;DR paragraph."
)


def _hot_block(paras: list[EpisodeParagraph]) -> str:
    if not paras:
        return "(none)"
    out = []
    for p in sorted(paras, key=lambda x: x.admit_seq):
        when = f" ({p.admit_date})" if p.admit_date else ""
        out.append(f"Admission {p.admit_seq}{when}: {p.paragraph_md}")
    return "\n\n".join(out)


def _chat(messages: list[dict]) -> str:
    # ou.chat_text retries once with think=False on empty output.
    return ou.chat_text(SPEC.model_id, messages, think=SPEC.think,
                        options={"temperature": TEMP, "num_ctx": NUM_CTX}, keep_alive=-1)


def synthesize(inp: TldrSynthesizerInput) -> TldrSynthesizerOutput:
    user = _TMPL.format(
        n=inp.n_total_admissions, first=inp.first_date or "?", last=inp.last_date or "?",
        prior=(inp.prior_admissions_md.strip() or "(none)"),
        hot=_hot_block(inp.hot_window_paragraphs))
    tldr = _chat([{"role": "system", "content": _SYS}, {"role": "user", "content": user}])
    hadms = [p.hadm_id for p in inp.hot_window_paragraphs]
    audit = AuditRecord.from_spec(
        SPEC, PROMPT_VERSION, subject_id=inp.subject_id, input_hadm_ids=hadms,
        output_text=tldr,
        scores={"n_total_admissions": inp.n_total_admissions,
                "n_hot_paragraphs": len(inp.hot_window_paragraphs),
                "has_prior": bool(inp.prior_admissions_md.strip()), "chars_out": len(tldr)})
    return TldrSynthesizerOutput(subject_id=inp.subject_id, tldr_md=tldr,
                                 n_total_admissions=inp.n_total_admissions, audit=audit)


_read_jsonl = io_utils.read_jsonl        # shared tolerant reader


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", type=int, default=None,
                    help="print only, no write")
    ap.add_argument("--subject", type=int, default=None,
                    help="recompute ONE subject and MERGE into tldr_results.jsonl (fold-in)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--fresh", action="store_true", help="overwrite tldr_results.jsonl")
    args = ap.parse_args()
    config.check_paths()

    # Prior Admissions paragraph per patient (corrected preferred over original).
    prior_by_pid = {int(r["subject_id"]): r.get("prior_admissions_md", "")
                    for r in _read_jsonl(COMPACTION_JSONL)}
    for r in _read_jsonl(CORRECTED_JSONL):
        prior_by_pid[int(r["subject_id"])] = r.get("prior_admissions_md", "")

    # Hot-window paragraphs per patient (D1).
    paras_by_pid: dict[int, list[EpisodeParagraph]] = {}
    for r in _read_jsonl(PARAGRAPHS_JSONL):
        paras_by_pid.setdefault(int(r["subject_id"]), []).append(EpisodeParagraph(**r))
    if not paras_by_pid:
        raise SystemExit(f"{PARAGRAPHS_JSONL} not found/empty. Run d1_paragrapher.py first.")

    # Holdout: drop the held-out admission's paragraph so the TL;DR reflects the
    # established record only (the incoming admission is reviewed separately).
    hold = holdout.holdout_hadms()
    if hold:
        for pid in list(paras_by_pid):
            paras_by_pid[pid] = [p for p in paras_by_pid[pid] if p.hadm_id not in hold]

    # n_total + date span per patient (established span only under holdout). Shared
    # cohort loader: dates parsed dayfirst, so [:10] slices below are clean ISO.
    df = cohort.load()
    df = holdout.established(df)
    span = df.groupby("subject_id").agg(
        n=("hadm_id", "count"), first=("admittime", "min"), last=("dischtime", "max"))

    if args.subject is not None:
        pids = [args.subject]
    elif args.preview is not None:
        pids = [args.preview]
    else:
        pids = sorted(paras_by_pid)
    if args.limit:
        pids = pids[:args.limit]
    done = set()
    if not args.fresh and args.preview is None and args.subject is None:
        done = {int(r["subject_id"]) for r in _read_jsonl(OUT_JSONL)}

    results, audits = [], []
    try:
        for pid in pids:
            if pid in done:
                continue
            paras = paras_by_pid.get(pid, [])
            if not paras:
                continue
            meta = span.loc[pid] if pid in span.index else None
            inp = TldrSynthesizerInput(
                subject_id=pid,
                prior_admissions_md=prior_by_pid.get(pid, ""),
                hot_window_paragraphs=paras,
                n_total_admissions=int(meta["n"]) if meta is not None else len(paras),
                first_date=str(meta["first"])[:10] if meta is not None else None,
                last_date=str(meta["last"])[:10] if meta is not None else None)
            out = synthesize(inp)
            results.append(out)
            audits.append(out.audit)
            logger.info("subject=%s  tldr %d chars  (prior=%s, hot=%d)", pid,
                        len(out.tldr_md), inp.prior_admissions_md != "", len(paras))
    finally:
        _ollama_unload()

    if args.preview is not None and results:
        print("\n## TL;DR\n" + results[0].tldr_md)
        return

    if args.subject is not None:
        io_utils.merge_jsonl(OUT_JSONL, [json.loads(o.model_dump_json()) for o in results],
                             lambda r: int(r["subject_id"]))
    else:
        mode_w = "w" if args.fresh else "a"
        with OUT_JSONL.open(mode_w, encoding="utf-8") as fh:
            for o in results:
                fh.write(o.model_dump_json() + "\n")
    with AUDIT_JSONL.open("a", encoding="utf-8") as fh:
        for a in audits:
            fh.write(a.model_dump_json() + "\n")

    print("\n" + "=" * 55)
    print("D2 TL;DR SYNTHESIZER")
    print(f"  TL;DRs written : {len(results)}")
    if results:
        print(f"  mean length    : {sum(len(o.tldr_md) for o in results)//len(results)} chars")
    print(f"  results -> {OUT_JSONL}")
    print("\nNEXT: coordinator -- assemble LongitudinalView (tldr + current + hot + prior + ledger).")


if __name__ == "__main__":
    main()
