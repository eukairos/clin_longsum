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
ingest_source.py
================
EHR adapters for the production ingest. An adapter returns SIGNED discharge
summaries (one per admission) whose signed_at is after a watermark.

Contract (EHRSource.fetch):
  - only finalised/signed summaries (the EHR side should guarantee this)
  - subject_id / hadm_id are the pipeline's internal identifiers (downstream consumers own the
    mapping from their own identifiers to subject_id and hadm_id)
  - dates are ISO 8601. A row whose dates are not ISO is REJECTED, never guessed:
    the pipeline's own parser (cohort.py) is day-first, so an ambiguous date would be
    read with day and month swapped.
  - one record per hadm_id; if a batch carries several, the latest signed_at wins

CsvDropSource is the reference adapter: the EHR (or an interface engine) drops CSV
extracts into a folder. Swap in a REST/HL7/FHIR adapter by implementing fetch() and
registering it in get_source().

Drop-file columns (header names exact):
  subject_id, hadm_id, admittime, dischtime, signed_at, bhc_text (or `target`),
  service, admission_type, discharge_location, hospital_expire_flag   (last four optional)
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

import pandas as pd

import config

REQUIRED = ("subject_id", "hadm_id", "admittime", "signed_at")
TEXT_COLS = ("bhc_text", "target")
_TRUE = {"1", "1.0", "true", "t", "yes", "y"}
_FALSE = {"", "0", "0.0", "false", "f", "no", "n"}


def text_sha256(text) -> str:
    """Identity of a discharge text for duplicate / amendment detection."""
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EHRDischarge:
    subject_id: int
    hadm_id: int
    admittime: datetime
    dischtime: datetime | None
    signed_at: datetime
    service: str | None
    admission_type: str | None
    discharge_location: str | None
    hospital_expire_flag: bool
    bhc_text: str

    @property
    def sha256(self) -> str:
        return text_sha256(self.bhc_text)


class EHRSource(Protocol):
    name: str

    def fetch(self, since: datetime | None) -> tuple[list[EHRDischarge], list[dict]]:
        """(records signed after `since`, rejects). Rejects are dicts with at least
        'reason'; they are logged, never raised."""
        ...


def _iso(value: str, field: str, required: bool) -> datetime | None:
    s = (value or "").strip()
    if not s:
        if required:
            raise ValueError(f"{field} is empty")
        return None
    try:
        ts = pd.to_datetime(s, format="ISO8601")
    except (ValueError, TypeError):
        raise ValueError(f"{field} is not ISO 8601: {s!r}")
    if ts.tzinfo is not None:                      # store naive local time, like the cohort
        ts = ts.tz_localize(None)
    return ts.to_pydatetime()


def _opt(value) -> str | None:
    s = (value or "").strip()
    return s or None


def parse_row(r: dict, text_col: str) -> EHRDischarge:
    """One drop-file row -> EHRDischarge. Raises ValueError with the rejection reason."""
    try:
        sid = int(str(r["subject_id"]).strip())
        hadm = int(str(r["hadm_id"]).strip())
    except (ValueError, KeyError):
        raise ValueError("subject_id/hadm_id not integer")
    text = (r.get(text_col) or "").strip()
    if not text:
        raise ValueError("empty bhc_text")
    flag = (r.get("hospital_expire_flag") or "").strip().lower()
    if flag in _TRUE:
        expired = True
    elif flag in _FALSE:
        expired = False
    else:
        raise ValueError(f"hospital_expire_flag not boolean: {flag!r}")
    admit = _iso(r.get("admittime"), "admittime", True)
    disch = _iso(r.get("dischtime"), "dischtime", False)
    if disch is not None and disch < admit:
        raise ValueError("dischtime before admittime")
    return EHRDischarge(
        subject_id=sid, hadm_id=hadm, admittime=admit, dischtime=disch,
        signed_at=_iso(r.get("signed_at"), "signed_at", True),
        service=_opt(r.get("service")), admission_type=_opt(r.get("admission_type")),
        discharge_location=_opt(r.get("discharge_location")),
        hospital_expire_flag=expired, bhc_text=text)


def latest_per_hadm(recs: list[EHRDischarge]) -> tuple[list[EHRDischarge], list[dict]]:
    """Keep the latest-signed record per hadm_id; report the superseded ones."""
    best: dict[int, EHRDischarge] = {}
    superseded: list[dict] = []
    for rec in recs:
        cur = best.get(rec.hadm_id)
        if cur is None or rec.signed_at > cur.signed_at:
            if cur is not None:
                superseded.append({"hadm_id": cur.hadm_id, "subject_id": cur.subject_id,
                                   "reason": "superseded by a later-signed record in the same fetch"})
            best[rec.hadm_id] = rec
        else:
            superseded.append({"hadm_id": rec.hadm_id, "subject_id": rec.subject_id,
                               "reason": "superseded by a later-signed record in the same fetch"})
    return sorted(best.values(), key=lambda x: (x.subject_id, x.admittime, x.hadm_id)), superseded


class CsvDropSource:
    """Reads every *.csv in a drop folder. Files are never moved or deleted: the
    watermark (signed_at) plus hadm-level idempotency makes re-reading safe."""

    name = "csv_drop"

    def __init__(self, drop_dir: Path):
        self.drop_dir = Path(drop_dir)

    def fetch(self, since: datetime | None) -> tuple[list[EHRDischarge], list[dict]]:
        if not self.drop_dir.exists():
            raise SystemExit(f"EHR drop folder not found: {self.drop_dir} "
                             f"(set LONGSUM_EHR_DROP_DIR)")
        recs: list[EHRDischarge] = []
        rejects: list[dict] = []
        for f in sorted(self.drop_dir.glob("*.csv")):
            df = pd.read_csv(f, dtype=str, keep_default_na=False)
            text_col = next((c for c in TEXT_COLS if c in df.columns), None)
            missing = [c for c in REQUIRED if c not in df.columns]
            if missing or text_col is None:
                rejects.append({"file": f.name, "reason":
                                f"file skipped: missing columns {missing + ([] if text_col else ['bhc_text'])}"})
                continue
            for i, r in enumerate(df.to_dict("records")):
                try:
                    rec = parse_row(r, text_col)
                except ValueError as exc:
                    rejects.append({"file": f.name, "row": i + 2, "hadm_id": r.get("hadm_id"),
                                    "subject_id": r.get("subject_id"), "reason": str(exc)})
                    continue
                if since is not None and rec.signed_at <= since:
                    continue
                recs.append(rec)
        recs, superseded = latest_per_hadm(recs)
        return recs, rejects + superseded


def get_source(name: str | None = None) -> EHRSource:
    name = (name or os.environ.get("LONGSUM_EHR_SOURCE", "csv_drop")).strip()
    if name == "csv_drop":
        drop = os.environ.get("LONGSUM_EHR_DROP_DIR", str(config.DATA_DIR / "ehr_drop"))
        return CsvDropSource(Path(drop))
    raise SystemExit(f"unknown EHR source {name!r}: implement an EHRSource adapter in "
                     f"ingest_source.py and register it in get_source().")
