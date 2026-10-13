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
c4_correct.py
=============
Corrects the compacted paragraphs that FAILED c3 faithfulness, via a
CONSTRAINED RE-COMPACT. A faithfulness failure means the paragraph
asserts something the SOURCE does not support (a hallucination); the fix is to
DROP or CORRECT that claim in the paragraph, never to add it.

The compactor remains the sole owner of the paragraph: c3's unsupported claims
are fed back with an instruction to remove/correct exactly those, regenerate, and
re-validate. The retention ledger is passed through intact and never edited.

TWO-REGION : only the MACHINE CONTINUATION (prior_generated_md) is
re-compacted; a physician-edited prefix (pinned_prefix_md) is frozen and re-prepended
afterwards, so correction can never touch physician text. The corrected record carries
the reconstructed served paragraph (prefix + marker + continuation) plus the region
fields. Pre-seeding records fall back to prior_admissions_md / covered_hadm_ids.

Two phases (one model resident at a time):
  A. re-compact failed paragraphs            (compactor model, then unload)
  B. re-validate the corrected paragraphs     (validator model, then unload)

Validator now bounds every judge call (num_predict + inactivity + wall-clock) 
and returns a judge_error RESULT (score=None) instead of hanging or raising on a deepseek thinking-loop. 
A bad subject self-terminates rather than crashing the batch; its corrected_score is
None and it is RE-TRIED on the next plain run.

RETRY / RESUME:
  --fresh           overwrite corrected jsonl + csv and re-correct all failures.
  (plain rerun)     resume: skip subjects already corrected AND re-validated OK;
                    RE-TRY any whose corrected_score is None (Phase-B error).
  --subjects A B    re-correct exactly these subject_ids and persist them
                    (replace-in-place), even if previously done.
  --subject S       singular alias for --subjects (one subject; matches the
                    flag every other fold-in stage + foldin's _run() use).
  --print           pretty-print the full CompactorOutput JSON for the target(s).
  --skip A B        skip these subject_ids this run.

Outputs:
  data/interim/compaction_corrected.jsonl       corrected CompactorOutput per patient
  data/output/compaction_corrections_audit.csv  one row per dropped/corrected claim,
                                                 with model action, validator evidence,
                                                 before/after score, judge_degraded
                                                 (no-think re-validation -> review),
                                                 source hadm_ids
  appends AuditRecords to data/output/generation_audit.jsonl

Reads c3 (compaction_faithfulness.jsonl) for the FAILED set and c1
(compaction_results.jsonl) for the original paragraph + ledger + covered hadm_ids.

    cd C:~/Summarization/clin_longsum/src
    python c4_correct.py
    python c4_correct.py --subject 90000017                 # targeted retry (singular alias)
    python c4_correct.py --subjects 90000017 90000029       # retry several
    python c4_correct.py --subject 90000017 --print         # retry + inspect
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys

import config
import retention_taxonomy as tax
import c1_compactor as c1
from roles.contracts import AuditRecord, CompactorOutput, RetentionFact

sys.path.insert(0, str(config.SYNTH_DIR))
import faithfulness_validator as fv          # noqa: E402
import ollama_utils as ou                    # noqa: E402  (shared chat_json cascade)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("c4_correct")

SPEC = config.model_for("compactor")
PROMPT_VERSION = "compactor.v1+correct"
TEMP = SPEC.params.get("temperature", 0.2)
NUM_CTX = SPEC.params.get("num_ctx", 32768)
NUM_PREDICT = SPEC.params.get("num_predict", 8192)   # bound the re-compact too

C1_JSONL = config.COMPACTION_JSONL
C3_JSONL = config.INTERIM_DIR / "compaction_faithfulness.jsonl"
OUT_JSONL = config.INTERIM_DIR / "compaction_corrected.jsonl"
CSV_OUT = config.OUTPUT_DIR / "compaction_corrections_audit.csv"
AUDIT_JSONL = config.AUDIT_JSONL

CSV_COLS = ["subject_id", "action", "claim", "correction", "model_reason",
            "validator_evidence", "resolved_after", "original_score",
            "corrected_score", "judge_degraded", "source_hadm_ids"]


# ── corrective prompt ───────────────────────────────────────────────────────────

_SYS = (
    "You revise a compacted 'Prior Admissions' paragraph to remove or correct claims that "
    "are NOT supported by the source admissions. You never add new facts. Output JSON only."
)

_TMPL = (
    "TASK: The PREVIOUS paragraph contains claims NOT supported by the SOURCE. Produce a "
    "corrected paragraph that DROPS each unsupported claim, or CORRECTS it to match exactly "
    "what the source says. Change nothing else.\n\n"
    "RULES:\n"
    "- Use ONLY the SOURCE. Do not add, infer, or introduce any fact not present in it.\n"
    "- For each unsupported claim: delete it, or rewrite it to match the source. Never invent "
    "a replacement.\n"
    "- Preserve every supported part unchanged. Keep it a single compact paragraph.\n"
    "- Do NOT re-list or contradict the RETENTION LEDGER.\n\n"
    "Return ONLY this JSON:\n"
    '{{"paragraph": "<corrected paragraph>", '
    '"changes": [{{"claim": "<unsupported claim, verbatim>", "action": "dropped|corrected", '
    '"correction": "<new wording if corrected, else empty>", "reason": "<why>"}}]}}\n\n'
    "RETENTION LEDGER (already retained; do not repeat or contradict):\n{ledger}\n\n"
    "SOURCE - admissions this paragraph was compacted from (oldest first):\n{source}\n\n"
    "UNSUPPORTED CLAIMS (from faithfulness review):\n{claims}\n\n"
    "PREVIOUS PARAGRAPH:\n{paragraph}\n"
)


# The compactor with think=True can spend the whole num_predict budget on reasoning and
# return EMPTY content (subject 90000042 stress case: ~74s generation, no JSON). The shared
# ollama_utils.chat_json cascade handles this: configured-think -> no-think -> no-think +
# explicit JSON-only, with loose parsing. Persistent failure still falls through to "keep
# original" in the caller.
def _compact_json(messages: list[dict], label: str) -> dict:
    return ou.chat_json(SPEC.model_id, messages, think=SPEC.think,
                        options={"temperature": TEMP, "num_ctx": NUM_CTX,
                                 "num_predict": NUM_PREDICT},
                        keep_alive=-1, label=label)


def _claims_block(failed: list[dict]) -> str:
    lines = []
    for f in failed:
        ev = f.get("evidence") or f.get("note") or ""
        lines.append(f"  - {f.get('statement', '')}" + (f"   (review note: {ev})" if ev else ""))
    return "\n".join(lines)


def _source_for(covered: list[int], cohort: dict[int, dict]) -> str:
    rows = []
    for h in covered:
        m = cohort.get(h)
        if not m:
            continue
        txt = (m.get("bhc_delta") or m.get("bhc_text") or "").strip()
        if txt:
            rows.append((m["admit_seq"], txt))
    return "\n\n".join(f"Admission {seq}:\n{txt}" for seq, txt in sorted(rows))


def _corrected_score(rec: dict):
    """Pull the re-validation score out of a stored CompactorOutput line."""
    try:
        return (rec.get("audit") or {}).get("scores", {}).get("corrected_score")
    except Exception:
        return None


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--subjects", type=int, nargs="*", default=[],
                    help="subject_id(s) to (re-)correct and persist, even if already done")
    ap.add_argument("--subject", type=int, default=None,
                    help="single subject_id alias for --subjects (pipeline/fold-in consistency)")
    ap.add_argument("--print", dest="do_print", action="store_true",
                    help="pretty-print the full CompactorOutput JSON for the targeted subject(s)")
    ap.add_argument("--fresh", action="store_true", help="overwrite corrected jsonl + csv")
    ap.add_argument("--skip", type=int, nargs="*", default=[],
                    help="subject_id(s) to skip (e.g. a known deepseek thinking-loop)")
    args = ap.parse_args()
    config.check_paths()

    c3 = c1._read_jsonl(C3_JSONL)
    if not c3:
        raise SystemExit(f"{C3_JSONL} not found/empty. Run c3_faithfulness.py first.")
    c1_by_pid = {int(r["subject_id"]): r for r in c1._read_jsonl(C1_JSONL)}
    cohort = c1._cohort_by_hadm()

    targets = set(args.subjects or [])
    if args.subject is not None:
        targets.add(args.subject)
    skip = set(args.skip or [])

    # GATE ON UNSUPPORTED STATEMENTS.
    # c3's `pass` is score >= threshold, so a paragraph with a handful of unsupported
    # statements still passes on a good aggregate. Observed: a compaction of admission 1 
    # asserting progression to a syndrome first documented at admission 8.
    # c3 flagged the statement, the paragraph scored 0.88 against a 0.6
    # threshold, `pass` was True, and the fabrication shipped. Any named unsupported
    # statement is now worth a correction pass; `pass` is retained for reporting only.
    failed = [r for r in c3 if r.get("failed_statements")]
    if targets:
        failed = [r for r in failed if int(r["subject_id"]) in targets]

    # Existing corrected output (resume + merge-rewrite). done = subjects already
    # re-validated with a non-None corrected_score; extraction/retention errors (None) auto-retry.
    existing: dict[int, dict] = {}
    if not args.fresh:
        for r in c1._read_jsonl(OUT_JSONL):
            if r.get("subject_id") is not None:
                existing[int(r["subject_id"])] = r
    done = {sid for sid, r in existing.items() if _corrected_score(r) is not None}
    done -= targets

    failed = [r for r in failed
              if int(r["subject_id"]) not in done and int(r["subject_id"]) not in skip]
    if not failed:
        print("No failed paragraphs to correct (all done/skipped, or C3 had no failures). Nothing to do.")
        return
    logger.info("correcting %d failed paragraph(s)%s", len(failed),
                f" ({len(done)} already done)" if done else "")

    # ── Phase A: constrained re-compact (compactor model) ───────────────────────
    staged = []          # (pid, corrected, changes, orig_score, failed_stmts, covered, gen_hadms, prefix, ledger_facts)
    try:
        for r in failed:
            pid = int(r["subject_id"])
            c1rec = c1_by_pid.get(pid)
            if not c1rec:
                logger.warning("subject=%s in C3 but not in C1 results -- skipping", pid)
                continue
            # Correct the MACHINE CONTINUATION only (two-region seeding); the physician prefix
            # is frozen and re-prepended after correction. Pre-seeding records lack these fields
            # -> fall back to the served paragraph + full covered (identical behaviour).
            prefix = c1rec.get("pinned_prefix_md", "") or ""
            paragraph = c1rec.get("prior_generated_md") or c1rec.get("prior_admissions_md", "")
            covered = c1rec.get("covered_hadm_ids", [])              # full union (persisted)
            gen_hadms = c1rec.get("generated_hadm_ids") or covered   # evidence scope for the region
            # `ledger_facts` is the SPAN-SCOPED set c1 showed the model. 
            ledger_facts = [RetentionFact(**d) for d in
                            (c1rec.get("ledger_facts") or c1rec.get("retention_ledger") or [])]
            source = _source_for(gen_hadms, cohort)
            user = _TMPL.format(
                ledger=c1._ledger_lines(ledger_facts), source=source,
                claims=_claims_block(r["failed_statements"]), paragraph=paragraph)
            try:
                data = _compact_json(
                    [{"role": "system", "content": _SYS}, {"role": "user", "content": user}],
                    f"subject={pid} re-compact")
            except Exception as exc:
                logger.error("subject=%s re-compact failed: %s -- keeping original", pid, exc)
                continue
            corrected = (data.get("paragraph") or "").strip()
            raw_changes = data.get("changes", [])
            raw_changes = raw_changes if isinstance(raw_changes, list) else []
            changes = [c for c in raw_changes if isinstance(c, dict)]   # model sometimes emits bare strings
            if len(changes) != len(raw_changes):
                logger.warning("subject=%s dropped %d non-dict change entries from model output",
                               pid, len(raw_changes) - len(changes))
            if not corrected:
                logger.warning("subject=%s empty corrected paragraph -- keeping original", pid)
                continue
            staged.append((pid, corrected, changes, r.get("faithfulness_score"),
                           r["failed_statements"], covered, gen_hadms, prefix, ledger_facts))
            logger.info("subject=%s re-compacted (%d changes)", pid, len(changes))
    finally:
        c1._ollama_unload()

    # ── Phase B: re-validate corrected paragraphs (validator model) ─────────────
    # Incremental persistence: each corrected paragraph is written the moment it re-validates,
    # so an interrupt (deepseek thinking-loop) preserves Phase-B progress and a restart resumes.
    results, new_recs, csv_rows = [], [], []
    staged_pids = {pid for (pid, *_rest) in staged}
    mode_w = "w" if args.fresh else "a"
    write_header = args.fresh or not CSV_OUT.exists()
    out_fh = OUT_JSONL.open(mode_w, encoding="utf-8")
    aud_fh = AUDIT_JSONL.open("a", encoding="utf-8")
    csv_fh = CSV_OUT.open(mode_w, encoding="utf-8", newline="")
    csv_w = csv.DictWriter(csv_fh, fieldnames=CSV_COLS)
    if write_header:
        csv_w.writeheader()
    try:
        for pid, corrected, changes, orig_score, failed_stmts, covered, gen_hadms, prefix, ledger_facts in staged:
            source = _source_for(gen_hadms, cohort)
            try:
                reval = fv.validate_faithfulness(
                    seed_ds=corrected, generated_notes={"assembled_input": source},
                    facts={}, hadm_id=str(pid), keep_alive=-1,
                    trace_label=f"subj={pid} hadm={covered}")   # REAL hadm_ids in console
            except Exception as exc:                # pragma: no cover -- belt-and-suspenders
                logger.error("subject=%s unexpected re-validate fault: %s", pid, exc, exc_info=True)
                reval = {"faithfulness_score": None, "pass": None,
                         "error": f"c4_exception: {exc}", "failed_statements": [],
                         "judge_think_mode": None, "judge_degraded": None}
            new_score = reval.get("faithfulness_score")
            pass_after = reval.get("pass")
            still_failed = {s.get("statement", "") for s in reval.get("failed_statements", [])}
            judge_degraded = reval.get("judge_degraded")
            judge_attempts = reval.get("judge_attempts")

            evidence_by_claim = {s.get("statement", ""): (s.get("evidence") or s.get("note") or "")
                                 for s in failed_stmts}
            rows_this = []
            for ch in changes:
                claim = ch.get("claim", "")
                row = {
                    "subject_id": pid,
                    "action": ch.get("action", ""),
                    "claim": claim,
                    "correction": ch.get("correction", ""),
                    "model_reason": ch.get("reason", ""),
                    "validator_evidence": evidence_by_claim.get(claim, ""),
                    "resolved_after": claim not in still_failed,
                    "original_score": orig_score,
                    "corrected_score": new_score,
                    "judge_degraded": judge_degraded,
                    "source_hadm_ids": ";".join(str(h) for h in gen_hadms),
                }
                rows_this.append(row)
                csv_rows.append(row)

            # Re-prepend the frozen physician prefix (never corrected); served text is
            # prefix + marker + corrected continuation. c3/c4 only ever touch the continuation.
            served = (prefix + c1.PRIOR_REGION_MARKER + corrected) if prefix else corrected
            assert (not prefix) or served.startswith(prefix), f"prefix mutated for subject={pid}"
            audit = AuditRecord.from_spec(
                SPEC, PROMPT_VERSION, taxonomy_version=tax.TAXONOMY_VERSION,
                subject_id=pid, input_hadm_ids=gen_hadms, output_text=corrected,
                scores={"correction": True, "n_changes": len(changes),
                        "original_score": orig_score, "corrected_score": new_score,
                        "pass_after": pass_after,
                        "judge_think_mode": reval.get("judge_think_mode"),
                        "judge_degraded": judge_degraded,
                        "judge_attempts": judge_attempts,
                        "unresolved": len(still_failed)},
                status="ok" if pass_after else ("flagged" if pass_after is False else "error"))
            out = CompactorOutput(
                subject_id=pid, prior_admissions_md=served,
                pinned_prefix_md=prefix, prior_generated_md=corrected,
                generated_hadm_ids=gen_hadms, ledger_facts=ledger_facts,
                n_admissions_compacted=len(covered), covered_hadm_ids=covered, audit=audit)
            results.append(out)
            new_recs.append(json.loads(out.model_dump_json()))
            out_fh.write(out.model_dump_json() + "\n"); out_fh.flush()
            aud_fh.write(audit.model_dump_json() + "\n"); aud_fh.flush()
            for row in rows_this:
                csv_w.writerow(row)
            csv_fh.flush()
            logger.info("subject=%s  hadm=%s  %.3f -> %s  %s%s", pid, covered, orig_score or 0.0,
                        new_score, "PASS" if pass_after else ("still FAIL" if pass_after is False
                                                              else f"ERR:{reval.get('error')}"),
                        "  [NO-THINK JUDGE]" if judge_degraded else "")
    finally:
        out_fh.close()
        aud_fh.close()
        csv_fh.close()
        fv.ollama_unload(fv.MODEL)
        fv.gpu_cleanup()

    # Merge-rewrite the corrected jsonl: dedupe by subject_id (new wins), sorted.
    merged = dict(existing)
    for rec in new_recs:
        merged[int(rec["subject_id"])] = rec
    with OUT_JSONL.open("w", encoding="utf-8") as fh:
        for sid in sorted(merged):
            fh.write(json.dumps(merged[sid]) + "\n")

    # CSV: when we re-ran specific subjects (targets/resume), drop their OLD rows so the
    # review CSV does not accumulate stale duplicates, then keep this run's fresh rows.
    if not args.fresh and CSV_OUT.exists() and staged_pids:
        kept = []
        with CSV_OUT.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                try:
                    sid = int(row.get("subject_id", ""))
                except (TypeError, ValueError):
                    continue
                # keep prior rows for subjects NOT touched this run; this run's rows are
                # already in the file (appended above) and belong to staged_pids.
                kept.append(row)
        # de-dup: for staged subjects keep only the LAST occurrence (this run's rows).
        seen_last: dict[tuple, dict] = {}
        ordered = []
        for row in kept:
            try:
                sid = int(row.get("subject_id", ""))
            except (TypeError, ValueError):
                ordered.append(row); continue
            if sid in staged_pids:
                key = (sid, row.get("claim", ""), row.get("action", ""))
                seen_last[key] = row          # last wins (this run)
            else:
                ordered.append(row)
        ordered.extend(seen_last.values())
        with CSV_OUT.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=CSV_COLS)
            w.writeheader()
            for row in ordered:
                w.writerow({k: row.get(k, "") for k in CSV_COLS})

    if args.do_print:
        shown = [o for o in results if not targets or o.subject_id in targets]
        for o in shown:
            print(o.model_dump_json(indent=2))

    n_pass = sum(1 for o in results if o.audit.scores.get("pass_after"))
    n_err = sum(1 for o in results if o.audit.scores.get("corrected_score") is None)
    n_resolved = sum(1 for r in csv_rows if r["resolved_after"])
    nothink_pass = [o for o in results if o.audit.scores.get("pass_after")
                    and o.audit.scores.get("judge_degraded")]
    print("\n" + "=" * 55)
    print("C4 CORRECTION (constrained re-compact)")
    print(f"  paragraphs corrected : {len(results)}  (this run)")
    print(f"  now passing          : {n_pass} / {len(results)}")
    if nothink_pass:
        print(f"  NO-THINK passes      : {len(nothink_pass)}  (weaker judge -> review)")
        for o in nothink_pass:
            print(f"    subject={o.subject_id:>10}  score={o.audit.scores.get('corrected_score')}  "
                  f"attempts={o.audit.scores.get('judge_attempts')}")
    print(f"  claims changed       : {len(csv_rows)}  (resolved {n_resolved})")
    print(f"  corrected -> {OUT_JSONL}")
    print(f"  review CSV -> {CSV_OUT}")
    if n_err:
        print(f"  {n_err} re-validate error(s) (corrected_score=None) -> auto-retried next run, "
              f"or: --subjects <id>")
    still = len(results) - n_pass - n_err
    if still:
        print(f"  {still} still failing -> human review (audit status=flagged).")
    print("\nNEXT: C2 retention-completeness (deterministic ledger check).")


if __name__ == "__main__":
    main()
