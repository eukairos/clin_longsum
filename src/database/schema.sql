-- clin_longsum - tiered longitudinal clinical summarization
-- Copyright (C) 2026 Eukairos
-- SPDX-License-Identifier: AGPL-3.0-only
--
-- This program is free software: you can redistribute it and/or modify it under
-- the terms of version 3 of the GNU Affero General Public License as published
-- by the Free Software Foundation. This program is distributed WITHOUT ANY
-- WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
-- A PARTICULAR PURPOSE. See the GNU Affero General Public License for details.

-- schema.sql
-- ============================================================================
-- clin_longsum -- full database schema, one file.
--
-- Creates every table the pipeline persists to, in dependency order. Run this
-- ONCE in PgAdmin (or psql) AFTER a4_load_and_embed.py, which creates the base
-- tables (patients, admissions) and the vector extension this file depends on.
-- Project policy: DDL is applied manually, never auto-migrated at startup.
--
-- PgAdmin note: the query tool executes the EDITOR BUFFER, not the file on disk.
-- After editing this file, re-open it in the tab or the old text will re-run.
--
-- IDEMPOTENT + NON-DESTRUCTIVE: every object uses CREATE ... IF NOT EXISTS (tables,
-- indexes, guarded FK adds), so re-running this file is a safe no-op; it never
-- drops a table or wipes data. FRESH-CREATION FORM: every column lives in its CREATE
-- TABLE below (no post-hoc ALTER section), so one pass builds a complete schema.
-- (This differs from the dev tree's per-table DDL,
-- which DROPs+recreates the derived tables for a clean re-extract. A fresh fork
-- starts empty and rebuilds derived data through the pipeline loaders - each
-- does its own per-subject DELETE/UPSERT -- so no drop-and-recreate is needed
-- here.)
--
-- Tables created (grouped by origin):
--   tiered_view / tiered_episode / tiered_ledger / tiered_ledger_raw
--                                     served review surface   (loaded by d3_load_tiers.py)
--   retention_dropped                 B2 prune audit trail    (loaded by d3_load_tiers.py)
--   retention_facts / generation_audit  source-of-truth records (db_persist_source.py)
--   compaction_review / compaction_change  C3 gate + C4 correction provenance
--                                                             (db_persist_compaction.py)
--   promotions                          fold-in approval log    (holdout.promote / db_persist_promotions.py)
--   retention_review                    C2 MISSING/PINNED_IN_ROLLUP re-review flags
--                                                             (db_persist_retention.py)
--   review_queue                        clinician triage queue, live-concern keyed
--                                                             (c5_review_queue.py)
--
-- Precondition: patients + admissions already loaded (run a4 first). tiered_view
-- FKs to patients; the ALTER at the foot fails if any tiered_view subject is
-- absent from patients (they will be aligned on a normal build - tiered_view is
-- empty at create time, so the constraint simply attaches).
--
-- ============================================================================

-- == Guard 1: refuse to run against a protected database ======================
-- Denylist of databases a manual DDL apply must never touch.
-- The dev ledger-of-record is 'longitudinal_bhc'.
DO $$
DECLARE
  -- 'longitudinal_bhc_test'      = the real dev ledger-of-record.
  -- 'longitudinal_bhc_dev'  = RESERVED SENTINEL. It must never be a real database. The
  --   smoke test CREATEs and DROPs this name to prove the guard fires, so pointing the
  --   test at a live database name would destroy it. Keep the sentinel in this list.
  denylist TEXT[] := ARRAY['longitudinal_bhc', 'longitudinal_bhc_dev'];
BEGIN
  IF current_database() = ANY (denylist) THEN
    RAISE EXCEPTION 'Refusing to run schema.sql against a protected database (%).', current_database();
  END IF;
END $$;

-- == Guard 2: base tables must exist (a4_load_and_embed.py creates them) ======
DO $$
BEGIN
  IF to_regclass('public.patients') IS NULL OR to_regclass('public.admissions') IS NULL THEN
    RAISE EXCEPTION 'patients/admissions not found in %. Run a4_load_and_embed.py FIRST - it creates the base tables this schema FKs to.', current_database();
  END IF;
END $$;

-- ############################################################################
-- 1. Tiered serving tables  (d3_load_tiers.py)
-- ############################################################################
-- tiered_view carries ALL columns inline: pipeline-derived + Phase E incoming_*/
-- review_* + sign-off approved_*/summary_version/folded_in_at. d3_load_tiers
-- UPSERTs on subject_id, refreshing pipeline + incoming_* and PRESERVING review_*
-- (a physician decision survives every reload; a fresh row defaults
-- review_status='pending_review').
CREATE TABLE IF NOT EXISTS tiered_view (
    subject_id          INT PRIMARY KEY,
    n_total_admissions  INT,
    first_date          DATE,
    last_date           DATE,
    current_hadm_id     INT,
    current_seq         INT,
    current_service     TEXT,
    tldr_md             TEXT,
    current_episode_md  TEXT,
    prior_admissions_md TEXT,
    -- Tier-3 machine CONTINUATION (Option A two-region seeding). When a physician has edited
    -- the compacted tier, the served text is edited_prior_admissions_md + marker +
    -- prior_generated_md; this column holds that continuation so the API can reconstruct the
    -- served string. GENERATED (d3-refreshed), NOT physician-owned - outside the C5 no-touch set.
    prior_generated_md  TEXT,
    taxonomy_version    TEXT,
    generated_at        TIMESTAMP DEFAULT NOW(),
    -- Phase E: incoming (held-out) admission, pipeline-derived (e1 + a2 holdout)
    incoming_hadm_id    INT,
    incoming_seq        INT,
    incoming_date       DATE,
    incoming_service    TEXT,
    incoming_bhc_text   TEXT,
    incoming_summary_md TEXT,
    -- Phase E: physician review (written by the review UI submit endpoint)
    review_status       TEXT DEFAULT 'pending_review',
    reviewed_by         TEXT,
    reviewed_at         TIMESTAMP,
    edited_summary_md   TEXT,
    physician_notes     TEXT,
    -- Phase E sign-off + fold-in bookkeeping
    --   (formerly added by e_review_schema.sql; inline here)
    approved_summary_md TEXT,
    summary_version     INT DEFAULT 0,
    folded_in_at        TIMESTAMP,
    -- Phase C5: physician edit of the COMPACTED tier (Tier 3, prior_admissions_md).
    -- GOVERNANCE: physician decisions override pipeline output. prior_admissions_md is
    -- a PROJECTION that d3_load_tiers refreshes from the file chain on every fold-in;
    -- the physician's text lives in edited_prior_admissions_md and d3 must NEVER touch
    -- the columns below. Readers use
    --     COALESCE(edited_prior_admissions_md, prior_admissions_md).
    edited_prior_admissions_md TEXT,
    prior_edited_by            TEXT,
    prior_edited_at            TIMESTAMPTZ,
    prior_edit_version         INT DEFAULT 0,
    -- PIN-AND-REQUEUE: an edited span is frozen (never regenerated over). We record the
    -- span membership the edit was written against; when a later fold-in ages a new
    -- admission into that span, membership diverges -- the edit is KEPT and the concern
    -- is re-queued (review_queue.source_role='prior_span_drift') for the physician to
    -- decide. Never a silent clobber, never silent staleness.
    prior_pinned               BOOLEAN DEFAULT FALSE,
    prior_pinned_hadm_ids      INT[],
    -- d2 regenerates the TL;DR from the prior-admissions text, so an edit leaves the
    -- stored TL;DR describing text the physician has since rewritten.
    tldr_stale                 BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS ix_tiered_view_review ON tiered_view (review_status);
CREATE INDEX IF NOT EXISTS ix_tiered_view_pinned ON tiered_view (subject_id) WHERE prior_pinned;

CREATE TABLE IF NOT EXISTS tiered_episode (
    subject_id    INT  NOT NULL,
    hadm_id       INT  NOT NULL,
    admit_seq     INT,
    admit_date    DATE,
    service       TEXT,
    paragraph_md  TEXT,
    in_hot_window BOOLEAN DEFAULT TRUE,
    PRIMARY KEY (subject_id, hadm_id)
);
CREATE INDEX IF NOT EXISTS ix_tiered_episode_subject ON tiered_episode (subject_id);

-- tiered_ledger: SERVED ledger facts (b4-consolidated nodes + flagged); the set
-- the review UI renders.
CREATE TABLE IF NOT EXISTS tiered_ledger (
    id                  SERIAL PRIMARY KEY,
    subject_id          INT NOT NULL,
    retention_class     TEXT,
    salience_tier       TEXT,
    text                TEXT,
    certainty           TEXT,
    current_lifecycle   TEXT,
    negative_kind       TEXT,                    -- NULL | absent_or_negative | resolved_relevant | surveillance
    provisional         BOOLEAN DEFAULT FALSE,   -- TRUE for flagged (suspected/patient-reported)
    onset_date          DATE,
    last_confirmed_date DATE,
    n_mentions          INT,
    source_hadm_ids     INT[],
    fact_ids            TEXT[],
    -- Per-attribute provenance (FM-2): attribute -> fact_id, since a node is assembled
    -- from DIFFERENT members of its cluster (tier from the strongest mention, text/
    -- certainty/lifecycle/class from the latest). Audit only; nothing routes on it.
    provenance          JSONB
);
CREATE INDEX IF NOT EXISTS ix_tiered_ledger_subject ON tiered_ledger (subject_id);
CREATE INDEX IF NOT EXISTS ix_tiered_ledger_tier    ON tiered_ledger (salience_tier);

-- tiered_ledger_raw: LOSSLESS ledger facts (b2 pre-consolidation,
-- retention_ledger.raw.jsonl). Same shape as tiered_ledger. b4's LLM-assisted
-- T3/T4 grouping is not reproducible, so the raw ledger must be recoverable from
-- the DB. Read by audit / recovery / completeness checks; not in the default UI.
CREATE TABLE IF NOT EXISTS tiered_ledger_raw (
    id                  SERIAL PRIMARY KEY,
    subject_id          INT NOT NULL,
    retention_class     TEXT,
    salience_tier       TEXT,
    text                TEXT,
    certainty           TEXT,
    current_lifecycle   TEXT,
    negative_kind       TEXT,
    provisional         BOOLEAN DEFAULT FALSE,
    onset_date          DATE,
    last_confirmed_date DATE,
    n_mentions          INT,
    source_hadm_ids     INT[],
    fact_ids            TEXT[],
    provenance          JSONB                    -- see tiered_ledger.provenance
);
CREATE INDEX IF NOT EXISTS ix_tiered_ledger_raw_subject ON tiered_ledger_raw (subject_id);

-- retention_dropped: the B2 PRUNE AUDIT TRAIL (forensic register FM-5).
-- One row per extracted fact that matched NO routing branch in b2._project_patient: not
-- pinned (is_ledger_member false), not ruled_out, not superseded, not a recurrent_episode.
-- Such a fact previously landed in no ledger array at all, with no counter and no log line,
-- so absence from every array was the only signature - indistinguishable from a fact that
-- was never extracted. The prune itself is unchanged and usually correct (an ordinary T5
-- transient); it is recorded here so it is inspectable.
--
-- NOT SERVED and NOT counted in C2 recall. Audit only.
-- `reason` is one of retention_taxonomy.PRUNE_REASONS; deliberately NOT a CHECK constraint,
-- because the reason vocabulary is owned by the taxonomy (a governance object that versions
-- independently) and a new reason must not require a DDL migration.
-- Source: retention_ledger.raw.jsonl `dropped[]` (falls back to the served ledger).
-- DELETE-then-INSERT per subject, like tiered_ledger_raw.
CREATE TABLE IF NOT EXISTS retention_dropped (
    id               BIGSERIAL   PRIMARY KEY,
    subject_id       INT         NOT NULL,
    fact_id          TEXT,                    -- sha1(source_hadm_id|text); joins retention_facts
    source_hadm_id   INT,
    admit_seq        INT,
    text             TEXT        NOT NULL,
    retention_class  TEXT,
    salience_tier    TEXT,
    certainty        TEXT,
    lifecycle        TEXT,
    negative_kind    TEXT,
    reason           TEXT        NOT NULL,    -- retention_taxonomy.prune_reason()
    taxonomy_version TEXT,                    -- the gate version that made this decision
    loaded_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_rd_dropped_subject ON retention_dropped (subject_id);
CREATE INDEX IF NOT EXISTS ix_rd_dropped_reason  ON retention_dropped (reason);
CREATE INDEX IF NOT EXISTS ix_rd_dropped_factid  ON retention_dropped (fact_id);


-- ############################################################################
-- 2. Source-of-truth records  (db_persist_source.py)
-- ############################################################################
-- retention_facts: one row per b1-extracted fact (the most fundamental source of
-- truth; the ledger is derived from this). Reload unit is per-admission --
-- db_persist_source does DELETE-then-INSERT on (subject_id, hadm_id).
CREATE TABLE IF NOT EXISTS retention_facts (
    id               BIGSERIAL PRIMARY KEY,
    subject_id       INT  NOT NULL,
    hadm_id          INT  NOT NULL,        -- the admission record this fact was extracted under
    admit_seq        INT,
    source_hadm_id   INT,                  -- admission the fact is attributed to (usually == hadm_id)
    fact_id          TEXT,                 -- sha1(source_hadm_id|text); assigned at load if absent
    text             TEXT NOT NULL,
    retention_class  TEXT,
    salience_tier    TEXT,
    certainty        TEXT,
    lifecycle        TEXT,
    occurrence       TEXT,
    negative_kind    TEXT,                 -- NULL | absent_or_negative | resolved_relevant | surveillance
    parent_condition TEXT,
    supersedes       TEXT,                 -- fact_id this fact amends (de-label / explant / status change)
    rationale        TEXT,
    in_ledger        BOOLEAN               -- DERIVED (tax.is_ledger_member) at load; the pin decision
);
CREATE INDEX IF NOT EXISTS ix_rf_subject ON retention_facts (subject_id);
CREATE INDEX IF NOT EXISTS ix_rf_hadm    ON retention_facts (subject_id, hadm_id);
CREATE INDEX IF NOT EXISTS ix_rf_factid  ON retention_facts (fact_id);
CREATE INDEX IF NOT EXISTS ix_rf_pinned  ON retention_facts (subject_id) WHERE in_ledger;

-- generation_audit: one row per role invocation (provenance). audit_sha =
-- sha256 of the canonical record JSON, the idempotent-reload dedup key.
CREATE TABLE IF NOT EXISTS generation_audit (
    id               BIGSERIAL PRIMARY KEY,
    audit_sha        TEXT UNIQUE,          -- dedup key (sha256 of the record)
    role             TEXT,
    provider         TEXT,
    model_id         TEXT,
    model_version    TEXT,
    prompt_version   TEXT,
    taxonomy_version TEXT,
    params           JSONB,
    subject_id       INT,
    input_hadm_ids   INT[],
    output_sha256    TEXT,
    scores           JSONB,
    status           TEXT,                 -- ok | error | flagged
    error            TEXT,
    created_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_ga_subject ON generation_audit (subject_id);
CREATE INDEX IF NOT EXISTS ix_ga_role    ON generation_audit (role);
CREATE INDEX IF NOT EXISTS ix_ga_flagged ON generation_audit (status) WHERE status <> 'ok';


-- ############################################################################
-- 3. Faithfulness gate + correction provenance  (db_persist_compaction.py)
-- ############################################################################
-- compaction_review: one row per subject -- the C3 faithfulness gate over the
-- compacted Prior-Admissions paragraph, plus the C4 correction outcome. UPSERT on
-- subject_id (C3 load refreshes c3_*; C4 load UPDATEs c4_* in place).
CREATE TABLE IF NOT EXISTS compaction_review (
    subject_id            INT PRIMARY KEY,
    covered_hadm_ids      INT[],                -- admissions the paragraph was compacted from

    -- C3 faithfulness gate (deepseek judge over the compacted Prior-Admissions paragraph)
    c3_score              FLOAT,                -- faithfulness_score; NULL = judge_error / un-judged
    n_total_statements    INT,
    n_supported           INT,
    c3_pass               BOOLEAN,              -- NULL = errored / un-gated
    c3_judge_think_mode   BOOLEAN,              -- Step-4 judge: TRUE thinking, FALSE degraded no-think
    c3_judge_degraded     BOOLEAN,              -- any step fell back to no-think (weaker judge -> review)
    c3_judge_attempts     JSONB,                -- per-step attempt index {step1..step4} (1=think,2/3=no-think)
    c3_error              TEXT,
    failed_statements     JSONB,                -- rejected claims (hallucinations) + judge evidence
    entailment_detail     JSONB,                -- full per-statement verdicts (no-think review evidence)
    c3_loaded_at          TIMESTAMPTZ DEFAULT NOW(),

    -- C4 constrained re-compact (only the C3-FAILED paragraphs are corrected)
    corrected             BOOLEAN DEFAULT FALSE,-- did C4 run for this subject
    c4_original_score     FLOAT,
    c4_corrected_score    FLOAT,                -- NULL = re-validate errored
    c4_pass_after         BOOLEAN,
    c4_n_changes          INT,
    c4_unresolved         INT,                  -- claims still failing after correction
    c4_judge_think_mode   BOOLEAN,
    c4_judge_degraded     BOOLEAN,              -- C4 re-validation used a weak no-think judge -> review
    c4_judge_attempts     JSONB,
    c4_status             TEXT,                 -- ok | flagged | error
    c4_loaded_at          TIMESTAMPTZ,
    -- Physician override of the compacted prose (set by the /prior-admissions endpoint), so a
    -- later c3/c4 pass reports against the authoritative text rather than the generated one.
    physician_edited      BOOLEAN DEFAULT FALSE,
    physician_edited_by   TEXT,
    physician_edited_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_cr_c3_fail     ON compaction_review (subject_id) WHERE c3_pass IS FALSE;
CREATE INDEX IF NOT EXISTS ix_cr_c3_degraded ON compaction_review (subject_id) WHERE c3_judge_degraded;
CREATE INDEX IF NOT EXISTS ix_cr_c4_degraded ON compaction_review (subject_id) WHERE c4_judge_degraded;
CREATE INDEX IF NOT EXISTS ix_cr_unresolved  ON compaction_review (subject_id) WHERE c4_unresolved > 0;

-- compaction_change: one row per dropped/corrected claim (the C4 per-claim
-- audit). FK -> compaction_review ON DELETE CASCADE, so it MUST be created after
-- compaction_review. DELETE-then-INSERT per subject.
CREATE TABLE IF NOT EXISTS compaction_change (
    id                  BIGSERIAL PRIMARY KEY,
    subject_id          INT NOT NULL REFERENCES compaction_review(subject_id) ON DELETE CASCADE,
    action              TEXT,                   -- dropped | corrected
    claim               TEXT,                   -- the unsupported claim (verbatim)
    correction          TEXT,                   -- new wording if corrected, else empty
    model_reason        TEXT,                   -- why the compactor changed it
    validator_evidence  TEXT,                   -- the C3 judge's note on the original claim
    resolved_after      BOOLEAN,                -- claim no longer failing after the correction
    judge_degraded      BOOLEAN,                -- C4 re-validation used a weak no-think judge
    source_hadm_ids     INT[],
    -- A physician edit is recorded alongside c4's automated changes, as action='physician_edit'
    -- (claim = superseded text, correction = physician text).
    edited_by           TEXT,
    edited_at           TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_cc_subject    ON compaction_change (subject_id);
CREATE INDEX IF NOT EXISTS ix_cc_action     ON compaction_change (action);
CREATE INDEX IF NOT EXISTS ix_cc_unresolved ON compaction_change (subject_id) WHERE NOT resolved_after;


-- ############################################################################
-- 4. Fold-in approval log  (holdout.promote / db_persist_promotions.py)
-- ############################################################################
-- A promotion is an immutable per-admission approval event, recorded once.
CREATE TABLE IF NOT EXISTS promotions (
    hadm_id      INT PRIMARY KEY,           -- a promotion is per-admission, recorded once
    subject_id   INT NOT NULL,
    promoted_at  TIMESTAMPTZ,               -- when the physician approval folded it in (from the CSV)
    loaded_at    TIMESTAMPTZ DEFAULT NOW()  -- when this row reached the DB
);
CREATE INDEX IF NOT EXISTS ix_promotions_subject ON promotions (subject_id);


-- ############################################################################
-- 5. Retention re-review flags  (db_persist_retention.py)
-- ############################################################################
-- C2 retention-completeness store (post-commit re-review flag). issue MISSING
-- (pinned fact absent from the ledger = genuine loss) | PINNED_IN_ROLLUP
-- (sentinel event filed only as a recurrence count = mis-tag candidate).
CREATE TABLE IF NOT EXISTS retention_review (
    id                  BIGSERIAL   PRIMARY KEY,
    subject_id          INT         NOT NULL,   -- INT (not BIGINT) to match patients.subject_id for the FK
    issue               TEXT        NOT NULL CHECK (issue IN ('MISSING', 'PINNED_IN_ROLLUP')),
    salience_tier       TEXT,
    retention_class     TEXT,
    certainty           TEXT,
    fact_text           TEXT        NOT NULL,
    source_hadm_id      INT,                     -- INT (not BIGINT) to match admissions.hadm_id for the FK
    -- per-subject context (denormalised from the c2 audit, for UI filtering)
    recall              NUMERIC(6, 4),
    complete            BOOLEAN,
    n_pinned            INTEGER,
    n_missing           INTEGER,
    n_pinned_in_rollup  INTEGER,
    taxonomy_version    TEXT,
    prompt_version      TEXT,
    -- re-review workflow (post-commit queue)
    review_status       TEXT        NOT NULL DEFAULT 'pending_review'
                                    CHECK (review_status IN ('pending_review', 'resolved', 'dismissed', 'auto_cleared')),
    reviewed_by         TEXT,
    review_notes        TEXT,
    checked_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Natural key for idempotent per-subject re-persistence on repeated fold-ins.
-- COALESCE guards the nullable source_hadm_id (NULLs are otherwise distinct in a
-- UNIQUE constraint). On conflict the loader refreshes recall/complete/checked_at
-- but PRESERVES review_status, so a physician's triage survives a re-fold.
CREATE UNIQUE INDEX IF NOT EXISTS retention_review_nk
    ON retention_review (subject_id, issue, COALESCE(source_hadm_id, -1), fact_text);
CREATE INDEX IF NOT EXISTS retention_review_subject_idx ON retention_review (subject_id);
CREATE INDEX IF NOT EXISTS retention_review_status_idx  ON retention_review (review_status);


-- ############################################################################
-- 6. Clinician review queue  (c5_review_queue.py)
-- ############################################################################
-- One row per LIVE concern, keyed by review_id = sha1(role|subject|hadm_ids|score
-- magnitude). c5 derives liveness from the LATEST generation_audit row per group
-- (extraction: per subject+admissions; span roles: per subject+role) - so a
-- concern the pipeline stops reproducing no longer re-offers forever.
--
-- UPSERT on review_id refreshes last_seen_at/severity/reason/detail and PRESERVES
-- review_status, so physician triage survives every re-fold. Pending rows whose
-- last_seen_at predates the run are set to 'auto_cleared' (never deleted - the
-- row and its provenance remain queryable).
--
-- subject_id is INTENTIONALLY left unconstrained, for the same reason as
-- generation_audit.subject_id: a flagged audit record may carry a NULL or
-- non-cohort subject, and an FK would reject legitimate provenance.
CREATE TABLE IF NOT EXISTS review_queue (
    review_id        TEXT        PRIMARY KEY,   -- stable concern fingerprint (not a row id)
    subject_id       INT,
    source_role      TEXT,
    severity         TEXT,                      -- high | medium
    reason           TEXT,                      -- human-readable triage line
    hadm_ids         INT[],
    detail           JSONB,                     -- the audit scores block
    prompt_version   TEXT,
    taxonomy_version TEXT,
    flagged_at       TIMESTAMPTZ,               -- generation_audit.created_at of the flag
    queued_at        TIMESTAMPTZ NOT NULL DEFAULT now(),   -- first time c5 saw this concern
    last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),   -- most recent c5 run that reproduced it
    review_status    TEXT        NOT NULL DEFAULT 'pending'
                                 CHECK (review_status IN ('pending', 'resolved', 'dismissed', 'auto_cleared')),
    reviewed_by      TEXT,
    reviewed_at      TIMESTAMPTZ,
    review_notes     TEXT
);
CREATE INDEX IF NOT EXISTS ix_rq_subject  ON review_queue (subject_id);
CREATE INDEX IF NOT EXISTS ix_rq_role     ON review_queue (source_role);
CREATE INDEX IF NOT EXISTS ix_rq_pending  ON review_queue (severity) WHERE review_status = 'pending';


-- review_decisions - APPEND-ONLY history of physician triage decisions.
-- review_queue holds only the LATEST decision, so a re-decision overwrites the prior
-- reviewer, timestamp and notes. For audit defence the trail itself is the artifact:
-- every resolve/dismiss appends a row here in the SAME transaction as the review_queue
-- update. Nothing in the pipeline UPDATEs or DELETEs this table.
CREATE TABLE IF NOT EXISTS review_decisions (
    id              BIGSERIAL PRIMARY KEY,
    review_id       TEXT        NOT NULL,
    subject_id      INT,
    source_role     TEXT,
    decision        TEXT        NOT NULL,   -- 'resolved' | 'dismissed'
    previous_status TEXT,                   -- what this decision superseded
    physician_name  TEXT        NOT NULL,
    notes           TEXT,                   -- mandatory for 'resolved' (enforced in API)
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_rd_review   ON review_decisions (review_id, decided_at DESC);
CREATE INDEX IF NOT EXISTS ix_rd_subject  ON review_decisions (subject_id);
CREATE INDEX IF NOT EXISTS ix_rd_when     ON review_decisions (decided_at DESC);


-- ############################################################################
-- 6b. (removed) In-place upgrades - this file is now the FRESH-CREATION form
-- ############################################################################
-- Earlier revisions kept post-hoc `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements
-- here to retrofit columns onto databases built before those columns existed. Every such
-- column now lives in its CREATE TABLE above (Phase-C5 physician-edit + pin-and-requeue and
-- prior_generated_md on tiered_view; edited_by/edited_at on compaction_change;
-- physician_edited* on compaction_review), so a clean build is complete in a single pass and
-- this section is intentionally empty.
--
-- To upgrade an OLDER database in place, apply the missing columns as a one-off migration
-- (ADD COLUMN IF NOT EXISTS) against that database -- do not re-accrete an ALTER section here.


-- ############################################################################
-- 7. Foreign keys -> patients / admissions (shared parents)
-- ############################################################################
-- Every child subject_id -> patients(subject_id) and every SCALAR hadm_id ->
-- admissions(hadm_id). Array hadm columns (tiered_ledger.source_hadm_ids,
-- compaction_review.covered_hadm_ids, compaction_change.source_hadm_ids,
-- generation_audit.input_hadm_ids) CANNOT be FK'd - SQL has no array-element FK;
-- they remain audit-only join bridges.
--
-- Each ADD is guarded by a NOT EXISTS check on the constraint name, so this whole
-- section is idempotent - safe to re-run with the rest of schema.sql. On a
-- populated table an ADD fails if a child key is absent from the parent; on a
-- fresh (empty) build every table is empty, so the constraints simply attach.
--
-- Precondition: patients + admissions exist (a4). generation_audit.subject_id is
-- INTENTIONALLY left UNCONSTRAINED - audit rows may carry a NULL or non-cohort
-- subject_id, and constraining them would reject legitimate provenance records.
--
-- Verify before a populated-DB apply (each should return 0 rows):
--   SELECT subject_id FROM retention_facts EXCEPT SELECT subject_id FROM patients;
--   SELECT hadm_id    FROM retention_facts EXCEPT SELECT hadm_id    FROM admissions;

DO $$
BEGIN
  -- subject_id -> patients(subject_id) -----------------------------------------
  IF NOT EXISTS (SELECT 1 FROM information_schema.table_constraints WHERE constraint_name='fk_tv_patient'    AND table_name='tiered_view')
    THEN ALTER TABLE tiered_view       ADD CONSTRAINT fk_tv_patient    FOREIGN KEY (subject_id) REFERENCES patients(subject_id); END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.table_constraints WHERE constraint_name='fk_te_patient'    AND table_name='tiered_episode')
    THEN ALTER TABLE tiered_episode    ADD CONSTRAINT fk_te_patient    FOREIGN KEY (subject_id) REFERENCES patients(subject_id); END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.table_constraints WHERE constraint_name='fk_tl_patient'    AND table_name='tiered_ledger')
    THEN ALTER TABLE tiered_ledger     ADD CONSTRAINT fk_tl_patient    FOREIGN KEY (subject_id) REFERENCES patients(subject_id); END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.table_constraints WHERE constraint_name='fk_tlr_patient'   AND table_name='tiered_ledger_raw')
    THEN ALTER TABLE tiered_ledger_raw ADD CONSTRAINT fk_tlr_patient   FOREIGN KEY (subject_id) REFERENCES patients(subject_id); END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.table_constraints WHERE constraint_name='fk_rf_patient'    AND table_name='retention_facts')
    THEN ALTER TABLE retention_facts   ADD CONSTRAINT fk_rf_patient    FOREIGN KEY (subject_id) REFERENCES patients(subject_id); END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.table_constraints WHERE constraint_name='fk_cr_patient'    AND table_name='compaction_review')
    THEN ALTER TABLE compaction_review ADD CONSTRAINT fk_cr_patient    FOREIGN KEY (subject_id) REFERENCES patients(subject_id); END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.table_constraints WHERE constraint_name='fk_prom_patient'  AND table_name='promotions')
    THEN ALTER TABLE promotions        ADD CONSTRAINT fk_prom_patient  FOREIGN KEY (subject_id) REFERENCES patients(subject_id); END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.table_constraints WHERE constraint_name='fk_rr_patient'    AND table_name='retention_review')
    THEN ALTER TABLE retention_review  ADD CONSTRAINT fk_rr_patient    FOREIGN KEY (subject_id) REFERENCES patients(subject_id); END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.table_constraints WHERE constraint_name='fk_rdrop_patient' AND table_name='retention_dropped')
    THEN ALTER TABLE retention_dropped ADD CONSTRAINT fk_rdrop_patient FOREIGN KEY (subject_id) REFERENCES patients(subject_id); END IF;

  -- scalar hadm_id -> admissions(hadm_id) --------------------------------------
  IF NOT EXISTS (SELECT 1 FROM information_schema.table_constraints WHERE constraint_name='fk_rf_adm'        AND table_name='retention_facts')
    THEN ALTER TABLE retention_facts   ADD CONSTRAINT fk_rf_adm        FOREIGN KEY (hadm_id)        REFERENCES admissions(hadm_id); END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.table_constraints WHERE constraint_name='fk_rf_src_adm'    AND table_name='retention_facts')
    THEN ALTER TABLE retention_facts   ADD CONSTRAINT fk_rf_src_adm    FOREIGN KEY (source_hadm_id) REFERENCES admissions(hadm_id); END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.table_constraints WHERE constraint_name='fk_te_adm'        AND table_name='tiered_episode')
    THEN ALTER TABLE tiered_episode    ADD CONSTRAINT fk_te_adm        FOREIGN KEY (hadm_id)        REFERENCES admissions(hadm_id); END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.table_constraints WHERE constraint_name='fk_prom_adm'      AND table_name='promotions')
    THEN ALTER TABLE promotions        ADD CONSTRAINT fk_prom_adm      FOREIGN KEY (hadm_id)        REFERENCES admissions(hadm_id); END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.table_constraints WHERE constraint_name='fk_rr_src_adm'    AND table_name='retention_review')
    THEN ALTER TABLE retention_review  ADD CONSTRAINT fk_rr_src_adm    FOREIGN KEY (source_hadm_id) REFERENCES admissions(hadm_id); END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.table_constraints WHERE constraint_name='fk_rdrop_src_adm' AND table_name='retention_dropped')
    THEN ALTER TABLE retention_dropped ADD CONSTRAINT fk_rdrop_src_adm FOREIGN KEY (source_hadm_id) REFERENCES admissions(hadm_id); END IF;
END $$;
