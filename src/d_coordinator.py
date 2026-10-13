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
d_coordinator.py
================
Deterministicallys assemble the served LongitudinalView per patient from the tier artifacts (no LLM):

  Tier 1   current_episode_md  <- latest admission BHC  (cohort_admissions.csv)
  Tier 2a  hot_window          <- d1_paragrapher.py     (episode_paragraphs.jsonl)
  Tier 2b  tldr_md             <- d2_tldr.py            (tldr_results.jsonl)
  Tier 3   prior_admissions_md <- c1 / c4               (compaction_(corrected_)results)
  crosscut ledger_facts        <- b2_build_ledger.py    (retention_ledger.jsonl)

Window math (N_HOT_DISPLAY always <= N_COMPACT_THRESHOLD):
  aged-out / compacted : admit_seq <= n - N_COMPACT_THRESHOLD          (Tier 3)
  current episode      : admit_seq == n                                (Tier 1)
  hot window           : the un-compacted span minus current, last
                         N_HOT_DISPLAY of admit_seq in [.. , n-1]       (Tier 2a)

Output: data/output/longitudinal_view.jsonl  (one LongitudinalView per patient)
        --write-md also renders data/output/views/{subject_id}.md (newest-first)
        --subject prints the rendered view for one patient (spot-check)

    cd C:~/Summarization/clin_longsum/src
    python d_coordinator.py --subject 90000029
    python d_coordinator.py --render 90000029        # print one patient's assembled view
    python d_coordinator.py --write-md --limit 5
"""

from __future__ import annotations

import argparse
import json
import logging

import pandas as pd

import cohort
import config
import c1_compactor as c1
import holdout
import io_utils
from roles.contracts import EpisodeParagraph, LongitudinalView, RetentionFact

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("d_coordinator")

TLDR_JSONL = config.TLDR_JSONL
COMPACTION_JSONL = config.COMPACTION_JSONL
CORRECTED_JSONL = config.INTERIM_DIR / "compaction_corrected.jsonl"
PARAGRAPHS_JSONL = config.EPISODE_PARAGRAPHS_JSONL
LEDGER_JSONL = config.OUTPUT_DIR / "retention_ledger.jsonl"
COHORT_CSV = config.INTERIM_DIR / "cohort_admissions.csv"
VIEW_JSONL = config.OUTPUT_DIR / "longitudinal_view.jsonl"
VIEWS_MD_DIR = config.OUTPUT_DIR / "views"


_read_jsonl = io_utils.read_jsonl        # shared tolerant reader


def _iso(ts) -> str:
    """A parsed cohort timestamp -> 'YYYY-MM-DD', or '' for missing. Cohort dates are
    parsed once in main() with dayfirst=True, so this is the single source of truth for
    every displayed date (header, current episode, and hot-window sub-headers)."""
    if ts is None or pd.isna(ts):
        return ""
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


def assemble(pid: int, adms: pd.DataFrame, tldr_by_pid: dict, prior_by_pid: dict,
             paras_by_seq: dict, ledger_by_pid: dict,
             incoming=None, prior_gen_by_pid: dict | None = None) -> tuple[LongitudinalView, dict]:
    adms = adms.sort_values("admit_seq")
    n = len(adms)
    latest = adms.iloc[-1]
    boundary = n - config.N_COMPACT_THRESHOLD          # aged-out: admit_seq <= boundary

    # hot window: un-compacted span (seq > boundary), excluding current (seq == n),
    # last N_HOT_DISPLAY most recent.
    hot_seqs = [s for s in range(boundary + 1, n)]      # n-... .. n-1
    hot_seqs = hot_seqs[-config.N_HOT_DISPLAY:]
    # Re-derive each hot paragraph's admit_date from the cohort (single source of truth)
    # rather than trusting the value D1 stored, so dates are correct without regenerating
    # paragraphs. admittime is already parsed (dayfirst) in main().
    seq_iso = {int(r["admit_seq"]): _iso(r["admittime"]) for _, r in adms.iterrows()}
    hot = []
    for s in hot_seqs:
        p = paras_by_seq.get((pid, s))
        if p is None:
            continue
        hot.append(p.model_copy(update={"admit_date": seq_iso.get(s) or p.admit_date}))

    led = ledger_by_pid.get(pid, {})
    ledger_facts = [c1._node_to_fact(nd) for nd in (led.get("nodes", []) + led.get("flagged", []))]

    # Tier 1 of the ESTABLISHED record is the most recent established admission
    # (the one before the incoming, held-out admission). Show its single-episode
    # PARAGRAPH (D1), not the raw BHC; falls back to BHC only if no paragraph exists.
    latest_seq = int(latest["admit_seq"])
    prev_para = paras_by_seq.get((pid, latest_seq))
    current_episode_md = (prev_para.paragraph_md if prev_para
                          else (str(latest["bhc_text"]) if pd.notna(latest.get("bhc_text")) else ""))

    view = LongitudinalView(
        subject_id=pid,
        n_total_admissions=n,
        tldr_md=tldr_by_pid.get(pid, ""),
        current_episode_md=current_episode_md,
        hot_window=hot,
        prior_admissions_md=prior_by_pid.get(pid, ""),
        # machine continuation for the display divider; pre-seeding records fall back to
        # the served text (served == generated when there is no physician prefix).
        prior_generated_md=(prior_gen_by_pid or {}).get(pid, prior_by_pid.get(pid, "")),
        ledger_facts=ledger_facts,
    )
    meta = {
        "first": _iso(adms["admittime"].min()),
        "last": _iso(adms["dischtime"].max()),
        "cur_seq": int(latest["admit_seq"]),
        "cur_date": _iso(latest["admittime"]),
        "cur_service": str(latest["service"]) if pd.notna(latest.get("service")) else "",
    }
    if incoming is not None:
        meta["incoming"] = {
            "hadm_id": int(incoming["hadm_id"]),
            "admit_seq": int(incoming["admit_seq"]),
            "admit_date": _iso(incoming["admittime"]),
            "service": str(incoming["service"]) if pd.notna(incoming.get("service")) else "",
            "bhc_text": str(incoming["bhc_text"]) if pd.notna(incoming.get("bhc_text")) else "",
        }
    return view, meta


def render(view: LongitudinalView, meta: dict) -> str:
    out = [f"{view.n_total_admissions} admissions ({meta['first']} to {meta['last']})", ""]
    out += ["## TL;DR", view.tldr_md or "(none)", ""]
    cur_when = f" ({meta['cur_date']}{', ' + meta['cur_service'] if meta['cur_service'] else ''})"
    out += [f"## Previous Admission {meta['cur_seq']}{cur_when}",
            view.current_episode_md or "(none)", ""]
    if view.hot_window:
        out.append("## Recent Admissions")
        for p in sorted(view.hot_window, key=lambda x: x.admit_seq, reverse=True):  # newest-first
            when = f" ({p.admit_date})" if p.admit_date else ""
            out += [f"### Admission {p.admit_seq}{when}", p.paragraph_md, ""]
    out += ["## Prior Admissions", view.prior_admissions_md or "(none)", ""]
    if meta.get("incoming"):
        inc = meta["incoming"]
        when = f" ({inc['admit_date']}{', ' + inc['service'] if inc['service'] else ''})"
        out += ["---",
                f"## Incoming (held out) -- Admission {inc['admit_seq']}{when}",
                "_Under review; NOT yet folded into the record above._",
                inc["bhc_text"] or "(none)", ""]
    if view.ledger_facts:
        out.append("## Retention Ledger (pinned facts)")
        for f in sorted(view.ledger_facts, key=lambda x: (x.salience_tier, x.retention_class)):
            cert = "" if f.certainty == "confirmed" else f" [{f.certainty}]"
            out.append(f"- [{f.salience_tier} {f.retention_class}] {f.text}{cert}")
    return "\n".join(out).rstrip() + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--render", type=int, default=None, help="print rendered view for one patient")
    ap.add_argument("--subject", type=int, default=None,
                    help="recompute ONE subject and MERGE into longitudinal_view.jsonl (fold-in)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--write-md", action="store_true", help="render data/output/views/{pid}.md")
    args = ap.parse_args()
    config.check_paths()                                # asserts N_HOT_DISPLAY <= N_COMPACT_THRESHOLD

    tldr_by_pid = {int(r["subject_id"]): r.get("tldr_md", "") for r in _read_jsonl(TLDR_JSONL)}
    # prior_by_pid = SERVED text (prefix+marker+continuation); prior_gen_by_pid = the machine
    # continuation alone (for the display divider). Corrected (c4) wins over c1 for both.
    prior_by_pid, prior_gen_by_pid = {}, {}
    for r in _read_jsonl(COMPACTION_JSONL):
        sid = int(r["subject_id"])
        prior_by_pid[sid] = r.get("prior_admissions_md", "")
        prior_gen_by_pid[sid] = r.get("prior_generated_md") or r.get("prior_admissions_md", "")
    for r in _read_jsonl(CORRECTED_JSONL):
        sid = int(r["subject_id"])
        prior_by_pid[sid] = r.get("prior_admissions_md", "")
        prior_gen_by_pid[sid] = r.get("prior_generated_md") or r.get("prior_admissions_md", "")
    paras_by_seq = {}
    for r in _read_jsonl(PARAGRAPHS_JSONL):
        paras_by_seq[(int(r["subject_id"]), int(r["admit_seq"]))] = EpisodeParagraph(**r)
    ledger_by_pid = {int(l["subject_id"]): l for l in _read_jsonl(LEDGER_JSONL)}

    df = cohort.load()                       # shared loader: admittime/dischtime parsed dayfirst
    by_pid = {pid: g for pid, g in df.groupby("subject_id")}

    if args.subject is not None:
        if args.subject not in by_pid:
            raise SystemExit(f"subject {args.subject} not in cohort.")
        pids = [args.subject]
    elif args.render is not None:
        pids = [args.render]
    else:
        pids = sorted(by_pid)
    if args.limit:
        pids = pids[:args.limit]

    views = []
    if args.write_md:
        VIEWS_MD_DIR.mkdir(parents=True, exist_ok=True)
    for pid in pids:
        est, hold = holdout.split(by_pid[pid])
        incoming = hold.sort_values("admit_seq").iloc[-1] if len(hold) else None
        view, meta = assemble(int(pid), est, tldr_by_pid, prior_by_pid,
                              paras_by_seq, ledger_by_pid, incoming,
                              prior_gen_by_pid=prior_gen_by_pid)
        views.append(view)
        if args.render is not None:
            print(render(view, meta))
            return
        if args.write_md:
            (VIEWS_MD_DIR / f"{pid}.md").write_text(render(view, meta), encoding="utf-8")

    if args.subject is not None:
        # Fold-in: merge just this subject's view into the cohort jsonl, keep the rest.
        io_utils.merge_jsonl(VIEW_JSONL, [json.loads(v.model_dump_json()) for v in views],
                             lambda r: int(r["subject_id"]))
    else:
        with VIEW_JSONL.open("w", encoding="utf-8") as fh:
            for v in views:
                fh.write(v.model_dump_json() + "\n")

    n_tldr = sum(1 for v in views if v.tldr_md)
    n_prior = sum(1 for v in views if v.prior_admissions_md)
    n_hot = sum(len(v.hot_window) for v in views)
    n_led = sum(len(v.ledger_facts) for v in views)
    print("\n" + "=" * 55)
    print("D COORDINATOR -- LongitudinalView assembled")
    print(f"  patients          : {len(views)}")
    print(f"  with TL;DR        : {n_tldr}")
    print(f"  with prior adm    : {n_prior}")
    print(f"  hot paragraphs    : {n_hot} (total)")
    print(f"  ledger facts      : {n_led} (total)")
    print(f"  views -> {VIEW_JSONL}")
    if args.write_md:
        print(f"  rendered md -> {VIEWS_MD_DIR}")
    print("\nNEXT: D3 -- persist tiers to Postgres tier tables.")


if __name__ == "__main__":
    main()
