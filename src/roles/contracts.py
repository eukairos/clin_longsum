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
contracts.py
============
Role interfaces for CLIN_LONGSUM.

Each pipeline step is a ROLE with a fixed input/output schema. The model behind
a role is bound in config.MODELS and can be swapped without changing any role's contract. 
Every role invocation emits an AuditRecord, so the provenance of any generated artifact 
- which model, which prompt version, which taxonomy version, over which source admissions - is
always recoverable.

This module defines DATA CONTRACTS ONLY.

Roles in the pipeline:
  single_summary        Tier 1 - latest episode summary (one BHC -> draft)
  retention_extractor   cross-cutting - BHC -> facts tagged on three axes
  episode_paragrapher   Tier 2a - approved episode summary -> cached paragraph
  tldr_synthesizer      Tier 2b - prior + hot paragraphs -> TL;DR (every adm)
  compactor             Tier 3 - fold aged-out admissions into Prior Admissions
  faithfulness_validator  no hallucination (reused from synth)
  retention_validator     no critical omission (compaction-specific)

RetentionFact carries the three-axis salience model:
salience_tier x retention_class x status modifiers. Ledger membership and
compressibility are DERIVED from the axes, not stored, so the rule lives in one
place (retention_taxonomy.is_ledger_member).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

import retention_taxonomy as tax


# ── AUDIT ────────────────────────────────────────────────────────────────────────

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


class AuditRecord(BaseModel):
    """
    Provenance for one role invocation. Written to generation_audit.jsonl and
    referenced by every produced artifact.

    A prompt change is as auditable as a model change: prompt_version is
    recorded alongside model_version. For retention-touching roles,
    taxonomy_version pins the governance object in force at generation time.
    """
    role: str
    provider: str
    model_id: str
    model_version: str
    prompt_version: str
    taxonomy_version: Optional[str] = None
    params: dict = Field(default_factory=dict)

    subject_id: Optional[int] = None
    input_hadm_ids: list[int] = Field(default_factory=list)

    output_sha256: Optional[str] = None
    scores: dict = Field(default_factory=dict)
    status: str = "ok"                            # "ok" | "error" | "flagged"
    error: Optional[str] = None
    created_at: str = Field(default_factory=_utc_now)

    @classmethod
    def from_spec(
        cls,
        spec,                       # config.ModelSpec (duck-typed)
        prompt_version: str,
        *,
        taxonomy_version: Optional[str] = None,
        subject_id: Optional[int] = None,
        input_hadm_ids: Optional[list[int]] = None,
        output_text: Optional[str] = None,
        scores: Optional[dict] = None,
        status: str = "ok",
        error: Optional[str] = None,
    ) -> "AuditRecord":
        params = dict(getattr(spec, "params", {}) or {})
        params["think"] = getattr(spec, "think", None)
        return cls(
            role=getattr(spec, "role"),
            provider=getattr(spec, "provider"),
            model_id=getattr(spec, "model_id"),
            model_version=getattr(spec, "version"),
            prompt_version=prompt_version,
            taxonomy_version=taxonomy_version,
            params = params,
            subject_id=subject_id,
            input_hadm_ids=list(input_hadm_ids or []),
            output_sha256=sha256_text(output_text) if output_text is not None else None,
            scores=scores or {},
            status=status,
            error=error,
        )


# ── SHARED CLINICAL UNIT: three-axis fact ────────────────────────────────────────

class RetentionFact(BaseModel):
    """
    One atomic clinical fact tagged on the three salience axes
    
    Axes:
      salience_tier         T1..T5  -- representation + temporal action
      retention_class       one of tax.class_ids()
      certainty             confirmed | suspected | ruled_out | patient_reported
      lifecycle             active | resolved | in_remission | superseded
      occurrence            index | recurrence
      negative_kind         None | absent_or_negative | resolved_relevant | surveillance
                    
    Linkage / provenance:
      parent_condition  for recurrent_episode (T4): the chronic_disease_state it flares from
      supersedes        fact_id this fact amends (de-label, explant, status change);
                        resolved during ledger projection (b2), usually null at extraction
      fact_id           stable id assigned by the runner (sha1 of hadm_id + text)

    Ledger membership and compressibility are DERIVED (not stored) so the rule
    lives only in retention_taxonomy.
    """
    text: str
    retention_class: str
    salience_tier: str
    certainty: str = "confirmed"
    lifecycle: str = "active"
    occurrence: str = "index"
    negative_kind: Optional[str] = None        # None | absent_or_negative | resolved_relevant | surveillance

    parent_condition: Optional[str] = None
    supersedes: Optional[str] = None
    rationale: Optional[str] = None

    fact_id: Optional[str] = None
    source_hadm_id: Optional[int] = None
    admit_seq: Optional[int] = None

    @property
    def in_ledger(self) -> bool:
        """Pinned in the must-retain ledger? Derived from the axes (v2.9: negative_kind)."""
        return tax.is_ledger_member(
            self.salience_tier, self.certainty, self.lifecycle,
            self.negative_kind, self.retention_class,
        )

    @property
    def is_compressible(self) -> bool:
        """Eligible for narrative compaction? (i.e. NOT a ledger member)."""
        return not self.in_ledger

    @property
    def first_occurrence(self) -> bool:
        return self.occurrence == "index"

    def assign_fact_id(self) -> str:
        base = f"{self.source_hadm_id}|{self.text}"
        self.fact_id = hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]
        return self.fact_id


# ── ROLE: retention_extractor ──────────────────────────────────────────────────

class RetentionExtractorInput(BaseModel):
    """One admission's BHC in, three-axis tagged facts out."""
    subject_id: int
    hadm_id: int
    admit_seq: int
    bhc_text: str
    admit_date: Optional[str] = None
    discharge_date: Optional[str] = None
    service: Optional[str] = None
    # Conditions already established in earlier admissions (fed forward in
    # admit_seq order). Lets the extractor set occurrence=index vs recurrence
    # and detect lifecycle changes (a prior active condition now resolved).
    known_conditions: list[str] = Field(default_factory=list)


class RetentionExtractorOutput(BaseModel):
    subject_id: int
    hadm_id: int
    admit_seq: int
    facts: list[RetentionFact] = Field(default_factory=list)
    audit: AuditRecord

    def ledger_facts(self) -> list[RetentionFact]:
        """Facts pinned to the must-retain ledger (derived)."""
        return [f for f in self.facts if f.in_ledger]

    def compressible_facts(self) -> list[RetentionFact]:
        return [f for f in self.facts if f.is_compressible]


# ── ROLE: episode_paragrapher (Tier 2a) ─────────────────────────────────────────

class EpisodeParagrapherInput(BaseModel):
    """
    One approved single-episode summary -> one cached longitudinal-grade
    paragraph. Runs ONCE per admission after physician approval; cached and retrieved thereafter.
    """
    subject_id: int
    hadm_id: int
    admit_seq: int
    approved_summary_md: str
    admit_date: Optional[str] = None
    discharge_date: Optional[str] = None
    service: Optional[str] = None
    known_conditions: list[str] = Field(default_factory=list)


class EpisodeParagraph(BaseModel):
    subject_id: int
    hadm_id: int
    admit_seq: int
    admit_date: Optional[str] = None
    service: Optional[str] = None
    paragraph_md: str
    audit: AuditRecord


# ── ROLE: tldr_synthesizer (Tier 2b) ────────────────────────────────────────────

class TldrSynthesizerInput(BaseModel):
    """
    Re-synthesize the TL;DR over the whole current picture. Inputs are
    PARAGRAPHS, not raw deltas. Prompt 'tldr_synthesizer.v1':
      "[short, succinct paragraph: overall clinical story, dominant condition
       pattern, and trajectory]"
    """
    subject_id: int
    prior_admissions_md: str = ""
    hot_window_paragraphs: list[EpisodeParagraph] = Field(default_factory=list)
    n_total_admissions: int = 0
    first_date: Optional[str] = None
    last_date: Optional[str] = None


class TldrSynthesizerOutput(BaseModel):
    subject_id: int
    tldr_md: str
    n_total_admissions: int = 0
    audit: AuditRecord


# ── ROLE: compactor (Tier 3) ─────────────────────────────────────────────────────

class AgedOutAdmission(BaseModel):
    """
    An admission leaving the hot window, to be folded into Prior Admissions.
    Facts are pre-extracted by retention_extractor so the compactor cannot drift
    the ledger. Compaction works from bhc_delta; bhc_text is a fallback.
    """
    hadm_id: int
    admit_seq: int
    admit_date: Optional[str] = None
    discharge_date: Optional[str] = None
    service: Optional[str] = None
    bhc_delta: Optional[str] = None
    bhc_text: Optional[str] = None
    facts: list[RetentionFact] = Field(default_factory=list)


class CompactorInput(BaseModel):
    """
    Fold one or more aged-out admissions into the Prior Admissions tier.

    mode "incremental": start from existing_prior_md, fold in `aged_out`.
    mode "full":        rebuild from `source_hadm_ids` (runner supplies the span).
    existing_prior_md is the MERGEABLE seed (prior machine continuation).
    pinned_prefix_md (seeded fold-in with a physician edit) is frozen byte-exact
    by the runner and prepended AFTER compaction; compact() merges only the
    continuation and never sees or rewrites it. seed_source is recorded to audit.
    
    retention_ledger carries every pinned fact already retained. The compactor
    returns it intact plus any new ledger facts from `aged_out`. It is never
    asked to summarize the ledger - only to merge.

    NAMING: the field is `ledger_facts`, a list[RetentionFact]. It is NOT the same
    shape as retention_ledger.jsonl, whose `nodes[]` are b2 LedgerNode objects. The
    two were both called "retention_ledger" and were routinely confused in tracing;
    readers of older artifacts should fall back to the legacy `retention_ledger` key.
    """
    subject_id: int
    existing_prior_md: str = ""
    pinned_prefix_md: str = ""          # physician block; runner freezes + prepends, compact() never touches it
    aged_out: list[AgedOutAdmission] = Field(default_factory=list)
    ledger_facts: list[RetentionFact] = Field(default_factory=list)
    source_hadm_ids: list[int] = Field(default_factory=list)
    n_prior_admissions: int = 0
    mode: str = "incremental"
    seed_source: str = "none"           # none | generated | edited (-> audit.scores)


class CompactorOutput(BaseModel):
    subject_id: int
    prior_admissions_md: str            # SERVED = pinned_prefix_md + marker + prior_generated_md (or generated alone)
    pinned_prefix_md: str = ""          # physician frozen block; "" when no edit
    prior_generated_md: str = ""        # machine continuation (never contains physician bytes)
    generated_hadm_ids: list[int] = Field(default_factory=list)  # continuation coverage = covered − prefix; c3/c4 scope
    ledger_facts: list[RetentionFact] = Field(default_factory=list)  # SPAN-SCOPED set the model was shown
    n_admissions_compacted: int = 0
    covered_hadm_ids: list[int] = Field(default_factory=list)     # FULL union (unchanged meaning)
    audit: AuditRecord


# ── ROLE: retention_validator (compaction-specific) ─────────────────────────────

class RetentionCompletenessInput(BaseModel):
    """
    Check that compaction dropped no pinned fact (critical OMISSION), the
    opposite failure to faithfulness. A miss here (a dropped Stevens-Johnson) is
    higher severity than a faithfulness miss.

    source_facts: every pinned (in_ledger) fact that SHOULD be represented for
                  the compacted span (union of ledger facts across covered_hadm_ids).
    """
    subject_id: int
    prior_admissions_md: str
    ledger_facts: list[RetentionFact] = Field(default_factory=list)
    source_facts: list[RetentionFact] = Field(default_factory=list)


class RetentionCompletenessOutput(BaseModel):
    subject_id: Optional[int] = None      # top-level key; also in audit.subject_id
    complete: bool
    score: float                          # represented / total pinned
    missing_facts: list[RetentionFact] = Field(default_factory=list)
    audit: AuditRecord


# ── ASSEMBLED VIEW (what the coordinator hands the UI) ───────────────────────────

class LongitudinalView(BaseModel):
    """
    Full assembled 3-tier view, newest-first:
      tldr_md             Tier 2b, top, re-synthesized every admission
      current_episode_md  Tier 1, latest approved single-episode summary
      hot_window          Tier 2a, last N_HOT_DISPLAY cached paragraphs
      prior_admissions_md Tier 3, compacted ("" if <= threshold admissions)
      ledger_facts        cross-cutting pinned facts (current-state projection)
    Known Conditions / Imaging / Major Procedures are assembled from structured
    data OUTSIDE this subproject and merged by the caller.
    """
    subject_id: int
    n_total_admissions: int
    tldr_md: str = ""
    current_episode_md: str = ""
    hot_window: list[EpisodeParagraph] = Field(default_factory=list)
    prior_admissions_md: str = ""       # served full (prefix + marker + continuation)
    prior_generated_md: str = ""        # machine continuation only; feeds tiered_view.prior_generated_md for display reconstruction
    ledger_facts: list[RetentionFact] = Field(default_factory=list)


