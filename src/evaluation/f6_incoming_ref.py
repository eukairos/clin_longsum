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
f6_incoming_ref.py
===================
Phase F extrinsic metric #3 - CURRENT ADMISSION'S HUMAN SUMMARY vs THE ESTABLISHED LEDGER.

Every patient's CURRENT (incoming) admission carries a real, clinician-authored BHC - a
free gold-standard reference for "what a physician thought worth recording". This stage
checks the established retention ledger (built from PRIOR admissions) against that human
summary, on two axes computed from ONE comparison.

GROUND TRUTH SOURCE: incoming_summary.jsonl `bhc_text` - the real, unedited clinician BHC
for the current admission.

ESTABLISHED RECORD: the retention ledger (the salient, auditable, clinician-visible store),
NOT retention_facts.jsonl. We measure against what the pipeline actually surfaces to a
clinician doing an audit, not the raw fact stream. (retention_facts.jsonl IS used, but only
as the PASS 2b reference to tell a true retention gap from a genuinely new fact - see below.)

WHY A TIERING STEP (the core of the design):
  The two sides speak different languages. The ledger side is already tiered (b1/b2 ran the
  deterministic taxonomy over prior admissions). The BHC side is free prose. To compare like
  with like we must tier the BHC's facts too - using the SAME taxonomy instructions b1 used
  (tax.render_for_prompt(), seeded into the extraction prompt), so the BHC side is tiered as
  close to the pipeline's own classifier as prose allows. We ALSO tag each BHC fact's ORIGIN
  (pre_existing vs arose_this_admission), because a fact that legitimately arose THIS
  admission (a new acute diagnosis, a code-status change) is not something the ledger, built
  before this admission, could ever have contained, so comparing it is a category error.

COMPARABLE SET (what actually gets compared, after tiering both sides):
  KEEP   T1-T3 AND origin=pre_existing.
  DROP   all T4 (recurrent episodes - implicit in their T3 parent) and T5 (transient: a
         distant T5 isn't salient now; a current-admission T5 is admission-local) on BOTH
         sides; and DROP T1-T3 facts that clearly arose THIS admission.
  DEDUP  by lowercased fact text. The Pass 1 compound split emits the bare
         established condition as its own fact, so a condition named twice in the BHC - once
         bare, once inside a compound - yielded two identical comparable facts, judged twice
         and counted twice in both the bucket and the denominator.
  Ambiguous origin -> pre_existing (CONSERVATIVE: a borderline fact stays in the comparison
  rather than being silently excused).

TWO PASSES, FOUR OUTCOMES, TWO RATES (coherence folded in):
  PASS 2a - align each comparable BHC fact against the rendered T1-T3 ledger:
    retained      - a ledger entry represents it (synonym/paraphrase/abbrev/inference OK)
    contradicted  - a ledger entry directly asserts something incompatible
    (anything else falls through to PASS 2b as a provisional 'missing')
  PASS 2b - the provisional misses are aligned against the patient's PRIOR RAW FACTS
  (retention_facts.jsonl, every NON-holdout admission's b1 extractions, holdout EXCLUDED):
    missing            - the condition IS in prior facts but NOT in the ledger: the
                          pipeline extracted it earlier and then DROPPED it -> a TRUE
                          retention gap (b1 had it, b2/consolidation lost it).
    not_in_prior_data  - the condition is in NO prior admission's facts: it is genuinely
                          NEW to the current admission (the clinician carried it as history
                          from their own knowledge / outside our dataset). The ledger, built
                          from prior admissions, was CORRECT not to contain it. EXCLUDED
                          from the denominator (like f4's not_addressable). Confirmed on
                          pilot: HTN/90000023 and AF/90000014 are real new-to-holdout facts,
                          absent from all earlier admissions; f6 is good at surfacing these
                          (see NOT_IN_PRIOR_DATA gotcha for the other-uses note).
  comparable = retained + missing + contradicted      (not_in_prior_data excluded)
    salience_retention = retained / comparable      (Tier 2 - retention of facts the
                                                     pipeline DEMONSTRABLY had)
    coherence          = 1 - contradicted/comparable (Tier 1 - is the ledger stale/wrong)
  Coherence is an ALARM, not a gradient: after the negative-rendering fix and the origin
  filter, contradicted should be near-zero, so coherence sits at ~1.0 by design and only
  moves on a genuine stale/wrong ledger fact. That is the intended behaviour.

NOT_IN_PRIOR_DATA - OTHER USES: the same engine (extract+tier+diff a new
note against the established record) has uses beyond eval. (1) LIVE FOLD-IN RECONCILIATION:
at the point a new admission is approved, run this diff to show the clinician what the note
contradicts or what is missing from the problem list - a point-of-care reconciliation step,
acting on contradicted + missing. (2) NEW-CONDITION SURFACING: the not_in_prior_data bucket
IS a feature, not just an excluded denominator - it is "what chronic conditions this
admission introduced for the first time", so they get promoted into the longitudinal record
rather than lost after discharge. #1 and #2 are the same engine, opposite ends of the
output (#1 = what is wrong with the incoming admission vs history; #2 = what is genuinely new
that history should now absorb). 

LEDGER RENDERING: a ledger negative_kind is NOT an assertion that the patient HAS the
thing - flattening everything to "NEGATIVE: X" made the judge read "established: history
of PE" as contradicting "patient has PE". Rendered on real b2 fields (negative_kind),
keyed in _ledger_text()/_kind_line() so the judge can tell them apart:
    negative_kind == "surveillance"       -> "under surveillance for: X"
    negative_kind == "absent_or_negative" -> "documented absent/excluded: X"
    negative_kind == "resolved_relevant"  -> "affirms (resolved, lifetime-relevant): X"
      (the whole point: a resolved PE must NOT read as "absent")
    considered_excluded list              -> "ruled out (worked up, excluded): X"
    everything else (confirmed node)      -> "affirms: X"
    everything else (flagged/suspected)   -> "affirms ({certainty}): X"
Only T1-T3 ledger nodes are rendered (the comparable set can never match a T4/T5 line, so
rendering them is pure noise/false-contradiction surface).

GOTCHAS:
  - Shared low-level judge machinery only (f4._chat/_cap/_ollama_unload/SPEC). f6 owns its
    prompts and its parser. NOT f4.judge_unit (different label set, and f4's prompt has a
    weaker paraphrase permission that overstated misses in an earlier f6 pilot). Factoring a
    shared eval_judge.py (decompose + entailment, task prompts layered on) is still a
    deferred refactor - EVALUATION.md sec 16 / PIPELINE.md.
  - PARSING: deepseek-r1:32b fails first-attempt JSON ~1/3 of the time for FIXABLE reasons. _parse_array bracket-
    searches RAW text first (fence-split only as fallback), then a markdown-pattern fallback before paying for a reparse call.
  - TIERING IS AN LLM APPROXIMATION of the pipeline's deterministic classifier (the BHC is
    prose; we cannot run b1's Python on it). Seeding tax.render_for_prompt() keeps it as
    close as possible, but BHC-side vs ledger-side tier disagreement is a known, accepted
    limitation - not a bug to chase with more prompt tuning.
  - Resumable: writes the output jsonl after EACH patient, and on a non--fresh run skips
    patients already recorded with status != judge_error. Use --fresh to re-pilot (wipes the
    resume state); omit --fresh to resume an interrupted run.
  - COMPOUND-FACT SPLIT (Pass 1): a 30-patient pilot showed the dominant miss source was established conditions bundled
    with a this-admission qualifier ('AFib with CHADS2 3', 'CKD stage 4 with worsening Cr')
    being judged 'missing' because the qualifier is not in the ledger. Pass 1 splits the
    bare established condition from its current-admission severity/trend/treatment qualifier
    (the qualifier becomes its own arose_this_admission fact, filtered out), and sharpens
    acute-on-chronic + in-hospital-event origin cues (PEG placement, hospital-acquired ulcer,
    in-hospital death/arrest -> arose_this_admission). Genuine retention gaps (a chronic
    condition simply absent from the ledger) remain as the real metric signal.
  - PASS 2 IS ALIGNMENT: a 30-patient pilot found the judge returned 'missing' for 
    terse abbreviations that ARE in the ledger (AF vs 'atrial fibrillation', CRF vs 'chronic kidney disease',
    HL vs 'hyperlipidemia') - a single fact searched against a wall of ledger text under-matches. 
    ledger_consolidate solved the same lexical-vs-semantic problem by framing it as a GROUPING task with all
    candidates laid out side by side. Pass 2a/2b now do likewise: lists are numbered
    (ledger l0,l1,...; prior facts p0,p1,...; facts f0,f1,...) and presented together, and
    the judge ALIGNS each fact to an entry by clinical meaning rather than searching one at
    a time. Deepseek for both passes; if abbreviation under-matching
    persists, the fallback is to run these on qwen like ledger_consolidate does),
    at the cost of a mid-script model swap. NOTE: some residual 'missing' items are genuine
    review-queue material, NOT bugs - a condition rolled into a broader ledger entry, or
    sourced by the human summarizer from outside our dataset. The aim is to clear MECHANICAL misses 
    (abbreviations) so the queue is short enough that the real judgment calls get a human's attention.
  - PASS 2b COST: only the residual 'missing' facts (a handful per patient) are aligned
    against prior facts, so it is a small second call, not a full re-judge. prior_status
    'no_prior_facts' means the patient had no non-holdout prior facts to check against (rare;
    e.g. a single prior admission) - residuals stay 'missing' conservatively in that case.
  - Holdout-aware: only hadm_ids still in holdout.holdout_hadms() are scored, AND the prior-
    facts reference EXCLUDES the held-out admission's own facts (else every BHC fact would
    trivially be 'in prior data').
  - One model in VRAM (deepseek via f4.SPEC) for all passes; never load qwen alongside.
  - TRACK only, never a hard gate (EVALUATION.md sec 2/2a). Thresholds are PROVISIONAL
    placeholders - re-baseline from a clean 20-patient re-pilot, not from a broken run.

OUTPUTS:
  data/output/eval/incoming_reference.jsonl   one record per subject (facts + outcomes)
  data/output/eval/incoming_reference.json    aggregate (both rates) + gates + flagged lists
  Flagged contradictions / misses carry hadm_id + admit_seq + basis; new_to_current_admission
  carries the not_in_prior_data bucket (the #2 new-condition-surfacing feature).
EXIT CODE: 0 if both gates pass, else 1 (track-only -- never blocks CI).

    cd ~/clin_longsum/src/evaluation
    python f6_incoming_ref.py --fresh --limit 10        # pilot first
    python f6_incoming_ref.py --subject 90000029
    python f6_incoming_ref.py --threshold-retention 0.9 --threshold-coherence 0.98 --json-only
    python f6_incoming_ref.py --fresh
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys

import _bootstrap  # noqa: F401  (puts src/ on sys.path)
import config
import c1_compactor as c1
import holdout
import retention_taxonomy as tax
import f4_faithfulness as f4          # reuse low-level only: SPEC, _chat, _cap,
                                       # _ollama_unload, PROMPT_VERSION

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("f6_incoming_ref")

INCOMING_JSONL = config.INTERIM_DIR / "incoming_summary.jsonl"
LEDGER_JSONL = config.OUTPUT_DIR / "retention_ledger.jsonl"
OUT_DIR = config.OUTPUT_DIR / "eval"
OUT_JSONL = OUT_DIR / "incoming_reference.jsonl"
OUT_JSON = OUT_DIR / "incoming_reference.json"

COMPARABLE_TIERS = ("T1", "T2", "T3")
# Four outcomes now. not_in_prior_data is EXCLUDED from the denominator (like f4's
# not_addressable): the fact is genuinely new to the current admission - it never
# appeared in any prior admission's extracted facts - so the ledger, built from prior
# admissions, was correct not to contain it, and it is NOT a retention failure. See the
# NOT_IN_PRIOR_DATA gotcha. The first three are the comparable denominator.
OUTCOMES = ("retained", "missing", "contradicted", "not_in_prior_data")
DENOM_OUTCOMES = ("retained", "missing", "contradicted")
RETENTION_FACTS_JSONL = config.RETENTION_FACTS_JSONL
_RECOVERED_STATUSES = ("retried_nothink", "reparsed", "md_fallback")

# taxonomy prompt block, rendered once - seeds the BHC-side tiering with the SAME
# instructions b1 used to tier the ledger side.
_TAX_BLOCK = tax.render_for_prompt()


# ── prompts ───────────────────────────────────────────────────────────────────────

# PASS 1: extract + tier + origin-tag, in one call. Uses the real taxonomy block so the
# BHC side is tiered against the pipeline's own definitions.
_EXTRACT_SYS = (
    "You are a clinical fact extractor. You read one admission's discharge summary (BHC) "
    "and emit each atomic clinical fact, tagging each with a salience tier and a retention "
    "class from the taxonomy provided, plus whether the fact pre-existed this admission or "
    "arose during it. Use ONLY the taxonomy's definitions for tiering. Tag tiers as the "
    "taxonomy instructs; do not invent tiers.\n\n" + _TAX_BLOCK
)

_EXTRACT_TMPL = (
    "CURRENT ADMISSION BHC:\n<<<\n{text}\n>>>\n\n"
    "Extract atomic clinical facts. TWO decomposition rules matter most:\n"
    "  (1) SPLIT a stable condition from any current-admission qualifier bundled with it. "
    "'AFib with CHADS2 3 and goal INR' -> two facts: 'atrial fibrillation' (pre_existing) "
    "and 'CHADS2 3 / INR goal this admission' (arose_this_admission). 'CKD stage 4 with "
    "worsening creatinine' -> 'CKD stage 4' (pre_existing) + 'worsening creatinine' "
    "(arose_this_admission). Emit the BARE established condition as its own fact so a "
    "this-admission severity/trend/treatment detail does not drag the whole condition into "
    "'arose_this_admission'.\n"
    "  (2) ACUTE-ON-CHRONIC: 'acute X on chronic Y' is TWO facts - X arose_this_admission, "
    "Y pre_existing (e.g. 'acute toxic-metabolic encephalopathy on chronic vascular "
    "dementia' -> encephalopathy=arose_this_admission, dementia=pre_existing).\n\n"
    "For each fact assign:\n"
    "  - salience_tier: one of T1, T2, T3, T4, T5 per the taxonomy above.\n"
    "  - retention_class: one taxonomy class id.\n"
    "  - negative_kind: one of null, \"absent_or_negative\", \"resolved_relevant\", "
    "\"surveillance\" - per the taxonomy's negative_kind guidance (null if the fact is a "
    "straightforward affirmed condition).\n"
    "  - origin: \"pre_existing\" if the fact is a chronic condition, past history, baseline, "
    "or problem the patient carried INTO this admission (cues: 'history of', 'known', "
    "'chronic', 'h/o', 's/p' a past procedure, 'on home <drug> for', 'PMH', 'baseline', "
    "'longstanding'); \"arose_this_admission\" if it began, was first diagnosed, was done, "
    "or occurred THIS admission. Treat as arose_this_admission: a NEW acute diagnosis "
    "('presents with', 'admitted for', 'new diagnosis of', 'found to have', 'developed'); "
    "an IN-HOSPITAL procedure or device placed this stay (e.g. PEG tube placement, line "
    "placement, this-stay surgery); a hospital-acquired finding (pressure/decubitus ulcer "
    "developed in hospital); an in-hospital event (cardiac arrest, death, a code-status "
    "change made this stay); and any current severity/trend/management qualifier split out "
    "under rule (1). If a condition is genuinely chronic but ambiguous as to WHEN it "
    "started, use \"pre_existing\".\n\n"
    "Do not emit the same fact twice.\n\n"
    "Return ONLY a JSON array, no prose, no markdown:\n"
    "[{{\"fact\": \"<short fact>\", \"salience_tier\": \"T1|T2|T3|T4|T5\", "
    "\"retention_class\": \"<class id>\", \"negative_kind\": false, "
    "\"origin\": \"pre_existing|arose_this_admission\"}}]"
)

# PASS 2a: ALIGN comparable BHC facts against the rendered T1-T3 ledger (both numbered
# lists, side by side). Alignment framing (not needle-search) + abbreviation examples.
_CMP_SYS = (
    "You align a clinician's carried-forward history against a patient's ESTABLISHED "
    "LEDGER (built from prior admissions). You are given TWO numbered lists: the LEDGER "
    "entries, and the FACTS the clinician carried into a new admission. For each fact you "
    "decide which ledger entry, if any, describes the SAME underlying clinical condition -- "
    "treating synonyms, abbreviations, and clinical paraphrase as the same (e.g. AF = "
    "atrial fibrillation; HTN = hypertension; CAD = coronary artery disease; CRF = CKD = "
    "chronic kidney disease/renal failure; HL = hyperlipidemia; s/p pleurodesis = recurrent "
    "pleural effusion managed by pleurodesis). This is an alignment task, not a keyword "
    "search: read both lists in full and match by clinical meaning. Use ONLY the two lists, "
    "no outside medical facts beyond knowing standard abbreviations/synonyms. Read the "
    "ledger entry prefixes: 'affirms: X' = record states X is/was present (including "
    "'affirms (resolved, lifetime-relevant): X' - the patient DID have X, now resolved, "
    "so a current note re-stating X is RETAINED, never contradicted); 'documented "
    "absent/excluded: X' = X recorded absent or screening-negative; 'ruled out: X' = X "
    "worked up and excluded; 'under surveillance for: X' = unconfirmed finding being watched."
)

_CMP_TMPL = (
    "LEDGER entries (prior admissions; T1-T3):\n<<<\n{source}\n>>>\n\n"
    "FACTS carried into the new admission (one per line, numbered f0, f1, ...):\n"
    "<<<\n{summary}\n>>>\n\n"
    "For EACH fact, find the ledger entry that describes the same condition (by synonym, "
    "abbreviation, or paraphrase), then label it exactly one of:\n"
    "  \"retained\"      - an 'affirms' ledger entry describes the same condition "
    "(differing treatment detail -- a changed drug/dose for an already-established "
    "condition -- is STILL retained, not contradicted).\n"
    "  \"missing\"       - NO ledger entry describes this condition, after genuinely "
    "checking abbreviations/synonyms. Use this only when nothing in the ledger matches.\n"
    "  \"contradicted\"  - the matching ledger entry asserts something incompatible: "
    "the fact says the patient HAS/HAD a condition the ledger records as 'ruled out' or "
    "'documented absent/resolved', or an allergy/laterality/stage conflict. ('under "
    "surveillance for: X' is NOT a contradiction of a later confirmed X.)\n\n"
    "Set 'basis' to the matched ledger entry text (for retained/contradicted) or a short "
    "note (for missing).\n\n"
    "Return ONLY a JSON array, one object per fact, no prose, no markdown:\n"
    "[{{\"fact\": \"<fact text>\", \"label\": "
    "\"retained|missing|contradicted\", \"basis\": \"<matched entry, or note>\"}}]"
)


# PASS 2b: for facts NOT resolved against the ledger (neither retained nor
# contradicted), align them against the patient's PRIOR raw extracted facts (every
# non-holdout admission's b1 facts, pre-consolidation). This separates a TRUE retention
# gap (b1 extracted it from a prior admission, but it is absent from the ledger -> the
# pipeline DROPPED it, label 'missing') from a fact that is simply NEW to the current
# admission (never in any prior admission's facts -> the ledger was right not to have it,
# label 'not_in_prior_data', excluded from the denominator).
_PRIOR_SYS = (
    "You decide, for each candidate fact, whether the SAME underlying clinical condition "
    "appears anywhere in a list of facts previously extracted from the patient's earlier "
    "admissions. Treat synonyms, abbreviations, and clinical paraphrase as the same "
    "(AF = atrial fibrillation; HTN = hypertension; CKD = CRF = chronic kidney disease; "
    "HL = hyperlipidemia). This is an alignment task, not a keyword search: read the prior "
    "facts in full and match by clinical meaning. Use ONLY the provided prior facts."
)

_PRIOR_TMPL = (
    "PRIOR FACTS (extracted from this patient's EARLIER admissions; numbered p0, p1, ...):\n"
    "<<<\n{source}\n>>>\n\n"
    "CANDIDATE FACTS (each absent from the current ledger; numbered f0, f1, ...):\n"
    "<<<\n{summary}\n>>>\n\n"
    "For EACH candidate fact, decide:\n"
    "  \"in_prior\"      - the SAME condition appears in the PRIOR FACTS (by synonym, "
    "abbreviation, or paraphrase). This means the pipeline HAD this fact earlier.\n"
    "  \"not_in_prior\"  - the condition appears NOWHERE in the prior facts. This means "
    "it is new to the current admission. Use this only after genuinely checking "
    "abbreviations/synonyms.\n\n"
    "Set 'basis' to the matched prior fact (for in_prior) or empty (for not_in_prior).\n\n"
    "Return ONLY a JSON array, one object per candidate fact, no prose, no markdown:\n"
    "[{{\"fact\": \"<fact text>\", \"label\": \"in_prior|not_in_prior\", "
    "\"basis\": \"<matched prior fact, or empty>\"}}]"
)


# ── parsing (see GOTCHAS) ───────────────────────────────────────────────────────────

_MD_PAIR_RE = re.compile(
    r'\*\*\s*(?:Fact|Claim):?\s*\*\*\s*"?(.+?)"?\s*\n+\s*\*\*\s*Label:?\s*\*\*\s*"?([A-Za-z_]+)"?',
    re.IGNORECASE)


def _parse_array(text: str) -> list:
    """Bracket-search-first JSON-array extraction; fence-segmentation as a fallback."""
    t = text.strip()
    s, e = t.find("["), t.rfind("]")
    if s != -1 and e != -1 and e > s:
        try:
            return json.loads(t[s:e + 1])
        except Exception:
            pass
    if "```" in t:
        seg = max(t.split("```"), key=len)
        if seg.lstrip().lower().startswith("json"):
            seg = seg.lstrip()[4:]
        s, e = seg.find("["), seg.rfind("]")
        if s != -1 and e != -1 and e > s:
            return json.loads(seg[s:e + 1])
    raise ValueError("no JSON array found in judge output")


def _parse_fact_label_markdown(text: str) -> list[dict]:
    return [{"fact": f.strip(), "label": l.strip().lower()}
            for f, l in _MD_PAIR_RE.findall(text)]


def _chat_with_retry(msgs: list[dict]) -> tuple[str, str]:
    """think -> no-think retry. Returns (raw_output, status). '' output -> ('','judge_error')."""
    try:
        out = f4._chat(msgs, f4.SPEC.think)
    except Exception as exc:
        logger.warning("chat failed: %s", exc)
        out = ""
    if out:
        return out, "ok"
    try:
        return f4._chat(msgs, False), "retried_nothink"
    except Exception as exc:
        logger.warning("no-think retry failed: %s", exc)
        return "", "judge_error"


# ── PRIOR FACTS (Pass 2b reference) ────────────────────────────────────────────────

def load_prior_facts(held: set[int]) -> dict[int, list[dict]]:
    """Per subject, the T1-T3 facts from every NON-holdout admission (b1 raw extractions,
    pre-consolidation). The holdout admission's own facts are EXCLUDED (else every BHC
    fact would trivially be 'in prior data'). Returns {subject_id: [fact_dict, ...]}."""
    out: dict[int, list[dict]] = {}
    for r in c1._read_jsonl(RETENTION_FACTS_JSONL):
        if int(r.get("hadm_id", -1)) in held:
            continue                                  # skip the held-out admission's facts
        pid = int(r["subject_id"])
        for f in r.get("facts", []) or []:
            if f.get("salience_tier") in COMPARABLE_TIERS and (f.get("text") or "").strip():
                out.setdefault(pid, []).append(f)
    return out


def _prior_facts_text(facts: list[dict]) -> str:
    """Dedup by lowercased text, render numbered p0,p1,... for alignment references."""
    seen, lines = set(), []
    for f in facts:
        t = (f.get("text") or "").strip()
        key = t.lower()
        if t and key not in seen:
            seen.add(key)
            lines.append(t)
    if not lines:
        return "(none)"
    return "\n".join(f"p{i}. {t}" for i, t in enumerate(lines))


def align_prior(prior_text: str, facts: list[dict]) -> tuple[dict[str, dict], str]:
    """Align residual (ledger-unresolved) facts against prior raw facts. Returns
    ({fact_text: {label,basis}}, status) with label in_prior|not_in_prior."""
    if not facts:
        return {}, "ok"
    summary = "\n".join(f"f{i}. {f['fact']}" for i, f in enumerate(facts))
    msgs = [{"role": "system", "content": _PRIOR_SYS},
            {"role": "user", "content": _PRIOR_TMPL.format(source=prior_text, summary=summary)}]
    out, status = _chat_with_retry(msgs)
    if not out:
        return {}, "judge_error"
    arr = None
    try:
        arr = _parse_array(out)
    except Exception:
        try:
            out2, _ = _chat_with_retry(
                msgs + [{"role": "user", "content": "Output ONLY the JSON array. No prose."}])
            arr = _parse_array(out2)
            status = "reparsed" if status == "ok" else status
        except Exception:
            return {}, "judge_error"
    if not isinstance(arr, list):
        return {}, "judge_error"
    by_text = {}
    for c in arr:
        if not isinstance(c, dict):
            continue
        lab = str(c.get("label", "")).strip().lower()
        by_text[str(c.get("fact", "")).strip()] = {
            "label": lab if lab in ("in_prior", "not_in_prior") else "in_prior",  # ambiguous -> conservative (counts as a real miss)
            "basis": str(c.get("basis", "")).strip()}
    return by_text, status


# ── PASS 1: extract + tier + origin ────────────────────────────────────────────────

def extract_tiered_facts(bhc_text: str) -> tuple[list[dict], str]:
    msgs = [{"role": "system", "content": _EXTRACT_SYS},
            {"role": "user", "content": _EXTRACT_TMPL.format(text=bhc_text)}]
    out, status = _chat_with_retry(msgs)
    if not out:
        return [], "judge_error"
    facts = None
    try:
        facts = _parse_array(out)
    except Exception:
        try:
            out2, _ = _chat_with_retry(
                msgs + [{"role": "user", "content": "Output ONLY the JSON array. No prose."}])
            facts = _parse_array(out2)
            status = "reparsed" if status == "ok" else status
        except Exception:
            return [], "judge_error"
    if not isinstance(facts, list):
        return [], "judge_error"
    clean = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        tier = str(f.get("salience_tier", "")).strip().upper()
        if tier not in ("T1", "T2", "T3", "T4", "T5"):
            tier = "T3"   # conservative: keep it in the comparable band rather than drop
        origin = str(f.get("origin", "")).strip().lower()
        if origin not in ("pre_existing", "arose_this_admission"):
            origin = "pre_existing"   # ambiguous -> conservative (stays in comparison)
        clean.append({
            "fact": str(f.get("fact", "")).strip(),
            "salience_tier": tier,
            "retention_class": str(f.get("retention_class", "")).strip(),
            "negative_kind": (f.get("negative_kind") if f.get("negative_kind") in
                               ("absent_or_negative", "resolved_relevant", "surveillance")
                               else None),
            "origin": origin,
        })
    return [f for f in clean if f["fact"]], status


def comparable(facts: list[dict]) -> list[dict]:
    """Keep T1-T3 AND pre_existing. Drop T4/T5 and arose_this_admission. Then DEDUP.

    Dedup by lowercased fact text (the same key _prior_lines() uses on the prior-fact side).
    Pass 1's compound split emits the BARE established condition as its own fact, so when a
    BHC names a condition twice - once bare in the problem list, once inside a compound like
    'pancytopenia and neutropenia on filgrastim' - the bare form is emitted twice. Without
    this the duplicate is judged twice and counted twice in BOTH the outcome bucket and the
    denominator, and compound-worded facts skew 'missing', so duplication weighted misses
    slightly. Measured on the 100-patient test cohort: 5 patients, 15 duplicate
    rows out of 673 extracted facts (one subject alone contributed 8, incl. 'Hep B cirrhosis' x3
    and 'HTN' x3). First occurrence wins, so tier/class/origin come from the first emission.
    """
    kept, seen = [], set()
    for f in facts:
        if f["salience_tier"] not in COMPARABLE_TIERS or f["origin"] != "pre_existing":
            continue
        key = (f.get("fact") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        kept.append(f)
    return kept


# ── PASS 2a: align vs ledger ───────────────────────────────────────────────────────

def compare_facts(ledger_text: str, facts: list[dict]) -> tuple[list[dict], str]:
    summary = "\n".join(f"f{i}. {f['fact']}" for i, f in enumerate(facts))
    msgs = [{"role": "system", "content": _CMP_SYS},
            {"role": "user", "content": _CMP_TMPL.format(source=ledger_text, summary=summary)}]
    out, status = _chat_with_retry(msgs)
    if not out:
        return [], "judge_error"
    claims = None
    try:
        claims = _parse_array(out)
    except Exception:
        md = _parse_fact_label_markdown(out)
        if md:
            claims = md
            status = "md_fallback" if status == "ok" else status
    if claims is None:
        try:
            out2, _ = _chat_with_retry(
                msgs + [{"role": "user", "content": "Output ONLY the JSON array. No prose."}])
            claims = _parse_array(out2)
            status = "reparsed" if status == "ok" else status
        except Exception:
            return [], "judge_error"
    if not isinstance(claims, list):
        return [], "judge_error"
    clean = []
    for c in claims:
        if not isinstance(c, dict):
            continue
        lab = str(c.get("label", "")).strip().lower()
        if lab not in ("retained", "missing", "contradicted"):
            lab = "missing"   # unparseable label -> conservative (counts against retention)
        clean.append({"fact": str(c.get("fact", "")).strip(), "label": lab,
                      "basis": str(c.get("basis", "")).strip()})
    return clean, status


# ── established record rendering (T1-T3 only; four-way negative split) ──────────────

def _ledger_text(led: dict) -> str:
    def _kind_line(n: dict):
        """Render by negative_kind. resolved_relevant AFFIRMS the event happened --
        the whole point of the fix: a resolved PE must NOT read as 'absent'."""
        nk = n.get("negative_kind")
        if nk == "surveillance":
            return f"- under surveillance for: {n.get('text', '')}"
        if nk == "absent_or_negative":
            return f"- documented absent/excluded: {n.get('text', '')}"
        if nk == "resolved_relevant":
            return f"- affirms (resolved, lifetime-relevant): {n.get('text', '')}"
        return None

    lines = []
    for n in led.get("nodes", []) or []:
        if n.get("salience_tier") not in COMPARABLE_TIERS:
            continue
        line = _kind_line(n)
        lines.append(line if line else f"- affirms: {n.get('text', '')}")
    for n in led.get("flagged", []) or []:
        if n.get("salience_tier") not in COMPARABLE_TIERS:
            continue
        line = _kind_line(n)
        if line:
            lines.append(line)
        else:
            cert = n.get("certainty", "suspected")
            lines.append(f"- affirms ({cert}): {n.get('text', '')}")
    for s in led.get("considered_excluded", []) or []:
        lines.append(f"- ruled out (worked up, excluded): {s}")
    if not lines:
        return "(none)"
    return "\n".join(f"l{i}. {ln[2:]}" for i, ln in enumerate(lines))


# ── main ─────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Current admission's human summary vs the established ledger "
                    "(salience-retention + coherence, one comparison).")
    ap.add_argument("--subject", type=int, default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--threshold-retention", type=float, default=0.8,
                    help="min salience_retention (PROVISIONAL; track only)")
    ap.add_argument("--threshold-coherence", type=float, default=0.95,
                    help="min coherence (PROVISIONAL; alarm metric, sits ~1.0 by design)")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()
    config.check_paths()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if not config.HOLDOUT_LAST_ADMISSION:
        raise SystemExit("HOLDOUT_LAST_ADMISSION is False -- no current admission to reference.")

    incoming = c1._read_jsonl(INCOMING_JSONL)
    if not incoming:
        raise SystemExit(f"{INCOMING_JSONL} not found/empty. Run e1_incoming_summary.py first.")
    ledgers = {int(l["subject_id"]): l for l in c1._read_jsonl(LEDGER_JSONL)}
    if not ledgers:
        raise SystemExit(f"{LEDGER_JSONL} not found/empty.")

    held = holdout.holdout_hadms()
    prior_by_pid = load_prior_facts(held)   # Pass 2b reference (prior raw facts, holdout-excluded)
    recs = [r for r in incoming if int(r["hadm_id"]) in held]
    if args.subject is not None:
        recs = [r for r in recs if int(r["subject_id"]) == args.subject]
    if args.limit:
        recs = recs[:args.limit]
    if not recs:
        print("No currently held-out admissions to reference.")
        return 0
    pid_set = {int(r["subject_id"]) for r in recs}

    existing = {} if args.fresh else {
        int(r["subject_id"]): r for r in c1._read_jsonl(OUT_JSONL)}
    done = {pid for pid, r in existing.items() if r.get("status") != "judge_error"}

    merged = dict(existing)            # incremental store; flushed after each patient

    def _flush() -> None:
        with OUT_JSONL.open("w", encoding="utf-8") as fh:
            for k in sorted(merged):
                fh.write(json.dumps(merged[k]) + "\n")

    n_judged = 0
    try:
        for r in recs:
            pid = int(r["subject_id"])
            if pid in done:
                continue
            led = ledgers.get(pid)
            if led is None:
                logger.warning("subject=%s no established ledger -- skipping", pid)
                continue
            bhc = (r.get("bhc_text") or "").strip()
            if not bhc:
                continue

            facts, ext_status = extract_tiered_facts(bhc)
            comp = comparable(facts)
            # Keep n_dropped meaning "filtered out by tier/origin" -- dedup removals are
            # reported separately so the two causes stay legible.
            n_dropped = sum(1 for f in facts
                            if f["salience_tier"] not in COMPARABLE_TIERS
                            or f["origin"] != "pre_existing")
            n_dupes = len(facts) - n_dropped - len(comp)
            established, truncated = f4._cap(_ledger_text(led))

            if comp:
                judged, cmp_status = compare_facts(established, comp)
            else:
                judged, cmp_status = [], "no_comparable_facts"

            # PASS 2b: facts the ledger lacked ('missing') -> check the patient's PRIOR raw
            # facts. In prior data => true retention gap (stays 'missing'); not in prior data
            # => new to this admission, ledger blameless => 'not_in_prior_data' (excluded).
            prior_status = "skipped"
            residual = [c for c in judged if c["label"] == "missing"]
            if residual:
                prior_text = _prior_facts_text(prior_by_pid.get(pid, []))
                if prior_text == "(none)":
                    # no prior facts at all (e.g. single prior admission already in ledger):
                    # cannot distinguish -> conservative, leave as 'missing'.
                    prior_status = "no_prior_facts"
                else:
                    prior_text, _ = f4._cap(prior_text)
                    prior_map, prior_status = align_prior(
                        prior_text, [{"fact": c["fact"]} for c in residual])
                    if prior_status != "judge_error":
                        for c in judged:
                            if c["label"] == "missing":
                                pm = prior_map.get(c["fact"])
                                if pm and pm["label"] == "not_in_prior":
                                    c["label"] = "not_in_prior_data"
                                    if pm.get("basis"):
                                        c["basis"] = pm["basis"]

            counts = {o: sum(1 for c in judged if c["label"] == o) for o in OUTCOMES}
            rec_status = ("judge_error" if ext_status == "judge_error"
                          or cmp_status == "judge_error" or prior_status == "judge_error"
                          else "ok")

            merged[pid] = {
                "subject_id": pid, "hadm_id": int(r["hadm_id"]), "admit_seq": r.get("admit_seq"),
                "status": rec_status, "established_truncated": truncated,
                "ext_status": ext_status, "cmp_status": cmp_status, "prior_status": prior_status,
                "n_facts_total": len(facts), "n_comparable": len(comp), "n_dropped": n_dropped,
                "n_duplicate_facts": n_dupes,
                "comparable_facts": comp, "judged": judged, **counts,
                "model": f4.SPEC.model_id, "prompt_version": f4.PROMPT_VERSION,
                "taxonomy_version": tax.TAXONOMY_VERSION,
            }
            _flush()                  # persist after each patient (resumable on interrupt)
            n_judged += 1
            logger.info("subj=%s facts=%d comparable=%d (dropped %d) -> retained=%d "
                       "missing=%d contradicted=%d not_in_prior=%d  [ext=%s cmp=%s prior=%s]",
                       pid, len(facts), len(comp), n_dropped, counts["retained"],
                       counts["missing"], counts["contradicted"], counts["not_in_prior_data"],
                       ext_status, cmp_status, prior_status)
    finally:
        f4._ollama_unload()

    _flush()                          # final flush (also covers the all-skipped case)

    rows = [r for pid, r in merged.items() if pid in pid_set]
    tot = {o: 0 for o in OUTCOMES}
    n_err = sum(1 for r in rows if r["status"] == "judge_error")
    n_trunc = sum(1 for r in rows if r.get("established_truncated"))
    n_no_comp = sum(1 for r in rows if r.get("cmp_status") == "no_comparable_facts")
    n_recovered = sum(1 for r in rows
                      if r.get("ext_status") in _RECOVERED_STATUSES
                      or r.get("cmp_status") in _RECOVERED_STATUSES
                      or r.get("prior_status") in _RECOVERED_STATUSES)
    contradictions, misses, new_facts = [], [], []
    for r in rows:
        for o in OUTCOMES:
            tot[o] += r.get(o, 0)
        for c in r.get("judged", []):
            if c["label"] == "contradicted":
                contradictions.append({"subject_id": r["subject_id"], "hadm_id": r["hadm_id"],
                                       "admit_seq": r.get("admit_seq"), "fact": c["fact"],
                                       "basis": c.get("basis", "")})
            elif c["label"] == "missing":
                misses.append({"subject_id": r["subject_id"], "hadm_id": r["hadm_id"],
                               "admit_seq": r.get("admit_seq"), "fact": c["fact"],
                               "basis": c.get("basis", "")})
            elif c["label"] == "not_in_prior_data":
                new_facts.append({"subject_id": r["subject_id"], "hadm_id": r["hadm_id"],
                                  "admit_seq": r.get("admit_seq"), "fact": c["fact"]})

    comparable_total = sum(tot[o] for o in DENOM_OUTCOMES)   # excludes not_in_prior_data

    def _frac(num: int, denom: int) -> float:
        return round(num / denom, 4) if denom else 1.0

    salience_retention = _frac(tot["retained"], comparable_total)
    coherence = round(1.0 - (tot["contradicted"] / comparable_total), 4) if comparable_total else 1.0

    gate_retention = salience_retention >= args.threshold_retention
    gate_coherence = coherence >= args.threshold_coherence
    all_pass = gate_retention and gate_coherence

    report = {
        "metric": "incoming_admission_reference",
        "model": f4.SPEC.model_id, "prompt_version": f4.PROMPT_VERSION,
        "taxonomy_version": tax.TAXONOMY_VERSION,
        "patients": len(pid_set), "patients_scored": len(rows),
        "n_judge_errors": n_err, "n_recovered": n_recovered,
        "n_established_truncated": n_trunc, "n_no_comparable_facts": n_no_comp,
        "comparable_total": comparable_total, "counts": tot,
        "n_not_in_prior_data": tot["not_in_prior_data"],
        "new_to_current_admission": new_facts,
        "salience_retention": {
            "value": salience_retention, "threshold": args.threshold_retention,
            "gate": gate_retention, "n_missing": len(misses), "missing": misses,
        },
        "coherence": {
            "value": coherence, "threshold": args.threshold_coherence,
            "gate": gate_coherence, "n_contradictions": len(contradictions),
            "contradictions": contradictions,
        },
        "gates": {"gate_salience_retention": gate_retention, "gate_coherence": gate_coherence},
        "all_gates_pass": all_pass,
    }
    with OUT_JSON.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    if not args.json_only:
        print("\n" + "=" * 60)
        print("F6 CURRENT-ADMISSION REFERENCE (salience-retention + coherence, two-pass)")
        print(f"  model / taxonomy             : {f4.SPEC.model_id} / {tax.TAXONOMY_VERSION}")
        print(f"  patients / scored            : {len(pid_set)} / {len(rows)}  (newly judged {n_judged})")
        print(f"  comparable facts (T1-T3, pre-existing): {comparable_total}  "
              f"(retained={tot['retained']} missing={tot['missing']} "
              f"contradicted={tot['contradicted']})")
        print(f"  excluded (new to current admission, not in prior data): "
              f"{tot['not_in_prior_data']}")
        print(f"  salience_retention           : {salience_retention:.4f}")
        print(f"  coherence (alarm; ~1.0 by design): {coherence:.4f}")
        print(f"  patients w/ no comparable facts: {n_no_comp}")
        print(f"  judge errors: {n_err}   recovered: {n_recovered}   ledger truncated: {n_trunc}")
        if contradictions:
            print(f"\n  {len(contradictions)} contradiction(s) -- stale/wrong ledger candidates, "
                 f"NOT auto-failed:")
            for c in contradictions[:15]:
                basis = f"  [basis: {c['basis'][:55]}]" if c.get("basis") else ""
                print(f"    subj {c['subject_id']} hadm {c['hadm_id']} (seq {c['admit_seq']}) "
                     f":: {c['fact'][:65]}{basis}")
            if len(contradictions) > 15:
                print(f"    ... +{len(contradictions) - 15} more (see {OUT_JSON.name})")
        if misses:
            print(f"\n  {len(misses)} comparable fact(s) MISSING from the ledger "
                 f"(in prior data -- true retention gaps):")
            for m in misses[:15]:
                print(f"    subj {m['subject_id']} hadm {m['hadm_id']} (seq {m['admit_seq']}) "
                     f":: {m['fact'][:70]}")
            if len(misses) > 15:
                print(f"    ... +{len(misses) - 15} more (see {OUT_JSON.name})")
        if new_facts:
            print(f"\n  {len(new_facts)} fact(s) NEW to the current admission "
                 f"(not in prior data -- excluded from rates; candidate new conditions):")
            for nf in new_facts[:10]:
                print(f"    subj {nf['subject_id']} hadm {nf['hadm_id']} (seq {nf['admit_seq']}) "
                     f":: {nf['fact'][:70]}")
            if len(new_facts) > 10:
                print(f"    ... +{len(new_facts) - 10} more (see {OUT_JSON.name})")
        print(f"\n  gate_salience_retention (>= {args.threshold_retention})  : "
              f"{'PASS' if gate_retention else 'FAIL'}  (track only)")
        print(f"  gate_coherence (>= {args.threshold_coherence})          : "
              f"{'PASS' if gate_coherence else 'FAIL'}  (track only)")
        print(f"\n  jsonl -> {OUT_JSONL}")
        print(f"  json  -> {OUT_JSON}")

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
