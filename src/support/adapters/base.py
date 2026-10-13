# clin_longsum - physician feedback loop
# Copyright (C) 2026 Eukairos
# SPDX-License-Identifier: AGPL-3.0-only

"""
adapters/base.py
================
The delivery contract. Two protocols and one result type.

A DELIVERY IS NOT A DOMAIN OPERATION. Adapters receive a fully-resolved payload
and report what happened; they never open a transaction, never touch the
feedback tables, and never decide policy. dispatcher.py owns all of that. Keeping
the boundary this strict so that swapping a board tool is a contained change
rather than an archaeology exercise.

FAILURE IS DATA, NOT AN EXCEPTION. An adapter returns Delivery(ok=False, ...)
rather than raising, so one dead channel cannot stop the queue draining for the
others. Only genuinely unexpected faults propagate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol


@dataclass
class Delivery:
    """Outcome of one delivery attempt.

    `skipped` means "correctly not sent" - a no-op board adapter, or a reporter
    with no directory match and therefore no address. That is distinct from
    failure: a skipped row must not be retried forever, and must not be counted
    as a stuck delivery by the loop-health check.
    """
    ok: bool
    skipped: bool = False
    external_ref: Optional[str] = None
    detail: Optional[str] = None

    @classmethod
    def sent(cls, external_ref: Optional[str] = None, detail: Optional[str] = None):
        return cls(ok=True, external_ref=external_ref, detail=detail)

    @classmethod
    def skip(cls, detail: str):
        return cls(ok=True, skipped=True, detail=detail)

    @classmethod
    def fail(cls, detail: str):
        return cls(ok=False, detail=detail)


class BoardAdapter(Protocol):
    """Projects a card onto a board.

    The three verbs are deliberately the whole surface. Anything richer - lane
    names, WIP limits, custom fields - belongs to the tool and is the adapter's
    private business, because the moment the core knows about it, the core is
    coupled to that tool.
    """

    name: str

    def upsert(self, card: Mapping[str, Any]) -> Delivery: ...
    def move(self, card: Mapping[str, Any], state: str) -> Delivery: ...
    def close(self, card: Mapping[str, Any], disposition: str) -> Delivery: ...


class Notifier(Protocol):
    """Sends one message to one person."""

    name: str

    def send(self, to_email: str, to_name: str, subject: str, body: str) -> Delivery: ...
