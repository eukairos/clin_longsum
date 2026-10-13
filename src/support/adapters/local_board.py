# clin_longsum - physician feedback loop
# Copyright (C) 2026 Eukairos
# SPDX-License-Identifier: AGPL-3.0-only

"""
adapters/local_board.py
=======================
Board adapter for the built-in board, which reads the support tables directly.

EVERY DELIVERY IS A SKIP, AND THAT IS THE POINT. The built-in board is a
projection of `feedback` and `feedback_events`; there is nothing to push to. The
adapter exists anyway so that the outbox carries a complete, ordered record of
every board-visible transition from day one. When a real workflow tool is
adopted later, its adapter replaces this one and the queue can be re-drained
against it to backfill history - which is only possible because these rows were
written even though nobody was listening.

Swapping is therefore: write PlankaAdapter, change one line in dispatcher.py,
re-drain. No migration, no vocabulary change, no data movement.
"""

from __future__ import annotations

from typing import Any, Mapping

try:
    from support.adapters.base import Delivery
except ImportError:  # pragma: no cover
    from adapters.base import Delivery

_NOTE = "built-in board reads the support tables directly; nothing to push"


class LocalBoardAdapter:
    name = "local"

    def upsert(self, card: Mapping[str, Any]) -> Delivery:
        return Delivery.skip(_NOTE)

    def move(self, card: Mapping[str, Any], state: str) -> Delivery:
        return Delivery.skip(_NOTE)

    def close(self, card: Mapping[str, Any], disposition: str) -> Delivery:
        return Delivery.skip(_NOTE)
