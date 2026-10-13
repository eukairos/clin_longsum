# clin_longsum - physician feedback loop
# Copyright (C) 2026 Eukairos
# SPDX-License-Identifier: AGPL-3.0-only
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of version 3 of the GNU Affero General Public License as published
# by the Free Software Foundation. This program is distributed WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE. See the GNU Affero General Public License for details.

"""
replay_policy.py
================
Replay a candidate triage policy over every historical report and diff its
decisions against the recorded ones.

THIS IS WHAT MAKES "GOVERNED LIKE THE TAXONOMY" TRUE. Without it, "we bumped the
policy version and got sign-off" is ceremony: nobody can say what the new version
would actually have done. With it, a proposed change to an urgency gate or a
routing rule is presented for sign-off alongside the exact list of past reports it
would have decided differently. That is only possible because triage is
deterministic.A model-based triage can be sampled and audited, but not replayed.

    # would the current file decide anything differently from what is recorded?
    python -m support.replay_policy

    # shadow-promote a candidate before adopting it
    python -m support.replay_policy --candidate support/policy/triage_policy.v1.1.yaml

    # why did one report land where it did?
    python -m support.replay_policy --explain 1247

    # machine-readable, for attaching to a sign-off record
    python -m support.replay_policy --candidate <path> --json out/replay_v1.1.json

    # quick pass over the first N reports only
    python -m support.replay_policy --limit 50

Exit status is 1 when any card's decision changes, so shadow promotion can gate a
release rather than being advisory.

POINT-IN-TIME CORRECTNESS
-------------------------
The subtle way to get this wrong is to replay U-5 - the systematic-defect gate -
against TODAY's dedup counts. Every historical card would then appear to have been
urgent, because by now several other reports share its signature. Replay therefore
reconstructs the dedup context AS OF each card's own created_at: only reports
filed strictly earlier, inside the lookback window ending at that moment, are
counted. Get this wrong and the harness reports mass disagreement on an unchanged
policy, which trains everyone to ignore it.

Evidence is treated differently and deliberately: it is a fact about the world
(is this fact_id in retention_dropped or not), not a time-varying opinion, so the
latest collected evidence is used regardless of when it was collected. A card
whose S1 sub-class was unresolved at capture and resolved later SHOULD replay to
the resolved answer.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

try:
    from support import policy_loader as pol_mod
    from support import service as svc
    from support import triage as triage_mod
except ImportError:  # pragma: no cover - direct execution from inside support/
    import policy_loader as pol_mod
    import service as svc
    import triage as triage_mod

# Fields compared between recorded and replayed decisions. Deliberately excludes
# state, disposition and reply_text: those are human decisions, not policy output,
# and a policy change must never appear to revise them.
COMPARED = ("urgency", "urgency_rule_id", "owner_roles", "defect_class", "s1_subclass")

CARD_COLS = (
    "feedback_id", "created_at", "symptom", "disputed_field", "clinical_action_flag",
    "seen_other_patients", "subject_id", "cohort", "fact_text", "salience_tier",
    "retention_class", "certainty", "current_lifecycle", "negative_kind",
    "policy_version", "urgency", "urgency_rule_id", "owner_roles", "defect_class",
    "s1_subclass", "dedup_key",
)


def _fetch_cards(cur, limit: Optional[int] = None) -> list[dict]:
    cur.execute(f"""SELECT {', '.join(CARD_COLS)}
                      FROM feedback ORDER BY created_at, feedback_id
                      {'LIMIT %s' if limit else ''};""",
                (limit,) if limit else None)
    return [dict(zip(CARD_COLS, r)) for r in cur.fetchall()]


def _latest_evidence(cur, feedback_id: int) -> Optional[dict]:
    cur.execute("""SELECT evidence FROM feedback_evidence
                    WHERE feedback_id=%s ORDER BY collected_at DESC LIMIT 1;""",
                (feedback_id,))
    row = cur.fetchone()
    return row[0] if row else None


def _dedup_context_asof(cur, key: str, subject_id: int, created_at,
                        lookback_days: int) -> triage_mod.DedupContext:
    """Dedup corroboration as it stood when the card was filed.

    Strictly earlier cards only, inside the window ending at created_at. See the
    point-in-time note in the module docstring - this is the one place replay can
    silently lie.
    """
    cur.execute(
        """SELECT subject_id, feedback_id
             FROM feedback
            WHERE dedup_key = %s
              AND created_at <  %s
              AND created_at >= %s - make_interval(days => %s);""",
        (key, created_at, created_at, lookback_days))
    rows = cur.fetchall()
    subjects = {r[0] for r in rows} | {subject_id}
    return triage_mod.DedupContext(distinct_subjects=len(subjects),
                                   prior_card_ids=tuple(r[1] for r in rows))


def _lookback_days(policy) -> int:
    for r in policy.urgency_rules:
        if r.id == "U-5":
            return int(r.params.get("lookback_days", 30))
    return 30


def _norm(v: Any) -> Any:
    """Compare owner_roles as an ordered list, everything else as-is."""
    return list(v) if isinstance(v, (list, tuple)) else v


def replay(conn, candidate_path: Optional[str] = None,
           limit: Optional[int] = None) -> dict:
    policy = pol_mod.load(candidate_path) if candidate_path else pol_mod.load()
    # A candidate naming a rule this build does not implement must fail here,
    # loudly, rather than silently replaying with that gate disabled.
    pol_mod.validate_against_implementations(policy, set(triage_mod._RULES))
    engine = triage_mod.RuleTriage(policy)
    lookback = _lookback_days(policy)

    cur = conn.cursor()
    cards = _fetch_cards(cur, limit)
    diffs, rule_counts, unchanged = [], Counter(), 0

    for card in cards:
        key = triage_mod.dedup_key(card["symptom"], card["retention_class"],
                                   card["salience_tier"], card["fact_text"])
        ctx = _dedup_context_asof(cur, key, card["subject_id"],
                                  card["created_at"], lookback)
        try:
            result = engine.triage(card, ctx, evidence=_latest_evidence(cur, card["feedback_id"]))
        except pol_mod.PolicyError as e:
            # A symptom retired by the candidate. That is a real finding, not a
            # crash: retiring a symptom orphans every card that used it.
            diffs.append({"feedback_id": card["feedback_id"], "error": str(e),
                          "recorded_policy": card["policy_version"]})
            continue

        for r in result.fired_rules:
            rule_counts[r] += 1

        changed = {}
        for f in COMPARED:
            was, now = _norm(card.get(f)), _norm(getattr(result, f))
            if was != now:
                changed[f] = {"recorded": was, "replayed": now}
        if changed:
            diffs.append({
                "feedback_id": card["feedback_id"],
                "symptom": card["symptom"],
                "tier": card["salience_tier"],
                "fact_text": (card["fact_text"] or "")[:80],
                "recorded_policy": card["policy_version"],
                "changed": changed,
                "fired_rules": result.fired_rules,
            })
        else:
            unchanged += 1

    return {
        "candidate_policy": policy.version,
        "candidate_path": str(policy.source_path),
        "n_cards": len(cards),
        "n_unchanged": unchanged,
        "n_changed": len(diffs),
        "recorded_policy_versions": dict(Counter(c["policy_version"] for c in cards)),
        "rule_fire_counts": dict(rule_counts),
        "diffs": diffs,
    }


def explain(conn, feedback_id: int, candidate_path: Optional[str] = None) -> dict:
    """Full derivation for one card: every rule, fired or not, and why."""
    policy = pol_mod.load(candidate_path) if candidate_path else pol_mod.load()
    cur = conn.cursor()
    cur.execute(f"SELECT {', '.join(CARD_COLS)} FROM feedback WHERE feedback_id=%s;",
                (feedback_id,))
    row = cur.fetchone()
    if not row:
        raise svc.ServiceError(f"no such report: {feedback_id}")
    card = dict(zip(CARD_COLS, row))

    key = triage_mod.dedup_key(card["symptom"], card["retention_class"],
                               card["salience_tier"], card["fact_text"])
    ctx = _dedup_context_asof(cur, key, card["subject_id"], card["created_at"],
                              _lookback_days(policy))
    evidence = _latest_evidence(cur, feedback_id)
    result = triage_mod.RuleTriage(policy).triage(card, ctx, evidence=evidence)

    rules = []
    for r in policy.urgency_rules:
        fn = triage_mod._RULES.get(r.id)
        fired = bool(fn and fn(card, ctx, r.params))
        rules.append({"rule": r.id, "fired": fired, "description": r.description})

    return {
        "feedback_id": feedback_id,
        "symptom": card["symptom"],
        "tier": card["salience_tier"],
        "disputed_field": card["disputed_field"],
        "clinical_action_flag": card["clinical_action_flag"],
        "dedup_key": key,
        "distinct_subjects_asof": ctx.distinct_subjects,
        "prior_cards_asof": list(ctx.prior_card_ids),
        "evidence_present": evidence is not None,
        "rules": rules,
        "recorded": {f: _norm(card.get(f)) for f in COMPARED},
        "replayed": {f: _norm(getattr(result, f)) for f in COMPARED},
        "notes": result.notes,
    }


# ── reporting ──────────────────────────────────────────────────────────────────

def _print_report(rep: dict) -> None:
    print(f"candidate : {rep['candidate_policy']}  ({rep['candidate_path']})")
    print(f"cards     : {rep['n_cards']}  unchanged {rep['n_unchanged']}  "
          f"CHANGED {rep['n_changed']}")
    if rep["recorded_policy_versions"]:
        print("recorded under:")
        for v, n in sorted(rep["recorded_policy_versions"].items(), key=lambda x: str(x[0])):
            print(f"  {v or '(none)'}: {n}")
    if rep["rule_fire_counts"]:
        print("rule fire counts under candidate:")
        for r, n in sorted(rep["rule_fire_counts"].items()):
            print(f"  {r}: {n}")
    if not rep["diffs"]:
        print("\nNo decision changes. Candidate is behaviourally identical on this history.")
        return
    print(f"\n{len(rep['diffs'])} card(s) would be decided differently:")
    for d in rep["diffs"]:
        if "error" in d:
            print(f"  #{d['feedback_id']}  ERROR: {d['error']}")
            continue
        print(f"  #{d['feedback_id']}  {d['symptom']} {d['tier'] or '-'}  "
              f"{d['fact_text']!r}")
        for fld, ch in d["changed"].items():
            print(f"      {fld}: {ch['recorded']!r} -> {ch['replayed']!r}")


def _print_explain(x: dict) -> None:
    print(f"report #{x['feedback_id']}  {x['symptom']}  tier={x['tier'] or '-'}  "
          f"disputed_field={x['disputed_field'] or '-'}")
    print(f"dedup {x['dedup_key']}  distinct subjects as-of filing: "
          f"{x['distinct_subjects_asof']}  prior cards: {x['prior_cards_asof'] or 'none'}")
    print(f"evidence collected: {'yes' if x['evidence_present'] else 'no'}")
    print("rules:")
    for r in x["rules"]:
        print(f"  [{'X' if r['fired'] else ' '}] {r['rule']}  {r['description'].strip()[:90]}")
    print(f"recorded: {x['recorded']}")
    print(f"replayed: {x['replayed']}")
    for n in x["notes"]:
        print(f"  note: {n}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Replay a candidate triage policy over recorded reports.")
    ap.add_argument("--candidate", help="Path to a candidate policy YAML "
                                        "(default: the active policy file).")
    ap.add_argument("--explain", type=int, metavar="FEEDBACK_ID",
                    help="Show the full derivation for one report and exit.")
    ap.add_argument("--limit", type=int, help="Replay only the first N reports.")
    ap.add_argument("--json", metavar="PATH", help="Also write the report as JSON.")
    args = ap.parse_args()

    conn = svc.connect()
    try:
        if args.explain:
            _print_explain(explain(conn, args.explain, args.candidate))
            return
        rep = replay(conn, args.candidate, args.limit)
    finally:
        conn.close()

    _print_report(rep)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
        print(f"\nwrote {args.json}")

    # Non-zero on any change, so shadow promotion can GATE a release rather than
    # being an advisory printout somebody skims.
    sys.exit(1 if rep["n_changed"] else 0)


if __name__ == "__main__":
    main()
