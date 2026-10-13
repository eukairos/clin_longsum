# clin_longsum - physician feedback loop
# Copyright (C) 2026 Eukairos
# SPDX-License-Identifier: AGPL-3.0-only
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of version 3 of the GNU Affero General Public License as published
# by the Free Software Foundation. This program is distributed WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE. See the GNU Affero General Public License for details.

"""
triage.py
=========
Deterministic triage: urgency, routing, S1 sub-class, dedup key.

NO MODEL IS INVOLVED. Every decision here is a boolean over captured fields or
an indexed query. That is a design choice with three payoffs:

  1. EXACT REPLAY. A proposed policy change can be re-run over every historical
     card and diffed, so its effect is known before promotion. A model-based
     policy could only be sampled and audited after the fact. This is the same
     discipline the taxonomy already gets from its regression corpus.
  2. It passes clinical governance and IT security review without a conversation
     about model behaviour.
  3. No VRAM contention with the fold-in cascade on the shared GPU.

`TriageEngine` is a seam, not decoration: at hospital scale an LLM assist for
paraphrase-level dedup can be added as a second implementation without the
domain core learning anything about it.

RULE / POLICY PAIRING
---------------------
Each U-* rule is one function here; its PARAMETERS live in the YAML. At import,
the two sets are asserted equal. An implemented-but-undeclared rule is the
dangerous direction - a gate firing that nobody signed off on - and it fails
just as loudly as the reverse.

WHAT TRIAGE DOES NOT DO
-----------------------
It never closes a card, never sets a disposition, and never downgrades urgency.
Urgency is a floor: rules may raise a card to urgent, only a named human may
lower it, and the downgrade is appended to feedback_events. Wrongly holding a
card costs attention; wrongly releasing one costs a patient.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol

# Package-qualified import: src/ is on sys.path for every stage, so `support.X`
# resolves the same way `database.pg` does. The fallback covers direct execution
# from inside support/, where sys.path[0] is this folder rather than src/.
try:
    from support import policy_loader as pol_mod
except ImportError:  # pragma: no cover - direct `python triage.py`
    import policy_loader as pol_mod

# ── normalisation + dedup ──────────────────────────────────────────────────────

_WS = re.compile(r"\s+")
_TRAILING_PUNCT = re.compile(r"[\s.,;:!?)\]}\-]+$")


def normalize_fact_text(text: Optional[str]) -> str:
    """casefold, collapse whitespace, strip trailing punctuation (per policy)."""
    if not text:
        return ""
    return _TRAILING_PUNCT.sub("", _WS.sub(" ", text.strip()).casefold())


def dedup_key(symptom: str, retention_class: Optional[str],
              salience_tier: Optional[str], fact_text: Optional[str]) -> str:
    """Signature identifying 'the same defect', for LINKING cards.

    Cards are never merged away on a match. The count of DISTINCT SUBJECTS
    carrying a key is the systematic-defect detector that U-5 reads; collapsing
    duplicates would destroy exactly the signal the loop exists to surface.
    """
    base = "|".join([symptom or "", (retention_class or "").casefold(),
                     (salience_tier or "").upper(), normalize_fact_text(fact_text)])
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


# ── inputs / outputs ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DedupContext:
    """Corroboration from cards already on file, supplied by the service layer.

    Kept as data rather than a DB handle so triage stays pure and can be replayed
    over historical cards offline.
    """
    distinct_subjects: int = 1      # distinct subjects carrying this dedup_key in window
    prior_card_ids: tuple[int, ...] = ()


@dataclass
class TriageResult:
    urgency: str = "routine"
    urgency_rule_id: Optional[str] = None
    fired_rules: list[str] = field(default_factory=list)
    owner_roles: list[str] = field(default_factory=list)
    defect_class: Optional[str] = None
    s1_subclass: Optional[str] = None
    dedup_key: Optional[str] = None
    policy_version: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def is_urgent(self) -> bool:
        return self.urgency == "urgent"


# ── urgency rules ──────────────────────────────────────────────────────────────
# Signature: (card, ctx, params) -> bool. `card` is any Mapping with the
# feedback-table column names, so a live row and a replayed historical row are
# interchangeable.

def _tier(card: Mapping[str, Any]) -> str:
    return (card.get("salience_tier") or "").upper()


def _in_tiers(card: Mapping[str, Any], params: Mapping[str, Any]) -> bool:
    tiers = {t.upper() for t in (params.get("tiers") or ())}
    return bool(tiers) and _tier(card) in tiers


def _is_symptom(card: Mapping[str, Any], params: Mapping[str, Any]) -> bool:
    syms = set(params.get("symptoms") or ())
    return bool(syms) and card.get("symptom") in syms


def _u1(card, ctx, params) -> bool:
    """T1 fact reported missing. The archetypal safety loss."""
    return _is_symptom(card, params) and _in_tiers(card, params)


def _u2(card, ctx, params) -> bool:
    """T1/T2 row reads as the opposite of the truth."""
    return _is_symptom(card, params) and _in_tiers(card, params)


def _u3(card, ctx, params) -> bool:
    """Safety lifecycle inversion on a T1 row.

    Needs disputed_field: a disputed current_lifecycle on T1 is a superseded
    allergy shown active; a disputed salience_tier on the same row is routine.
    Without the field the two are indistinguishable, so an S3 report that omits
    it cannot fire this gate.
    """
    fields = set(params.get("disputed_fields") or ())
    return (_is_symptom(card, params) and _in_tiers(card, params)
            and card.get("disputed_field") in fields)


def _u4(card, ctx, params) -> bool:
    """Wrong patient. Also a privacy incident, not only a defect."""
    return _is_symptom(card, params)


def _u5(card, ctx, params) -> bool:
    """Systematic: the same signature now seen on N+ distinct subjects at T1/T2.

    Corroboration comes from the dedup context, not the reporter's impression.
    `seen_other_patients` on the card raises attention but does not fire this
    rule on its own - one physician's recollection is not a cohort query.
    """
    threshold = int(params.get("distinct_subjects_threshold", 2))
    return _in_tiers(card, params) and ctx.distinct_subjects >= threshold


def _u6(card, ctx, params) -> bool:
    """A clinical decision was made, or nearly made, on this row.

    The one question of FACT asked of the reporter. It replaces a self-declared
    severity dial, which would inflate to urgent on every report.
    """
    return bool(card.get(params.get("field", "clinical_action_flag")))


def _u7(card, ctx, params) -> bool:
    """Text not in the source notes, on a T1/T2 row."""
    return _is_symptom(card, params) and _in_tiers(card, params)


_RULES = {
    "U-1": _u1, "U-2": _u2, "U-3": _u3, "U-4": _u4,
    "U-5": _u5, "U-6": _u6, "U-7": _u7,
}

# Fail at import if the YAML and this module disagree in either direction.
pol_mod.validate_against_implementations(pol_mod.load(), set(_RULES))


# ── engine ─────────────────────────────────────────────────────────────────────

class TriageEngine(Protocol):
    def triage(self, card: Mapping[str, Any], ctx: DedupContext,
               evidence: Optional[Mapping[str, Any]] = None) -> TriageResult: ...


class RuleTriage:
    """The only engine, and the only one currently wanted."""

    def __init__(self, policy=None):
        self.policy = policy or pol_mod.load()

    # -- routing ---------------------------------------------------------------

    def _route(self, card: Mapping[str, Any], result: TriageResult,
               evidence: Optional[Mapping[str, Any]]) -> None:
        sym = self.policy.symptom(card["symptom"])
        result.owner_roles = list(sym.owner_roles)
        result.defect_class = sym.defect_class_hint

        if card["symptom"] != "S1":
            return

        # S1 is the common report and the ambiguous one. Its sub-class is
        # resolved deterministically by evidence.py against the clinical DB
        # (retention_facts -> retention_dropped -> tiered_ledger_raw ->
        # tiered_ledger). Both owners remain on the card regardless: the
        # sub-class tells them where to look, it does not decide who is right.
        sub = (evidence or {}).get("s1_subclass")
        if sub and sub in self.policy.s1_subclass:
            result.s1_subclass = sub
            result.defect_class = self.policy.defect_class_for_s1(sub)
            result.notes.append(
                f"S1 resolved to {sub}: "
                f"{self.policy.s1_subclass[sub].get('meaning', '')}")
        else:
            result.notes.append(
                "S1 sub-class unresolved - evidence not yet collected. "
                "Card is dual-owned until it is.")

    # -- urgency ---------------------------------------------------------------

    def _urgency(self, card: Mapping[str, Any], ctx: DedupContext,
                 result: TriageResult) -> None:
        for rule in self.policy.urgency_rules:
            fn = _RULES.get(rule.id)
            if fn is None:                      # unreachable: asserted at import
                continue
            try:
                if fn(card, ctx, rule.params):
                    result.fired_rules.append(rule.id)
            except Exception as exc:            # a broken rule must not swallow the rest
                result.notes.append(f"rule {rule.id} errored ({exc}); treated as not fired")
        if result.fired_rules:
            result.urgency = "urgent"
            # First declared rule wins as the headline. Order in the YAML is
            # therefore meaningful: it is the order a human should read them in.
            result.urgency_rule_id = result.fired_rules[0]

    # -- effective card --------------------------------------------------------

    @staticmethod
    def _effective(card: Mapping[str, Any],
                   evidence: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
        """Overlay evidence-discovered axes onto axes the card could not carry.

        A report filed against a visible row carries that row's tier and class. A
        report that something is MISSING carries neither, because there is no row
        - and U-1, the gate that makes a missing T1 fact urgent, keys on tier. The
        physician cannot supply it; tiers are an internal construct.

        So where the card is silent and the collector found the fact's real axes,
        they are used. Strictly additive: a value the card already carries is
        never overwritten, so evidence can complete a card but never contradict
        what the physician was actually looking at.
        """
        axes = (evidence or {}).get("discovered_axes") or {}
        if not axes:
            return card
        merged = dict(card)
        applied = []
        for k, v in axes.items():
            if not merged.get(k) and v:
                merged[k] = v
                applied.append(k)
        merged["_axes_from_evidence"] = applied
        return merged

    # -- entry point -----------------------------------------------------------

    def triage(self, card: Mapping[str, Any], ctx: DedupContext | None = None,
               evidence: Optional[Mapping[str, Any]] = None) -> TriageResult:
        ctx = ctx or DedupContext()
        result = TriageResult(policy_version=self.policy.version)
        card = self._effective(card, evidence)
        applied = card.get("_axes_from_evidence") or []
        if applied:
            result.notes.append(
                f"axes recovered from evidence ({(evidence or {}).get('discovered_axes_source')}): "
                f"{', '.join(applied)}")
        # Dedup key is computed on the EFFECTIVE card so that a missing-fact
        # report and a row-anchored report about the same defect share a
        # signature - otherwise U-5 would never see them as the same thing.
        result.dedup_key = dedup_key(card["symptom"], card.get("retention_class"),
                                     card.get("salience_tier"), card.get("fact_text"))
        self._route(card, result, evidence)
        self._urgency(card, ctx, result)
        return result


_default_engine: RuleTriage | None = None


def get_engine() -> RuleTriage:
    global _default_engine
    if _default_engine is None:
        _default_engine = RuleTriage()
    return _default_engine


def triage(card: Mapping[str, Any], ctx: DedupContext | None = None,
           evidence: Optional[Mapping[str, Any]] = None) -> TriageResult:
    return get_engine().triage(card, ctx, evidence)


if __name__ == "__main__":
    p = pol_mod.load()
    print(pol_mod.summary(p))
    demo = {"symptom": "S1", "salience_tier": "T1", "retention_class": "allergy_intolerance",
            "fact_text": "Penicillin allergy - anaphylaxis", "clinical_action_flag": False}
    r = triage(demo)
    print(f"urgency={r.urgency} rule={r.urgency_rule_id} owners={r.owner_roles} "
          f"dedup={r.dedup_key}")
