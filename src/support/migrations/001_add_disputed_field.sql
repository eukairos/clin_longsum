-- clin_longsum - physician feedback loop
-- Copyright (C) 2026 Eukairos
-- SPDX-License-Identifier: AGPL-3.0-only
--
-- 001_add_disputed_field
-- ======================
-- Adds feedback.disputed_field to support databases created before the column
-- existed.
--
-- WHY THIS IS NEEDED AT ALL. schema_support.sql is fresh-creation form: every
-- statement is CREATE TABLE IF NOT EXISTS, so re-running it against a database
-- whose tables already exist is a no-op - INCLUDING for columns added to the
-- file afterwards. A support DB bootstrapped before disputed_field was added
-- therefore stayed silently one column behind, and the first symptom was a
-- physician seeing "HTTP 500" on the send button.
--
-- WHAT THE COLUMN IS FOR. Urgency rule U-3 fires on a disputed current_lifecycle
-- at T1 - a superseded allergy shown as active. Without knowing WHICH attribute
-- the reporter disputes, that case is indistinguishable from a disputed tier on
-- the same row, which is routine. The gate cannot fire deterministically, so a
-- safety inversion would be triaged as routine.

ALTER TABLE feedback
    ADD COLUMN IF NOT EXISTS disputed_field TEXT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'ck_feedback_disputed_field') THEN
        ALTER TABLE feedback
            ADD CONSTRAINT ck_feedback_disputed_field
            CHECK (disputed_field IS NULL OR disputed_field IN
                   ('salience_tier','retention_class','certainty',
                    'current_lifecycle','negative_kind','onset_date',
                    'last_confirmed_date','text'));
    END IF;
END $$;
