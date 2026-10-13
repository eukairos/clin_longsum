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
inspect_ledger.py  (scratch diagnostic)
====================================
Print EVERY ledger entry for one subject, so we can see what is actually in the
ledger (e.g. is 'depression' present at all?) - and whether it sits in the served
or raw ledger.

Paths follow the RUNNING namespace via config (LONGSUM_DATA_ROOT + LONGSUM_NAMESPACE),
so this reads whichever cohort the shell is pointed at.

    cd src/clinical_review_tools/b3_loss_audit_tool
    python inspect_ledger.py 90000017
    python inspect_ledger.py 90000017 --grep depress mood suicid psych
    python inspect_ledger.py 90000017 --grep depress --facts   # also scan raw facts for the terms
    python inspect_ledger.py 90000017 --ledger raw
"""

import argparse
import json
import sys
from pathlib import Path

# config lives in src/ (this tool is at src/clinical_review_tools/b3_loss_audit_tool/).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config

SERVED = config.OUTPUT_DIR / "retention_ledger.jsonl"
RAW = config.OUTPUT_DIR / "retention_ledger.raw.jsonl"
FACTS = config.INTERIM_DIR / "retention_facts.jsonl"


def _find(path: Path, sid: int):
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        # cheap prefilter before json.loads
        if f'"subject_id":{sid}' in line or f'"subject_id": {sid}' in line:
            rec = json.loads(line)
            if int(rec.get("subject_id", -1)) == sid:
                return rec
    return None


def _print_ledger(led: dict, grep: list[str]) -> None:
    g = [s.lower() for s in grep]

    def hit(t):
        return any(s in (t or "").lower() for s in g)

    print(f"\nsubject {led['subject_id']}  n_admissions={led.get('n_admissions')}  "
          f"consolidated={led.get('consolidated', False)}  tax={led.get('taxonomy_version')}")
    for home in ("nodes", "flagged", "amended"):
        bucket = led.get(home, []) or []
        print(f"\n-- {home} ({len(bucket)}) --")
        for e in bucket:
            mark = "  <== MATCH" if (g and hit(e.get("text"))) else ""
            print(f"  [{e.get('salience_tier','?')}/{e.get('retention_class','?')}/"
                  f"{e.get('current_lifecycle','?')}/nk={e.get('negative_kind')}] "
                  f"{e.get('text','')}  (n={e.get('n_mentions')}, "
                  f"hadms={e.get('source_hadm_ids')}){mark}")
    eps = led.get("episodes", []) or []
    print(f"\n-- episodes ({len(eps)}) --")
    for ep in eps:
        mark = "  <== MATCH" if (g and hit(ep.get("parent_condition"))) else ""
        print(f"  {ep.get('parent_condition','')}  (count={ep.get('count')}, "
              f"hadms={ep.get('source_hadm_ids')}){mark}")
    exc = led.get("considered_excluded", []) or []
    print(f"\n-- considered_excluded ({len(exc)}) --")
    for t in exc:
        mark = "  <== MATCH" if (g and hit(t)) else ""
        print(f"  {t}{mark}")


def _scan_facts(sid: int, grep: list[str]) -> None:
    """Did the RAW facts ever mention the grep terms for this subject, and how were they
    tagged? Answers 'extracted-but-dropped' vs 'never-extracted'."""
    if not FACTS.exists() or not grep:
        return
    g = [s.lower() for s in grep]
    print(f"\n== raw facts mentioning {grep} (subject {sid}) ==")
    found = 0
    for line in FACTS.read_text(encoding="utf-8").splitlines():
        if not line.strip() or f'"subject_id":{sid}' not in line.replace(" ", ""):
            continue
        rec = json.loads(line)
        if int(rec.get("subject_id", -1)) != sid:
            continue
        for f in rec.get("facts", []):
            if any(s in (f.get("text", "")).lower() for s in g):
                found += 1
                print(f"  hadm {rec.get('hadm_id')} seq {rec.get('admit_seq')}: "
                      f"[{f.get('salience_tier')}/{f.get('retention_class')}/"
                      f"{f.get('certainty')}/{f.get('lifecycle')}/nk={f.get('negative_kind')}] "
                      f"{f.get('text','')}")
    if not found:
        print("  (none -- never extracted as a fact)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("subject", type=int)
    ap.add_argument("--ledger", choices=["auto", "served", "raw"], default="auto")
    ap.add_argument("--grep", nargs="*", default=["depress", "mood", "suicid", "psych", "affective"])
    ap.add_argument("--facts", action="store_true", help="also scan raw facts for the grep terms")
    args = ap.parse_args()

    print(f"namespace: {config.RUN_NAMESPACE or '(default)'}  |  data: {config.DATA_DIR}")
    src = SERVED
    served = _find(SERVED, args.subject)
    if args.ledger == "raw":
        src = RAW
    elif args.ledger == "auto" and served and served.get("consolidated") and RAW.exists():
        src = RAW
    led = _find(src, args.subject)
    print(f"ledger source: {src.name}")
    if not led:
        print(f"subject {args.subject} NOT FOUND in {src.name}")
        return
    _print_ledger(led, args.grep)
    # always scan facts too -- it answers extracted-but-dropped vs never-extracted
    _scan_facts(args.subject, args.grep)


if __name__ == "__main__":
    main()
