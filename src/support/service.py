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
service.py
==========
Domain core for the physician feedback loop. Every state transition passes
through here.

THIS IS THE LAYER THAT SURVIVES A TOOL SWAP. The board - whether the built-in
view or a workflow product adopted later - is a PROJECTION of these tables, never
the record. So this module holds no HTTP, no board vocabulary, no lane names, no
email formatting. It knows only the canonical `state` / `disposition` /
`urgency` / `owner_roles` vocabulary defined in the policy file. An adapter maps
that outward; nothing maps inward.

TRANSACTIONS. Each public operation is one transaction and commits on success.
That follows the project's governance rule that physician decisions are persisted
transactionally with an append-only audit trail: the card update and its
feedback_events row commit together or not at all, so no decision can exist
without its provenance.

SIDE EFFECTS GO TO THE OUTBOX, NEVER INLINE. A report must not fail because a
mail host or board is down, and a re-drain must not double-send. Every outbound
effect is an `outbox` row with an idempotency key, drained separately.

WHAT THIS MODULE REFUSES TO DO
------------------------------
  - close a card without a disposition and a written reply (also a DB CHECK, so
    the rule holds even if a future caller bypasses this module);
  - close a dual-owned card without the second owner's acknowledgement;
  - mark a card urgent without naming the rule that fired;
  - downgrade urgency without a named human and a reason.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Mapping, Optional, Sequence

try:
    from support import policy_loader as pol_mod
    from support import triage as triage_mod
except ImportError:  # pragma: no cover - direct execution from inside support/
    import policy_loader as pol_mod
    import triage as triage_mod

logger = logging.getLogger(__name__)

COLLECTOR_UNSET = "evidence.none"


class ServiceError(RuntimeError):
    """A refused operation. Callers surface the message; it is written for humans."""


# ── connection ─────────────────────────────────────────────────────────────────

def connect():
    """Open a support-database connection.

    Prefers database.pg.get_support_conn() so application code has one import
    site; falls back to the bootstrap helper if that patch is not yet applied.
    Either way this NEVER resolves PGDATABASE - support state must not be able
    to land in a clinical cohort by way of a forgotten shell variable.
    """
    try:
        from database.pg import get_support_conn
        return get_support_conn()
    except (ImportError, AttributeError):
        try:
            from support.bootstrap_support_db import get_support_conn
        except ImportError:
            from bootstrap_support_db import get_support_conn
        return get_support_conn()


# ── helpers ────────────────────────────────────────────────────────────────────

def _log_event(cur, feedback_id: int, event_type: str, *, from_state=None,
               to_state=None, actor_kind="system", actor_id=None, rule_id=None,
               note=None, detail=None) -> None:
    cur.execute(
        """INSERT INTO feedback_events
             (feedback_id, event_type, from_state, to_state, actor_kind,
              actor_id, rule_id, note, detail)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s);""",
        (feedback_id, event_type, from_state, to_state, actor_kind, actor_id,
         rule_id, note, json.dumps(detail) if detail is not None else None))


def enqueue(cur, channel: str, feedback_id: Optional[int], payload: Mapping[str, Any],
            *, key_parts: Sequence[str]) -> None:
    """Queue one outbound effect.

    The idempotency key is derived from the event, not from a timestamp, so a
    re-drain after a crash cannot double-send and a replayed history cannot
    duplicate cards on a newly adopted board.
    """
    key = hashlib.sha1("|".join([channel, str(feedback_id or ""), *key_parts])
                       .encode("utf-8")).hexdigest()[:32]
    cur.execute(
        """INSERT INTO outbox (channel, feedback_id, idempotency_key, payload)
           VALUES (%s,%s,%s,%s)
           ON CONFLICT (idempotency_key) DO NOTHING;""",
        (channel, feedback_id, key, json.dumps(payload)))


def alias_for(cur, cohort: str, subject_id: int) -> str:
    """Resolve, creating if needed, the synthetic alias for a patient.

    Every physician-visible surface outside the clinical UI - board, email,
    digest - refers to PT-nnnn. Costs nothing, and means no DUA-covered
    identifier ever leaves the clinical database.
    """
    cur.execute("""SELECT display_alias FROM subject_alias
                    WHERE cohort=%s AND subject_id=%s;""", (cohort, subject_id))
    row = cur.fetchone()
    if row:
        return row[0]
    for _ in range(5):                       # tiny retry: alias is globally unique
        cur.execute("SELECT count(*) FROM subject_alias;")
        alias = f"PT-{cur.fetchone()[0] + 1:04d}"
        cur.execute(
            """INSERT INTO subject_alias (cohort, subject_id, display_alias)
               VALUES (%s,%s,%s)
               ON CONFLICT DO NOTHING
               RETURNING display_alias;""", (cohort, subject_id, alias))
        got = cur.fetchone()
        if got:
            return got[0]
        cur.execute("""SELECT display_alias FROM subject_alias
                        WHERE cohort=%s AND subject_id=%s;""", (cohort, subject_id))
        row = cur.fetchone()
        if row:
            return row[0]
    raise ServiceError("could not allocate a patient alias after 5 attempts")


def _dedup_context(cur, key: str, subject_id: int, lookback_days: int) -> triage_mod.DedupContext:
    """Distinct subjects carrying this defect signature in the window.

    Counts the CURRENT subject too, so a first report yields 1. U-5 fires on the
    second distinct subject, which is the point at which one patient's defect
    becomes a cohort's.
    """
    cur.execute(
        """SELECT subject_id, feedback_id
             FROM feedback
            WHERE dedup_key = %s
              AND created_at >= NOW() - make_interval(days => %s);""",
        (key, lookback_days))
    rows = cur.fetchall()
    subjects = {r[0] for r in rows} | {subject_id}
    return triage_mod.DedupContext(distinct_subjects=len(subjects),
                                   prior_card_ids=tuple(r[1] for r in rows))


def _lookback_days(policy) -> int:
    for r in policy.urgency_rules:
        if r.id == "U-5":
            return int(r.params.get("lookback_days", 30))
    return 30


def _fetch(cur, feedback_id: int) -> dict:
    cur.execute("""SELECT feedback_id, state, urgency, urgency_rule_id, symptom,
                          owner_roles, reporter_id, acknowledged_by, cohort,
                          subject_id, disposition
                     FROM feedback WHERE feedback_id=%s;""", (feedback_id,))
    row = cur.fetchone()
    if not row:
        raise ServiceError(f"no such report: {feedback_id}")
    cols = ["feedback_id", "state", "urgency", "urgency_rule_id", "symptom",
            "owner_roles", "reporter_id", "acknowledged_by", "cohort",
            "subject_id", "disposition"]
    return dict(zip(cols, row))


# ── capture ────────────────────────────────────────────────────────────────────

CAPTURE_FIELDS = (
    "reporter_id", "reporter_name_raw", "symptom", "report_text", "disputed_field",
    "clinical_action_flag", "seen_other_patients", "cohort", "namespace",
    "subject_id", "anchor_kind", "anchor_key", "fact_ids", "source_hadm_ids",
    "fact_text", "salience_tier", "retention_class", "certainty",
    "current_lifecycle", "negative_kind", "taxonomy_version", "prompt_version",
    "artifact_generated_at",
)


def create_report(conn, **fields) -> int:
    """Capture one physician report, triage it, and queue the acknowledgement.

    The SNAPSHOT fields are not optional decoration. With the support database
    holding no foreign key into the clinical one - deliberately, since d3 deletes
    and reinserts tiered_ledger per subject on every fold-in - the card must
    carry everything needed to read it months later. In particular
    taxonomy_version, prompt_version and artifact_generated_at: a large share of
    reports turn out to be against artifacts that predate a fix, and without
    those three a card cannot be dispositioned at all.

    None of this is asked of the physician. The UI captures it from the row.
    """
    policy = pol_mod.load()
    sym = policy.symptom(fields.get("symptom", ""))     # raises on unknown symptom

    for required in ("symptom", "report_text", "cohort", "subject_id", "anchor_kind"):
        if fields.get(required) in (None, ""):
            raise ServiceError(f"'{required}' is required to file a report")

    cur = conn.cursor()
    try:
        cols = [f for f in CAPTURE_FIELDS if f in fields]
        vals = [fields[f] for f in cols]
        cur.execute(
            f"""INSERT INTO feedback ({', '.join(cols)})
                VALUES ({', '.join(['%s'] * len(cols))})
                RETURNING feedback_id;""", vals)
        fid = cur.fetchone()[0]
        _log_event(cur, fid, "created", to_state="new", actor_kind="physician",
                   actor_id=fields.get("reporter_id"),
                   note=f"{sym.id}: {sym.physician_label}")

        # Triage immediately. It is deterministic and cheap - no queue, no worker,
        # no model - so a physician's report is routed before they leave the page.
        key = triage_mod.dedup_key(fields["symptom"], fields.get("retention_class"),
                                   fields.get("salience_tier"), fields.get("fact_text"))
        ctx = _dedup_context(cur, key, int(fields["subject_id"]), _lookback_days(policy))
        result = triage_mod.get_engine().triage(fields, ctx, evidence=None)
        _apply_triage(cur, fid, result, from_state="new")

        alias = alias_for(cur, fields["cohort"], int(fields["subject_id"]))
        enqueue(cur, "email", fid,
                {"template": "acknowledge", "feedback_id": fid, "alias": alias,
                 "symptom": sym.id, "symptom_label": sym.physician_label,
                 "urgency": result.urgency, "owner_roles": result.owner_roles,
                 "to_physician_id": fields.get("reporter_id")},
                key_parts=["acknowledge"])
        enqueue(cur, "board", fid, {"action": "upsert", "feedback_id": fid},
                key_parts=["upsert", "new"])
        conn.commit()
        return fid
    except Exception:
        conn.rollback()
        raise


def _apply_triage(cur, feedback_id: int, result, *, from_state: str) -> None:
    """Write triage output and log one event per rule that fired.

    One event PER RULE, not one per card: the rule-level counts are what reveal a
    miscalibrated policy, and a headline-only log would hide a gate that fires on
    everything.
    """
    cur.execute(
        """UPDATE feedback
              SET policy_version=%s, urgency=%s, urgency_rule_id=%s,
                  owner_roles=%s, defect_class=%s, s1_subclass=%s, dedup_key=%s,
                  state='triaged', triaged_at=NOW()
            WHERE feedback_id=%s;""",
        (result.policy_version, result.urgency, result.urgency_rule_id,
         result.owner_roles, result.defect_class, result.s1_subclass,
         result.dedup_key, feedback_id))
    _log_event(cur, feedback_id, "triaged", from_state=from_state, to_state="triaged",
               actor_kind="rule", note="; ".join(result.notes) or None,
               detail={"policy_version": result.policy_version,
                       "fired_rules": result.fired_rules,
                       "owner_roles": result.owner_roles,
                       "defect_class": result.defect_class,
                       "s1_subclass": result.s1_subclass})
    for rid in result.fired_rules:
        _log_event(cur, feedback_id, "urgency_change", actor_kind="rule", rule_id=rid,
                   note=pol_mod.load().rule(rid).description)


# ── evidence ───────────────────────────────────────────────────────────────────

def attach_evidence(conn, feedback_id: int, collector_version: str,
                    evidence: Mapping[str, Any], *, retriage: bool = True) -> None:
    """Record collector output and, by default, re-run triage with it.

    Evidence is APPENDED, never overwritten: a newer collector adds a row, so a
    card always shows which collector produced the finding it was routed on.

    Re-triage matters mainly for S1, whose sub-class is unresolved at capture and
    determined only once the four-query decision tree has run against the
    clinical database.
    """
    cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO feedback_evidence
                         (feedback_id, collector_version, evidence)
                       VALUES (%s,%s,%s);""",
                    (feedback_id, collector_version, json.dumps(dict(evidence))))
        _log_event(cur, feedback_id, "comment", note=f"evidence collected ({collector_version})",
                   detail=dict(evidence))
        if retriage:
            cur.execute("""SELECT symptom, retention_class, salience_tier, fact_text,
                                  disputed_field, clinical_action_flag,
                                  seen_other_patients, subject_id, urgency, state
                             FROM feedback WHERE feedback_id=%s;""", (feedback_id,))
            r = cur.fetchone()
            if not r:
                raise ServiceError(f"no such report: {feedback_id}")
            card = dict(zip(["symptom", "retention_class", "salience_tier", "fact_text",
                             "disputed_field", "clinical_action_flag",
                             "seen_other_patients", "subject_id"], r[:8]))
            prev_urgency, state = r[8], r[9]
            policy = pol_mod.load()
            key = triage_mod.dedup_key(card["symptom"], card["retention_class"],
                                       card["salience_tier"], card["fact_text"])
            ctx = _dedup_context(cur, key, int(card["subject_id"]), _lookback_days(policy))
            result = triage_mod.get_engine().triage(card, ctx, evidence=evidence)

            # Urgency is a FLOOR. Re-triage may raise a card but must never lower
            # one: only a named human may downgrade, via set_urgency().
            if prev_urgency == "urgent" and result.urgency != "urgent":
                result.urgency = "urgent"
                result.notes.append(
                    "urgency held at 'urgent' - re-triage may raise, never lower")
            _apply_triage(cur, feedback_id, result, from_state=state)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ── lifecycle ──────────────────────────────────────────────────────────────────

def transition(conn, feedback_id: int, to_state: str, *, actor_id: Optional[int] = None,
               note: Optional[str] = None) -> None:
    """Move a card. Validated against the policy's state machine."""
    policy = pol_mod.load()
    if to_state == "closed":
        raise ServiceError("use close() - a terminal state needs a disposition and a reply")
    cur = conn.cursor()
    try:
        card = _fetch(cur, feedback_id)
        if not policy.can_transition(card["state"], to_state):
            raise ServiceError(
                f"{card['state']} -> {to_state} is not a legal transition under "
                f"{policy.version}; allowed: {list(policy.states.get(card['state'], ()))}")
        cur.execute("UPDATE feedback SET state=%s WHERE feedback_id=%s;",
                    (to_state, feedback_id))
        _log_event(cur, feedback_id, "state_change", from_state=card["state"],
                   to_state=to_state,
                   actor_kind="physician" if actor_id else "system",
                   actor_id=actor_id, note=note)
        enqueue(cur, "board", feedback_id,
                {"action": "move", "feedback_id": feedback_id, "state": to_state},
                key_parts=["move", to_state])
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def acknowledge(conn, feedback_id: int, actor_id: int, note: Optional[str] = None) -> None:
    """Second owner's acknowledgement on a dual-owned card.

    One click, not a discussion. Its only job is to stop a card being closed
    unilaterally into the wrong defect class - the specific risk on S1, where
    an extraction miss and a taxonomy prune look identical to the reporter.
    """
    cur = conn.cursor()
    try:
        _fetch(cur, feedback_id)
        cur.execute("UPDATE feedback SET acknowledged_by=%s WHERE feedback_id=%s;",
                    (actor_id, feedback_id))
        _log_event(cur, feedback_id, "acknowledged", actor_kind="physician",
                   actor_id=actor_id, note=note)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def set_urgency(conn, feedback_id: int, urgency: str, *, actor_id: int,
                reason: str, rule_id: Optional[str] = None) -> None:
    """Change urgency by hand.

    A DOWNGRADE requires a named human and a written reason, both appended to the
    audit trail. An UPGRADE must still name a rule, so that every urgent card in
    the system - however it got there - can be traced to a signed-off gate.
    """
    if urgency not in ("urgent", "routine"):
        raise ServiceError(f"unknown urgency '{urgency}'")
    if not reason or not reason.strip():
        raise ServiceError("a reason is required to change urgency by hand")
    if not actor_id:
        raise ServiceError("urgency may only be changed by a named person")
    cur = conn.cursor()
    try:
        card = _fetch(cur, feedback_id)
        if urgency == "urgent":
            rule_id = rule_id or card.get("urgency_rule_id")
            if not rule_id:
                raise ServiceError(
                    "an urgent card must name the rule that justifies it")
        cur.execute("UPDATE feedback SET urgency=%s, urgency_rule_id=%s WHERE feedback_id=%s;",
                    (urgency, rule_id if urgency == "urgent" else card.get("urgency_rule_id"),
                     feedback_id))
        _log_event(cur, feedback_id, "urgency_change", actor_kind="physician",
                   actor_id=actor_id, rule_id=rule_id,
                   note=f"{card['urgency']} -> {urgency}: {reason}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def reassign(conn, feedback_id: int, owner_roles: Sequence[str], *, actor_id: int,
             reason: str) -> None:
    """Hand a card to different owners.

    The designed path for S4: polarity is a clinical judgement, so the clinician
    owns it, and where the evidence shows a rendering fault they hand it to
    engineering explicitly. A logged handoff, rather than standing dual ownership
    that leaves nobody accountable.
    """
    bad = set(owner_roles) - pol_mod.VALID_ROLES
    if bad:
        raise ServiceError(f"unknown role(s): {sorted(bad)}")
    if not owner_roles:
        raise ServiceError("a card must have at least one owner")
    if not reason or not reason.strip():
        raise ServiceError("a reason is required to reassign a card")
    cur = conn.cursor()
    try:
        card = _fetch(cur, feedback_id)
        cur.execute("UPDATE feedback SET owner_roles=%s WHERE feedback_id=%s;",
                    (list(owner_roles), feedback_id))
        _log_event(cur, feedback_id, "state_change", actor_kind="physician",
                   actor_id=actor_id,
                   note=f"reassigned {card['owner_roles']} -> {list(owner_roles)}: {reason}",
                   detail={"from": card["owner_roles"], "to": list(owner_roles)})
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def close(conn, feedback_id: int, *, disposition: str, reply_text: str,
          actor_id: int, fix_version: Optional[str] = None,
          duplicate_of: Optional[int] = None) -> None:
    """Close a card with a verdict and a written answer to the reporter.

    THE REPLY IS MANDATORY, including for duplicates and not-reproducibles. That
    is the whole closed loop: it is what makes HOW_TO_READ_THE_LEDGER.md section 8
    truthful, and it is the reason a physician files a second report after the
    first one. The DB carries the same rule as a CHECK constraint, so it holds
    even against a caller that bypasses this module.
    """
    policy = pol_mod.load()
    if disposition not in policy.dispositions:
        raise ServiceError(f"unknown disposition '{disposition}'; known: "
                           f"{sorted(policy.dispositions)}")
    if not reply_text or not reply_text.strip():
        raise ServiceError("a written reply to the reporter is required to close a report")
    if not actor_id:
        raise ServiceError("a report may only be closed by a named person")
    if disposition == "duplicate" and not duplicate_of:
        raise ServiceError("disposition 'duplicate' must name the original report")

    cur = conn.cursor()
    try:
        card = _fetch(cur, feedback_id)
        if card["state"] == "closed":
            raise ServiceError(f"report {feedback_id} is already closed")
        if not policy.can_transition(card["state"], "closed"):
            raise ServiceError(
                f"{card['state']} -> closed is not a legal transition under "
                f"{policy.version}; allowed: {list(policy.states.get(card['state'], ()))}")
        if len(card["owner_roles"] or []) > 1 and not card["acknowledged_by"]:
            raise ServiceError(
                "this report is owned by both a clinician and an engineer; the "
                "second owner must acknowledge before it can be closed")

        cur.execute(
            """UPDATE feedback
                  SET state='closed', disposition=%s, reply_text=%s,
                      fix_version=%s, duplicate_of=%s, closed_at=NOW()
                WHERE feedback_id=%s;""",
            (disposition, reply_text.strip(), fix_version, duplicate_of, feedback_id))
        _log_event(cur, feedback_id, "state_change", from_state=card["state"],
                   to_state="closed", actor_kind="physician", actor_id=actor_id,
                   note=f"{disposition}: {reply_text.strip()[:200]}",
                   detail={"disposition": disposition, "fix_version": fix_version,
                           "duplicate_of": duplicate_of})
        if duplicate_of:
            _log_event(cur, feedback_id, "linked_duplicate", actor_kind="physician",
                       actor_id=actor_id, detail={"duplicate_of": duplicate_of})

        alias = alias_for(cur, card["cohort"], int(card["subject_id"]))
        enqueue(cur, "email", feedback_id,
                {"template": disposition, "feedback_id": feedback_id, "alias": alias,
                 "reply_text": reply_text.strip(), "fix_version": fix_version,
                 "to_physician_id": card["reporter_id"]},
                key_parts=["disposition", disposition])
        enqueue(cur, "board", feedback_id,
                {"action": "close", "feedback_id": feedback_id,
                 "disposition": disposition}, key_parts=["close", disposition])
        _log_event(cur, feedback_id, "replied", actor_kind="system",
                   note="reply queued to reporter")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ── loop health ────────────────────────────────────────────────────────────────

def loop_gaps(conn, *, triage_hours: int = 24, reply_days: int = 14) -> list[dict]:
    """Reports the loop has stopped moving.

    Follows the retention_visibility_gaps() idiom in database/pg.py: pure SQL,
    NEVER raises, returns [] when healthy, and leaves the caller to decide whether
    a gap is fatal or advisory.

    The failure this guards against is the one section 8 cannot survive: a loop
    that quietly stops answering. Physicians do not file a second report after an
    unanswered first, so the loop dies silently and looks exactly like success.
    """
    sql = """
    SELECT 'untriaged' AS gap, feedback_id, symptom, urgency, state, created_at
      FROM feedback
     WHERE state = 'new' AND created_at < NOW() - make_interval(hours => %s)
    UNION ALL
    SELECT 'unanswered', feedback_id, symptom, urgency, state, created_at
      FROM feedback
     WHERE state <> 'closed' AND created_at < NOW() - make_interval(days => %s)
    UNION ALL
    SELECT 'urgent_open', feedback_id, symptom, urgency, state, created_at
      FROM feedback
     WHERE state <> 'closed' AND urgency = 'urgent'
    ORDER BY created_at;
    """
    cur = conn.cursor()
    try:
        cur.execute(sql, (triage_hours, reply_days))
        return [dict(zip(["gap", "feedback_id", "symptom", "urgency", "state", "created_at"],
                         r)) for r in cur.fetchall()]
    except Exception as e:
        logger.warning("loop_gaps: check failed (%s)", e)
        return []
    finally:
        cur.close()


def outbox_gaps(conn, *, stuck_minutes: int = 60) -> list[dict]:
    """Outbound effects that never left. Same never-raises contract."""
    cur = conn.cursor()
    try:
        cur.execute(
            """SELECT outbox_id, channel, feedback_id, state, attempts, last_error
                 FROM outbox
                WHERE state IN ('pending','failed')
                  AND created_at < NOW() - make_interval(mins => %s)
                ORDER BY created_at;""", (stuck_minutes,))
        return [dict(zip(["outbox_id", "channel", "feedback_id", "state",
                          "attempts", "last_error"], r)) for r in cur.fetchall()]
    except Exception as e:
        logger.warning("outbox_gaps: check failed (%s)", e)
        return []
    finally:
        cur.close()
