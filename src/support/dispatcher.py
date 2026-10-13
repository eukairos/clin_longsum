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
dispatcher.py
=============
Drains the outbox. The only place adapters are called.

    python -m support.dispatcher                # drain once
    python -m support.dispatcher --watch 60     # drain every 60s
    python -m support.dispatcher --digest       # queue the weekly cc digest
    python -m support.dispatcher --status       # what is queued, what is stuck
    python -m support.dispatcher --limit 50     # max rows claimed per drain (default 200)
    python -m support.dispatcher --channel email --channel board   # restrict to channels: email, board, digest (repeatable)
    python -m support.dispatcher --mqtt         # after draining, publish a retained queue-depth nudge to longsum/feedback/pending
    python -m support.dispatcher --digest ROLE  # queue the digest for ROLE (bare --digest = engineer)

WHY AN OUTBOX AT ALL. Every outbound effect is written as a row inside the same
transaction as the state change that caused it, then delivered separately. Three
things fall out, all of which the loop depends on:

  1. A physician's report cannot fail because a mail host or board is down.
  2. A crash mid-delivery cannot lose a reply - the row is still pending.
  3. A re-drain cannot double-send, because every row carries an idempotency key
     derived from the event rather than the moment.

The third also makes swapping board tools cheap: point the adapter at the new
tool and re-drain, and history backfills.

PAYLOADS CARRY IDS, NOT PROSE. The dispatcher joins the card at send time rather
than trusting what was captured when the row was queued. So a correction to the
wording of a reply, or to an email template, applies to everything still queued -
and a card that was closed with the wrong text can be fixed before it goes out.

CONCURRENCY. Rows are claimed with FOR UPDATE SKIP LOCKED, so several dispatchers
(or a watcher plus a manual run) can drain the same queue without collision.

FAILURE POLICY. An adapter returning failure increments `attempts` and leaves the
row pending until MAX_ATTEMPTS, then marks it failed. Failures never raise: one
dead channel must not stop the others draining. loop_gaps()/outbox_gaps() surface
anything stuck, which is the guard against a loop that has quietly stopped
answering - the failure section 8 of the physician guide cannot survive.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from typing import Any, Optional

try:
    from support import service as svc
    from support.adapters.base import Delivery
    from support.adapters.local_board import LocalBoardAdapter
    from support.adapters.mailer import get_mailer
    from support.adapters import email_templates
except ImportError:  # pragma: no cover - direct execution from inside support/
    import service as svc
    from adapters.base import Delivery
    from adapters.local_board import LocalBoardAdapter
    from adapters.mailer import get_mailer
    from adapters import email_templates

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5

# Swap point for a real workflow tool: replace this one line, re-drain the
# outbox, and history backfills onto the new board.
BOARD = LocalBoardAdapter()


# ── card resolution ────────────────────────────────────────────────────────────

_CARD_SQL = """
SELECT f.feedback_id, f.symptom, f.fact_text, f.salience_tier, f.retention_class,
       f.urgency, f.state, f.disposition, f.reply_text, f.fix_version,
       f.duplicate_of, f.owner_roles, f.dedup_key, f.reporter_id,
       f.reporter_name_raw, f.cohort, f.subject_id,
       sa.display_alias, sc.physician_label,
       p.email, p.display_name, p.notify_mode
  FROM feedback f
  LEFT JOIN subject_alias   sa ON sa.cohort = f.cohort AND sa.subject_id = f.subject_id
  LEFT JOIN symptom_catalog sc ON sc.symptom_id = f.symptom
  LEFT JOIN physicians      p  ON p.physician_id = f.reporter_id
 WHERE f.feedback_id = %s;
"""

_CARD_COLS = ["feedback_id", "symptom", "fact_text", "salience_tier", "retention_class",
              "urgency", "state", "disposition", "reply_text", "fix_version",
              "duplicate_of", "owner_roles", "dedup_key", "reporter_id",
              "reporter_name_raw", "cohort", "subject_id", "alias", "symptom_label",
              "reporter_email", "reporter_display_name", "notify_mode"]


def _card(cur, feedback_id: int) -> Optional[dict]:
    cur.execute(_CARD_SQL, (feedback_id,))
    row = cur.fetchone()
    return dict(zip(_CARD_COLS, row)) if row else None


def _n_affected(cur, dedup_key: Optional[str], feedback_id: int) -> Optional[int]:
    """Other patients carrying the same defect signature.

    Goes into the 'accepted' templates. Telling a physician that their report
    fixed it for thirty other patients is the single most effective thing the
    loop can say - it converts reporting from a chore into something with
    visible leverage.
    """
    if not dedup_key:
        return None
    cur.execute("""SELECT count(DISTINCT subject_id) FROM feedback
                    WHERE dedup_key = %s AND feedback_id <> %s;""",
                (dedup_key, feedback_id))
    n = cur.fetchone()[0]
    return n or None


# ── channel handlers ───────────────────────────────────────────────────────────

def _deliver_email(cur, payload: dict, mailer) -> Delivery:
    fid = payload.get("feedback_id")
    card = _card(cur, fid) if fid else None
    if not card:
        return Delivery.skip(f"card {fid} no longer exists")

    # An unmatched reporter still gets their report filed - refusing would lose
    # the observation - but there is no address to answer to. Skipped, not
    # failed: retrying cannot conjure an inbox. The reply stays on the card and
    # is visible in the board and in /tiered/feedback/mine.
    if not card.get("reporter_email"):
        return Delivery.skip(
            f"reporter '{card.get('reporter_name_raw')}' is not in the staff "
            f"directory - reply recorded on the card only")
    if card.get("notify_mode") == "none":
        return Delivery.skip("reporter has notifications disabled")

    card = dict(card)
    card["n_affected"] = _n_affected(cur, card.get("dedup_key"), fid)
    template = payload.get("template") or card.get("disposition") or "acknowledge"
    subject, body = email_templates.render(template, card)
    return mailer.send(card["reporter_email"], card.get("reporter_display_name") or "",
                       subject, body)


def _deliver_board(cur, payload: dict) -> Delivery:
    fid = payload.get("feedback_id")
    card = _card(cur, fid) if fid else None
    if not card:
        return Delivery.skip(f"card {fid} no longer exists")
    action = payload.get("action")
    if action == "move":
        return BOARD.move(card, payload.get("state") or card["state"])
    if action == "close":
        return BOARD.close(card, payload.get("disposition") or card["disposition"])
    return BOARD.upsert(card)


def _deliver_digest(cur, payload: dict, mailer) -> Delivery:
    """Weekly summary to the cc role.

    The non-owner gets a digest, NOT a per-card push. Notifying both parties on
    every card is ignored inside a month, which defeats the concurrent-awareness
    the loop is meant to provide. A weekly list of what the other role is
    carrying is read; twenty interrupts a day are not.
    """
    role = payload.get("role")
    cur.execute("""SELECT display_name, email FROM physicians
                    WHERE active AND role = %s AND notify_mode <> 'none';""", (role,))
    people = cur.fetchall()
    if not people:
        return Delivery.skip(f"no active recipients for role '{role}'")

    cur.execute("""SELECT f.feedback_id, f.urgency, f.state, f.symptom,
                          left(coalesce(f.fact_text,''), 70), sa.display_alias
                     FROM feedback f
                     LEFT JOIN subject_alias sa
                            ON sa.cohort = f.cohort AND sa.subject_id = f.subject_id
                    WHERE f.state <> 'closed'
                    ORDER BY (f.urgency='urgent') DESC, f.created_at;""")
    rows = cur.fetchall()
    if not rows:
        return Delivery.skip("nothing open; digest not sent")

    urgent = [r for r in rows if r[1] == "urgent"]
    lines = [f"{len(rows)} open ledger report(s), {len(urgent)} urgent.", ""]
    for r in rows:
        flag = "URGENT " if r[1] == "urgent" else "       "
        lines.append(f"  {flag}#{r[0]}  {r[2]:<11} {r[3]:<4} {r[5] or '?':<8} {r[4]}")
    lines += ["", "Open the review board for detail. You are copied on all of these;",
              "the named owner is actioning them."]
    body = f"{email_templates.BANNER}\n\n" + "\n".join(lines) + f"\n\n{email_templates.BANNER}\n"
    subject = f"{email_templates.SUBJECT_PREFIX} weekly digest - {len(rows)} open, {len(urgent)} urgent"

    failures = []
    for name, email in people:
        d = mailer.send(email, name, subject, body)
        if not d.ok:
            failures.append(f"{email}: {d.detail}")
    return Delivery.fail("; ".join(failures)) if failures else Delivery.sent(
        detail=f"digest to {len(people)} recipient(s) in role '{role}'")


# ── drain ──────────────────────────────────────────────────────────────────────

def drain(conn, *, limit: int = 200, channels: Optional[list[str]] = None) -> dict:
    """Claim and deliver pending outbox rows. Never raises on delivery failure."""
    mailer = get_mailer()
    counts = {"sent": 0, "skipped": 0, "failed": 0, "retry": 0}
    cur = conn.cursor()

    where = "state = 'pending'"
    params: list[Any] = []
    if channels:
        where += " AND channel = ANY(%s)"
        params.append(channels)
    params.append(limit)

    # SKIP LOCKED lets several dispatchers share the queue safely.
    cur.execute(f"""SELECT outbox_id, channel, payload, attempts
                      FROM outbox WHERE {where}
                     ORDER BY created_at
                     LIMIT %s FOR UPDATE SKIP LOCKED;""", params)
    rows = cur.fetchall()

    for outbox_id, channel, payload, attempts in rows:
        payload = payload if isinstance(payload, dict) else json.loads(payload or "{}")
        try:
            if channel == "email":
                d = _deliver_email(cur, payload, mailer)
            elif channel == "board":
                d = _deliver_board(cur, payload)
            elif channel == "digest":
                d = _deliver_digest(cur, payload, mailer)
            else:
                d = Delivery.skip(f"unknown channel '{channel}'")
        except Exception as e:                       # adapter bug, not a send failure
            logger.exception("dispatcher: channel %s raised", channel)
            d = Delivery.fail(f"{type(e).__name__}: {e}")

        if d.ok and d.skipped:
            cur.execute("""UPDATE outbox SET state='skipped', sent_at=NOW(),
                                  last_error=%s, attempts=attempts+1
                            WHERE outbox_id=%s;""", (d.detail, outbox_id))
            counts["skipped"] += 1
        elif d.ok:
            cur.execute("""UPDATE outbox SET state='sent', sent_at=NOW(),
                                  external_ref=%s, last_error=NULL, attempts=attempts+1
                            WHERE outbox_id=%s;""", (d.external_ref, outbox_id))
            counts["sent"] += 1
        else:
            final = (attempts + 1) >= MAX_ATTEMPTS
            cur.execute("""UPDATE outbox SET state=%s, attempts=attempts+1, last_error=%s
                            WHERE outbox_id=%s;""",
                        ("failed" if final else "pending", d.detail, outbox_id))
            counts["failed" if final else "retry"] += 1
            logger.warning("dispatcher: %s delivery failed (%s): %s",
                           channel, "final" if final else "will retry", d.detail)
        conn.commit()

    return counts


def queue_digest(conn, role: str = "engineer") -> int:
    """Queue one digest for a role. Idempotent within the calendar week."""
    from datetime import date
    cur = conn.cursor()
    week = date.today().isocalendar()
    svc.enqueue(cur, "digest", None, {"role": role},
                key_parts=["digest", role, f"{week[0]}W{week[1]}"])
    conn.commit()
    cur.execute("""SELECT count(*) FROM outbox
                    WHERE channel='digest' AND state='pending';""")
    return cur.fetchone()[0]


def publish_mqtt(conn, host: str = "localhost", port: int = 1883,
                 topic: str = "longsum/feedback/pending") -> bool:
    """Retained MQTT nudge: how much is waiting, and how much is urgent.

    Mirrors c5_review_queue._publish() exactly - retained so a late-started
    subscriber still gets the latest, best-effort so failure is logged and never
    raised, with the database as the source of truth.

    DELIBERATELY A SEPARATE TOPIC, NOT AN ADDITION TO c5's DIGEST. Folding a
    feedback count into the c5 digest would make a clinical pipeline stage query
    the SUPPORT database - a dependency in exactly the direction the separate
    database exists to prevent, and a new failure mode for c5 owned by a system
    it should know nothing about. Two publishers on sibling topics cost one
    subscriber rule; the coupling would cost more later.
    """
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        logger.warning("paho-mqtt not installed; MQTT nudge skipped")
        return False
    cur = conn.cursor()
    cur.execute("""SELECT count(*) FILTER (WHERE state <> 'closed'),
                          count(*) FILTER (WHERE state <> 'closed' AND urgency='urgent'),
                          count(*) FILTER (WHERE state = 'new')
                     FROM feedback;""")
    n_open, n_urgent, n_new = cur.fetchone()
    digest = {"n_open": n_open, "n_urgent": n_urgent, "n_new": n_new,
              "n_gaps": len(svc.loop_gaps(conn))}
    try:
        try:                                            # paho 2.x
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except (AttributeError, TypeError):             # paho 1.x
            client = mqtt.Client()
        client.connect(host, port, keepalive=30)
        client.loop_start()
        info = client.publish(topic, json.dumps(digest), qos=1, retain=True)
        info.wait_for_publish(timeout=5)
        client.loop_stop()
        client.disconnect()
        print(f"[dispatcher] mqtt {topic} <- {digest}")
        return True
    except Exception as exc:
        logger.warning("MQTT publish to %s:%s failed (%s); nudge skipped", host, port, exc)
        return False


def status(conn) -> dict:
    cur = conn.cursor()
    cur.execute("""SELECT channel, state, count(*) FROM outbox
                    GROUP BY channel, state ORDER BY channel, state;""")
    by = [{"channel": r[0], "state": r[1], "n": r[2]} for r in cur.fetchall()]
    return {"outbox": by, "stuck": svc.outbox_gaps(conn), "mailer": get_mailer().name,
            "board_adapter": BOARD.name}


def main() -> None:
    ap = argparse.ArgumentParser(description="Drain the feedback outbox.")
    ap.add_argument("--watch", type=int, metavar="SECONDS",
                    help="Drain repeatedly at this interval.")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--channel", action="append",
                    help="Restrict to a channel (repeatable): email, board, digest.")
    ap.add_argument("--digest", metavar="ROLE", nargs="?", const="engineer",
                    help="Queue the weekly digest for a role and exit.")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--mqtt", action="store_true",
                    help="Publish a retained queue-depth nudge to "
                         "longsum/feedback/pending after draining.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = svc.connect()
    try:
        if args.status:
            print(json.dumps(status(conn), indent=2, default=str))
            return
        if args.digest:
            print(f"[dispatcher] digest queued for '{args.digest}'; "
                  f"{queue_digest(conn, args.digest)} pending")
            return
        while True:
            c = drain(conn, limit=args.limit, channels=args.channel)
            if any(c.values()) or not args.watch:
                print(f"[dispatcher] sent={c['sent']} skipped={c['skipped']} "
                      f"retry={c['retry']} failed={c['failed']}")
            if args.mqtt:
                publish_mqtt(conn)
            if not args.watch:
                break
            time.sleep(args.watch)
    except KeyboardInterrupt:
        print("\n[dispatcher] stopped")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
