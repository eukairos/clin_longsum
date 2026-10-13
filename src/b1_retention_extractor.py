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
b1_retention_extractor.py
=========================

Extracts atomic clinical facts from each admission's BHC and tags them on the
three salience axes: salience_tier x retention_class x
status modifiers. These facts are the LOSSLESS layer that makes lossy prose
compaction safe in Phase C.

  - model/params from config.MODELS["retention_extractor"] (swappable)
  - output is a LIST of three-axis facts
  - ENUM-CONSTRAINED structured output (Ollama format=<json schema>): the model
    cannot invent a retention_class or modifier value.
  - PATIENT-SEQUENTIAL with known_conditions fed forward
  - extracts from full bhc_text (ledger must catch facts wherever stated)
  - deterministic surveillance-finding promotion (tax.is_surveillance_finding):
    an incidental/suspicious ?malignancy is forced to negative_kind=surveillance
    even if the model omitted it (B2 applies the same net on load).

HOLDOUT / PRODUCTION MODEL: b1 extracts the ESTABLISHED span only --
still-held-out (unpromoted) admissions are excluded at the cohort-read boundary
(holdout.established), so retention_facts.jsonl never contains one. 
Production works the same way (holdout = pending physician review; src/ingest inserts new admissions 
held out and appends them to cohort_admissions.csv). A promoted admission's facts -- physician-approved, 
or a patient's first admission auto-folded by the ingest -- are extracted at fold-in time via the
single-admission mode:
    python b1_retention_extractor.py --subject S --hadm H
which reads the admission from cohort_admissions.csv (it must already be projected there),
extracts it (feed-forward known_conditions rebuilt from that patient's EARLIER stored facts)
and splices it into retention_facts.jsonl in place. foldin_patient.py calls this as the
FIRST step of the fold-in chain, before holdout.promote() + b2.

Source: data/interim/cohort_admissions.csv
Output: data/interim/retention_facts.jsonl, data/output/generation_audit.jsonl

Resumable at PATIENT granularity (see _prune_incomplete_patients).

Run:
    cd ~/clin_longsum/src
    python b1_retention_extractor.py --fresh                # re-extract the whole cohort
    python b1_retention_extractor.py --limit 3              # smoke test (first 3 patients)
    python b1_retention_extractor.py --subject S            # re-extract ONE subject_id (all its established
                                                            #   admissions); strips + rewrites that subject's
                                                            #   rows in place -- use to repair/re-do a patient
    python b1_retention_extractor.py --target 50            # top up on-disk output to 50 COMPLETE patients
                                                            #   then stop (processes only the shortfall)
    python b1_retention_extractor.py --subject S --hadm H   # fold-in: one admission, splice in
  (--target N / --limit N cap how many COMPLETE patients to (re)process; resume-safe.
   --subject re-does a whole subject; --subject + --hadm re-does a single admission.)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from typing import Optional

import pandas as pd
import ollama

import config
import holdout
import retention_taxonomy as tax
from roles.contracts import (
    AuditRecord,
    RetentionExtractorInput,
    RetentionExtractorOutput,
    RetentionFact,
)

import io_utils

sys.path.insert(0, str(config.SYNTH_DIR))
from text_cleaner import clean_for_llm   # noqa: E402
import ollama_utils as ou                # noqa: E402  (shared unload / VRAM hygiene)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("b1_retention_extractor")

SPEC = config.model_for("retention_extractor")
PROMPT_VERSION = config.prompt_version("retention_extractor")
NUM_CTX = int(SPEC.params.get("num_ctx", 16384))
TEMPERATURE = float(SPEC.params.get("temperature", 0.0))

_KEEP_ALIVE_BATCH = -1

COHORT_CSV = config.INTERIM_DIR / "cohort_admissions.csv"
FACTS_JSONL = config.RETENTION_FACTS_JSONL
AUDIT_JSONL = config.AUDIT_JSONL

# Enum-constrained structured-output schema. retention_class / salience_tier /
# certainty / lifecycle / occurrence are restricted to the taxonomy's own
# vocabularies, so the model cannot invent a value or smuggle a modifier into
# the class field.
_FACTS_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "retention_class": {"type": "string", "enum": tax.class_ids()},
                    "salience_tier": {"type": "string", "enum": tax.tier_ids()},
                    "certainty": {"type": "string", "enum": list(tax.CERTAINTY)},
                    "lifecycle": {"type": "string", "enum": list(tax.LIFECYCLE)},
                    "occurrence": {"type": "string", "enum": list(tax.OCCURRENCE)},
                    "negative_kind": {"type": ["string", "null"], "enum": [None, *tax.NEGATIVE_KIND]},
                    "parent_condition": {"type": ["string", "null"]},
                    "rationale": {"type": "string"},
                },
                "required": ["text", "retention_class", "salience_tier",
                             "certainty", "lifecycle", "occurrence", "negative_kind"],
            },
        },
    },
    "required": ["facts"],
}


# ── Prompts ──────────────────────────────────────────────────────────────────

_SYSTEM = (
    "You are a clinical informatics assistant extracting structured, atomic "
    "clinical facts from hospital discharge summaries and triaging each for "
    "lifetime salience. Return only valid JSON matching the requested schema."
)

_EXTRACT_TMPL = """\
Extract atomic clinical facts from the discharge note and tag each on three axes.

{taxonomy}

CONDITIONS ALREADY ESTABLISHED in this patient's earlier admissions (use to set
occurrence = index vs recurrence; to flag a previously-active condition that is
now RESOLVED; and to flag one whose earlier RECORD has been retracted or replaced
by this admission - lifecycle=superseded. Superseded applies only to a condition
ALREADY in this list. A drug stopped or switched because of a reaction is NOT
superseded: the contraindication stands, lifecycle=active):
{known}

Return a JSON object {{"facts": [ ... ]}} where each fact has: text,
retention_class, salience_tier, certainty, lifecycle, occurrence,
negative_kind (or null), parent_condition (or null), rationale (<= 12 words).

Reminders:
- One fact = one clinical idea. Do not merge multiple problems.
- SPLIT a compound of DISTINCT problems joined by "/", ",", or "and" into one fact
  each (e.g. "depression/anxiety" -> depression AND anxiety; "COPD/asthma" -> COPD
  AND asthma), each tagged on its own axes.
- REDACTIONS: "___" is a removed de-identification token, not a fact. A disease named
  only as "___ disease" is an EXPLICIT UNKNOWN. Extract the described features
  (neuropathy, nonhealing wound, amputation) but never fill "___" with a specific
  diagnosis, however typical.  
- Extract PAST SURGICAL / PROCEDURAL history as its own fact even when only mentioned
  in passing PMH/PSH: anatomy-altering or permanent procedures (ostomy creation or
  takedown, amputation, organ resection, bypass/graft, transplant, indwelling hardware)
  -> anatomy_altering_surgery or implanted_device.
- DECOMPOSE embedded etiology: when a fact names a condition or device "secondary to"
  or "due to" an independently salient event (endocarditis, MI, sepsis, VTE,
  malignancy), emit that underlying event as its OWN fact too, not only inside the
  parent phrase.
- Never encode certainty, lifecycle, severity, or occurrence in retention_class;
  use the dedicated fields. A mild reaction is severe_adr_allergy at a LOW tier,
  not a new class.
- Apply the EXTRACTION RULES above: surface a chronic/permanent substrate that is
  NAMED in the note, even when it appears only as background or a medication
  indication. Surface the SUBSTRATE THE NOTE STATES -- do not infer an unnamed
  diagnosis from a drug, a complication, or a redacted "___". If the note gives a
  drug or sequela but never names the underlying disease, emit the named finding
  (the drug's use, the neuropathy, the ulcer) - NOT a guessed cause; emit two facts
  for acute-on-chronic; set negative_kind for documented negatives, resolved-relevant
  events, and incidental/suspicious surveillance findings (the last also suspected).

DISCHARGE NOTE:
{note}
"""

_FEEDBACK_TMPL = """\
Review the tagged facts against the note. Focus on the dangerous errors:

1. Any ruled-out or merely suspected item tagged as if confirmed?
2. Any resolved / de-labelled item still tagged active?
3. Any FIRST presentation of a chronic disease tagged recurrent_episode (T4)
   instead of chronic_disease_state (T3) with occurrence=index?
4. Any missed operational alert (T1): severe ADR/allergy, code status,
   resistant organism, safety-critical device?
5. SUBSTRATE CHECK (both directions):
   (a) any chronic disease, transplant, or dialysis-dependence NAMED in the note but
       present only as background / medication indication, not emitted as its own
       chronic_disease_state / permanent_condition / altered_immune_status fact?
   (b) any chronic-substrate fact whose named diagnosis is NOT actually in the note --
       inferred from a drug, a complication, or a "___" redaction? Mark it for removal.
6. Any incidental nodule/mass/suspicious finding not flagged suspected +
   negative_kind=surveillance? Any resolved-but-relevant event (provoked VTE/PE,
   significant fracture) not set negative_kind=resolved_relevant?
7. Any recurrent_episode (T4) missing parent_condition?
8. Any fact absent from the note (hallucination)?
9. Any "/"-, comma-, or "and"-joined compound of DISTINCT problems left as a
   single fact (each should be its own)?
10. Any anatomy-altering surgery or permanent procedural history (ostomy, amputation,
   resection, transplant, device) in the PMH/PSH not emitted as its own fact?
11. Any "X secondary to / due to Y" where Y is independently salient (endocarditis,
   MI, sepsis, VTE, malignancy) but not emitted as its own fact?

At most 8 issues, each <= 12 words. If nothing is wrong, complete=true, empty list.

Return ONLY: {{"complete": true_or_false, "issues": ["short issue", "..."]}}

DISCHARGE NOTE:
{note}

TAGGED FACTS:
{facts}
"""

_UPDATE_TMPL = """\
Fix the listed issues in the facts. Preserve correct facts unchanged. Do not add
facts absent from the note. Return ONLY {{"facts": [...]}}.

ISSUES:
{issues}

CURRENT FACTS:
{facts}
"""


# ── Ollama call + JSON recovery ───────────────────────────────────────────────

def _chat_raw(messages: list[dict], keep_alive: int = _KEEP_ALIVE_BATCH, fmt="json",
              temperature: float | None = None, num_ctx: int | None = None):
    """Full ollama response (carries done_reason: 'stop' vs 'length' for truncation)."""
    return ollama.chat(
        model=SPEC.model_id,
        messages=messages,
        think=SPEC.think,
        format=fmt,                       # "json" or an enum-constrained JSON schema
        options={"temperature": TEMPERATURE if temperature is None else temperature,
                 "num_ctx": NUM_CTX if num_ctx is None else num_ctx},
        keep_alive=keep_alive,
    )


def _chat(messages: list[dict], keep_alive: int = _KEEP_ALIVE_BATCH, fmt="json",
          temperature: float | None = None) -> str:
    return _chat_raw(messages, keep_alive, fmt, temperature).message.content.strip()


def _recover_truncated_json(text: str) -> Optional[dict]:
    """Salvage a length-truncated {"facts":[...]} by keeping every COMPLETE fact
    object and dropping only the incomplete trailing one. String-aware brace match,
    so it handles the common 'cut right after a complete }' shape that the old
    last-quote heuristic broke (it stripped back past the valid brace)."""
    i = text.find('"facts"')
    lb = text.find("[", i) if i >= 0 else -1
    if lb < 0:
        return None
    objs: list = []
    depth = 0
    start: Optional[int] = None
    in_str = esc = False
    for j in range(lb + 1, len(text)):
        ch = text[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = j
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    objs.append(json.loads(text[start:j + 1]))
                except json.JSONDecodeError:
                    pass
                start = None
        elif ch == "]" and depth == 0:
            break
    return {"facts": objs} if objs else None


def _parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            rec = _recover_truncated_json(m.group())
            if rec is not None:
                logger.warning("JSON truncation recovered; some facts may be lost.")
                return rec
    raise ValueError(f"Could not parse JSON:\n{text[:400]}")


# Automatic truncation recovery. Root cause: thinking-mode reasoning consumes the
# num_ctx budget and generation is cut mid-array (done_reason="length"). Strategy,
# fully automatic (no manual rerun): salvage every COMPLETE fact from a cut output
# (so nothing already emitted is lost), then retry at a perturbed temperature (and,
# if RECOVER_NUM_CTX is set, more context budget) for a COMPLETE set. A clean stop
# returns immediately; an all-truncated run returns the largest salvaged set and the
# caller flags the audit. Raise only if NO attempt produced parseable JSON.
RECOVER_RETRIES = 2
RECOVER_NUM_CTX: int | None = None   # e.g. 49152 to give truncating admissions more budget (watch VRAM)


def _chat_json(messages: list[dict], fmt, label: str = "") -> tuple[dict, dict]:
    lbl = f"{label} " if label else ""
    temps = (None, 0.3, 0.5)
    best_part: Optional[dict] = None
    best_part_n = -1
    last_exc: Optional[Exception] = None
    for attempt in range(RECOVER_RETRIES + 1):
        temp = temps[min(attempt, len(temps) - 1)]
        nctx = RECOVER_NUM_CTX if (attempt > 0 and RECOVER_NUM_CTX) else None
        resp = _chat_raw(messages, fmt=fmt, temperature=temp, num_ctx=nctx)
        truncated = getattr(resp, "done_reason", None) == "length"
        try:
            data = _parse_json(resp.message.content.strip())
        except ValueError as exc:
            last_exc = exc
            logger.warning("%sparse failed (attempt %d/%d, done=%s)%s", lbl, attempt + 1,
                           RECOVER_RETRIES + 1, getattr(resp, "done_reason", "?"),
                           "; retrying" if attempt < RECOVER_RETRIES else "")
            continue
        if not truncated:
            return data, {"truncated": False, "attempts": attempt + 1}
        n = len(data.get("facts", [])) if isinstance(data, dict) else 0
        if n > best_part_n:
            best_part, best_part_n = data, n
        logger.warning("%struncated (done_reason=length); salvaged %d facts, retrying for full set",
                       lbl, n)
    if best_part is not None:
        logger.warning("%sstill truncated after %d attempts; returning salvaged %d facts "
                       "-> audit flagged for review", lbl, RECOVER_RETRIES + 1, best_part_n)
        return best_part, {"truncated": True, "attempts": RECOVER_RETRIES + 1}
    raise last_exc if last_exc is not None else ValueError("Could not parse JSON")


# Shared lifecycle helpers (vendor/ollama_utils); names kept for b1_rerun_hadms.
gpu_cleanup = ou.gpu_cleanup


def ollama_unload() -> None:
    ou.ollama_unload(SPEC.model_id)


# ── Normalisation (enum output means class/tier are already valid; we still
#    coerce defensively and never drop a fact) ─────────────────────────────────

def _normalise_fact(raw: dict, hadm_id: int, admit_seq: int) -> Optional[RetentionFact]:
    text = str(raw.get("text", "")).strip()
    if not text:
        return None

    rc = str(raw.get("retention_class", "")).strip().lower().replace(" ", "_").replace("-", "_")
    if not tax.is_known_class(rc):
        logger.warning("unexpected retention_class %r (text=%r) -> transient_event (hadm=%s)",
                       rc, text[:60], hadm_id)
        rc = "transient_event"

    tier = str(raw.get("salience_tier", "")).strip().upper()
    if not tax.is_known_tier(tier):
        tier = tax.default_tier_for_class(rc) or "T5"
    # v2.10: the per-class default_tier is a FLOOR -- the model may escalate for safety but
    # must not de-escalate below it (a chronic_disease_state mis-tagged T4/T5 must not silently
    # fall out of the ledger). Floor to the class default; escalations (e.g. device -> T1) kept.
    tier = tax.floor_tier(tier, tax.default_tier_for_class(rc))

    cert = str(raw.get("certainty", "confirmed")).strip().lower()
    cert = cert if cert in tax.CERTAINTY else "confirmed"
    life = str(raw.get("lifecycle", "active")).strip().lower()
    life = life if life in tax.LIFECYCLE else "active"
    occ = str(raw.get("occurrence", "index")).strip().lower()
    occ = occ if occ in tax.OCCURRENCE else "index"

    nk = raw.get("negative_kind")
    nk = str(nk).strip().lower() if nk not in (None, "", "null") else None
    if nk not in tax.NEGATIVE_KIND:
        nk = None
    # Deterministic safety net: an incidental/suspicious ?malignancy is forced to
    # negative_kind=surveillance even if the model omitted it (asymmetric cost). Does
    # not override an explicit resolved_relevant/absent_or_negative the model already set.
    if nk is None and tax.is_surveillance_finding(text):
        nk = "surveillance"

    parent = raw.get("parent_condition")
    parent = str(parent).strip() if parent not in (None, "", "null") else None
    rationale = raw.get("rationale")
    rationale = str(rationale).strip() if rationale else None

    fact = RetentionFact(
        text=text, retention_class=rc, salience_tier=tier,
        certainty=cert, lifecycle=life, occurrence=occ,
        negative_kind=nk, parent_condition=parent, rationale=rationale,
        source_hadm_id=hadm_id, admit_seq=admit_seq,
    )
    fact.assign_fact_id()
    return fact


def _extract_admission(inp: RetentionExtractorInput) -> RetentionExtractorOutput:
    note = clean_for_llm(inp.bhc_text)
    if not note:
        raise ValueError(f"empty note after cleaning (hadm_id={inp.hadm_id})")

    known = "\n".join(f"- {c}" for c in inp.known_conditions) or "(none -- first admission)"
    sys_msg = {"role": "system", "content": _SYSTEM}

    # Step A: extract (enum-constrained). Automatic truncation recovery in
    # _chat_json: salvage complete facts + perturbed retries for a full set. A
    # still-partial result is returned and the audit is flagged (never dropped).
    raw_a, rec_meta = _chat_json(
        [sys_msg, {"role": "user", "content": _EXTRACT_TMPL.format(
            taxonomy=tax.render_for_prompt(), known=known, note=note)}],
        fmt=_FACTS_SCHEMA, label="Step-A")
    facts_raw = raw_a.get("facts", []) if isinstance(raw_a, dict) else []

    # Step B: feedback (free json)
    try:
        raw_b = _parse_json(_chat([sys_msg, {"role": "user", "content": _FEEDBACK_TMPL.format(
            note=note, facts=json.dumps({"facts": facts_raw}, indent=2))}], fmt="json"))
        complete = raw_b.get("complete", True)
        issues = raw_b.get("issues", []) or []
    except Exception as exc:
        logger.warning("feedback step failed (hadm=%s): %s", inp.hadm_id, exc)
        complete, issues = True, []

    # Step C: update if needed (enum-constrained)
    if not complete and issues:
        try:
            raw_c = _parse_json(_chat([sys_msg, {"role": "user", "content": _UPDATE_TMPL.format(
                issues="\n".join(f"- {i}" for i in issues),
                facts=json.dumps({"facts": facts_raw}, indent=2))}], fmt=_FACTS_SCHEMA))
            if isinstance(raw_c, dict) and isinstance(raw_c.get("facts"), list):
                facts_raw = raw_c["facts"]
        except Exception as exc:
            logger.warning("update step failed (hadm=%s): keeping step A: %s", inp.hadm_id, exc)

    facts = [f for f in (_normalise_fact(r, inp.hadm_id, inp.admit_seq)
                         for r in facts_raw if isinstance(r, dict)) if f]

    audit = AuditRecord.from_spec(
        SPEC, PROMPT_VERSION, taxonomy_version=tax.TAXONOMY_VERSION,
        subject_id=inp.subject_id, input_hadm_ids=[inp.hadm_id],
        output_text=json.dumps([f.text for f in facts]),
        scores={"n_facts": len(facts), "n_ledger": sum(f.in_ledger for f in facts),
                "recovered": bool(rec_meta.get("truncated"))},
        status="flagged" if rec_meta.get("truncated") else "ok",
    )
    return RetentionExtractorOutput(
        subject_id=inp.subject_id, hadm_id=inp.hadm_id,
        admit_seq=inp.admit_seq, facts=facts, audit=audit,
    )


def _update_known(known: list[str], out: RetentionExtractorOutput) -> list[str]:
    seen = {k.lower() for k in known}
    for f in out.facts:
        if f.in_ledger and f.salience_tier in ("T1", "T2", "T3") and f.text.lower() not in seen:
            known.append(f.text)
            seen.add(f.text.lower())
    return known


# ── Resume bookkeeping (patient-level) ────────────────────────────────────────

_read_jsonl_safe = io_utils.read_jsonl          # shared tolerant reader (io_utils)


def _prune_incomplete_patients(counts: dict[int, int]) -> set[int]:
    rows = _read_jsonl_safe(FACTS_JSONL)
    if not rows:
        return set()
    by_pid: dict[int, list[dict]] = {}
    for r in rows:
        by_pid.setdefault(int(r["subject_id"]), []).append(r)
    complete = {pid for pid, rs in by_pid.items() if len(rs) >= counts.get(pid, 10**9)}
    kept = [r for r in rows if int(r["subject_id"]) in complete]
    if len(kept) != len(rows):
        FACTS_JSONL.write_text("".join(json.dumps(r) + "\n" for r in kept), encoding="utf-8")
        logger.info("resume: kept %d complete patients, dropped %d partial lines",
                    len(complete), len(rows) - len(kept))
    return complete


def _rebuild_known_from_stored(records: list[dict], subject_id: int, before_seq: int) -> list[str]:
    """Reconstruct the feed-forward known_conditions a one-off extraction should see:
    the in_ledger T1-T3 fact texts from this patient's EARLIER stored admissions
    (admit_seq < before_seq), in admit_seq order. Mirrors _update_known on stored dicts;
    in_ledger recomputed via tax so it tracks the current taxonomy."""
    known: list[str] = []
    seen: set[str] = set()
    earlier = sorted(
        (r for r in records
         if int(r["subject_id"]) == subject_id and int(r.get("admit_seq", -1)) < before_seq),
        key=lambda r: int(r.get("admit_seq", -1)))
    for r in earlier:
        for f in r.get("facts", []):
            in_ledger = tax.is_ledger_member(
                f.get("salience_tier"), f.get("certainty", "confirmed"),
                f.get("lifecycle", "active"), f.get("negative_kind"), f.get("retention_class"))
            if in_ledger and f.get("salience_tier") in ("T1", "T2", "T3"):
                t = f.get("text", "")
                if t and t.lower() not in seen:
                    known.append(t)
                    seen.add(t.lower())
    return known


def extract_one_admission(subject_id: int, hadm_id: int) -> int:
    """Fold-in entry point: extract ONE admission's facts and splice into
    retention_facts.jsonl in place (idempotent -- replaces any existing record for the
    hadm). known_conditions are rebuilt from the patient's earlier stored admissions, so
    the one-off sees the same prior context a full sequential pass would. Returns the
    number of facts extracted. Used by foldin_patient.py before b2."""
    if not COHORT_CSV.exists():
        raise SystemExit(f"{COHORT_CSV} not found.")
    df = pd.read_csv(COHORT_CSV).sort_values(["subject_id", "admit_seq"]).reset_index(drop=True)
    match = df[(df["subject_id"] == subject_id) & (df["hadm_id"] == hadm_id)]
    if match.empty:
        raise SystemExit(f"admission subject={subject_id} hadm={hadm_id} not in {COHORT_CSV.name}")
    row = match.iloc[0]
    seq = int(row["admit_seq"])

    records = _read_jsonl_safe(FACTS_JSONL)
    known = _rebuild_known_from_stored(records, subject_id, seq)
    inp = RetentionExtractorInput(
        subject_id=subject_id, hadm_id=hadm_id, admit_seq=seq,
        bhc_text=str(row["bhc_text"]) if pd.notna(row.get("bhc_text")) else "",
        admit_date=str(row.get("admittime")) if pd.notna(row.get("admittime")) else None,
        service=str(row.get("service")) if pd.notna(row.get("service")) else None,
        known_conditions=known,
    )
    try:
        out = _extract_admission(inp)
    finally:
        ollama_unload()

    rec = json.loads(out.model_dump_json())
    pos = next((i for i, r in enumerate(records) if int(r.get("hadm_id", -1)) == hadm_id), None)
    if pos is None:
        records.append(rec)
    else:
        records[pos] = rec
    records.sort(key=lambda r: (int(r["subject_id"]), int(r.get("admit_seq", -1))))
    tmp = FACTS_JSONL.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    tmp.replace(FACTS_JSONL)
    with AUDIT_JSONL.open("a", encoding="utf-8") as fh:
        fh.write(out.audit.model_dump_json() + "\n")
    logger.info("fold-in extract: subject=%s hadm=%s seq=%s -> %d facts (%d pinned); spliced into %s",
                subject_id, hadm_id, seq, len(out.facts),
                sum(f.in_ledger for f in out.facts), FACTS_JSONL.name)
    return len(out.facts)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="process at most N patients this run")
    ap.add_argument("--target", type=int, default=None,
                    help="ensure the output holds this many COMPLETE patients total (resume-safe)")
    #ap.add_argument("--patient", type=int, default=None, help="process one subject_id")
    ap.add_argument("--subject", type=int, default=None,
                    help="process one subject_id; else with --hadm, extract ONE admission and splice into the facts file")
    ap.add_argument("--hadm", type=int, default=None,
                    help="fold-in: the hadm_id to extract (requires --subject)")
    ap.add_argument("--fresh", action="store_true", help="ignore + overwrite existing output")
    args = ap.parse_args()

    config.check_paths()

    # Fold-in single-admission mode: extract ONE admission and splice it in. This is the
    # production path (and the dev fold-in path) for adding the incoming admission's facts.
    if args.hadm is not None:
        if args.subject is None or args.hadm is None:
            raise SystemExit("fold-in mode requires BOTH --subject and --hadm")
        extract_one_admission(args.subject, args.hadm)
        return

    if not COHORT_CSV.exists():
        raise SystemExit(f"{COHORT_CSV} not found. Run a2_build_cohort.py first.")

    df = pd.read_csv(COHORT_CSV).sort_values(["subject_id", "admit_seq"]).reset_index(drop=True)
    # Extract the ESTABLISHED span only -- never a still-held-out (unapproved) admission,
    # in dev and production alike. The approved admission is added later via
    # extract_one_admission at fold-in. (No-op only if HOLDOUT_LAST_ADMISSION is False,
    # the unreviewed batch mode.)
    n_before = len(df)
    df = holdout.established(df).reset_index(drop=True)
    if len(df) != n_before:
        logger.info("holdout: excluded %d still-held-out admission(s) from extraction "
                    "(established span only)", n_before - len(df))
    counts = df.groupby("subject_id")["hadm_id"].count().to_dict()

    if args.fresh and FACTS_JSONL.exists():
        FACTS_JSONL.unlink()
    done_pids = set() if args.fresh else _prune_incomplete_patients(counts)

    pids = [int(p) for p in df["subject_id"].drop_duplicates().tolist()]
    if args.subject is not None and args.hadm is None:
        rows = _read_jsonl_safe(FACTS_JSONL)
        kept = [r for r in rows if int(r["subject_id"]) != args.subject]
        if len(kept) != len(rows):
            FACTS_JSONL.write_text("".join(json.dumps(r) + "\n" for r in kept), encoding="utf-8")
        pids = [args.subject]
        done_pids.discard(args.subject)

    todo = [p for p in pids if p not in done_pids]
    if args.target is not None:
        todo = todo[: max(args.target - len(done_pids), 0)]
    if args.limit is not None:
        todo = todo[: args.limit]
    logger.info("patients: %d total, %d done, %d to process", len(pids), len(done_pids), len(todo))
    if not todo:
        logger.info("nothing to do.")
        return

    facts_out = FACTS_JSONL.open("a", encoding="utf-8")
    audit_out = AUDIT_JSONL.open("a", encoding="utf-8")
    n_adm = n_facts = n_ledger = 0
    try:
        for i, pid in enumerate(todo):
            grp = df[df["subject_id"] == pid]
            known: list[str] = []
            logger.info("patient %d/%d (subject_id=%s, %d admissions)", i + 1, len(todo), pid, len(grp))
            for _, row in grp.iterrows():
                inp = RetentionExtractorInput(
                    subject_id=int(row["subject_id"]), hadm_id=int(row["hadm_id"]),
                    admit_seq=int(row["admit_seq"]),
                    bhc_text=str(row["bhc_text"]) if pd.notna(row.get("bhc_text")) else "",
                    admit_date=str(row.get("admittime")) if pd.notna(row.get("admittime")) else None,
                    service=str(row.get("service")) if pd.notna(row.get("service")) else None,
                    known_conditions=list(known),
                )
                try:
                    out = _extract_admission(inp)
                except Exception as exc:
                    logger.error("hadm=%s failed: %s", inp.hadm_id, exc, exc_info=True)
                    err = AuditRecord.from_spec(
                        SPEC, PROMPT_VERSION, taxonomy_version=tax.TAXONOMY_VERSION,
                        subject_id=inp.subject_id, input_hadm_ids=[inp.hadm_id],
                        status="error", error=str(exc))
                    audit_out.write(err.model_dump_json() + "\n"); audit_out.flush()
                    out = RetentionExtractorOutput(
                        subject_id=inp.subject_id, hadm_id=inp.hadm_id,
                        admit_seq=inp.admit_seq, facts=[], audit=err)
                facts_out.write(out.model_dump_json() + "\n"); facts_out.flush()
                audit_out.write(out.audit.model_dump_json() + "\n"); audit_out.flush()
                known = _update_known(known, out)
                n_adm += 1
                n_facts += len(out.facts)
                n_ledger += sum(f.in_ledger for f in out.facts)
    finally:
        facts_out.close()
        audit_out.close()
        ollama_unload()

    logger.info("=" * 55)
    logger.info("B1 done. admissions=%d facts=%d ledger=%d (%.0f%% pinned)",
                n_adm, n_facts, n_ledger, 100 * n_ledger / max(n_facts, 1))
    logger.info("facts -> %s", FACTS_JSONL)
    logger.info("Next: B2 ledger projection.")


if __name__ == "__main__":
    main()
