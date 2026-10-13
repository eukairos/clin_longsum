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
metrics.py
==========
Continuous-improvement metrics over the feedback loop.

    python -m support.metrics            # human-readable
    python -m support.metrics --json
    python -m support.metrics --window 30   # look-back window in days (default 90)

WHAT THIS IS FOR. A physician's observation resolves to a bounded
piece of work: a defect signature, the exact set of patients carrying it, a
versioned fix, and a verification. These numbers are the evidence.

EVERY METRIC HERE IS MEANT TO INDICT SOMETHING. Each of these has a reading that says the loop, 
the policy or the documentation is failing:

  by_design share rising      -> the GUIDE is failing, not the pipeline. Physicians
                                 are reporting designed behaviour because nothing
                                 told them it was designed.
  urgent share rising         -> the POLICY is miscalibrated. If everything is
                                 urgent, nothing is.
  urgent closed as by_design
    or not_reproducible       -> specific gates are firing on non-defects. This is
                                 the per-rule calibration signal, which is only
                                 readable because every urgent card names its rule.
  unmatched reporters         -> replies are undeliverable. The loop looks healthy
                                 and is silently not closing.
  time to disposition rising  -> the backlog is growing faster than it is worked.
  systematic signatures       -> defects affecting several patients that are still
                                 being handled one card at a time.

NEVER RAISES. Follows the retention_visibility_gaps() idiom in database/pg.py: a
metric that cannot be computed returns None and says so, rather than taking down
the board that displays it.
"""

from __future__ import annotations

import argparse
import json
import logging
from typing import Any, Optional

try:
    from support import service as svc
except ImportError:  # pragma: no cover
    import service as svc

logger = logging.getLogger(__name__)


def _rows(cur, sql: str, params=None) -> list[tuple]:
    try:
        cur.execute(sql, params or ())
        return cur.fetchall()
    except Exception as e:
        logger.warning("metrics: query failed (%s)", e)
        return []


def _one(cur, sql: str, params=None, default=None):
    r = _rows(cur, sql, params)
    return r[0][0] if r and r[0] and r[0][0] is not None else default


def collect(conn, *, window_days: int = 90) -> dict:
    cur = conn.cursor()
    m: dict[str, Any] = {"window_days": window_days}

    # ── volume and standing ────────────────────────────────────────────────────
    m["total"] = _one(cur, "SELECT count(*) FROM feedback;", default=0)
    m["open"] = _one(cur, "SELECT count(*) FROM feedback WHERE state <> 'closed';", default=0)
    m["closed"] = _one(cur, "SELECT count(*) FROM feedback WHERE state = 'closed';", default=0)
    m["by_state"] = {r[0]: r[1] for r in _rows(
        cur, "SELECT state, count(*) FROM feedback GROUP BY state;")}
    m["by_symptom"] = {r[0]: r[1] for r in _rows(
        cur, """SELECT symptom, count(*) FROM feedback GROUP BY symptom
                 ORDER BY count(*) DESC;""")}
    m["by_week"] = [{"week": str(r[0])[:10], "n": r[1]} for r in _rows(
        cur, """SELECT date_trunc('week', created_at) AS w, count(*)
                  FROM feedback
                 WHERE created_at >= NOW() - make_interval(days => %s)
                 GROUP BY w ORDER BY w;""", (window_days,))]

    # ── responsiveness ─────────────────────────────────────────────────────────
    # Time to the first HUMAN action, not to triage: triage is deterministic and
    # instant, so measuring it would flatter the loop while a card sat untouched.
    m["hours_to_first_human_action"] = _one(cur, """
        SELECT round(percentile_cont(0.5) WITHIN GROUP (
                 ORDER BY EXTRACT(EPOCH FROM (e.first_human - f.created_at))/3600)::numeric, 1)
          FROM feedback f
          JOIN (SELECT feedback_id, min(created_at) AS first_human
                  FROM feedback_events WHERE actor_kind = 'physician'
                   AND event_type <> 'created'
                 GROUP BY feedback_id) e ON e.feedback_id = f.feedback_id;""")
    m["hours_to_disposition"] = _one(cur, """
        SELECT round(percentile_cont(0.5) WITHIN GROUP (
                 ORDER BY EXTRACT(EPOCH FROM (closed_at - created_at))/3600)::numeric, 1)
          FROM feedback WHERE closed_at IS NOT NULL;""")
    m["oldest_open_days"] = _one(cur, """
        SELECT round(max(EXTRACT(EPOCH FROM (NOW() - created_at))/86400)::numeric, 1)
          FROM feedback WHERE state <> 'closed';""")

    # ── disposition mix ────────────────────────────────────────────────────────
    m["by_disposition"] = {r[0]: r[1] for r in _rows(
        cur, """SELECT disposition, count(*) FROM feedback
                 WHERE disposition IS NOT NULL GROUP BY disposition
                 ORDER BY count(*) DESC;""")}
    n_disposed = sum(m["by_disposition"].values()) or 0
    by_design = m["by_disposition"].get("by_design", 0)
    m["by_design_share"] = round(by_design / n_disposed, 2) if n_disposed else None
    m["by_design_reading"] = (
        "high - the guide is failing, not the pipeline: physicians are reporting "
        "designed behaviour because nothing told them it was designed"
        if m["by_design_share"] and m["by_design_share"] > 0.3 else "within range")

    # ── policy calibration ─────────────────────────────────────────────────────
    m["urgent_open"] = _one(
        cur, "SELECT count(*) FROM feedback WHERE urgency='urgent' AND state<>'closed';",
        default=0)
    n_urgent = _one(cur, "SELECT count(*) FROM feedback WHERE urgency='urgent';", default=0)
    m["urgent_share"] = round(n_urgent / m["total"], 2) if m["total"] else None
    m["urgent_reading"] = (
        "high - if most reports are urgent, the gates are not discriminating"
        if m["urgent_share"] and m["urgent_share"] > 0.5 else "within range")

    # Per-rule counts, only readable because every urgent card names the rule
    # that fired. A rule that fires constantly, or one that never fires, is a
    # policy defect - and this is the evidence a policy bump is argued from.
    m["rule_fire_counts"] = {r[0]: r[1] for r in _rows(
        cur, """SELECT rule_id, count(DISTINCT feedback_id) FROM feedback_events
                 WHERE rule_id IS NOT NULL GROUP BY rule_id ORDER BY 2 DESC;""")}
    m["urgent_closed_as_non_defect"] = [
        {"rule": r[0], "disposition": r[1], "n": r[2]} for r in _rows(
            cur, """SELECT urgency_rule_id, disposition, count(*)
                      FROM feedback
                     WHERE urgency='urgent' AND state='closed'
                       AND disposition IN ('by_design','not_reproducible')
                     GROUP BY 1,2 ORDER BY 3 DESC;""")]

    # ── systematic defects ─────────────────────────────────────────────────────
    # The number that converts a complaint into scoped work. A signature on
    # several patients is one fix, not several conversations.
    m["systematic"] = [
        {"dedup_key": r[0], "n_patients": r[1], "n_reports": r[2],
         "example": (r[3] or "")[:70], "open": r[4]} for r in _rows(
            cur, """SELECT dedup_key, count(DISTINCT subject_id), count(*),
                           min(fact_text), count(*) FILTER (WHERE state <> 'closed')
                      FROM feedback WHERE dedup_key IS NOT NULL
                     GROUP BY dedup_key HAVING count(DISTINCT subject_id) > 1
                     ORDER BY 2 DESC LIMIT 10;""")]
    m["n_systematic"] = len(m["systematic"])

    # ── evidence quality ───────────────────────────────────────────────────────
    # How often the collector actually settles a report. A rising `unresolved`
    # share means the deterministic probes are losing ground and each card is
    # costing a human trace.
    m["s1_subclass_mix"] = {r[0] or "unresolved": r[1] for r in _rows(
        cur, """SELECT s1_subclass, count(*) FROM feedback
                 WHERE symptom = 'S1' GROUP BY s1_subclass;""")}
    m["probe_verdicts"] = {r[0]: r[1] for r in _rows(
        cur, """SELECT evidence->'missing_probe'->>'verdict', count(*)
                  FROM feedback_evidence
                 WHERE evidence ? 'missing_probe'
                 GROUP BY 1 ORDER BY 2 DESC;""")}

    # ── loop integrity ─────────────────────────────────────────────────────────
    # Undeliverable replies are the quiet killer: the board reads healthy, the
    # cards close, and the physician never hears anything.
    m["unmatched_reporters"] = _one(
        cur, "SELECT count(*) FROM feedback WHERE reporter_id IS NULL;", default=0)
    m["closed_without_delivery"] = _one(cur, """
        SELECT count(*) FROM feedback f
         WHERE f.state='closed' AND NOT EXISTS (
               SELECT 1 FROM outbox o WHERE o.feedback_id=f.feedback_id
                AND o.channel='email' AND o.state='sent');""", default=0)
    m["outbox"] = {f"{r[0]}/{r[1]}": r[2] for r in _rows(
        cur, "SELECT channel, state, count(*) FROM outbox GROUP BY 1,2;")}
    m["gaps"] = svc.loop_gaps(conn)
    m["stuck_outbox"] = svc.outbox_gaps(conn)

    m["policy_version"] = _one(
        cur, "SELECT policy_version FROM symptom_catalog LIMIT 1;")
    return m


def render(m: dict) -> str:
    L: list[str] = []
    a = L.append
    a(f"Feedback loop metrics   policy {m.get('policy_version') or '?'}   "
      f"window {m['window_days']}d")
    a("=" * 68)
    a(f"  reports        {m['total']} total   {m['open']} open   {m['closed']} closed")
    a(f"  by state       {m.get('by_state')}")
    a(f"  by symptom     {m.get('by_symptom')}")
    a("")
    a("Responsiveness")
    a(f"  median hours to first human action : {m.get('hours_to_first_human_action')}")
    a(f"  median hours to disposition        : {m.get('hours_to_disposition')}")
    a(f"  oldest open (days)                 : {m.get('oldest_open_days')}")
    a("")
    a("Disposition")
    a(f"  mix            {m.get('by_disposition') or '(none closed yet)'}")
    a(f"  by_design      {m.get('by_design_share')}  -> {m.get('by_design_reading')}")
    a("")
    a("Policy calibration")
    a(f"  urgent share   {m.get('urgent_share')}  -> {m.get('urgent_reading')}")
    a(f"  urgent open    {m.get('urgent_open')}")
    a(f"  rule fires     {m.get('rule_fire_counts') or '(none)'}")
    for u in m.get("urgent_closed_as_non_defect", []):
        a(f"    ! {u['rule']} fired on {u['n']} card(s) closed as {u['disposition']}")
    a("")
    a(f"Systematic defects ({m.get('n_systematic', 0)} signature(s) on >1 patient)")
    for s in m.get("systematic", []):
        a(f"  {s['n_patients']} patients, {s['n_reports']} reports, {s['open']} open"
          f"  {s['example']!r}")
    if not m.get("systematic"):
        a("  (none yet)")
    a("")
    a("Evidence")
    a(f"  S1 sub-class   {m.get('s1_subclass_mix') or '(none)'}")
    a(f"  probe verdicts {m.get('probe_verdicts') or '(none)'}")
    a("")
    a("Loop integrity")
    a(f"  unmatched reporters (reply undeliverable) : {m.get('unmatched_reporters')}")
    a(f"  closed with no email sent                 : {m.get('closed_without_delivery')}")
    a(f"  outbox                                    : {m.get('outbox')}")
    a(f"  loop gaps                                 : {len(m.get('gaps', []))}")
    a(f"  stuck outbox rows                         : {len(m.get('stuck_outbox', []))}")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="Feedback loop metrics.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--window", type=int, default=90)
    args = ap.parse_args()
    conn = svc.connect()
    try:
        m = collect(conn, window_days=args.window)
    finally:
        conn.close()
    print(json.dumps(m, indent=2, default=str) if args.json else render(m))


if __name__ == "__main__":
    main()
