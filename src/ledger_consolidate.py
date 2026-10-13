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
ledger_consolidate.py
=====================
Tier-aware LLM-assisted consolidation of a patient's confirmed ledger nodes.
Used by b4_consolidate_ledger.py (and importable for tests). Operates on plain
LedgerNode dicts and returns LedgerNode-shaped dicts (full schema preserved by
building each merged node from its representative member).

Per the salience taxonomy's rule engine:
  T3 chronic   -> STATE ROLL-UP: LLM groups same-condition entries; one canonical
                  multi-year state line per condition.
  T4 recurrent -> GRAPH COLLAPSE: LLM groups episodes by condition; the script
                  formats a clinician-readable metric line (count, date range,
                  rough frequency). LLM only names the condition.
  T5 transient -> PRUNE PAST T: deterministic recency prune vs the patient's
                  established last_date, EXCEPT the kinds in T5_PRUNE_EXEMPT_KINDS
                  (an unconfirmed finding still under surveillance never ages out
                  of the served ledger). No LLM.
T1/T2 pass through untouched; the caller leaves the flagged/provisional bucket alone.

Division of labour (T3/T4): the LLM DECIDES grouping; this module MERGES provenance
deterministically (union source_hadm_ids/fact_ids, summed mentions, min onset / max
last_confirmed, strongest certainty). Guardrail: never merge across current_lifecycle.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime

import ollama

import config
import retention_taxonomy as tax
from roles.contracts import AuditRecord

sys.path.insert(0, str(config.SYNTH_DIR))
import ollama_utils as _ou               # noqa: E402  (shared VRAM / unload)

SPEC = config.model_for("single_summary")          # generation model (ornith:35b)
PROMPT_VERSION = "ledger_consolidate.v1"

T3, T4, T5 = "T3", "T4", "T5"
_TIER_RANK = {"T1": 1, "T2": 2, "T3": 3, "T4": 4, "T5": 5}

# Which significant negatives are exempt from the T5 recency prune
# (config.CONSOLIDATE_T5_PRUNE_DAYS). SERVING-LAYER only: pin membership is untouched,
# every negative_kind still pins at b2 and every pruned node stays recoverable in
# retention_ledger.raw.jsonl.
#   surveillance         -> EXEMPT. An unconfirmed finding still being watched; ageing it
#                           out of the served ledger drops the only standing record that
#                           something needs a repeat scan. Asymmetric cost, same rationale
#                           as tax.is_surveillance_finding.
#   absent_or_negative   -> pruned on age. A screening/workup negative is a dated historical
#                           statement; past the window it is stale, not wrong.
#   resolved_relevant    -> pruned on age. A resolved event whose weight is genuinely
#                           lifetime-lasting should be carrying a T2 class (sentinel_event /
#                           permanent_condition), not sitting at T5.
# Widen only with one; and note the taxonomy's T5 representation string still reads "unless a significant negative", unqualified.
T5_PRUNE_EXEMPT_KINDS = frozenset({"surveillance"})
CERT_RANK = {"confirmed": 4, "probable": 3, "suspected": 2,
             "patient_reported": 1, "reported": 1, "": 0, None: 0}


# ── GPU hygiene (called by the stage at batch end; shared vendor helpers) ────────

gpu_cleanup = _ou.gpu_cleanup


def ollama_unload() -> None:
    _ou.ollama_unload(SPEC.model_id)


# ── LLM grouping (T3 / T4) ───────────────────────────────────────────────────────

_SYS = (
    "You consolidate a patient's background problem-list entries. You decide which "
    "entries describe the SAME underlying clinical condition and should be one entry. "
    "You invent nothing - text must be supported by the member entries."
)

_INSTR = {
    T3: ("These are chronic conditions. Group entries that refer to the SAME underlying "
         "condition (e.g. several phrasings of essential hypertension). For each group "
         "write canonical_text = the most complete, clinically useful single phrasing "
         "drawn from its members."),
    T4: ("These are recurrent acute episodes. Group episodes that are the SAME recurrent "
         "condition (e.g. repeated COPD exacerbations, recurrent UTIs). For each group "
         "write canonical_text = the SHORT condition name only - counts and dates are "
         "added separately, so do not include them."),
}

_TMPL = (
    "{instr}\n\n"
    "RULES:\n"
    "- Every entry appears in exactly ONE group. Singletons are fine.\n"
    "- Do NOT merge entries with different clinical status/lifecycle, or clearly "
    "different conditions.\n"
    "- Be faithful to the members; add no new facts.\n\n"
    "ENTRIES:\n{entries}\n\n"
    'Return ONLY JSON: {{"groups":[{{"members":["n0","n2"],"canonical_text":"..."}}]}}'
)


def _entries_block(nodes: list[dict]) -> str:
    return "\n".join(
        f"n{i} | class={nd.get('retention_class','')} | "
        f"lifecycle={nd.get('current_lifecycle','')} | certainty={nd.get('certainty','')} | "
        f"text=\"{nd.get('text','')}\""
        for i, nd in enumerate(nodes))


def _llm_groups(nodes: list[dict], tier: str) -> tuple[list[dict], str]:
    msgs = [{"role": "system", "content": _SYS},
            {"role": "user", "content": _TMPL.format(instr=_INSTR[tier],
                                                      entries=_entries_block(nodes))}]

    def _call(think):
        r = ollama.chat(model=SPEC.model_id, messages=msgs, think=think, format="json",
                        options={"temperature": 0.1, "num_ctx": 16384}, keep_alive=-1)
        return r.message.content.strip()

    raw = _call(SPEC.think)
    groups = _parse_groups(raw)
    if not groups:
        raw = _call(False)                              # think can eat the budget
        groups = _parse_groups(raw)
    return groups, raw


def _parse_groups(raw: str) -> list[dict]:
    if not raw:
        return []
    try:
        obj = json.loads(raw)
    except Exception:
        a, b = raw.find("{"), raw.rfind("}")
        if a < 0 or b < 0:
            return []
        try:
            obj = json.loads(raw[a:b + 1])
        except Exception:
            return []
    return obj.get("groups", []) if isinstance(obj, dict) else []


# ── deterministic merge + metric ─────────────────────────────────────────────────

def _pd(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d")
    except Exception:
        return None


def _my(s):
    d = _pd(s)
    return d.strftime("%b %Y") if d else ""


def _merge_node(members: list[dict]) -> dict:
    """Build one node from a group, preserving the representative's full schema and
    merging provenance. Representative = most-corroborated (n_mentions, then longest)."""
    rep = max(members, key=lambda n: (int(n.get("n_mentions") or 0), len(n.get("text") or "")))
    out = dict(rep)                                     # preserve all fields
    onsets = [m.get("onset_date") for m in members if m.get("onset_date")]
    lasts = [m.get("last_confirmed_date") for m in members if m.get("last_confirmed_date")]
    best = max(members, key=lambda m: CERT_RANK.get(m.get("certainty"), 0))
    tiers = sorted({m.get("salience_tier") for m in members if m.get("salience_tier")},
                   key=lambda t: _TIER_RANK.get(t, 9))
    out.update({
        "certainty": best.get("certainty", out.get("certainty")),
        "negative_kind": tax.merge_negative_kind(m.get("negative_kind") for m in members),
        "onset_date": min(onsets) if onsets else None,
        "last_confirmed_date": max(lasts) if lasts else None,
        "n_mentions": sum(int(m.get("n_mentions") or 0) for m in members),
        "source_hadm_ids": sorted({h for m in members for h in (m.get("source_hadm_ids") or [])}),
        "fact_ids": sorted({f for m in members for f in (m.get("fact_ids") or [])}),
        "salience_tier": tiers[0] if tiers else out.get("salience_tier"),
    })
    return out


def _metric_text(label: str, node: dict) -> str:
    """Clinician-readable recurrence line, e.g.
    'COPD exacerbation: 7 episodes from Oct 2202 to Jan 2207, about 1.4 per year'."""
    count = len(node.get("source_hadm_ids") or []) or int(node.get("n_mentions") or 0) or 1
    first, last = _my(node.get("onset_date")), _my(node.get("last_confirmed_date"))
    base = (label or node.get("text", "")).strip().rstrip(".")
    if count <= 1:
        return f"{base} ({last})" if last else base
    span = (f"from {first} to {last}" if first and last and first != last
            else (f"as of {last}" if last else ""))
    txt = f"{base}: {count} episodes" + (f" {span}" if span else "")
    d0, d1 = _pd(node.get("onset_date")), _pd(node.get("last_confirmed_date"))
    if d0 and d1:
        yrs = (d1 - d0).days / 365.25
        if yrs >= 1.0:
            txt += f", about {count / yrs:.1f} per year"
    return txt


def _members_of(g: dict, nodes: list[dict], seen: set) -> list[int]:
    idxs = []
    for h in g.get("members", []):
        try:
            i = int(str(h).lstrip("n"))
        except Exception:
            continue
        if 0 <= i < len(nodes) and i not in seen:
            idxs.append(i)
    seen.update(idxs)
    return idxs


def _audit(subject_id: int, nodes: list[dict], groups: list, raw: str) -> dict:
    src = sorted({h for n in nodes for h in (n.get("source_hadm_ids") or [])})
    rec = AuditRecord.from_spec(
        SPEC, PROMPT_VERSION, subject_id=subject_id, input_hadm_ids=src, output_text=raw,
        scores={"nodes_in": len(nodes), "groups_out": len(groups)},
        status="ok" if groups else "flagged")
    return json.loads(rec.model_dump_json())


def _roll_up(subject_id: int, nodes: list[dict], tier: str) -> tuple[list[dict], list[dict]]:
    if len(nodes) <= 1:
        if tier == T4 and nodes:
            m = dict(nodes[0])
            m["text"] = _metric_text(nodes[0].get("text", ""), nodes[0])
            return [m], []
        return list(nodes), []

    groups, raw = _llm_groups(nodes, tier)
    audits = [_audit(subject_id, nodes, groups, raw)]
    if not groups:
        return list(nodes), audits                      # parse failure -> no change (safe)

    out, seen = [], set()
    for g in groups:
        idxs = _members_of(g, nodes, seen)
        if not idxs:
            continue
        members = [nodes[i] for i in idxs]
        if len(members) > 1 and len({m.get("current_lifecycle") for m in members}) > 1:
            out.extend(members)                         # guardrail: never cross lifecycle
            continue
        merged = _merge_node(members)
        label = (g.get("canonical_text") or members[0].get("text", "")).strip()
        merged["text"] = _metric_text(label, merged) if tier == T4 else (label or merged["text"])
        out.append(merged)
    for i, nd in enumerate(nodes):                      # never silently lose a node
        if i not in seen:
            out.append(nd)
    return out, audits


def _prune_t5(nodes: list[dict], ref_date, days: int) -> tuple[list[dict], list[dict]]:
    kept, pruned = [], []
    ref = _pd(ref_date)
    for nd in nodes:
        if nd.get("negative_kind") in T5_PRUNE_EXEMPT_KINDS:
            kept.append(nd)                             # still under surveillance -> never ages out
            continue
        d = _pd(nd.get("last_confirmed_date") or nd.get("onset_date"))
        if ref is None or d is None:
            kept.append(nd)                             # unknown age -> keep (safe)
            continue
        (pruned if (ref - d).days > days else kept).append(nd)
    return kept, pruned


def consolidate_nodes(subject_id: int, nodes: list[dict], ref_date) -> tuple[list[dict], list[dict], dict]:
    """Return (new_nodes, audits, report). Confirmed nodes only -- caller passes the
    'nodes' bucket; T1/T2 pass through, the flagged bucket is the caller's to leave alone."""
    by = {T3: [], T4: [], T5: []}
    out = []
    for nd in nodes:
        t = nd.get("salience_tier")
        (by[t].append(nd) if t in by else out.append(nd))    # T1/T2/other pass through

    audits, report = [], {}

    if config.CONSOLIDATE_T3:
        r3, a3 = _roll_up(subject_id, by[T3], T3)
        out += r3; audits += a3; report["t3"] = [len(by[T3]), len(r3)]
    else:
        out += by[T3]

    if config.CONSOLIDATE_T4:
        r4, a4 = _roll_up(subject_id, by[T4], T4)
        out += r4; audits += a4; report["t4"] = [len(by[T4]), len(r4)]
    else:
        out += by[T4]

    days = config.CONSOLIDATE_T5_PRUNE_DAYS
    if days and days > 0:
        keep, prune = _prune_t5(by[T5], ref_date, days)
        out += keep
        report["t5"] = {"before": len(by[T5]), "kept": len(keep), "pruned": len(prune)}
    else:
        out += by[T5]

    return out, audits, report
