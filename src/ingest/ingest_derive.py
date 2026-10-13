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
ingest_derive.py
================
Pure decision logic for the production ingest (no DB, no files) - what to do with
each fetched EHR record for one patient, and the derived columns a2 would have
computed for it.

Per record, exactly one action:
  insert        new hadm_id, chronologically at or after the patient's latest stored
                admission. Gets admit_seq = max + 1, is_holdout = True, and bhc_delta /
                dedup_jaccard_to_prev from a2.dedup_patient_bhc run over the patient's
                full chronological chain (imported with same parameters).
  duplicate     hadm_id already stored with identical text (re-poll overlap). No-op.
  amendment     hadm_id already stored, text differs. Never applied; manual review.
  out_of_order  admitted BEFORE the patient's latest stored admission. Inserting it
                would renumber admit_seq under already-built artifacts; manual review.
  after_death   the patient already has an admission with hospital_expire_flag.
                Manual review (identifier-mapping error on the EHR side is likely).

Embedding is NOT derived here: fold-in's e2_embed_incoming embeds an admission only
after physician approval, so unreviewed text never enters RAG retrieval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import a2_build_cohort as a2
from ingest_source import EHRDischarge, text_sha256


@dataclass(frozen=True)
class Existing:
    """One admission already in the admissions table."""
    hadm_id: int
    admit_seq: int
    admit_date: date | None
    bhc_text: str
    hospital_expire_flag: bool
    folded_in: bool                  # established (never held out) or promoted


@dataclass
class Action:
    kind: str                        # insert | duplicate | amendment | out_of_order | after_death
    rec: EHRDischarge
    row: dict | None = None          # admissions / cohort row, inserts only
    first_admission: bool = False    # patient had no stored admission (auto fold-in candidate)
    detail: dict = field(default_factory=dict)


def plan_subject(new_recs: list[EHRDischarge], existing: list[Existing]) -> list[Action]:
    """Decide every fetched record of ONE patient, oldest first."""
    existing = sorted(existing, key=lambda e: e.admit_seq)
    by_hadm = {e.hadm_id: e for e in existing}
    chain = [{"hadm_id": e.hadm_id, "bhc_text": e.bhc_text} for e in existing]
    last_seq = existing[-1].admit_seq if existing else 0
    last_date = max((e.admit_date for e in existing if e.admit_date), default=None)
    dead_hadm = next((e.hadm_id for e in existing if e.hospital_expire_flag), None)

    actions: list[Action] = []
    inserts: list[Action] = []
    for rec in sorted(new_recs, key=lambda r: (r.admittime, r.hadm_id)):
        prev = by_hadm.get(rec.hadm_id)
        if prev is not None:
            stored = text_sha256(prev.bhc_text)
            if stored == rec.sha256:
                actions.append(Action("duplicate", rec))
            else:
                actions.append(Action("amendment", rec, detail={
                    "stored_sha256": stored, "admission_folded_in": prev.folded_in,
                    "stored_admit_seq": prev.admit_seq}))
            continue
        admit_d = rec.admittime.date()
        if dead_hadm is not None:
            actions.append(Action("after_death", rec, detail={"expired_hadm_id": dead_hadm}))
            continue
        if last_date is not None and admit_d < last_date:
            actions.append(Action("out_of_order", rec, detail={
                "latest_stored_admit_date": last_date.isoformat(),
                "incoming_admit_date": admit_d.isoformat()}))
            continue

        last_seq += 1
        row = {
            "hadm_id": rec.hadm_id, "subject_id": rec.subject_id, "admit_seq": last_seq,
            "admittime": rec.admittime, "dischtime": rec.dischtime,
            "service": rec.service, "admission_type": rec.admission_type,
            "discharge_location": rec.discharge_location,
            "hospital_expire_flag": rec.hospital_expire_flag,
            "is_holdout": True, "bhc_text": rec.bhc_text,
            "bhc_delta": None, "dedup_jaccard_to_prev": None,
        }
        act = Action("insert", rec, row=row, first_admission=(last_seq == 1),
                     detail={"admit_seq": last_seq})
        actions.append(act)
        inserts.append(act)
        chain.append({"hadm_id": rec.hadm_id, "bhc_text": rec.bhc_text})
        by_hadm[rec.hadm_id] = Existing(rec.hadm_id, last_seq, admit_d, rec.bhc_text,
                                        rec.hospital_expire_flag, False)
        last_date = admit_d if last_date is None or admit_d > last_date else last_date
        if rec.hospital_expire_flag:
            dead_hadm = rec.hadm_id

    if inserts:
        # Each admission's delta depends only on the texts BEFORE it, so running the
        # whole chain reproduces exactly what a2 would have computed in batch.
        res = {int(r["hadm_id"]): r for r in a2.dedup_patient_bhc(chain)}
        for act in inserts:
            r = res[act.rec.hadm_id]
            act.row["bhc_delta"] = r["bhc_delta"]
            act.row["dedup_jaccard_to_prev"] = r["dedup_jaccard_to_prev"]
    return actions
