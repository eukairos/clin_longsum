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
f5_pdsqi.py
===========
Phase F Tier 1 - PDSQI-10 LLM-as-judge (summary quality).

Scores the assembled LongitudinalView on ten attributes (PDSQI-9 + a longitudinal
temporal_coherence; synthesized redefined for cross-admission integration), Likert
1-5 (5 = best for every attribute, including stigmatizing where 5 = no stigmatizing
language). The judge sees the rendered summary the physician sees, plus the
retention ledger as the evidence base for the source-dependent attributes.

Same judge as f4 (config.model_for("faithfulness_validator"), SWAPPABLE), and reuses
f4's chat + graceful recovery (empty -> think=False retry, malformed -> reparse,
else judge_error and the patient is excluded + logged, never crashes).

This is Tier 1 (nightly / tolerance-banded), not a hard gate. accurate and
temporal_coherence overlap the deterministic gates and f4 -- divergence between the
judge here and those is itself a judge-reliability signal (see EVALUATION.md s7).

OUTPUTS:
  data/output/eval/pdsqi10.jsonl   per patient: scores + rationales
  data/output/eval/pdsqi10.json    per-attribute means/min/distribution + gate
EXIT CODE: 0 if overall mean >= --threshold, else 1.

    cd ~/clin_longsum/src/evaluation
    python f5_pdsqi.py --limit 5
    python f5_pdsqi.py --subject 90000029
    python f5_pdsqi.py --fresh --threshold 4.0
    python f5_pdsqi.py --json-only      # suppress the text report
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from statistics import mean

import _bootstrap  # noqa: F401  (puts src/ on sys.path)
import config
import c1_compactor as c1
import f4_faithfulness as f4          # reuse judge: SPEC, _chat, _ollama_unload, _ledger_text, _cap

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("f5_pdsqi")

PROMPT_VERSION = "pdsqi10.v1"
SCORE_FLOOR = 3.5                      # attributes whose mean falls below this are flagged (advisory)

OUT_DIR = config.OUTPUT_DIR / "eval"
OUT_JSONL = OUT_DIR / "pdsqi10.jsonl"
OUT_JSON = OUT_DIR / "pdsqi10.json"
VIEW_JSONL = config.OUTPUT_DIR / "longitudinal_view.jsonl"

ATTRS = [
    ("cited", "claims are traceable to the source notes / ledger"),
    ("accurate", "no factual errors or hallucinations"),
    ("thorough", "all clinically significant events are captured"),
    ("useful", "actionable for a physician seeing this patient now"),
    ("organized", "logical structure and flow"),
    ("comprehensible", "clear language, appropriate abbreviations"),
    ("succinct", "appropriately concise, no unnecessary repetition"),
    ("synthesized", "integrates information coherently ACROSS admissions"),
    ("stigmatizing", "free of stigmatizing language (5 = none, 1 = highly stigmatizing)"),
    ("temporal_coherence", "events presented in clinically logical chronological order"),
]
KEYS = [k for k, _ in ATTRS]

_SYS = (
    "You are a senior physician evaluating the quality of a longitudinal patient "
    "summary using a structured rubric. Score strictly and consistently. Use the "
    "REFERENCE only as the evidence base for accuracy/citation/thoroughness."
)


def _rubric() -> str:
    lines = [f"  {k} -- {d}" for k, d in ATTRS]
    return "\n".join(lines)


def _render_summary(view: dict) -> str:
    parts = []
    if (view.get("tldr_md") or "").strip():
        parts.append("## TL;DR\n" + view["tldr_md"])
    if (view.get("current_episode_md") or "").strip():
        parts.append("## Current episode\n" + view["current_episode_md"])
    hot = [ep.get("paragraph_md", "") for ep in (view.get("hot_window") or [])
           if (ep.get("paragraph_md") or "").strip()]
    if hot:
        parts.append("## Recent admissions\n" + "\n\n".join(hot))
    if (view.get("prior_admissions_md") or "").strip():
        parts.append("## Prior admissions (compacted)\n" + view["prior_admissions_md"])
    return "\n\n".join(parts)


_TMPL = (
    "REFERENCE (the retained evidence base - retention ledger):\n<<<\n{reference}\n>>>\n\n"
    "SUMMARY to evaluate:\n<<<\n{summary}\n>>>\n\n"
    "Score each attribute from 1 (worst) to 5 (best). 5 is best for every attribute, "
    "including 'stigmatizing' (5 = no stigmatizing language):\n{rubric}\n\n"
    "Return ONLY a JSON object, no prose, no markdown:\n"
    "{{\"cited\": {{\"score\": 1-5, \"rationale\": \"<brief>\"}}, ... one entry per attribute ...}}"
)


def _parse_scores(text: str) -> tuple[dict, dict]:
    t = text.strip()
    if "```" in t:
        t = max(t.split("```"), key=len)
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    s, e = t.find("{"), t.rfind("}")
    if s != -1 and e != -1 and e > s:
        t = t[s:e + 1]
    obj = json.loads(t)
    scores, rationales = {}, {}
    for k in KEYS:
        v = obj.get(k)
        sc, rat = None, ""
        if isinstance(v, dict):
            sc, rat = v.get("score"), str(v.get("rationale", ""))[:300]
        elif isinstance(v, (int, float, str)):
            sc = v
        try:
            sc = int(round(float(sc)))
        except Exception:
            sc = None
        if sc is not None and not (1 <= sc <= 5):
            sc = None
        scores[k] = sc
        rationales[k] = rat
    return scores, rationales


def judge_pdsqi(summary: str, reference: str) -> tuple[dict, dict, str]:
    """Return (scores, rationales, status). Graceful recovery mirrors f4."""
    msgs = [{"role": "system", "content": _SYS},
            {"role": "user", "content": _TMPL.format(
                reference=reference, summary=summary, rubric=_rubric())}]
    status = "ok"
    try:
        out = f4._chat(msgs, f4.SPEC.think)
    except Exception as exc:
        logger.warning("judge call failed: %s", exc)
        out = ""
    if not out:
        try:
            out = f4._chat(msgs, False)
            status = "retried_nothink"
        except Exception as exc:
            logger.warning("no-think retry failed: %s", exc)
            return {k: None for k in KEYS}, {}, "judge_error"

    try:
        scores, rationales = _parse_scores(out)
    except Exception:
        try:
            out2 = f4._chat(msgs + [{"role": "user",
                                     "content": "Output ONLY the JSON object. No reasoning, no prose."}],
                            False)
            scores, rationales = _parse_scores(out2)
            status = "reparsed" if status == "ok" else status
        except Exception:
            return {k: None for k in KEYS}, {}, "judge_error"

    if all(scores[k] is None for k in KEYS):
        return scores, rationales, "judge_error"
    return scores, rationales, status


def main() -> int:
    ap = argparse.ArgumentParser(description="PDSQI-10 LLM-as-judge (summary quality).")
    ap.add_argument("--subject", type=int, default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--threshold", type=float, default=4.0,
                    help="gate: min overall mean score (provisional; tune from baseline)")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()
    config.check_paths()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    views = c1._read_jsonl(VIEW_JSONL)
    if not views:
        raise SystemExit(f"{VIEW_JSONL} not found/empty. Run the coordinator first.")

    pids = [args.subject] if args.subject is not None else [int(v["subject_id"]) for v in views]
    if args.limit:
        pids = pids[:args.limit]
    pid_set = set(pids)
    views = [v for v in views if int(v["subject_id"]) in pid_set]

    existing = {} if args.fresh else {
        int(r["subject_id"]): r for r in c1._read_jsonl(OUT_JSONL)}
    done = {sid for sid, r in existing.items() if r.get("status") != "judge_error"}

    new_records: list[dict] = []
    n_judged = 0
    try:
        for v in views:
            pid = int(v["subject_id"])
            if pid in done:
                continue
            summary, _ = f4._cap(_render_summary(v))
            reference, _ = f4._cap(f4._ledger_text(v))
            scores, rationales, status = judge_pdsqi(summary, reference)
            new_records.append({
                "subject_id": pid, "status": status,
                "scores": scores, "rationales": rationales,
                "model": f4.SPEC.model_id, "prompt_version": PROMPT_VERSION,
            })
            n_judged += 1
            shown = {k: scores[k] for k in KEYS if scores[k] is not None}
            logger.info("subj=%s status=%s scores=%s", pid, status, shown)
    finally:
        f4._ollama_unload()

    merged = dict(existing)
    for r in new_records:
        merged[r["subject_id"]] = r
    with OUT_JSONL.open("w", encoding="utf-8") as fh:
        for sid in sorted(merged):
            fh.write(json.dumps(merged[sid]) + "\n")

    rows = [r for sid, r in merged.items() if sid in pid_set]
    n_err = sum(1 for r in rows if r["status"] == "judge_error")
    n_recov = sum(1 for r in rows if r["status"] in ("retried_nothink", "reparsed"))

    by_attr: dict[str, dict] = {}
    for k in KEYS:
        vals = [r["scores"].get(k) for r in rows if r["status"] != "judge_error"]
        vals = [x for x in vals if isinstance(x, int)]
        dist = {str(s): sum(1 for x in vals if x == s) for s in range(1, 6)}
        by_attr[k] = {"n": len(vals),
                      "mean": round(mean(vals), 3) if vals else None,
                      "min": min(vals) if vals else None,
                      "dist": dist}

    all_scores = [x for r in rows if r["status"] != "judge_error"
                  for x in r["scores"].values() if isinstance(x, int)]
    overall = round(mean(all_scores), 3) if all_scores else None
    gate = overall is not None and overall >= args.threshold
    low_attrs = [k for k in KEYS if by_attr[k]["mean"] is not None and by_attr[k]["mean"] < SCORE_FLOOR]

    report = {
        "metric": "pdsqi10",
        "model": f4.SPEC.model_id, "prompt_version": PROMPT_VERSION,
        "threshold": args.threshold, "score_floor_advisory": SCORE_FLOOR,
        "patients": len(pid_set), "scored": len(rows) - n_err,
        "overall_mean": overall,
        "by_attribute": by_attr,
        "low_attributes_advisory": low_attrs,
        "n_judge_errors": n_err, "n_recovered": n_recov,
        "gates": {"gate_pdsqi": gate}, "all_gates_pass": gate,
    }
    with OUT_JSON.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    if not args.json_only:
        print("\n" + "=" * 60)
        print("F5 PDSQI-10 (Tier 1, LLM judge)")
        print(f"  model            : {f4.SPEC.model_id}")
        print(f"  patients scored  : {len(rows) - n_err} / {len(pid_set)}  (newly judged {n_judged})")
        print(f"  overall mean     : {overall}")
        for k in KEYS:
            a = by_attr[k]
            mark = "  <- low" if k in low_attrs else ""
            print(f"    {k:18s}: {a['mean']}  (min {a['min']}, n {a['n']}){mark}")
        print(f"  judge errors     : {n_err}   recovered: {n_recov}")
        if n_err:
            print(f"    ({n_err} patients excluded -- if many, swap the judge model)")
        print(f"  gate (>= {args.threshold}): {'PASS' if gate else 'FAIL'}")
        print(f"\n  jsonl -> {OUT_JSONL}")
        print(f"  json  -> {OUT_JSON}")

    return 0 if gate else 1


if __name__ == "__main__":
    sys.exit(main())
