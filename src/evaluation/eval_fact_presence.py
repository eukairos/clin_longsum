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
eval_fact_presence.py
=====================
Phase F shared primitive - BATCHED PROSE FACT-PRESENCE JUDGE (Tier 1, LLM judge).

Given a TEXT and a list of candidate FACTS, decide for EACH fact whether the text
states or directly supports it. ONE judge call per (patient, text) covers all the
facts for that text - cheap, vs one call per fact.

This is DISTINCT from f4.judge_unit: f4 DECOMPOSES a summary into its own atomic
claims and labels each; this checks a list of SPECIFIC, externally-supplied facts
against a text. Consumers:
  - the ledger-in-context ablation (studies/ledger_context_ablation) -- prose-drop check
  - the position-bias regression (studies/position_bias)              -- the outcome variable

Two verbs share the judge + recovery cascade: fact_presence (does the text STATE the fact)
and fact_contradiction (does the text ASSERT SOMETHING INCOMPATIBLE with the fact; silence
is NOT a contradiction). The ablation uses contradiction as its clean premise test.

Same judge SPEC as f4 (`config.model_for("faithfulness_validator")`, deepseek-r1,
temperature 0) and the SAME graceful-recovery cascade (empty -> no-think retry;
malformed JSON -> reparse; still failing -> judge_error). Kept in this one module so
both consumers import it and the judge cannot drift between them.

    cd ~/clin_longsum/src/evaluation
    python eval_fact_presence.py        # 2-fact smoke test against the live judge
"""

from __future__ import annotations

import json
import logging
import sys

import ollama

import _bootstrap  # noqa: F401  (puts src/ on sys.path)
import config

sys.path.insert(0, str(config.SYNTH_DIR))
import ollama_utils as ou               # noqa: E402  (shared VRAM / unload)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("eval_fact_presence")

SPEC = config.model_for("faithfulness_validator")
PROMPT_VERSION = "fact_presence.v1"
TEMP = SPEC.params.get("temperature", 0.0)
NUM_CTX = SPEC.params.get("num_ctx", 65536)
NUM_PREDICT = SPEC.params.get("num_predict", 8192)

SRC_CHAR_CAP = 120_000          # ~ under 65K tokens; a single prose unit never approaches this


# ── VRAM (same pattern as f4) ────────────────────────────────────────────────────

def _gpu_cleanup() -> None:
    ou.gpu_cleanup()


def ollama_unload() -> None:
    """Drop the judge from VRAM. Call at batch end (consumers own the lifecycle).
    Shared vendor helper; public name kept -- both studies call it."""
    ou.ollama_unload(SPEC.model_id)


# ── prompt (fact_presence.v1) ────────────────────────────────────────────────────

_SYS = (
    "You are a meticulous clinical fact-checker. Given a TEXT and a numbered list of "
    "FACTS, decide for EACH fact whether the TEXT states or directly supports it. Use "
    "ONLY the TEXT, never outside medical knowledge. A fact is supported if the TEXT "
    "asserts it or it follows directly from what the TEXT says (a faithful paraphrase, "
    "or a count/trend the TEXT makes explicit). It is NOT supported if the TEXT does "
    "not mention it or contradicts it."
)

_TMPL = (
    "TEXT:\n<<<\n{text}\n>>>\n\n"
    "FACTS (numbered):\n{facts}\n\n"
    "For EACH fact id, decide whether the TEXT supports it. Return ONLY a JSON array, "
    "no prose, no markdown, one object per fact:\n"
    "[{{\"id\": <int>, \"supported\": true|false}}]"
)

_SYS_CONTRA = (
    "You are a meticulous clinical fact-checker. Given a TEXT and a numbered list of "
    "FACTS, decide for EACH fact whether the TEXT CONTRADICTS it - i.e., the TEXT "
    "asserts something that cannot be true at the same time as the fact (e.g. states a "
    "condition was ruled out or absent when the fact says it is present, or gives a "
    "conflicting value/laterality/status). Use ONLY the TEXT, never outside medical "
    "knowledge. If the TEXT is SILENT about the fact, that is NOT a contradiction. Only "
    "flag a contradiction when the TEXT states something incompatible."
)

_TMPL_CONTRA = (
    "TEXT:\n<<<\n{text}\n>>>\n\n"
    "FACTS (numbered):\n{facts}\n\n"
    "For EACH fact id, decide whether the TEXT contradicts it (silence = not a "
    "contradiction). Return ONLY a JSON array, no prose, no markdown, one object per fact:\n"
    "[{{\"id\": <int>, \"contradicted\": true|false}}]"
)


def _chat(messages: list[dict], think) -> str:
    resp = ollama.chat(
        model=SPEC.model_id, messages=messages, think=think,
        options={"temperature": TEMP, "num_ctx": NUM_CTX, "num_predict": NUM_PREDICT},
        keep_alive=-1,
    )
    return (resp.message.content or "").strip()


def _parse_array(text: str):
    t = text.strip()
    if "```" in t:
        t = max(t.split("```"), key=len)
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    s, e = t.find("["), t.rfind("]")
    if s != -1 and e != -1 and e > s:
        t = t[s:e + 1]
    return json.loads(t)


def _cap(s: str) -> str:
    return s[:SRC_CHAR_CAP] if len(s) > SRC_CHAR_CAP else s


def _run_judge(sys_prompt: str, user_prompt: str):
    """Shared judge call + graceful-recovery cascade (empty -> no-think; malformed ->
    reparse; else judge_error). Returns (list|None, status)."""
    msgs = [{"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt}]
    status = "ok"
    try:
        out = _chat(msgs, SPEC.think)
    except Exception as exc:
        logger.warning("judge call failed: %s", exc)
        out = ""
    if not out:
        try:
            out = _chat(msgs, False)
            status = "retried_nothink"
        except Exception as exc:
            logger.warning("no-think retry failed: %s", exc)
            return None, "judge_error"
    try:
        arr = _parse_array(out)
    except Exception:
        try:
            out2 = _chat(msgs + [{"role": "user",
                                  "content": "Output ONLY the JSON array. No reasoning, no prose."}],
                         False)
            arr = _parse_array(out2)
            status = "reparsed" if status == "ok" else status
        except Exception:
            return None, "judge_error"
    if not isinstance(arr, list):
        return None, "judge_error"
    return arr, status


def fact_presence(text: str, facts: list[str]) -> tuple[list[dict], str]:
    """Judge whether `text` supports each fact in `facts`.

    Returns (results, status) where results aligns 1:1 with `facts`:
        [{"id": i, "fact": <str>, "supported": True|False|None}]
    supported=None marks a fact the judge omitted from its array (coverage gap).
    status in {ok, retried_nothink, reparsed, judge_error}. On judge_error every
    `supported` is None and the consumer should DROP this (patient, text).
    """
    facts = [str(f) for f in facts]
    base = [{"id": i, "fact": f, "supported": None} for i, f in enumerate(facts)]
    if not facts:
        return base, "ok"
    numbered = "\n".join(f"{i}. {f}" for i, f in enumerate(facts))
    arr, status = _run_judge(_SYS, _TMPL.format(text=_cap(text), facts=numbered))
    if arr is None:
        return base, "judge_error"
    by_id = {}
    for item in arr:
        if not isinstance(item, dict):
            continue
        try:
            fid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        by_id[fid] = bool(item.get("supported"))
    for rec in base:
        if rec["id"] in by_id:
            rec["supported"] = by_id[rec["id"]]
    return base, status


def fact_presence_bools(text: str, facts: list[str]) -> tuple[list, str]:
    """Convenience: return just the aligned list of True/False/None + status."""
    results, status = fact_presence(text, facts)
    return [r["supported"] for r in results], status


def fact_contradiction(text: str, facts: list[str]) -> tuple[list[dict], str]:
    """Judge whether `text` CONTRADICTS each fact (silence is NOT a contradiction).

    Returns (results, status); results aligns 1:1 with `facts`:
        [{"id": i, "fact": <str>, "contradicted": True|False|None}]
    None marks a fact the judge omitted. status as in fact_presence.
    """
    facts = [str(f) for f in facts]
    base = [{"id": i, "fact": f, "contradicted": None} for i, f in enumerate(facts)]
    if not facts:
        return base, "ok"
    numbered = "\n".join(f"{i}. {f}" for i, f in enumerate(facts))
    arr, status = _run_judge(_SYS_CONTRA, _TMPL_CONTRA.format(text=_cap(text), facts=numbered))
    if arr is None:
        return base, "judge_error"
    by_id = {}
    for item in arr:
        if not isinstance(item, dict):
            continue
        try:
            fid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        by_id[fid] = bool(item.get("contradicted"))
    for rec in base:
        if rec["id"] in by_id:
            rec["contradicted"] = by_id[rec["id"]]
    return base, status


def fact_contradiction_bools(text: str, facts: list[str]) -> tuple[list, str]:
    """Convenience: aligned list of True/False/None (contradicted) + status."""
    results, status = fact_contradiction(text, facts)
    return [r["contradicted"] for r in results], status


# ── smoke test (runs against the live judge; clears VRAM after) ──────────────────

if __name__ == "__main__":
    demo_text = ("Patient with COPD, five exacerbations between 2019 and 2021, all "
                 "resolved. History of orthotopic liver transplant. No documented "
                 "penicillin allergy.")
    demo_facts = [
        "COPD with recurrent exacerbations",          # expect supported
        "orthotopic liver transplant",                # expect supported
        "penicillin anaphylaxis",                     # expect NOT supported (text says no allergy)
        "stage IV chronic kidney disease",            # expect NOT supported (absent)
    ]
    try:
        results, status = fact_presence(demo_text, demo_facts)
        print(f"status={status}")
        for r in results:
            print(f"  [{r['supported']}] {r['fact']}")
    finally:
        ollama_unload()
