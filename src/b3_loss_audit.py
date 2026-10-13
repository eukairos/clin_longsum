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
Digest of potential must-retain losses in retention_facts.jsonl.

Runs on the workstation (no LLM). Writes a compact data/interim/loss_audit.md
so the dropped bucket can be reviewed without scrolling the full ~1.5MB jsonl.

"Loss" here = a fact that is NOT pinned to the ledger (per the real
retention_taxonomy.is_ledger_member rule, after the surveillance-finding promotion
B2 also applies) AND carries a salient keyword - a candidate for a genuinely missed
must-retain fact. A second section lists facts whose retention_class is a must-retain
class but which still did not pin (mis-tag, or suspected/superseded/patient_reported
edge cases) - the inverse signal.

REDUNDANCY CHECK: most flagged items are harmless because the SAME condition is
already represented by another ledger entry. For each flagged item this digest 
reuses b2's matching (same-class token cluster, curated condition key, phrase
containment; plus episode rollups and considered_excluded) against the patient's
ledger and labels it:
  REDUNDANT [home: <covering entry>]  - condition covered elsewhere; ignore.
  REVIEW                              - not represented; check the BHC (resolved,
                                        or a genuine miss). Use (subj, hadm) to find it.
The ledger is read raw (retention_ledger.raw.jsonl) when the served one is consolidated,
so coverage is judged against the lossless set. If no ledger exists yet, items are all
REVIEW with a note - run b2 first.

    cd C:~/Summarization/clin_longsum/src
    python b3_loss_audit.py
"""

import json
import re
import collections

import config
import retention_taxonomy as tax
import b2_build_ledger as b2
import holdout

FACTS = config.RETENTION_FACTS_JSONL
OUT = config.INTERIM_DIR / "loss_audit.md"
LEDGER_JSONL = config.OUTPUT_DIR / "retention_ledger.jsonl"
RAW_LEDGER = config.OUTPUT_DIR / "retention_ledger.raw.jsonl"

# High-signal salient terms (word-boundaried where a substring would false-match,
# e.g. CRE in 'creatinine', stent in 'consistent').
RED = re.compile(
    r"allerg|anaphylax|MRSA|VRE|\bCRE\b|tubercul|splenectom|transplant|graft|"
    r"pacemaker|defibrillat|\bICD\b|prosthe|\bstent|colostom|amputat|cancer|carcinoma|"
    r"malignan|metasta|lymphoma|leukaem|leukem|cirrhosis|varice|asplen|\bHIV\b|\bDNR\b|"
    r"\bDNI\b|seizure|epilep|dialysis|fistula|\bESRD\b|\bCKD\b|\bCVA\b|stroke|infarct|"
    r"haemorrhage|hemorrhage|embol|\bDVT\b|\bVTE\b|aneurysm|nodule|\bmass\b|lesion|cyst|"
    r"immunosupp|chemotherap|tacrolimus|cyclospor|sirolimus|warfarin|anticoagul",
    re.I,
)

MUST_CLASSES = {
    "severe_adr_allergy", "infection_control_status", "code_status_directive",
    "sentinel_event", "implanted_device", "anatomy_altering_surgery",
    "permanent_condition", "altered_immune_status", "transfusion_obstetric_history",
    "chronic_disease_state",
}


def pinned(f: dict) -> bool:
    return tax.is_ledger_member(
        f["salience_tier"], f["certainty"], f["lifecycle"],
        f.get("negative_kind"), f.get("retention_class"))


def _load_ledgers():
    """Patient ledgers for the redundancy check. Prefer the raw (lossless) ledger when
    the served one is consolidated, so 'represented elsewhere' is judged against the
    full set. Returns ({subject_id: ledger}, source_name|None)."""
    def rd(p):
        return ([json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
                if p.exists() else [])
    primary = rd(LEDGER_JSONL)
    if not primary:
        return {}, None
    src = LEDGER_JSONL
    if primary[0].get("consolidated") and RAW_LEDGER.exists():
        src = RAW_LEDGER
    return {int(l["subject_id"]): l for l in rd(src)}, src.name


def _represented_elsewhere(f: dict, led: dict):
    """Is the flagged fact's condition represented by some OTHER ledger entry? Reuses
    B2's matching so 'related entry' means exactly what the pipeline means. Returns
    (home, covering_text) or None."""
    if not led:
        return None
    ft = b2._content_tokens(f["text"])
    grp = b2._group(f["retention_class"])
    fkey = b2._condition_key(f["text"])
    for home in ("nodes", "flagged", "amended"):
        for e in led.get(home, []):
            if b2._group(e["retention_class"]) != grp:
                continue
            et = b2._content_tokens(e["text"])
            if ((len(ft & et) >= b2._MIN_SHARED and b2._overlap(ft, et) >= b2.CLUSTER_THRESH)
                    or (fkey and fkey == b2._condition_key(e["text"]))
                    or b2._phrase_contained(f["text"], e["text"])):
                return home, e["text"]
    for ep in led.get("episodes", []):
        et = b2._content_tokens(ep.get("parent_condition", ""))
        if len(ft & et) >= b2._MIN_SHARED and b2._overlap(ft, et) >= b2.CLUSTER_THRESH:
            return "episode", ep.get("parent_condition", "")
    for t in led.get("considered_excluded", []):
        et = b2._content_tokens(t)
        if len(ft & et) >= b2._MIN_SHARED and b2._overlap(ft, et) >= b2.CLUSTER_THRESH:
            return "excluded", t
    return None


def _verdict(f: dict, ledgers: dict, have_ledger: bool) -> tuple[str, bool]:
    """(verdict_string, is_redundant)."""
    if not have_ledger:
        return "REVIEW (no ledger loaded -- run b2 first)", False
    rep = _represented_elsewhere(f, ledgers.get(f.get("_subject_id")))
    if rep:
        return f"REDUNDANT [{rep[0]}: {rep[1][:80]}]", True
    return "REVIEW -- not represented in ledger; check BHC (resolved? or genuine miss)", False


def main() -> None:
    rows = [json.loads(l) for l in FACTS.read_text(encoding="utf-8").splitlines() if l.strip()]
    pids = {r["subject_id"] for r in rows}
    facts = []
    for r in rows:
        for f in r.get("facts", []):
            f["_subject_id"] = r["subject_id"]
            facts.append(f)

    # Holdout consistency: the loss audit must cover the SAME span as the ledger it is
    # checked against - the ESTABLISHED record. b2 excludes the held-out (incoming)
    # admission from the ledger, so its facts must be excluded here too, or every
    # holdout admission's salient facts surface as false 'REVIEW' losses (they are
    # correctly absent from the established ledger; the incoming admission is reviewed
    # directly in the UI, not via the ledger).
    hold = holdout.holdout_hadms()
    n_hold_facts = 0
    if hold:
        before = len(facts)
        facts = [f for f in facts if f.get("source_hadm_id") not in hold]
        n_hold_facts = before - len(facts)

    # Mirror b2 via the shared normalizer: surveillance promotion + disease-course
    # reclassification (tax.normalize_fact - same rules b2/c1/db_persist apply).
    promoted = 0
    reclassified = 0
    for f in facts:
        p, r = tax.normalize_fact(f)
        promoted += p
        reclassified += r

    n_facts = len(facts)
    n_pinned = sum(pinned(f) for f in facts)
    by_tier = collections.Counter(f["salience_tier"] for f in facts if pinned(f))
    by_class = collections.Counter(f["retention_class"] for f in facts if pinned(f))

    # Potential losses: not pinned + salient keyword, deduped by text.
    seen, losses = set(), []
    for f in facts:
        if not pinned(f) and RED.search(f["text"]) and f["text"] not in seen:
            seen.add(f["text"])
            losses.append(f)
    losses.sort(key=lambda f: (f["salience_tier"], f["retention_class"]))

    # Inverse signal: must-retain class but not pinned.
    seen2, unpinned_must = set(), []
    for f in facts:
        if f["retention_class"] in MUST_CLASSES and not pinned(f) and f["text"] not in seen2:
            seen2.add(f["text"])
            unpinned_must.append(f)

    ledgers, ledger_src = _load_ledgers()
    have_ledger = bool(ledgers)

    # Compute verdicts up front so the header can summarise REVIEW counts.
    loss_v = [(f, *_verdict(f, ledgers, have_ledger)) for f in losses]
    must_v = [(f, *_verdict(f, ledgers, have_ledger)) for f in unpinned_must]
    loss_review = sum(1 for _, _, red in loss_v if not red)
    must_review = sum(1 for _, _, red in must_v if not red)

    out = [
        "# Loss audit -- retention_facts.jsonl\n",
        f"- patients: {len(pids)}   admissions: {len(rows)}   facts: {n_facts}",
        f"- holdout admissions excluded: {len(hold)}  (facts dropped: {n_hold_facts})",
        f"- pinned (in ledger): {n_pinned} ({100 * n_pinned / max(n_facts, 1):.0f}%)",
        f"- surveillance promotions applied: {promoted}",
        f"- disease-course reclassifications applied: {reclassified}",
        f"- pinned by tier: {dict(sorted(by_tier.items()))}",
        f"- pinned by class: {dict(by_class.most_common())}",
        f"- redundancy checked against: {ledger_src or '(no ledger -- run b2 first)'}",
        f"- potential losses: {len(losses)}  (REVIEW: {loss_review}, redundant: {len(losses) - loss_review})",
        f"- must-retain unpinned: {len(unpinned_must)}  (REVIEW: {must_review}, "
        f"redundant: {len(unpinned_must) - must_review})\n",
        "TIP: scan for 'REVIEW' -- those are the only items needing a BHC check.\n",
        f"## Potential losses (not pinned + salient keyword) -- {len(losses)} distinct\n",
        "format: [tier/class/certainty/lifecycle/neg_kind] (subj, hadm) text  => verdict\n",
    ]
    for f, verdict, _ in loss_v:
        out.append(f"- [{f['salience_tier']}/{f['retention_class']}/{f['certainty']}/"
                   f"{f['lifecycle']}/nk={f.get('negative_kind') or '-'}] "
                   f"(subj {f.get('_subject_id')}, hadm {f.get('source_hadm_id')}) {f['text']}\n"
                   f"    => {verdict}")
    out.append(f"\n## Must-retain class but NOT pinned -- {len(unpinned_must)} distinct\n")
    for f, verdict, _ in must_v:
        out.append(f"- [{f['retention_class']}/{f['certainty']}/{f['lifecycle']}] "
                   f"(subj {f.get('_subject_id')}, hadm {f.get('source_hadm_id')}) {f['text']}\n"
                   f"    => {verdict}")

    OUT.write_text("\n".join(out), encoding="utf-8")
    print(f"wrote {OUT}")
    print(f"  facts={n_facts} (holdout facts excluded={n_hold_facts}) pinned={n_pinned} "
          f"promoted={promoted} reclassified={reclassified}")
    print(f"  potential losses={len(losses)} (REVIEW {loss_review})  "
          f"unpinned_must={len(unpinned_must)} (REVIEW {must_review})")
    print(f"  redundancy vs {ledger_src or 'NO LEDGER (run b2 first)'}")


if __name__ == "__main__":
    main()
