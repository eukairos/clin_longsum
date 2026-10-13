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
c1_compactor.py
===============
Folds a patient's aged-out admissions (older than the most recent N_COMPACT_THRESHOLD) 
into a single compact "Prior Admissions" paragraph.

SAFETY MODEL (see PROJECT_PLAN "Core safety idea"):
  The compactor emits ONLY lossy narrative prose. Critical facts are preserved
  losslessly in the retention ledger, which is passed in as "already retained"
  context and echoed back INTACT - the compactor is never asked to summarize or
  re-derive it, so prose compression cannot drift a pinned fact. Faithfulness (C3)
  and retention-completeness (C2) guard the output separately.

MODES (config.COMPACTION_MODE; --mode overrides):
  full        -- one call over the whole aged-out span (stable, audit-true). Default
                 for offline backfill.
  incremental -- fold each aged-out admission into the accumulating paragraph, in
                 admit_seq order (the rolling production path; cheaper, may drift).
                 
SEEDING (incremental):
  On a fold-in, c1 seeds from the prior result and folds ONLY the newly-aged delta, so a
  fold-in is one call regardless of span depth. If a physician has edited the Prior-Admissions
  tier (tiered_view.prior_pinned), that text is frozen byte-exact as a PREFIX and the machine
  continuation is kept separate (prior_generated_md); the served paragraph is
  prefix + marker + continuation. The DB is read best-effort for pin/edit STATE only; seed
  text + span come from the file record, so DB-free runs (evals, first build, --fresh) still
  work. --mode full skips pinned subjects unless --force-repin (which discards the edit).

Enabling holdout shifts the compaction boundary by one (n drops by the held-out admission), 
so c1 MUST be rerun alongside b2/d2/coordinator/d3. Per-patient fold-in already does this (c1 is in the chain); 
the cohort-level holdout-enable path must too.

Inputs:
  data/output/retention_ledger.jsonl   (B2 - per-patient current-state ledger)
  data/interim/retention_facts.jsonl   (B1 - per-admission facts, attached per adm)
  data/interim/cohort_admissions.csv   (bhc_delta / dates / service / admit_seq)
Output:
  data/interim/compaction_results.jsonl  (one CompactorOutput per compacted patient)
  appends one AuditRecord per model call to data/output/generation_audit.jsonl

    cd C:~/Summarization/clin_longsum/src
    python c1_compactor.py
    python c1_compactor.py --mode full --preview 90000029 # print-only, no write
    python c1_compactor.py --subject 90000029          # fold-in: recompute ONE subject, MERGE in place
    python c1_compactor.py --limit 5 --fresh
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

import ollama
import pandas as pd

import cohort
import config
import io_utils
import retention_taxonomy as tax
from roles.contracts import (AgedOutAdmission, AuditRecord, CompactorInput,
                             CompactorOutput, RetentionFact, sha256_text)

sys.path.insert(0, str(config.SYNTH_DIR))
import ollama_utils as ou                # noqa: E402  (shared unload / VRAM hygiene)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("c1_compactor")

SPEC = config.model_for("compactor")
PROMPT_VERSION = config.prompt_version("compactor")
TEMP = SPEC.params.get("temperature", 0.2)
NUM_CTX = SPEC.params.get("num_ctx", 32768)

LEDGER_JSONL = config.OUTPUT_DIR / "retention_ledger.jsonl"
FACTS_JSONL = config.RETENTION_FACTS_JSONL
COHORT_CSV = config.INTERIM_DIR / "cohort_admissions.csv"
OUT_JSONL = config.COMPACTION_JSONL
AUDIT_JSONL = config.AUDIT_JSONL

# Separator between the frozen physician block and the machine continuation in the served
# Prior-Admissions text (Option A two-region seeding). The review UI renders it as a divider
# labelled "Added since your review". A module constant so c4 reconstructs the served text
# byte-identically.
PRIOR_REGION_MARKER = "\n\n---\n\n"


# ── VRAM ────────────────────────────────────────────────────────────────────────

# Shared VRAM / unload helpers (vendor/ollama_utils); names kept for c4 + evals.
_gpu_cleanup = ou.gpu_cleanup


def _ollama_unload() -> None:
    ou.ollama_unload(SPEC.model_id)


# ── PROMPTS (compactor.v1) ──────────────────────────────────────────────────────

_SYS = (
    "You compress the OLDER portion of a patient's longitudinal hospital record into a "
    "single dense 'Prior Admissions' paragraph. You never invent facts. Critical facts "
    "are preserved separately in a retention ledger, so you may compress freely but"
    "you must not contradict that ledger."
)

_RULES_COMMON = (
    "- Use ONLY information in the SOURCE below. Do not add, infer, or extrapolate any "
    "fact, date, or finding not present; unsupported content fails faithfulness review.\n"
    "- The RETENTION LEDGER lists facts already preserved elsewhere. Do NOT re-list them "
    "and do NOT contradict them; reference a condition only for narrative flow.\n"
    "- Compress recurrent or resolved events into trends and counts "
    "(e.g. 'five COPD exacerbations 2019-2021, all resolved'), not a per-admission list.\n"
    "- Preserve date ranges and the clinical trajectory across the span.\n"
    "- Plain clinical prose, a few sentences to a short paragraph. No headers, no bullets. "
    "This is a compression, not a transcription.\n"
    "Write ONLY the Prior Admissions paragraph."
)

_FULL_TMPL = (
    "TASK: Write ONE compact 'Prior Admissions' paragraph summarising this patient's "
    "{n} oldest admissions (the historical base of the record).\n\n"
    "RULES:\n" + _RULES_COMMON + "\n\n"
    "RETENTION LEDGER (already retained; do not repeat or contradict):\n{ledger}\n\n"
    "SOURCE - aged-out admissions (oldest first):\n{aged}\n"
)

_INCR_TMPL = (
    "TASK: Update the existing 'Prior Admissions' paragraph by folding in ONE further "
    "admission that has just aged out of the recent window. Keep it a SINGLE compact "
    "paragraph - merge, do not append; increment counts and extend date ranges rather "
    "than lengthen.\n\n"
    "RULES:\n" + _RULES_COMMON + "\n\n"
    "RETENTION LEDGER (already retained; do not repeat or contradict):\n{ledger}\n\n"
    "EXISTING PRIOR ADMISSIONS PARAGRAPH:\n{existing}\n\n"
    "NEW AGED-OUT ADMISSION:\n{aged}\n"
)


def _ledger_lines(facts: list[RetentionFact]) -> str:
    """Render the ledger with a TEMPORAL ANCHOR per fact.

    Without `adm N` the model cannot tell which facts postdate the admission it is
    summarising, and will happily write a later condition into an earlier paragraph as if
    it were already emerging.
    """
    if not facts:
        return "(none)"
    by_class: dict[str, list[str]] = {}
    for f in facts:
        tag = "" if f.certainty == "confirmed" else f" [{f.certainty}]"
        anchor = f"adm {f.admit_seq}" if f.admit_seq is not None else "adm ?"
        by_class.setdefault(f.retention_class, []).append(f"[{anchor}] {f.text}{tag}")
    out = []
    for cls in sorted(by_class):
        for t in by_class[cls]:
            out.append(f"  - ({cls}) {t}")
    return "\n".join(out)


def _one_aged(a: AgedOutAdmission) -> str:
    head = f"Admission {a.admit_seq}"
    if a.admit_date:
        head += f" ({a.admit_date}{', ' + a.service if a.service else ''})"
    elif a.service:
        head += f" ({a.service})"
    body = (a.bhc_delta or a.bhc_text or "").strip() or "(no narrative)"
    return f"{head}:\n{body}"


def _aged_block(aged: list[AgedOutAdmission]) -> str:
    return "\n\n".join(_one_aged(a) for a in sorted(aged, key=lambda x: x.admit_seq))


# ── compactor role ──────────────────────────────────────────────────────────────

def _chat(messages: list[dict]) -> str:
    resp = ollama.chat(
        model=SPEC.model_id, messages=messages, think=SPEC.think,
        options={"temperature": TEMP, "num_ctx": NUM_CTX}, keep_alive=-1,
    )
    return resp.message.content.strip()


def compact(inp: CompactorInput) -> CompactorOutput:
    """One compactor call. mode picks the prompt; incremental folds inp.aged_out
    (typically one admission) into inp.existing_prior_md."""
    ledger_str = _ledger_lines(inp.ledger_facts)
    aged_str = _aged_block(inp.aged_out)
    if inp.mode == "incremental":
        user = _INCR_TMPL.format(ledger=ledger_str,
                                 existing=inp.existing_prior_md.strip() or "(none yet)",
                                 aged=aged_str)
    else:
        user = _FULL_TMPL.format(n=inp.n_prior_admissions or len(inp.aged_out),
                                 ledger=ledger_str, aged=aged_str)

    prose = _chat([{"role": "system", "content": _SYS},
                   {"role": "user", "content": user}])

    covered = [a.hadm_id for a in inp.aged_out]
    src_chars = sum(len((a.bhc_delta or a.bhc_text or "")) for a in inp.aged_out)
    if inp.mode == "incremental":
        src_chars += len(inp.existing_prior_md)
    audit = AuditRecord.from_spec(
        SPEC, PROMPT_VERSION, taxonomy_version=tax.TAXONOMY_VERSION,
        subject_id=inp.subject_id, input_hadm_ids=covered, output_text=prose,
        scores={"mode": inp.mode, "n_compacted": len(inp.aged_out),
                "chars_in": src_chars, "chars_out": len(prose),
                "compression_ratio": round(len(prose) / max(src_chars, 1), 4)},
    )
    return CompactorOutput(
        subject_id=inp.subject_id, prior_admissions_md=prose,
        ledger_facts=inp.ledger_facts,                  # echoed INTACT (already span-scoped by the caller)
        n_admissions_compacted=len(inp.aged_out),
        covered_hadm_ids=covered, audit=audit,
    )


# ── loaders ─────────────────────────────────────────────────────────────────────

_read_jsonl = io_utils.read_jsonl        # shared tolerant reader; name kept for c2/c3/c4/f*


def _node_to_fact(n: dict, seq_by_hadm: dict[int, int] | None = None) -> RetentionFact:
    """Ledger node -> RetentionFact, anchored to its EARLIEST source admission.

    A consolidated node (b2/b4) can carry several source_hadm_ids. The earliest is what
    dates the fact; admit_seq is populated so the ledger can be scoped to a compaction span and
    rendered with a temporal anchor - without it the compactor would have been shown the patient's
    whole current-state ledger while summarising a single old admission, and can assert
    progression to conditions first documented years later.
    """
    seq_by_hadm = seq_by_hadm or {}
    hadms = [int(h) for h in (n.get("source_hadm_ids") or []) if h is not None]
    first_h, first_seq = (hadms[0] if hadms else None), None
    for h in hadms:
        s = seq_by_hadm.get(h)
        if s is not None and (first_seq is None or s < first_seq):
            first_h, first_seq = h, s
    return RetentionFact(
        text=n["text"], retention_class=n["retention_class"], salience_tier=n["salience_tier"],
        certainty=n.get("certainty", "confirmed"), lifecycle=n.get("current_lifecycle", "active"),
        negative_kind=n.get("negative_kind"),
        source_hadm_id=first_h, admit_seq=first_seq,
    )


def _ledger_for_span(facts: list[RetentionFact], max_seq: int) -> list[RetentionFact]:
    """Facts first documented at or before max_seq.

    Facts with no resolvable admit_seq are KEPT: dropping a pinned fact is a worse failure
    than showing one that may be slightly ahead of the span.
    """
    return [f for f in facts if f.admit_seq is None or f.admit_seq <= max_seq]


def _facts_by_hadm() -> dict[int, list[RetentionFact]]:
    """Per-admission facts, normalized on load (tax.normalize_fact - the same
    surveillance + disease-course rules b2 applies)."""
    out: dict[int, list[RetentionFact]] = {}
    for rec in _read_jsonl(FACTS_JSONL):
        h = int(rec["hadm_id"])
        facts = []
        for fd in rec.get("facts", []):
            tax.normalize_fact(fd)
            facts.append(RetentionFact(**fd))
        out[h] = facts
    return out


# Cohort rows comes from the shared loader (cohort.py - ISO dates, dayfirst-parsed,
# Alias kept for c4/f2.
_cohort_by_hadm = cohort.by_hadm


def _aged_out_for(pid_hadms: list[int], cohort: dict[int, dict],
                  facts_by_hadm: dict[int, list[RetentionFact]],
                  n_admissions: int) -> list[AgedOutAdmission]:
    """Admissions with admit_seq in 1..(n - N_COMPACT_THRESHOLD), oldest first."""
    boundary = n_admissions - config.N_COMPACT_THRESHOLD
    aged = []
    for h in pid_hadms:
        meta = cohort.get(h)
        if not meta or meta["admit_seq"] > boundary:
            continue
        aged.append(AgedOutAdmission(
            hadm_id=h, admit_seq=meta["admit_seq"], admit_date=meta["admit_date"],
            discharge_date=meta["discharge_date"], service=meta["service"],
            bhc_delta=meta["bhc_delta"], bhc_text=meta["bhc_text"],
            facts=facts_by_hadm.get(h, []),
        ))
    return sorted(aged, key=lambda a: a.admit_seq)


# ── seed read (best-effort tiered_view pin/edit STATE; c1 never hard-depends on DB)

def _seed_map(subject_ids=None) -> dict[int, dict]:
    """Best-effort {subject_id: {edited, pinned, pinned_hadm_ids}} from tiered_view.

    Returns {} if the DB is unavailable/absent - c1 then falls back to the file record /
    empty seed, so DB-free runs (evals, first build, --fresh) still work. Only the PIN/EDIT
    STATE is read here; the mergeable seed text and last-covered span come from the prior
    FILE record, so this read never becomes a hard dependency."""
    try:
        from database.pg import get_conn
    except Exception:
        return {}
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            if subject_ids:
                cur.execute(
                    "SELECT subject_id, edited_prior_admissions_md, prior_pinned, "
                    "prior_pinned_hadm_ids FROM tiered_view WHERE subject_id = ANY(%s)",
                    (list(subject_ids),))
            else:
                cur.execute(
                    "SELECT subject_id, edited_prior_admissions_md, prior_pinned, "
                    "prior_pinned_hadm_ids FROM tiered_view")
            rows = cur.fetchall()
    except Exception as exc:
        logger.info("seed: tiered_view read unavailable (%s) -- file/empty seed", exc)
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    out: dict[int, dict] = {}
    for sid, edited, pinned, hadms in rows:
        out[int(sid)] = {"edited": edited, "pinned": bool(pinned),
                         "pinned_hadm_ids": [int(h) for h in (hadms or [])]}
    return out


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["full", "incremental"], default=config.COMPACTION_MODE)
    ap.add_argument("--preview", type=int, default=None, help="compact one subject_id and print it")
    ap.add_argument("--subject", type=int, default=None,
                    help="recompute ONE subject and MERGE into compaction_results.jsonl (fold-in)")
    ap.add_argument("--limit", type=int, default=None, help="cap number of patients")
    ap.add_argument("--fresh", action="store_true", help="overwrite compaction_results.jsonl")
    ap.add_argument("--force-repin", action="store_true",
                    help="under --mode full, rebuild a PINNED subject whole-span, DISCARDING the "
                         "physician prefix (explicit reset; clear the DB pin/edit separately)")
    args = ap.parse_args()
    config.check_paths()

    ledgers = {int(l["subject_id"]): l for l in _read_jsonl(LEDGER_JSONL)}
    if not ledgers:
        raise SystemExit(f"{LEDGER_JSONL} not found/empty. Run b2_build_ledger.py first.")
    cohort = _cohort_by_hadm()
    facts_by_hadm = _facts_by_hadm()
    hadms_by_pid: dict[int, list[int]] = {}
    cdf = pd.read_csv(COHORT_CSV, usecols=["subject_id", "hadm_id"])
    for _, r in cdf.iterrows():
        hadms_by_pid.setdefault(int(r["subject_id"]), []).append(int(r["hadm_id"]))

    if args.subject is not None:
        pids = [args.subject]
    elif args.preview is not None:
        pids = [args.preview]
    else:
        pids = sorted(ledgers)
    if args.limit:
        pids = pids[:args.limit]

    # Prior FILE records (mergeable seed text + last-covered span) and pin/edit STATE (DB).
    # Both gated to non-fresh: --fresh is a clean rebuild that seeds from nothing.
    prior_recs: dict[int, dict] = {}
    if not args.fresh:
        prior_recs = {int(r["subject_id"]): r for r in _read_jsonl(OUT_JSONL)}
    seed_map = {} if args.fresh else _seed_map(pids)
    done_pids = set(prior_recs) if (args.preview is None and args.subject is None) else set()

    results, audits, skipped, compacted = [], [], 0, 0
    try:
        for pid in pids:
            led = ledgers.get(pid)
            if led is None:
                logger.warning("no ledger for subject_id=%s -- skipping", pid)
                continue
            n_adm = int(led.get("n_admissions") or 0)
            if n_adm <= config.N_COMPACT_THRESHOLD:
                skipped += 1
                continue
            if pid in done_pids:
                continue
            aged = _aged_out_for(hadms_by_pid.get(pid, []), cohort, facts_by_hadm, n_adm)
            if not aged:
                skipped += 1
                continue
            seq_by_hadm = {h: m["admit_seq"] for h, m in cohort.items() if m.get("admit_seq")}
            ledger_facts = [_node_to_fact(n, seq_by_hadm)
                            for n in (led.get("nodes", []) + led.get("flagged", []))]

            # ── seed resolution (two-region model) ─────────────────────────
            # DB gives ONLY the pin/edit STATE (best-effort); the mergeable seed text and
            # last-covered span come from the prior FILE record.
            sinfo = seed_map.get(pid)
            pinned = bool(sinfo and sinfo["pinned"] and sinfo["edited"])
            prefix = sinfo["edited"] if pinned else ""
            prefix_hadms = set(sinfo["pinned_hadm_ids"]) if pinned else set()
            prior_rec = prior_recs.get(pid)

            if args.mode == "full":
                if pinned and not args.force_repin:
                    logger.warning("subject=%s is pinned (physician edit) -- SKIP under "
                                   "--mode full; use --force-repin to reset (discards the edit).", pid)
                    skipped += 1
                    continue
                # Scope the ledger to the span being summarised - facts documented AFTER
                # the newest aged-out admission must not inform a paragraph about older ones.
                span_max = max(a.admit_seq for a in aged)
                scoped_ledger = _ledger_for_span(ledger_facts, span_max)
                cout = compact(CompactorInput(
                    subject_id=pid, aged_out=aged,
                    ledger_facts=scoped_ledger,
                    source_hadm_ids=[a.hadm_id for a in aged],
                    n_prior_admissions=len(aged), mode="full"))
                audits.append(cout.audit)
                generated, prefix = cout.prior_admissions_md, ""   # full rebuild: no frozen prefix
                gen_hadms = [a.hadm_id for a in aged]
                served = generated
                base_audit = cout.audit
                base_audit.scores.update(seed_source="none", seed_sha256="", n_delta=len(aged),
                                         ledger_scope_max_seq=span_max,
                                         n_ledger_scoped=len(scoped_ledger),
                                         n_ledger_total=len(ledger_facts))
                n_folded = len(aged)
                if pinned and args.force_repin:
                    logger.warning("subject=%s --force-repin: whole-span rebuild; physician "
                                   "prefix DISCARDED. Clear the DB pin/edit separately.", pid)
            else:  # incremental, seeded: fold ONLY the newly-aged delta into the prior result
                prev_cov = set(int(x) for x in (prior_rec or {}).get("covered_hadm_ids", []))
                last_seq = None
                if prev_cov:
                    seqs = [cohort[h]["admit_seq"] for h in prev_cov if h in cohort]
                    last_seq = max(seqs) if seqs else None
                delta = [a for a in aged if last_seq is None or a.admit_seq > last_seq]
                if not delta:
                    skipped += 1          # nothing newly aged since the last run
                    continue
                # Continuation seed = prose for admissions ALREADY summarised BELOW the pin
                # (prev_cov - pinned span). On the first fold-in after a pin that set is empty -
                # the physician's prefix subsumes the whole prior span - so the continuation must
                # start FRESH from the delta. Seeding the whole prior paragraph there makes the
                # continuation re-describe the pinned span, which c3/c4 flag as unsupported against
                # the delta source and which duplicates the physician prefix in the served text.
                prev_cont = prev_cov - prefix_hadms
                if pinned:
                    seed = ((prior_rec or {}).get("prior_generated_md", "") or "") if prev_cont else ""
                    seed_source = "edited" if seed else "none"
                else:
                    seed = (prior_rec or {}).get("prior_admissions_md", "") or ""
                    seed_source = "generated" if seed else "none"
                prior_md, last_out = seed, None
                scope_max = max(a.admit_seq for a in delta)
                scoped_ledger = _ledger_for_span(ledger_facts, scope_max)
                for a in delta:
                    # Per fold, not per patient: folding admission k must not see facts
                    # first documented later.
                    last_out = compact(CompactorInput(
                        subject_id=pid, existing_prior_md=prior_md, aged_out=[a],
                        ledger_facts=_ledger_for_span(ledger_facts, a.admit_seq),
                        n_prior_admissions=len(aged), mode="incremental",
                        pinned_prefix_md=prefix, seed_source=seed_source))
                    prior_md = last_out.prior_admissions_md
                    audits.append(last_out.audit)
                generated = prior_md
                gen_hadms = sorted(prev_cont | {a.hadm_id for a in delta})
                served = (prefix + PRIOR_REGION_MARKER + generated) if prefix else generated
                base_audit = last_out.audit
                seeded = prior_rec is not None or bool(prefix)
                base_audit.scores.update(
                    mode="incremental_seeded" if seeded else "incremental",
                    seed_source=seed_source, seed_sha256=(sha256_text(seed) if seed else ""),
                    n_delta=len(delta), ledger_scope_max_seq=scope_max,
                    n_ledger_scoped=len(scoped_ledger), n_ledger_total=len(ledger_facts))
                # n_folded is THIS CALL's work (the delta) and is used only for the log line
                # below. It must NOT become n_admissions_compacted: that field means "how many
                # admissions this paragraph represents", which after seeding is the whole aged
                # span, and f2's gate_consistency asserts it equals len(covered_hadm_ids). The
                # per-call count lives in audit.scores.n_delta.
                n_folded = len(delta)

            # served must start with the frozen physician block (guards a code path that would
            # let the model rewrite it - true by construction here).
            assert (not prefix) or served.startswith(prefix), f"prefix mutated for subject={pid}"

            # persist the SPAN-SCOPED ledger the model was actually shown, not the
            # patient's whole current-state ledger. The unscoped echo made this record
            # useless as evidence of what c1 saw, and fed c4's re-compaction a set that
            # includes facts first documented AFTER the span - reintroducing the leak the
            # scoping was added to prevent.
            out = CompactorOutput(
                subject_id=pid, prior_admissions_md=served,
                pinned_prefix_md=prefix, prior_generated_md=generated,
                generated_hadm_ids=gen_hadms, ledger_facts=scoped_ledger,
                n_admissions_compacted=len(aged),
                covered_hadm_ids=[a.hadm_id for a in aged], audit=base_audit)
            results.append(out)
            compacted += 1
            logger.info("subject=%s  mode=%s  folded %d  prefix=%s  ratio=%.3f",
                        pid, base_audit.scores.get("mode", args.mode), n_folded, bool(prefix),
                        out.audit.scores.get("compression_ratio", 0))
    finally:
        _ollama_unload()

    if args.preview is not None and results:
        print(results[0].model_dump_json(indent=2))
        return

    if args.subject is not None:
        io_utils.merge_jsonl(OUT_JSONL, [json.loads(o.model_dump_json()) for o in results],
                             lambda r: int(r["subject_id"]))
        # Invalidate any stale C4-corrected paragraph for this subject. C4 now runs later in
        # the fold-in chain and rewrites this if the fresh paragraph fails faithfulness; if it
        # passes (no C4 entry), the coordinator falls back to this fresh compaction.
        corrected = config.INTERIM_DIR / "compaction_corrected.jsonl"
        if corrected.exists():
            ck = [r for r in _read_jsonl(corrected) if int(r["subject_id"]) != args.subject]
            io_utils.write_jsonl(corrected, ck)
    else:
        mode_w = "w" if args.fresh else "a"
        with OUT_JSONL.open(mode_w, encoding="utf-8") as fh:
            for o in results:
                fh.write(o.model_dump_json() + "\n")
    with AUDIT_JSONL.open("a", encoding="utf-8") as fh:
        for a in audits:
            fh.write(a.model_dump_json() + "\n")

    print("\n" + "=" * 55)
    print(f"C1 COMPACTOR ({args.mode})")
    print(f"  patients compacted : {compacted}")
    print(f"  skipped (<= N={config.N_COMPACT_THRESHOLD} or no aged-out): {skipped}")
    print(f"  model calls        : {len(audits)}")
    if results:
        ratios = [o.audit.scores.get("compression_ratio", 0) for o in results]
        print(f"  mean compression   : {sum(ratios)/len(ratios):.3f} (out/in chars)")
    print(f"  results -> {OUT_JSONL}")
    print("\nNEXT: C3 faithfulness (threshold 0.6) + C2 retention-completeness on these.")


if __name__ == "__main__":
    main()
