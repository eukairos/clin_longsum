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
retention_taxonomy.py
=====================
Three-axis clinical salience taxonomy for triaging facts into the must-retain
ledger and for driving compaction behaviour.

Axes (per fact):
  AXIS 1 salience_tier   T1 alert / T2 baseline / T3 chronic / T4 episode / T5 transient
  AXIS 2 retention_class 12 classes, each with a default tier; ledger groups by this
  AXIS 3 status modifiers certainty / lifecycle / occurrence

Ledger membership is DERIVED (is_ledger_member), not stored. The certainty gate
is class- and lifecycle-aware rather than confirmed-only:
  - ruled_out                     -> never pinned (kept as a negative)
  - superseded                    -> never pinned EXCEPT for the safety classes 
  - severe_adr_allergy / code_status_directive -> pinned unless ruled_out 
    (safety classes are robust to tier/certainty/lifecycle mis-tagging -
    a resolved or suspected severe drug reaction is still a lifetime
    contraindication; a directive is binding however it was tagged)
  - negative_kind         -> see Axis 3 comments below
  - confirmed, tier T1-T3         -> pinned (current state)
  - suspected + active, tier T1-T3 -> pinned as PROVISIONAL (open problem / under workup)
  - patient_reported, for allergy / code-status / infection-control -> pinned
    (a patient-reported transfusion refusal or drug allergy must be honoured)
This captures the open-problem list and patient-asserted directives without
claiming them as confirmed - the served ledger separates confirmed nodes from a
provisional / patient-reported "flagged" section (B2).

A deterministic surveillance-finding detector (is_surveillance_finding) promotes
incidental nodules / masses / lesions / rising tumour markers to
negative_kind so a "?malignancy under surveillance" can never silently
drop on a prompt miss. Keyword-based by design - a short, high-precision safety
net, not an open-ended synonym map.

Mortality is NOT a class.

GOVERNANCE OBJECT: bump TAXONOMY_VERSION and append to CHANGELOG on any edit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── VERSION + CHANGELOG ──────────────────────────────────────────────────────────

TAXONOMY_VERSION = "retention_taxonomy.v2.12"

CHANGELOG = [
    ("retention_taxonomy.v1", "2026-06-02", "Flat retain/compressible."),
    ("retention_taxonomy.v2", "2026-06-03", "Three-axis model; derived ledger membership."),
    ("retention_taxonomy.v2.1", "2026-06-04", "significant_negative pins even when suspected."),
    ("retention_taxonomy.v2.2", "2026-06-04",
     "Re-added sentinel_event (T2); EXTRACTION_RULES (surface substrate, "
     "acute-on-chronic two facts, incidental->suspected+signeg)."),
    ("retention_taxonomy.v2.3", "2026-06-04",
     "Certainty gate made class/lifecycle-aware: suspected+active T1-T3 pins as "
     "PROVISIONAL (open-problem list); patient_reported pins for allergy/"
     "code-status/infection-control (e.g. Jehovah's Witness transfusion refusal). "
     "Added deterministic is_surveillance_finding -> promotes incidentalomas to "
     "significant_negative. Added certainty-discipline extraction rule. "
     "Widened surveillance net (incidental lesion/cyst window; PSA/marker trend)."),
    ("retention_taxonomy.v2.4", "2026-06-06",
     "Widened is_surveillance_finding recall (full-run 100-case loss audit): "
     "possible-malignancy phrasing; GI/GU wall thickening; sized nodule/mass/lesion "
     "(mm/cm); hypoechoic/hypodense/enhancing lesion; nodular opacity; malignancy "
     "progression/recurrence/relapse; 'increase in' lesion. Trades some precision "
     "for recall on asymmetric-cost findings -- review the b3 'promoted' count."),
    ("retention_taxonomy.v2.5", "2026-06-06",
     "Safety classes robust to mis-tagging: severe_adr_allergy and "
     "code_status_directive pin unless ruled_out/superseded, independent of "
     "tier/certainty/lifecycle. Full-run audit dropped resolved/suspected DRESS, "
     "warfarin skin necrosis, vancomycin/infliximab reactions, statin intolerance, "
     "and mis-tiered drug rashes -- a resolved or suspected severe reaction is "
     "still a lifetime contraindication. Finding 1 (malignancy recurrence / graft "
     "rejection mis-cast as recurrent_episode) deferred to Phase C."),
    ("retention_taxonomy.v2.6", "2026-06-06",
     "Finding 1 resolved via deterministic net: is_disease_course_event() detects a "
     "recurrent_episode that is really malignancy recurrence/relapse/progression or "
     "graft/organ rejection; B2/B3 reclassify it to sentinel_event (T2) so it pins as "
     "its own node instead of collapsing into a frequency rollup. High precision "
     "(event term AND malignancy/graft context); benign recurrent episodes untouched."),
    ("retention_taxonomy.v2.7", "2026-06-06",
     "At-source prevention for Finding 1: EXTRACTION_RULES + a recurrent_episode "
     "counter-example now instruct the extractor to tag malignancy recurrence/relapse/"
     "progression and graft/organ rejection as sentinel_event, not recurrent_episode. "
     "Complements the v2.6 B2/B3 deterministic net (kept as backstop and for already-"
     "extracted data). render_for_prompt change -> affects future B1 runs only."),
    ("retention_taxonomy.v2.8", "2026-06-06",
     "Acute sentinel-episode net (C2 surfaced pinned recurrent_episodes whose text is lost "
     "when a parent_condition collapses them into a rollup): is_sentinel_episode() promotes "
     "status epilepticus, gangrene, and graft/bypass/stent occlusion or thrombosis from "
     "recurrent_episode to sentinel_event (T2) at B2/B3/C1 load, plus an at-source extraction "
     "rule. High precision (specific terms); benign recurrences untouched."),
    ("retention_taxonomy.v2.10", "2026-06-26",
     "Two scoped pinning fixes from the b3 loss-audit review (subjects 90000038 / "
     "90000033 / 90000087 et al). (A) TIER FLOOR: the per-class default_tier is a FLOOR "
     "to escalate from, not a value the extractor may de-escalate below -- _normalise_fact "
     "now floors salience_tier to default_tier_for_class (new floor_tier helper). A "
     "chronic_disease_state mis-tagged T4/T5 (e.g. 'Long-standing leukopenia', confirmed/"
     "active but dropped) is restored to >= T3 and pins; also corrects downstream tier-"
     "driven compaction. No over-pinning: a recurrent_episode floored to T4 / transient to "
     "T5 still does not pin. Corrects a stored value -> needs the b1 re-extraction. (B) "
     "SUSPECTED SENTINEL: a suspected sentinel_event now pins as PROVISIONAL regardless of "
     "lifecycle (was active-only), unless ruled_out/superseded -- a presumed major event "
     "that resolved (type II NSTEMI / troponin leak, UGI bleed, delirium) still has lasting "
     "prognostic weight and must reach physician review. Routes to B2's flagged bucket via "
     "certainty=suspected. Re-derives on existing facts at load. SCOPE (deliberately narrow): "
     "does NOT touch suspected+resolved CHRONIC classes (mis-classified acute episodes -- fix "
     "by reclassification upstream) nor patient_reported sentinel_event (suicide ideation -- "
     "carried by the chronic depression node)."),
    ("retention_taxonomy.v2.9", "2026-06-24",
     "BREAKING: significant_negative (bool) -> negative_kind (enum). The single boolean "
     "overloaded FOUR incompatible meanings -- documented absence, screening/workup "
     "negative, a RESOLVED-but-lifetime-relevant POSITIVE event (provoked VTE/PE, "
     "fracture), and an incidentaloma under surveillance -- so a real historical PE "
     "rendered as 'documented absent' (f6/f4 false contradiction; a clinician-facing "
     "display-safety risk). negative_kind in {None, 'absent_or_negative', "
     "'resolved_relevant', 'surveillance'} disambiguates at the SOURCE (B1 extraction), "
     "because only the extractor can tell 'had it, resolved' from 'ruled out' from "
     "'absent' from the text. is_ledger_member pins iff negative_kind is not None (== the "
     "old bool's pin behaviour). is_surveillance_finding promotion sets 'surveillance'. "
     "Requires full re-extraction (B1) + cascade (B2/B4/C1/D2/coordinator/D3). No "
     "backward compat (dev, no live consumers)."),
    ("retention_taxonomy.v2.11", "2026-07-29",
     "AUDIT THE PRUNE (forensic register FM-5). A fact that was neither pinned, "
     "ruled_out, superseded nor a recurrent_episode matched no branch in B2's "
     "_project_patient and landed in NO ledger array -- no counter, no log line. "
     "Absence from all five arrays was the only signature, indistinguishable from a "
     "fact that was never extracted. Adds prune_reason(), which names WHICH branch of "
     "is_ledger_member declined the fact, so B2 can record it in the new "
     "RetentionLedger.dropped[] (persisted to the retention_dropped table by D3). "
     "is_ledger_member is UNTOUCHED: nothing changes about what is pinned or served, "
     "so no re-extraction is required -- b2 onward re-derives. Purely additive "
     "inspectability. NOTE this does not address the separate B4 T5 recency prune, "
     "whose casualties remain recoverable from retention_ledger.raw.jsonl."),
    ("retention_taxonomy.v2.12", "2026-08-19",
     "SAFETY CLASSES ROBUST TO lifecycle=superseded (forensic register FM-8). The gate "
     "tested lifecycle=superseded BEFORE _SAFETY_ROBUST_CLASSES, so a T1 contraindication "
     "tagged superseded un-pinned silently -- B2's _is_candidate re-admitted it, "
     "_build_node routed it to `amended`, which D3 does not load and C2 counts as "
     "REPRESENTED. No dropped[] entry, no considered_excluded entry, no C2 flag: sole "
     "signature was membership in `amended`. Cohort audit 2026-08-19 (110 pts / 11,930 "
     "facts): 6 amended nodes, ALL n_mentions==1 (the de-label cancel path has never "
     "fired); 4 were T1 severe_adr_allergy absent from the served ledger; data_gen 3/14 "
     "(21%) of severe_adr_allergy affected. B1 was assigning superseded on drug "
     "SUBSTITUTION rather than record RETRACTION -- 4/4 false positives. Genuine "
     "de-labels were instead expressed as certainty=ruled_out and routed correctly. "
     "FIX: safety-class branch moved ABOVE the superseded guard (ruled_out is now the "
     "SOLE exit from a safety pin); adds is_safety_robust() so B2 can exempt safety "
     "classes from the `amended` route and SERVE a de-labelled allergy as an explicit "
     "negative; render_for_prompt() now distinguishes retraction from substitution. "
     "Re-derives on existing facts at load -- b2 onward, NO re-extraction required. The "
     "prompt tightening is STAGED and unexercised on the current cohort; the gate "
     "reorder is what protects the existing data."),
    # NOT a version bump. No axis, gate, net or prompt changed -- render_for_prompt() is
    # byte-identical before and after (sha256 verified 2026-09-01). Recorded here because a
    # reader comparing the taxonomy against the running pipeline would otherwise mispredict
    # b4's behaviour, which is the drift the CHANGELOG exists to prevent.
    ("retention_taxonomy.v2.12", "2026-09-01",
     "DOC ONLY -- T5 serving-prune exemption made explicit. The T5 tier and the "
     "transient_event class both read 'prunable past the window UNLESS carrying a "
     "negative_kind', unqualified. That clause was implemented at the PIN decision only: "
     "is_ledger_member pins any T5 fact carrying a negative_kind, and B2 applies no recency "
     "window at all. The window lives solely at b4 (ledger_consolidate._prune_t5, "
     "config.CONSOLIDATE_T5_PRUNE_DAYS=90), which read no negative_kind and aged out every "
     "T5 node on date alone - 364/474 nodes in the 100-patient test cohort, 100% of which "
     "carried a negative_kind, since at T5 that branch is the only route through the gate. "
     "`surveillance` ONLY is exempted: an unconfirmed finding "
     "still being watched must not age out; a dated screening negative goes stale, and a "
     "resolved event with genuinely lifetime weight should be carrying a T2 class rather "
     "than sitting at T5. Implemented as ledger_consolidate.T5_PRUNE_EXEMPT_KINDS. This "
     "entry updates the T5 SalienceTier.representation string (documentation only -- no "
     "code reads it) and adds a comment on transient_event. transient_event.decision_rule "
     "is UNCHANGED: it is rendered into the b1 prompt and correctly describes the pin "
     "decision, the only prune b1 can influence. NO re-extraction, NO prompt version bump."),
]


# ── AXIS 1: SALIENCE TIERS ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class SalienceTier:
    id: str
    label: str
    selecting_property: str
    action: str
    representation: str
    recency_window_days: int | None


SALIENCE_TIERS: tuple[SalienceTier, ...] = (
    SalienceTier("T1", "Operational alert",
                 "A clinician must see this BEFORE any clinical action.",
                 "pin", "verbatim fact, always surfaced", None),
    SalienceTier("T2", "Permanent baseline",
                 "Irreversible anatomy/genetics/device, or a past event with lasting prognostic weight.",
                 "carry_forward", "structured state metadata", None),
    SalienceTier("T3", "Chronic axis",
                 "Incurable systemic disease that fluctuates between stability and exacerbation.",
                 "state_track", "current stage/severity + trend", None),
    SalienceTier("T4", "Recurrent episode",
                 "A discrete exacerbation of an existing chronic (T3) parent state.",
                 "graph_collapse", "count / frequency / date range under the parent", None),
    # `representation` is DOCUMENTATION ONLY - no code reads SalienceTier.representation, and
    # render_for_prompt emits selecting_property / action / recency_window_days, not this field.
    # The window is implemented ONLY at b4 (ledger_consolidate._prune_t5, driven by
    # config.CONSOLIDATE_T5_PRUNE_DAYS). b2 applies no window at all - the safer split, since a
    # T5 fact pinned by negative_kind enters the ledger regardless of age and is aged out later,
    # at the serving layer, where the raw ledger still holds it.
    SalienceTier("T5", "Transient",
                 "Self-limiting event that resolves without altering the long-term trajectory.",
                 "prune",
                 "dropped past the window. Of the negative kinds, only `surveillance` is exempt "
                 "from the b4 serving prune (ledger_consolidate.T5_PRUNE_EXEMPT_KINDS): an "
                 "unconfirmed finding still being watched must not age out of the served ledger. "
                 "absent_or_negative and resolved_relevant DO age out - a dated screening "
                 "negative goes stale, and a resolved event with genuinely lifetime weight should "
                 "be carrying a T2 class rather than sitting at T5. Pruned nodes remain in "
                 "retention_ledger.raw.jsonl and are unaffected in C2 recall.",
                 90),
)

_TIER_IDS = frozenset(t.id for t in SALIENCE_TIERS)


# ── AXIS 3: STATUS MODIFIERS ─────────────────────────────────────────────────────

CERTAINTY = ("confirmed", "suspected", "ruled_out", "patient_reported")
LIFECYCLE = ("active", "resolved", "in_remission", "superseded")
OCCURRENCE = ("index", "recurrence")

# negative_kind. None means the fact is a positive present condition (not a significant negative). 
# The three non-None kinds are:
#   "absent_or_negative"  documented absence OR screening/workup negative - the patient
#                         does NOT (currently) have X. e.g. 'no known drug allergies',
#                         'HIV negative', 'colonoscopy normal 2024', 'EGD ruled out
#                         varices'. Keep the verbatim text so the exclusion reads true.
#   "resolved_relevant"   a POSITIVE event the patient DID have, now resolved, but with
#                         lasting prognostic weight: provoked VTE/PE, first unprovoked
#                         seizure, significant fracture. 
#   "surveillance"        an unconfirmed finding being watched: incidental nodule/mass/
#                         indeterminate cyst, lesion 'concerning for' malignancy, rising
#                         tumour marker. Set deterministically by is_surveillance_finding
#                         (and certainty=suspected).
NEGATIVE_KIND = ("absent_or_negative", "resolved_relevant", "surveillance")

# Merge priority when a b2/b4 cluster of same-condition facts carries DIFFERENT
# negative_kind values. Higher wins. None (a present/active positive) dominates every
# negative - if any sibling says the condition is currently present, the node is present,
# never masked by an 'absent' sibling. Among negatives, a real resolved event outranks a
# surveillance finding, which outranks a plain absence/screening-negative. 
_NEGATIVE_KIND_RANK = {None: 4, "resolved_relevant": 3, "surveillance": 2, "absent_or_negative": 1}


def merge_negative_kind(kinds) -> str | None:
    """Resolve the negative_kind of a merged cluster from its members' kinds, by the
    priority None > resolved_relevant > surveillance > absent_or_negative."""
    best, best_rank = None, -1
    saw_any = False
    for k in kinds:
        saw_any = True
        k = k if k in NEGATIVE_KIND else None
        r = _NEGATIVE_KIND_RANK.get(k, 4)
        if r > best_rank:
            best, best_rank = k, r
    return best if saw_any else None

# Classes for which a patient-reported assertion is itself actionable and must be
# retained (the patient is often the only source).
_PATIENT_REPORTED_RETAIN = ("severe_adr_allergy", "code_status_directive", "infection_control_status")

# Safety classes that pin unless ruled_out, regardless of tier / certainty /
# lifecycle. A resolved or suspected severe drug reaction is still a lifetime
# contraindication; a code-status directive is binding however tagged.
# certainty=ruled_out is the SOLE exit - the safety branch is tested
# ABOVE the superseded guard, so a de-labelled allergy stays pinned and is served as
# an explicit negative (b2 exempts it from the `amended` route via is_safety_robust).
_SAFETY_ROBUST_CLASSES = ("severe_adr_allergy", "code_status_directive")


def is_safety_robust(retention_class: str | None) -> bool:
    """Class whose pin survives tier / certainty / lifecycle mis-tagging. Only certainty=ruled_out un-pins these. 
    Public accessor so b2 can exempt safety classes from the `amended` route without reaching for the private tuple."""
    return retention_class in _SAFETY_ROBUST_CLASSES


# ── AXIS 2: RETENTION CLASSES ────────────────────────────────────────────────────

@dataclass(frozen=True)
class RetentionClass:
    id: str
    label: str
    default_tier: str
    description: str
    decision_rule: str
    examples: tuple = field(default_factory=tuple)
    counter_examples: tuple = field(default_factory=tuple)


RETENTION_CLASSES: tuple[RetentionClass, ...] = (
    RetentionClass(
        id="severe_adr_allergy", label="Severe ADR / true allergy", default_tier="T1",
        description="Life- or organ-threatening drug/substance reactions; IgE anaphylaxis; malignant hyperthermia.",
        decision_rule="Repeating the exposure could be fatal.",
        examples=("Stevens-Johnson syndrome from allopurinol", "Anaphylaxis to penicillin", "Malignant hyperthermia",
                  "Penicillin allergy DE-LABELLED on challenge - STILL this class, with "
                  "lifecycle=superseded + negative_kind=absent_or_negative (served as an "
                  "explicit negative, so the reader sees the de-labelling rather than nothing)"),
        counter_examples=("Transient nausea on metronidazole",
                          "A drug stopped or switched BECAUSE of a reaction - that is this class "
                          "with lifecycle=ACTIVE, never superseded"),
    ),
    RetentionClass(
        id="infection_control_status", label="Infection-control status", default_tier="T1",
        description="Multi-drug-resistant organisms and notifiable infections with isolation / antibiotic implications.",
        decision_rule="Changes future antibiotic choice or infection-control precautions.",
        examples=("MRSA colonisation", "Carbapenem-resistant Enterobacteriaceae", "Active tuberculosis", "VRE"),
        counter_examples=("Uncomplicated E. coli UTI, sensitive, treated -> transient_event",),
    ),
    RetentionClass(
        id="code_status_directive", label="Code status / goals of care", default_tier="T1",
        description="Resuscitation status, ceiling of care, advance directives, transfusion/treatment refusals.",
        decision_rule="Must be visible before acting. LATEST-WINS. Often patient-reported and still binding.",
        examples=("DNR / DNI", "Ceiling of care: ward-level", "Jehovah's Witness transfusion refusal", "Advance directive"),
        counter_examples=("Full code with no special directive",),
    ),
    RetentionClass(
        id="sentinel_event", label="Sentinel critical event", default_tier="T2",
        description="A major acute event with lasting prognostic weight: permanently changes how future care/risk are assessed.",
        decision_rule="Retain if a future clinician must know it happened. VTE/PE also negative_kind=resolved_relevant.",
        examples=("Ischaemic/haemorrhagic stroke", "Intracranial haemorrhage", "Cardiac arrest",
                  "ICU admission requiring mechanical ventilation", "Major haemorrhage", "VTE / PE"),
        counter_examples=("Single uncomplicated ward admission", "Overnight observation"),
    ),
    RetentionClass(
        id="implanted_device", label="Implanted device / prosthesis", default_tier="T2",
        description="Permanent implants with lifetime management implications. Escalate to T1 if safety-critical.",
        decision_rule="Device persists and constrains future care.",
        examples=("Pacemaker / ICD (T1: MRI)", "Prosthetic heart valve", "Coronary stents", "AV fistula / dialysis access"),
        counter_examples=("Temporary urinary catheter", "PICC line"),
    ),
    RetentionClass(
        id="anatomy_altering_surgery", label="Anatomy-altering surgery", default_tier="T2",
        description="Procedures that permanently change anatomy or remove an organ.",
        decision_rule="A future clinician must know the anatomy is changed.",
        examples=("Splenectomy", "Bowel resection / colostomy", "Organ transplant", "Amputation"),
        counter_examples=("Diagnostic endoscopy with no resection", "Incision and drainage"),
    ),
    RetentionClass(
        id="permanent_condition", label="Permanent / irreversible condition", default_tier="T2",
        description="Genetic, structural, or acquired irreversible states that reset the baseline.",
        decision_rule="Permanently modifies how future symptoms are interpreted.",
        examples=("Cystic fibrosis", "Sickle cell disease", "Type 1 diabetes", "Paraplegia", "Post-stroke deficit", "Pancreas divisum"),
        counter_examples=("Reversible AKI that resolved -> transient_event",),
    ),
    RetentionClass(
        id="altered_immune_status", label="Altered immune status", default_tier="T2",
        description="Persistent immunocompromise or asplenia altering infection risk and prophylaxis.",
        decision_rule="Permanently raises infection risk or changes prophylaxis/vaccination.",
        examples=("HIV infection", "Asplenia", "Transplant immunosuppression (tacrolimus/ciclosporin/sirolimus)", "Active chemotherapy"),
        counter_examples=("Short completed prednisolone course",),
    ),
    RetentionClass(
        id="transfusion_obstetric_history", label="Transfusion / lifetime obstetric history", default_tier="T2",
        description="Permanent relevance to cross-matching, alloimmunisation, or future pregnancy.",
        decision_rule="Constrains future transfusion or obstetric management.",
        examples=("Red-cell alloantibodies", "Prior transfusion reaction", "Previous caesarean", "Postpartum haemorrhage"),
        counter_examples=("Uncomplicated vaginal delivery with no sequelae",),
    ),
    RetentionClass(
        id="chronic_disease_state", label="Chronic disease state", default_tier="T3",
        description="Long-term incurable systemic disease tracked as a continuous state.",
        decision_rule="The INDEX diagnosis establishes the state node; later flares are recurrent_episode (T4).",
        examples=("COPD", "CHF", "CKD / ESRD on dialysis", "Metastatic malignancy", "IBD", "Epilepsy",
                  "Atrial fibrillation", "Cirrhosis", "Bipolar disorder"),
        counter_examples=("The 7th COPD exacerbation -> recurrent_episode",),
    ),
    RetentionClass(
        id="recurrent_episode", label="Recurrent acute episode", default_tier="T4",
        description="Discrete, time-bound exacerbation of a chronic (T3) parent. Low individual, high aggregate salience.",
        decision_rule="Set parent_condition to the T3 state. Collapses to a frequency metric.",
        examples=("7th COPD exacerbation", "Acute-on-chronic heart failure", "DKA admission"),
        counter_examples=("FIRST presentation of a chronic disease -> chronic_disease_state (T3), occurrence=index",
                          "Malignancy recurrence/relapse/progression or graft/organ rejection -> sentinel_event (T2), NOT recurrent_episode"),
    ),
    # decision_rule below is RENDERED INTO THE b1 PROMPT (render_for_prompt, the "rule:" line),
    # so it deliberately describes the PIN decision only - the one thing the extractor controls.
    # As written it is correct: a negative_kind does exempt the fact from b2's tier prune. It does
    # NOT exempt it from b4's serving-layer recency prune, where only `surveillance` survives
    # (ledger_consolidate.T5_PRUNE_EXEMPT_KINDS). Qualifying the rule text here would alter the
    # extraction prompt - prompt_version bump plus re-extraction - for a distinction b1 cannot
    # act on. Keep the b4 nuance in this comment and in the T5 tier `representation` above.
    RetentionClass(
        id="transient_event", label="Transient self-limiting event", default_tier="T5",
        description="Minor acute illness, symptom, lab abnormality, or administrative event that resolves.",
        decision_rule="Prunable past the recency window UNLESS flagged with a negative_kind.",
        examples=("Acute viral rhinopharyngitis", "Resolved AKI", "Uncomplicated gastroenteritis"),
        counter_examples=("Normal screening colonoscopy with date -> negative_kind=absent_or_negative",
                          "Incidental lung nodule -> negative_kind=surveillance + certainty=suspected"),
    ),
)

_CLASS_BY_ID = {c.id: c for c in RETENTION_CLASSES}
_CLASS_IDS = frozenset(_CLASS_BY_ID)

NEGATIVE_KIND_GUIDANCE = (
    "absent_or_negative - documented ABSENCE with care implications: 'no known drug "
    "allergies', 'HIV negative'.",
    "absent_or_negative - screening/workup result, with date where given: 'colonoscopy "
    "normal 2024', 'biopsies negative for malignancy', 'EGD ruled out varices'. Keep the "
    "verbatim finding text.",
    "resolved_relevant - a POSITIVE event the patient DID have, now resolved, but with "
    "lasting prognostic weight: provoked VTE/PE, first unprovoked seizure, significant "
    "fracture. (This is NOT an absence - the event happened.)",
    "surveillance - incidental nodule / mass / indeterminate cyst, a lesion 'concerning "
    "for' malignancy, or a rising tumour marker - also set certainty=suspected.",
    "de-labelled prior label: 'penicillin allergy disproven on challenge' is "
    "absent_or_negative AND lifecycle=superseded.",
    "Otherwise leave negative_kind null (a positive, currently-present condition is NOT a "
    "significant negative).",
)

EXTRACTION_RULES = (
    "SURFACE THE SUBSTRATE. Whenever a chronic disease, transplant, dialysis-dependence, or "
    "permanent condition is present - even ONLY as background to an acute event ('AKI on CKD III') "
    "or ONLY as a medication indication ('mesalamine for UC'; tacrolimus/ciclosporin/sirolimus -> "
    "transplant + immunosuppression; anti-epileptics -> epilepsy; chemotherapy -> malignancy; insulin "
    "/ HbA1c -> diabetes; chronic dialysis -> ESRD) - emit it as its OWN chronic_disease_state / "
    "permanent_condition / altered_immune_status / sentinel_event fact.",
    "ACUTE-ON-CHRONIC: emit TWO facts - the chronic state and the acute episode (recurrent_episode, parent_condition set).",
    "DISEASE-COURSE EVENTS ARE NOT RECURRENT EPISODES: malignancy recurrence / relapse / "
    "progression / transformation, and graft or organ rejection, are sentinel_event (T2), NOT "
    "recurrent_episode - they permanently change prognosis and must pin as their own node. "
    "recurrent_episode is only for benign exacerbations of a stable chronic state (COPD "
    "exacerbation, DKA, acute-on-chronic heart failure).",
    "ACUTE SENTINEL EVENTS ARE NOT RECURRENT EPISODES: status epilepticus, gangrene, and "
    "graft/bypass/stent occlusion or thrombosis are sentinel_event (T2), NOT recurrent_episode "
    "- they are limb- or life-threatening events that must pin as their own node.",
    "INCIDENTAL / SUSPICIOUS FINDINGS: an incidental nodule, mass, indeterminate cyst, lesion 'concerning "
    "for' malignancy, or rising tumour marker is certainty=suspected AND negative_kind=surveillance.",
    "CERTAINTY DISCIPLINE: a condition being actively treated or stated as a diagnosis is "
    "certainty=confirmed. Reserve suspected for genuine diagnostic uncertainty ('concern for', a "
    "differential, 'cannot exclude'). Do not down-rank an established managed chronic disease to suspected.",
)

# Deterministic surveillance-finding net: promotes a ?malignancy-under-surveillance
# to significant_negative so it cannot drop on a prompt miss. High precision; the
# windows are deliberately wide on the recall side (a missed ?cancer is the
# asymmetric cost). Validated against the patient 1-30 audit incidentalomas.
_SURVEILLANCE_RE = re.compile(
    r"concerning for (?:a |an )?(?:malignan|cancer|carcinoma|neoplas|tumou?r|metasta|lymphoma|sarcoma|rcc)"
    r"|suspicious for (?:a |an )?(?:malignan|cancer|carcinoma|neoplas|tumou?r|metasta|lymphoma|sarcoma)"
    r"|worrisome for (?:a |an )?(?:malignan|cancer|carcinoma|neoplas|tumou?r|metasta)"
    r"|malignancy (?:not ruled out|not excluded|cannot be excluded|not yet excluded)"
    r"|cannot exclude (?:malignan|cancer|carcinoma|metasta)"
    r"|(?:growing|enlarging|increas\w*|interval (?:growth|increase|enlargement))[^.]{0,40}(?:nodule|mass|lesion)"
    r"|(?:nodule|mass|lesion)[^.]{0,40}(?:concerning|suspicious|indeterminate|growing|enlarging)"
    r"|indeterminate[^.]{0,25}(?:nodule|mass|lesion|cyst)"
    r"|incidental[^.]{0,60}(?:nodule|mass|lesion|cyst)"
    r"|applecore lesion"
    r"|(?:rising|increasing|elevated)[^.]{0,25}(?:psa|cea|ca[ -]?125|afp)"
    # v2.4 recall widening (full-run loss audit) -- wider on recall, asymmetric cost
    r"|possible[^.]{0,30}(?:malignan|cancer|carcinoma|neoplas)"
    r"|(?:malignan\w*|cancer|carcinoma|lymphoma|leukaem\w*|leukem\w*|metasta\w*|tumou?r|neoplas\w*|sarcoma)[^.]{0,25}(?:progress|recurren|relaps)"
    r"|(?:colon|colonic|rectal|rectum|sigmoid|gastric|duodenal|esophag\w*|oesophag\w*|bowel|bladder)[^.]{0,25}(?:wall )?thicken"
    r"|thicken\w*[^.]{0,25}(?:colon|colonic|rectal|rectum|sigmoid|gastric|duodenal|esophag\w*|oesophag\w*|bowel|bladder)"
    r"|nodular (?:opacit|densit)"
    r"|(?:hypoechoic|hypodense|hyperdense|enhancing)[^.]{0,25}(?:lesion|nodule|mass|focus|foci)"
    r"|\d+(?:\.\d+)?\s*-?\s*[mc]m\b[^.]{0,30}(?:nodule|mass|lesion)"
    r"|(?:nodule|mass|lesion)[^.]{0,30}\d+(?:\.\d+)?\s*-?\s*[mc]m\b",
    re.I,
)


# Deterministic disease-course-event net (Finding 1). A recurrent_episode fact that
# is really a malignancy recurrence/relapse/progression or a graft/organ rejection
# is a MATERIAL disease-course event, not a frequency-rollup exacerbation. b2/b3
# reclassify a match to sentinel_event (T2) so it pins as its own node instead of
# collapsing into an EpisodeRollup. High precision: fires only when an event term
# co-occurs with malignancy/graft context, so benign recurrent episodes (COPD, DKA,
# UTI, gout, lupus) are untouched. Same governance-versioned, recall-extensible
# design as is_surveillance_finding.
_ONCO_CTX_RE = re.compile(
    r"cancer|carcinoma|malignan|lymphoma|leukaem|leukem|myelom|sarcoma|tumou?r|"
    r"neoplas|metasta|\bhcc\b|\brcc\b|\baml\b|\bcml\b|\bcll\b|\bnhl\b|hodgkin|"
    r"melanoma|glioma|blastoma|adenocarc|\bgvhd\b",
    re.I,
)
_ONCO_EVENT_RE = re.compile(r"recurren|relaps|progress|refractory|transform|metasta|flare", re.I)
_REJECTION_RE = re.compile(r"\brejection\b", re.I)
_GRAFT_CTX_RE = re.compile(
    r"graft|allograft|transplant|cellular|humoral|antibody|organ|"
    r"renal|hepatic|cardiac|pulmonary|lung|kidney|liver|pancreas",
    re.I,
)


# ── HELPERS ──────────────────────────────────────────────────────────────────────

def is_known_class(class_id: str) -> bool:
    return class_id in _CLASS_IDS


def is_known_tier(tier_id: str) -> bool:
    return tier_id in _TIER_IDS


def class_ids() -> list[str]:
    return [c.id for c in RETENTION_CLASSES]


def tier_ids() -> list[str]:
    return [t.id for t in SALIENCE_TIERS]


def get_class(class_id: str) -> RetentionClass | None:
    return _CLASS_BY_ID.get(class_id)


def default_tier_for_class(class_id: str) -> str | None:
    c = _CLASS_BY_ID.get(class_id)
    return c.default_tier if c else None


# Tier salience order (lower rank = MORE salient): T1 < T2 < T3 < T4 < T5.
_TIER_RANK = {t.id: i for i, t in enumerate(SALIENCE_TIERS)}


def floor_tier(tier: str, floor: str | None) -> str:
    """Return the MORE-salient of `tier` and `floor` (T1 > T2 > T3 > T4 > T5). The per-class
    default_tier is a FLOOR to escalate from, not a value the extractor may de-escalate
    below - a chronic_disease_state must not be stored below T3, a sentinel_event below T2.
    Unknown inputs pass through unchanged."""
    if floor not in _TIER_RANK:
        return tier
    if tier not in _TIER_RANK:
        return floor
    return tier if _TIER_RANK[tier] <= _TIER_RANK[floor] else floor


def tier_action(tier_id: str) -> str | None:
    for t in SALIENCE_TIERS:
        if t.id == tier_id:
            return t.action
    return None


def is_surveillance_finding(text: str) -> bool:
    """Deterministic detector for incidental/suspicious findings needing surveillance."""
    return bool(_SURVEILLANCE_RE.search(text or ""))


def is_disease_course_event(text: str, parent_condition: str | None = None) -> bool:
    """True if a (recurrent_episode) fact is really a malignancy recurrence/relapse/
    progression or a graft/organ rejection -- a material disease-course event that
    must pin as its own node, not collapse into a frequency rollup. The malignancy/
    graft context may come from the text or the parent_condition; the event term
    must be in the text."""
    t = (text or "").lower()
    ctx = t + " " + (parent_condition or "").lower()
    if _REJECTION_RE.search(t) and _GRAFT_CTX_RE.search(ctx):
        return True
    if _ONCO_EVENT_RE.search(t) and _ONCO_CTX_RE.search(ctx):
        return True
    return False


_SENTINEL_EPISODE_RE = re.compile(
    r"status epilepticus"
    r"|gangrene"
    r"|(?:graft|bypass|stent|fistula|shunt|endograft|angioplasty)[^.]{0,30}"
    r"(?:occlu|thrombos|stenos|reocclu|fail|rupture)"
    r"|(?:occlu|thrombos|reocclu)\w*[^.]{0,30}"
    r"(?:graft|bypass|stent|fistula|shunt|endograft)",
    re.I,
)


def is_sentinel_episode(text: str) -> bool:
    """True if a (recurrent_episode) fact is really an acute sentinel event that must pin
    as its own node, not collapse into a frequency rollup: status epilepticus, gangrene,
    or graft/bypass/stent occlusion or thrombosis. High precision (specific terms);
    extensible like is_surveillance_finding."""
    return bool(_SENTINEL_EPISODE_RE.search(text or ""))


# ---- NET GOLDEN CASES (governance regression guard) -----------------------------
# Every deterministic re-routing net approved under TAXONOMY_EXTENSION.md section 4G
# lands its exemplars here, MUST-catch and MUST-NOT-catch, so a later recall widening
# cannot silently break an earlier ratified rule. Run:
#
#     python src/retention_taxonomy.py --selfcheck
#
# before AND after any edit to a net regex. Non-zero exit = a required case broke.
#
# status:
#   "required"  -- the rule must behave this way. A mismatch is a regression.
#   "xfail"     -- a KNOWN, RATIFIED gap. Must keep failing; an XPASS means the gap
#                  closed and the row should be promoted to "required".
#   "tolerated" -- the predicate fires wider than its clinical intent, but harmlessly,
#                  because normalize_fact only consults it for recurrent_episode.
#
# Case texts are short, generic clinical phrases drawn from the dev cohort. They carry
# no identifiers.
#
# (predicate, text, parent_condition, expected, status, note)
_NET_GOLDEN_CASES: tuple[tuple[str, str, str | None, bool, str, str], ...] = (
    # -- is_surveillance_finding  ------------------------------------
    ("is_surveillance_finding",
     "Incidental 8 mm right upper lobe pulmonary nodule",
     None, True, "required", "sized incidental nodule -- the core case"),
    ("is_surveillance_finding",
     "Multiple bilateral lung nodules up to 4.5 mm suspicious for metastases",
     None, True, "required", "sized nodule + suspicion wording"),
    ("is_surveillance_finding",
     "Acute COPD exacerbation",
     None, False, "required", "benign exacerbation must not be promoted"),
    ("is_surveillance_finding",
     "Acute alcohol withdrawal managed with CIWA protocol and diazepam",
     None, False, "required", "no lesion / no suspicion wording"),

    # -- is_disease_course_event  ------------------------------------
    ("is_disease_course_event",
     "Relapse of acute myeloid leukemia (AML) confirmed on bone marrow biopsy",
     None, True, "required", "malignancy relapse, context in text"),
    ("is_disease_course_event",
     "Richter transformation of CLL to large B-cell lymphoma",
     "Chronic lymphocytic leukemia", True, "required", "transformation"),
    ("is_disease_course_event",
     "CML flare presenting with leukocytosis and left shift after holding imatinib",
     "Chronic myelogenous leukemia (CML)", True, "required",
     "context supplied by parent_condition, event term in text"),
    ("is_disease_course_event",
     "Mild acute cellular rejection of liver allograft",
     None, True, "required", "graft rejection"),
    ("is_disease_course_event",
     "Ongoing vaginal bleeding with clots secondary to pelvic mass progression",
     "Metastatic squamous cell carcinoma", True, "required",
     "progression of a known malignancy presenting as a bleed"),
    ("is_disease_course_event",
     "Acute COPD exacerbation",
     "Chronic obstructive pulmonary disease", False, "required",
     "benign exacerbation -- the anchor recurrent_episode"),
    ("is_disease_course_event",
     "Acute gout flare with bilateral lower extremity pain, most severe on dorsum of foot and ankle",
     "Gout", False, "required", "flare without oncologic context"),
    ("is_disease_course_event",
     "Acute pancreatitis flare clinically resolving",
     "Chronic pancreatitis", False, "required", "flare without oncologic context"),
    ("is_disease_course_event",
     "Acute-on-chronic anemia secondary to multiple myeloma and GI bleeding requiring RBC transfusion",
     "Multiple myeloma", False, "required",
     "malignancy context present but NO disease-course event term -- must not fire"),
    ("is_disease_course_event",
     "Acute foot pain and ulcer flare without radiographic evidence of osteomyelitis",
     None, False, "required", "negation guard"),

    # -- is_sentinel_episode  -----------------------------------------------
    ("is_sentinel_episode",
     "Convulsive status epilepticus",
     None, True, "required", "status epilepticus"),
    ("is_sentinel_episode",
     "Non-convulsive status epilepticus requiring intubation and midazolam drip",
     None, True, "required", "status epilepticus, non-convulsive"),
    ("is_sentinel_episode",
     "Wet gangrene of left toe",
     None, True, "required", "gangrene"),
    ("is_sentinel_episode",
     "Occluded left femoral-peroneal bypass graft",
     None, True, "required", "bypass graft occlusion"),
    ("is_sentinel_episode",
     "Thrombosed arteriovenous fistula requiring alternative vascular access",
     None, True, "required", "dialysis access thrombosis"),
    ("is_sentinel_episode",
     "In-stent restenosis in LAD and RCA stents",
     None, True, "required", "stent restenosis"),
    ("is_sentinel_episode",
     "Distal arterial bypass graft stenosis",
     None, True, "required", "graft stenosis"),
    ("is_sentinel_episode",
     "Breakthrough generalized tonic-clonic seizure episode",
     "Epilepsy", False, "required",
     "breakthrough seizure in known epilepsy is a legitimate recurrent_episode"),
    ("is_sentinel_episode",
     "Coronary artery bypass grafting",
     None, False, "required", "the operation is not an occlusion event"),
    ("is_sentinel_episode",
     "Acute COPD exacerbation",
     None, False, "required", "benign exacerbation"),
    ("is_sentinel_episode",
     "Recurrent venous thrombosis around upper extremity hemodialysis lines and AV fistula",
     None, True, "xfail",
     "RATIFIED GAP: but the 30-char window between the "
     "thrombosis term and the access term does not reach. Window deliberately left at 30 "
     "pending evidence that widening does not leak. Promote to required if widened."),
    ("is_sentinel_episode",
     "End-stage renal disease requiring maintenance hemodialysis (qMWF) due to allograft failure",
     None, True, "tolerated",
     "graft + fail matches, but this is a permanent_condition -- normalize_fact only "
     "consults the net for recurrent_episode, so the over-match cannot route anything"),
)

_NET_PREDICATES = {
    "is_surveillance_finding": lambda t, p: is_surveillance_finding(t),
    "is_disease_course_event": lambda t, p: is_disease_course_event(t, p),
    "is_sentinel_episode":     lambda t, p: is_sentinel_episode(t),
}


def run_net_selfcheck(verbose: bool = True) -> int:
    """Execute _NET_GOLDEN_CASES against the live predicates. Returns the number of HARD
    failures: a "required" mismatch, an "xfail" that unexpectedly passed, or a "tolerated"
    case that changed. Zero means the nets behave as ratified."""
    hard = 0
    counts = {"pass": 0, "fail": 0, "xfail": 0, "xpass": 0}
    for pred, text, parent, expected, status, note in _NET_GOLDEN_CASES:
        fn = _NET_PREDICATES.get(pred)
        if fn is None:
            hard += 1
            if verbose:
                print(f"  ERROR   unknown predicate {pred!r}")
            continue
        got = bool(fn(text, parent))
        ok = (got == expected)
        if status == "xfail":
            if ok:
                counts["xpass"] += 1
                hard += 1
                if verbose:
                    print(f"  XPASS   {pred}: gap has CLOSED -- promote this row to "
                          f"'required'" + "\n" + f"          {text[:88]}")
            else:
                counts["xfail"] += 1
                if verbose:
                    print(f"  xfail   {pred}: known gap ({note.split(':')[0]})")
        elif ok:
            counts["pass"] += 1
        else:
            counts["fail"] += 1
            hard += 1
            if verbose:
                print("\n".join([
                    f"  FAIL    {pred}: expected {expected}, got {got}",
                    f"          text   : {text}",
                    f"          parent : {parent!r}",
                    f"          why    : {note}",
                ]))
    if verbose:
        print("\n" + f"{TAXONOMY_VERSION} net self-check: "
              f"{counts['pass']} passed, {counts['fail']} failed, "
              f"{counts['xfail']} xfail, {counts['xpass']} XPASS "
              f"({len(_NET_GOLDEN_CASES)} cases)")
        print("OK" if hard == 0 else f"** {hard} HARD FAILURE(S) -- do not ship")
    return hard


def is_ledger_member(
    salience_tier: str,
    certainty: str,
    lifecycle: str,
    negative_kind: str | None = None,
    retention_class: str | None = None,
) -> bool:
    """
    Derived must-retain decision. Class/lifecycle-aware certainty gate.

      ruled_out               -> never pinned (-> considered_excluded in B2)
      severe_adr_allergy / code_status_directive -> pinned unless ruled_out
        (safety classes robust to tier/certainty/lifecycle mis-tagging: a resolved
        or suspected severe drug reaction is still a lifetime contraindication; a
        directive is binding however tagged. v2.12 / FM-8: this branch now sits ABOVE
        the superseded guard, so a de-labelled allergy is SERVED as an explicit
        negative rather than vanishing into B2's `amended`. Formal disproof =
        ruled_out, still excluded by the guard above.)
      superseded              -> never pinned, EXCEPT the safety classes above
      negative_kind is not None -> pinned (any of absent_or_negative / resolved_relevant /
        surveillance: screening negatives, resolved-relevant events, incidentaloma under
        surveillance -- same pin behaviour as the old significant_negative bool)
      confirmed, T1-T3        -> pinned (current state)
      suspected + active, T1-T3 -> pinned as PROVISIONAL (open problem / under workup)
      suspected sentinel_event, T1-T3, ANY lifecycle -> pinned as PROVISIONAL (v2.10: a
        presumed major event that resolved still has lasting prognostic weight)
      patient_reported        -> pinned for allergy / code-status / infection-control only
    """
    if certainty == "ruled_out":
        return False
    # safety classes are tested BEFORE the superseded guard. 
    # certainty=ruled_out (above) is the SOLE exit from a safety-class pin.
    if retention_class in _SAFETY_ROBUST_CLASSES:
        return True
    if lifecycle == "superseded":
        return False
    if negative_kind is not None:
        return True
    if certainty == "confirmed":
        return salience_tier in ("T1", "T2", "T3")
    if certainty == "suspected":
        if lifecycle == "active":
            return salience_tier in ("T1", "T2", "T3")
        # A suspected sentinel_event pins as PROVISIONAL regardless of lifecycle -
        # a presumed major event that resolved (type II NSTEMI, UGI bleed) still has lasting
        # prognostic weight and must reach physician review. ruled_out / superseded are
        # already excluded by the guards above. Tier-gated, so it relies on the tier
        # floor (sentinel_event >= T2) applied at extraction.
        if retention_class == "sentinel_event":
            return salience_tier in ("T1", "T2", "T3")
    if certainty == "patient_reported":
        return retention_class in _PATIENT_REPORTED_RETAIN
    return False


def is_compressible(
    salience_tier: str,
    certainty: str,
    lifecycle: str,
    negative_kind: str | None = None,
    retention_class: str | None = None,
) -> bool:
    return not is_ledger_member(salience_tier, certainty, lifecycle, negative_kind, retention_class)


# Reasons a fact was declined by is_ledger_member. Recorded on b2's dropped[] so a
# prune is inspectable rather than silent.
PRUNE_REASONS = (
    "ruled_out",                      # certainty=ruled_out - kept as a negative in considered_excluded
    "superseded",                     # lifecycle=superseded - de-labelled / replaced
                                      # (never fires for a safety class)
    "not_pinned_tier",                # T4/T5 with no negative_kind - the ordinary tier prune.
                                      # confirmed, OR suspected+active / suspected
                                      # sentinel_event, both of which are tier-gated too
    "suspected_inactive",             # suspected + resolved/in_remission, non-sentinel class
                                      # (lifecycle IS the reason here - any tier)
    "patient_reported_other_class",   # patient_reported outside allergy/code-status/infection-control
    "unclassified",                   # gate declined for a combination not named above
)


def prune_reason(
    salience_tier: str,
    certainty: str,
    lifecycle: str,
    negative_kind: str | None = None,
    retention_class: str | None = None,
) -> str | None:
    """WHY is_ledger_member() declined this fact. None when the fact IS a ledger member.

    Mirrors the gate's branch order exactly - edit both together, or the recorded
    reason will drift from the decision it claims to explain. Consumed by b2 to fill
    RetentionLedger.dropped[]: the prune itself is unchanged and usually correct,
    but it must leave a trace, so a pruned fact is distinguishable from a lost one.

    The safety-class branch needs no mirror - those facts return None above, so
    only ruled_out ever yields a reason for them and "superseded" is unreachable for a
    safety class. 
    """
    if is_ledger_member(salience_tier, certainty, lifecycle, negative_kind, retention_class):
        return None
    if certainty == "ruled_out":
        return "ruled_out"
    if lifecycle == "superseded":
        return "superseded"
    if certainty == "confirmed":
        return "not_pinned_tier"
    if certainty == "suspected":
        # Mirror the gate's TWO suspected branches. Both suspected+active and
        # suspected+sentinel_event are TIER-gated, so a decline there is a tier prune, not
        # an inactivity prune. Reporting "suspected_inactive" for a fact whose lifecycle is
        # ACTIVE hid a tier decision behind a lifecycle word, and made those facts invisible
        # to any dropped[] query looking for tier-threshold defects.
        if lifecycle == "active" or retention_class == "sentinel_event":
            return "not_pinned_tier"
        return "suspected_inactive"
    if certainty == "patient_reported":
        return "patient_reported_other_class"
    return "unclassified"


def normalize_fact(fd: dict) -> tuple[bool, bool]:
    """Deterministic taxonomy normalization applied by EVERY consumer of a stored
    fact dict (b2 ledger build, c1 per-admission facts, b3 loss audit,
    db_persist_source DB load), so file readers and the DB rows see the SAME
    classes:

      1. surveillance promotion: an incidental/suspicious finding with no explicit
         negative_kind -> negative_kind='surveillance' (asymmetric-cost safety net).
      2. disease-course reclassification: a recurrent_episode that is a disease-
         course event or sentinel episode -> sentinel_event, T2.

    Mutates fd in place; returns (promoted, reclassified) so callers can count.
    b1's extraction already applies (1) at write time; (2) is intentionally
    derived on read so the stored facts stay raw and re-derivation tracks the
    current taxonomy without re-extraction."""
    promoted = reclassified = False
    if fd.get("negative_kind") is None and is_surveillance_finding(fd.get("text", "")):
        fd["negative_kind"] = "surveillance"
        promoted = True
    if fd.get("retention_class") == "recurrent_episode" and (
            is_disease_course_event(fd.get("text", ""), fd.get("parent_condition"))
            or is_sentinel_episode(fd.get("text", ""))):
        fd["retention_class"] = "sentinel_event"
        fd["salience_tier"] = "T2"
        reclassified = True
    return promoted, reclassified


def render_for_prompt() -> str:
    lines = [
        f"CLINICAL SALIENCE TAXONOMY ({TAXONOMY_VERSION})",
        "Tag each extracted fact on THREE axes. Anchor cases:",
        "  - A sixth routine COPD exacerbation is a recurrent_episode (T4).",
        "  - A single Stevens-Johnson episode is a severe_adr_allergy (T1), kept for life.",
        "  - A lymphoma CONSIDERED then EXCLUDED is certainty=ruled_out: record it, do NOT pin it.",
        "  - A penicillin allergy DISPROVEN on challenge is lifecycle=superseded.",
        "  - Amiodarone STOPPED because it caused a reaction is lifecycle=ACTIVE, NOT "
        "superseded: the drug changed, the contraindication did not.",
        "",
        "AXIS 1 - salience_tier (one of T1..T5):",
    ]
    for t in SALIENCE_TIERS:
        win = "never prune" if t.recency_window_days is None else f"prune after ~{t.recency_window_days}d"
        lines.append(f"  {t.id} {t.label}: {t.selecting_property} [{t.action}; {win}]")
    lines += ["", "AXIS 2 -- retention_class (one id; sets a default tier you may escalate for safety):"]
    for c in RETENTION_CLASSES:
        lines.append(f"  {c.id} (default {c.default_tier}) -- {c.description}")
        lines.append(f"      rule: {c.decision_rule}")
        lines.append(f"      e.g.: {'; '.join(c.examples)}")
        if c.counter_examples:
            lines.append(f"      NOT: {'; '.join(c.counter_examples)}")
    lines += [
        "",
        "AXIS 3 - status modifiers:",
        f"  certainty:  {' | '.join(CERTAINTY)}",
        f"  lifecycle:  {' | '.join(LIFECYCLE)}",
        "    superseded = THE RECORD IS WITHDRAWN, not the treatment. Use it ONLY when a "
        "LATER note retracts or replaces an EARLIER assertion in this patient's own "
        "history: an allergy de-labelled on challenge, a code-status directive replaced "
        "by a newer one. A fact appearing for the FIRST time is NEVER superseded.",
        "    A drug STOPPED, SWITCHED, SUBSTITUTED or DISCONTINUED because it caused a "
        "reaction is NOT superseded - the reaction is a standing contraindication that "
        "outlives the prescription. Use lifecycle=active.",
        "    e.g. 'amiodarone intolerance, switched to sotalol' -> severe_adr_allergy, "
        "lifecycle=ACTIVE. 'penicillin allergy disproven on challenge' -> superseded AND "
        "negative_kind=absent_or_negative (served as an explicit negative).",
        f"  occurrence: {' | '.join(OCCURRENCE)}  (index = first time seen across this patient's history)",
        "",
        "negative_kind (one of: null | " + " | ".join(NEGATIVE_KIND) + "): set for clinically "
        "important negatives/resolved-events/surveillance findings; null for an ordinary "
        "positive present condition:",
    ]
    for g in NEGATIVE_KIND_GUIDANCE:
        lines.append(f"  - {g}")
    lines += ["", "EXTRACTION RULES (completeness + discipline):"]
    for r in EXTRACTION_RULES:
        lines.append(f"  - {r}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys as _sys
    if "--selfcheck" in _sys.argv[1:]:
        raise SystemExit(run_net_selfcheck())
    print(render_for_prompt())
    print(f"\n{len(SALIENCE_TIERS)} tiers x {len(RETENTION_CLASSES)} classes.")
