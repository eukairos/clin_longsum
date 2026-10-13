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
f4_faithfulness.py
==================
Phase F intrinsic metric #1 - ATOMIC-CLAIM FAITHFULNESS (Tier 1, LLM judge).

COMPOSITIONAL design: each generated unit is verified against EXACTLY what its
generator consumed - not raw notes - so the check both scales (bounded sources)
and localizes faults (a tldr error is a d2 fault, a paragraph error a d1 fault).

  faithful_fraction = supported / (supported + not_supported)   [not_addressable excluded]

SOURCE per unit = its generator's actual input:
  tldr             -> admission count + prior_admissions paragraph + ALL established
                      per-admission paragraphs (current + hot) - exactly d2's input;
                      NOT the ledger, NOT raw notes, NOT the display hot_window
  prior_admissions -> the retention ledger + the covered admissions' bhc_delta
                      (exactly c1's input)
  current_episode  -> the current admission's bhc_text   (d1's input)   (--units all)
  hot_<seq>        -> that admission's bhc_text           (d1's input)   (--units all)

Why bhc_text for d1 units (not bhc_delta): d1 generated from bhc_text, so verifying
against the deduped delta would falsely flag carried-forward content d1 included.
One admission is bounded, so there is no scaling reason to use delta there.

Semantics by mode:
  --units synth (default)  tldr + prior_admissions. prior is ground-truth-ish (vs the
      aged delta c1 read); tldr is checked vs the paragraphs, so its faithfulness is
      CONDITIONAL on those paragraphs being faithful (internal consistency).
  --units all              adds the paragraph-vs-bhc_text checks, closing the loop to
      compositional ground truth (every tier verified against its input).
Only prior_admissions has an unbounded source (aged bhc_delta) - far smaller than raw
notes, but at production lifetime-scale it is where per-fold / RAG verification goes
(n_truncated flags when it hits the cap).

JUDGE MODEL is config.model_for("faithfulness_validator") - SWAPPABLE, no code change.
Thinking model (deepseek-r1) graceful recovery: empty (think budget) -> think=False
retry; malformed JSON -> reparse retry; still failing -> judge_error, EXCLUDED from the
metric and logged, never crashes. Each record carries source_admit_seqs / source_hadm_ids
for traceability. Resumable: skips (subject, unit) already judged ok unless --fresh.

Tier 1 (nightly / tolerance-banded), not a hard gate. Default threshold is mode-aware
(synth 0.6 / all 0.9) - PROVISIONAL, re-baseline from a clean pilot.

OUTPUTS:
  data/output/eval/faithfulness.jsonl   one record per (subject, unit) with claims+labels
  data/output/eval/faithfulness.json    aggregate (overall + by unit type) + gate
EXIT CODE: 0 if overall faithful_fraction >= threshold, else 1.

    cd ~/clin_longsum/src/evaluation
    python f4_faithfulness.py --fresh --limit 5 --units all   # pilot first
    python f4_faithfulness.py --subject 90000029
    python f4_faithfulness.py --skip 90000001 --view-path P --comp-path P --out-dir D --json-only
    python f4_faithfulness.py --fresh --threshold 0.7
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path

import ollama
import pandas as pd

import _bootstrap  # noqa: F401  (puts src/ on sys.path)
import config
import c1_compactor as c1
import holdout

sys.path.insert(0, str(config.SYNTH_DIR))
import ollama_utils as ou               # noqa: E402  (shared VRAM / unload)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("f4_faithfulness")

SPEC = config.model_for("faithfulness_validator")
PROMPT_VERSION = config.prompt_version("faithfulness_validator")
TEMP = SPEC.params.get("temperature", 0.0)
NUM_CTX = SPEC.params.get("num_ctx", 65536)
NUM_PREDICT = SPEC.params.get("num_predict", 8192)

SRC_CHAR_CAP = 120_000          # ~ under 65K tokens; only prior_admissions can approach this
LABELS = ("supported", "not_supported", "not_addressable")

OUT_DIR = config.OUTPUT_DIR / "eval"
OUT_JSONL = OUT_DIR / "faithfulness.jsonl"
OUT_JSON = OUT_DIR / "faithfulness.json"
VIEW_JSONL = config.OUTPUT_DIR / "longitudinal_view.jsonl"
COHORT_CSV = config.INTERIM_DIR / "cohort_admissions.csv"


# Shared VRAM / unload helpers (vendor/ollama_utils). Names kept: f6 calls
# f4._ollama_unload, and f9/f10/f11 reuse f4's judge machinery.
_gpu_cleanup = ou.gpu_cleanup


def _ollama_unload() -> None:
    ou.ollama_unload(SPEC.model_id)


# ── prompt (faithfulness_validator.v3 -- compositional source, synthesis-aware) ──

_SYS = (
    "You are a meticulous clinical fact-checker. You decide whether each claim in a "
    "SUMMARY is grounded in the SOURCE - the material the summary was generated from. "
    "Use ONLY the SOURCE, never outside medical knowledge. Be conservative about "
    "calling something unsupported: faithful summary-level synthesis, aggregation, and "
    "counts that follow from the SOURCE are supported."
)

_TMPL = (
    "SOURCE - the material the SUMMARY was generated from (prior-tier summaries and/or "
    "admission content); admission blocks are delimited [admission seq=N hadm=H] where "
    "present:\n<<<\n{source}\n>>>\n\n"
    "SUMMARY to verify:\n<<<\n{summary}\n>>>\n\n"
    "Decompose the SUMMARY into atomic factual claims (one verifiable assertion each) "
    "and label every claim:\n"
    "  \"supported\"        - the SOURCE states it, OR it is a faithful summary, "
    "aggregation, or count that follows from the SOURCE (e.g. 'eight admissions' when "
    "the SOURCE shows eight admissions; summarizing a course across admissions).\n"
    "  \"not_supported\"   - a SPECIFIC factual assertion the SOURCE contradicts, or "
    "that has no basis anywhere in the SOURCE (a hallucination). Do NOT label a claim "
    "not_supported merely because it paraphrases, generalizes, or synthesizes.\n"
    "  \"not_addressable\" - vague framing with no specific checkable content.\n\n"
    "Return ONLY a JSON array, no prose, no markdown:\n"
    "[{{\"claim\": \"<short claim>\", \"label\": \"supported|not_supported|not_addressable\"}}]"
)


def _chat(messages: list[dict], think) -> str:
    resp = ollama.chat(
        model=SPEC.model_id, messages=messages, think=think,
        options={"temperature": TEMP, "num_ctx": NUM_CTX, "num_predict": NUM_PREDICT},
        keep_alive=-1,
    )
    return (resp.message.content or "").strip()


def _parse_claims(text: str):
    t = text.strip()
    if "```" in t:
        t = max(t.split("```"), key=len)
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    s, e = t.find("["), t.rfind("]")
    if s != -1 and e != -1 and e > s:
        t = t[s:e + 1]
    return json.loads(t)


def judge_unit(source: str, summary: str) -> tuple[list[dict], str]:
    """Return (claims, status). think -> no-think retry on empty, reparse on
    malformed, else judge_error (empty claims)."""
    msgs = [{"role": "system", "content": _SYS},
            {"role": "user", "content": _TMPL.format(source=source, summary=summary)}]
    status = "ok"
    try:
        out = _chat(msgs, SPEC.think)
    except Exception as exc:
        logger.warning("judge call failed: %s", exc)
        out = ""
    if not out:
        try:
            out = _chat(msgs, False)
            status = "retried_nothink"
        except Exception as exc:
            logger.warning("no-think retry failed: %s", exc)
            return [], "judge_error"
    try:
        claims = _parse_claims(out)
    except Exception:
        try:
            out2 = _chat(msgs + [{"role": "user",
                                  "content": "Output ONLY the JSON array. No reasoning, no prose."}],
                         False)
            claims = _parse_claims(out2)
            status = "reparsed" if status == "ok" else status
        except Exception:
            return [], "judge_error"
    if not isinstance(claims, list):
        return [], "judge_error"
    clean = []
    for c in claims:
        if not isinstance(c, dict):
            continue
        lab = str(c.get("label", "")).strip().lower()
        if lab not in LABELS:
            lab = "not_addressable"
        clean.append({"claim": str(c.get("claim", "")).strip(), "label": lab})
    return clean, status


# ── sources (each unit verified against its generator's input) ──────────────────

def _ledger_text(view: dict) -> str:
    """Retained-set rendering. Used as the do-not-contradict context for the
    prior_admissions source (it is part of c1's input), and imported by f5.
    render by negative_kind - a resolved_relevant event AFFIRMS it happened
    (must NOT read as 'NEGATIVE', which falsely told the compactor it was absent)."""
    _TAG = {"absent_or_negative": "ABSENT/EXCLUDED: ",
            "surveillance": "UNDER SURVEILLANCE: ",
            "resolved_relevant": "RESOLVED (occurred, lifetime-relevant): "}
    lines = []
    for f in view.get("ledger_facts") or view.get("retention_ledger") or []:
        tag = _TAG.get(f.get("negative_kind"), "")
        lines.append(f"- [{f.get('salience_tier', '')}/{f.get('retention_class', '')}] "
                     f"{tag}{f.get('text', '')}")
    return "\n".join(lines) or "(none)"


def _cap(s: str) -> tuple[str, bool]:
    return (s[:SRC_CHAR_CAP], True) if len(s) > SRC_CHAR_CAP else (s, False)


def _units_for(view: dict, covered: list[int], paras: list[dict],
               seq_by_hadm: dict[int, int], bt: dict[int, str], bd: dict[int, str],
               mode: str):
    """Yield unit dicts, each source = what that unit's generator actually consumed.
    `paras` = the patient's ESTABLISHED d1 paragraphs (d2's tldr input), sorted by seq."""
    def blk(hadm: int, text: str) -> str:
        return f"[admission seq={seq_by_hadm.get(hadm, '?')} hadm={hadm}]\n{text}"

    out = []

    # tldr  <-  d2 input: count + prior paragraph + ALL established paragraphs (current + hot)
    if (view.get("tldr_md") or "").strip() and paras:
        para_blk = "\n\n".join(
            f"[admission seq={p['admit_seq']} hadm={p['hadm_id']}]\n{p['paragraph_md']}"
            for p in paras)
        prior_md = (view.get("prior_admissions_md") or "").strip() or "(none)"
        src = (f"Total established admissions: {view.get('n_total_admissions', len(paras))}\n\n"
               f"PRIOR ADMISSIONS (compacted summary):\n{prior_md}\n\n"
               f"RECENT ADMISSIONS (per-admission summaries, chronological):\n{para_blk}")
        out.append({"unit_id": "tldr", "unit_type": "tldr", "summary": view["tldr_md"],
                    "source": src,
                    "hadms": list(covered) + [p["hadm_id"] for p in paras],
                    "seqs": [p["admit_seq"] for p in paras]})

    # prior_admissions  <-  c1 input: ledger + covered admissions' bhc_delta
    if (view.get("prior_admissions_md") or "").strip() and covered:
        aged = "\n\n".join(blk(h, bd.get(h) or bt.get(h) or "") for h in covered)
        src = (f"RETENTION LEDGER (already retained, do not contradict):\n{_ledger_text(view)}\n\n"
               f"AGED-OUT ADMISSIONS (delta content compacted):\n{aged}")
        out.append({"unit_id": "prior_admissions", "unit_type": "prior_admissions",
                    "summary": view["prior_admissions_md"], "source": src,
                    "hadms": list(covered), "seqs": [seq_by_hadm.get(h) for h in covered]})

    # per-admission paragraphs  <-  d1 input: each admission's bhc_text (current = latest seq)
    if mode == "all" and paras:
        cur_seq = paras[-1]["admit_seq"]
        for p in paras:
            h = int(p["hadm_id"])
            is_cur = p["admit_seq"] == cur_seq
            out.append({"unit_id": "current_episode" if is_cur else f"hot_{p['admit_seq']}",
                        "unit_type": "current_episode" if is_cur else "hot",
                        "summary": p["paragraph_md"], "source": blk(h, bt.get(h, "")),
                        "hadms": [h], "seqs": [p["admit_seq"]]})
    return out


# ── main ────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Atomic-claim faithfulness (compositional, LLM judge).")
    ap.add_argument("--subject", type=int, default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--units", choices=["synth", "all"], default="synth",
                    help="synth = tldr + prior_admissions (default); all adds current + hot")
    ap.add_argument("--threshold", type=float, default=None,
                    help="min overall faithful_fraction (default mode-aware: synth 0.9 / all 0.95)")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--skip", type=int, nargs="*", default=[],
                    help="subject_id(s) to skip entirely")
    ap.add_argument("--view-path", default=None,
                    help="override longitudinal_view.jsonl (f11 points this at a perturbed copy)")
    ap.add_argument("--comp-path", default=None,
                    help="override compaction_results.jsonl (perturbation harness)")
    ap.add_argument("--out-dir", default=None,
                    help="override the eval output dir (sandbox the jsonl/json; default data/output/eval)")
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()
    config.check_paths()

    global OUT_DIR, OUT_JSONL, OUT_JSON, VIEW_JSONL
    if args.out_dir:
        OUT_DIR = Path(args.out_dir)
        OUT_JSONL = OUT_DIR / "faithfulness.jsonl"
        OUT_JSON = OUT_DIR / "faithfulness.json"
    if args.view_path:
        VIEW_JSONL = Path(args.view_path)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    threshold = args.threshold if args.threshold is not None else (0.95 if args.units == "all" else 0.9)
    comp_path = Path(args.comp_path) if args.comp_path else config.COMPACTION_JSONL

    views = c1._read_jsonl(VIEW_JSONL)
    if not views:
        raise SystemExit(f"{VIEW_JSONL} not found/empty. Run the coordinator first.")
    comp = {int(c["subject_id"]): c for c in c1._read_jsonl(comp_path)}

    # cohort: bhc_text + bhc_delta per hadm, and established (seq, hadm) per subject
    cols = {"subject_id", "hadm_id", "admit_seq", "is_holdout", "bhc_text", "bhc_delta"}
    cdf = pd.read_csv(COHORT_CSV, usecols=lambda c: c in cols)
    have_delta = "bhc_delta" in cdf.columns
    bt = {int(r.hadm_id): (str(r.bhc_text) if pd.notna(r.bhc_text) else "")
          for r in cdf.itertuples(index=False)}
    bd = {}
    if have_delta:
        bd = {int(r.hadm_id): (str(r.bhc_delta) if pd.notna(r.bhc_delta) else "")
              for r in cdf.itertuples(index=False)}
    estdf = holdout.established(cdf)
    seq_by_hadm: dict[int, int] = {}
    for sid, g in estdf.groupby("subject_id"):
        for s, h in zip(g["admit_seq"], g["hadm_id"]):
            seq_by_hadm[int(h)] = int(s)

    # d1 paragraphs ARE d2's tldr input (current + all hot). Load them holdout-filtered,
    # exactly as d2 does, so the tldr source matches what d2 consumed - NOT the view's
    # display hot_window, which omits the current episode (the parallax that produced
    # false not_supported for claims tracing to the latest admission).
    hold = set(holdout.holdout_hadms())
    paras_by_pid: dict[int, list[dict]] = {}
    for r in c1._read_jsonl(config.EPISODE_PARAGRAPHS_JSONL):
        if int(r["hadm_id"]) in hold or not (r.get("paragraph_md") or "").strip():
            continue
        paras_by_pid.setdefault(int(r["subject_id"]), []).append(
            {"admit_seq": int(r["admit_seq"]), "hadm_id": int(r["hadm_id"]),
             "paragraph_md": r["paragraph_md"]})
    for plist in paras_by_pid.values():
        plist.sort(key=lambda p: p["admit_seq"])

    pids = [args.subject] if args.subject is not None else [int(v["subject_id"]) for v in views]
    if args.limit:
        pids = pids[:args.limit]
    pid_set = set(pids)
    views = [v for v in views if int(v["subject_id"]) in pid_set]

    existing = {} if args.fresh else {
        (int(r["subject_id"]), r["unit_id"]): r for r in c1._read_jsonl(OUT_JSONL)}
    done = {k for k, r in existing.items() if r.get("status") != "judge_error"}
    skip = set(args.skip or [])

    new_records: list[dict] = []
    n_judged = 0
    # Incremental persistence: append each judged unit (flushed) so an interrupt never loses
    # the batch; resume (above) skips done units. A clean finish rewrites the file deduped +
    # sorted below; on interrupt the appended superset stays valid (read-into-dict dedupes).
    inc_fh = OUT_JSONL.open("w" if args.fresh else "a", encoding="utf-8")
    try:
        for v in views:
            pid = int(v["subject_id"])
            if pid in skip:
                continue
            covered = [int(h) for h in comp.get(pid, {}).get("covered_hadm_ids", [])]
            paras = paras_by_pid.get(pid, [])
            for u in _units_for(v, covered, paras, seq_by_hadm, bt, bd, args.units):
                if (pid, u["unit_id"]) in done:
                    continue
                source, truncated = _cap(u["source"])
                claims, status = judge_unit(source, u["summary"])
                counts = {lab: sum(1 for c in claims if c["label"] == lab) for lab in LABELS}
                rec = {
                    "subject_id": pid, "unit_id": u["unit_id"], "unit_type": u["unit_type"],
                    "source_admit_seqs": u["seqs"], "source_hadm_ids": u["hadms"],
                    "status": status, "source_chars": len(source), "source_truncated": truncated,
                    "n_claims": len(claims), "supported": counts["supported"],
                    "not_supported": counts["not_supported"],
                    "not_addressable": counts["not_addressable"],
                    "model": SPEC.model_id, "prompt_version": PROMPT_VERSION, "claims": claims,
                }
                new_records.append(rec)
                inc_fh.write(json.dumps(rec) + "\n"); inc_fh.flush()
                n_judged += 1
                logger.info("subj=%s unit=%s status=%s claims=%d (sup=%d nsup=%d na=%d)",
                            pid, u["unit_id"], status, len(claims), counts["supported"],
                            counts["not_supported"], counts["not_addressable"])
    finally:
        inc_fh.close()
        _ollama_unload()

    merged = dict(existing)
    for r in new_records:
        merged[(r["subject_id"], r["unit_id"])] = r
    with OUT_JSONL.open("w", encoding="utf-8") as fh:
        for k in sorted(merged):
            fh.write(json.dumps(merged[k]) + "\n")

    rows = [r for k, r in merged.items() if k[0] in pid_set]
    by_type = defaultdict(lambda: {"supported": 0, "not_supported": 0, "not_addressable": 0})
    tot = {"supported": 0, "not_supported": 0, "not_addressable": 0}
    n_err = sum(1 for r in rows if r["status"] == "judge_error")
    n_trunc = sum(1 for r in rows if r.get("source_truncated"))
    n_retry = sum(1 for r in rows if r["status"] in ("retried_nothink", "reparsed"))
    for r in rows:
        for lab in LABELS:
            tot[lab] += r.get(lab, 0)
            by_type[r["unit_type"]][lab] += r.get(lab, 0)

    def _ff(d):
        denom = d["supported"] + d["not_supported"]
        return round(d["supported"] / denom, 4) if denom else 1.0

    faithful = _ff(tot)
    gate = faithful >= threshold
    report = {
        "metric": "atomic_claim_faithfulness_compositional",
        "model": SPEC.model_id, "prompt_version": PROMPT_VERSION,
        "units_mode": args.units, "threshold": threshold,
        "patients": len(pid_set), "units_scored": len(rows),
        "faithful_fraction": faithful, "counts": tot,
        "by_unit_type": {t: {**by_type[t], "faithful_fraction": _ff(by_type[t])}
                         for t in sorted(by_type)},
        "n_judge_errors": n_err, "n_truncated": n_trunc, "n_recovered": n_retry,
        "gates": {"gate_faithful": gate}, "all_gates_pass": gate,
    }
    with OUT_JSON.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    if not args.json_only:
        print("\n" + "=" * 60)
        print("F4 ATOMIC-CLAIM FAITHFULNESS (compositional, Tier 1, LLM judge)")
        print(f"  model              : {SPEC.model_id}")
        print(f"  patients / units   : {len(pid_set)} / {len(rows)}  (newly judged {n_judged})")
        print(f"  faithful_fraction  : {faithful:.4f}   "
              f"(sup={tot['supported']} nsup={tot['not_supported']} na={tot['not_addressable']})")
        for t in sorted(by_type):
            print(f"    {t:16s}: {_ff(by_type[t]):.4f}")
        print(f"  judge errors       : {n_err}   recovered: {n_retry}   truncated: {n_trunc}")
        if n_trunc:
            print(f"    ({n_trunc} prior_admissions source(s) truncated -- deep aged span; RAG candidate)")
        if n_err:
            print(f"    ({n_err} units excluded -- if many, swap the judge model)")
        print(f"  gate (>= {threshold}): {'PASS' if gate else 'FAIL'}")
        print(f"\n  jsonl -> {OUT_JSONL}")
        print(f"  json  -> {OUT_JSON}")

    return 0 if gate else 1


if __name__ == "__main__":
    sys.exit(main())
