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
faithfulness_validator.py
=========================
Step 3 of the synthetic note generator pipeline (vendored into the self-contained repo).

4-step faithfulness validation adapted from SynthMedic (Grazhdanski et al.
JBI 2025, §3.4.1) for reverse-generation direction:

  Direction: facts in the seed discharge summary (DS) must be traceable
  back to the generated admission H&P + progress notes.

Steps:
  1. Extract atomic statements from the seed DS.
  2. Classify each statement as context-independent or context-dependent.
  3. Filter which context-dependent statements are relevant for this case.
  4. Score: inferrable statements / total applicable statements.

Statement format rules:
  - Unconditional facts: "must" phrasing (not "should").
  - Conditional facts: strict "if [condition], then [action]" format.
  - Negatively phrased statements excluded (unverifiable from synthetic notes).

Step 4 entailment: clinical inference is acceptable.
  A diagnosis implies its standard workup. Documented procedure implies
  prerequisites. Standard treatment implies it was given.

Context budgets (per step):
  Steps 1-3: _CTX_STEPS_123 = 16384
  Step 4:    _CTX_STEP4     = 32768
    Step 4 is the most context-intensive: it must fit the generated notes
    (can be 8K+ chars), the full statement list, judge model's thinking tokens,
    AND a long JSON response. 16384 caused empty entailment lists (score=0,
    no failed statements) - raising to 32768 fixes this.

Score=0 with no failed statements guard:
  If Step 4 returns an empty entailment list, the result is treated as an
  error ("step4_empty_entailment") rather than a valid score of 0. This
  prevents the corrector from being called with no actual edits to make.

Hang containment:
  deepseek-r1 can enter a thinking-loop on a hard entailment and never return
  (a synthetic worked example -- subject 90000017 -- ran 1.5-4 h before a manual
  kill, and a truncated payload surfaced downstream as an UNCAUGHT json parse
  error that crashed the whole c3/c4 batch). Three independent watchdogs now
  bound a single judge call - num_predict (forward-progress token cap), an
  inactivity timeout, and a wall-clock ceiling - backed by a think -> no-think
  -> JSON-only recovery cascade. Any unrecoverable call degrades to a judge_error
  RESULT (score=None) instead of raising, so the batch self-terminates that one
  unit and carries on. validate_faithfulness is now TOTAL: it never raises for a
  judge/parse/timeout fault, mirroring f4's judge_unit. See _chat / _chat_json.
"""

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import httpx
import ollama

from text_cleaner import clean_for_llm
from ollama_utils import gpu_cleanup, ollama_unload
import config

# ── Configuration ──────────────────────────────────────────────────────────────

# Judge model is config-driven (single source of truth for model swaps). Swap it in
# config.py's "faithfulness_validator" role, NOT here. config resolves because c3/c4
# load this module from src/ via sys.path.insert(config.SYNTH_DIR), with src/ on the path.
MODEL            = config.model_for("faithfulness_validator").model_id
TEMP_VALIDATE    = 0.1
_CTX_STEPS_123   = 16384   # Steps 1-3: lighter context demands
_CTX_STEP4       = 32768   # Step 4: notes + statements + thinking + long JSON response

# Forward-progress token caps. A thinking-loop that keeps emitting tokens cannot
# exceed these, so the call self-terminates instead of running for hours. Steps 1-3
# emit short JSON; Step 4 emits one entry per statement (longer), so it gets more.
_NPREDICT_STEPS_123 = 8192
_NPREDICT_STEP4     = 16384

# Step-4 completeness guard: a PARTIAL entailment (fewer verdicts than statements) scores
# the missing statements as not-supported -> a FALSE fail (a synthetic worked example --
# subject 90000042 -- returned 10 verdicts for 46 statements -> 0.196 FAIL; a clean rerun
# passed). Accept only if >= this fraction of statements got a verdict; otherwise retry with
# think=False (returns the full list more reliably), then mark judge_error rather than
# emit a false FAIL.
_STEP4_MIN_COVERAGE = 0.9
_STEP4_MAX_RETRIES  = 2

# Watchdogs against a thinking-loop that returns nothing (the hang class above):
#   - inactivity: abort if NO token (thinking OR content) arrives within this window
#                 (also enforced at the transport layer via the httpx read-timeout)
#   - wall-clock: abort if a single judge call exceeds this total, regardless of
#                 token flow (catches a steady thinking-loop that stays under the
#                 num_predict cap per-chunk but never converges)
# These turn a multi-hour hang into a worst case of minutes. Raise _WALL_CLOCK_S
# only if a legitimate Step 4 over a very long source is being cut; the inactivity
# guard trips only on a true stall, so it rarely needs tuning.
_INACTIVITY_S = 240
_WALL_CLOCK_S = 600

FAITHFULNESS_THRESHOLD = 0.6

_KEEP_ALIVE_SINGLE = 0
_KEEP_ALIVE_BATCH  = -1

# Dedicated client. The httpx read-timeout is a transport-level inactivity backstop
# (fires if the server stops emitting mid-stream); the Python loop in _chat adds the
# total wall-clock cap and a clean, attributable log line. OLLAMA_HOST is honoured so
# a non-default endpoint still works; None lets ollama use its own default.
_OLLAMA_HOST = os.environ.get("OLLAMA_HOST") or None
_client = ollama.Client(host=_OLLAMA_HOST,
                        timeout=httpx.Timeout(_INACTIVITY_S, connect=30.0))

logger = logging.getLogger(__name__)


class JudgeTimeout(RuntimeError):
    """A single judge call tripped the inactivity or wall-clock watchdog."""


class JudgeError(RuntimeError):
    """No parseable JSON obtainable after the full recovery cascade."""


# ── Prompts ────────────────────────────────────────────────────────────────────

_SYSTEM = """\
You are a clinical NLP expert evaluating whether facts from a hospital \
discharge summary are supported by the patient's admission and progress notes. \
Return only valid JSON. No preamble, no markdown fences, no explanation outside \
the JSON structure.\
"""

_STEP1_EXTRACT_TMPL = """\
Extract a list of atomic clinical statements from the discharge summary below.

RULES:
- Each statement must be a single, atomic clinical fact (not compound).
- Unconditional facts: use "must" (not "should"). Example:
    "The patient must have received IV fluid resuscitation."
- Conditional facts: use strict "if [condition], then [action]" format. Example:
    "If the patient had AKI, then creatinine must have been elevated on admission."
- Focus on: diagnoses, key investigations and results, procedures performed,
  medications started or changed, clinical events, disposition.
- Exclude: demographic details, appointment dates, administrative content.
- Aim for 15-30 statements. More is better than fewer for coverage.

CRITICAL -- DO NOT extract negatively phrased statements:
- Do NOT write "the patient must NOT have X" or "there must be no evidence of Y."
  These are unverifiable from clinical notes and create false failures.
- Instead, extract the positive clinical inference. Examples:
    WRONG: "The patient must not have had ischemic ECG changes."
    RIGHT: "The patient must have had ECG performed as part of the cardiac workup."
    WRONG: "There must be no documented allergy to penicillin."
    RIGHT: "If the patient had a documented drug allergy, it must appear in the notes."
- If the DS mentions a negative finding as clinically significant (e.g. "no PE"),
  rephrase as: "The patient must have been evaluated for PE and found not to have it."

Return ONLY this JSON:
{{"statements": ["statement 1", "statement 2", ...]}}

DISCHARGE SUMMARY:
{ds_text}
"""

_STEP2_CLASSIFY_TMPL = """\
Classify each statement as either context-independent or context-dependent.

DEFINITIONS:
- context-independent: A concrete fact verifiable against any note for this
  case without additional conditions.
- context-dependent: A conditional fact that only applies if a specific
  condition is met.

Return ONLY this JSON:
{{
  "classified": [
    {{"statement": "...", "type": "independent", "explanation": "..."}},
    {{"statement": "...", "type": "dependent",   "explanation": "..."}}
  ]
}}

STATEMENTS:
{statements_json}
"""

_STEP3_FILTER_TMPL = """\
For each context-dependent statement, determine whether it is RELEVANT to this
specific case based on the generated clinical notes.

A statement is RELEVANT if its condition (the "if" part) is met in the notes.
A statement is IRRELEVANT if its condition does not apply to this patient.

IMPORTANT: If the condition is met but the consequent action is missing,
the statement is still RELEVANT (it will fail entailment in Step 4).

Return ONLY this JSON:
{{
  "filtered": [
    {{"statement": "...", "relevant": true,  "reason": "..."}},
    {{"statement": "...", "relevant": false, "reason": "..."}}
  ]
}}

CONTEXT-DEPENDENT STATEMENTS:
{dependent_json}

GENERATED CLINICAL NOTES:
{notes_text}
"""

_STEP4_ENTAIL_TMPL = """\
For each statement below, determine whether it can be INFERRED from the
generated clinical notes.

DEFINITION: A statement is SUPPORTED if the notes contain sufficient information
to conclude the statement is true - either explicitly stated OR clinically
inferable from documented facts.

CLINICAL INFERENCE IS ACCEPTABLE:
- A documented diagnosis implies its standard workup was completed.
  Example: NSTEMI implies ECG performed; AKI implies creatinine monitored;
  pneumonia implies CXR done. Mark SUPPORTED even if the specific result
  is not documented.
- Standard treatment for a documented condition implies that treatment was given.
- A documented procedure implies its prerequisites were met.

MARK AS NOT SUPPORTED only if:
- The statement contradicts documented information, OR
- The statement refers to a specific finding genuinely absent and non-inferable.

IMPORTANT: You must evaluate EVERY statement in the list. Do not skip any.
The "entailment" array in your response must contain exactly one entry per
statement, in the same order as the input list.

Return ONLY this JSON:
{{
  "entailment": [
    {{
      "statement": "...",
      "supported": true,
      "evidence": "brief reference from notes or clinical inference basis",
      "note": "optional: explain inference if not explicit"
    }}
  ]
}}

STATEMENTS TO EVALUATE ({n_statements} total -- include all {n_statements} in response):
{final_statements_json}

GENERATED CLINICAL NOTES:
{notes_text}
"""


# ── Helpers ────────────────────────────────────────────────────────────────────

def _chat(messages: list[dict], num_ctx: int, num_predict: int,
          keep_alive: int = _KEEP_ALIVE_SINGLE, think: bool = True) -> str:
    """Stream a single judge call under three independent watchdogs:

      - num_predict : hard forward-progress cap -- a token-emitting loop ends here.
      - inactivity  : no token (thinking OR content) within _INACTIVITY_S -> abort
                      (also backstopped by the client's httpx read-timeout).
      - wall-clock  : total call time over _WALL_CLOCK_S -> abort.

    Aborting closes the stream; the disconnect makes ollama cancel generation and
    free the runner. Raises JudgeTimeout on any trip. Returns the assembled
    `content` (thinking tokens are watched for liveness but never returned)."""
    start = last = time.monotonic()
    parts: list[str] = []
    stream = _client.chat(
        model=MODEL, messages=messages, think=think, format="json",
        options={"temperature": TEMP_VALIDATE, "num_ctx": num_ctx,
                 "num_predict": num_predict},
        keep_alive=keep_alive, stream=True,
    )
    try:
        for chunk in stream:
            now = time.monotonic()
            if now - start > _WALL_CLOCK_S:
                raise JudgeTimeout(f"wall-clock {_WALL_CLOCK_S}s exceeded")
            msg = getattr(chunk, "message", None)
            content  = (getattr(msg, "content", "") or "") if msg else ""
            thinking = (getattr(msg, "thinking", "") or "") if msg else ""
            if content or thinking:
                last = now
            if content:
                parts.append(content)
            if now - last > _INACTIVITY_S:
                raise JudgeTimeout(f"inactivity {_INACTIVITY_S}s (no token)")
    except httpx.TimeoutException as exc:
        raise JudgeTimeout(f"httpx timeout: {exc}") from exc
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
    return "".join(parts).strip()


def _parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    raise ValueError(f"Could not parse JSON:\n{text[:2000]}")


def _chat_json(messages: list[dict], num_ctx: int, num_predict: int,
               keep_alive: int, label: str) -> tuple[dict, dict]:
    """Robust parsed-JSON judge call with a recovery cascade so a thinking-loop or
    a malformed payload degrades to a JudgeError (the caller maps that to a
    judge_error result) - never an indefinite hang or an uncaught crash:

        1. think=True   (normal path)
        2. think=False  (kills a deepseek reasoning loop; usually clean JSON)
        3. think=False + an explicit 'JSON only' reprompt

    Returns (parsed_json, meta) where meta = {"attempt": 1|2|3, "think": bool}.
    attempt>1 / think=False is a DEGRADED (weaker) judge - deepseek rubber-stamps
    supported:true more readily without reasoning - so the caller records it and a
    no-think PASS becomes a human-review target rather than a trusted pass.
    Raises JudgeError only if all three attempts fail."""
    # attempt 1 -- thinking
    try:
        out = _chat(messages, num_ctx, num_predict, keep_alive, think=True)
        if out:
            return _parse_json(out), {"attempt": 1, "think": True}
        logger.warning("%s: empty output (think) -- retrying no-think", label)
    except JudgeTimeout as exc:
        logger.warning("%s: timeout (think) -- %s; retrying no-think", label, exc)
    except ValueError:
        logger.warning("%s: malformed JSON (think) -- retrying no-think", label)

    # attempt 2 -- no thinking
    try:
        out = _chat(messages, num_ctx, num_predict, keep_alive, think=False)
        return _parse_json(out), {"attempt": 2, "think": False}
    except (JudgeTimeout, ValueError) as exc:
        logger.warning("%s: no-think failed -- %s; final JSON-only retry", label, exc)

    # attempt 3 -- no thinking + hard JSON-only instruction
    try:
        out = _chat(
            messages + [{"role": "user",
                         "content": "Output ONLY the JSON object. "
                                    "No reasoning, no markdown, no prose."}],
            num_ctx, num_predict, keep_alive, think=False)
        return _parse_json(out), {"attempt": 3, "think": False}
    except (JudgeTimeout, ValueError) as exc:
        raise JudgeError(f"{label}: unrecoverable after 3 attempts ({exc})") from exc


def _has_medications(facts: dict) -> bool:
    adm = facts.get("meds_admission", [])
    dc  = facts.get("meds_discharge", [])
    if not adm or not dc:
        return False
    return bool(
        {str(m).lower() for m in adm}.symmetric_difference(
        {str(m).lower() for m in dc})
    )


def _dedupe_entailment(entailment: list, tl: str) -> list:
    """Drop duplicate verdicts by statement text (a malformed Step 4 can repeat entries;
    a duplicate supported:true would inflate the score). Keep first seen."""
    n_in = len(entailment or [])
    seen: set[str] = set()
    out = []
    for e in entailment or []:
        key = (e.get("statement", "") or "").strip().lower()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(e)
    if len(out) != n_in:
        logger.warning("Step 4 had %d duplicate entailment entries (%s) -- deduped.",
                       n_in - len(out), tl)
    return out


# ── 4-step validator ───────────────────────────────────────────────────────────

def validate_faithfulness(
    seed_ds: str,
    generated_notes: dict,
    facts: dict,
    hadm_id: Optional[str] = None,
    keep_alive: int = _KEEP_ALIVE_SINGLE,
    trace_label: Optional[str] = None,
) -> dict:
    """Validate that `seed_ds`'s facts are entailed by `generated_notes`.

    `trace_label` is a free-text tag used ONLY in log lines (does not change the
    result). Callers that pass a subject_id as `hadm_id` (e.g. c3/c4, where one
    compaction covers many admissions) should pass the real hadm_id(s) here so the
    console is informative - e.g. trace_label="subj=123 hadm=[456, 789]".

    TOTAL: never raises for a judge/parse/timeout fault. Any such fault returns an
    _empty_result with score=None and an error reason, so a batch keeps running."""
    tl = trace_label or f"hadm_id={hadm_id}"
    sys_msg    = {"role": "system", "content": _SYSTEM}
    ds_clean   = clean_for_llm(seed_ds)
    notes_text = generated_notes.get("assembled_input", "")

    if not notes_text:
        parts = [generated_notes.get("admission_note", {}).get("text", "")]
        for pn in generated_notes.get("progress_notes", []):
            parts.append(pn.get("text", ""))
        notes_text = "\n\n".join(p for p in parts if p)

    notes_clean = clean_for_llm(notes_text)

    if not ds_clean:
        logger.error("Empty seed DS after cleaning (%s)", tl)
        return _empty_result(hadm_id, "empty_seed_ds")
    if not notes_clean:
        logger.error("Empty generated notes after cleaning (%s)", tl)
        return _empty_result(hadm_id, "empty_notes")

    try:
        # Step 1
        logger.info("Step 1: statement extraction (%s)", tl)
        s1, m1 = _chat_json(
            [sys_msg, {"role": "user",
                       "content": _STEP1_EXTRACT_TMPL.format(ds_text=ds_clean)}],
            _CTX_STEPS_123, _NPREDICT_STEPS_123, keep_alive, f"step1 {tl}",
        )
        all_statements = s1.get("statements", [])
        logger.info("  Extracted %d statements.", len(all_statements))
        if not all_statements:
            return _empty_result(hadm_id, "no statements extracted")

        # Step 2
        logger.info("Step 2: classify (%s)", tl)
        s2, m2 = _chat_json(
            [sys_msg, {"role": "user",
                       "content": _STEP2_CLASSIFY_TMPL.format(
                           statements_json=json.dumps(all_statements, indent=2))}],
            _CTX_STEPS_123, _NPREDICT_STEPS_123, keep_alive, f"step2 {tl}",
        )
        classified  = s2.get("classified", [])
        independent = [c["statement"] for c in classified if c.get("type") == "independent"]
        dependent   = [c["statement"] for c in classified if c.get("type") == "dependent"]
        logger.info("  Independent: %d  Dependent: %d", len(independent), len(dependent))

        # Step 3
        relevant_dependent = []
        m3 = None
        if dependent:
            logger.info("Step 3: filter relevant (%s)", tl)
            s3, m3 = _chat_json(
                [sys_msg, {"role": "user",
                           "content": _STEP3_FILTER_TMPL.format(
                               dependent_json=json.dumps(dependent, indent=2),
                               notes_text=notes_clean)}],
                _CTX_STEPS_123, _NPREDICT_STEPS_123, keep_alive, f"step3 {tl}",
            )
            relevant_dependent = [
                f["statement"] for f in s3.get("filtered", []) if f.get("relevant")
            ]
            logger.info("  Relevant: %d / %d", len(relevant_dependent), len(dependent))

        final_statements = independent + relevant_dependent
        logger.info("  Final list: %d statements.", len(final_statements))
        if not final_statements:
            return _empty_result(hadm_id, "no applicable statements after filtering")

        # Step 4 -- larger context + token budget (longest output)
        logger.info("Step 4: entailment scoring (%s)", tl)
        n_final = len(final_statements)
        step4_msgs = [sys_msg, {"role": "user",
                                "content": _STEP4_ENTAIL_TMPL.format(
                                    n_statements=n_final,
                                    final_statements_json=json.dumps(final_statements, indent=2),
                                    notes_text=notes_clean)}]
        s4, m4 = _chat_json(step4_msgs, _CTX_STEP4, _NPREDICT_STEP4, keep_alive, f"step4 {tl}")
        entailment = _dedupe_entailment(s4.get("entailment", []), tl)

        # Completeness guard: a PARTIAL entailment scores the missing statements as
        # not-supported -> a FALSE fail. Retry with think=False (returns the full list
        # more reliably) before accepting; a retry that fixes it is a degraded judge
        # (flagged for review). Persistent incompleteness becomes judge_error below.
        retry = 0
        while len(entailment) < n_final * _STEP4_MIN_COVERAGE and retry < _STEP4_MAX_RETRIES:
            retry += 1
            logger.warning("step4 %s: incomplete entailment %d/%d -- no-think retry %d/%d",
                           tl, len(entailment), n_final, retry, _STEP4_MAX_RETRIES)
            try:
                out = _chat(step4_msgs + [{"role": "user",
                    "content": f"Your previous response was incomplete. Evaluate EVERY "
                               f"statement: return EXACTLY {n_final} entailment entries, one per "
                               f"statement, in order. Output ONLY the JSON object."}],
                    _CTX_STEP4, _NPREDICT_STEP4, keep_alive, think=False)
                cand = _dedupe_entailment(_parse_json(out).get("entailment", []), tl)
            except (JudgeTimeout, ValueError) as exc:
                logger.warning("step4 %s: retry %d failed -- %s", tl, retry, exc)
                continue
            if len(cand) > len(entailment):
                entailment, m4 = cand, {"attempt": 2, "think": False}   # degraded -> review
    except JudgeError as exc:
        logger.error("judge_error (%s): %s", tl, exc)
        return _empty_result(hadm_id, "judge_error")

    # Guard: empty entailment means Step 4 failed (context overrun or parse error).
    if not entailment:
        logger.error("Step 4 returned empty entailment list (%s). Marking as error.", tl)
        return _empty_result(hadm_id, "step4_empty_entailment")

    # Guard: still materially incomplete after retries -> do NOT score a false FAIL;
    # mark judge_error so c4 skips it (only pass is False is corrected) and the next
    # c3 run re-judges it (error/None rows auto-retry).
    if len(entailment) < n_final * _STEP4_MIN_COVERAGE:
        logger.error("Step 4 incomplete %d/%d after %d retries (%s) -- judge_error "
                     "(not a false FAIL).", len(entailment), n_final, _STEP4_MAX_RETRIES, tl)
        return _empty_result(hadm_id, "step4_incomplete")

    # Minor shortfall tolerated: a few missing verdicts -> scored not-supported.
    if len(entailment) < n_final:
        logger.warning("Step 4 returned %d entailment entries for %d statements (%s); "
                       "missing scored not-supported.", len(entailment), n_final, tl)

    n_total     = n_final
    # Clamp: supported can never exceed the statement count even if Step 4 emitted
    # extra/hallucinated entries -- guards a spurious score > 1.0 -> auto-pass.
    n_supported = min(sum(1 for e in entailment if e.get("supported")), n_total)
    score       = n_supported / n_total if n_total > 0 else 0.0

    failed = [
        {"statement": e.get("statement", ""),
         "evidence":  e.get("evidence", ""),
         "note":      e.get("note", "")}
        for e in entailment if not e.get("supported")
    ]
    med_flag = _has_medications(facts)

    judge_attempts = {"step1": m1["attempt"], "step2": m2["attempt"],
                      "step3": (m3["attempt"] if m3 else None),
                      "step4": m4["attempt"]}
    step4_think    = m4["think"]
    judge_degraded = (not m1["think"]) or (not m2["think"]) or (not m4["think"]) \
                     or (m3 is not None and not m3["think"])

    logger.info(
        "Faithfulness: %.3f (%d/%d) -- %s (%s)%s%s",
        score, n_supported, n_total,
        "PASS" if score >= FAITHFULNESS_THRESHOLD else "FAIL",
        tl,
        " [DOSAGE REVIEW]" if med_flag else "",
        " [NO-THINK JUDGE]" if judge_degraded else "",
    )

    return {
        "hadm_id":            hadm_id,
        "faithfulness_score": round(score, 4),
        "n_total_statements": n_total,
        "n_supported":        n_supported,
        "pass":               score >= FAITHFULNESS_THRESHOLD,
        "judge_think_mode":   step4_think,      # Step 4 scoring judge: True=thinking, False=degraded no-think
        "judge_degraded":     judge_degraded,   # any step fell back to no-think -> weaker judge; review these passes
        "judge_attempts":     judge_attempts,   # per-step attempt: 1=think, 2=no-think, 3=no-think+json-only
        "failed_statements":  failed,
        "entailment_detail":  entailment,
        "statement_counts": {
            "extracted":          len(all_statements),
            "independent":        len(independent),
            "dependent_total":    len(dependent),
            "dependent_relevant": len(relevant_dependent),
            "final_evaluated":    n_total,
        },
        "flags": {
            "dosage_review_needed": med_flag,
            "below_threshold":      score < FAITHFULNESS_THRESHOLD,
        },
        "validation_metadata": {
            "model":       MODEL,
            "temperature": TEMP_VALIDATE,
            "think_mode":  step4_think,
            "judge_degraded": judge_degraded,
            "threshold":   FAITHFULNESS_THRESHOLD,
            "ctx_step4":   _CTX_STEP4,
            "num_predict_step4": _NPREDICT_STEP4,
            "inactivity_s":      _INACTIVITY_S,
            "wall_clock_s":      _WALL_CLOCK_S,
        },
    }


def _empty_result(hadm_id: Optional[str], reason: str) -> dict:
    return {
        "hadm_id":            hadm_id,
        "faithfulness_score": None,
        "pass":               None,
        "error":              reason,
        "failed_statements":  [],
        "validation_metadata": {
            "model": MODEL, "temperature": TEMP_VALIDATE,
            "think_mode": True, "threshold": FAITHFULNESS_THRESHOLD,
        },
    }


# ── Batch runner (CSV-based, consistent with Steps 1 and 2) ───────────────────

def validate_batch_from_csv(
    bhc_csv: str,
    facts_path: str,
    notes_path: str,
    output_path: Optional[str] = None,
    n: int = 9999999,
    skip: int = 0,
    unload_after: bool = True,
) -> list[dict]:
    """
    Iterate the BHC CSV row-by-row (same as Steps 1 and 2).

    --skip: number of BHC CSV rows to skip before starting. Set to the
            same value used for --skip in the previous extraction run to
            process the matching batch.
    --n:    number of BHC CSV rows to scan (not cases to validate). Set
            to the same value used for --n in the previous extraction run.

    For each CSV row, if a matching extracted fact AND generated note exist,
    that case is validated. Rows with no match are skipped silently with a
    counter.

    Prints a handover tip at the end: the --skip value for the next run.
    """
    import csv as _csv

    # Build lookup indexes from JSONL files
    def _load_index(path, id_fn):
        idx = {}
        p = Path(path)
        if not p.exists():
            logger.warning("File not found: %s", path)
            return idx
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if "error" in rec:
                    continue
                k = id_fn(rec)
                if k:
                    idx[k] = rec
        logger.info("Loaded %d records from %s", len(idx), p.name)
        return idx

    facts_by_id = _load_index(
        facts_path,
        lambda r: str(r.get("extraction_metadata", {}).get("hadm_id")
                      or r.get("hadm_id", "")),
    )
    notes_by_id = _load_index(
        notes_path,
        lambda r: str(r.get("hadm_id", "")),
    )

    # Already-validated hadm_ids: skip silently to allow safe re-runs
    already_done = set()
    out_path = Path(output_path) if output_path else None
    if out_path and out_path.exists():
        with open(out_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rec = json.loads(line)
                        hid = str(rec.get("hadm_id", ""))
                        if hid:
                            already_done.add(hid)
                    except json.JSONDecodeError:
                        pass
        if already_done:
            logger.info("%d hadm_ids already in output -- will skip.",
                        len(already_done))

    results        = []
    rows_scanned   = 0
    rows_skipped   = 0   # no matching facts/notes
    rows_duplicate = 0   # already validated
    cases_found    = 0
    out_file = out_path.open("a", encoding="utf-8") if out_path else None

    try:
        with open(bhc_csv, encoding="utf-8", errors="replace") as f:
            reader = _csv.DictReader(f)
            for row_idx, row in enumerate(reader):
                if row_idx < skip:
                    continue
                if rows_scanned >= n:
                    break
                rows_scanned += 1

                hadm_id = str(row.get("hadm_id", "")).strip()
                seed_ds = (row.get("target") or row.get("input") or
                           row.get("text") or "").strip()

                if not hadm_id or not seed_ds:
                    rows_skipped += 1
                    continue
                if hadm_id in already_done:
                    rows_duplicate += 1
                    continue
                if hadm_id not in facts_by_id:
                    rows_skipped += 1
                    logger.debug("hadm_id=%s: no extracted facts -- skipping.",
                                 hadm_id)
                    continue
                if hadm_id not in notes_by_id:
                    rows_skipped += 1
                    logger.debug("hadm_id=%s: no generated notes -- skipping.",
                                 hadm_id)
                    continue

                cases_found += 1
                logger.info(
                    "Validating case %d (CSV row %d, hadm_id=%s)",
                    cases_found, row_idx, hadm_id,
                )
                try:
                    result = validate_faithfulness(
                        seed_ds=seed_ds,
                        generated_notes=notes_by_id[hadm_id],
                        facts=facts_by_id[hadm_id],
                        hadm_id=hadm_id,
                        keep_alive=_KEEP_ALIVE_BATCH,
                    )
                    results.append(result)
                    already_done.add(hadm_id)
                    if out_file:
                        out_file.write(json.dumps(result) + "\n")
                        out_file.flush()
                except Exception as exc:
                    logger.error("hadm_id=%s failed: %s", hadm_id, exc,
                                 exc_info=True)
                    err = {"hadm_id": hadm_id, "error": str(exc)}
                    results.append(err)
                    if out_file:
                        out_file.write(json.dumps(err) + "\n")
                        out_file.flush()
    finally:
        if out_file:
            out_file.close()
        if unload_after:
            ollama_unload(MODEL)
            gpu_cleanup()

    scored = [r for r in results if r.get("faithfulness_score") is not None]
    avg    = (sum(r["faithfulness_score"] for r in scored) / len(scored)
              if scored else 0.0)
    n_pass = sum(1 for r in scored if r.get("pass"))

    next_skip = skip + rows_scanned
    logger.info(
        "Batch complete: %d CSV rows scanned | %d cases validated | "
        "%d no match | %d duplicates skipped",
        rows_scanned, cases_found, rows_skipped, rows_duplicate,
    )
    if scored:
        logger.info("Scores: avg=%.3f | %d/%d pass", avg, n_pass, len(scored))
    logger.info("Next run: --skip %d", next_skip)
    print(f"\nStep 3 done. {cases_found} cases validated "
          f"({rows_scanned} CSV rows scanned, {rows_skipped} skipped -- no match).")
    print(f"Next Step 3 run:  --skip {next_skip}")
    print(f"Step 4 (corrector): run on {sum(1 for r in results if r.get('pass') is False)} "
          f"failed cases (no --skip needed for Step 4).")

    return results


def validate_batch(
    cases: list[dict],
    output_path: Optional[str] = None,
    unload_after: bool = True,
    skip: int = 0,
) -> list[dict]:
    """
    Legacy batch runner operating on a pre-assembled case list.
    Prefer validate_batch_from_csv() for new code.
    """
    if skip > 0:
        logger.info("Skipping first %d cases.", skip)
        cases = cases[skip:]

    results  = []
    out_file = None

    try:
        for idx, case in enumerate(cases):
            hadm_id = str(case.get("hadm_id", f"idx_{idx}"))
            logger.info("Validating %d/%d (hadm_id=%s)", idx + 1, len(cases), hadm_id)
            try:
                result = validate_faithfulness(
                    seed_ds=case["seed_ds"],
                    generated_notes=case["generated_notes"],
                    facts=case["facts"],
                    hadm_id=hadm_id,
                    keep_alive=_KEEP_ALIVE_BATCH,
                )
                results.append(result)
                if out_file:
                    out_file.write(json.dumps(result) + "\n")
                    out_file.flush()
            except Exception as exc:
                logger.error("hadm_id=%s failed: %s", hadm_id, exc, exc_info=True)
                err = {"hadm_id": hadm_id, "error": str(exc)}
                results.append(err)
                if out_file:
                    out_file.write(json.dumps(err) + "\n")
                    out_file.flush()
    finally:
        if unload_after:
            ollama_unload(MODEL)
            gpu_cleanup()

    return results
