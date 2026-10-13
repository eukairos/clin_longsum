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
fhir_interop.py  -  crosswalk from clin_longsum status axes to HL7 FHIR R5
===========================================================================
READ-ONLY CROSSWALK. b1's extraction vocabulary, is_ledger_member's routing, 
the ledger artefacts and the Postgres columns all keep their native values. 
Nothing in the pipeline imports this module; it exists for downstream FHIR consumers 
and for the interoperability section of the documentation.

WHY A MAPPING AND NOT A RENAME
------------------------------
retention_taxonomy.render_for_prompt() builds b1's prompt vocabulary directly
from the CERTAINTY and LIFECYCLE tuples. Renaming those values therefore changes
the extraction prompt, which invalidates retention_facts.jsonl and every artefact
downstream of it - ledger, compaction, and all f-metrics. 

TWO VALUES NOT TO MAP, 
-------------------------------------------------------
  certainty=patient_reported   FHIR verificationStatus has no equivalent. Who
                               asserted a condition is PROVENANCE in FHIR
                               (Condition.asserter / the Provenance resource),
                               not a verification state. We fold it into
                               `unconfirmed` and carry the provenance separately;
                               the round trip is lossy and flagged as such.
  lifecycle=superseded         A record-management state, not a clinical one: the
                               label was withdrawn or replaced (a penicillin
                               allergy DISPROVEN on challenge). `entered-in-error`
                               is wrong -- supersession is not a data-entry error,
                               it is a later clinical judgement. `inactive` is
                               wrong - it implies the condition existed and
                               stopped. Mapped to `inactive` as the least-bad
                               option, LOSSY, flagged.
                               EXCEPTION: 
                               Safety classes (severe_adr_allergy / code_status_directive)
                               are now served while superseded, carrying
                               negative_kind=absent_or_negative. A de-labelled allergy
                               therefore exports as clinicalStatus=inactive +
                               verificationStatus from certainty. Defensible, still
                               lossy: `inactive` does not say the LABEL was withdrawn.
                               A consumer that reads only clinicalStatus cannot tell a
                               de-labelled allergy from a resolved one - carry the
                               node text, which states the de-labelling in words.

And in the other direction, FHIR clinicalStatus carries `recurrence` and
`relapse`, but recurrence is modelled here as a RETENTION
CLASS (recurrent_episode) rather than as a lifecycle state of a persisting
condition. That asymmetry is a live design question, not an oversight,
and is recorded in UNMAPPED_FHIR below. 

DRIFT GUARD
-----------
The tables are keyed on retention_taxonomy.CERTAINTY / .LIFECYCLE and coverage is
asserted at import. Add a fifth certainty value upstream and this module raises exception on
import rather than silently emitting a wrong FHIR code.

    cd ~/clin_longsum/src
    python fhir_interop.py                 # human-readable crosswalk
    python fhir_interop.py --format json   # machine-readable, for FHIR devs
    python fhir_interop.py --format md     # markdown table for the docs
    python fhir_interop.py --format md --out crosswalk.md   # write to a file instead of stdout
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import retention_taxonomy as tax

# FHIR R5 code system URIs.
SYS_VERIFICATION = "http://terminology.hl7.org/CodeSystem/condition-ver-status"
SYS_CLINICAL = "http://terminology.hl7.org/CodeSystem/condition-clinical"

FHIR_VERSION = "R5"


@dataclass(frozen=True)
class Mapping:
    """One crosswalk row. `lossy` marks a mapping that cannot round-trip."""
    local: str
    fhir_code: str
    system: str
    lossy: bool
    note: str


# -- certainty -> Condition.verificationStatus ----------------------------------
VERIFICATION_STATUS: dict[str, Mapping] = {
    "confirmed": Mapping(
        "confirmed", "confirmed", SYS_VERIFICATION, False,
        "Exact. Asserted as a diagnosis or actively treated."),
    "suspected": Mapping(
        "suspected", "provisional", SYS_VERIFICATION, True,
        "FHIR splits this into unconfirmed / provisional / differential. "
        "`provisional` is the closest single code: a working diagnosis under "
        "genuine uncertainty ('concern for', a rising tumour marker). Lossy in "
        "the FHIR->local direction only -- all three FHIR codes collapse back "
        "to `suspected`."),
    "ruled_out": Mapping(
        "ruled_out", "refuted", SYS_VERIFICATION, False,
        "Exact. Considered and excluded; recorded as a negative, never pinned."),
    "patient_reported": Mapping(
        "patient_reported", "unconfirmed", SYS_VERIFICATION, True,
        "NO FHIR EQUIVALENT. Provenance, not verification state -- FHIR expresses "
        "the asserter via Condition.asserter / Provenance. Emit alongside an "
        "asserter of the patient to preserve the distinction."),
}

# -- lifecycle -> Condition.clinicalStatus --------------------------------------
CLINICAL_STATUS: dict[str, Mapping] = {
    "active": Mapping(
        "active", "active", SYS_CLINICAL, False,
        "Exact."),
    "resolved": Mapping(
        "resolved", "resolved", SYS_CLINICAL, False,
        "Exact."),
    "in_remission": Mapping(
        "in_remission", "remission", SYS_CLINICAL, False,
        "Exact."),
    "superseded": Mapping(
        "superseded", "inactive", SYS_CLINICAL, True,
        "NO FHIR EQUIVALENT. The label was withdrawn or replaced by later "
        "judgement (allergy disproven on challenge). `entered-in-error` is wrong "
        "-- not a data-entry error. `inactive` is the least-bad code but implies "
        "the condition existed and stopped, which supersession denies."),
}

# FHIR codes with no local counterpart. Recorded deliberately: see module docstring.
UNMAPPED_FHIR: dict[str, str] = {
    "recurrence": "Modelled here as retention_class=recurrent_episode (a class), "
                  "not as a lifecycle state of a persisting condition. See FM-4.",
    "relapse": "As `recurrence`. No local lifecycle state.",
    "unknown": "Not emitted: b1 must commit to a lifecycle; absent data defaults "
               "to `active` at contracts.RetentionFact.",
    "differential": "Collapsed into local `suspected`.",
    "entered-in-error": "No local counterpart. Erroneous extractions are handled "
                        "by physician amendment, not by a status value.",
}


def _assert_total_coverage() -> None:
    """Fail loudly at import if the taxonomy grew a value this module doesn't know.
    A stale crosswalk emits a WRONG FHIR code silently; that is the failure mode
    this guard exists to prevent."""
    missing_c = set(tax.CERTAINTY) - set(VERIFICATION_STATUS)
    missing_l = set(tax.LIFECYCLE) - set(CLINICAL_STATUS)
    extra_c = set(VERIFICATION_STATUS) - set(tax.CERTAINTY)
    extra_l = set(CLINICAL_STATUS) - set(tax.LIFECYCLE)
    problems = []
    if missing_c:
        problems.append(f"CERTAINTY values with no FHIR mapping: {sorted(missing_c)}")
    if missing_l:
        problems.append(f"LIFECYCLE values with no FHIR mapping: {sorted(missing_l)}")
    if extra_c:
        problems.append(f"mapped certainty values not in taxonomy: {sorted(extra_c)}")
    if extra_l:
        problems.append(f"mapped lifecycle values not in taxonomy: {sorted(extra_l)}")
    if problems:
        raise RuntimeError(
            f"fhir_interop is out of sync with retention_taxonomy "
            f"{tax.TAXONOMY_VERSION}:\n  " + "\n  ".join(problems) +
            "\nUpdate VERIFICATION_STATUS / CLINICAL_STATUS before using this module.")


_assert_total_coverage()


# -- public API ------------------------------------------------------------------

def verification_status(certainty: str) -> Mapping:
    """clin_longsum certainty -> FHIR Condition.verificationStatus."""
    try:
        return VERIFICATION_STATUS[certainty]
    except KeyError:
        raise ValueError(f"unknown certainty {certainty!r}; "
                         f"expected one of {tax.CERTAINTY}") from None


def clinical_status(lifecycle: str) -> Mapping:
    """clin_longsum lifecycle -> FHIR Condition.clinicalStatus."""
    try:
        return CLINICAL_STATUS[lifecycle]
    except KeyError:
        raise ValueError(f"unknown lifecycle {lifecycle!r}; "
                         f"expected one of {tax.LIFECYCLE}") from None


def as_codeable_concept(m: Mapping) -> dict:
    """Render a Mapping as a FHIR CodeableConcept."""
    return {"coding": [{"system": m.system, "code": m.fhir_code}]}


def condition_status_fields(certainty: str, lifecycle: str) -> dict:
    """Both status fields for a FHIR Condition, plus a lossiness report.

    Returns the two CodeableConcepts and a `_lossy` list naming any axis whose
    mapping cannot round-trip. Callers should surface `_lossy` rather than drop
    it -- it is the difference between an exact export and an approximate one.
    Note this does NOT emit Condition.asserter for patient_reported; the caller
    owns provenance."""
    v, c = verification_status(certainty), clinical_status(lifecycle)
    return {
        "verificationStatus": as_codeable_concept(v),
        "clinicalStatus": as_codeable_concept(c),
        "_lossy": [m.local for m in (v, c) if m.lossy],
    }


def crosswalk() -> dict:
    """The full crosswalk as plain data, for serialisation or docs generation."""
    return {
        "taxonomy_version": tax.TAXONOMY_VERSION,
        "fhir_version": FHIR_VERSION,
        "certainty_to_verification_status": {k: asdict(v) for k, v in VERIFICATION_STATUS.items()},
        "lifecycle_to_clinical_status": {k: asdict(v) for k, v in CLINICAL_STATUS.items()},
        "fhir_codes_with_no_local_counterpart": UNMAPPED_FHIR,
    }


# -- CLI -------------------------------------------------------------------------

def _render_text() -> str:
    out = [f"clin_longsum {tax.TAXONOMY_VERSION}  ->  HL7 FHIR {FHIR_VERSION}", ""]
    for title, table, field in (
            ("certainty -> Condition.verificationStatus", VERIFICATION_STATUS, "verificationStatus"),
            ("lifecycle -> Condition.clinicalStatus", CLINICAL_STATUS, "clinicalStatus")):
        out += [title, "-" * len(title)]
        for m in table.values():
            out.append(f"  {m.local:<18} -> {m.fhir_code:<16}"
                       f"{'  [LOSSY]' if m.lossy else ''}")
            out.append(f"      {m.note}")
        out.append("")
    out += ["FHIR codes with no local counterpart", "-" * 36]
    for code, note in UNMAPPED_FHIR.items():
        out += [f"  {code}", f"      {note}"]
    return "\n".join(out)


def _render_md() -> str:
    out = [f"# clin_longsum {tax.TAXONOMY_VERSION} to HL7 FHIR {FHIR_VERSION}", ""]
    for title, table, col in (
            ("certainty to `Condition.verificationStatus`", VERIFICATION_STATUS, "verificationStatus"),
            ("lifecycle to `Condition.clinicalStatus`", CLINICAL_STATUS, "clinicalStatus")):
        out += [f"## {title}", "",
                f"| local | FHIR {col} | exact | note |", "| --- | --- | --- | --- |"]
        out += [f"| `{m.local}` | `{m.fhir_code}` | {'no' if m.lossy else 'yes'} | {m.note} |"
                for m in table.values()]
        out.append("")
    out += ["## FHIR codes with no local counterpart", "",
            "| FHIR code | why |", "| --- | --- |"]
    out += [f"| `{c}` | {n} |" for c, n in UNMAPPED_FHIR.items()]
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Print the clin_longsum -> FHIR R5 status crosswalk. Read-only.")
    ap.add_argument("--format", choices=("text", "json", "md"), default="text")
    ap.add_argument("--out", default=None, help="write to a file instead of stdout")
    args = ap.parse_args()

    body = {"text": _render_text, "md": _render_md,
            "json": lambda: json.dumps(crosswalk(), indent=2)}[args.format]()
    if args.out:
        Path(args.out).write_text(body, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
