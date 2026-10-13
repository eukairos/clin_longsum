# clin_longsum - physician feedback loop
# Copyright (C) 2026 Eukairos
# SPDX-License-Identifier: AGPL-3.0-only

"""
adapters/email_templates.py
===========================
Physician-facing correspondence. Wording is a governance artifact, not an
implementation detail, so it lives in one readable place and is reviewed like the
policy file.

DIVISION OF LABOUR WITH replies.py.
`reply_text` is the ANSWER, written or accepted by the owner. This module is the
ENVELOPE: subject line, identifying header, and a short disposition-specific
footer saying what happens next.

DEPLOYMENT ASSUMPTION: an air-gapped intranet corporate mail system. That is what
permits clinical text in the body at all. Two safeguards hold regardless:

  1. PATIENTS ARE REFERENCED BY SYNTHETIC ALIAS (PT-nnnn), never subject_id.
  2. Every message carries a distribution banner, top and bottom.

WHY EVERY DISPOSITION HAS ITS OWN FOOTER, INCLUDING THE UNWELCOME ONES.
"No change" and "could not reproduce" are the replies most likely to be rushed,
and they are exactly the ones that decide whether a physician reports a second
time. Each gets a footer that says what to do next, so no one has to compose that
part under time pressure.

TONE. Read by a busy clinician between patients. The reply carries the substance;
the footer says what to do about THIS patient now. Never explain the pipeline.
"""

from __future__ import annotations

from typing import Any, Mapping

BANNER = ("This email contains patient information. Internal distribution only. "
          "Do not forward outside the network.")

# Prefixed so a mailbox rule can file them, and carrying the alias so a physician
# can find the right one without opening it.
SUBJECT_PREFIX = "[Ledger feedback]"

_RULE = "-" * 68


def _fmt(card: Mapping[str, Any]) -> dict:
    """Flatten a card into template variables, with safe fallbacks throughout.

    A missing field must degrade the sentence, never break the send: a reply that
    fails to go out is worse than one that reads slightly thin.
    """
    return {
        "id": card.get("feedback_id"),
        "alias": card.get("alias") or "this patient",
        "fact": (card.get("fact_text") or "the row you reported").strip(),
        "symptom_label": card.get("symptom_label") or "a problem",
        "tier": card.get("salience_tier") or "",
        "reply": (card.get("reply_text") or "").strip(),
        "fix_version": card.get("fix_version") or "",
        "owners": ", ".join(card.get("owner_roles") or []) or "triage",
        "urgency": card.get("urgency") or "routine",
        "n_affected": card.get("n_affected"),
        "duplicate_of": card.get("duplicate_of"),
    }


def _header(v: dict) -> str:
    """Identifying block. Same shape on every message, so it can be skimmed past."""
    tier = f" [{v['tier']}]" if v["tier"] else ""
    return (f"  Report #{v['id']}\n"
            f"  Patient: {v['alias']}\n"
            f"  Row:     {v['fact']}{tier}")


def _scope(v: dict) -> str:
    """How many other patients carry the same defect.

    The single most effective sentence the loop can send. It converts reporting
    from a chore into something with visible leverage - one physician's two
    minutes fixed the record for thirty other people.

    Returns a bare sentence; the caller owns the spacing. (It used to lead with
    its own newlines, which produced triple blank lines whenever it was joined
    to another block.)
    """
    n = v.get("n_affected")
    if not n:
        return ""
    return (f"The same defect was found on {n} other patient"
            f"{'s' if n != 1 else ''}, and will be corrected in the same release.")


# ── acknowledgement (sent on receipt, before anyone has looked) ────────────────

def acknowledge(card: Mapping[str, Any]) -> tuple[str, str]:
    v = _fmt(card)
    urgent = ("\nThis has been flagged as urgent and will be looked at ahead of the "
              "routine queue.\n" if v["urgency"] == "urgent" else "")
    return (
        f"{SUBJECT_PREFIX} #{v['id']} received - {v['alias']}",
        f"""Your report has been received and routed to: {v['owners']}.

{_header(v)}
  Issue:   {v['symptom_label']}
{urgent}
You do not need to do anything further. You will be emailed when it is
answered, including if the answer turns out to be that the system is working
as designed.

If this matters for {v['alias']} now, correct the paragraph as well as
reporting the row: the report fixes it for future patients, the paragraph
fixes it for this one.""")


# ── dispositions ──────────────────────────────────────────────────────────────
# Each supplies a subject and a footer only. The reply is inserted verbatim by
# _compose(); no template restates the verdict.

def _join(*parts: str) -> str:
    """Join non-empty blocks with one blank line. Spacing lives here, not in the
    fragments, so an absent optional block leaves no hole.

    Strips newlines only, never leading spaces - the header block is indented and
    a plain .strip() would flatten it against the margin.
    """
    return "\n\n".join(p.strip("\n") for p in parts if p and p.strip())


def _accepted_footer(v: dict, timeline: str) -> str:
    return _join(
        _scope(v), timeline,
        "Until then, correct the paragraph directly for any affected patient in "
        "front of you - that is what the next clinician reads.")


_DISPOSITIONS = {
    "accepted_taxonomy": (
        "accepted - fix scheduled",
        lambda v: _accepted_footer(
            v, f"The fix will ship {('in ' + v['fix_version']) if v['fix_version'] else 'in an upcoming release'}. "
               f"Corrections of this kind do not require the records to be re-read, "
               f"so it is one of the quicker ones."),
    ),
    "accepted_engineering": (
        "accepted - fix scheduled",
        lambda v: _accepted_footer(
            v, f"The correction will ship {('in ' + v['fix_version']) if v['fix_version'] else ''} "
               f"on the normal release cycle."),
    ),
    # Distinct from accepted_engineering, deliberately. This is the only class
    # that requires re-reading every record, so the honest timeline is different
    # and the physician should not be told "the next release" when it is not.
    "accepted_prompt": (
        "accepted - fix scheduled, but not quickly",
        lambda v: _accepted_footer(
            v, "Correcting this means changing how the notes are read and then "
               "re-processing the affected records, so it will take longer than a "
               "usual fix and will not appear in the next release."),
    ),
    "by_design": (
        "no change - working as designed",
        lambda v: _join(
            # The drafted replies already open by thanking the reporter, so
            # thanking again here read as a form letter stapled to a human one.
            # Only added when the answer did not already do it - which is the
            # case when an owner typed a bare sentence into the box.
            "" if "thank" in v["reply"].lower() else
            "Thank you for raising it even though it turned out not to be a fault. "
            "If the record read wrongly to you it will read wrongly to someone "
            "else, so reports like this are how the interface and the guide get "
            "better.",
            f"If the behaviour matters clinically for {v['alias']}, pin the row and "
            "edit the paragraph. If you think the design itself is wrong, say so "
            "and this will be reopened as a design question rather than a defect."),
    ),
    "not_reproducible": (
        "could not reproduce",
        lambda v: (
            "This is not a judgement that you were mistaken. It usually means the "
            "record has changed since you looked, or that something about the case "
            "is not visible from the discharge summaries - the only thing this "
            "system reads.\n\n"
            f"If you see it again, please report it again and mention #{v['id']}."),
    ),
    "already_fixed": (
        "already fixed",
        lambda v: (
            f"{v['alias']}'s record has since been regenerated and no longer shows "
            "this.\n\nYour report was still useful: it is how we confirm a fix "
            "actually landed on real records rather than only in testing."),
    ),
    "duplicate": (
        "same as an existing report",
        lambda v: (
            f"This is the same defect as report #{v['duplicate_of']}, which is "
            "already open.\n\nReporting it again was not wasted. Seeing the same "
            "defect on more than one patient is how we tell an isolated slip from "
            "a systematic one, and it raises the priority of the original."),
    ),
}


def _compose(disposition: str, card: Mapping[str, Any]) -> tuple[str, str]:
    v = _fmt(card)
    subject_tail, footer_fn = _DISPOSITIONS[disposition]
    return (f"{SUBJECT_PREFIX} #{v['id']} {subject_tail}",
            _join(_header(v), v["reply"], footer_fn(v)))


TEMPLATES = {"acknowledge": acknowledge}
TEMPLATES.update({
    name: (lambda card, _n=name: _compose(_n, card)) for name in _DISPOSITIONS
})


def render(template: str, card: Mapping[str, Any]) -> tuple[str, str]:
    """Render one message, with the banner wrapped around the body.

    An unknown template falls back to the header plus the written reply rather
    than raising. The reply is the part that matters and it is already on the
    card; refusing to send because the wording is not catalogued would break the
    closed loop for the sake of presentation.
    """
    fn = TEMPLATES.get(template)
    if fn:
        subject, body = fn(card)
    else:
        v = _fmt(card)
        subject = f"{SUBJECT_PREFIX} #{v['id']} - {template}"
        body = _join(_header(v), v["reply"])
    return subject, f"{BANNER}\n\n{_RULE}\n\n{body}\n\n{_RULE}\n{BANNER}\n"
