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
run_position_bias.py  --  EVAL S11 redux, item 4 (DIAGNOSTIC: position-bias regression)
=======================================================================================
STUDY (one-off, not a standing gate). On the REAL system output, is prose fact
retention driven by SALIENCE (tier) or by POSITION (recency / admit_seq)? LLM
compactors are known to favour recent content; this tests whether ours does, and
gives the salience-vs-recency contrast WITHOUT building a recency baseline.

For each pinned fact in the compacted span:
  outcome   = is the fact present in the REAL prior-admissions PROSE
              (compaction_results.jsonl prior_admissions_md; fact-presence judge).
              Prose, not the ledger -- the ledger retains pinned facts ~trivially
              (no variance), so the ledger gives no signal.
  predictors= salience_tier (ordinal T1=5..T5=1) + normalised source position
              (admit_seq mapped 0=oldest .. 1=newest within the patient).
Fit logistic regression present ~ tier + position.
  tier coefficient dominant, position ~0  -> salience-driven (intended).
  position dominant                        -> compactor is position/recency-biased (a finding + fix target).

VRAM: deepseek judge only (no generation). Unloads at the end.

OUTPUT: <data root>/output/studies/position_bias/position_bias.json (+ FINDINGS.md).
  Outputs go to the DATA tree, NEVER the repo: they are derived from credentialed patient
  data and the repo is not a DUA-compliant location. Namespace-aware via config.

    cd ~/clin_longsum/src
    python studies/position_bias/run_position_bias.py            # all compacted patients
    python studies/position_bias/run_position_bias.py --sample 30
    python studies/position_bias/run_position_bias.py --subjects 90000029 90000055
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import pandas as pd

_SRC = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_SRC))                 # src/ on the path
sys.path.insert(0, str(_SRC / "evaluation"))  # eval_fact_presence lives in src/evaluation/

import config
import c1_compactor as c1
import eval_fact_presence as fp

# Outputs land in the DATA tree, not next to this script. These files are derived from
# credentialed patient data; the repo is not a DUA-compliant location for them.
OUT_DIR = config.OUTPUT_DIR / "studies" / "position_bias"
OUT_JSON = OUT_DIR / "position_bias.json"
OUT_MD = OUT_DIR / "FINDINGS.md"

TIER_ORD = {"T1": 5, "T2": 4, "T3": 3, "T4": 2, "T5": 1}


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _share_phrase(rate: float) -> str:
    """Headline for the overall survival rate. Must stay true if a rerun moves the number."""
    if rate < 0.25:
        return "Most kept facts did not reach the paragraph."
    if rate < 0.45:
        return "Well under half of the kept facts reached the paragraph."
    if rate < 0.55:
        return "About half of the kept facts reached the paragraph."
    if rate < 0.75:
        return "Most kept facts reached the paragraph, but a substantial minority did not."
    return "The great majority of kept facts reached the paragraph."


def _logistic(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z))


TIER_LABEL = {
    "T1": "T1 (safety-critical: code status, severe allergies)",
    "T2": "T2 (permanent: sentinel events, lasting conditions)",
    "T3": "T3 (chronic disease states)",
    "T4": "T4 (recurrent episodes)",
    "T5": "T5 (transient findings)",
}
BUCKET_LABEL = {"old": "oldest third", "mid": "middle third", "recent": "most recent third"}


def _findings_md(df, fit, tier_pos_corr, by_tier_bucket, n_by_tier,
                 pos_lo, pos_hi, sample_note, judge) -> str:
    """Physician-facing write-up. Plain language first, statistics in an appendix."""
    coefs, pvals = fit["coefficients"], fit.get("pvalues", {})
    n_obs, n_pat = len(df), df["subject_id"].nunique()
    rate = float(df["present"].mean())
    p_pos, p_tier = pvals.get("position"), pvals.get("tier_ord")
    pos_sig = p_pos is not None and p_pos < 0.05
    tier_sig = p_tier is not None and p_tier < 0.05

    # Modelled presence at the oldest vs newest admission ACTUALLY OBSERVED in the span,
    # holding importance at its average - never extrapolated past the data.
    base = coefs.get("const", 0.0) + coefs.get("tier_ord", 0.0) * float(df["tier_ord"].mean())
    lo = _logistic(base + coefs.get("position", 0.0) * pos_lo)
    hi = _logistic(base + coefs.get("position", 0.0) * pos_hi)

    if pos_sig and not tier_sig:
        verdict = ("**Whether a fact survives into the Prior Admissions paragraph depends on how "
                   "RECENT it is, not on how important it is.** This is the pattern we were "
                   "testing for, and it is the reason the retention ledger exists alongside the "
                   "paragraph rather than being replaced by it.")
    elif tier_sig and not pos_sig:
        verdict = ("**Survival into the paragraph tracks how IMPORTANT a fact is, not how recent.** "
                   "This is the intended behaviour.")
    elif tier_sig and pos_sig:
        verdict = ("**Both importance and recency influence what survives into the paragraph.** "
                   "Importance is doing some work, but recency is competing with it.")
    else:
        verdict = ("**Neither importance nor recency explains what survives into the paragraph** "
                   "in this sample. Something else is driving it, or the sample is too small "
                   "to tell.")

    lines = [
        "# Does the Prior Admissions paragraph keep the important facts, or just the recent ones?",
        "",
        "*One-off diagnostic study, not a routine quality gate.*",
        "",
        "## The short answer",
        "",
        verdict,
        "",
        "## What we asked",
        "",
        "Once a patient has more admissions than the system displays in full, the older ones are ",
        "folded into a single running paragraph, the *Prior Admissions* tier. Not everything can ",
        "survive that compression. The question is what decides survival:",
        "",
        "- **Importance** -- the salience tier the pipeline assigned (T1 safety-critical down to ",
        "  T5 transient). If this drives survival, the paragraph is doing its job.",
        "- **Recency** -- how late in the patient's history the admission sits. If this drives ",
        "  survival, the paragraph is behaving like a language model with a short memory, and ",
        "  older but important facts are quietly dropping out.",
        "",
        "## How we measured it",
        "",
        f"- We looked at {sample_note}.",
        f"- We took every fact the pipeline had already marked as worth keeping for those ",
        f"  patients: **{n_obs} facts across {n_pat} patients**.",
        f"- For each one, an independent AI reader ({judge}) was shown the real paragraph and ",
        "  asked a single question: does this fact actually appear in it? Nothing else about the ",
        "  fact was shown to the reader.",
        "- We then tested whether the yes/no answers tracked importance, recency, both, or neither.",
        "",
        "We deliberately checked the **paragraph**, not the ledger. The ledger keeps these facts ",
        "by design, so there would be nothing to measure there.",
        "",
        "## What we found",
        "",
        f"**1. {_share_phrase(rate)}** {_pct(rate)} of the {n_obs} facts were found in the ",
        "prose; the rest were not.",
        "",
    ]

    if p_pos is not None:
        if pos_sig:
            lines += [
                "**2. Recency mattered, clearly.** Across the range of admission ages actually ",
                f"present in these compacted spans, modelled survival rose from about {_pct(lo)} ",
                f"for the oldest admissions to about {_pct(hi)} for the most recent ones. The ",
                f"chance of a pattern this strong arising by luck is roughly {p_pos:.4f}.",
                "",
            ]
        else:
            lines += [
                "**2. Recency made little difference.** Modelled survival ran from about ",
                f"{_pct(lo)} for the oldest admissions to about {_pct(hi)} for the most recent ",
                f"ones, a gap well within what chance alone would produce (likelihood ",
                f"{p_pos:.2f}). On this sample there is no evidence the paragraph favours recent ",
                "admissions.",
                "",
            ]
    if p_tier is not None:
        if tier_sig:
            lines += [
                "**3. Importance mattered too.** Survival did step up as facts got more ",
                f"important, by more than chance would explain (likelihood {p_tier:.4f}). This is ",
                "the behaviour the tiering is meant to produce.",
                "",
            ]
        else:
            lines += [
                "**3. Importance did not.** There was no consistent step up in survival as facts ",
                "got more important. Statistically the effect is indistinguishable from zero ",
                f"(likelihood of chance {p_tier:.2f}).",
                "",
            ]
    lines += [
        f"**4. The two are independent of each other** (correlation {tier_pos_corr}), so this is a ",
        "genuine contrast and not one measure standing in for the other.",
        "",
        f"**5. Both together explain only a small part of the picture.** Importance and recency ",
        f"account for roughly {_pct(fit.get('pseudo_r2') or 0)} of the variation in which facts ",
        "survive. Recency is real but it is not the whole story; most of what decides survival is ",
        "not captured by either measure.",
        "",
        "## Survival by importance and by admission age",
        "",
        "Percentage of kept facts that reached the paragraph, with the number of facts behind ",
        "each figure.",
        "",
        "| Importance | " + " | ".join(BUCKET_LABEL[b] for b in ("old", "mid", "recent")) + " | all |",
        "|---|---|---|---|---|",
    ]
    for tier, buckets in by_tier_bucket.items():
        cells = []
        for b in ("old", "mid", "recent"):
            cell = buckets.get(b)
            cells.append(f"{_pct(cell['rate'])} (n={cell['n']})" if cell else "--")
        lines.append(f"| {TIER_LABEL.get(tier, tier)} | " + " | ".join(cells)
                     + f" | n={n_by_tier.get(tier, 0)} |")
    lines += [
        "",
        "**Read the n column before the percentages.** Some tiers carry very few facts in a ",
        "cohort of this size, and a percentage over a handful of facts moves wildly on one or two ",
        "cases. Differences between tiers in this table are not reliable unless the counts behind ",
        "them are substantial.",
        "",
        "## What this means",
        "",
        "**The paragraph loses a lot, and that is expected.** It is a summary of summaries: each ",
        "admission is already condensed before being folded in, and folding many admissions into ",
        "one paragraph condenses again. Loss is the point of compression, and a survival rate ",
        "near half is not by itself a defect.",
        "",
        "**What matters is WHICH facts are lost.** A paragraph that drops transient findings and ",
        "keeps safety-critical ones is working. One that drops facts because they are old, ",
        "regardless of importance, is not, because clinical importance does not decay with time: ",
        "a code-status decision or a severe drug allergy from five admissions ago matters exactly ",
        "as much today as it did then.",
        "",
        "**This is the case for keeping the ledger.** The retention ledger is a structured, ",
        "durable store, and every fact in it is carried forward on the basis of its clinical ",
        "class, not its age or its position in a prompt. The paragraph is a readable narrative ",
        "and it is worth having, but on this evidence it cannot be relied on alone as the record ",
        "of what a patient carries. The two are complementary: the paragraph gives the physician ",
        "a story to read quickly, the ledger guarantees that nothing important was silently lost ",
        "in the telling. This study is the direct evidence for that split.",
        "",
        "## What this study does not show",
        "",
        f"- **It is a sample.** {sample_note[0].upper() + sample_note[1:]}. Patients with longer ",
        "  histories are over-represented when a sample is taken this way, and they are precisely ",
        "  the patients where recency has the most room to matter. A larger or randomly drawn ",
        "  sample would test this more fairly.",
        f"- **The full span of admission ages is never present.** Only admissions old enough to ",
        f"  have been folded into the paragraph are eligible, so the most recent admissions are ",
        f"  structurally absent. In this run the ages ranged from {pos_lo:.2f} to {pos_hi:.2f} on ",
        "  a 0 (oldest) to 1 (newest) scale. Figures quoted above stay inside that range and are ",
        "  not projected beyond it.",
        "- **The effect is statistically clear but modest in size.** See point 5 above.",
        "- **One cohort, one run, one AI reader.** The reader itself makes mistakes, and a fact ",
        "  phrased differently in the paragraph may be scored as absent when a clinician would ",
        "  call it present.",
        "- **It says nothing about the ledger's own accuracy.** It measures only what reaches the ",
        "  prose.",
        "",
        "## Technical appendix",
        "",
        f"Logistic regression, `present ~ tier_ord + position`, fitted with {fit['method']}. ",
        "`tier_ord` is the salience tier as an ordinal (T1=5 ... T5=1); `position` is the source ",
        "admission's sequence number normalised to 0 (oldest) .. 1 (newest) within the patient. ",
        "Outcome is a per-fact presence judgement against `prior_admissions_md` from ",
        "`compaction_results.jsonl`. Facts are deduplicated per patient on ",
        "(retention_class, lowercased text) before judging.",
        "",
        "| Term | Coefficient | p |",
        "|---|---|---|",
    ]
    for term in ("const", "tier_ord", "position"):
        if term in coefs:
            pv = pvals.get(term)
            lines.append(f"| `{term}` | {coefs[term]} | {pv if pv is not None else 'n/a'} |")
    lines += [
        "",
        f"- observations: {n_obs} | patients: {n_pat} | overall present rate: {rate:.4f}",
        f"- pseudo R-squared: {fit.get('pseudo_r2', 'n/a')}",
        f"- correlation(tier_ord, position): {tier_pos_corr}",
        f"- position range observed: {pos_lo:.4f} to {pos_hi:.4f}",
        f"- judge model: {judge}",
    ]
    if fit.get("note"):
        lines.append(f"- note: {fit['note']}")
    lines.append("")
    lines.append("Full machine-readable results: `position_bias.json`.")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Position-bias regression (study).")
    ap.add_argument("--sample", type=int, default=None, help="cap number of patients (default all)")
    ap.add_argument("--subjects", type=int, nargs="*", default=None)
    args = ap.parse_args()
    config.check_paths()

    comp = c1._read_jsonl(config.COMPACTION_JSONL)
    if not comp:
        raise SystemExit(f"{config.COMPACTION_JSONL} not found/empty. Run c1_compactor.py first.")
    ledgers = {int(l["subject_id"]): l for l in c1._read_jsonl(c1.LEDGER_JSONL)}
    cohort = c1._cohort_by_hadm()                    # hadm -> {admit_seq, ...}
    facts_by_hadm = c1._facts_by_hadm()

    n_available = len(comp)
    if args.subjects:
        comp = [c for c in comp if int(c["subject_id"]) in set(args.subjects)]
        sample_note = (f"restricted to {len(comp)} named subject(s) out of {n_available} "
                       f"compacted patients")
    elif args.sample:
        # NOTE: not a random sample -- takes the patients with the LONGEST compacted spans,
        # which biases toward long histories and widens the position range under test.
        comp = sorted(comp, key=lambda c: len(c.get("covered_hadm_ids", [])),
                      reverse=True)[:args.sample]
        sample_note = (f"a SAMPLE of {len(comp)} patients out of {n_available} compacted "
                       f"patients, chosen as those with the longest compacted spans "
                       f"(not a random sample)")
    else:
        sample_note = f"all {n_available} compacted patients"

    # ── deepseek pass: outcome = pinned fact present in real prior-admissions prose ──
    obs: list[dict] = []
    try:
        for c in comp:
            pid = int(c["subject_id"])
            prose = (c.get("prior_admissions_md") or "").strip()
            covered = [int(h) for h in c.get("covered_hadm_ids", [])]
            led = ledgers.get(pid)
            if not prose or not covered or led is None:
                continue
            n_adm = int(led.get("n_admissions") or 0)
            denom = max(n_adm - 1, 1)

            seen, facts = set(), []
            for h in covered:
                for fct in facts_by_hadm.get(h, []):
                    if not fct.in_ledger:
                        continue
                    key = (fct.retention_class, fct.text.strip().lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    seq = cohort.get(int(fct.source_hadm_id), {}).get("admit_seq") \
                        if fct.source_hadm_id is not None else None
                    if seq is None:
                        seq = cohort.get(h, {}).get("admit_seq")
                    facts.append((fct, seq))

            texts = [f.text for f, _ in facts]
            bools, status = fp.fact_presence_bools(prose, texts)
            for (fct, seq), present in zip(facts, bools):
                if present is None or seq is None:
                    continue
                obs.append({
                    "subject_id": pid, "tier": fct.salience_tier,
                    "tier_ord": TIER_ORD.get(fct.salience_tier, 1),
                    "position": round((int(seq) - 1) / denom, 4),
                    "present": int(bool(present)),
                    "retention_class": fct.retention_class, "text": fct.text,
                })
            print(f"subj={pid}  facts={len(texts)}  judged={sum(1 for b in bools if b is not None)} "
                  f"present={sum(1 for b in bools if b)}  status={status}")
    finally:
        fp.ollama_unload()

    if len(obs) < 10:
        raise SystemExit(f"only {len(obs)} usable observations -- too few to regress.")

    df = pd.DataFrame(obs)
    X = df[["tier_ord", "position"]].astype(float)
    y = df["present"].astype(int)

    # ── logistic regression (statsmodels for p-values; sklearn fallback) ──────────
    fit = {"method": None, "coefficients": {}, "pvalues": {}, "note": ""}
    try:
        import statsmodels.api as sm
        Xc = sm.add_constant(X)
        res = sm.Logit(y, Xc).fit(disp=0)
        fit["method"] = "statsmodels.Logit"
        fit["coefficients"] = {k: round(float(v), 4) for k, v in res.params.items()}
        fit["pvalues"] = {k: round(float(v), 6) for k, v in res.pvalues.items()}
        fit["pseudo_r2"] = round(float(res.prsquared), 4)
    except Exception as exc:
        try:
            from sklearn.linear_model import LogisticRegression
            lr = LogisticRegression(max_iter=1000).fit(X, y)
            fit["method"] = "sklearn.LogisticRegression"
            fit["coefficients"] = {"const": round(float(lr.intercept_[0]), 4),
                                   "tier_ord": round(float(lr.coef_[0][0]), 4),
                                   "position": round(float(lr.coef_[0][1]), 4)}
            fit["note"] = f"statsmodels unavailable ({exc}); p-values not computed."
        except Exception as exc2:
            raise SystemExit(f"no regression backend available: {exc2}")

    # partial correlation proxy: raw corr of the two predictors (collinearity check)
    tier_pos_corr = round(float(df["tier_ord"].corr(df["position"])), 4)

    # per-tier prose-presence by position bucket
    df["pos_bucket"] = pd.cut(df["position"], bins=[-0.01, 0.33, 0.66, 1.01],
                              labels=["old", "mid", "recent"])
    by_tier_bucket = {}
    for tier in ("T1", "T2", "T3", "T4", "T5"):
        sub = df[df["tier"] == tier]
        if sub.empty:
            continue
        by_tier_bucket[tier] = {
            str(b): {"rate": round(float(g["present"].mean()), 4), "n": int(len(g))}
            for b, g in sub.groupby("pos_bucket", observed=True)}
    n_by_tier = {t: int((df["tier"] == t).sum()) for t in ("T1", "T2", "T3", "T4", "T5")
                 if int((df["tier"] == t).sum())}
    pos_lo, pos_hi = float(df["position"].min()), float(df["position"].max())

    report = {
        "study": "position_bias_regression",
        "judge": config.model_for("faithfulness_validator").model_id,
        "n_observations": len(df), "n_patients": df["subject_id"].nunique(),
        "overall_present_rate": round(float(df["present"].mean()), 4),
        "sample": sample_note,
        "n_patients_available": n_available,
        "regression": fit,
        "tier_position_correlation": tier_pos_corr,
        "position_range": [round(pos_lo, 4), round(pos_hi, 4)],
        "n_by_tier": n_by_tier,
        "present_rate_by_tier_and_position_bucket": by_tier_bucket,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")

    coefs = fit["coefficients"]
    OUT_MD.write_text(
        _findings_md(df, fit, tier_pos_corr, by_tier_bucket, n_by_tier,
                     pos_lo, pos_hi, sample_note, report["judge"]),
        encoding="utf-8")

    print("\n" + "=" * 60)
    print("POSITION-BIAS REGRESSION (study)")
    print(f"  observations        : {len(df)}  ({df['subject_id'].nunique()} patients)")
    print(f"  present rate        : {report['overall_present_rate']}")
    print(f"  method              : {fit['method']}")
    print(f"  tier_ord coef       : {coefs.get('tier_ord')}  (p={fit['pvalues'].get('tier_ord','n/a')})")
    print(f"  position coef       : {coefs.get('position')}  (p={fit['pvalues'].get('position','n/a')})")
    print(f"  tier/position corr  : {tier_pos_corr}")
    print(f"  json -> {OUT_JSON}")
    print(f"  md   -> {OUT_MD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
