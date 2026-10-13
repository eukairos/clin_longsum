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
reconcile_missing_from_ledger.py  -  READ-ONLY except for the CSVs it writes.

Turns f6's `incoming_reference.json` into two physician-reviewable CSVs:

  missing_facts_review_<date>.csv       one row per salience_retention.missing entry
  contradictions_trace_<date>.csv       one row per coherence.contradictions entry

f6 reports a fact as "missing" or "contradicted" against the LEDGER only, and gives just
`fact` + `basis`. Reviewing that by hand means, for every row, looking up the source fact
in retention_facts.jsonl, hunting for a near-miss ledger entry, and checking whether the
prose tiers cover it anyway. This tool pre-fills all three so the reviewer only has to
make the clinical call.

Columns added beyond f6's own fields:
  salience_tier / retention_class / certainty / lifecycle / negative_kind
                       from the nearest fact in retention_facts.jsonl
  nearest_prior_fact   that fact, with its admit_seq
  prior_fact_match     identity-token overlap with it, 0.00-1.00. NOTE this is a LEXICAL
                       score and f6's own bucketing is not: Pass 2b aligns residual misses
                       against retention_facts SEMANTICALLY, via the judge. A low score
                       here does NOT mean b1 failed to extract the condition - "sacral
                       decubitus ulcer" vs "Chronic pressure ulcer (left buttock)" is the
                       same condition at 0.33. Read it as "how lexically close", nothing more
  nearest_fact_in_ledger_entry
                       if that prior fact's fact_id already sits inside a ledger node, the
                       node's TEXT. Non-empty means the verdict is SUSPECT: Pass 2a read the
                       rendered ledger text and saw nothing describing the fact, while Pass
                       2b found the fact in prior data - but the fact is in the ledger, just
                       invisible in the entry's wording (FM-7's residual) or present under a
                       different qualifier/stage. 21 of 34 rows on a test run
  candidate ledger entry 1 / 2                  best identity-token matches, ranked
  in_raw_only          matches .raw.jsonl at >= RAW_ONLY_BAR but not the served ledger
                       => lost at b4 consolidation (T5 prune or roll-up), not at b2
  in_prose_tiers       keyword hit in tldr / current_episode / prior_admissions. COARSE
                       and over-inclusive: treat "yes" as worth checking, not as covered
  review / notes       left empty for the reviewer

The contradictions CSV adds `opposing_facts`, `n_opposing`, `oldest_opposing_seq` and
`seq_gap` - the ruled_out / absent_or_negative source facts that disagree with the
incoming fact, and how many admissions ago the oldest was recorded. A large `seq_gap` is
the signature of a stale negative: the ledger has no expiry for a finding excluded once,
long ago, so it keeps contradicting later positives.

Matching is identity-token overlap (b2's content tokens minus qualifier boilerplate), the
same notion b2 uses to decide whether two entries describe the same condition, so a
candidate here is a candidate by the pipeline's own definition.

Follows the RUNNING namespace (LONGSUM_DATA_ROOT + LONGSUM_NAMESPACE) via config.

    cd src/clinical_review_tools/f6_reconcile_missing_from_ledger
    python reconcile_missing_from_ledger.py
    python reconcile_missing_from_ledger.py --subject 90000001
    python reconcile_missing_from_ledger.py --out C:/tmp/review.csv --quiet
    python reconcile_missing_from_ledger.py --ref C:/tmp/incoming_reference.json   # default: <OUTPUT_DIR>/eval/incoming_reference.json
    python reconcile_missing_from_ledger.py --contradictions-out C:/tmp/contra.csv  # default: <OUTPUT_DIR>/review_tools/contradictions_trace_<date>.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # src/ on the path (tool is 2 dirs deep)

import config
import io_utils
import b2_build_ledger as b2

EVAL_DIR = config.OUTPUT_DIR / "eval"
OUT_DIR  = config.OUTPUT_DIR / "review_tools"  
REF_JSON = EVAL_DIR / "incoming_reference.json"
LEDGER = config.OUTPUT_DIR / "retention_ledger.jsonl"
RAW_LEDGER = config.OUTPUT_DIR / "retention_ledger.raw.jsonl"
FACTS = config.INTERIM_DIR / "retention_facts.jsonl"
VIEW = config.OUTPUT_DIR / "longitudinal_view.jsonl"

BUCKETS = ("nodes", "flagged", "amended")
PROSE_FIELDS = ("tldr_md", "current_episode_md", "prior_admissions_md")
MIN_FACT_MATCH = 0.5          # below this the retention_facts match is not trusted
MIN_PROSE_TOKEN_LEN = 5       # ignore short tokens when keyword-scanning the prose
STALE_NEGATIVE_GAP = 3        # admissions; flag contradictions older than this
RAW_ONLY_BAR = 0.5            # in_raw_only fires only on a match this strong in raw and
                              # nothing this strong in served -- an "any overlap" test is
                              # masked by incidental token hits and never fires


def _identity(text: str) -> set:
    """Condition-identity tokens. Uses b2's own helper when present (post-2026-08-01)."""
    if hasattr(b2, "_identity_tokens"):
        return b2._identity_tokens(text or "")
    return b2._content_tokens(text or "") - b2._QUALIFIERS


def _overlap(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _load_ledger(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for rec in io_utils.read_jsonl(path):
        out[int(rec["subject_id"])] = rec
    return out


def _load_facts() -> dict:
    out: dict[int, list] = {}
    for rec in io_utils.read_jsonl(FACTS):
        out.setdefault(int(rec["subject_id"]), []).extend(rec.get("facts", []))
    return out


def _load_prose() -> dict:
    out = {}
    if not VIEW.exists():
        return out
    for rec in io_utils.read_jsonl(VIEW):
        out[int(rec["subject_id"])] = " \n ".join((rec.get(k) or "") for k in PROSE_FIELDS)
    return out


def _entries(pid: int, src: dict):
    rec = src.get(pid, {})
    return [(b, nd) for b in BUCKETS for nd in rec.get(b, [])]


def _best_ledger(pid: int, text: str, src: dict, k: int = 2) -> list:
    toks = _identity(text)
    scored = [(_overlap(_identity(nd.get("text")), toks), b, nd)
              for b, nd in _entries(pid, src)]
    scored = [s for s in scored if s[0] > 0]
    scored.sort(key=lambda x: -x[0])
    return scored[:k]


def _best_fact(pid: int, text: str, facts: dict):
    toks = _identity(text)
    best, best_score = None, 0.0
    for f in facts.get(pid, []):
        s = _identity(f.get("text"))
        if toks and s == toks:
            return f, 1.0
        sc = _overlap(s, toks)
        if sc > best_score:
            best, best_score = f, sc
    return best, best_score


def _fact_home(pid: int, fact_id: str, src: dict) -> str:
    """The ledger entry whose fact_ids contains this fact, if any.

    A non-empty result on a 'missing' row is the FM-7 signature seen from the eval side:
    the fact IS retained, but the entry carrying it displays another condition's wording, so
    f6's Pass 2a, which reads rendered ledger TEXT, cannot see it.
    """
    if not fact_id:
        return ""
    for _, nd in _entries(pid, src):
        if fact_id in (nd.get("fact_ids") or []):
            return f"[{nd.get('salience_tier')}] {nd.get('text')}"
    return ""


def _in_prose(pid: int, text: str, prose: dict) -> str:
    blob = prose.get(pid, "")
    if not blob:
        return "?"
    words = [w for w in _identity(text) if len(w) >= MIN_PROSE_TOKEN_LEN]
    hit = any(re.search(r"\b" + re.escape(w) + r"\b", blob, re.I) for w in words)
    return "yes" if hit else "no"


def _fmt(entry) -> str:
    _, bucket, nd = entry
    return f"[{bucket}][{nd.get('salience_tier')}] {nd.get('text')}"


def build_rows(items: list, served: dict, raw: dict, facts: dict, prose: dict,
               contradiction: bool) -> list:
    rows = []
    for it in items:
        pid, text = int(it["subject_id"]), it.get("fact", "")
        f, score = _best_fact(pid, text, facts)
        trusted = score >= MIN_FACT_MATCH
        cands = _best_ledger(pid, text, served, k=2)
        rawc = _best_ledger(pid, text, raw, k=1)
        raw_only = bool(rawc) and rawc[0][0] >= RAW_ONLY_BAR and (
            not cands or cands[0][0] < RAW_ONLY_BAR)
        row = {
            "subject_id": pid,
            "hadm_id": it.get("hadm_id", ""),
            "admit_seq": it.get("admit_seq", ""),
            "fact": text,
            "salience_tier": (f or {}).get("salience_tier", "") if trusted else "",
            "retention_class": (f or {}).get("retention_class", "") if trusted else "",
            "certainty": (f or {}).get("certainty", "") if trusted else "",
            "lifecycle": (f or {}).get("lifecycle", "") if trusted else "",
            "negative_kind": (f or {}).get("negative_kind", "") if trusted else "",
            "nearest_prior_fact": (f"seq{f.get('admit_seq')} {f.get('text')}" if f else ""),
            "prior_fact_match": f"{score:.2f}" if f else "",
            "nearest_fact_in_ledger_entry": _fact_home(pid, (f or {}).get("fact_id"), served),
            "basis": it.get("basis", ""),
            "candidate ledger entry 1": _fmt(cands[0]) if cands else "",
            "candidate ledger entry 2": _fmt(cands[1]) if len(cands) > 1 else "",
            "in_raw_only": "yes" if raw_only else "",
            "in_prose_tiers": _in_prose(pid, text, prose),
            "review": "",
            "notes": "",
        }
        if contradiction:
            # The useful extra for a contradiction is WHICH source facts disagree, and
            # whether the disagreeing entry is a negative that simply never expired.
            toks = _identity(text)
            opposing = [g for g in facts.get(pid, [])
                        if (_identity(g.get("text")) & toks)
                        and (g.get("certainty") == "ruled_out"
                             or g.get("negative_kind") == "absent_or_negative")]
            opposing.sort(key=lambda g: g.get("admit_seq") or 0)
            row["n_opposing"] = len(opposing)
            row["oldest_opposing_seq"] = opposing[0].get("admit_seq") if opposing else ""
            row["seq_gap"] = ((it.get("admit_seq") or 0) - (opposing[0].get("admit_seq") or 0)
                              if opposing else "")
            row["opposing_facts"] = " || ".join(
                f"seq{g.get('admit_seq')} [{g.get('salience_tier')}/{g.get('certainty')}"
                f"/{g.get('negative_kind')}] {g.get('text')}" for g in opposing)
        rows.append(row)
    return rows


def write_csv(rows: list, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", type=Path, default=REF_JSON, help="incoming_reference.json")
    ap.add_argument("--out", type=Path, default=None, help="missing-facts CSV path")
    ap.add_argument("--contradictions-out", type=Path, default=None)
    ap.add_argument("--subject", type=int, default=None, help="restrict to one subject_id")
    ap.add_argument("--quiet", action="store_true", help="suppress the console trace")
    args = ap.parse_args()

    if not args.ref.exists():
        sys.exit(f"not found -> {args.ref}\nRun evaluation/f6_incoming_ref.py first.")

    ref = json.loads(args.ref.read_text(encoding="utf-8"))
    missing = ref.get("salience_retention", {}).get("missing", []) or []
    contra = ref.get("coherence", {}).get("contradictions", []) or []
    if args.subject is not None:
        missing = [m for m in missing if int(m["subject_id"]) == args.subject]
        contra = [c for c in contra if int(c["subject_id"]) == args.subject]

    served, raw = _load_ledger(LEDGER), _load_ledger(RAW_LEDGER)
    facts, prose = _load_facts(), _load_prose()

    stamp = date.today().strftime("%Y%m%d")
    out_missing = args.out or (OUT_DIR / f"missing_facts_review_{stamp}.csv")
    out_contra = args.contradictions_out or (OUT_DIR / f"contradictions_trace_{stamp}.csv")

    print(f"source     : {args.ref}")
    print(f"metric     : {ref.get('metric')} | model {ref.get('model')} | "
          f"{ref.get('taxonomy_version')}")
    print(f"counts     : {ref.get('counts')}")
    sr, co = ref.get("salience_retention", {}), ref.get("coherence", {})
    print(f"retention  : {sr.get('value')} (threshold {sr.get('threshold')}, "
          f"gate {sr.get('gate')})")
    print(f"coherence  : {co.get('value')} (threshold {co.get('threshold')}, "
          f"gate {co.get('gate')})")

    if missing:
        write_csv(build_rows(missing, served, raw, facts, prose, False), out_missing)
        print(f"\nwrote {len(missing):>3} missing-fact rows  -> {out_missing}")
    else:
        print("\nno missing facts to review")

    if not contra:
        print("no contradictions to trace")
        return

    rows = build_rows(contra, served, raw, facts, prose, True)
    write_csv(rows, out_contra)
    print(f"wrote {len(contra):>3} contradiction rows  -> {out_contra}")
    stale = [r for r in rows
             if isinstance(r["seq_gap"], int) and r["seq_gap"] >= STALE_NEGATIVE_GAP]
    if stale:
        print(f"  NOTE: {len(stale)} of {len(rows)} contradict a negative recorded "
              f">={STALE_NEGATIVE_GAP} admissions earlier "
              f"(negatives have no expiry in the ledger)")

    if args.quiet:
        return
    print("\n" + "=" * 78)
    print("CONTRADICTION TRACE")
    for c, r in zip(contra, rows):
        pid = int(c["subject_id"])
        print(f"\n--- [{pid}] hadm={c.get('hadm_id')} seq={c.get('admit_seq')} "
              f" incoming fact: {c.get('fact')!r}")
        print(f"    judge basis: {c.get('basis')}")
        print("    ledger entries sharing identity tokens:")
        for sc, bucket, nd in _best_ledger(pid, c.get("fact", ""), served, k=4):
            print(f"      ({sc:.2f}) [{bucket}][{nd.get('salience_tier')}/"
                  f"{nd.get('retention_class')}] cert={nd.get('certainty')} "
                  f"lc={nd.get('current_lifecycle')} nk={nd.get('negative_kind')} "
                  f"n={nd.get('n_mentions')}")
            print(f"            {nd.get('text')}")
        if r["n_opposing"]:
            print(f"    opposing source facts ({r['n_opposing']}, oldest at seq "
                  f"{r['oldest_opposing_seq']}, gap {r['seq_gap']} admissions):")
            for part in r["opposing_facts"].split(" || "):
                print(f"      {part}")


if __name__ == "__main__":
    main()
