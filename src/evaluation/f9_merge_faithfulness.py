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
f9_merge_faithfulness.py
========================
Phase F intrinsic metric - LEDGER MERGE FAITHFULNESS (Tier 1, LLM judge).

The "axis-2" consolidation check: when b4 folds several raw ledger entries into one
consolidated line, does that line assert anything its source entries did NOT? This is
PRECISION only - correct consolidation drops duplicates by design, so loss is NOT
penalised here (that is f1's job: SWR already guarantees no salient fact is dropped
across the whole ledger). This metric catches the merge *inventing* a qualifier,
severity, or condition the members never stated.

  merge_faithful_fraction = supported / (supported + not_supported)   [not_addressable excluded]

How members are recovered (no LLM re-run): `_merge_node` unions its members' fact_ids
into the merged node, and b4 keeps the pre-consolidation ledger at
retention_ledger.raw.jsonl. So a consolidated node C's members are exactly the RAW
nodes whose fact_ids are a subset of C's fact_ids. Only nodes with >= 2 members are
real merges and get judged; singletons / pass-throughs are skipped.

SOURCE per merged node = the member entries' text + a deterministic provenance line
(member count, admission count) so any count/date wording in the consolidated line
(e.g. a T4 metric "7 episodes from ...") verifies against the provenance, not as a
hallucination. SUMMARY = the consolidated node's text. Judge + graceful recovery are
reused wholesale from f4 (same deepseek judge, swappable in the same config slot).

Tier 1, not a hard gate. Default threshold high (merges should rarely fabricate) but
PROVISIONAL - re-baseline from a clean run. Unqualified until the judge clears f7.

OUTPUTS:
  data/output/eval/merge_faithfulness.jsonl   one record per merged node, with claims
  data/output/eval/merge_faithfulness.json    aggregate (overall + by tier) + gate
EXIT CODE: 0 if overall merge_faithful_fraction >= threshold, else 1.

    cd ~/clin_longsum
    python src/evaluation/f9_merge_faithfulness.py --fresh --limit 10
    python src/evaluation/f9_merge_faithfulness.py --subject 90000062
    python src/evaluation/f9_merge_faithfulness.py --threshold 0.7 --skip 90000001 --json-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from collections import defaultdict

import _bootstrap  # noqa: F401  (puts src/ on sys.path)
import config
import c1_compactor as c1
import f4_faithfulness as f4          # reuse judge: judge_unit, _ollama_unload, _cap, SPEC

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("f9_merge_faithfulness")

LABELS = ("supported", "not_supported", "not_addressable")
LEDGER = config.OUTPUT_DIR / "retention_ledger.jsonl"
RAW = config.OUTPUT_DIR / "retention_ledger.raw.jsonl"
OUT_DIR = config.OUTPUT_DIR / "eval"
OUT_JSONL = OUT_DIR / "merge_faithfulness.jsonl"
OUT_JSON = OUT_DIR / "merge_faithfulness.json"


def _node_key(pid: int, fact_ids: list) -> str:
    h = hashlib.sha1((str(pid) + "|" + ",".join(map(str, sorted(fact_ids)))).encode()).hexdigest()
    return h[:16]


def _members_of(c_node: dict, raw_nodes: list[dict]) -> list[dict]:
    """Raw nodes whose fact_ids are a subset of the consolidated node's fact_ids."""
    fc = set(c_node.get("fact_ids") or [])
    if not fc:
        return []
    members = []
    for r in raw_nodes:
        rf = set(r.get("fact_ids") or [])
        if rf and rf <= fc:
            members.append(r)
    return members


def _source(members: list[dict]) -> str:
    lines, hadms = [], set()
    for m in members:
        tier = m.get("salience_tier", "")
        life = m.get("current_lifecycle", "")
        lines.append(f"- [{tier}/{life}] (n_mentions={m.get('n_mentions', 0)}) {m.get('text', '')}")
        hadms.update(m.get("source_hadm_ids") or [])
    prov = (f"\n\nDeterministic provenance (a count/date in the line may rely on this): "
            f"{len(members)} member entries across {len(hadms)} admissions.")
    return ("MEMBER ENTRIES that were merged into a single ledger line. The line must "
            "assert nothing beyond these, though it may count, date, or summarise them:\n"
            + "\n".join(lines) + prov)


def main() -> int:
    ap = argparse.ArgumentParser(description="Ledger merge faithfulness (consolidation precision).")
    ap.add_argument("--subject", type=int, default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--threshold", type=float, default=0.95,
                    help="min overall merge_faithful_fraction (provisional; re-baseline)")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--skip", type=int, nargs="*", default=[],
                    help="subject_id(s) to skip entirely")
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()
    config.check_paths()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if not RAW.exists():
        raise SystemExit(f"{RAW} not found. Merge faithfulness needs the pre-consolidation "
                         f"ledger; run b4 with CONSOLIDATE_LEDGER on (it writes the .raw backup).")
    con = {int(l["subject_id"]): l for l in c1._read_jsonl(LEDGER)}
    raw = {int(l["subject_id"]): l for l in c1._read_jsonl(RAW)}
    if not con:
        raise SystemExit(f"{LEDGER} not found/empty.")

    pids = [args.subject] if args.subject is not None else sorted(con)
    if args.limit:
        pids = pids[:args.limit]
    pid_set = set(pids)

    existing = {} if args.fresh else {
        (int(r["subject_id"]), r["node_key"]): r for r in c1._read_jsonl(OUT_JSONL)}
    done = {k for k, r in existing.items() if r.get("status") != "judge_error"}
    skip = set(args.skip or [])

    new_records: list[dict] = []
    n_judged = 0
    # Incremental persistence: append each judged merge (flushed) so an interrupt never loses
    # the batch; resume (above) skips done merges. Clean finish rewrites deduped + sorted below.
    inc_fh = OUT_JSONL.open("w" if args.fresh else "a", encoding="utf-8")
    try:
        for pid in pids:
            if pid in skip:
                continue
            c_nodes = con.get(pid, {}).get("nodes", []) or []
            r_nodes = raw.get(pid, {}).get("nodes", []) or []
            for c_node in c_nodes:
                members = _members_of(c_node, r_nodes)
                if len(members) < 2:
                    continue                                 # not a real merge
                key = _node_key(pid, c_node.get("fact_ids") or [])
                if (pid, key) in done:
                    continue
                source, truncated = f4._cap(_source(members))
                summary = (c_node.get("text") or "").strip()
                claims, status = f4.judge_unit(source, summary)
                counts = {lab: sum(1 for c in claims if c["label"] == lab) for lab in LABELS}
                rec = {
                    "subject_id": pid, "node_key": key,
                    "salience_tier": c_node.get("salience_tier"),
                    "consolidated_text": summary, "n_members": len(members),
                    "member_texts": [m.get("text", "") for m in members],
                    "fact_ids": sorted(c_node.get("fact_ids") or []),
                    "status": status, "source_truncated": truncated,
                    "supported": counts["supported"], "not_supported": counts["not_supported"],
                    "not_addressable": counts["not_addressable"],
                    "model": f4.SPEC.model_id, "prompt_version": f4.PROMPT_VERSION,
                    "claims": claims,
                }
                new_records.append(rec)
                inc_fh.write(json.dumps(rec) + "\n"); inc_fh.flush()
                n_judged += 1
                logger.info("subj=%s tier=%s members=%d status=%s (sup=%d nsup=%d na=%d) :: %.60s",
                            pid, c_node.get("salience_tier"), len(members), status,
                            counts["supported"], counts["not_supported"],
                            counts["not_addressable"], summary)
    finally:
        inc_fh.close()
        f4._ollama_unload()

    merged = dict(existing)
    for r in new_records:
        merged[(r["subject_id"], r["node_key"])] = r
    with OUT_JSONL.open("w", encoding="utf-8") as fh:
        for k in sorted(merged):
            fh.write(json.dumps(merged[k]) + "\n")

    rows = [r for k, r in merged.items() if k[0] in pid_set]
    by_tier = defaultdict(lambda: {"supported": 0, "not_supported": 0, "not_addressable": 0})
    tot = {"supported": 0, "not_supported": 0, "not_addressable": 0}
    n_err = sum(1 for r in rows if r["status"] == "judge_error")
    n_retry = sum(1 for r in rows if r["status"] in ("retried_nothink", "reparsed"))
    for r in rows:
        for lab in LABELS:
            tot[lab] += r.get(lab, 0)
            by_tier[r.get("salience_tier") or "?"][lab] += r.get(lab, 0)

    def _ff(d):
        denom = d["supported"] + d["not_supported"]
        return round(d["supported"] / denom, 4) if denom else 1.0

    faithful = _ff(tot)
    gate = faithful >= args.threshold
    flagged = sorted((r for r in rows if r["not_supported"] > 0),
                     key=lambda r: -r["not_supported"])
    report = {
        "metric": "ledger_merge_faithfulness",
        "model": f4.SPEC.model_id, "prompt_version": f4.PROMPT_VERSION,
        "threshold": args.threshold, "patients": len(pid_set),
        "merges_judged": len(rows), "merge_faithful_fraction": faithful, "counts": tot,
        "by_tier": {t: {**by_tier[t], "merge_faithful_fraction": _ff(by_tier[t])}
                    for t in sorted(by_tier)},
        "n_judge_errors": n_err, "n_recovered": n_retry,
        "n_merges_with_fabrication": len(flagged),
        "gates": {"gate_merge_faithful": gate}, "all_gates_pass": gate,
    }
    with OUT_JSON.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    if not args.json_only:
        print("\n" + "=" * 60)
        print("F9 LEDGER MERGE FAITHFULNESS (Tier 1, LLM judge)")
        print(f"  model               : {f4.SPEC.model_id}")
        print(f"  patients / merges   : {len(pid_set)} / {len(rows)}  (newly judged {n_judged})")
        print(f"  merge_faithful_frac : {faithful:.4f}   "
              f"(sup={tot['supported']} nsup={tot['not_supported']} na={tot['not_addressable']})")
        for t in sorted(by_tier):
            print(f"    {t:4s}: {_ff(by_tier[t]):.4f}")
        print(f"  judge errors        : {n_err}   recovered: {n_retry}")
        if flagged:
            print(f"\n  {len(flagged)} merge(s) with a fabricated claim (review):")
            for r in flagged[:15]:
                print(f"    subj {r['subject_id']} [{r['salience_tier']}] nsup={r['not_supported']} "
                      f":: {r['consolidated_text'][:70]}")
        print(f"  gate (>= {args.threshold}): {'PASS' if gate else 'FAIL'}")
        print(f"\n  jsonl -> {OUT_JSONL}")
        print(f"  json  -> {OUT_JSON}")

    return 0 if gate else 1


if __name__ == "__main__":
    sys.exit(main())
