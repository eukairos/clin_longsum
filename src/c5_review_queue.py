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
c5_review_queue.py
==================
Clinician trigger. Turn the status="flagged" audit signal into a
durable, deduplicated review queue and notify a Mosquitto review topic
so a clinician triages the cases the pipeline could not resolve on its own.

Flagged sources (status="flagged" in DB generation_audit table):
  retention_extractor  scores.recovered - truncated extraction auto-recovered;
                       partial fact set, verify nothing material was cut       [high]
  retention_validator  a pinned fact in a compacted span not represented in the
                       ledger (genuine retention loss)                          [high]
  compactor (v1+correct) compaction still failing faithfulness after re-compact [high]
  faithfulness_validator compacted paragraph contains unsupported claim(s)      [medium]

LIVENESS, NOT HISTORY: generation_audit is append-only, so a concern that a later
re-fold fixed still has its old flagged row sitting there forever. c5 therefore
looks only at the LATEST audit row per concern group and queues it only if that
row is still flagged. Grouping is role-dependent:
  retention_extractor    per (subject, input_hadm_ids) - an extraction flag stands
                         until THAT admission is re-extracted
  the span roles         per (subject, role) - one verdict per subject per run; the
  (retention_validator, compacted span drifts between fold-ins, so hadm-keyed
   compactor, grouping would strand old flags permanently faithfulness_validator)

Durable store is the Postgres `review_queue` table (schema.sql section 6). 
Each row is keyed by review_id = sha1(role|subject|hadm_ids|score magnitude).
UPSERT refreshes last_seen_at/severity/reason/detail and PRESERVES review_status, so
physician triage survives every re-fold; pending rows not reproduced by this run are
set 'auto_cleared' (never deleted). A magnitude change mints a new id (re-alerts) and
auto-clears the superseded one, so the same concern is never double-counted.
review_queue.jsonl is a DB-projected cache, rewritten each run. 
MQTT is fire-and-forget: the digest counts PENDING rows in the DB, so the retained
payload is finally accurate; a dead broker never fails the run.

Resolve/dismiss from the UI: POST /tiered/review-queue/resolve (longsum_api.py).

    cd ~/clin_longsum
    python src/c5_review_queue.py
    python src/c5_review_queue.py --no-publish        # sync queue, skip MQTT
    python src/c5_review_queue.py --subject 90000042  # fold-in: scope + clear one subject
    python src/c5_review_queue.py --broker-host 192.168.1.10 --broker-port 1883 --topic longsum/review/pending
    python src/c5_review_queue.py --fresh --confirm   # TRUNCATE + rebuild (DESTROYS triage)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import psycopg2.extras
from datetime import datetime, timezone
from psycopg2.extras import Json

import config
import io_utils
from database.pg import get_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("c5_review_queue")

# DB-projected cache of the queue (rewritten each run); the record is the
# `review_queue` TABLE. Kept on disk for grep/inspection without a psql session.
QUEUE_JSONL = config.OUTPUT_DIR / "review_queue.jsonl"

DEFAULT_BROKER = "localhost"
DEFAULT_PORT = 1883
DEFAULT_TOPIC = "longsum/review/pending"
REVIEW_URL = "http://localhost:8000/tiered/review"


_AUDIT_COLS = ("role, subject_id, input_hadm_ids, scores, prompt_version, "
               "taxonomy_version, status, created_at")

# Span-scoped roles: one verdict per subject per run (see module docstring).
# Admission-scoped roles: one verdict per subject per admission-group. 'role' MUST be in
# the DISTINCT ON key - with >1 role, two roles sharing input_hadm_ids would collide and
# one flag would be silently dropped.
_ADMISSION_ROLES = ("retention_extractor", "episode_paragrapher", "single_summary")

# retention_validator is DELIBERATELY absent: c2's fact-level losses reach the physician
# through retention_review -> the RETENTION tab, which shows the individual missing facts.
# Surfacing the same c2 verdict here too would put one concern in two places with two
# resolve paths writing to two different tables. One concern, one home.
_SPAN_ROLES = ("compactor", "faithfulness_validator")


_DRIFT_SQL = """
    SELECT tv.subject_id,
           tv.prior_pinned_hadm_ids,
           tv.prior_edited_by,
           tv.prior_edited_at,
           (SELECT cr.covered_hadm_ids
              FROM compaction_review cr
             WHERE cr.subject_id = tv.subject_id
             ORDER BY cr.c3_loaded_at DESC NULLS LAST
             LIMIT 1) AS current_hadm_ids
      FROM tiered_view tv
     WHERE tv.prior_pinned
"""


def _drifted_pins(conn, subject=None) -> list[dict]:
    """PIN-AND-REQUEUE. A physician-edited compacted paragraph is frozen as the PREFIX of
    the served text (two-region model); the machine continuation below it keeps covering
    newly-aged admissions automatically. So this concern does NOT mean the edit went stale -
    it means new prior-admission material was folded in AFTER the edit (below the "Added since
    your review" divider) and the physician should review that addition. We compare the span
    the edit was written against (tiered_view.prior_pinned_hadm_ids) with the span compaction
    now covers; on divergence the edit is KEPT and the concern is raised.

    Emitted in the same shape as a generation_audit record so it flows through the
    normal upsert / watermark / auto-clear path - if a later fold-in restores the
    original membership the concern auto-clears like any other.
    """
    q, params = _DRIFT_SQL, []
    if subject is not None:
        q, params = q + " AND tv.subject_id = %s", [subject]
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(q, params)
    out: list[dict] = []
    for r in cur.fetchall():
        pinned  = set(r["prior_pinned_hadm_ids"] or [])
        current = set(r["current_hadm_ids"] or [])
        if not current or pinned == current:
            continue
        added, removed = sorted(current - pinned), sorted(pinned - current)
        out.append({
            "role":             "prior_span_drift",
            "subject_id":       r["subject_id"],
            "input_hadm_ids":   sorted(current),
            # scores drive _review_id: membership IS the magnitude here, so a further
            # drift mints a new id and re-alerts rather than silently widening.
            "scores":           {"n_added": len(added), "n_removed": len(removed),
                                 "added": added, "removed": removed,
                                 "edited_by": r["prior_edited_by"]},
            "prompt_version":   None,
            "taxonomy_version": None,
            "status":           "flagged",
            "created_at":       r["prior_edited_at"] or datetime.now(timezone.utc),
        })
    cur.close()
    return out


def _live_flagged(conn, subject=None) -> list[dict]:
    """LIVE flagged concerns only: the LATEST generation_audit row per concern group,
    kept if it is still status='flagged'. DISTINCT ON does the per-group latest; the
    status filter is applied OUTSIDE it, so a newer ok row correctly suppresses an
    older flag instead of the old flag surviving forever."""
    scope, scope_params = "", []
    if subject is not None:
        scope, scope_params = " AND subject_id = %s", [subject]

    q_extract = f"""
        SELECT * FROM (
            SELECT DISTINCT ON (subject_id, role, input_hadm_ids) {_AUDIT_COLS}
              FROM generation_audit
             WHERE role IN %s{scope}
             ORDER BY subject_id, role, input_hadm_ids, created_at DESC NULLS LAST, id DESC
        ) t WHERE status = 'flagged'
    """
    q_span = f"""
        SELECT * FROM (
            SELECT DISTINCT ON (subject_id, role) {_AUDIT_COLS}
              FROM generation_audit
             WHERE role IN %s{scope}
             ORDER BY subject_id, role, created_at DESC NULLS LAST, id DESC
        ) t WHERE status = 'flagged'
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    out: list[dict] = []
    cur.execute(q_extract, [_ADMISSION_ROLES] + scope_params)
    out.extend(dict(r) for r in cur.fetchall())
    cur.execute(q_span, [_SPAN_ROLES] + scope_params)
    out.extend(dict(r) for r in cur.fetchall())
    out.extend(_drifted_pins(conn, subject))
    for d in out:
        if d.get("created_at") is not None:
            d["created_at"] = d["created_at"].isoformat()
    return out


_UPSERT_SQL = """
INSERT INTO review_queue (review_id, subject_id, source_role, severity, reason, hadm_ids,
                          detail, prompt_version, taxonomy_version, flagged_at,
                          queued_at, last_seen_at, review_status)
VALUES %s
ON CONFLICT (review_id) DO UPDATE SET
    last_seen_at = EXCLUDED.last_seen_at,
    severity     = EXCLUDED.severity,
    reason       = EXCLUDED.reason,
    detail       = EXCLUDED.detail,
    flagged_at   = EXCLUDED.flagged_at
RETURNING (xmax = 0) AS inserted
"""          # review_status / reviewed_by / review_notes deliberately NOT touched

_read_jsonl = io_utils.read_jsonl        # shared tolerant reader


def _describe(rec: dict) -> tuple[str, str]:
    """(human-readable reason, severity) for a flagged audit record."""
    role = rec.get("role", "")
    sc = rec.get("scores", {}) or {}
    if role == "retention_validator":
        return (f"Retention loss: {sc.get('n_missing', 0)} pinned fact(s) from a compacted "
                f"span absent from the ledger (recall {sc.get('recall', 0)}).", "high")
    if role == "retention_extractor":
        return ("Truncated extraction auto-recovered to a partial fact set -- verify nothing "
                "clinically material was cut before relying on this admission.", "high")
    if role == "compactor":
        # Counts, not a score - same reasoning as faithfulness_validator below. What the
        # physician needs is what automated correction could not fix, and that the fix is
        # now theirs to make in the editor directly beneath this message.
        n_drop, n_corr = sc.get("n_dropped"), sc.get("n_corrected")
        did = []
        if n_corr: did.append(f"rewrote {n_corr}")
        if n_drop: did.append(f"removed {n_drop}")
        detail = (" It already " + " and ".join(did) + " statement(s).") if did else ""
        return ("Automatic correction ran on this compacted paragraph and could not make it "
                f"match the source admissions.{detail} The remaining unmatched statements are "
                "listed below - correct the paragraph yourself, or dismiss if it reads "
                "correctly to you.", "high")
    if role == "faithfulness_validator":
        # Report a COUNT
        # The claims themselves come from compaction_review.failed_statements and are
        # quoted verbatim in the TRIAGE tab.
        n_tot, n_sup = sc.get("n_total"), sc.get("n_supported")
        if n_tot and n_sup is not None and n_tot > n_sup:
            n_bad = n_tot - n_sup
            return (f"{n_bad} of {n_tot} statements in the compacted paragraph could not be "
                    f"matched to the source admissions. Each is listed below with the "
                    f"checker's reasoning - confirm from your knowledge of the patient, or "
                    f"correct the paragraph.", "medium")
        return ("The faithfulness checker flagged the compacted paragraph, but reported no "
                "specific unmatched statement - likely a checker fault rather than a "
                "content problem. Verify, then dismiss.", "medium")
    if role == "prior_span_drift":
        added, removed = sc.get("n_added", 0), sc.get("n_removed", 0)
        bits = []
        if added:   bits.append(f"{added} newer admission(s) were folded in below it")
        if removed: bits.append(f"{removed} admission(s) are no longer in the span")
        return ("Your edited prior-admissions paragraph is preserved unchanged; new material "
                f"has been added after it, under \"Added since your review\": {'; '.join(bits)}. "
                "Review the added section and fold it into your text if you wish, or dismiss to "
                "keep things as they are.", "high")
    if role == "episode_paragrapher":
        return ("Episode paragraph came back empty - this admission has no narrative in the "
                "established record. Regenerate before relying on this patient's history.", "high")
    if role == "single_summary":
        return ("Incoming-admission summary came back empty - there is nothing to review for "
                "this discharge. Regenerate before sign-off.", "high")
    return ("Flagged for review.", "medium")


def _scores_fingerprint(scores: dict | None) -> str:
    """Canonical, timestamp-free digest of the flag's MAGNITUDE (floats rounded so jitter
    across a re-fold or a jsonl->DB round-trip does not shift the id)."""
    def _canon(v):
        if isinstance(v, float):
            return round(v, 4)
        if isinstance(v, dict):
            return {k: _canon(v[k]) for k in sorted(v)}
        if isinstance(v, (list, tuple)):
            return [_canon(x) for x in v]
        return v
    return json.dumps(_canon(scores or {}), sort_keys=True, separators=(",", ":"))


def _review_id(rec: dict) -> str:
    """Stable identity of a flagged concern: role + subject + admissions + score magnitude.
    Excludes output_sha256/created_at, so an unchanged concern keeps its id across re-folds
    and the jsonl->DB source switch; a magnitude change yields a new id (re-alerts)."""
    hadms = ",".join(str(h) for h in sorted(rec.get("input_hadm_ids", [])))
    base = f"{rec.get('role')}|{rec.get('subject_id')}|{hadms}|{_scores_fingerprint(rec.get('scores'))}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def _to_item(rec: dict) -> dict:
    reason, severity = _describe(rec)
    return {
        "review_id": _review_id(rec),
        "severity": severity,
        "subject_id": rec.get("subject_id"),
        "source_role": rec.get("role"),
        "reason": reason,
        "hadm_ids": list(rec.get("input_hadm_ids") or []),
        "detail": rec.get("scores") or {},
        "prompt_version": rec.get("prompt_version"),
        "taxonomy_version": rec.get("taxonomy_version"),
        "flagged_at": rec.get("created_at"),
    }


def _publish(digest: dict, host: str, port: int, topic: str) -> bool:
    """Publish ONE digest notification matching review_notifier.py's schema, retained so
    a late-started notifier still gets the latest. Best-effort: failure is logged, not
    raised -- the durable queue file is the source of truth."""
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        logger.warning("paho-mqtt not installed; queue file written, MQTT notify skipped.")
        return False
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
        return True
    except Exception as exc:
        logger.warning("MQTT publish to %s:%s failed (%s); queue file written, "
                       "notify skipped.", host, port, exc)
        return False


_DIGEST_SQL = """
SELECT count(*)                                                             AS n_queued,
       count(*) FILTER (WHERE severity = 'high')                            AS n_high,
       count(*) FILTER (WHERE source_role IN ('faithfulness_validator',
                                              'compactor'))                AS n_fail
  FROM review_queue WHERE review_status = 'pending'
"""

_PROJECT_SQL = """
SELECT review_id, subject_id, source_role, severity, reason, hadm_ids, detail,
       prompt_version, taxonomy_version, flagged_at, queued_at, last_seen_at,
       review_status, reviewed_by, reviewed_at, review_notes
  FROM review_queue ORDER BY review_status, severity, subject_id, source_role
"""


def _project_jsonl(cur) -> int:
    """Overwrite the jsonl cache from the DB (the table is the record)."""
    cur.execute(_PROJECT_SQL)
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        for k in ("flagged_at", "queued_at", "last_seen_at", "reviewed_at"):
            if d.get(k) is not None and hasattr(d[k], "isoformat"):
                d[k] = d[k].isoformat()
        d["hadm_ids"] = list(d.get("hadm_ids") or [])
        rows.append(d)
    io_utils.write_jsonl(QUEUE_JSONL, rows)
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--broker-host", default=DEFAULT_BROKER)
    ap.add_argument("--broker-port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--topic", default=DEFAULT_TOPIC)
    ap.add_argument("--no-publish", action="store_true", help="sync queue only, skip MQTT")
    ap.add_argument("--subject", type=int, default=None,
                    help="scope the sync to one subject (fold-in re-review notification)")
    ap.add_argument("--fresh", action="store_true",
                    help="TRUNCATE review_queue and rebuild -- DESTROYS physician triage")
    ap.add_argument("--confirm", action="store_true", help="required with --fresh")
    args = ap.parse_args()
    config.check_paths()

    if args.fresh:
        if args.subject is not None:
            raise SystemExit("--fresh is global; do not combine it with --subject.")
        if not args.confirm:
            raise SystemExit(
                "--fresh TRUNCATEs review_queue, discarding every physician decision "
                "(resolved / dismissed / notes). It is a repair tool, not a refresh - "
                "a normal run already reconciles the queue. Re-run with --confirm if "
                "that is really what you want.")

    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Watermark for this run. clock_timestamp() (not now(), which is frozen at
        # transaction start) so the < comparison in the auto-clear is unambiguous.
        cur.execute("SELECT clock_timestamp() AS ts")
        run_ts = cur.fetchone()["ts"]

        if args.fresh:
            cur.execute("TRUNCATE review_queue;")

        live = _live_flagged(conn, args.subject)
        items, seen = [], set()
        for rec in live:
            it = _to_item(rec)
            if it["review_id"] in seen:
                continue
            seen.add(it["review_id"])
            items.append(it)

        rows = [(it["review_id"], it["subject_id"], it["source_role"], it["severity"],
                 it["reason"], it["hadm_ids"], Json(it["detail"]), it["prompt_version"],
                 it["taxonomy_version"], it["flagged_at"], run_ts, run_ts, "pending")
                for it in items]
        n_new = 0
        if rows:
            res = psycopg2.extras.execute_values(cur, _UPSERT_SQL, rows, fetch=True)
            n_new = sum(1 for r in res if r["inserted"])

        # Auto-clear: pending rows this run did NOT reproduce. Scoped to the subject on
        # a fold-in (a per-subject run says nothing about other subjects' concerns).
        clear_sql = ("UPDATE review_queue SET review_status = 'auto_cleared', "
                     "reviewed_at = %s, review_notes = 'auto-cleared: no longer "
                     "reproduced by the pipeline' "
                     "WHERE review_status = 'pending' AND last_seen_at < %s")
        clear_params = [run_ts, run_ts]
        if args.subject is not None:
            clear_sql += " AND subject_id = %s"
            clear_params.append(args.subject)
        cur.execute(clear_sql, clear_params)
        n_cleared = cur.rowcount

        cur.execute(_DIGEST_SQL)
        d = cur.fetchone()
        digest = {
            "n_queued": d["n_queued"],
            "n_high": d["n_high"],
            "n_fail": d["n_fail"],
            "review_url": REVIEW_URL,
            "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        n_proj = _project_jsonl(cur)
        conn.commit()
    finally:
        conn.close()

    published = False
    if (n_new or n_cleared) and not args.no_publish:
        published = _publish(digest, args.broker_host, args.broker_port, args.topic)

    by_role: dict[str, int] = {}
    for it in items:
        by_role[it["source_role"]] = by_role.get(it["source_role"], 0) + 1
    print("\n" + "=" * 55)
    print("C5 CLINICIAN REVIEW QUEUE")
    print(f"  live flagged concerns : {len(items)}  (latest audit row per group)")
    print(f"  by source             : {dict(sorted(by_role.items()))}")
    print(f"  newly queued          : {n_new}")
    print(f"  auto-cleared          : {n_cleared}  (pending, no longer reproduced)")
    print(f"  total pending         : {digest['n_queued']}  (high {digest['n_high']}, fail {digest['n_fail']})")
    if args.no_publish:
        print("  MQTT digest           : skipped (--no-publish)")
    elif not (n_new or n_cleared):
        print("  MQTT digest           : skipped (queue unchanged)")
    else:
        print(f"  MQTT digest           : {'sent' if published else 'FAILED'} "
              f"-> {args.broker_host}:{args.broker_port} [{args.topic}] (retain=True)")
    print(f"  record  -> Postgres review_queue")
    print(f"  cache   -> {QUEUE_JSONL}  ({n_proj} row(s) projected)")
    if not (n_new or n_cleared):
        print("  nothing new to triage.")


if __name__ == "__main__":
    main()
