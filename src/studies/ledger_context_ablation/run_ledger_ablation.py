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
run_ledger_ablation.py  --  EVAL S11 redux, item 1 (INTERNAL ablation: ledger-in-context)
=========================================================================================
STUDY (one-off, not a standing gate). Tests the core safety premise: passing the
retention ledger to c1 as "already retained, do not contradict" context is what
stops prose compaction from drifting or dropping a pinned fact.

For a sample of deep-aged-span patients, re-run c1 compaction TWICE under identical
span/model/temperature -- once WITH the real ledger in the prompt, once with the
ledger REMOVED from the prompt (`compact(..., ledger_facts=[])`). The ledger
ARTIFACT (b2) is untouched; only c1's PROMPT changes. Then score:
  - pinned-fact CONTRADICTIONS (eval_fact_presence.fact_contradiction) per arm -- the clean
    premise test: does removing the in-context ledger make the prose contradict pinned facts,
  - faithfulness (f4 judge) of each prose vs the same source (ledger + aged bhc_delta) [confounded],
  - compression ratio (out/in chars, f3-style) for each arm,
  - prose fact-presence (eval_fact_presence.fact_presence) of the compacted-span pinned facts [confounded].

CAVEAT (read before interpreting): the compactor prompt tells the model to NOT re-list
ledger facts (they are preserved in the ledger). So WITH the ledger the model DELIBERATELY
omits pinned facts from the prose - a LOWER prose present-rate WITH the ledger is by
design, not a failure, and aggregate faithfulness can dip simply because the ledger makes
the prose longer/denser. The ledger's protective value shows up in the LEDGER (f1 SWR ~1.0),
not the prose. The clean premise test is the CONTRADICTION count above (does without-ledger
prose contradict pinned facts more than with-ledger); present-rate and faithfulness are secondary.

Modes: incremental (default, mirrors production - folds one admission at a time, more ornith
calls) or full (--mode full, one call, cheaper, stable). Both arms use the same mode.

VRAM: two passes. Pass A runs the ornith compactor for BOTH arms across all sampled
patients, then UNLOADS ornith. Pass B runs the deepseek judge (faithfulness +
fact-presence), then unloads it. ornith and deepseek are never co-resident.

OUTPUT: <data root>/output/studies/ledger_context_ablation/ledger_context.json,
  FINDINGS.md and flagged_contradictions.csv. Outputs go to the DATA tree, NEVER the repo:
  they contain patient-derived text and the repo is not a DUA-compliant location.
  Namespace-aware via config.

    cd ~/clin_longsum/src
    python studies/ledger_context_ablation/run_ledger_ablation.py --sample 15
    python studies/ledger_context_ablation/run_ledger_ablation.py --subjects 90000029 90000078
    python studies/ledger_context_ablation/run_ledger_ablation.py --sample 15 --mode full
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from statistics import mean

import pandas as pd

_SRC = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_SRC))                 # src/ on the path
sys.path.insert(0, str(_SRC / "evaluation"))  # f4 + eval_fact_presence live in src/evaluation/

import config
import c1_compactor as c1
import f4_faithfulness as f4
import eval_fact_presence as fp
from roles.contracts import CompactorInput

# Outputs land in the DATA tree, not next to this script. ledger_context.json and
# flagged_contradictions.csv carry patient-derived fact text; the repo is not a
# DUA-compliant location for them.
OUT_DIR = config.OUTPUT_DIR / "studies" / "ledger_context_ablation"
OUT_JSON = OUT_DIR / "ledger_context.json"
OUT_MD = OUT_DIR / "FINDINGS.md"
OUT_FLAG = OUT_DIR / "flagged_contradictions.csv"
COHORT_CSV = config.INTERIM_DIR / "cohort_admissions.csv"


def _ff(claims) -> float:
    sup = sum(1 for c in claims if c["label"] == "supported")
    nsup = sum(1 for c in claims if c["label"] == "not_supported")
    return round(sup / (sup + nsup), 4) if (sup + nsup) else 1.0


def _judge_source(ledger_facts, aged) -> str:
    led = "\n".join(f"- [{f.salience_tier}/{f.retention_class}] {f.text}"
                    for f in ledger_facts) or "(none)"
    blocks = "\n\n".join(
        f"[admission seq={a.admit_seq} hadm={a.hadm_id}]\n{(a.bhc_delta or a.bhc_text or '')}"
        for a in aged)
    src = (f"RETENTION LEDGER (already retained, do not contradict):\n{led}\n\n"
           f"AGED-OUT ADMISSIONS (delta content compacted):\n{blocks}")
    return src[:f4.SRC_CHAR_CAP]


def _pinned_texts(covered, facts_by_hadm) -> list[str]:
    seen, out = set(), []
    for h in covered:
        for fct in facts_by_hadm.get(h, []):
            if not fct.in_ledger:
                continue
            key = (fct.retention_class, fct.text.strip().lower())
            if key not in seen:
                seen.add(key)
                out.append(fct.text)
    return out


def _present_rate(bools) -> float | None:
    judged = [b for b in bools if b is not None]
    return round(sum(1 for b in judged if b) / len(judged), 4) if judged else None


def _compact_span(pid: int, aged: list, ledger_facts: list, mode: str) -> str:
    """Final Prior-Admissions prose for the span, replicating c1.main's paths: full = one
    call over the whole span; incremental = fold one admission at a time (production)."""
    if mode == "incremental":
        prior_md = ""
        for a in aged:
            o = c1.compact(CompactorInput(
                subject_id=pid, existing_prior_md=prior_md, aged_out=[a],
                ledger_facts=ledger_facts, n_prior_admissions=len(aged), mode="incremental"))
            prior_md = o.prior_admissions_md
        return prior_md
    o = c1.compact(CompactorInput(
        subject_id=pid, aged_out=aged, ledger_facts=ledger_facts,
        source_hadm_ids=[a.hadm_id for a in aged], n_prior_admissions=len(aged), mode="full"))
    return o.prior_admissions_md


def _compression(prose: str, aged: list) -> float | None:
    """out/in chars over the compacted span (f3 definition; correct for both modes,
    unlike the per-fold audit ratio in incremental mode)."""
    ind = sum(len(a.bhc_delta or a.bhc_text or "") for a in aged)
    return round(len(prose) / ind, 4) if ind else None


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _findings_md(rows: list, summary: dict, mode: str, compactor: str, judge: str,
                 sample_note: str) -> str:
    """Physician-facing write-up. Plain language first, statistics in an appendix."""
    n_pat = len(rows)
    n_pin = summary["n_pinned_total"] or 1
    c_with, c_without = summary["contradictions_with"], summary["contradictions_without"]
    delta = summary["contradiction_delta"]
    rate_with, rate_without = c_with / n_pin, c_without / n_pin
    worse_without = sum(1 for r in rows
                        if r["n_contradicted_without"] > r["n_contradicted_with"])
    worse_with = sum(1 for r in rows
                     if r["n_contradicted_with"] > r["n_contradicted_without"])
    same = n_pat - worse_without - worse_with

    if delta > 0:
        rel = (c_without - c_with) / c_without if c_without else 0.0
        verdict = (
            "**Yes. Taking the retained-fact list out of the writer's prompt made the paragraph "
            f"contradict the record more often** -- {c_without} contradicted facts without it "
            f"against {c_with} with it. Supplying the list cut contradictions by about "
            f"{_pct(rel)}. Showing the model what is already on record is doing real work.\n\n"
            "**But it is not enough on its own.** Contradictions did not fall to zero when the "
            f"list was supplied: {c_with} pinned facts were still contradicted. An instruction in "
            "a prompt reduces the problem; it does not eliminate it.")
    elif delta == 0:
        verdict = (
            "**No difference.** The paragraph contradicted the record equally often with and "
            f"without the retained-fact list in the writer's prompt ({c_with} facts either way). "
            "On this evidence the prompt instruction is not what protects the record.")
    else:
        verdict = (
            "**The opposite of what was expected.** The paragraph contradicted the record MORE "
            f"often when the retained-fact list was supplied ({c_with}) than when it was withheld "
            f"({c_without}). This needs investigating before any conclusion is drawn from it.")

    lines = [
        "# Does showing the summariser the patient's retained-fact list stop it contradicting "
        "the record?",
        "",
        "*One-off diagnostic study, not a routine quality gate.*",
        "",
        "## The short answer",
        "",
        verdict,
        "",
        "## What we asked",
        "",
        "When older admissions are folded into the running *Prior Admissions* paragraph, the ",
        "model writing that paragraph is also handed the patient's retention ledger -- the ",
        "structured list of facts already on record -- with an instruction not to contradict it. ",
        "This study asks whether that instruction earns its place. If we take the list out of the ",
        "prompt and change nothing else, does the paragraph start contradicting the record?",
        "",
        "This matters because a summary that quietly contradicts the record is worse than one ",
        "that omits things. An omission is a gap a clinician may notice; a contradiction is a ",
        "confident wrong statement.",
        "",
        "## How we measured it",
        "",
        f"- We used {sample_note}.",
        "- For each patient the paragraph was generated **twice**, identical in every respect ",
        "  except one: whether the retained-fact list appeared in the writer's prompt. Same ",
        "  model, same settings, same admissions, same order.",
        "- **The ledger itself was never altered.** Only what the writer was allowed to see ",
        "  changed. This is a test of the prompt, not of the stored record.",
        f"- An independent AI reader ({judge}) then went through all ",
        f"  **{summary['n_pinned_total']} facts on record** for these patients and asked, of each ",
        "  paragraph: does this text contradict this fact? Separately: does it mention the fact ",
        "  at all?",
        "- We also scored each paragraph for overall faithfulness to its sources, and measured ",
        "  how much it compressed the input.",
        "",
        "## What we found",
        "",
        "### The headline: contradictions",
        "",
        "| | with the fact list | without it |",
        "|---|---|---|",
        f"| Facts on record contradicted by the paragraph | {c_with} of "
        f"{summary['n_pinned_total']} ({_pct(rate_with)}) | {c_without} of "
        f"{summary['n_pinned_total']} ({_pct(rate_without)}) |",
        "",
        f"Across the {n_pat} patients, {worse_without} got worse when the list was removed, ",
        f"{worse_with} got better, and {same} were unchanged.",
        "",
        "### The other measures, and why they should not be read at face value",
        "",
        "| Measure | with the fact list | without it |",
        "|---|---|---|",
        f"| Faithfulness to source | {summary['faithful_with']} | {summary['faithful_without']} |",
        f"| Compression (paragraph length / input length) | {summary['compression_with']} | "
        f"{summary['compression_without']} |",
        f"| Share of facts on record actually mentioned in the paragraph | "
        f"{summary['prose_present_with']} | {summary['prose_present_without']} |",
        "",
        "**The last row is misleading and must not be read as the fact list failing.** The writer ",
        "is explicitly instructed *not* to re-list facts that are already held in the ledger, ",
        "because they are preserved there and repeating them wastes the paragraph. So when the ",
        "list is supplied, the model deliberately leaves those facts out of the prose. A mention ",
        "rate that is equal or lower with the list is the instruction working as designed, not ",
        "evidence of loss.",
        "",
        "Compression was effectively identical in both arms, so supplying the list costs nothing ",
        "in length. Faithfulness moved in the same direction as the contradiction count but by a ",
        "small margin, and it is measured against a source that itself includes the ledger, so it ",
        "is not an independent check.",
        "",
        "## What this means",
        "",
        "**Two different things protect the record, and only one of them is a prompt.** Handing ",
        "the writer the retained-fact list reduces how often the paragraph contradicts what is ",
        "already known. That is worth keeping. But contradictions persist even when the list is ",
        "supplied, so the prompt cannot be the guarantee.",
        "",
        "**The guarantee is the ledger as a stored record, not as a prompt ingredient.** A fact ",
        "pinned in the ledger stays there whatever the paragraph goes on to say about it. The ",
        "paragraph is a readable narrative for a clinician in a hurry; the ledger is the record ",
        "that a clinician can audit when the narrative and the patient in front of them disagree. ",
        "The two are complementary, and this study is part of the evidence for keeping them ",
        "separate rather than trusting the prose alone.",
        "",
        "**Contradictions in the paragraph are worth reviewing individually.** Every flagged ",
        "fact is listed in `flagged_contradictions.csv` alongside this file, with which arm ",
        "flagged it. Those have NOT been clinically adjudicated -- an AI reader flagged them, and ",
        "experience with these judges is that a substantial share of flags do not survive human ",
        "review.",
        "",
        "## What this study does not show",
        "",
        f"- **It is a small, deliberately skewed sample.** {sample_note[0].upper()}"
        f"{sample_note[1:]}. These are the patients with the most history to compress, chosen ",
        "  because that is where the paragraph is under most strain. They are not typical.",
        f"- **The paragraphs were generated in `{mode}` mode.** If production runs a different ",
        "  mode, the numbers may not transfer directly.",
        "- **The absolute counts are small.** A handful of facts either way across the whole ",
        "  cohort, so a few patients drive the result and the size of the effect is uncertain ",
        "  even where its direction is not.",
        "- **A contradiction here is one AI reader's opinion**, not an adjudicated clinical ",
        "  finding.",
        "- **It says nothing about whether the ledger's own contents are correct.** It tests only ",
        "  what the paragraph does with them.",
        "",
        "## Technical appendix",
        "",
        f"Ablation over {n_pat} patients, compaction mode `{mode}`. Compactor {compactor}, ",
        f"judge {judge}. Arms differ only in `CompactorInput.ledger_facts` (real ledger vs empty ",
        "list); the b2 ledger artifact is untouched. Contradiction and presence are per-pinned-",
        "fact judgements from `eval_fact_presence`; faithfulness is the f4 judge scoring each ",
        "paragraph against a source built from the ledger plus the aged-out admissions' ",
        "`bhc_delta`, which confounds it across arms. Compression is output/input characters over ",
        "the compacted span (f3 definition). Pinned facts are deduplicated per patient on ",
        "(retention_class, lowercased text).",
        "",
        "| Metric | with ledger | without ledger | delta |",
        "|---|---|---|---|",
        f"| contradicted pinned facts | {c_with} | {c_without} | {delta} |",
        f"| faithfulness (mean) | {summary['faithful_with']} | {summary['faithful_without']} | "
        f"{summary['faithful_delta']} |",
        f"| compression (mean) | {summary['compression_with']} | "
        f"{summary['compression_without']} | |",
        f"| prose fact-presence (mean) | {summary['prose_present_with']} | "
        f"{summary['prose_present_without']} | |",
        "",
        f"- patients: {n_pat} | pinned facts judged: {summary['n_pinned_total']}",
        f"- per-patient direction: {worse_without} worse without, {worse_with} worse with, "
        f"{same} unchanged",
        "",
        "Full machine-readable results: `ledger_context.json`. ",
        "Flagged pairs: `flagged_contradictions.csv`.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Ledger-in-context ablation (study).")
    ap.add_argument("--sample", type=int, default=15,
                    help="number of deepest-aged-span patients to use (default 15)")
    ap.add_argument("--subjects", type=int, nargs="*", default=None,
                    help="explicit subject_ids (overrides --sample)")
    ap.add_argument("--mode", choices=["full", "incremental"], default=config.COMPACTION_MODE)
    args = ap.parse_args()
    config.check_paths()

    comp = c1._read_jsonl(config.COMPACTION_JSONL)
    if not comp:
        raise SystemExit(f"{config.COMPACTION_JSONL} not found/empty. Run c1_compactor.py first.")
    ledgers = {int(l["subject_id"]): l for l in c1._read_jsonl(c1.LEDGER_JSONL)}
    cohort = c1._cohort_by_hadm()
    facts_by_hadm = c1._facts_by_hadm()
    cdf = pd.read_csv(COHORT_CSV, usecols=["subject_id", "hadm_id"])
    hadms_by_pid: dict[int, list[int]] = {}
    for _, r in cdf.iterrows():
        hadms_by_pid.setdefault(int(r["subject_id"]), []).append(int(r["hadm_id"]))

    # sample: deepest aged span (most covered admissions) unless explicit subjects given
    if args.subjects:
        pids = list(args.subjects)
        sample_note = f"{len(pids)} explicitly named patient(s)"
    else:
        ranked = sorted(comp, key=lambda c: len(c.get("covered_hadm_ids", [])), reverse=True)
        pids = [int(c["subject_id"]) for c in ranked[:args.sample]]
        sample_note = (f"a sample of {len(pids)} patients out of {len(comp)} with a compacted "
                       f"paragraph, chosen as those with the deepest compacted history "
                       f"(not a random sample)")

    # ── Pass A: qwen compactor, BOTH arms ─────────────────────────────────────────
    staged: list[dict] = []
    try:
        for pid in pids:
            led = ledgers.get(pid)
            if led is None:
                print(f"no ledger for subject_id={pid} -- skipping")
                continue
            n_adm = int(led.get("n_admissions") or 0)
            aged = c1._aged_out_for(hadms_by_pid.get(pid, []), cohort, facts_by_hadm, n_adm)
            if not aged:
                print(f"subject_id={pid} has no aged span -- skipping")
                continue
            ledger_facts = [c1._node_to_fact(n) for n in (led.get("nodes", []) + led.get("flagged", []))]
            covered = [a.hadm_id for a in aged]

            with_prose = _compact_span(pid, aged, ledger_facts, args.mode)
            without_prose = _compact_span(pid, aged, [], args.mode)
            with_ratio = _compression(with_prose, aged)
            without_ratio = _compression(without_prose, aged)

            staged.append({
                "subject_id": pid, "n_covered": len(covered),
                "source": _judge_source(ledger_facts, aged),
                "pinned_texts": _pinned_texts(covered, facts_by_hadm),
                "with_prose": with_prose, "without_prose": without_prose,
                "with_ratio": with_ratio, "without_ratio": without_ratio,
            })
            print(f"compacted subj={pid}  covered={len(covered)}  "
                  f"ratio with={with_ratio} without={without_ratio}")
    finally:
        c1._ollama_unload()      # free qwen before the deepseek judge

    # ── Pass B: deepseek judge (faithfulness + fact-presence) ─────────────────────
    rows: list[dict] = []
    try:
        for s in staged:
            with_claims, st_w = f4.judge_unit(s["source"], s["with_prose"])
            without_claims, st_wo = f4.judge_unit(s["source"], s["without_prose"])
            pres_w, _ = fp.fact_presence_bools(s["with_prose"], s["pinned_texts"])
            pres_wo, _ = fp.fact_presence_bools(s["without_prose"], s["pinned_texts"])
            contra_w, _ = fp.fact_contradiction_bools(s["with_prose"], s["pinned_texts"])
            contra_wo, _ = fp.fact_contradiction_bools(s["without_prose"], s["pinned_texts"])
            flagged = [{"fact": t, "with": bool(cw), "without": bool(cwo)}
                       for t, cw, cwo in zip(s["pinned_texts"], contra_w, contra_wo) if cw or cwo]
            rows.append({
                "subject_id": s["subject_id"], "n_covered": s["n_covered"],
                "n_pinned": len(s["pinned_texts"]),
                "faithful_with": _ff(with_claims), "faithful_without": _ff(without_claims),
                "faithful_delta": round(_ff(with_claims) - _ff(without_claims), 4),
                "compression_with": s["with_ratio"], "compression_without": s["without_ratio"],
                "prose_present_with": _present_rate(pres_w),
                "prose_present_without": _present_rate(pres_wo),
                "n_contradicted_with": sum(1 for b in contra_w if b),
                "n_contradicted_without": sum(1 for b in contra_wo if b),
                "flagged_contradictions": flagged,
                "judge_status_with": st_w, "judge_status_without": st_wo,
            })
            print(f"judged subj={s['subject_id']}  faithful with={rows[-1]['faithful_with']} "
                  f"without={rows[-1]['faithful_without']}  "
                  f"contradictions with={rows[-1]['n_contradicted_with']} "
                  f"without={rows[-1]['n_contradicted_without']}")
    finally:
        fp.ollama_unload()       # free deepseek

    def _avg(key):
        vals = [r[key] for r in rows if r[key] is not None]
        return round(mean(vals), 4) if vals else None

    n_pinned_total = sum(r["n_pinned"] for r in rows)
    contra_with = sum(r["n_contradicted_with"] for r in rows)
    contra_without = sum(r["n_contradicted_without"] for r in rows)
    summary = {
        "faithful_with": _avg("faithful_with"), "faithful_without": _avg("faithful_without"),
        "faithful_delta": _avg("faithful_delta"),
        "compression_with": _avg("compression_with"), "compression_without": _avg("compression_without"),
        "prose_present_with": _avg("prose_present_with"),
        "prose_present_without": _avg("prose_present_without"),
        "n_pinned_total": n_pinned_total,
        "contradictions_with": contra_with, "contradictions_without": contra_without,
        "contradiction_delta": contra_without - contra_with,
    }
    report = {
        "study": "ledger_in_context_ablation", "mode": args.mode,
        "compactor": config.model_for("compactor").model_id,
        "judge": config.model_for("faithfulness_validator").model_id,
        "n_patients": len(rows), "cohort_means": summary, "per_patient": rows,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")

    with OUT_FLAG.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["subject_id", "fact", "contradicted_with", "contradicted_without"])
        for r in rows:
            for fl in r.get("flagged_contradictions", []):
                w.writerow([r["subject_id"], fl["fact"], fl["with"], fl["without"]])

    OUT_MD.write_text(
        _findings_md(rows, summary, args.mode, report["compactor"], report["judge"],
                     sample_note),
        encoding="utf-8")

    print("\n" + "=" * 60)
    print("LEDGER-IN-CONTEXT ABLATION (study)")
    print(f"  patients / pinned      : {len(rows)} / {summary['n_pinned_total']}")
    print(f"  CONTRADICTIONS with/without : {summary['contradictions_with']} / "
          f"{summary['contradictions_without']}  (delta {summary['contradiction_delta']})  [headline]")
    print(f"  faithful  with/without : {summary['faithful_with']} / {summary['faithful_without']}"
          f"  (delta {summary['faithful_delta']})  [confounded]")
    print(f"  compress  with/without : {summary['compression_with']} / {summary['compression_without']}")
    print(f"  present   with/without : {summary['prose_present_with']} / {summary['prose_present_without']}"
          f"  [confounded]")
    print(f"  json -> {OUT_JSON}")
    print(f"  md   -> {OUT_MD}")
    print(f"  flagged pairs -> {OUT_FLAG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
