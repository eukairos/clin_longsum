-- clin_longsum - physician feedback loop
-- Copyright (C) 2026 Eukairos
-- SPDX-License-Identifier: AGPL-3.0-only
--
-- 002_s1e_incoming_only
-- =====================
-- Widens feedback.s1_subclass to admit 'S1e' (triage_policy v1.2).
--
-- S1e = the reported fact is documented ONLY in the incoming admission. The
-- ledger is built from prior admissions, so it is correctly absent and appears
-- once that admission is folded in. Not a defect.
--
-- WHY A MIGRATION RATHER THAN AN EDIT TO schema_support.sql. The CHECK
-- constraint is the thing that would silently reject the new value at INSERT
-- time, and schema_support.sql is fresh-creation form: re-running it against an
-- existing database changes nothing. Editing the file alone would work on a new
-- install and fail on every existing one, which is exactly the failure that
-- produced an opaque HTTP 500 on a physician's send button in 001.
--
-- The constraint name is not assumed: PostgreSQL auto-names inline CHECKs
-- (feedback_s1_subclass_check) but that is an implementation detail, so the old
-- one is located by reading pg_constraint rather than by guessing.

DO $$
DECLARE
    conname_found TEXT;
BEGIN
    SELECT c.conname INTO conname_found
      FROM pg_constraint c
      JOIN pg_class t ON t.oid = c.conrelid
     WHERE t.relname = 'feedback'
       AND c.contype = 'c'
       AND pg_get_constraintdef(c.oid) ILIKE '%s1_subclass%';

    IF conname_found IS NOT NULL THEN
        EXECUTE format('ALTER TABLE feedback DROP CONSTRAINT %I', conname_found);
    END IF;

    ALTER TABLE feedback
        ADD CONSTRAINT ck_feedback_s1_subclass
        CHECK (s1_subclass IS NULL OR s1_subclass IN ('S1a','S1b','S1c','S1d','S1e'));
END $$;
