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
feedback_routes.py
==================
Capture endpoints for the physician feedback loop, mounted on the tiered review
API as a separate APIRouter.

    GET  /tiered/feedback/health      is the loop available, and is it moving
    GET  /tiered/feedback/symptoms    picker contents (projection of the policy)
    POST /tiered/feedback/report      file one report
    GET  /tiered/feedback/mine        a physician's own reports and their replies

SEPARATE MODULE, DELIBERATELY. longsum_api.py is already 1100+ lines and serves
the clinical review surface; the support loop is a different concern against a
different database and is meant to be removable. Mounting it as a router keeps
the seam visible.

WHAT THE SERVER CAPTURES, NOT THE PHYSICIAN
-------------------------------------------
The client sends only what the physician actually knows: which row, what is
wrong, and the two factual checkboxes. Everything needed to disposition the card
later - cohort, namespace, taxonomy_version, artifact generated_at, the row's
axes - is read server-side from the database. Two reasons, and the second is the
important one:

  1. a client cannot be trusted to report the version of the artifact it is
     displaying;
  2. a large share of reports turn out to be against artifacts that predate a
     fix, and without the version stamp those cards cannot be dispositioned at
     all - they can only be re-investigated.

DEGRADES, DOES NOT BREAK. Every endpoint connects lazily and returns 503 with a
readable message if the support database is absent. The clinical review UI must
keep working on a machine where the feedback loop was never bootstrapped.

NEVER BLOCKS THE PHYSICIAN. Triage is deterministic and runs inline (microseconds).
Evidence collection queries the cohort and runs in a background task, after the
response. The report is already routed and acknowledged either way; evidence only
sharpens the S1 sub-class.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tiered/feedback", tags=["feedback"])

# Anchors the UI can file against. 'ledger_row' carries stable fact_ids;
# 'retention_flag' carries only (fact_text, source_hadm_id) and the collector
# reconstructs the id from them.
ANCHOR_KINDS = ("ledger_row", "retention_flag", "concern", "prose", "other")


def _svc():
    """Import the domain core lazily so an un-bootstrapped install still boots."""
    from support import service
    return service


def _support_conn():
    try:
        return _svc().connect()
    except Exception as e:
        logger.warning("feedback: support database unavailable (%s)", e)
        raise HTTPException(
            status_code=503,
            detail="The feedback loop is not available on this server (support "
                   "database unreachable). Reports cannot be filed right now.")


def _cohort_fallback() -> str:
    """Last-resort cohort name. See _clinical_context() for why this is a fallback."""
    return os.getenv("PGDATABASE") or "unknown"


def _namespace() -> Optional[str]:
    try:
        import config
        return config.RUN_NAMESPACE or None
    except Exception:
        return None


def _resolve_reporter(cur, physician_name: str) -> Optional[int]:
    """Match a typed name to the staff directory.

    NO AUTO-CREATE. An unmatched name still files a report - refusing would lose
    the signal, which is the worst possible trade - but it is stored as
    reporter_name_raw with a null reporter_id, and the reply cannot be emailed.
    The loop-health check surfaces those so they are chased rather than lost.
    """
    if not physician_name:
        return None
    cur.execute("""SELECT physician_id FROM physicians
                    WHERE active AND lower(display_name) = lower(%s)
                    ORDER BY physician_id LIMIT 1;""", (physician_name.strip(),))
    row = cur.fetchone()
    return row[0] if row else None


def _clinical_context(subject_id: int) -> dict:
    """Cohort, taxonomy version and artifact timestamp for the record on screen.

    COHORT COMES FROM current_database(), NOT FROM THE ENVIRONMENT. os.getenv
    ('PGDATABASE') looks equivalent but is not: longsum_api calls load_dotenv()
    at import, so a shell that omits the override silently resolves to whatever
    .env names. A card stamped with the wrong cohort sends the evidence collector
    to the wrong database, and the error is invisible - the queries succeed, they
    just answer about a different patient population. Asking the live connection
    what it is connected to cannot drift.

    Everything here degrades rather than blocks: a missing snapshot weakens the
    card, but refusing the report would lose the physician's observation, which
    is the more expensive loss.
    """
    out = {"cohort": _cohort_fallback(), "taxonomy_version": None,
           "artifact_generated_at": None}
    try:
        from database.pg import get_conn
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT current_database();")
            out["cohort"] = cur.fetchone()[0]
            cur.execute("""SELECT taxonomy_version, generated_at FROM tiered_view
                            WHERE subject_id = %s;""", (subject_id,))
            row = cur.fetchone()
            if row:
                out["taxonomy_version"] = row[0]
                out["artifact_generated_at"] = row[1]
        finally:
            conn.close()
    except Exception as e:
        logger.warning("feedback: clinical context failed for %s (%s)", subject_id, e)
    return out


# ── models ─────────────────────────────────────────────────────────────────────

class FeedbackReport(BaseModel):
    physician_name: str
    subject_id: int
    symptom: str
    report_text: str
    anchor_kind: str = "ledger_row"

    # The two questions of FACT. Not a severity dial: physicians asked to rate
    # urgency rate everything urgent, whereas these are answerable yes or no and
    # feed U-6 and corroborate U-5.
    clinical_action_flag: bool = False
    seen_other_patients: bool = False

    # Required for S3 so U-3 can distinguish a disputed lifecycle on a T1 row
    # (safety inversion) from a disputed tier on the same row (routine).
    disputed_field: Optional[str] = None

    # Row identity, sent by the UI straight from the rendered row.
    fact_ids: list[str] = []
    source_hadm_ids: list[int] = []
    anchor_key: Optional[str] = None
    fact_text: Optional[str] = None
    salience_tier: Optional[str] = None
    retention_class: Optional[str] = None
    certainty: Optional[str] = None
    current_lifecycle: Optional[str] = None
    negative_kind: Optional[str] = None


# ── endpoints ──────────────────────────────────────────────────────────────────

@router.get("/health")
def feedback_health():
    """Availability plus loop health.

    `gaps` is the guard against the failure section 8 cannot survive: a loop that
    quietly stops answering. Physicians do not file a second report after an
    unanswered first, so a dead loop looks exactly like success.
    """
    try:
        conn = _svc().connect()
    except Exception as e:
        return {"available": False, "reason": str(e)}
    try:
        cur = conn.cursor()
        cur.execute("""SELECT state, count(*) FROM feedback GROUP BY state;""")
        by_state = {r[0]: r[1] for r in cur.fetchall()}
        gaps = _svc().loop_gaps(conn)
        stuck = _svc().outbox_gaps(conn)
        cur.execute("SELECT policy_version, count(*) FROM symptom_catalog GROUP BY 1;")
        pol = cur.fetchone()
        return {"available": True, "by_state": by_state,
                "policy_version": pol[0] if pol else None,
                "n_gaps": len(gaps), "gaps": gaps[:20],
                "n_stuck_outbox": len(stuck)}
    finally:
        conn.close()


@router.get("/symptoms")
def feedback_symptoms():
    """Picker contents, in policy order.

    Served from symptom_catalog - a projection of the policy file seeded at
    bootstrap - so the UI cannot drift from the governed list, and retiring a
    symptom removes it from the picker on the next policy load.
    """
    conn = _support_conn()
    try:
        cur = conn.cursor()
        cur.execute("""SELECT symptom_id, physician_label, owner_roles, policy_version
                         FROM symptom_catalog ORDER BY sort_order;""")
        rows = cur.fetchall()
        if not rows:
            raise HTTPException(
                status_code=503,
                detail="No symptoms are loaded. Run bootstrap_support_db.py to "
                       "project the triage policy.")
        return {"policy_version": rows[0][3],
                "symptoms": [{"id": r[0], "label": r[1], "owner_roles": r[2]}
                             for r in rows]}
    finally:
        conn.close()


@router.get("/physicians")
def feedback_physicians():
    """Active staff roster for the identity picker.

    A typed name cannot be matched reliably - one typo and the reply has nowhere
    to go and the cross-patient repeat counter (U-5) splits one defect into two.
    A picker fixes both at the cost of a lookup.

    In a real deployment this is the hospital EMR/HR directory, which already
    holds registration number, subspecialty and credentials. `directory_ref` on
    the physicians table is the seam for that; none of those fields are read by
    this loop, so the mock stays minimal.

    The clinical review UI shares this control, so this endpoint must never be
    load-bearing for it: the client falls back to a free-text box if the roster
    cannot be fetched. Sign-off must not become unavailable because a support
    database is down.
    """
    conn = _support_conn()
    try:
        cur = conn.cursor()
        cur.execute("""SELECT physician_id, display_name, role
                         FROM physicians WHERE active
                        ORDER BY role, display_name;""")
        return {"physicians": [{"id": r[0], "name": r[1], "role": r[2]}
                               for r in cur.fetchall()]}
    finally:
        conn.close()


@router.post("/report")
def file_report(req: FeedbackReport, background_tasks: BackgroundTasks):
    """File one physician report. Triage runs inline; evidence follows in background."""
    if req.anchor_kind not in ANCHOR_KINDS:
        raise HTTPException(status_code=400,
                            detail=f"anchor_kind must be one of {list(ANCHOR_KINDS)}")
    if not req.physician_name.strip():
        raise HTTPException(status_code=400, detail="physician_name is required")
    if not req.report_text.strip():
        raise HTTPException(
            status_code=400,
            detail="Please describe what is wrong with this row. The free text is "
                   "what the owner reads first.")
    # A missing-fact report has no row, so nothing else identifies what is being
    # reported. Without the text the card is unactionable and the collector has
    # nothing to search for.
    if req.anchor_kind == "other" and not (req.fact_text or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Please say what should be in the record but isn't.")
    # S3 without a disputed field cannot fire U-3, so a safety lifecycle inversion
    # would silently be triaged routine. Refuse at the door rather than mis-triage.
    if req.symptom == "S3" and not req.disputed_field:
        raise HTTPException(
            status_code=400,
            detail="Please say which field is wrong (tier, class, certainty, status, "
                   "date, or the text itself).")

    svc = _svc()
    ctx = _clinical_context(req.subject_id)
    conn = _support_conn()
    try:
        cur = conn.cursor()
        reporter_id = _resolve_reporter(cur, req.physician_name)
        try:
            fid = svc.create_report(
                conn,
                reporter_id=reporter_id,
                reporter_name_raw=req.physician_name.strip(),
                symptom=req.symptom,
                report_text=req.report_text.strip(),
                disputed_field=req.disputed_field,
                clinical_action_flag=req.clinical_action_flag,
                seen_other_patients=req.seen_other_patients,
                cohort=ctx["cohort"],
                namespace=_namespace(),
                subject_id=req.subject_id,
                anchor_kind=req.anchor_kind,
                anchor_key=req.anchor_key,
                fact_ids=req.fact_ids or None,
                source_hadm_ids=req.source_hadm_ids or None,
                fact_text=req.fact_text,
                salience_tier=req.salience_tier,
                retention_class=req.retention_class,
                certainty=req.certainty,
                current_lifecycle=req.current_lifecycle,
                negative_kind=req.negative_kind,
                taxonomy_version=ctx["taxonomy_version"],
                artifact_generated_at=ctx["artifact_generated_at"],
            )
        except svc.ServiceError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            # An unexpected failure here is almost always schema drift or a
            # database fault. The physician must not be shown a bare 500: they
            # have just spent effort describing a defect, and an opaque error
            # teaches them the loop does not work. Say plainly that the report
            # did not save, and log the detail for whoever has to fix it.
            logger.exception("feedback: filing report failed")
            raise HTTPException(
                status_code=500,
                detail=f"The report could not be saved ({type(e).__name__}). "
                       f"Nothing was recorded - please tell the team directly, "
                       f"and check the server log.")

        cur.execute("""SELECT urgency, urgency_rule_id, owner_roles, s1_subclass
                         FROM feedback WHERE feedback_id=%s;""", (fid,))
        urgency, rule_id, owners, sub = cur.fetchone()
    finally:
        conn.close()

    background_tasks.add_task(_collect_evidence, fid)

    return {
        "feedback_id": fid,
        "urgency": urgency,
        "urgency_rule_id": rule_id,
        "owner_roles": owners,
        "s1_subclass": sub,
        "reporter_matched": reporter_id is not None,
        # Shown verbatim to the physician. It states what happens next, which is
        # the whole point of asking them to report in the first place.
        "message": (
            f"Report #{fid} received and routed to: {', '.join(owners or []) or 'triage'}."
            + (" Flagged as urgent." if urgency == "urgent" else "")
            + (" You will be emailed when it is answered."
               if reporter_id else
               " Your name was not found in the staff directory, so the reply will"
               " not be emailed - it will be recorded against the report.")),
    }


def _collect_evidence(feedback_id: int) -> None:
    """Background evidence collection. Best-effort by design.

    Follows the _publish() idiom in c5_review_queue.py: failure is logged, never
    raised. The card is already filed, triaged and acknowledged; missing evidence
    leaves the S1 sub-class unresolved and dual-owned, which is the correct
    fallback rather than an error the physician has to see.
    """
    try:
        from support import evidence
        conn = _svc().connect()
        try:
            evidence.collect_and_attach(conn, feedback_id, retriage=True)
        finally:
            conn.close()
    except Exception as e:
        logger.warning("feedback: evidence collection failed for #%s (%s)",
                       feedback_id, e)


@router.get("/mine")
def my_reports(physician_name: str, limit: int = 50):
    """A physician's own reports and the answers they were given.

    The in-UI half of the closed loop. Email is the nudge; this is the record,
    and it is what a physician checks when they want to know whether reporting
    changed anything.
    """
    conn = _support_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT f.feedback_id, f.symptom, f.fact_text, f.urgency, f.state,
                   f.disposition, f.reply_text, f.fix_version, f.created_at,
                   f.closed_at, sa.display_alias
              FROM feedback f
              LEFT JOIN subject_alias sa
                     ON sa.cohort = f.cohort AND sa.subject_id = f.subject_id
             WHERE lower(f.reporter_name_raw) = lower(%s)
             ORDER BY f.created_at DESC
             LIMIT %s;""", (physician_name.strip(), limit))
        cols = ["feedback_id", "symptom", "fact_text", "urgency", "state",
                "disposition", "reply_text", "fix_version", "created_at",
                "closed_at", "alias"]
        return {"reports": [dict(zip(cols, r)) for r in cur.fetchall()]}
    finally:
        conn.close()
