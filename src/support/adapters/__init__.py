# clin_longsum - physician feedback loop
# Copyright (C) 2026 Eukairos
# SPDX-License-Identifier: AGPL-3.0-only

"""
adapters
========
Outbound delivery. Everything the loop sends to the world leaves through here.

THE SEAM. `BoardAdapter` exists so the built-in board can be replaced by a
workflow product (Planka, Jira, ServiceNow) without the domain core learning
anything about it. The core speaks its own vocabulary - state, disposition,
urgency, owner_roles - and an adapter maps that outward. Nothing maps inward
except through an explicit, lossy-by-refusal sync_back.

WHY ADAPTERS NEVER RUN INLINE. They are called only by dispatcher.py draining
the outbox. A physician's report must not fail because a mail host is down, and
a re-drain after a crash must not double-send. Both fall out of writing an
outbox row inside the same transaction as the state change, then delivering
separately under an idempotency key.
"""

__all__ = []
