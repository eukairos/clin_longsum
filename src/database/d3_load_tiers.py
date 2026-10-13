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
d3_load_tiers.py
================
Phase D3 -- load the assembled tiered view into Postgres for the review UI.

Reads (no LLM, no GPU):
  data/output/longitudinal_view.jsonl   (d_coordinator) -> tiered_view + tiered_episode
  data/output/retention_ledger.jsonl    (b4 served)     -> tiered_ledger (consolidated)
  data/output/retention_ledger.raw.jsonl (b2 lossless)  -> tiered_ledger_raw (source of
                                                           truth; falls back to the served
                                                           ledger when unconsolidated)
  data/interim/incoming_summary.jsonl    (e1)            -> tiered_view incoming_* columns
  data/interim/cohort_admissions.csv                    -> n_total, date span, current admission
                                                           (ESTABLISHED span only under holdout)

Writes (idempotent):
  tiered_view       UPSERT on subject_id. Refreshes pipeline + incoming_* columns;
                    PRESERVES review_* (never clobbers a physician decision).
  tiered_episode    DELETE-then-INSERT per subject -- the episode set is authoritative
                    per load, so an admission that leaves the hot window (e.g. becomes
                    the current episode under holdout) does not linger as a stale row.
  tiered_ledger     DELETE-then-INSERT per subject -- SERVED nodes + flagged (provisional).
  tiered_ledger_raw DELETE-then-INSERT per subject -- the LOSSLESS pre-consolidation
                    ledger, persisted so the high-fidelity source of truth lives in the
                    DB, not solely on disk (b4's LLM grouping is not reproducible).
  retention_dropped DELETE-then-INSERT per subject -- b2's PRUNE AUDIT TRAIL (`dropped[]`,
                    taxonomy v2.11 / FM-5): every extracted fact that matched no routing
                    branch, with the reason the gate declined it. Audit only, never served.

  --prune          after loading, delete tier rows for subjects NOT in the current
                   view jsonl (clears orphans when the cohort is subset/shrunk).

Connection: the shared database.pg helper (PGHOST/PGPORT/PGDATABASE/PGUSER/
PGPASSWORD from PROJECT_ROOT/.env). Tables are NOT created here --
apply src/database/schema.sql in PgAdmin first (project policy: no auto-migration).

    cd C:~/Summarization/clin_longsum/src/database
    python d3_load_tiers.py
    python d3_load_tiers.py --subject 90000123
    python d3_load_tiers.py --subject 90000123 --set-folded-in   # fold-in: set folded_in_at in the same txn
    python d3_load_tiers.py --limit 5
    python d3_load_tiers.py --prune
    python d3_load_tiers.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging

import pandas as pd
import psycopg2
import psycopg2.extras

import _bootstrap  # noqa: F401  (puts src/ on sys.path)
import config
import holdout
import io_utils
from database.pg import get_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("d3_load_tiers")

VIEW_JSONL = config.OUTPUT_DIR / "longitudinal_view.jsonl"
LEDGER_JSONL = config.OUTPUT_DIR / "retention_ledger.jsonl"
RAW_LEDGER_JSONL = config.OUTPUT_DIR / "retention_ledger.raw.jsonl"
INCOMING_JSONL = config.INTERIM_DIR / "incoming_summary.jsonl"
COHORT_CSV = config.INTERIM_DIR / "cohort_admissions.csv"

_read_jsonl = io_utils.read_jsonl        # shared tolerant reader


def _d(s):
    """Parse any date-ish value to a DATE-castable 'YYYY-MM-DD' string, or None.
    The cohort is DAY-FIRST (D/M/YYYY, e.g. '31/5/2182' = 31 May) -- a Singapore-
    locale export -- so dayfirst=True is required. Month-first parsing both NaT's
    every day>12 value AND silently swaps day/month on the rest. ISO inputs are
    unambiguous and unaffected by dayfirst."""
    if s is None:
        return None
    ts = pd.to_datetime(str(s).strip(), format="mixed", dayfirst=True, errors="coerce")
    return None if pd.isna(ts) else ts.strftime("%Y-%m-%d")


# tiered_view: refresh pipeline + incoming_* columns; review_* are intentionally
# NOT in the UPDATE set, so a physician decision survives every reload (a fresh row
# takes the column default review_status='pending_review').
_VIEW_SQL = """
INSERT INTO tiered_view
    (subject_id, n_total_admissions, first_date, last_date, current_hadm_id,
     current_seq, current_service, tldr_md, current_episode_md, prior_admissions_md,
     prior_generated_md, taxonomy_version, generated_at,
     incoming_hadm_id, incoming_seq, incoming_date, incoming_service,
     incoming_bhc_text, incoming_summary_md)
VALUES (%(subject_id)s, %(n_total_admissions)s, %(first_date)s, %(last_date)s,
        %(current_hadm_id)s, %(current_seq)s, %(current_service)s, %(tldr_md)s,
        %(current_episode_md)s, %(prior_admissions_md)s, %(prior_generated_md)s,
        %(taxonomy_version)s, NOW(),
        %(incoming_hadm_id)s, %(incoming_seq)s, %(incoming_date)s, %(incoming_service)s,
        %(incoming_bhc_text)s, %(incoming_summary_md)s)
ON CONFLICT (subject_id) DO UPDATE SET
    n_total_admissions  = EXCLUDED.n_total_admissions,
    first_date          = EXCLUDED.first_date,
    last_date           = EXCLUDED.last_date,
    current_hadm_id     = EXCLUDED.current_hadm_id,
    current_seq         = EXCLUDED.current_seq,
    current_service     = EXCLUDED.current_service,
    tldr_md             = EXCLUDED.tldr_md,
    current_episode_md  = EXCLUDED.current_episode_md,
    prior_admissions_md = EXCLUDED.prior_admissions_md,
    prior_generated_md  = EXCLUDED.prior_generated_md,
    taxonomy_version    = EXCLUDED.taxonomy_version,
    generated_at        = NOW(),
    incoming_hadm_id    = EXCLUDED.incoming_hadm_id,
    incoming_seq        = EXCLUDED.incoming_seq,
    incoming_date       = EXCLUDED.incoming_date,
    incoming_service    = EXCLUDED.incoming_service,
    incoming_bhc_text   = EXCLUDED.incoming_bhc_text,
    incoming_summary_md = EXCLUDED.incoming_summary_md,
    -- GOVERNANCE: the physician columns (edited_prior_admissions_md, prior_edited_*,
    -- prior_pinned*, review_*) are deliberately ABSENT from this SET -- a clinician
    -- edit or decision survives every reload. Only generated columns refresh.
    -- d2 derives tldr_md from the GENERATED prior prose, so when a physician has
    -- overridden that prose the refreshed TL;DR describes text they replaced.
    tldr_stale          = (tiered_view.edited_prior_admissions_md IS NOT NULL);
"""

_EPISODE_SQL = """
INSERT INTO tiered_episode
    (subject_id, hadm_id, admit_seq, admit_date, service, paragraph_md, in_hot_window)
VALUES %s
ON CONFLICT (subject_id, hadm_id) DO UPDATE SET
    admit_seq     = EXCLUDED.admit_seq,
    admit_date    = EXCLUDED.admit_date,
    service       = EXCLUDED.service,
    paragraph_md  = EXCLUDED.paragraph_md,
    in_hot_window = EXCLUDED.in_hot_window;
"""

_LEDGER_SQL = """
INSERT INTO tiered_ledger
    (subject_id, retention_class, salience_tier, text, certainty, current_lifecycle,
     negative_kind, provisional, onset_date, last_confirmed_date, n_mentions,
     source_hadm_ids, fact_ids, provenance)
VALUES %s;
"""

_LEDGER_RAW_SQL = """
INSERT INTO tiered_ledger_raw
    (subject_id, retention_class, salience_tier, text, certainty, current_lifecycle,
     negative_kind, provisional, onset_date, last_confirmed_date, n_mentions,
     source_hadm_ids, fact_ids, provenance)
VALUES %s;
"""

_DROPPED_SQL = """
INSERT INTO retention_dropped
    (subject_id, fact_id, source_hadm_id, admit_seq, text, retention_class,
     salience_tier, certainty, lifecycle, negative_kind, reason, taxonomy_version)
VALUES %s;
"""


def _ledger_rows(pid: int, led: dict) -> list[tuple]:
    """Flatten a ledger dict (nodes + flagged) to tiered_ledger / tiered_ledger_raw rows.
    Stores the full negative_kind enum (NULL | absent_or_negative | resolved_relevant |
    surveillance) -- the DB carries the sense, so a resolved event is distinguishable
    from a documented absence (display-safety)."""
    rows = []
    for prov, bucket in ((False, led.get("nodes", [])), (True, led.get("flagged", []))):
        for nd in bucket:
            rows.append((
                pid, nd.get("retention_class"), nd.get("salience_tier"),
                nd.get("text"), nd.get("certainty"), nd.get("current_lifecycle"),
                nd.get("negative_kind"), prov,
                _d(nd.get("onset_date")), _d(nd.get("last_confirmed_date")),
                nd.get("n_mentions"), nd.get("source_hadm_ids") or [],
                nd.get("fact_ids") or [],
                psycopg2.extras.Json(nd.get("provenance") or {})))
    return rows


def _dropped_rows(pid: int, led: dict) -> list[tuple]:
    """Flatten a ledger dict's `dropped[]` to retention_dropped rows -- b2's prune audit
    trail (FM-5). Read from the RAW ledger: b4 preserves the array untouched, but raw is
    b2's own output and so the authoritative copy. Pre-v2.11 ledgers have no `dropped`
    key and simply yield no rows."""
    rows = []
    tv = led.get("taxonomy_version")
    for d in led.get("dropped", []) or []:
        rows.append((
            pid, d.get("fact_id"), d.get("source_hadm_id"), d.get("admit_seq"),
            d.get("text"), d.get("retention_class"), d.get("salience_tier"),
            d.get("certainty"), d.get("lifecycle"), d.get("negative_kind"),
            d.get("reason"), tv))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", type=int, default=None, help="load one subject_id only")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--prune", action="store_true",
                    help="delete tier rows for subjects not in the current view jsonl")
    ap.add_argument("--dry-run", action="store_true", help="parse + report, no DB writes")
    ap.add_argument("--set-folded-in", action="store_true",
                    help="set folded_in_at=NOW() for loaded subjects IN THE SAME per-patient "
                         "transaction (used by the fold-in so the DB write is atomic)")
    args = ap.parse_args()

    all_views = _read_jsonl(VIEW_JSONL)
    if not all_views:
        raise SystemExit(f"{VIEW_JSONL} not found/empty. Run d_coordinator.py first.")
    keep_ids = sorted({int(v["subject_id"]) for v in all_views})   # authoritative cohort
    ledger_by_pid = {int(l["subject_id"]): l for l in _read_jsonl(LEDGER_JSONL)}
    # Lossless source of truth: the b2 pre-consolidation ledger. Falls back to the served
    # ledger when there is no raw backup (CONSOLIDATE_LEDGER off -> served == raw).
    raw_src = RAW_LEDGER_JSONL if RAW_LEDGER_JSONL.exists() else LEDGER_JSONL
    raw_ledger_by_pid = {int(l["subject_id"]): l for l in _read_jsonl(raw_src)}
    held_hadms = holdout.holdout_hadms()   # populate incoming_* only while still held out
    # incoming_summary.jsonl carries one row per HELD-OUT admission, so a patient with >1
    # holdout has >1 row. Keying a plain dict by subject_id let the LAST row win -- and when
    # the newest holdout had been folded in, that winner was the promoted admission, which
    # line ~235 then nulled, leaving the patient with NO incoming (and absent from the queue)
    # even though an older holdout was still reviewable. Pick a STILL-HELD-OUT row.
    # OLDEST wins (2026-09-27, production ingest): fold-in must go oldest-first (the d1
    # demotion order replay_production.py enforces), and Approve folds in exactly the
    # admission shown here -- so a newer queued discharge must stay hidden until the older
    # one is folded. A row with no admit_seq never wins. No-op when a patient has one holdout.
    incoming_by_pid: dict[int, dict] = {}
    for r in _read_jsonl(INCOMING_JSONL):
        if int(r.get("hadm_id", -1)) not in held_hadms:
            continue                                   # skip promoted/folded holdouts
        pid_r = int(r["subject_id"])
        cur_r = incoming_by_pid.get(pid_r)
        seq_r = r.get("admit_seq")
        seq_r = int(seq_r) if seq_r is not None else float("inf")     # unknown seq never wins
        if cur_r is None or seq_r < cur_r["_seq"]:
            incoming_by_pid[pid_r] = {**r, "_seq": seq_r}             # oldest still-held-out wins

    # Cohort meta on the ESTABLISHED span (holdout removed), so current_*/n_total/
    # span match the established current_episode_md -- the held-out admission is the
    # incoming episode, not the current one.
    cur_meta: dict[int, dict] = {}
    if COHORT_CSV.exists():
        df = pd.read_csv(COHORT_CSV)
        df = holdout.established(df)
        df["admittime"] = pd.to_datetime(df["admittime"], format="mixed", dayfirst=True, errors="coerce")
        df["dischtime"] = pd.to_datetime(df["dischtime"], format="mixed", dayfirst=True, errors="coerce")
        for pid, g in df.groupby("subject_id"):
            g = g.sort_values("admit_seq")
            last = g.iloc[-1]
            fmin, lmax = g["admittime"].min(), g["dischtime"].max()
            cur_meta[int(pid)] = {
                "n_total": int(len(g)),
                "first": None if pd.isna(fmin) else fmin.strftime("%Y-%m-%d"),
                "last": None if pd.isna(lmax) else lmax.strftime("%Y-%m-%d"),
                "hadm": int(last["hadm_id"]) if pd.notna(last.get("hadm_id")) else None,
                "seq": int(last["admit_seq"]) if pd.notna(last.get("admit_seq")) else None,
                "service": (str(last["service"]) if pd.notna(last.get("service")) else None),
            }
    else:
        logger.warning("%s not found -- date span / current admission will be NULL", COHORT_CSV)

    views = all_views
    if args.subject is not None:
        views = [v for v in views if int(v["subject_id"]) == args.subject]
    if args.limit:
        views = views[:args.limit]
    if not views:
        raise SystemExit("no matching patients to load.")

    n_view = n_epi = n_led = n_led_raw = n_incoming = n_dropped = 0
    conn = None if args.dry_run else get_conn()
    try:
        cur = None if args.dry_run else conn.cursor()
        for v in views:
            pid = int(v["subject_id"])
            m = cur_meta.get(pid, {})
            inc = incoming_by_pid.get(pid, {})
            if inc and int(inc.get("hadm_id", -1)) not in held_hadms:
                inc = {}                    # promoted/folded admission -> no longer incoming
            params = {
                "subject_id": pid,
                "n_total_admissions": m.get("n_total", v.get("n_total_admissions")),
                "first_date": m.get("first"), "last_date": m.get("last"),
                "current_hadm_id": m.get("hadm"), "current_seq": m.get("seq"),
                "current_service": m.get("service"),
                "tldr_md": v.get("tldr_md", ""),
                "current_episode_md": v.get("current_episode_md", ""),
                "prior_admissions_md": v.get("prior_admissions_md", ""),
                "prior_generated_md": v.get("prior_generated_md", ""),
                "taxonomy_version": ledger_by_pid.get(pid, {}).get("taxonomy_version"),
                "incoming_hadm_id": inc.get("hadm_id"),
                "incoming_seq": inc.get("admit_seq"),
                "incoming_date": _d(inc.get("admit_date")),
                "incoming_service": inc.get("service"),
                "incoming_bhc_text": inc.get("bhc_text"),
                "incoming_summary_md": inc.get("summary_md"),
            }

            hot = v.get("hot_window", [])
            epi_rows = [(pid, int(p["hadm_id"]), p.get("admit_seq"), _d(p.get("admit_date")),
                         p.get("service"), p.get("paragraph_md", ""), True) for p in hot]

            led = ledger_by_pid.get(pid, {})
            led_rows = _ledger_rows(pid, led)
            raw_led = raw_ledger_by_pid.get(pid, {})
            raw_rows = _ledger_rows(pid, raw_led)
            drop_rows = _dropped_rows(pid, raw_led or led)

            if args.dry_run:
                logger.info("subject=%s  view(1)  episodes(%d)  ledger(%d)  ledger_raw(%d)  "
                            "dropped(%d)  incoming(%s)",
                            pid, len(epi_rows), len(led_rows), len(raw_rows), len(drop_rows),
                            "yes" if inc else "no")
            else:
                cur.execute(_VIEW_SQL, params)
                cur.execute("DELETE FROM tiered_episode WHERE subject_id = %s;", (pid,))
                if epi_rows:
                    psycopg2.extras.execute_values(cur, _EPISODE_SQL, epi_rows)
                cur.execute("DELETE FROM tiered_ledger WHERE subject_id = %s;", (pid,))
                if led_rows:
                    psycopg2.extras.execute_values(cur, _LEDGER_SQL, led_rows)
                cur.execute("DELETE FROM tiered_ledger_raw WHERE subject_id = %s;", (pid,))
                if raw_rows:
                    psycopg2.extras.execute_values(cur, _LEDGER_RAW_SQL, raw_rows)
                cur.execute("DELETE FROM retention_dropped WHERE subject_id = %s;", (pid,))
                if drop_rows:
                    psycopg2.extras.execute_values(cur, _DROPPED_SQL, drop_rows)
                if args.set_folded_in:
                    cur.execute("UPDATE tiered_view SET folded_in_at = NOW() "
                                "WHERE subject_id = %s;", (pid,))
                conn.commit()
            n_view += 1
            n_epi += len(epi_rows)
            n_led += len(led_rows)
            n_led_raw += len(raw_rows)
            n_dropped += len(drop_rows)
            if inc:
                n_incoming += 1

        # Prune orphans: subjects in the DB but absent from the current view jsonl.
        n_pruned = 0
        if args.prune:
            if args.dry_run:
                logger.info("--prune (dry-run): would delete tier rows for subjects "
                            "not in the %d-subject view jsonl", len(keep_ids))
            else:
                for tbl in ("tiered_episode", "tiered_ledger", "tiered_ledger_raw",
                            "retention_dropped", "tiered_view"):
                    cur.execute(
                        f"DELETE FROM {tbl} WHERE NOT (subject_id = ANY(%s));", (keep_ids,))
                    n_pruned += cur.rowcount
                conn.commit()
                logger.info("pruned %d orphan rows across tier tables", n_pruned)
    except psycopg2.errors.UndefinedTable as exc:
        if conn:
            conn.rollback()
        raise SystemExit(
            f"\nTable/column missing: {exc}\nRun the DDL first in PgAdmin:\n"
            f"  {config.SRC_DIR / 'database' / 'd3_tier_tables.sql'}")
    except psycopg2.errors.UndefinedColumn as exc:
        if conn:
            conn.rollback()
        raise SystemExit(
            f"\nColumn missing: {exc}\nThe DB schema is stale -- re-run d3_tier_tables.sql "
            f"in PgAdmin (it DROPs + recreates the tiered_* tables with negative_kind "
            f"and tiered_ledger_raw), then rerun.")
    finally:
        if conn:
            conn.close()

    print("\n" + "=" * 55)
    print("D3 LOAD TIERS" + ("  (DRY RUN -- no writes)" if args.dry_run else ""))
    print(f"  tiered_view rows    : {n_view}")
    print(f"  tiered_episode rows : {n_epi}")
    print(f"  tiered_ledger rows  : {n_led}  (served/consolidated)")
    print(f"  tiered_ledger_raw   : {n_led_raw}  (lossless source of truth)")
    print(f"  retention_dropped   : {n_dropped}  (b2 prune audit trail)")
    print(f"  incoming summaries  : {n_incoming}")
    if not args.dry_run:
        print("\nLoaded. Tiers + incoming persisted for the tiered review UI (Phase E).")


if __name__ == "__main__":
    main()
