-- clin_longsum - physician feedback loop
-- Copyright (C) 2026 Eukairos
-- SPDX-License-Identifier: AGPL-3.0-only
--
-- ============================================================================
-- SUPPORT DATABASE SCHEMA  (database: longsum_support)
-- ============================================================================
-- SEPARATE DATABASE, DELIBERATELY. Support state - ticket lifecycle, triage
-- tags, staff identity, correspondence - does not belong in a patient-oriented
-- clinical database. Consequences that follow, and that the design depends on:
--
--   1. NO FOREIGN KEYS ACROSS DATABASES. subject_id is a plain value here.
--      This is also protective: d3_load_tiers.py issues
--      `DELETE FROM tiered_ledger WHERE subject_id = %s` on every fold-in, and
--      a cascading FK would delete reports.
--
--   2. THE CARD CARRIES ITS OWN SNAPSHOT. With no join back to the clinical DB,
--      every fact needed to read a card months later must be copied at capture:
--      taxonomy_version, prompt_version, artifact_generated_at, the axes, the
--      fact_ids. 
--
--   3. ONE SUPPORT DB, NOT ONE PER COHORT. `cohort` names which clinical
--      database a card refers to.
--
-- ANCHORING - tiered_ledger.id is SERIAL and d3 deletes-then-reinserts per
-- subject, so it changes on every fold-in and is USELESS as an anchor. Cards
-- anchor on fact_ids[] (sha1(source_hadm_id|text)[:16], stable by construction
-- in contracts.RetentionFact.assign_fact_id) or, for Completeness-tab reports,
-- on the retention_review natural key.
--
-- Fresh-creation form, matching src/database/schema.sql convention: no
-- ADD COLUMN IF NOT EXISTS. Upgrades require a one-off migration.
--
-- Apply with:  python src/support/bootstrap_support_db.py --create-db
-- ============================================================================


-- ── STAFF DIRECTORY ─────────────────────────────────────────────────────────
-- Minimal by intent. A real deployment reads registration number, subspecialty,
-- credentials and insurance from the hospital EMR/HR directory; none of those
-- are read by this loop, so they stay out. `directory_ref` is the swap seam -
-- the same role external_ref plays for a board tool - so the real directory can
-- supply them later without a schema change.
--
-- SEED WITH SYNTHETIC ROWS (example.invalid). This is staff PII, not patient
-- data and not DUA-covered, but a test database is the wrong home for a
-- colleague's real address.
CREATE TABLE IF NOT EXISTS physicians (
    physician_id  BIGSERIAL   PRIMARY KEY,
    directory_ref TEXT        UNIQUE,          -- EMR/HR key; NULL in the mock
    display_name  TEXT        NOT NULL,
    email         TEXT        NOT NULL,
    role          TEXT        NOT NULL DEFAULT 'clinician'
                  CHECK (role IN ('clinician','engineer','admin')),
    notify_mode   TEXT        NOT NULL DEFAULT 'immediate'
                  CHECK (notify_mode IN ('immediate','digest','none')),
    active        BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Partial unique: a departed physician's row must stay (old cards reference it)
-- without blocking reuse of the address.
CREATE UNIQUE INDEX IF NOT EXISTS ux_physicians_email
    ON physicians (lower(email)) WHERE active;
CREATE INDEX IF NOT EXISTS ix_physicians_role ON physicians (role) WHERE active;


-- ── PATIENT ALIASES ─────────────────────────────────────────────────────────
-- Every physician-visible surface outside the clinical UI - board, email,
-- digest - refers to patients as PT-nnnn. Mapping is resolvable internally only.
-- Costs nothing and means no DUA-covered identifier ever leaves the clinical DB.
CREATE TABLE IF NOT EXISTS subject_alias (
    cohort        TEXT        NOT NULL,
    subject_id    INT         NOT NULL,
    display_alias TEXT        NOT NULL UNIQUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (cohort, subject_id)
);


-- ── SYMPTOM CATALOG ─────────────────────────────────────────────────────────
-- PROJECTION of policy/triage_policy.yaml, seeded at bootstrap so the review UI
-- can populate its picker from the DB. The FILE is authoritative. Same pattern
-- as holdout.promote() writing `promotions` then projecting the CSV.
-- Rows are replaced wholesale on each policy load; never edit in place.
CREATE TABLE IF NOT EXISTS symptom_catalog (
    symptom_id        TEXT        PRIMARY KEY,
    physician_label   TEXT        NOT NULL,
    owner_roles       TEXT[]      NOT NULL,
    defect_class_hint TEXT,
    notes             TEXT,
    sort_order        INT         NOT NULL,
    policy_version    TEXT        NOT NULL,
    loaded_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ── THE CARD ────────────────────────────────────────────────────────────────
-- `state` is lifecycle position; `disposition` is the verdict. Separating them
-- is what lets a board tool be swapped without remapping verdicts onto someone
-- else's lane names.
CREATE TABLE IF NOT EXISTS feedback (
    feedback_id        BIGSERIAL   PRIMARY KEY,

    -- WHO
    reporter_id        BIGINT      REFERENCES physicians(physician_id),
    reporter_name_raw  TEXT,                   -- as typed, before directory match

    -- WHAT THEY SAW
    symptom            TEXT        NOT NULL,   -- FK-free: symptom_catalog is a
                                               -- projection and is replaced wholesale
    report_text        TEXT        NOT NULL,
    -- Which attribute the reporter disputes. Required for S3 (wrong label) and
    -- U-3 keys on it: a disputed current_lifecycle on a T1 row is a safety
    -- lifecycle inversion (superseded allergy shown active), whereas a disputed
    -- salience_tier on the same row is routine. Without this the two are
    -- indistinguishable and U-3 cannot fire deterministically.
    disputed_field     TEXT        CHECK (disputed_field IN
                       ('salience_tier','retention_class','certainty',
                        'current_lifecycle','negative_kind','onset_date',
                        'last_confirmed_date','text')),
    clinical_action_flag  BOOLEAN  NOT NULL DEFAULT FALSE,  -- feeds U-6
    seen_other_patients   BOOLEAN  NOT NULL DEFAULT FALSE,  -- corroborates U-5

    -- WHICH ROW  (see ANCHORING note in header)
    cohort             TEXT        NOT NULL,   -- clinical DB the card refers to
    namespace          TEXT,                   -- LONGSUM_NAMESPACE at capture
    subject_id         INT         NOT NULL,
    anchor_kind        TEXT        NOT NULL
                       CHECK (anchor_kind IN ('ledger_row','retention_flag',
                                              'concern','prose','other')),
    anchor_key         TEXT,                   -- natural key for non-ledger anchors
    fact_ids           TEXT[],                 -- stable anchor for ledger rows
    source_hadm_ids    INT[],

    -- SNAPSHOT OF THE ROW AS SEEN  (no join back; see header note 2)
    fact_text          TEXT,
    salience_tier      TEXT,
    retention_class    TEXT,
    certainty          TEXT,
    current_lifecycle  TEXT,
    negative_kind      TEXT,

    -- SNAPSHOT OF THE ARTIFACT AS SEEN - without these a card cannot be
    -- dispositioned, because a large share of reports turn out to be against
    -- artifacts that predate a fix.
    taxonomy_version   TEXT,
    prompt_version     TEXT,
    artifact_generated_at TIMESTAMPTZ,

    -- TRIAGE OUTPUT  (deterministic; see policy/triage_policy.yaml)
    policy_version     TEXT,
    urgency            TEXT        NOT NULL DEFAULT 'routine'
                       CHECK (urgency IN ('urgent','routine')),
    urgency_rule_id    TEXT,                   -- MUST be set when urgency='urgent'
    s1_subclass        TEXT        CHECK (s1_subclass IN ('S1a','S1b','S1c','S1d')),
    defect_class       TEXT        CHECK (defect_class IN
                       ('clinical_taxonomy','engineering_routing',
                        'prompt_wording','documentation')),
    owner_roles        TEXT[]      NOT NULL DEFAULT '{}',
    dedup_key          TEXT,

    -- LIFECYCLE
    state              TEXT        NOT NULL DEFAULT 'new'
                       CHECK (state IN ('new','triaged','contained',
                                        'in_progress','resolved','closed')),
    disposition        TEXT        CHECK (disposition IN
                       ('accepted_taxonomy','accepted_engineering','accepted_prompt',
                        'by_design','not_reproducible','already_fixed','duplicate')),
    duplicate_of       BIGINT      REFERENCES feedback(feedback_id),
    fix_version        TEXT,                   -- e.g. retention_taxonomy.v2.13
    reply_text         TEXT,                   -- the answer sent to the reporter
    acknowledged_by    BIGINT      REFERENCES physicians(physician_id),

    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    triaged_at         TIMESTAMPTZ,
    closed_at          TIMESTAMPTZ,

    -- An urgent card must always name the rule that fired. Enforced here rather
    -- than in application code so no future caller can bypass it.
    CONSTRAINT ck_urgent_has_rule
        CHECK (urgency <> 'urgent' OR urgency_rule_id IS NOT NULL),

    -- THE CLOSED LOOP, AS A CONSTRAINT. Nothing reaches a terminal state
    -- without a verdict and a written answer - including duplicates and
    -- not-reproducibles. This single line is why a physician who reports gets
    -- a reply, and it is the reason HOW_TO_READ_THE_LEDGER.md section 8 can
    -- promise one.
    CONSTRAINT ck_closed_is_answered
        CHECK (state <> 'closed'
               OR (disposition IS NOT NULL
                   AND reply_text IS NOT NULL
                   AND length(btrim(reply_text)) > 0))
);
CREATE INDEX IF NOT EXISTS ix_feedback_state    ON feedback (state);
CREATE INDEX IF NOT EXISTS ix_feedback_urgency  ON feedback (urgency)
    WHERE state <> 'closed';
CREATE INDEX IF NOT EXISTS ix_feedback_subject  ON feedback (cohort, subject_id);
CREATE INDEX IF NOT EXISTS ix_feedback_dedup    ON feedback (dedup_key);
CREATE INDEX IF NOT EXISTS ix_feedback_reporter ON feedback (reporter_id);
CREATE INDEX IF NOT EXISTS ix_feedback_owner    ON feedback USING GIN (owner_roles);


-- ── APPEND-ONLY TRANSITION LOG ──────────────────────────────────────────────
-- Mirrors review_decisions in the clinical DB. Never updated, never deleted.
-- An urgency downgrade lands here with the acting human named; that is the
-- audit the policy's downgrade rule depends on.
CREATE TABLE IF NOT EXISTS feedback_events (
    event_id      BIGSERIAL   PRIMARY KEY,
    feedback_id   BIGINT      NOT NULL REFERENCES feedback(feedback_id) ON DELETE CASCADE,
    event_type    TEXT        NOT NULL,        -- created | triaged | state_change |
                                               -- urgency_change | comment | replied |
                                               -- acknowledged | linked_duplicate
    from_state    TEXT,
    to_state      TEXT,
    actor_kind    TEXT        NOT NULL DEFAULT 'system'
                  CHECK (actor_kind IN ('physician','system','rule')),
    actor_id      BIGINT      REFERENCES physicians(physician_id),
    rule_id       TEXT,                        -- when actor_kind = 'rule'
    note          TEXT,
    detail        JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_fe_feedback ON feedback_events (feedback_id, created_at);


-- ── EVIDENCE ────────────────────────────────────────────────────────────────
-- Output of support/evidence.py: the S1 decision tree over retention_facts /
-- retention_dropped / tiered_ledger_raw / tiered_ledger, plus the bhc_text grep
-- and live normalize_fact / prune_reason results for S2 and S3.
-- Versioned so a card records WHICH collector produced its evidence; re-running
-- a newer collector appends a row rather than overwriting.
CREATE TABLE IF NOT EXISTS feedback_evidence (
    evidence_id       BIGSERIAL   PRIMARY KEY,
    feedback_id       BIGINT      NOT NULL REFERENCES feedback(feedback_id) ON DELETE CASCADE,
    collector_version TEXT        NOT NULL,
    evidence          JSONB       NOT NULL,
    collected_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_fev_feedback ON feedback_evidence (feedback_id, collected_at DESC);


-- ── OUTBOX ──────────────────────────────────────────────────────────────────
-- One outbox for every outbound side effect: board push, email, digest. Never
-- call an external system inline from a request path. Buys retries, ordering
-- and offline safety, and means a dead SMTP host or board cannot block a
-- physician's report. On swapping board tools, re-drain history against the new
-- adapter to backfill.
--
-- Follows the _publish() idiom in c5_review_queue.py: delivery failure is
-- logged, never raised; the durable row is the source of truth.
CREATE TABLE IF NOT EXISTS outbox (
    outbox_id       BIGSERIAL   PRIMARY KEY,
    channel         TEXT        NOT NULL CHECK (channel IN ('board','email','digest')),
    feedback_id     BIGINT      REFERENCES feedback(feedback_id) ON DELETE CASCADE,
    idempotency_key TEXT        NOT NULL UNIQUE,
    payload         JSONB       NOT NULL,
    state           TEXT        NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending','sent','failed','skipped')),
    attempts        INT         NOT NULL DEFAULT 0,
    last_error      TEXT,
    external_ref    TEXT,                      -- foreign tool's id; the ONLY place
                                               -- a board tool exists in the core
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_outbox_pending ON outbox (channel, created_at)
    WHERE state = 'pending';
