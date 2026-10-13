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
d1_paragrapher.py
=================
Episode_paragrapher (Tier 2a). Condense one admission's BHC (the
approved single-episode summary) into ONE concise longitudinal-grade paragraph. 
Cached; the coordinator builds the hot-window list from these.

The view needs paragraphs only for the recent un-compacted span (older admissions
are represented by the compacted Prior Admissions tier + the ledger), so by
default this paragraphs the last N_COMPACT_THRESHOLD admissions per patient.
--all paragraphs every admission (production semantics: one per admission as it
occurs, later aged into compaction).

HOLDOUT: the span is taken over the ESTABLISHED admissions (holdout.established),
so n matches the coordinator's n. The held-out admission is rendered as raw
incoming text. holdout.established is a no-op when holdout is off, so production is unaffected. 
Because the boundary moves with holdout, d1, like c1, MUST be rerun on any holdout toggle: b2, c1, d1, d2, coordinator, d3.

Input:  data/interim/cohort_admissions.csv (bhc_text / admit_seq / dates / service)
Output: data/interim/episode_paragraphs.jsonl  (one EpisodeParagraph per admission)
        appends one AuditRecord per paragraph to generation_audit.jsonl

    cd C:~/Summarization/clin_longsum/src
    python d1_paragrapher.py
    python d1_paragrapher.py --preview <subject_id> # prints only, no write
    python d1_paragrapher.py --subject 90000029
    python d1_paragrapher.py --limit 5
    python d1_paragrapher.py --all --fresh
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
from roles.contracts import AuditRecord, EpisodeParagraph, EpisodeParagrapherInput

sys.path.insert(0, str(config.SYNTH_DIR))
import ollama_utils as ou                # noqa: E402  (shared chat retry / unload)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("d1_paragrapher")

SPEC = config.model_for("episode_paragrapher")
PROMPT_VERSION = config.prompt_version("episode_paragrapher")
TEMP = SPEC.params.get("temperature", 0.2)
NUM_CTX = SPEC.params.get("num_ctx", 65536)

COHORT_CSV = config.INTERIM_DIR / "cohort_admissions.csv"
OUT_JSONL = config.EPISODE_PARAGRAPHS_JSONL
AUDIT_JSONL = config.AUDIT_JSONL


# Shared VRAM / unload helpers (vendor/ollama_utils).
_gpu_cleanup = ou.gpu_cleanup


def _ollama_unload() -> None:
    ou.ollama_unload(SPEC.model_id)


# ── prompt (episode_paragrapher.v1) ─────────────────────────────────────────────

_SYS = (
    "You write ONE concise paragraph summarising a single hospital admission for a "
    "patient's longitudinal record. Faithful to the source only; you invent nothing."
)

_TMPL = (
    "TASK: Summarise this admission as ONE concise clinical paragraph for a longitudinal "
    "record (the patient has many admissions). Capture the reason for admission, key "
    "diagnoses, major interventions or procedures, and the outcome/disposition.\n\n"
    "RULES:\n"
    "- Use ONLY the source summary below. Invent nothing; add no fact, date, or finding "
    "not present.\n"
    "- One tight paragraph (3-6 sentences), past tense. Plain clinical prose - no headers, "
    "no bullets.\n"
    "- Summarise THIS admission; do not re-narrate the patient's entire chronic history.\n\n"
    "ADMISSION {seq}{when} - SOURCE SUMMARY:\n{summary}\n\n"
    "Write only the paragraph."
)


def _chat(messages: list[dict]) -> str:
    # ou.chat_text retries once with think=False on empty output (thinking budget blowout).
    return ou.chat_text(SPEC.model_id, messages, think=SPEC.think,
                        options={"temperature": TEMP, "num_ctx": NUM_CTX}, keep_alive=-1)


def paragraph_one(inp: EpisodeParagrapherInput) -> EpisodeParagraph:
    when = ""
    if inp.admit_date:
        when = f" ({inp.admit_date}{', ' + inp.service if inp.service else ''})"
    elif inp.service:
        when = f" ({inp.service})"
    user = _TMPL.format(seq=inp.admit_seq, when=when, summary=inp.approved_summary_md.strip())
    msgs = [{"role": "system", "content": _SYS}, {"role": "user", "content": user}]
    para = _chat(msgs)
    status = "ok" if para.strip() else "flagged"
    audit = AuditRecord.from_spec(
        SPEC, PROMPT_VERSION, subject_id=inp.subject_id, input_hadm_ids=[inp.hadm_id],
        output_text=para,
        scores={"chars_in": len(inp.approved_summary_md), "chars_out": len(para),
                "ratio": round(len(para) / max(len(inp.approved_summary_md), 1), 4)},
        status=status,
    )
    return EpisodeParagraph(
        subject_id=inp.subject_id, hadm_id=inp.hadm_id, admit_seq=inp.admit_seq,
        admit_date=inp.admit_date, service=inp.service, paragraph_md=para, audit=audit)


_read_jsonl = io_utils.read_jsonl        # shared tolerant reader


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="paragraph EVERY admission (default: recent span only)")
    ap.add_argument("--preview", type=int, default=None) # prints only, no write
    ap.add_argument("--subject", type=int, default=None,
                    help="fold-in write-mode: paragraph one subject and MERGE into the jsonl "
                         "(replace-in-place). Unlike --preview, this writes + audits.")
    ap.add_argument("--limit", type=int, default=None, help="cap number of patients")
    ap.add_argument("--fresh", action="store_true", help="overwrite episode_paragraphs.jsonl")
    args = ap.parse_args()
    config.check_paths()

    df = cohort.load()                              # dayfirst-parsed dates: [:10] on a
    df = holdout.established(df)                    # Timestamp str is clean ISO. Paragraph the
    df = df.sort_values(["subject_id", "admit_seq"]).reset_index(drop=True)  # ESTABLISHED span
    # so n matches the coordinator; no-op when holdout off.
    by_pid: dict[int, list] = {}
    for _, r in df.iterrows():
        by_pid.setdefault(int(r["subject_id"]), []).append(r)

    target = args.subject if args.subject is not None else args.preview
    pids = [target] if target is not None else sorted(by_pid)
    if args.limit:
        pids = pids[:args.limit]

    existing = {} if args.fresh else {
        (int(r["subject_id"]), int(r["hadm_id"])): r for r in _read_jsonl(OUT_JSONL)}
    # empty paragraphs are NOT done -- they regenerate (self-healing)
    done = {k for k, r in existing.items() if (r.get("paragraph_md") or "").strip()}

    results, audits, n_done = [], [], 0
    try:
        for pid in pids:
            adms = by_pid.get(pid, [])
            n = len(adms)
            boundary = n - config.N_COMPACT_THRESHOLD
            for r in adms:
                seq = int(r["admit_seq"])
                if not args.all and seq <= boundary:
                    continue                                   # compacted -> no paragraph needed
                hadm = int(r["hadm_id"])
                if (pid, hadm) in done:
                    continue
                summary = str(r["bhc_text"]) if pd.notna(r.get("bhc_text")) else ""
                if not summary.strip():
                    continue
                inp = EpisodeParagrapherInput(
                    subject_id=pid, hadm_id=hadm, admit_seq=seq,
                    approved_summary_md=summary,
                    admit_date=str(r["admittime"])[:10] if pd.notna(r.get("admittime")) else None,
                    discharge_date=str(r["dischtime"])[:10] if pd.notna(r.get("dischtime")) else None,
                    service=str(r["service"]) if pd.notna(r.get("service")) else None,
                )
                ep = paragraph_one(inp)
                results.append(ep)
                audits.append(ep.audit)
                n_done += 1
                logger.info("subject=%s seq=%s hadm=%s  ratio=%.3f", pid, seq, hadm,
                            ep.audit.scores.get("ratio", 0))
    finally:
        _ollama_unload()

    if args.preview is not None and args.subject is None and results:
        for ep in results:
            print(f"\n### Admission {ep.admit_seq}"
                  f"{' (' + ep.admit_date + ')' if ep.admit_date else ''}\n{ep.paragraph_md}")
        return

    # Merge new results over existing (a regenerated paragraph replaces its stale/empty
    # line instead of duplicating), then rewrite deduped by (subject_id, hadm_id).
    merged = dict(existing)
    for ep in results:
        merged[(ep.subject_id, ep.hadm_id)] = json.loads(ep.model_dump_json())
    with OUT_JSONL.open("w", encoding="utf-8") as fh:
        for k in sorted(merged):
            fh.write(json.dumps(merged[k]) + "\n")
    with AUDIT_JSONL.open("a", encoding="utf-8") as fh:
        for a in audits:
            fh.write(a.model_dump_json() + "\n")

    print("\n" + "=" * 55)
    print(f"D1 EPISODE PARAGRAPHER ({'all admissions' if args.all else 'recent span'})")
    print(f"  paragraphs written : {n_done}")
    if results:
        ratios = [ep.audit.scores.get("ratio", 0) for ep in results]
        print(f"  mean condensation  : {sum(ratios)/len(ratios):.3f} (out/in chars)")
    print(f"  results -> {OUT_JSONL}")
    print("\nNEXT: D2 tldr_synthesizer, then the coordinator (LongitudinalView).")


if __name__ == "__main__":
    main()
