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
board_routes.py
===============
The triage board: a read view over the support database plus the actions that
move a card.

    GET  /board                       the board itself
    GET  /board/api/cards             lanes, filtered
    GET  /board/api/cards/{id}        one card with events and evidence
    POST /board/api/cards/{id}/action every state change
    GET  /board/api/stats             counts and loop health

THE BOARD IS A PROJECTION, NEVER THE RECORD. Every action here calls
support.service, which owns the state machine and the audit trail. This module
holds no policy: it cannot close a card without a reply, cannot skip the
dual-owner acknowledgement, cannot mark something urgent without a rule - not
because it declines to, but because it has no path to. That is what makes
replacing this board with a workflow product a contained change.

ONE ACTION ENDPOINT, NOT SIX. Every transition is the same shape - who, what,
why - and routing them through one handler keeps the audit trail uniform and
leaves exactly one place where an action can be added or refused.

ACTOR IDENTITY IS REQUIRED FOR EVERY ACTION. The service enforces it; the board
surfaces it as a picker. A decision without a named person is not a decision, it
is a change of state with no one accountable for it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/board", tags=["board"])
BOARD_HTML = Path(__file__).resolve().parent / "board.html"

# Lane order is the order a card travels, so the board reads left to right as a
# lifecycle rather than an arbitrary grouping.
LANES = ["new", "triaged", "contained", "in_progress", "resolved", "closed"]

LIST_COLS = ["feedback_id", "symptom", "symptom_label", "fact_text", "urgency",
             "urgency_rule_id", "state", "disposition", "owner_roles", "s1_subclass",
             "defect_class", "alias", "subject_id", "cohort", "reporter_name_raw",
             "created_at", "closed_at", "acknowledged_by", "dedup_key",
             "salience_tier", "n_same"]

_LIST_SQL = """
SELECT f.feedback_id, f.symptom, sc.physician_label, f.fact_text, f.urgency,
       f.urgency_rule_id, f.state, f.disposition, f.owner_roles, f.s1_subclass,
       f.defect_class, sa.display_alias, f.subject_id, f.cohort,
       f.reporter_name_raw, f.created_at, f.closed_at, f.acknowledged_by,
       f.dedup_key, f.salience_tier,
       (SELECT count(DISTINCT g.subject_id) FROM feedback g
         WHERE g.dedup_key = f.dedup_key) AS n_same
  FROM feedback f
  LEFT JOIN subject_alias   sa ON sa.cohort = f.cohort AND sa.subject_id = f.subject_id
  LEFT JOIN symptom_catalog sc ON sc.symptom_id = f.symptom
 WHERE (%(state)s   IS NULL OR f.state = %(state)s)
   AND (%(urgency)s IS NULL OR f.urgency = %(urgency)s)
   AND (%(owner)s   IS NULL OR %(owner)s = ANY(f.owner_roles))
 ORDER BY (f.urgency = 'urgent') DESC, f.created_at DESC
 LIMIT %(limit)s;
"""


def _svc():
    from support import service
    return service


def _conn():
    try:
        return _svc().connect()
    except Exception as e:
        logger.warning("board: support database unavailable (%s)", e)
        raise HTTPException(status_code=503,
                            detail="The support database is unreachable.")


class Action(BaseModel):
    action: str
    actor_id: int
    note: Optional[str] = None
    # close
    disposition: Optional[str] = None
    reply_text: Optional[str] = None
    fix_version: Optional[str] = None
    duplicate_of: Optional[int] = None
    # transition / reassign / urgency
    to_state: Optional[str] = None
    owner_roles: Optional[list[str]] = None
    urgency: Optional[str] = None
    rule_id: Optional[str] = None


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def board_page():
    if not BOARD_HTML.exists():
        raise HTTPException(status_code=500, detail=f"{BOARD_HTML.name} not found")
    return HTMLResponse(BOARD_HTML.read_text(encoding="utf-8"))


@router.get("/api/cards")
def list_cards(state: Optional[str] = None, urgency: Optional[str] = None,
               owner: Optional[str] = None, limit: int = 400):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(_LIST_SQL, {"state": state, "urgency": urgency,
                                "owner": owner, "limit": limit})
        cards = [dict(zip(LIST_COLS, r)) for r in cur.fetchall()]
        lanes = {ln: [c for c in cards if c["state"] == ln] for ln in LANES}
        return {"lanes": LANES, "cards": cards, "counts": {k: len(v) for k, v in lanes.items()}}
    finally:
        conn.close()


@router.get("/api/cards/{feedback_id}")
def card_detail(feedback_id: int):
    """One card, its full audit trail, and the collector's findings.

    The event list is returned in full rather than summarised. An owner deciding
    a disposition needs to see that a rule fired, that evidence arrived and
    changed the routing, and who acknowledged - a condensed view would hide
    exactly the steps that justify the decision.
    """
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(_LIST_SQL.replace(
            "WHERE (%(state)s   IS NULL OR f.state = %(state)s)\n   AND (%(urgency)s IS NULL OR f.urgency = %(urgency)s)\n   AND (%(owner)s   IS NULL OR %(owner)s = ANY(f.owner_roles))",
            "WHERE f.feedback_id = %(fid)s"),
            {"fid": feedback_id, "limit": 1})
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"no such report: {feedback_id}")
        card = dict(zip(LIST_COLS, row))

        cur.execute("""SELECT report_text, disputed_field, clinical_action_flag,
                              seen_other_patients, taxonomy_version, prompt_version,
                              artifact_generated_at, anchor_kind, fact_ids,
                              source_hadm_ids, retention_class, certainty,
                              current_lifecycle, negative_kind, policy_version,
                              reply_text, fix_version, duplicate_of, namespace
                         FROM feedback WHERE feedback_id=%s;""", (feedback_id,))
        extra = ["report_text", "disputed_field", "clinical_action_flag",
                 "seen_other_patients", "taxonomy_version", "prompt_version",
                 "artifact_generated_at", "anchor_kind", "fact_ids", "source_hadm_ids",
                 "retention_class", "certainty", "current_lifecycle", "negative_kind",
                 "policy_version", "reply_text", "fix_version", "duplicate_of",
                 "namespace"]
        card.update(dict(zip(extra, cur.fetchone())))

        cur.execute("""SELECT e.event_type, e.from_state, e.to_state, e.actor_kind,
                              e.rule_id, e.note, e.created_at, p.display_name
                         FROM feedback_events e
                         LEFT JOIN physicians p ON p.physician_id = e.actor_id
                        WHERE e.feedback_id=%s ORDER BY e.event_id;""", (feedback_id,))
        ecols = ["event_type", "from_state", "to_state", "actor_kind", "rule_id",
                 "note", "created_at", "actor"]
        events = [dict(zip(ecols, r)) for r in cur.fetchall()]

        cur.execute("""SELECT collector_version, evidence, collected_at
                         FROM feedback_evidence WHERE feedback_id=%s
                        ORDER BY collected_at DESC;""", (feedback_id,))
        evid = [{"collector_version": r[0], "evidence": r[1], "collected_at": r[2]}
                for r in cur.fetchall()]

        cur.execute("""SELECT channel, state, payload->>'template', last_error, sent_at
                         FROM outbox WHERE feedback_id=%s ORDER BY outbox_id;""",
                    (feedback_id,))
        outbox = [{"channel": r[0], "state": r[1], "template": r[2],
                   "detail": r[3], "sent_at": r[4]} for r in cur.fetchall()]

        # Same defect signature on other patients. This is the number that turns
        # an isolated complaint into a scoped piece of work, and it is the first
        # thing an owner should see when deciding how much a fix is worth.
        cur.execute("""SELECT feedback_id, subject_id, state FROM feedback
                        WHERE dedup_key=%s AND feedback_id<>%s ORDER BY feedback_id;""",
                    (card.get("dedup_key"), feedback_id))
        related = [{"feedback_id": r[0], "subject_id": r[1], "state": r[2]}
                   for r in cur.fetchall()]

        # A pre-written disposition and reply where the evidence settles the
        # question. Returned as a suggestion the owner edits, never applied:
        # the closed loop depends on a written answer being cheap to produce,
        # and a blank box on every card is how a support team stops closing them.
        suggestion = None
        try:
            from support import replies
            suggestion = replies.suggest(card, evid[0]["evidence"] if evid else None)
        except Exception as e:
            logger.warning("board: reply suggestion failed for #%s (%s)", feedback_id, e)

        return {"card": card, "events": events, "evidence": evid,
                "outbox": outbox, "related": related, "suggestion": suggestion}
    finally:
        conn.close()


@router.post("/api/cards/{feedback_id}/action")
def card_action(feedback_id: int, req: Action):
    """Every state change. Delegates to the service, which owns the rules."""
    svc = _svc()
    conn = _conn()
    try:
        a = req.action
        if a == "transition":
            if not req.to_state:
                raise HTTPException(status_code=400, detail="to_state is required")
            svc.transition(conn, feedback_id, req.to_state,
                           actor_id=req.actor_id, note=req.note)
        elif a == "acknowledge":
            svc.acknowledge(conn, feedback_id, req.actor_id, note=req.note)
        elif a == "reassign":
            svc.reassign(conn, feedback_id, req.owner_roles or [],
                         actor_id=req.actor_id, reason=req.note or "")
        elif a == "urgency":
            svc.set_urgency(conn, feedback_id, req.urgency or "routine",
                            actor_id=req.actor_id, reason=req.note or "",
                            rule_id=req.rule_id)
        elif a == "close":
            svc.close(conn, feedback_id, disposition=req.disposition or "",
                      reply_text=req.reply_text or "", actor_id=req.actor_id,
                      fix_version=req.fix_version, duplicate_of=req.duplicate_of)
        elif a == "recollect":
            # Re-run the collector. Evidence is appended, never overwritten, so a
            # card keeps the finding it was originally routed on alongside the
            # new one - which is what lets you tell "we were wrong then" from
            # "the record changed since".
            from support import evidence
            evidence.collect_and_attach(conn, feedback_id, retriage=True)
        else:
            raise HTTPException(status_code=400, detail=f"unknown action '{a}'")
        return {"ok": True}
    except svc.ServiceError as e:
        # Refusals are the interesting case: they are the governance rules
        # speaking, and the message is written to be shown to the person.
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("board: action %s failed on #%s", req.action, feedback_id)
        raise HTTPException(status_code=500,
                            detail=f"Action failed ({type(e).__name__}). See server log.")
    finally:
        conn.close()


@router.get("/api/stats")
def board_stats():
    conn = _conn()
    try:
        svc = _svc()
        cur = conn.cursor()
        cur.execute("SELECT state, count(*) FROM feedback GROUP BY state;")
        by_state = {r[0]: r[1] for r in cur.fetchall()}
        cur.execute("""SELECT count(*) FROM feedback
                        WHERE urgency='urgent' AND state<>'closed';""")
        urgent_open = cur.fetchone()[0]
        cur.execute("""SELECT disposition, count(*) FROM feedback
                        WHERE disposition IS NOT NULL GROUP BY disposition;""")
        by_disposition = {r[0]: r[1] for r in cur.fetchall()}
        cur.execute("SELECT policy_version, count(*) FROM symptom_catalog GROUP BY 1;")
        pol = cur.fetchone()
        return {"by_state": by_state, "urgent_open": urgent_open,
                "by_disposition": by_disposition,
                "policy_version": pol[0] if pol else None,
                "gaps": svc.loop_gaps(conn), "stuck_outbox": svc.outbox_gaps(conn)}
    finally:
        conn.close()


@router.get("/api/metrics")
def board_metrics(window: int = 90):
    """Continuous-improvement metrics.

    Separate from /api/stats, which is the header's cheap poll. This one runs
    percentiles and aggregate scans, so it is fetched on demand rather than every
    thirty seconds.
    """
    conn = _conn()
    try:
        from support import metrics
        return metrics.collect(conn, window_days=window)
    except ImportError as e:
        raise HTTPException(status_code=503, detail=f"metrics unavailable: {e}")
    finally:
        conn.close()


@router.get("/api/policy")
def board_policy():
    """Dispositions and legal transitions, so the board cannot offer an illegal one."""
    try:
        from support import policy_loader as pol
        p = pol.load()
        return {"version": p.version,
                "dispositions": [{"id": d, "owner_role": v.get("owner_role"),
                                  "notes": v.get("notes")}
                                 for d, v in p.dispositions.items()],
                "states": {k: list(v) for k, v in p.states.items()}}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"policy unavailable: {e}")
