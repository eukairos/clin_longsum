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
review_merge_faithfulness.py  -  READ-ONLY except for the CSV it writes.

Lays out f9_merge_faithfulness's `merge_faithfulness.jsonl` as one row PER CLAIM, so a
reviewer can see the consolidated wording, the member texts it was built from, and the
judge's verdict side by side without opening the jsonl.

f9 asks: does b4's consolidated node text assert anything its member nodes do not support?
It decomposes `consolidated_text` into claims and labels each supported / not_supported /
not_addressable. A `not_supported` claim is a putative MERGE FABRICATION - wording invented
by consolidation rather than carried from a member.

The judge's Not-Supported positive predictive value is low (~0.24 measured on the reference
cohort), so every not_supported claim needs a human read before it counts as a defect. This
tool exists to make that job easier, and to carry adjudications already done so they are not
repeated. See References/MODEL_SELECTION.md section 3 for how to calibrate your own judge.

ADJUDICATION COLUMN
  `adjudication` is filled from ADJUDICATIONS below for claims already reviewed; blank for
  anything new. Values:
    warranted      -- the consolidated text really does assert something no member supports
    not_warranted  -- judge error; a member supports it, or the terms are synonyms
  `adjudication_reason` gives the basis. `review` / `notes` are left empty for the reviewer.

Follows the RUNNING namespace (LONGSUM_DATA_ROOT + LONGSUM_NAMESPACE) via config.

    cd src/clinical_review_tools/f9_merge_faithfulness_review
    python review_merge_faithfulness.py                 # flagged claims only (default)
    python review_merge_faithfulness.py --all-claims    # every claim, incl. supported
    python review_merge_faithfulness.py --subject 90000017
    python review_merge_faithfulness.py --src C:/tmp/merge_faithfulness.jsonl   # default: <OUTPUT_DIR>/eval/merge_faithfulness.jsonl
    python review_merge_faithfulness.py --out C:/tmp/review.csv                  # default: <OUTPUT_DIR>/review_tools/merge_faithfulness_review_<suffix>_<date>.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # src/ on the path

import config
import io_utils

SRC_JSONL = config.OUTPUT_DIR / "eval" / "merge_faithfulness.jsonl"
OUT_DIR = config.OUTPUT_DIR / "review_tools"

# ---------------------------------------------------------------------------------------
# ADJUDICATIONS -- TEMPLATE. Replace every entry below with your own.
#
# The three entries shipped here are ILLUSTRATIVE ONLY: synthetic subject ids, synthetic
# node keys, and invented wording. They exist to show the shape of an entry and the three
# judge-failure archetypes worth recognising. They will not match any claim in your data,
# so on a real run every claim starts unadjudicated, which is correct.
#
# No adjudications from the reference cohort ship with this repository - they are derived
# from credentialed data and cannot be redistributed.
#
# Key   : (subject_id, node_key, claim_text) -- must match the run exactly
# Value : (verdict, reason)
#           warranted      -- the consolidated text really does assert something no member
#                             supports; a genuine merge fabrication
#           not_warranted  -- judge error; a member supports it, or the terms are synonyms
#
# To keep your own adjudications OUT of version control, put them in a sibling
# `adjudications_local.py` defining a dict named ADJUDICATIONS. It is gitignored and, if
# present, is merged over the template below.
# ---------------------------------------------------------------------------------------
ADJUDICATIONS: dict[tuple[int, str, str], tuple[str, str]] = {
    (90000001, "0000000000000001", "Condition X with finding Y on exam"): (
        "warranted",
        "NEGATION INVERSION. The only member that mentions the exam records the OPPOSITE "
        "finding. Consolidation spliced an attribute from an earlier member onto the exam "
        "clause of a later one, inverting a negative finding. The archetype worth catching: "
        "clinically material, and the judge was right."),
    (90000002, "0000000000000002", "Condition A managed with drug D"): (
        "warranted",
        "DISTRIBUTIVE INFERENCE. One member gives 'condition B managed with drug D', another "
        "gives 'condition A and condition B' with no treatment. Consolidation extended the "
        "treatment claim from B to A. Plausible, strictly unsupported, low impact."),
    (90000003, "0000000000000003", "Condition C managed with drug E"): (
        "not_warranted",
        "SYNONYMY. A member states the same thing using the brand name rather than the "
        "generic (or a subtype of the same entity). Judge error, not a fabrication - the "
        "single most common cause of a false not_supported."),
}

try:                                    # optional, gitignored, local-only overrides
    from adjudications_local import ADJUDICATIONS as _LOCAL_ADJUDICATIONS
except ImportError:
    pass
else:
    ADJUDICATIONS = {**ADJUDICATIONS, **_LOCAL_ADJUDICATIONS}


def build_rows(records: list, all_claims: bool, subject: int | None) -> list:
    rows = []
    for rec in records:
        pid = int(rec["subject_id"])
        if subject is not None and pid != subject:
            continue
        members = rec.get("member_texts") or []
        for claim in rec.get("claims") or []:
            label = claim.get("label")
            if not all_claims and label == "supported":
                continue
            text = claim.get("claim", "")
            adj, reason = ADJUDICATIONS.get((pid, rec.get("node_key"), text), ("", ""))
            rows.append({
                "subject_id": pid,
                "node_key": rec.get("node_key"),
                "salience_tier": rec.get("salience_tier"),
                "claim": text,
                "label": label,
                "adjudication": adj,
                "adjudication_reason": reason,
                "consolidated_text": rec.get("consolidated_text"),
                "n_members": rec.get("n_members"),
                "member_texts": "\n".join(f"{i + 1}. {m}" for i, m in enumerate(members)),
                "n_fact_ids": len(rec.get("fact_ids") or []),
                "n_supported": rec.get("supported"),
                "n_not_supported": rec.get("not_supported"),
                "n_not_addressable": rec.get("not_addressable"),
                "judge_status": rec.get("status"),
                "source_truncated": rec.get("source_truncated"),
                "model": rec.get("model"),
                "prompt_version": rec.get("prompt_version"),
                "review": "",
                "notes": "",
            })
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, default=SRC_JSONL)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--all-claims", action="store_true",
                    help="include supported claims (default: flagged claims only)")
    ap.add_argument("--subject", type=int, default=None)
    args = ap.parse_args()

    if not args.src.exists():
        sys.exit(f"not found -> {args.src}\nRun evaluation/f9_merge_faithfulness.py first.")

    records = list(io_utils.read_jsonl(args.src))
    n_claims = sum(len(r.get("claims") or []) for r in records)
    tally = {k: sum(r.get(k) or 0 for r in records)
             for k in ("supported", "not_supported", "not_addressable")}
    errors = [r for r in records if r.get("status") == "judge_error"]
    truncated = [r for r in records if r.get("source_truncated")]

    print(f"source     : {args.src}")
    print(f"nodes      : {len(records)}  |  claims: {n_claims}")
    print(f"verdicts   : {tally}")
    print(f"judge_error: {len(errors)}  |  source_truncated: {len(truncated)}")

    rows = build_rows(records, args.all_claims, args.subject)
    if not rows:
        print("\nnothing to review")
        return

    stamp = date.today().strftime("%Y%m%d")
    suffix = "all_claims" if args.all_claims else "flagged"
    out = args.out or (OUT_DIR / f"merge_faithfulness_review_{suffix}_{stamp}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {len(rows)} rows -> {out}")

    done = [r for r in rows if r["adjudication"]]
    if done:
        warranted = [r for r in done if r["adjudication"] == "warranted"]
        print(f"adjudicated: {len(done)}/{len(rows)}  |  warranted {len(warranted)}, "
              f"not warranted {len(done) - len(warranted)}")
        print(f"Not-Supported PPV on adjudicated rows: {len(warranted) / len(done):.2f}")
        for r in warranted:
            print(f"  WARRANTED [{r['subject_id']}] {r['claim']}")
    todo = [r for r in rows if not r["adjudication"]]
    if todo:
        print(f"awaiting review: {len(todo)}")


if __name__ == "__main__":
    main()
