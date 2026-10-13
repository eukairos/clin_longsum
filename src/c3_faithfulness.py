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
c3_faithfulness.py
==================
faithfulness check on the compacted Prior Admissions paragraph.
No hallucination: every claim in the paragraph must be supported by the source
(the aged-out admissions' bhc_delta it was compacted from).

Reuses the vendored faithfulness_validator.validate_faithfulness VERBATIM (4-step
SynthMedic, deepseek-r1:32b, threshold 0.6). Direction mapping:
    seed_ds         = compacted paragraph        (statements extracted from HERE)
    generated_notes = {assembled_input: source}  (must ENTAIL those statements)
    facts           = {}                          (dosage-review flag only; unused)
A failed statement is therefore a paragraph claim the source does not support
- i.e. a hallucination.

TWO-REGION : the judged paragraph is the MACHINE CONTINUATION
(prior_generated_md), scored against ONLY the admissions it covers
(generated_hadm_ids). A physician-edited prefix (pinned_prefix_md) is NEVER judged
here - physician text is authoritative and would false-positive against the notes.
Pre-seeding records lack these fields and fall back to prior_admissions_md /
covered_hadm_ids, i.e. identical behaviour.

NOTE (leniency): the synth Step-4 entailment allows clinical inference (a
diagnosis implies its workup). That is conservative for note-generation but can
MASK a subtly unsupported claim here. It will pass clean paragraphs (verified
c1 output is clean); Phase F's independent judge can apply stricter entailment.

The validator bounds every judge call (num_predict + inactivity + wall-clock) and returns a judge_error RESULT
(score=None) instead of hanging or raising on a deepseek thinking-loop. So a bad
subject self-terminates rather than crashing the batch. Errored subjects are
RE-TRIED automatically on the next plain run (they are not counted as done).

RETRY / RESUME:
  --fresh           overwrite the output and re-judge everything.
  (plain rerun)     resume: skip subjects already judged OK; RE-TRY error/None.
  --subjects A B    re-judge exactly these subject_ids and persist them
                    (replace-in-place), even if previously OK. Use to retry a
                    subject that was --skip'd or that errored.
  --subject S       singular alias for --subjects (one subject; matches the
                    flag every other fold-in stage + foldin's _run() use).
  --print           pretty-print the full result JSON for the targeted subject(s).
  --skip A B        skip these subject_ids this run (e.g. a known slow case).

CONSOLE: the validator logs the REAL covered hadm_id(s) per paragraph (via
trace_label), not the subject_id - more informative for locating the source.

Input:  data/interim/compaction_results.jsonl (c1), data/interim/cohort_admissions.csv
Output: data/interim/compaction_faithfulness.jsonl  (one result per subject, keyed by
        subject_id - there is deliberately NO hadm_id field: the vendored validator's
        trace key held str(subject_id), which made the row look joinable on an admission
        id when it is not. Scope is covered_hadm_ids / generated_hadm_ids. Each row
        carries judge_think_mode / judge_degraded / judge_attempts - a no-think PASS is a
        weaker-judge verdict to review: scratch/c3_nothink_review.py)
        appends one AuditRecord per paragraph to data/output/generation_audit.jsonl

    cd C:~/Summarization/clin_longsum/src
    python c3_faithfulness.py
    python c3_faithfulness.py --subject 90000017                 # targeted retry (singular alias)
    python c3_faithfulness.py --subjects 90000017 90000029       # retry several
    python c3_faithfulness.py --subject 90000017 --print         # retry + inspect
    python c3_faithfulness.py --limit 5 --fresh
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

import cohort
import config
import io_utils
from roles.contracts import AuditRecord

# src/vendor (config.SYNTH_DIR) on path, then import the validator verbatim.
sys.path.insert(0, str(config.SYNTH_DIR))
import faithfulness_validator as fv          # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("c3_faithfulness")

SPEC = config.model_for("faithfulness_validator")
PROMPT_VERSION = config.prompt_version("faithfulness_validator")
THRESHOLD = fv.FAITHFULNESS_THRESHOLD

COMPACTION_JSONL = config.COMPACTION_JSONL
COHORT_CSV = config.INTERIM_DIR / "cohort_admissions.csv"
OUT_JSONL = config.INTERIM_DIR / "compaction_faithfulness.jsonl"
AUDIT_JSONL = config.AUDIT_JSONL


_read_jsonl = io_utils.read_jsonl        # shared tolerant reader


def _delta_by_hadm() -> dict[int, tuple[int, str]]:
    """hadm_id -> (admit_seq, bhc_delta or bhc_text), via the shared cohort loader."""
    return {h: (m["admit_seq"], m["bhc_delta"] or m["bhc_text"] or "")
            for h, m in cohort.by_hadm().items()}


def _source_for(covered: list[int], deltas: dict[int, tuple[int, str]]) -> str:
    """Concatenate the covered admissions' deltas, oldest first -- the evidence base."""
    rows = sorted(((deltas.get(h, (0, ""))[0], deltas.get(h, (0, ""))[1]) for h in covered),
                  key=lambda x: x[0])
    return "\n\n".join(f"Admission {seq}:\n{txt}" for seq, txt in rows if txt)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--subjects", type=int, nargs="*", default=[],
                    help="subject_id(s) to (re-)validate and persist, even if already done")
    ap.add_argument("--subject", type=int, default=None,
                    help="single subject_id alias for --subjects (pipeline/fold-in consistency)")
    ap.add_argument("--print", dest="do_print", action="store_true",
                    help="pretty-print the full result JSON for the targeted subject(s)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--fresh", action="store_true", help="overwrite compaction_faithfulness.jsonl")
    ap.add_argument("--skip", type=int, nargs="*", default=[],
                    help="subject_id(s) to skip (e.g. a known deepseek thinking-loop)")
    args = ap.parse_args()
    config.check_paths()

    comp = _read_jsonl(COMPACTION_JSONL)
    if not comp:
        raise SystemExit(f"{COMPACTION_JSONL} not found/empty. Run c1_compactor.py first.")
    deltas = _delta_by_hadm()

    # Targeted set: --subjects -> forced re-validate + persist.
    targets = set(args.subjects or [])
    if args.subject is not None:
        targets.add(args.subject)
    skip = set(args.skip or [])

    if targets:
        comp = [c for c in comp if int(c["subject_id"]) in targets]
    elif args.limit:
        comp = comp[:args.limit]

    # Existing results (resume + merge-rewrite). done = subjects already judged OK;
    # error/None records are NOT counted as done, so they auto-retry on a plain rerun.
    existing: dict[int, dict] = {}
    if not args.fresh:
        for r in _read_jsonl(OUT_JSONL):
            if r.get("subject_id") is not None:
                existing[int(r["subject_id"])] = r
    done = {sid for sid, r in existing.items() if r.get("faithfulness_score") is not None}
    done -= targets   # forced targets always re-run

    results, n_pass, n_fail = [], 0, 0
    # Incremental persistence: append each result the moment it lands (flushed), so an
    # interrupt - e.g. a deepseek thinking-loop - never loses the batch, and a restart
    # WITHOUT --fresh resumes. A clean finish rewrites the file deduped + sorted (below);
    # on interrupt the appended superset stays valid (read-into-dict dedupes, last wins).
    out_fh = OUT_JSONL.open("w" if args.fresh else "a", encoding="utf-8")
    aud_fh = AUDIT_JSONL.open("a", encoding="utf-8")
    try:
        for c in comp:
            pid = int(c["subject_id"])
            if pid in done or pid in skip:
                continue
            # Judge the MACHINE CONTINUATION only (two-region seeding); the physician prefix
            # is never judged. 
            paragraph = c.get("prior_generated_md") or c.get("prior_admissions_md", "")
            covered = c.get("covered_hadm_ids", [])             # full union (persisted for c5 drift)
            gen_hadms = c.get("generated_hadm_ids") or covered  # evidence scope for the judged region
            source = _source_for(gen_hadms, deltas)
            if not paragraph.strip() or not source.strip():
                logger.warning("subject=%s: empty paragraph or source -- skipping", pid)
                continue

            # Defensive: the validator is TOTAL (returns judge_error, never raises), but
            # any unexpected fault here is caught so one bad subject never kills the batch.
            try:
                res = fv.validate_faithfulness(
                    seed_ds=paragraph,
                    generated_notes={"assembled_input": source},
                    facts={},
                    hadm_id=str(pid),
                    keep_alive=-1,                  # keep model resident across the batch
                    trace_label=f"subj={pid} hadm={covered}",   # REAL hadm_ids in console
                )
            except Exception as exc:               # pragma: no cover -- belt-and-suspenders
                logger.error("subject=%s unexpected validator fault: %s", pid, exc, exc_info=True)
                res = {"faithfulness_score": None, "pass": None,
                       "error": f"c3_exception: {exc}", "failed_statements": [],
                       "judge_think_mode": None, "judge_degraded": None}
            # The vendored validator echoes back whatever was passed as its `hadm_id` trace
            # key - here that is str(subject_id), NOT an admission id. Keeping it made this
            # file look joinable on hadm_id when it is not. Dropped; the real ids are below.
            res.pop("hadm_id", None)
            res["subject_id"] = pid
            res["covered_hadm_ids"] = covered        # FULL union (c5 drift)
            res["generated_hadm_ids"] = gen_hadms    # the region actually judged
            results.append(res)

            score = res.get("faithfulness_score")
            passed = res.get("pass")
            if passed:
                n_pass += 1
            elif passed is False:
                n_fail += 1
            audit = AuditRecord.from_spec(
                SPEC, PROMPT_VERSION, subject_id=pid, input_hadm_ids=gen_hadms,
                output_text=paragraph,
                scores={"faithfulness_score": score, "n_total": res.get("n_total_statements"),
                        "n_supported": res.get("n_supported"), "pass": passed,
                        "judge_think_mode": res.get("judge_think_mode"),
                        "judge_degraded": res.get("judge_degraded")},
                status="ok" if passed else ("flagged" if passed is False else "error"),
                error=res.get("error"),
            )
            out_fh.write(json.dumps(res) + "\n"); out_fh.flush()
            aud_fh.write(audit.model_dump_json() + "\n"); aud_fh.flush()
            logger.info("subject=%s  hadm=%s  score=%s  %s%s", pid, covered, score,
                        "PASS" if passed else ("FAIL" if passed is False else f"ERR:{res.get('error')}"),
                        "  [NO-THINK JUDGE]" if res.get("judge_degraded") else "")
    finally:
        out_fh.close()
        aud_fh.close()
        fv.ollama_unload(fv.MODEL)
        fv.gpu_cleanup()

    # Merge-rewrite: dedupe by subject_id (new wins), sorted. Keeps the file clean after
    # incremental appends, targeted retries, and resumes.
    merged = dict(existing)
    for r in results:
        merged[int(r["subject_id"])] = r
    with OUT_JSONL.open("w", encoding="utf-8") as fh:
        for sid in sorted(merged):
            fh.write(json.dumps(merged[sid]) + "\n")

    if args.do_print:
        shown = [r for r in results if not targets or int(r["subject_id"]) in targets]
        for r in shown:
            print(json.dumps(r, indent=2))

    scored = [r for r in results if r.get("faithfulness_score") is not None]
    avg = sum(r["faithfulness_score"] for r in scored) / len(scored) if scored else 0.0
    errored = [r for r in results if r.get("faithfulness_score") is None]
    print("\n" + "=" * 55)
    print(f"C3 FAITHFULNESS (threshold {THRESHOLD})")
    print(f"  paragraphs validated : {len(results)}  (this run)")
    print(f"  mean score           : {avg:.3f}")
    print(f"  pass / fail / error  : {n_pass} / {n_fail} / {len(errored)}")
    nothink_pass = [r for r in results if r.get("pass") and r.get("judge_degraded")]
    if nothink_pass:
        print(f"  NO-THINK passes      : {len(nothink_pass)}  (weaker judge -> review)")
        for r in nothink_pass:
            print(f"    subject={r['subject_id']:>10}  score={r['faithfulness_score']:.3f}  "
                  f"attempts={r.get('judge_attempts')}")
        print("    review: python scratch/c3_nothink_review.py")
    if errored:
        print("  ERRORED (auto-retried next plain run, or: --subjects <id>):")
        for r in errored:
            print(f"    subject={r['subject_id']:>10}  {r.get('error')}")
    if n_fail:
        print("  FAILED (hallucination -> C4 correction set):")
        for r in results:
            if r.get("pass") is False:
                print(f"    subject={r['subject_id']:>10}  score={r['faithfulness_score']:.3f}  "
                      f"failed={len(r.get('failed_statements', []))}")
    print(f"  results -> {OUT_JSONL}")
    print("\nNEXT: C4 correction on the FAILED set, then C2 retention-completeness.")


if __name__ == "__main__":
    main()
