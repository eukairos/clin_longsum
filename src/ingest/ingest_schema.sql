-- clin_longsum - tiered longitudinal clinical summarization
-- Copyright (C) 2026 Eukairos
-- SPDX-License-Identifier: AGPL-3.0-only
--
-- This program is free software: you can redistribute it and/or modify it under
-- the terms of version 3 of the GNU Affero General Public License as published
-- by the Free Software Foundation. This program is distributed WITHOUT ANY
-- WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
-- A PARTICULAR PURPOSE. See the GNU Affero General Public License for details.

-- ingest_schema.sql
-- ============================================================================
-- Tables owned by the production EHR ingest (src/ingest/). Apply ONCE in PgAdmin
-- AFTER a4_load_and_embed.py and src/database/schema.sql (project policy: DDL is
-- applied manually, never auto-migrated). Idempotent: CREATE ... IF NOT EXISTS only,
-- never drops or rewrites data.
--
--   ingest_runs            one row per poll (watermark, counts, status)
--   ingest_log             one row per record decision (inserted / duplicate / ...)
--   ingest_amendments      MANUAL REVIEW: EHR records the ingest refused to apply
--                          (text changed after insert, out-of-order admission,
--                          admission after a death). Not surfaced in the review UI.
--   ingest_review_history  archive of a folded admission's tiered_view review_*
--                          columns, taken before the next incoming reuses the row
-- ============================================================================

-- == Guard ===================================================================
-- Which databases are protected is a per-deployment setting (LONGSUM_PROTECTED_DBS,
-- enforced by run_ingest.py via database/pg.py); PgAdmin cannot read it, so this file
-- names no real database. 'longitudinal_bhc_dev' is the reserved sentinel shared with
-- schema.sql's guard smoke test - it must never be a real database.
DO $$
DECLARE
  denylist TEXT[] := ARRAY['longitudinal_bhc_dev'];
BEGIN
  IF current_database() = ANY (denylist) THEN
    RAISE EXCEPTION 'Refusing to apply ingest_schema.sql to protected database (%).', current_database();
  END IF;
  IF to_regclass('public.admissions') IS NULL OR to_regclass('public.tiered_view') IS NULL THEN
    RAISE EXCEPTION 'admissions/tiered_view not found in %. Run a4_load_and_embed.py and schema.sql first.', current_database();
  END IF;
END $$;

-- == 1. Poll runs ==============================================================
CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id        BIGSERIAL   PRIMARY KEY,
    source        TEXT        NOT NULL,              -- EHRSource.name
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    status        TEXT        NOT NULL DEFAULT 'running'
                              CHECK (status IN ('running', 'ok', 'partial', 'error')),
    since_ts      TIMESTAMP,                         -- signed_at lower bound used for the fetch
    watermark_to  TIMESTAMP,                         -- highest signed_at seen (next run starts here - overlap)
    n_fetched     INT,
    n_rejected    INT,
    n_inserted    INT,
    counts        JSONB,                             -- every per-action count for the run
    error         TEXT
);
CREATE INDEX IF NOT EXISTS ix_ingest_runs_status ON ingest_runs (status, run_id DESC);

-- == 2. Per-record decisions ===================================================
-- subject_id / hadm_id are deliberately NOT foreign keys: rejected and anomalous
-- records never reach admissions, and their decision must still be recorded.
CREATE TABLE IF NOT EXISTS ingest_log (
    id          BIGSERIAL   PRIMARY KEY,
    run_id      BIGINT      REFERENCES ingest_runs(run_id),
    subject_id  INT,
    hadm_id     INT,
    action      TEXT        NOT NULL CHECK (action IN (
                    'inserted', 'duplicate', 'amendment', 'out_of_order', 'after_death',
                    'rejected', 'inconsistent', 'auto_folded', 'auto_foldin_failed',
                    'surfaced', 'hidden', 'review_reset', 'folded_flag_cleared')),
    admittime   TIMESTAMP,                           -- full EHR timestamps (admissions keeps DATE only)
    dischtime   TIMESTAMP,
    signed_at   TIMESTAMP,
    bhc_sha256  TEXT,
    detail      JSONB,
    logged_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ingest_log_inserted ON ingest_log (hadm_id) WHERE action = 'inserted';
CREATE INDEX IF NOT EXISTS ix_ingest_log_run     ON ingest_log (run_id);
CREATE INDEX IF NOT EXISTS ix_ingest_log_subject ON ingest_log (subject_id);

-- == 3. Amendments / anomalies for MANUAL review ================================
-- Review location: this table. Suggested query:
--   SELECT * FROM ingest_amendments WHERE review_status = 'pending_review' ORDER BY detected_at;
-- Resolve by setting review_status/reviewed_by/reviewed_at/review_notes by hand.
CREATE TABLE IF NOT EXISTS ingest_amendments (
    id                   BIGSERIAL   PRIMARY KEY,
    run_id               BIGINT      REFERENCES ingest_runs(run_id),
    subject_id           INT         NOT NULL,
    hadm_id              INT         NOT NULL,
    kind                 TEXT        NOT NULL CHECK (kind IN ('text_changed', 'out_of_order', 'after_death')),
    stored_sha256        TEXT,                       -- text_changed: sha of admissions.bhc_text
    incoming_sha256      TEXT        NOT NULL,
    incoming_bhc_text    TEXT        NOT NULL,
    incoming_admittime   TIMESTAMP,
    incoming_dischtime   TIMESTAMP,
    signed_at            TIMESTAMP,
    admission_folded_in  BOOLEAN,                    -- text_changed: was the stored admission already folded in
    detail               JSONB,
    detected_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    review_status        TEXT        NOT NULL DEFAULT 'pending_review'
                                     CHECK (review_status IN ('pending_review', 'resolved', 'dismissed')),
    reviewed_by          TEXT,
    reviewed_at          TIMESTAMPTZ,
    review_notes         TEXT
);
-- the same amended text re-polled on later days is recorded once
CREATE UNIQUE INDEX IF NOT EXISTS ux_ingest_amend ON ingest_amendments (hadm_id, kind, incoming_sha256);
CREATE INDEX IF NOT EXISTS ix_ingest_amend_pending ON ingest_amendments (detected_at) WHERE review_status = 'pending_review';

-- == 4. Archive of per-admission review decisions ================================
-- tiered_view holds ONE review_* set per subject. When a folded admission's decision
-- would otherwise block the next incoming from the queue, the ingest copies it here
-- and resets the row to pending_review. Append-only.
CREATE TABLE IF NOT EXISTS ingest_review_history (
    id                   BIGSERIAL   PRIMARY KEY,
    run_id               BIGINT      REFERENCES ingest_runs(run_id),
    subject_id           INT         NOT NULL,
    reviewed_hadm_id     INT,                        -- latest promotion for the subject at archive time
    next_incoming_hadm_id INT,                       -- the admission the reset makes reviewable
    review_status        TEXT,
    reviewed_by          TEXT,
    reviewed_at          TIMESTAMP,
    edited_summary_md    TEXT,
    approved_summary_md  TEXT,
    physician_notes      TEXT,
    summary_version      INT,
    folded_in_at         TIMESTAMP,
    archived_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_ingest_rh_subject ON ingest_review_history (subject_id);
