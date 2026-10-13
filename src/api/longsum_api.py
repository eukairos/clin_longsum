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
FastAPI service for longitudinal BHC summarization + physician review
Start from the api directory (simplest; no package path needed):
    cd <repo>/src/api
    uvicorn longsum_api:app --host 127.0.0.1 --port 8000

Endpoints (base):
    GET  /health
    GET  /patient/{subject_id}/summary
    GET  /patient/{subject_id}/admissions
    GET  /admission/{hadm_id}/text
    POST /search/similar

Endpoints (tiered review):
    GET  /tiered/review                       -- tiered review UI (HTML)
    GET  /tiered/review/patient/{subject_id}  -- full tiered view (tiers + episodes + ledger)

Endpoints (C5 clinician triage queue - the review_queue table synced by c5_review_queue.py):
    GET  /tiered/review-queue                 -- pending concerns (filter: subject, severity, status)
    POST /tiered/review-queue/resolve         -- physician resolves / dismisses one concern
"""

import math
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional
import threading

import numpy as np
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer


def safe_float(x):
    if x is None:
        return None
    if isinstance(x, float) and math.isnan(x):
        return None
    return x


# -- Load environment ----------------------------------------------------------
# This file is at <repo>/src/api/longsum_api.py, so the repo root is parents[2].
# Self-contained: no reach-back to a shared parent tree (.env is gitignored).
REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / '.env'
load_dotenv(ENV_PATH)

SRC_DIR = REPO_ROOT / 'src'
FOLDIN_SCRIPT = SRC_DIR / 'foldin_patient.py'   # foldin_patient.py lives in src/ root

# uvicorn is launched from src/api, so src/ is not on sys.path by default. Add it so the
# API can reuse shared helpers (database.pg) instead of re-implementing them here.
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
from database.pg import get_conn as _pg_get_conn, retention_visibility_gaps   # noqa: E402

# NOTE: the feedback and board routers are mounted further down, immediately
# after `app` is created. Mounting cannot happen here - `app` does not exist yet,
# and a try/except around it would swallow the NameError and report the routes as
# unavailable rather than as misplaced.

_foldin_lock = threading.Lock()   # in-process fast-path only; the CROSS-worker/-process guarantee is
                                  # the Postgres advisory lock inside foldin_patient.py (this Lock covers
                                  # a single uvicorn worker; a manual CLI fold-in bypasses it)

def _run_foldin(subject_id: int, hadm_id: int) -> None:
    """Background task: run the per-patient fold-in, SERIALIZED. Concurrent fold-ins
    corrupt the shared ledger/compaction/view JSONL (each does a full read-modify-write
    of promoted_admissions.csv and the *.jsonl) and oversubscribe Ollama."""
    with _foldin_lock:
        try:
            r = subprocess.run(
                [sys.executable, str(FOLDIN_SCRIPT), '--subject', str(subject_id),
                 '--hadm', str(hadm_id)],
                cwd=str(SRC_DIR), capture_output=True, text=True)
            if r.returncode != 0:
                print(f'[foldin {subject_id}] FAILED rc={r.returncode}\n{r.stderr}', flush=True)
            else:
                print(f'[foldin {subject_id}] ok', flush=True)
        except Exception as e:
            print(f'[foldin {subject_id}] spawn error: {e}', flush=True)


MODEL_NAME   = 'Snowflake/snowflake-arctic-embed-l-v2.0'
QUERY_PREFIX = 'query: '

# Two-region Prior-Admissions separator between the physician prefix and the machine
# continuation. MUST match c1_compactor.PRIOR_REGION_MARKER (kept local to avoid importing
# c1, which pulls ollama into the API process).
PRIOR_REGION_MARKER = '\n\n---\n\n'

# -- App state (model loaded once at startup) ----------------------------------
app_state: dict = {}


def get_conn():
    """Open a psycopg2 connection with pgvector registered.

    Delegates to database.pg so the API is covered by the SAME protected-database guard as
    every CLI stage. Serving longitudinal_bhc requires LONGSUM_ALLOW_PROTECTED_DB=1.
    """
    return _pg_get_conn(vector=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load embedding model at startup; release at shutdown."""
    print(f'Loading embedding model: {MODEL_NAME}')
    app_state['model'] = SentenceTransformer(MODEL_NAME)
    app_state['model'].eval()
    print('Model ready')
    yield
    app_state.clear()
    print('Model unloaded')


app = FastAPI(
    title='LongSum API',
    description='Longitudinal BHC summarization service',
    version='2.0',
    lifespan=lifespan,
)

# ── physician feedback loop ───────────────────────────────────────────────────
# Two routers against a SEPARATE support database (longsum_support): the capture
# endpoints used by the review UI, and the triage board.
#
# MOUNTED DEFENSIVELY, ON PURPOSE. The feedback loop is an addition to the
# clinical review surface, not a dependency of it. A support database that is
# absent, un-bootstrapped, or behind on migrations must degrade the reporting
# feature and nothing else - sign-off, Tier 3 edits and concern triage all keep
# working. The routers themselves connect lazily and return 503 rather than
# raising, so this guard only catches genuine import failures.
#
# Both import forms are tried because uvicorn may be launched from src/ or from
# src/api, which changes what 'api.' resolves to.
for _mod, _label in (('feedback_routes', 'feedback'), ('board_routes', 'board')):
    try:
        try:
            _m = __import__(f'api.{_mod}', fromlist=['router'])
        except ImportError:
            _m = __import__(_mod, fromlist=['router'])
        app.include_router(_m.router)
        print(f'[api] {_label} routes mounted')
    except Exception as _e:                       # noqa: BLE001
        print(f'[api] {_label} routes NOT mounted ({type(_e).__name__}: {_e})')

# -- Pydantic models -----------------------------------------------------------
class SimilarRequest(BaseModel):
    query_text: str
    n_results: int = 5
    exclude_subject_id: Optional[int] = None


class RetentionResolve(BaseModel):
    physician_name: str
    disposition: str = 'resolved'          # 'resolved' (accepted/noted) | 'dismissed' (mis-tag)
    notes: Optional[str] = None
    ids: Optional[list[int]] = None         # specific flag ids; omit = all pending for the subject

# -- Original endpoints --------------------------------------------------------

@app.get('/health')
def health():
    """Confirm DB connectivity and return basic counts."""
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute('SELECT current_database();')
        db_name = cur.fetchone()[0]
        cur.execute('SELECT COUNT(*) FROM patients;')
        n_patients = cur.fetchone()[0]
        cur.execute('SELECT COUNT(*) FROM admissions;')
        n_admissions = cur.fetchone()[0]
        cur.execute('SELECT COUNT(*) FROM admissions WHERE embedding IS NOT NULL;')
        n_embedded = cur.fetchone()[0]
        cur.execute("""
            SELECT COUNT(*) FROM tiered_view
            WHERE incoming_hadm_id IS NOT NULL
              AND COALESCE(review_status, 'pending_review') = 'pending_review';
        """)
        n_pending = cur.fetchone()[0]
        cur.close()
        conn.close()
        return {
            'status':       'ok',
            'db_name':      db_name,
            'db':           'connected',
            'n_patients':   n_patients,
            'n_admissions': n_admissions,
            'n_embedded':   n_embedded,
            'n_pending_review': n_pending,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'DB error: {e}')


# (removed) GET /patient/{subject_id}/summary - legacy single-document `longitudinal`
# table + dropped snomed_* columns; superseded by GET /tiered/review/patient/{subject_id}.


@app.get('/patient/{subject_id}/admissions')
def get_admissions(subject_id: int):
    """Return admission metadata list for a patient (no bhc_text or embedding)."""
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute(
            """
            SELECT hadm_id, admit_seq, admit_date, discharge_date,
                   service, admission_type, discharge_location,
                   hospital_expire_flag, is_holdout,
                   dedup_jaccard_to_prev,
                   (embedding IS NOT NULL) AS has_embedding
            FROM admissions
            WHERE subject_id = %s
            ORDER BY admit_seq;
            """,
            (subject_id,)
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        if not rows:
            raise HTTPException(status_code=404,
                                detail=f'No admissions found for subject_id={subject_id}')
        return {
            'subject_id': subject_id,
            'admissions': [
                {
                    'hadm_id':               r[0],
                    'admit_seq':             r[1],
                    'admit_date':            str(r[2]),
                    'discharge_date':        str(r[3]),
                    'service':               r[4],
                    'admission_type':        r[5],
                    'discharge_location':    r[6],
                    'hospital_expire_flag':  r[7],
                    'is_holdout':            r[8],
                    'dedup_jaccard_to_prev': safe_float(r[9]),
                    'has_embedding':         r[10],
                }
                for r in rows
            ],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'DB error: {e}')


@app.get('/admission/{hadm_id}/text')
def get_admission_text(hadm_id: int):
    """Return BHC text and metadata for a single admission."""
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute(
            """
            SELECT hadm_id, subject_id, admit_seq, admit_date, discharge_date,
                   service, admission_type, discharge_location,
                   hospital_expire_flag, is_holdout,
                   bhc_text, bhc_delta
            FROM admissions
            WHERE hadm_id = %s;
            """,
            (hadm_id,)
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            raise HTTPException(status_code=404,
                                detail=f'No admission found for hadm_id={hadm_id}')
        return {
            'hadm_id':              row[0],
            'subject_id':           row[1],
            'admit_seq':            row[2],
            'admit_date':           str(row[3]),
            'discharge_date':       str(row[4]),
            'service':              row[5],
            'admission_type':       row[6],
            'discharge_location':   row[7],
            'hospital_expire_flag': row[8],
            'is_holdout':           row[9],
            'bhc_text':             row[10],
            'bhc_delta':            row[11],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'DB error: {e}')


@app.post('/search/similar')
def search_similar(req: SimilarRequest):
    """
    Embed query_text (with 'query: ' prefix) and return the
    top-n most similar admissions by cosine similarity.
    """
    if not req.query_text.strip():
        raise HTTPException(status_code=400, detail='query_text must not be empty')

    model = app_state.get('model')
    if model is None:
        raise HTTPException(status_code=503, detail='Embedding model not loaded')

    try:
        query_vec = model.encode(
            QUERY_PREFIX + req.query_text,
            normalize_embeddings=True,
        ).astype(np.float32)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Encoding error: {e}')

    try:
        conn = get_conn()
        cur  = conn.cursor()

        exclude_clause = (
            'AND subject_id != %s' if req.exclude_subject_id is not None else ''
        )

        params = [query_vec]
        if req.exclude_subject_id is not None:
            params.append(req.exclude_subject_id)
        params += [query_vec, req.n_results]

        cur.execute(
            f"""
            SELECT a.hadm_id, a.subject_id, a.admit_seq,
                   a.admit_date, a.discharge_date, a.service,
                   1 - (a.embedding <=> %s::vector) AS cosine_sim
            FROM admissions a
            WHERE a.embedding IS NOT NULL
              {exclude_clause}
            ORDER BY a.embedding <=> %s::vector
            LIMIT %s;
            """,
            params
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return {
            'query_text': req.query_text,
            'n_results':  len(rows),
            'results': [
                {
                    'hadm_id':        r[0],
                    'subject_id':     r[1],
                    'admit_seq':      r[2],
                    'admit_date':     str(r[3]),
                    'discharge_date': str(r[4]),
                    'service':        r[5],
                    'cosine_sim':     round(float(r[6]), 4),
                }
                for r in rows
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'DB error: {e}')

@app.post('/tiered/review/reconcile-foldins')
def reconcile_foldins(background_tasks: BackgroundTasks):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        SELECT subject_id, incoming_hadm_id FROM tiered_view
        WHERE review_status IN ('approved','approved_with_edit')
          AND folded_in_at IS NULL AND incoming_hadm_id IS NOT NULL;
    """)
    stuck = cur.fetchall(); cur.close(); conn.close()
    for sid, hadm in stuck:
        background_tasks.add_task(_run_foldin, int(sid), int(hadm))
    return {'scheduled': len(stuck), 'subjects': [int(s) for s, _ in stuck]}

# Tiered review endpoints (reads the D3 tier tables) -----
# tiered_view + tiered_episode + tiered_ledger, all keyed
# on subject_id (FK -> patients).

@app.get('/tiered/review', include_in_schema=False)
def tiered_review_ui():
    """Serve the tiered physician review web UI."""
    html_path = Path(__file__).parent / 'tiered_review.html'
    if not html_path.exists():
        raise HTTPException(status_code=404, detail='tiered_review.html not found')
    return HTMLResponse(content=html_path.read_text(encoding='utf-8'))


@app.get('/tiered/review/queue')
def get_tiered_queue():
    """
    Physician discharge tasklist: patients with an incoming (held-out) admission
    still pending review. One row per discharge episode, newest discharge first.
    Reviewed patients drop off. Counts: ledger nodes + flagged (provisional) facts.
    """
    try:
        conn = get_conn()
        cur  = conn.cursor()
        # Retention pending counts, only if the table exists (to_regclass -> NULL if not).
        ret_cols = "0 AS n_retention, 0 AS n_ret_missing"
        ret_join = ""
        cur.execute("SELECT to_regclass('public.retention_review');")
        if cur.fetchone()[0] is not None:
            ret_cols = ("COALESCE(rr.n_retention, 0)   AS n_retention, "
                        "COALESCE(rr.n_ret_missing, 0) AS n_ret_missing")
            ret_join = """
            LEFT JOIN (
                SELECT subject_id,
                       COUNT(*)                                 AS n_retention,
                       COUNT(*) FILTER (WHERE issue='MISSING')  AS n_ret_missing
                FROM retention_review
                WHERE review_status = 'pending_review'
                GROUP BY subject_id
            ) rr ON rr.subject_id = tv.subject_id"""
        cur.execute(f"""
            SELECT
                tv.subject_id,
                tv.n_total_admissions,
                tv.incoming_hadm_id,
                tv.incoming_seq,
                tv.incoming_date,
                tv.incoming_service,
                tv.taxonomy_version,
                tv.review_status,
                COALESCE(l.n_nodes, 0)   AS n_nodes,
                COALESCE(l.n_flagged, 0) AS n_flagged,
                {ret_cols}
            FROM tiered_view tv
            LEFT JOIN (
                SELECT subject_id,
                       COUNT(*) FILTER (WHERE NOT provisional) AS n_nodes,
                       COUNT(*) FILTER (WHERE provisional)     AS n_flagged
                FROM tiered_ledger GROUP BY subject_id
            ) l ON l.subject_id = tv.subject_id{ret_join}
            WHERE tv.incoming_hadm_id IS NOT NULL
              AND COALESCE(tv.review_status, 'pending_review') = 'pending_review'
            ORDER BY tv.incoming_date DESC NULLS LAST, tv.subject_id;
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return {
            'count': len(rows),
            'queue': [
                {
                    'subject_id':         r[0],
                    'n_total_admissions': r[1],
                    'incoming_hadm_id':   r[2],
                    'incoming_seq':       r[3],
                    'incoming_date':      str(r[4]) if r[4] else None,
                    'incoming_service':   r[5],
                    'taxonomy_version':   r[6],
                    'review_status':      r[7],
                    'n_nodes':            r[8],
                    'n_flagged':          r[9],
                    'n_retention':        r[10],
                    'n_ret_missing':      r[11],
                }
                for r in rows
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'DB error: {e}')

@app.get('/tiered/review/retention-queue')
def get_retention_queue():
    """Patients with outstanding c2 retention flags (pending_review), regardless of whether
    they have an incoming discharge. This is the POST-COMMIT surface: a fold-in can age a
    prior admission into the compacted span and surface a loss AFTER the discharge was
    approved and dropped off the discharge tasklist. MISSING-first, then most recent."""
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT to_regclass('public.retention_review');")
        if cur.fetchone()[0] is None:
            cur.close(); conn.close()
            return {'count': 0, 'queue': []}
        cur.execute("""
            SELECT rr.subject_id,
                   COUNT(*)                                       AS n_retention,
                   COUNT(*) FILTER (WHERE rr.issue='MISSING')     AS n_ret_missing,
                   (array_agg(rr.recall ORDER BY rr.checked_at DESC))[1] AS recall,
                   MAX(rr.checked_at)                             AS last_checked,
                   tv.n_total_admissions, tv.last_date, tv.current_service
            FROM retention_review rr
            LEFT JOIN tiered_view tv ON tv.subject_id = rr.subject_id
            WHERE rr.review_status = 'pending_review'
            GROUP BY rr.subject_id, tv.n_total_admissions, tv.last_date, tv.current_service
            ORDER BY (COUNT(*) FILTER (WHERE rr.issue='MISSING')) DESC, MAX(rr.checked_at) DESC;
        """)
        # Read-time half of the retention-visibility guard:
        # a write-time assertion lives INSIDE the stage it checks, so it cannot detect that
        # stage never running - the failure this guards against. This runs whenever
        # a physician opens the tab. Advisory only, never fatal: degrading to 'no warning'
        # is bad, but degrading to 'no tab' is worse.
        gaps = retention_visibility_gaps(conn)
        rows = cur.fetchall(); cur.close(); conn.close()
        return {
            'count': len(rows),
            'visibility_gaps': gaps,
            'queue': [
                {
                    'subject_id':         r[0],
                    'n_retention':        r[1],
                    'n_ret_missing':      r[2],
                    'recall':             float(r[3]) if r[3] is not None else None,
                    'last_checked':       str(r[4]) if r[4] else None,
                    'n_total_admissions': r[5],
                    'last_date':          str(r[6]) if r[6] else None,
                    'current_service':    r[7],
                }
                for r in rows
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'DB error: {e}')


@app.post('/tiered/review/patient/{subject_id}/retention/resolve')
def resolve_retention(subject_id: int, req: RetentionResolve):
    """Triage a patient's retention flags. This is an ACKNOWLEDGEMENT, not a data edit: the
    physician has seen the flags and decided ('resolved' = accepted/noted, 'dismissed' = not
    a real concern). Sets pending_review rows to the disposition; auto_cleared/resolved/
    dismissed rows are untouched. Preserved across re-folds (the loader never overwrites a
    non-pending status), so a decided flag does not re-alert even if it re-flags."""
    if req.disposition not in ('resolved', 'dismissed'):
        raise HTTPException(status_code=400, detail="disposition must be 'resolved' or 'dismissed'")
    if not req.physician_name.strip():
        raise HTTPException(status_code=400, detail='physician_name is required')
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('public.retention_review');")
        if cur.fetchone()[0] is None:
            raise HTTPException(status_code=404, detail='retention_review table not present')
        if req.ids:
            cur.execute("""
                UPDATE retention_review SET review_status=%s, reviewed_by=%s, review_notes=%s
                 WHERE subject_id=%s AND review_status='pending_review' AND id = ANY(%s)
                RETURNING id;""",
                (req.disposition, req.physician_name.strip(), req.notes, subject_id, req.ids))
        else:
            cur.execute("""
                UPDATE retention_review SET review_status=%s, reviewed_by=%s, review_notes=%s
                 WHERE subject_id=%s AND review_status='pending_review'
                RETURNING id;""",
                (req.disposition, req.physician_name.strip(), req.notes, subject_id))
        n = len(cur.fetchall())
        conn.commit(); cur.close()
        return {'success': True, 'subject_id': subject_id, 'disposition': req.disposition, 'resolved': n}
    except HTTPException:
        conn.rollback(); raise
    except Exception as e:
        conn.rollback(); raise HTTPException(status_code=500, detail=f'DB error: {e}')
    finally:
        conn.close()

@app.get('/tiered/review/patient/{subject_id}')
def get_tiered_patient(subject_id: int):
    """
    Full tiered view for one patient: header + the three single-text tiers
    (TL;DR, current episode, Prior Admissions), the hot-window episode paragraphs
    (newest admit_seq first), and the retention ledger split into nodes vs flagged
    (provisional). Read-only. patients is LEFT-JOINed for n_admissions/conditions.
    """
    try:
        conn = get_conn()
        cur  = conn.cursor()

        cur.execute("""
            SELECT tv.subject_id, tv.n_total_admissions, tv.first_date, tv.last_date,
                   tv.current_hadm_id, tv.current_seq, tv.current_service,
                   tv.tldr_md, tv.current_episode_md, tv.prior_admissions_md,
                   tv.taxonomy_version, tv.generated_at,
                   p.n_admissions, p.conditions_json,
                   tv.incoming_hadm_id, tv.incoming_seq, tv.incoming_date,
                   tv.incoming_service, tv.incoming_bhc_text, tv.incoming_summary_md,
                   tv.review_status, tv.reviewed_by, tv.reviewed_at,
                   tv.edited_summary_md, tv.physician_notes,
                   tv.approved_summary_md, tv.summary_version, tv.folded_in_at,
                   -- APPENDED at the end: existing code indexes this row positionally,
                   -- so new columns must never be inserted mid-list.
                   tv.edited_prior_admissions_md, tv.prior_edited_by, tv.prior_edited_at,
                   tv.prior_edit_version, tv.prior_pinned, tv.prior_pinned_hadm_ids,
                   tv.tldr_stale,
                   tv.prior_generated_md
            FROM tiered_view tv
            LEFT JOIN patients p ON p.subject_id = tv.subject_id
            WHERE tv.subject_id = %s;
        """, (subject_id,))
        v = cur.fetchone()
        if not v:
            raise HTTPException(status_code=404,
                                detail=f'No tiered view for subject_id={subject_id}')

        cur.execute("""
            SELECT hadm_id, admit_seq, admit_date, service, paragraph_md, in_hot_window
            FROM tiered_episode
            WHERE subject_id = %s
            ORDER BY admit_seq DESC;
        """, (subject_id,))
        episodes = [
            {
                'hadm_id':       e[0],
                'admit_seq':     e[1],
                'admit_date':    str(e[2]) if e[2] else None,
                'service':       e[3],
                'paragraph_md':  e[4],
                'in_hot_window': e[5],
            }
            for e in cur.fetchall()
        ]

        cur.execute("""
            SELECT retention_class, salience_tier, text, certainty, current_lifecycle,
                   negative_kind, provisional, onset_date, last_confirmed_date,
                   n_mentions, source_hadm_ids, fact_ids
            FROM tiered_ledger
            WHERE subject_id = %s
            ORDER BY provisional,
                     salience_tier,
                     onset_date          NULLS LAST,
                     last_confirmed_date NULLS LAST,
                     retention_class,
                     text;
        """, (subject_id,))
        nodes, flagged = [], []
        for r in cur.fetchall():
            item = {
                'retention_class':      r[0],
                'salience_tier':        r[1],
                'text':                 r[2],
                'certainty':            r[3],
                'current_lifecycle':    r[4],
                'negative_kind':        r[5],
                'provisional':          r[6],
                'onset_date':           str(r[7]) if r[7] else None,
                'last_confirmed_date':  str(r[8]) if r[8] else None,
                'n_mentions':           r[9],
                'source_hadm_ids':      r[10] or [],
                'fact_ids':             r[11] or [],
            }
            (flagged if r[6] else nodes).append(item)

        # C2 retention re-review flags (post-commit): pinned facts from a compacted span
        # that went MISSING or are represented only as a rollup count. Guarded by to_regclass
        # so a DB without the table (never migrated) returns [] instead of 500-ing the view.
        retention_flags = []
        cur.execute("SELECT to_regclass('public.retention_review');")
        if cur.fetchone()[0] is not None:
            cur.execute("""
                SELECT rr.id, rr.issue, rr.salience_tier, rr.retention_class, rr.certainty,
                       rr.fact_text, rr.source_hadm_id, rr.recall, rr.n_missing,
                       rr.n_pinned_in_rollup, rr.checked_at
                FROM retention_review rr
                LEFT JOIN tiered_episode te
                       ON te.subject_id = rr.subject_id
                      AND te.hadm_id    = rr.source_hadm_id
                WHERE rr.subject_id = %s AND rr.review_status = 'pending_review'
                ORDER BY CASE rr.issue WHEN 'MISSING' THEN 0 ELSE 1 END,
                         rr.salience_tier,
                         te.admit_date NULLS LAST,
                         te.admit_seq  NULLS LAST,
                         rr.fact_text;
            """, (subject_id,))
            retention_flags = [
                {
                    'id':                 rr[0],
                    'issue':              rr[1],
                    'salience_tier':      rr[2],
                    'retention_class':    rr[3],
                    'certainty':          rr[4],
                    'fact_text':          rr[5],
                    'source_hadm_id':     rr[6],
                    'recall':             float(rr[7]) if rr[7] is not None else None,
                    'n_missing':          rr[8],
                    'n_pinned_in_rollup': rr[9],
                    'checked_at':         str(rr[10]) if rr[10] else None,
                }
                for rr in cur.fetchall()
            ]

        # MUST run before cur.close() below -- it issues its own query on this cursor.
        faithfulness = _faithfulness_detail(cur, subject_id)
        cur.close()
        conn.close()

        # Two-region reconstruction: when a physician edit exists it is the
        # authoritative PREFIX; append the machine continuation (prior_generated_md) below the
        # divider. Rebuild from the LIVE edit. No edit -> serve the pipeline text.
        _edited_prior, _prior_gen = v[28], (v[35] or '')
        prior_effective = (((_edited_prior + PRIOR_REGION_MARKER + _prior_gen) if _prior_gen
                            else _edited_prior) if _edited_prior else v[9])
        return {
            'subject_id':          v[0],
            'n_total_admissions':  v[1],
            'first_date':          str(v[2]) if v[2] else None,
            'last_date':           str(v[3]) if v[3] else None,
            'current_hadm_id':     v[4],
            'current_seq':         v[5],
            'current_service':     v[6],
            'tldr_md':             v[7],
            'current_episode_md':  v[8],
            'prior_admissions_md': v[9],
            'faithfulness':        faithfulness,
            # GOVERNANCE: physician text wins. 'prior_admissions_effective' is the two-region
            # reconstruction (live edit as prefix + machine continuation); the UI can also use
            # the parts (edited_prior_admissions_md + prior_generated_md) to render its own divider.
            'prior_admissions_effective': prior_effective,
            'prior_generated_md':         v[35],
            'edited_prior_admissions_md': v[28],
            'prior_edited_by':            v[29],
            'prior_edited_at':            str(v[30]) if v[30] else None,
            'prior_edit_version':         v[31] or 0,
            'prior_pinned':               bool(v[32]),
            'prior_pinned_hadm_ids':      list(v[33]) if v[33] else [],
            'tldr_stale':                 bool(v[34]),
            'taxonomy_version':    v[10],
            'generated_at':        str(v[11]) if v[11] else None,
            'n_admissions':        v[12],
            'conditions_json':     v[13],
            'incoming': {
                'hadm_id':    v[14],
                'admit_seq':  v[15],
                'admit_date': str(v[16]) if v[16] else None,
                'service':    v[17],
                'bhc_text':   v[18],
                'summary_md': v[19],
            } if v[14] is not None else None,
            'review': {
                'status':            v[20],
                'reviewed_by':       v[21],
                'reviewed_at':       str(v[22]) if v[22] else None,
                'edited_summary_md': v[23],
                'physician_notes':   v[24],
                'approved_summary_md': v[25],
                'summary_version':     v[26] if v[26] is not None else 0,
                'folded_in_at':        str(v[27]) if v[27] else None,
            },
            'episodes':            episodes,
            'ledger':              {'nodes': nodes, 'flagged': flagged},
            'retention_flags':     retention_flags,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'DB error: {e}')


# -- Tiered physician sign-off -------------------------------------------------

class TieredReviewSubmit(BaseModel):
    physician_name: str
    edited_summary_md: str             # the (possibly edited) Tier 1 incoming summary
    physician_notes: Optional[str] = None


def _norm_text(s: Optional[str]) -> str:
    return ' '.join((s or '').split())


@app.post('/tiered/review/patient/{subject_id}/submit')
def submit_tiered_review(subject_id: int, req: TieredReviewSubmit,
                         background_tasks: BackgroundTasks):
    """
    Persist a physician approval of the incoming (held-out) admission's Tier 1
    summary. A single 'Approve' action: the system derives 'approved' vs
    'approved_with_edit' by comparing the submitted text to the LLM draft
    (incoming_summary_md). Writes the approved text to approved_summary_md (and
    edited_summary_md), bumps summary_version, and records the decision.

    The background per-patient fold-in (promotion of the approved admission into the
    established record) is triggered on submit, but takes several minutes to complete.
    """
    if not req.physician_name.strip():
        raise HTTPException(status_code=400, detail='physician_name is required')
    if not req.edited_summary_md.strip():
        raise HTTPException(status_code=400, detail='edited_summary_md is required')

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT incoming_summary_md, incoming_hadm_id
            FROM tiered_view WHERE subject_id = %s;
        """, (subject_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404,
                                detail=f'No tiered view for subject_id={subject_id}')
        draft_md, incoming_hadm_id = row[0], row[1]

        edited = _norm_text(req.edited_summary_md) != _norm_text(draft_md)
        decision = 'approved_with_edit' if edited else 'approved'
        now = datetime.now()
        final_md = req.edited_summary_md.strip()

        # Atomic version bump (COALESCE + RETURNING) - avoids a read-compute-write race on
        # summary_version under concurrent approvals; the whole UPDATE is one transaction.
        cur.execute("""
            UPDATE tiered_view SET
                review_status       = %s,
                reviewed_by         = %s,
                reviewed_at         = %s,
                edited_summary_md   = %s,
                approved_summary_md = %s,
                physician_notes     = %s,
                summary_version     = COALESCE(summary_version, 0) + 1
            WHERE subject_id = %s
            RETURNING summary_version;
        """, (decision, req.physician_name.strip(), now, final_md, final_md,
              req.physician_notes, subject_id))
        new_version = cur.fetchone()[0]
        conn.commit()
        cur.close()

        # Fold-in: promote the approved admission into the established record in the
        # background so the physician can move to the next discharge without waiting.
        if incoming_hadm_id is not None:
            background_tasks.add_task(_run_foldin, subject_id, int(incoming_hadm_id))

        return {
            'success':         True,
            'subject_id':      subject_id,
            'decision':        decision,
            'summary_version': new_version,
            'reviewed_by':     req.physician_name.strip(),
            'reviewed_at':     str(now),
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f'DB error: {e}')
    finally:
        conn.close()


# -- c5 clinician triage queue -------------------------------------------------
# Backed by the review_queue table (schema.sql section 6), synced by
# c5_review_queue.py: one row per LIVE concern, keyed by review_id. c5 owns
# discovery and auto-clearing; these endpoints own the PHYSICIAN decision
# (resolved / dismissed) - which c5's UPSERT deliberately never overwrites.

_RQ_STATUSES = ('pending', 'resolved', 'dismissed', 'auto_cleared')


class ReviewQueueResolve(BaseModel):
    review_id: str
    physician_name: str
    decision: str                       # 'resolved' | 'dismissed'
    notes: Optional[str] = None


def _faithfulness_detail(cur, subject_id: int) -> dict:
    """c3's per-statement verdicts for the compacted span.

    A physician cannot adjudicate "contains unsupported claims" without being shown
    WHICH claims. failed_statements holds the statement plus the judge's evidence, so
    the TRIAGE tab quotes them verbatim instead of reporting a bare score.
    """
    try:
        cur.execute("""
            SELECT c3_score, n_total_statements, n_supported, c3_pass,
                   failed_statements, c3_judge_degraded, corrected, c4_unresolved
              FROM compaction_review
             WHERE subject_id = %s
             ORDER BY c3_loaded_at DESC NULLS LAST LIMIT 1;
        """, (subject_id,))
        r = cur.fetchone()
        if not r:
            return {}
        failed = r[4] or []
        return {
            'score':          r[0],
            'n_total':        r[1],
            'n_supported':    r[2],
            'n_unsupported':  (r[1] - r[2]) if (r[1] is not None and r[2] is not None) else None,
            'pass':           r[3],
            'judge_degraded': r[5],
            'corrected':      r[6],
            'c4_unresolved':  r[7],
            'failed_statements': [
                {'statement': (f or {}).get('statement', ''),
                 'evidence':  (f or {}).get('evidence', '')} for f in failed
            ],
        }
    except Exception as e:
        # Do not fail the whole patient view for a missing verdict, but never swallow it
        # silently either - an empty block here is indistinguishable from 'no concerns'.
        print(f'[faithfulness_detail] subject {subject_id}: {e}')
        return {}


class PriorAdmissionsEdit(BaseModel):
    physician_name: str
    edited_prior_admissions_md: str     # authoritative text; overrides the generated tier
    notes: Optional[str] = None
    review_id: Optional[str] = None     # queue item this edit answers, if launched from TRIAGE


@app.post('/tiered/review/patient/{subject_id}/prior-admissions')
def edit_prior_admissions(subject_id: int, req: PriorAdmissionsEdit):
    """Save a physician edit of the COMPACTED tier (Tier 3) and PIN the span.

    GOVERNANCE: physician-entered text overrides pipeline-generated output. The generated column
    (prior_admissions_md) keeps refreshing from the file chain on every fold-in; this
    writes a separate authoritative column that no automated stage touches. Readers use
    COALESCE(edited_prior_admissions_md, prior_admissions_md).

    PIN-AND-REQUEUE: we record the span membership the edit was written against. A later
    fold-in that ages an admission into that span does NOT overwrite the edit - c5
    detects the divergence and re-queues it as 'prior_span_drift' for the physician.

    Sets tldr_stale: d2 derives the TL;DR from the generated prose, so it now describes
    text that has been replaced.
    """
    text = (req.edited_prior_admissions_md or '').strip()
    if not text:
        raise HTTPException(status_code=400, detail='edited_prior_admissions_md is empty')
    if not req.physician_name.strip():
        raise HTTPException(status_code=400, detail='physician_name is required')

    conn = get_conn()
    try:
        cur = conn.cursor()
        now = datetime.now()
        # Current compacted span -> what we pin against.
        cur.execute("""
            SELECT covered_hadm_ids FROM compaction_review
             WHERE subject_id = %s ORDER BY c3_loaded_at DESC NULLS LAST LIMIT 1;
        """, (subject_id,))
        row = cur.fetchone()
        covered = list(row[0]) if row and row[0] else None

        cur.execute("""
            UPDATE tiered_view SET
                edited_prior_admissions_md = %s,
                prior_edited_by            = %s,
                prior_edited_at            = %s,
                prior_edit_version         = COALESCE(prior_edit_version, 0) + 1,
                prior_pinned               = TRUE,
                prior_pinned_hadm_ids      = %s,
                tldr_stale                 = TRUE
              WHERE subject_id = %s
          RETURNING prior_admissions_md, prior_edit_version;
        """, (text, req.physician_name.strip(), now, covered, subject_id))
        got = cur.fetchone()
        if not got:
            raise HTTPException(status_code=404, detail=f'No tiered_view row for {subject_id}')
        superseded, version = got

        # Provenance, same transaction: recorded alongside c4's automated changes.
        cur.execute("""
            INSERT INTO compaction_change
                (subject_id, action, claim, correction, model_reason,
                 validator_evidence, resolved_after, judge_degraded,
                 source_hadm_ids, edited_by, edited_at)
            VALUES (%s, 'physician_edit', %s, %s, %s, NULL, TRUE, FALSE, %s, %s, %s);
        """, (subject_id, superseded, text,
              (req.notes or '').strip() or 'physician edit of compacted tier',
              covered, req.physician_name.strip(), now))
        cur.execute("""
            UPDATE compaction_review
               SET physician_edited = TRUE, physician_edited_by = %s, physician_edited_at = %s
             WHERE subject_id = %s;
        """, (req.physician_name.strip(), now, subject_id))
        conn.commit()
        cur.close()
        return {
            'success':            True,
            'subject_id':         subject_id,
            'prior_edit_version': version,
            'pinned_hadm_ids':    covered,
            'tldr_stale':         True,
            'edited_by':          req.physician_name.strip(),
            'edited_at':          str(now),
        }
    except HTTPException:
        conn.rollback(); raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f'DB error: {e}')
    finally:
        conn.close()


@app.get('/tiered/review-queue')
def get_review_queue(subject_id: Optional[int] = None,
                     severity: Optional[str] = None,
                     status: str = 'pending'):
    """Triage queue. Defaults to status='pending'; pass status='all' for the full
    history including resolved / dismissed / auto_cleared rows. Guarded by
    to_regclass so a DB that has not had schema.sql applied returns an empty queue
    instead of 500-ing the UI."""
    if status != 'all' and status not in _RQ_STATUSES:
        raise HTTPException(status_code=400,
                            detail=f'status must be "all" or one of {_RQ_STATUSES}')
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('public.review_queue');")
        if cur.fetchone()[0] is None:
            return {'items': [], 'n_pending': 0, 'n_high': 0,
                    'note': 'review_queue table absent -- apply src/database/schema.sql'}

        sql = ("SELECT review_id, subject_id, source_role, severity, reason, hadm_ids, "
               "detail, flagged_at, queued_at, last_seen_at, review_status, reviewed_by, "
               "reviewed_at, review_notes FROM review_queue WHERE TRUE")
        params: list = []
        if status != 'all':
            sql += ' AND review_status = %s'
            params.append(status)
        if subject_id is not None:
            sql += ' AND subject_id = %s'
            params.append(subject_id)
        if severity is not None:
            sql += ' AND severity = %s'
            params.append(severity)
        sql += (" ORDER BY CASE severity WHEN 'high' THEN 0 ELSE 1 END, "
                "subject_id, source_role")
        cur.execute(sql, params)
        items = [
            {
                'review_id':     r[0],
                'subject_id':    r[1],
                'source_role':   r[2],
                'severity':      r[3],
                'reason':        r[4],
                'hadm_ids':      r[5] or [],
                'detail':        r[6] or {},
                'flagged_at':    str(r[7]) if r[7] else None,
                'queued_at':     str(r[8]) if r[8] else None,
                'last_seen_at':  str(r[9]) if r[9] else None,
                'review_status': r[10],
                'reviewed_by':   r[11],
                'reviewed_at':   str(r[12]) if r[12] else None,
                'review_notes':  r[13],
            }
            for r in cur.fetchall()
        ]
        cur.execute("SELECT count(*), count(*) FILTER (WHERE severity = 'high') "
                    "FROM review_queue WHERE review_status = 'pending';")
        n_pending, n_high = cur.fetchone()
        cur.close()
        return {'items': items, 'n_pending': n_pending, 'n_high': n_high}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'DB error: {e}')
    finally:
        conn.close()


@app.post('/tiered/review-queue/resolve')
def resolve_review_queue_item(req: ReviewQueueResolve):
    """Record a physician decision on one queued concern. 'resolved' = the concern was
    real and has been dealt with; 'dismissed' = not a real problem. Either way c5
    preserves the status on every subsequent sync, so the item never re-surfaces -
    unless the underlying score magnitude changes, which mints a NEW review_id (a
    genuinely different concern) and re-alerts.

    An already-decided or auto_cleared row can be re-decided; the previous status is
    returned so the UI can show what was overwritten.
    """
    if req.decision not in ('resolved', 'dismissed'):
        raise HTTPException(status_code=400,
                            detail="decision must be 'resolved' or 'dismissed'")
    if not req.physician_name.strip():
        raise HTTPException(status_code=400, detail='physician_name is required')
    notes = (req.notes or '').strip()
    if req.decision == 'resolved' and not notes:
        raise HTTPException(
            status_code=400,
            detail="notes are required when resolving - the decision trail is the audit "
                   "artifact. Use 'dismissed' if this was a false alarm.")

    conn = get_conn()
    try:
        cur = conn.cursor()
        now = datetime.now()
        # ONE statement: the read is a FOR UPDATE CTE, so a concurrent decision on the
        # same item cannot interleave between read and write (the previous two-statement
        # SELECT-then-UPDATE was last-write-wins and misreported previous_status).
        cur.execute("""
            WITH prev AS (
                SELECT review_id, review_status
                  FROM review_queue
                 WHERE review_id = %s
                   FOR UPDATE
            )
            UPDATE review_queue rq SET
                review_status = %s,
                reviewed_by   = %s,
                reviewed_at   = %s,
                review_notes  = COALESCE(NULLIF(%s, ''), rq.review_notes)
              FROM prev
             WHERE rq.review_id = prev.review_id
          RETURNING rq.subject_id, rq.source_role, prev.review_status;
        """, (req.review_id, req.decision, req.physician_name.strip(), now, notes))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404,
                                detail=f'No queue item review_id={req.review_id}')
        subject_id, source_role, previous_status = row
        # Append-only trail, SAME transaction. review_queue keeps only the latest
        # decision; this keeps every decision ever made, for audit defence.
        cur.execute("""
            INSERT INTO review_decisions
                (review_id, subject_id, source_role, decision, previous_status,
                 physician_name, notes, decided_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
        """, (req.review_id, subject_id, source_role, req.decision, previous_status,
              req.physician_name.strip(), notes or None, now))
        conn.commit()
        cur.close()
        return {
            'success':         True,
            'review_id':       req.review_id,
            'subject_id':      subject_id,
            'source_role':     source_role,
            'previous_status': previous_status,
            'review_status':   req.decision,
            'reviewed_by':     req.physician_name.strip(),
            'reviewed_at':     str(now),
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f'DB error: {e}')
    finally:
        conn.close()
