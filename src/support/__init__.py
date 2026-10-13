# clin_longsum - physician feedback loop
# Copyright (C) 2026 Eukairos
# SPDX-License-Identifier: AGPL-3.0-only

"""
support
=======
Physician feedback loop for the clin_longsum review UI.

HOW_TO_READ_THE_LEDGER.md section 8 tells physicians to report ledger issues so
they are fixed at source. This package is what makes that promise true: capture
in the review UI, deterministic triage and routing, a board over a SEPARATE
support database, and a written reply to the reporter on every disposition.

Layout
------
    policy/triage_policy.yaml   governance object - symptoms, urgency rules,
                                routing, states. Versioned and clinically
                                signed off, exactly like retention_taxonomy.py.
    schema_support.sql          DDL for longsum_support (separate database).
    bootstrap_support_db.py     create + migrate + project the policy.

Design notes that are easy to get wrong later
---------------------------------------------
1. The board is a PROJECTION, never the record. The record is the tables. Any
   workflow tool adopted later renders them; nothing in the domain core learns
   that tool's vocabulary.
2. Cards anchor on fact_ids[], not tiered_ledger.id - that column is SERIAL and
   d3_load_tiers.py deletes and reinserts per subject on every fold-in.
3. Triage is deterministic. No LLM. That is what allows a proposed policy change
   to be replayed exactly over every historical card before promotion.
4. Nothing reaches state 'closed' without a disposition and reply_text, enforced
   by a CHECK constraint rather than by application code.
"""

__all__ = []
