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
replies.py
==========
Turns a collector finding into a pre-written disposition and reply.

WHY THIS EXISTS. The closed loop rests on one rule: nothing closes without a
written answer to the reporter. That rule is only sustainable if writing the
answer is cheap. A support team facing a blank textarea on every card will, by
the third week, either write two words or stop closing cards - and an unanswered
report is worse than no loop at all, because the physician concludes that
reporting does nothing and stops.

So where the evidence already settles the question, the answer is written in
advance. The commonest case by far is expected to be S1e: a fact documented only
in the incoming admission, correctly absent from a ledger built from prior
admissions. That answer is the same every time, and nobody should be composing
it from scratch.

THESE ARE DRAFTS, NOT DECISIONS. The suggestion is pre-filled into the board's
reply box and the owner edits or replaces it before sending. Nothing here can
close a card, and no suggestion is offered where the evidence is ambiguous - a
confident-sounding draft on an unresolved card is worse than an empty box,
because it invites a rubber stamp.

WORDING RULES, learned from the email templates:
  - lead with the verdict, because a clinician reads the first line and triages;
  - say what to do about THIS patient now, since the fix ships on a release
    cycle and the patient is in front of them today;
  - never explain the pipeline. "Not yet folded in" is a fact about the record;
    "b4 consolidation absorbed the node" is not the reporter's problem;
  - thank them for a report that turns out not to be a defect, and mean it - the
    alternative is a physician who stops reporting borderline cases, which is
    where the real defects hide.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

# ── S1 sub-class answers ───────────────────────────────────────────────────────

def _s1e(card: Mapping[str, Any], ev: Mapping[str, Any]) -> tuple[str, str]:
    probe = ev.get("missing_probe") or {}
    inc = next((p for p in probe.get("per_admission", []) if p.get("is_incoming")), None)
    where = f" (admission {inc['admit_seq']})" if inc else ""
    return "by_design", (
        f"Thank you - this one is working as intended, though I can see why it "
        f"looks wrong.\n\n"
        f"This fact is documented only in the admission you are reviewing now"
        f"{where}. The Retention Ledger covers the patient's PRIOR admissions - "
        f"the established record - so anything documented for the first time in "
        f"the current admission is correctly absent from it. It will appear in "
        f"the ledger once this admission is approved and folded in.\n\n"
        f"No change is needed and nothing has been lost. If the fact matters for "
        f"the summary a receiving clinician reads, it belongs in the paragraph "
        f"rather than the ledger, and you can edit that directly.")


def _s1b(card: Mapping[str, Any], ev: Mapping[str, Any]) -> tuple[str, str]:
    dropped = (ev.get("dropped") or [{}])[0]
    reason = dropped.get("reason")
    why = f" The gate recorded the reason as `{reason}`." if reason else ""
    return "accepted_taxonomy", (
        f"Accepted - you were right.\n\n"
        f"The fact was extracted from the notes correctly but was then removed by "
        f"the classification rules that decide what belongs in the ledger.{why} "
        f"That is a rule defect rather than a reading defect, and it is one of "
        f"the cheaper things to correct: the extracted facts do not need "
        f"re-processing.\n\n"
        f"The correction will ship in the next release. Until then, correct the "
        f"paragraph directly for any affected patient in front of you.")


def _s1c(card: Mapping[str, Any], ev: Mapping[str, Any]) -> tuple[str, str]:
    return "accepted_engineering", (
        "Accepted - you were right.\n\n"
        "The fact was extracted and held correctly, then absorbed into another "
        "entry when related facts were merged together, so it stopped being "
        "visible as a row of its own. The merge was too aggressive here.\n\n"
        "The correction will ship on the normal release cycle. Until then, "
        "correct the paragraph directly for any affected patient in front of you.")


def _s1d(card: Mapping[str, Any], ev: Mapping[str, Any]) -> tuple[str, str]:
    rows = ev.get("ledger_rows") or []
    shown = f"\n\nIt is listed as: \"{rows[0].get('text')}\"" if rows else ""
    return "by_design", (
        f"Thank you - this fact is present in the ledger, so nothing has been "
        f"lost from the record.{shown}\n\n"
        f"That it was hard to find is still worth knowing. If the wording or the "
        f"placement made it easy to read past, say so and we will treat it as a "
        f"display problem rather than closing it here.")


def _s1a(card: Mapping[str, Any], ev: Mapping[str, Any]) -> tuple[str, str]:
    return "accepted_prompt", (
        "Accepted - you were right, and this is a useful catch.\n\n"
        "The fact is in the source notes for an earlier admission but was never "
        "picked up when those notes were read, so it never entered the record at "
        "any stage. That is the most consequential kind of miss, because nothing "
        "downstream can recover a fact that was never extracted.\n\n"
        "Correcting it means changing how notes are read and re-processing the "
        "affected records, so it will not be immediate. Please correct the "
        "paragraph directly for any affected patient in front of you.")


_S1 = {"S1a": _s1a, "S1b": _s1b, "S1c": _s1c, "S1d": _s1d, "S1e": _s1e}


# ── probe verdicts that are not retention failures ─────────────────────────────

def _negated_only(card: Mapping[str, Any], ev: Mapping[str, Any]) -> tuple[str, str]:
    return "not_reproducible", (
        "We could not confirm this one, and the reason is worth flagging back to "
        "you.\n\nThe only mentions of it in this patient's notes are negative - "
        "the notes record it as absent or ruled out rather than present. The "
        "ledger reflects that, so it is not a retention failure.\n\n"
        "If you have documentation from outside these discharge summaries, that "
        "would not be visible to this system at all - it reads only the discharge "
        "summaries, not the EMR problem list or investigations.")


def _not_in_source(card: Mapping[str, Any], ev: Mapping[str, Any]) -> tuple[str, str]:
    return "not_reproducible", (
        "We could not find this in the record, so no change has been made.\n\n"
        "There is no mention of it in any of this patient's discharge summaries. "
        "The ledger is built only from those, so it cannot carry forward "
        "something they do not contain - this is not a case of the fact being "
        "found and then lost.\n\n"
        "This is not a judgement that you were mistaken. If you are working from "
        "the EMR problem list, clinic letters or investigations, none of those "
        "are visible to this system. If you can point to the discharge summary "
        "that documents it, please report it again and reference this one.")


_VERDICT = {"negated_only": _negated_only, "not_in_source": _not_in_source}


# ── artefact staleness ─────────────────────────────────────────────────────────

def _already_fixed(card: Mapping[str, Any], ev: Mapping[str, Any]) -> tuple[str, str]:
    return "already_fixed", (
        "This looks to have been fixed already.\n\n"
        "The record you reviewed was produced by an older version of the system, "
        "and the behaviour you reported has since been corrected. Regenerating "
        "this patient's record should clear it.\n\n"
        "Your report was still useful: it is how we confirm a fix actually "
        "landed on real records rather than only in testing.")


# ── entry point ────────────────────────────────────────────────────────────────

def suggest(card: Mapping[str, Any], evidence: Optional[Mapping[str, Any]]) -> Optional[dict]:
    """Propose a disposition and a draft reply, or None where evidence is thin.

    ORDER MATTERS. A resolved S1 sub-class is a stronger statement than artefact
    staleness: knowing the fact sits in the incoming admission explains the report
    completely, whereas "the record is a version behind" only means it MIGHT have
    changed. Answering with the weaker one would send a physician away with a
    guess dressed as a finding.

    Returns None whenever the evidence does not settle the question. That is the
    common case and it is the right default: a blank box asks the owner to think,
    while a plausible-looking draft on an ambiguous card invites a rubber stamp.
    """
    ev = evidence or {}
    if not ev:
        return None

    sub = ev.get("s1_subclass")
    if sub in _S1:
        disposition, text = _S1[sub](card, ev)
        return {"disposition": disposition, "reply_text": text,
                "basis": f"S1 sub-class {sub}",
                "confident": sub in ("S1b", "S1c", "S1d", "S1e")}

    verdict = (ev.get("missing_probe") or {}).get("verdict")
    if verdict in _VERDICT:
        disposition, text = _VERDICT[verdict](card, ev)
        return {"disposition": disposition, "reply_text": text,
                "basis": f"source-note probe: {verdict}", "confident": True}

    art = ev.get("artifact") or {}
    if art.get("regenerated_since_report") or art.get("artifact_behind_code"):
        disposition, text = _already_fixed(card, ev)
        return {"disposition": disposition, "reply_text": text,
                "basis": art.get("stale_versus") or "artefact regenerated since the report",
                # Not confident: staleness means the defect MAY be gone, not that
                # it is. The owner should verify before sending this one.
                "confident": False}

    return None
