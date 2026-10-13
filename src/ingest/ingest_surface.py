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
ingest_surface.py
=================
Make each patient's OLDEST still-held-out admission the reviewable incoming, and keep
newer ones queued and hidden (fold-in must go oldest-first).

Why this module has to do it: two stages pick the NEWEST held-out admission -
d3_load_tiers (incoming_by_pid: "newest still-held-out wins") and d_coordinator
(hold...iloc[-1]). d3 only considers admissions that have a row in
incoming_summary.jsonl, so "hidden" here means exactly that: the oldest held-out
admission has an e1 summary row, queued ones do not. Rows for queued admissions are
removed (they are regenerable e1 output) so d3 cannot surface them.

Per run, for every patient with >= 1 established admission and >= 1 held-out:
  1. hide     drop incoming_summary.jsonl rows of queued (non-oldest) admissions
  2. e1       summarise the oldest held-out if it has no summary (e1.summarise_one,
              imported - same model, prompt and AuditRecord as the e1 stage)
  3. d3       reload tiered_view for patients whose incoming changed
  4. reset    if tiered_view still carries the PREVIOUS admission's decision
              (review_status != pending_review AND folded_in_at set), archive it to
              ingest_review_history and reset the row to pending_review. Without this,
              every patient's second-ever incoming stays off the review queue, which
              filters on review_status = 'pending_review'.
Steps 3-4 run under the fold-in advisory lock. Patients with no established admission
are skipped: their first admission is auto-folded by run_ingest, not reviewed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd

import config
import io_utils

SRC = Path(__file__).resolve().parents[1]
D3 = SRC / "database" / "d3_load_tiers.py"
INCOMING_JSONL = config.INTERIM_DIR / "incoming_summary.jsonl"
VIEW_JSONL = config.OUTPUT_DIR / "longitudinal_view.jsonl"


# -- pure helpers (unit-tested) -----------------------------------------------------

def _rec(r) -> dict:
    """e1's record shape for one cohort row (cohort.load() has parsed admittime)."""
    at = r.get("admittime")
    return {
        "subject_id": int(r["subject_id"]),
        "hadm_id": int(r["hadm_id"]),
        "admit_seq": int(r["admit_seq"]) if pd.notna(r.get("admit_seq")) else None,
        "admit_date": None if at is None or pd.isna(at) else pd.Timestamp(at).strftime("%Y-%m-%d"),
        "service": str(r["service"]) if pd.notna(r.get("service")) else None,
        "bhc_text": str(r["bhc_text"]) if pd.notna(r.get("bhc_text")) else "",
    }


def queue_state(df: pd.DataFrame) -> dict[int, dict]:
    """subject_id -> {oldest: e1 rec, queued: [hadm], n_established}. Only patients
    with at least one still-held-out admission."""
    import holdout
    est, hold = holdout.split(df)
    n_est = est.groupby("subject_id").size().to_dict() if len(est) else {}
    out: dict[int, dict] = {}
    for sid, g in hold.groupby("subject_id"):
        g = g.sort_values("admit_seq")
        out[int(sid)] = {
            "oldest": _rec(g.iloc[0]),
            "queued": [int(h) for h in g["hadm_id"].iloc[1:]],
            "n_established": int(n_est.get(sid, 0)),
        }
    return out


def plan_incoming_rows(rows: list[dict], state: dict[int, dict]):
    """(rows to keep, hidden (sid, hadm) keys, recs needing an e1 summary)."""
    queued = {(sid, h) for sid, st in state.items() for h in st["queued"]}

    def k(r):
        return int(r["subject_id"]), int(r["hadm_id"])

    keep = [r for r in rows if k(r) not in queued]
    hidden = sorted(k(r) for r in rows if k(r) in queued)
    have = {k(r) for r in keep if (r.get("summary_md") or "").strip()}
    need = [st["oldest"] for sid, st in sorted(state.items())
            if st["n_established"] > 0
            and (sid, st["oldest"]["hadm_id"]) not in have
            and (st["oldest"]["bhc_text"] or "").strip()]
    return keep, hidden, need


def review_reset_decision(tv: dict | None, surfaced_hadm: int) -> str:
    """What to do with a subject's tiered_view review state once `surfaced_hadm` is its
    incoming. reset_with_archive | clear_folded_flag | awaiting_foldin | none."""
    if not tv or tv.get("incoming_hadm_id") != surfaced_hadm:
        return "none"
    status = tv.get("review_status") or "pending_review"
    folded = tv.get("folded_in_at") is not None
    if status != "pending_review":
        # A decision + folded_in_at while surfaced_hadm is still held out can only be the
        # PREVIOUS admission's. Approved but not folded = that fold-in is still pending
        # (reconcile-foldins owns it) -- never reset under it.
        return "reset_with_archive" if folded else "awaiting_foldin"
    return "clear_folded_flag" if folded else "none"


# -- side-effecting steps --------------------------------------------------------------

def _write_incoming(rows: list[dict]) -> None:
    rows = sorted(rows, key=lambda r: (int(r["subject_id"]), int(r["hadm_id"])))
    tmp = INCOMING_JSONL.with_name(INCOMING_JSONL.name + ".ingest_tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    os.replace(tmp, INCOMING_JSONL)


def _summarise(recs: list[dict]) -> list[dict]:
    import e1_incoming_summary as e1          # heavy import (ollama client) -- only when needed
    outs = []
    try:
        for rec in recs:
            out = e1.summarise_one(rec)
            outs.append(out)
            print(f"[ingest] e1 subject={rec['subject_id']} hadm={rec['hadm_id']} "
                  f"chars_out={len(out['summary_md'])}")
    finally:
        e1._ollama_unload()                   # free VRAM for the next stage
        e1._gpu_cleanup()
    return outs


def _tiered_view(conn, sids: list[int]) -> dict[int, dict]:
    import psycopg2.extras
    if not sids:
        return {}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT subject_id, incoming_hadm_id, review_status, reviewed_by, reviewed_at,
               edited_summary_md, approved_summary_md, physician_notes, summary_version,
               folded_in_at
        FROM tiered_view WHERE subject_id = ANY(%s);""", (sids,))
    out = {int(r["subject_id"]): dict(r) for r in cur.fetchall()}
    cur.close()
    conn.rollback()
    return out


def _latest_promotion(cur, sid: int):
    cur.execute("SELECT hadm_id FROM promotions WHERE subject_id = %s "
                "ORDER BY promoted_at DESC NULLS LAST, loaded_at DESC LIMIT 1;", (sid,))
    r = cur.fetchone()
    return int(r[0]) if r else None


def _apply_reset(cur, run_id, sid: int, hadm: int, tv: dict, decision: str) -> bool:
    if decision == "reset_with_archive":
        cur.execute("""
            UPDATE tiered_view SET review_status = 'pending_review', reviewed_by = NULL,
                   reviewed_at = NULL, edited_summary_md = NULL, approved_summary_md = NULL,
                   physician_notes = NULL, folded_in_at = NULL
            WHERE subject_id = %s AND incoming_hadm_id = %s
              AND review_status = %s AND folded_in_at = %s;""",
                    (sid, hadm, tv["review_status"], tv["folded_in_at"]))
        if cur.rowcount != 1:
            return False                      # state moved under us -- next run re-evaluates
        cur.execute("""
            INSERT INTO ingest_review_history
                (run_id, subject_id, reviewed_hadm_id, next_incoming_hadm_id, review_status,
                 reviewed_by, reviewed_at, edited_summary_md, approved_summary_md,
                 physician_notes, summary_version, folded_in_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);""",
                    (run_id, sid, _latest_promotion(cur, sid), hadm, tv["review_status"],
                     tv["reviewed_by"], tv["reviewed_at"], tv["edited_summary_md"],
                     tv["approved_summary_md"], tv["physician_notes"], tv["summary_version"],
                     tv["folded_in_at"]))
        return True
    if decision == "clear_folded_flag":
        cur.execute("""
            UPDATE tiered_view SET folded_in_at = NULL
            WHERE subject_id = %s AND incoming_hadm_id = %s AND folded_in_at IS NOT NULL
              AND COALESCE(review_status, 'pending_review') = 'pending_review';""", (sid, hadm))
        return cur.rowcount == 1
    return False


def surface(conn, run_id: int | None, dry_run: bool = False) -> dict:
    import cohort
    import foldin_patient
    import ingest_db

    # Only patients with an ASSEMBLED record (in longitudinal_view.jsonl). A patient whose
    # auto fold-in is pending or failed has none: summarising their next admission now
    # would cache a draft that e1 then never refreshes, and d3 could not load it anyway.
    view_pids = {int(v["subject_id"]) for v in io_utils.read_jsonl(VIEW_JSONL)} \
        if VIEW_JSONL.exists() else set()
    all_state = {sid: st for sid, st in queue_state(cohort.load()).items()
                 if st["n_established"] > 0}
    state = {sid: st for sid, st in all_state.items() if sid in view_pids}
    for sid in sorted(set(all_state) - set(state)):
        print(f"[ingest] subject {sid}: no assembled record yet -- not surfaced this run")
    rows = io_utils.read_jsonl(INCOMING_JSONL) if INCOMING_JSONL.exists() else []
    keep, hidden, need = plan_incoming_rows(rows, state)
    counts = {"surface_subjects": len(state), "hidden": len(hidden), "e1_summarised": 0,
              "d3_loaded": 0, "d3_failed": 0, "no_view": len(all_state) - len(state), "surfaced": 0,
              "review_reset": 0, "folded_flag_cleared": 0, "awaiting_foldin": 0}
    tv = _tiered_view(conn, sorted(state))

    if dry_run:
        print(f"[ingest] surface (dry-run): {len(state)} patient(s) with an incoming; "
              f"would hide {len(hidden)} queued summary row(s), summarise {len(need)}")
        for sid, st in sorted(state.items()):
            d = review_reset_decision(tv.get(sid), st["oldest"]["hadm_id"])
            print(f"   subject {sid}: incoming hadm {st['oldest']['hadm_id']} "
                  f"(queued {st['queued'] or '-'}), tiered_view incoming "
                  f"{tv.get(sid, {}).get('incoming_hadm_id')}, review: {d}")
        return counts

    cur = conn.cursor()
    changed: set[int] = set()
    if hidden:
        _write_incoming(keep)
        for sid, hadm in hidden:
            ingest_db.log_action(cur, run_id, "hidden", sid, hadm,
                                 detail={"reason": "queued behind an older held-out admission"})
            changed.add(sid)
        conn.commit()
    if need:
        outs = _summarise(need)
        merged = {(int(r["subject_id"]), int(r["hadm_id"])): r for r in keep}
        for o in outs:
            merged[(int(o["subject_id"]), int(o["hadm_id"]))] = o
            changed.add(int(o["subject_id"]))
        _write_incoming(list(merged.values()))
        with config.AUDIT_JSONL.open("a", encoding="utf-8") as fh:
            for o in outs:
                fh.write(json.dumps(o["audit"]) + "\n")
        counts["e1_summarised"] = len(outs)

    targets = sorted(
sid for sid, st in state.items()
                     if sid in changed or tv.get(sid, {}).get("incoming_hadm_id")
                     != st["oldest"]["hadm_id"])

    with foldin_patient._foldin_advisory_lock(False):
        for sid in targets:
            r = subprocess.run([sys.executable, str(D3), "--subject", str(sid)],
                               cwd=str(SRC), capture_output=True, text=True)
            if r.returncode != 0:
                counts["d3_failed"] += 1
                print(f"[ingest] subject {sid}: d3 FAILED rc={r.returncode}\n{r.stderr[-800:]}")
                continue
            counts["d3_loaded"] += 1

        tv = _tiered_view(conn, sorted(state))
        for sid, st in sorted(state.items()):
            hadm = st["oldest"]["hadm_id"]
            t = tv.get(sid)
            if t and t.get("incoming_hadm_id") == hadm and sid in targets:
                counts["surfaced"] += 1
                ingest_db.log_action(cur, run_id, "surfaced", sid, hadm,
                                     detail={"queued": st["queued"]})
            decision = review_reset_decision(t, hadm)
            if decision == "awaiting_foldin":
                counts["awaiting_foldin"] += 1
            elif decision != "none" and _apply_reset(cur, run_id, sid, hadm, t, decision):
                key = "review_reset" if decision == "reset_with_archive" else "folded_flag_cleared"
                counts[key] += 1
                ingest_db.log_action(cur, run_id, key, sid, hadm,
                                     detail={"previous_status": t.get("review_status")})
        conn.commit()
    cur.close()
    return counts
