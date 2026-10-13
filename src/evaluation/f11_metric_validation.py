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
f11_metric_validation.py
========================
Phase F META metric - KNOWN-ANSWER PERTURBATION HARNESS (validates the metrics).

Every Tier-1 number rests on the metrics actually DETECTING the failures they
claim to. This harness injects ONE labelled corruption into a SANDBOXED COPY of
the system's own artifacts, runs the real metric against the copy, and asserts the
expected gate/label fires. A MISS is a metric blind spot to fix. It needs no
baseline system, so it is the highest-leverage validation we have - run it on
every release to guard against metric regressions.

Corruptions and expected detectors (briefing EVAL_S11_REDUX_BRIEFING.md S3):
  delete a T1 node                 -> f1 gate_T1 FAIL
  delete a T2 node                 -> f1 gate_T2 FAIL   (anatomy_altering_surgery preferred)
  delete a safety-class node       -> f1 gate_safety FAIL
  collapse a gated node to rollup  -> f1 tier gate FAIL + n_rollup_only up
  drop a negative_kind fact        -> f1 gate_signeg FAIL
  inject a fabricated prose claim  -> f4 labels it not_supported   (--with-f4; GPU)

Targets are LOAD-BEARING: the harness replicates f1's pinned-fact set (S) and c2's
representation-homing IN-PROCESS to find a node that some pinned compacted-span fact
actually homes to, then strips exactly that representation (escalating until the fact
no longer homes to node/excluded) so the known-answer is guaranteed. Confirmation runs
the REAL f1/f4 as a SUBPROCESS (via the --ledger-path/--view-path/--out-dir overrides).
Detection is PER-FACT: the stripped fact must appear as a NEW row in the perturbed run's
salience_recall_misses.csv (present perturbed, absent at baseline), immune to whatever
the cohort-wide aggregate gate was already doing (a pre-existing gate_signeg=False no
longer masks the signeg case). The aggregate gate value is still recorded for context.

NOTHING real is mutated: all perturbed copies live under a sandbox dir; f1/f4 write
their JSON there too. Deterministic except the optional f4 case.

OUTPUT: data/output/eval/ablations/known_answer.json
EXIT CODE: 0 if every applicable corruption was detected, 1 if any was missed
           (a metric blind spot); --soft forces 0.

    cd ~/clin_longsum/src/evaluation
    python f11_metric_validation.py                 # deterministic f1 corruptions only
    python f11_metric_validation.py --with-f4        # + the prose-injection f4 case (GPU)
    python f11_metric_validation.py --json-only      # suppress the text report
    python f11_metric_validation.py --keep-sandbox    # leave the perturbed copies on disk
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import _bootstrap  # noqa: F401  (puts src/ on sys.path)
import config
import b2_build_ledger as b2
import c1_compactor as c1
import c2_retention_validator as c2v
import f1_salience_recall as f1

HERE = Path(__file__).resolve().parent
OUT_DIR = config.OUTPUT_DIR / "eval" / "ablations"
OUT_JSON = OUT_DIR / "known_answer.json"
SANDBOX = OUT_DIR / "known_answer_sandbox"

INJECT_MARKER = ("Additionally, the patient underwent an experimental porcine cardiac "
                 "xenotransplant in 2150 with no complications.")


# ── pinned-fact set S (same dedup as f1/c2) ──────────────────────────────────────

def _sset(covered: list[int], facts_by_hadm: dict) -> list:
    seen, source = set(), []
    for h in covered:
        for f in facts_by_hadm.get(h, []):
            if not f.in_ledger:
                continue
            key = (f.retention_class, f.text.strip().lower())
            if key not in seen:
                seen.add(key)
                source.append(f)
    return source


def _home(fact, led) -> str | None:
    return c2v._where_represented(fact, *c2v._ledger_index(led))


def _node_entry_for(fact, led):
    """(listname, entry) for the nodes/flagged/amended entry `fact` homes to, mirroring
    c2v._where_represented's precedence (fact_id across all lists, then class-gated token
    cluster). (None, None) if it does not home to a node."""
    fid = fact.fact_id or fact.assign_fact_id()
    for ln in ("nodes", "flagged", "amended"):
        for e in led.get(ln, []) or []:
            if fid in (e.get("fact_ids") or []):
                return ln, e
    ft = b2._content_tokens(fact.text)
    for ln in ("nodes", "flagged", "amended"):
        for e in led.get(ln, []) or []:
            if e.get("retention_class") != fact.retention_class:
                continue
            if c2v._clusters(ft, b2._content_tokens(e.get("text", ""))):
                return ln, e
    return None, None


def _strip_representation(led: dict, fact) -> int:
    """Remove node + excluded representations of `fact` until it homes nowhere
    (None). Returns how many representations were stripped. Bounded."""
    removed = 0
    for _ in range(50):
        home = _home(fact, led)
        if home not in ("node", "excluded"):
            return removed
        if home == "node":
            ln, e = _node_entry_for(fact, led)
            if e is not None and ln and e in led.get(ln, []):
                led[ln].remove(e)
                removed += 1
                continue
            return removed                      # cannot localise -- stop
        # excluded
        ft = b2._content_tokens(fact.text)
        ex = led.get("considered_excluded", []) or []
        hit = [t for t in ex if c2v._clusters(ft, b2._content_tokens(t))]
        if not hit:
            return removed
        led["considered_excluded"] = [t for t in ex if t not in hit]
        removed += len(hit)
    return removed


def _collapse_to_rollup(led: dict, fact) -> bool:
    """Strip node representation and add an episode rollup whose parent_condition is the
    fact text, so the fact now homes 'episode' (a gated-tier fact collapsed to a count)."""
    _strip_representation(led, fact)
    led.setdefault("episodes", []).append({"parent_condition": fact.text, "count": 1})
    return _home(fact, led) == "episode"


# ── target discovery ─────────────────────────────────────────────────────────────

def _discover_targets(comp, ledgers, facts_by_hadm):
    """First load-bearing node fact per corruption category: a pinned compacted-span
    fact that homes to a ledger node. Returns {category: (subject_id, fact)}."""
    want = {"delete_T1": None, "delete_T2": None, "delete_safety": None,
            "collapse_rollup": None, "drop_signeg": None}
    for c in comp:
        pid = int(c["subject_id"])
        led = ledgers.get(pid)
        if led is None:
            continue
        for f in _sset([int(h) for h in c.get("covered_hadm_ids", [])], facts_by_hadm):
            if _home(f, led) != "node":
                continue
            tier, cls = f.salience_tier, f.retention_class
            neg = getattr(f, "negative_kind", None)
            if want["delete_T1"] is None and tier == "T1":
                want["delete_T1"] = (pid, f)
            if tier == "T2" and (want["delete_T2"] is None
                                 or (cls == "anatomy_altering_surgery"
                                     and want["delete_T2"][1].retention_class != "anatomy_altering_surgery")):
                want["delete_T2"] = (pid, f)
            if want["delete_safety"] is None and cls in f1.SAFETY_CLASSES:
                want["delete_safety"] = (pid, f)
            if want["collapse_rollup"] is None and tier in f1.GATE_TIERS:
                want["collapse_rollup"] = (pid, f)
            if want["drop_signeg"] is None and neg is not None:
                want["drop_signeg"] = (pid, f)
        if all(v is not None for v in want.values()):
            break
    return want


# ── perturb + run ────────────────────────────────────────────────────────────────

def _write_perturbed_ledger(records: list[dict], pid: int, mutate, dest: Path) -> None:
    out = []
    for rec in records:
        if int(rec["subject_id"]) == pid:
            rec = deepcopy(rec)
            mutate(rec)
        out.append(rec)
    dest.write_text("".join(json.dumps(r) + "\n" for r in out), encoding="utf-8")


def _run_f1(ledger_path: Path, comp_path: Path, out_dir: Path) -> tuple[int, dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "f1_salience_recall.py", "--json-only",
         "--ledger-path", str(ledger_path), "--comp-path", str(comp_path),
         "--out-dir", str(out_dir)],
        cwd=str(HERE), capture_output=True, text=True,
    )
    rep_path = out_dir / "salience_recall.json"
    report = json.loads(rep_path.read_text(encoding="utf-8")) if rep_path.exists() else {}
    return proc.returncode, report


def _run_f4(view_path: Path, comp_path: Path, out_dir: Path, subject: int) -> tuple[int, list]:
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "f4_faithfulness.py", "--fresh", "--units", "synth", "--json-only",
         "--subject", str(subject), "--view-path", str(view_path),
         "--comp-path", str(comp_path), "--out-dir", str(out_dir)],
        cwd=str(HERE), capture_output=True, text=True,
    )
    jsonl = out_dir / "faithfulness.jsonl"
    recs = c1._read_jsonl(jsonl) if jsonl.exists() else []
    return proc.returncode, recs


def _read_misses(out_dir: Path) -> dict:
    """{(subject_id, text.strip()): row} from f1's salience_recall_misses.csv (empty if absent).
    f1 writes the CSV only when there are misses."""
    p = out_dir / "salience_recall_misses.csv"
    if not p.exists():
        return {}
    out = {}
    with p.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                key = (int(row["subject_id"]), (row.get("text") or "").strip())
            except (TypeError, ValueError):
                continue
            out[key] = row
    return out


# ── main ────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Known-answer perturbation harness (metric validation).")
    ap.add_argument("--with-f4", action="store_true",
                    help="also run the prose-injection case through f4 (GPU, deepseek judge)")
    ap.add_argument("--keep-sandbox", action="store_true", help="do not delete the perturbed copies")
    ap.add_argument("--soft", action="store_true", help="always exit 0 (report-only)")
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()
    config.check_paths()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    SANDBOX.mkdir(parents=True, exist_ok=True)

    comp = c1._read_jsonl(config.COMPACTION_JSONL)
    if not comp:
        raise SystemExit(f"{config.COMPACTION_JSONL} not found/empty. Run c1_compactor.py first.")
    real_ledger_src, _ = f1._resolve_ledger_src()
    ledger_records = c1._read_jsonl(real_ledger_src)
    ledgers = {int(l["subject_id"]): l for l in ledger_records}
    facts_by_hadm = c1._facts_by_hadm()

    # copy the unperturbed compaction (read-only isolation) + a clean ledger baseline
    comp_copy = SANDBOX / "compaction_results.jsonl"
    shutil.copy(config.COMPACTION_JSONL, comp_copy)
    clean_ledger = SANDBOX / "ledger_clean.jsonl"
    shutil.copy(real_ledger_src, clean_ledger)

    base_rc, base = _run_f1(clean_ledger, comp_copy, SANDBOX / "baseline")
    base_gates = base.get("gates", {})
    base_rollup = base.get("n_rollup_only", 0)
    base_misses = _read_misses(SANDBOX / "baseline")
    if not args.json_only:
        print(f"baseline gates: {base_gates}  (all_pass={base.get('all_gates_pass')})")

    targets = _discover_targets(comp, ledgers, facts_by_hadm)
    results: list[dict] = []

    def _record(ctype, target, applicable, detected, detail, expected):
        results.append({"corruption": ctype, "expected_detector": expected,
                        "applicable": applicable, "detected": detected,
                        "target_subject": (target[0] if target else None),
                        "target_text": (target[1].text if target else None),
                        "target_tier": (target[1].salience_tier if target else None),
                        "target_class": (target[1].retention_class if target else None),
                        "detail": detail})

    # ── f1 deletion corruptions ───────────────────────────────────────────────────
    f1_cases = [
        ("delete_T1", "delete_T1", "gate_T1"),
        ("delete_T2", "delete_T2", "gate_T2"),
        ("delete_safety", "delete_safety", "gate_safety"),
        ("drop_signeg", "drop_signeg", "gate_signeg"),
    ]
    for ctype, tkey, gate in f1_cases:
        tgt = targets.get(tkey)
        if tgt is None:
            _record(ctype, None, False, None, "no_target_available", f"new miss row (+ {gate})")
            continue
        pid, fact = tgt
        dest = SANDBOX / f"ledger_{ctype}.jsonl"
        _write_perturbed_ledger(ledger_records, pid,
                                lambda led, fc=fact: _strip_representation(led, fc), dest)
        rc, rep = _run_f1(dest, comp_copy, SANDBOX / ctype)
        miss = _read_misses(SANDBOX / ctype)
        key = (pid, fact.text.strip())
        row = miss.get(key, {})
        new_miss = key in miss and key not in base_misses
        signeg_ok = True
        if ctype == "drop_signeg":
            signeg_ok = bool((row.get("negative_kind") or "").strip())
        detected = bool(new_miss and signeg_ok)
        _record(ctype, tgt, True, detected,
                {"per_fact_new_miss": new_miss, "miss_issue": row.get("issue"),
                 "miss_negative_kind": row.get("negative_kind"),
                 "gate_value": rep.get("gates", {}).get(gate), "f1_exit": rc},
                f"new miss row (+ {gate})")

    # ── collapse-to-rollup (gated tier) ───────────────────────────────────────────
    tgt = targets.get("collapse_rollup")
    if tgt is None:
        _record("collapse_rollup", None, False, None, "no_target_available",
                "new ROLLUP_ONLY miss row")
    else:
        pid, fact = tgt
        gate = "gate_T1" if fact.salience_tier == "T1" else "gate_T2"
        ok_box = {}
        def _mut(led, fc=fact, box=ok_box):
            box["ok"] = _collapse_to_rollup(led, fc)
        dest = SANDBOX / "ledger_collapse_rollup.jsonl"
        _write_perturbed_ledger(ledger_records, pid, _mut, dest)
        rc, rep = _run_f1(dest, comp_copy, SANDBOX / "collapse_rollup")
        miss = _read_misses(SANDBOX / "collapse_rollup")
        key = (pid, fact.text.strip())
        row = miss.get(key, {})
        new_rollup = (key in miss and key not in base_misses
                      and row.get("issue") == "ROLLUP_ONLY")
        _record("collapse_rollup", tgt, True, bool(new_rollup),
                {"per_fact_new_rollup_miss": bool(new_rollup), "miss_issue": row.get("issue"),
                 "homed_episode": ok_box.get("ok"), "gate_value": rep.get("gates", {}).get(gate),
                 "f1_exit": rc}, "new ROLLUP_ONLY miss row")

    # ── f4 prose injection (optional, GPU) ────────────────────────────────────────
    if args.with_f4:
        views = c1._read_jsonl(f1.LEDGER_JSONL.parent / "longitudinal_view.jsonl")
        vtgt = next((v for v in views if (v.get("prior_admissions_md") or "").strip()), None)
        if vtgt is None:
            _record("inject_prose", None, False, None, "no_view_with_prior_admissions",
                    "f4 not_supported>=1")
        else:
            pid = int(vtgt["subject_id"])
            vdest = SANDBOX / "view_inject.jsonl"
            out = []
            for v in views:
                if int(v["subject_id"]) == pid:
                    v = deepcopy(v)
                    v["prior_admissions_md"] = (v.get("prior_admissions_md") or "").rstrip() + " " + INJECT_MARKER
                out.append(v)
            vdest.write_text("".join(json.dumps(r) + "\n" for r in out), encoding="utf-8")
            rc, recs = _run_f4(vdest, comp_copy, SANDBOX / "inject_prose", pid)
            unit = next((r for r in recs if r.get("unit_id") == "prior_admissions"
                         and int(r["subject_id"]) == pid), None)
            detected = bool(unit and unit.get("not_supported", 0) >= 1)
            _record("inject_prose", (pid, type("F", (), {"text": INJECT_MARKER,
                    "salience_tier": None, "retention_class": None})()), True, detected,
                    {"f4_exit": rc, "unit_found": unit is not None,
                     "not_supported": (unit or {}).get("not_supported"),
                     "n_claims": (unit or {}).get("n_claims")}, "f4 not_supported>=1")

    applicable = [r for r in results if r["applicable"]]
    n_detected = sum(1 for r in applicable if r["detected"])
    detection_rate = round(n_detected / len(applicable), 4) if applicable else None
    misses = [r["corruption"] for r in applicable if not r["detected"]]
    skipped = [r["corruption"] for r in results if not r["applicable"]]

    report = {
        "metric": "known_answer_metric_validation",
        "n_corruptions": len(results), "n_applicable": len(applicable),
        "n_detected": n_detected, "detection_rate": detection_rate,
        "misses": misses, "skipped_no_target": skipped,
        "with_f4": args.with_f4, "results": results,
        "baseline_all_gates_pass": base.get("all_gates_pass"),
    }
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if not args.keep_sandbox:
        shutil.rmtree(SANDBOX, ignore_errors=True)

    if not args.json_only:
        print("\n" + "=" * 60)
        print("F11 KNOWN-ANSWER METRIC VALIDATION")
        print(f"  corruptions applicable : {len(applicable)} / {len(results)}")
        print(f"  detected               : {n_detected}  (rate {detection_rate})")
        for r in results:
            tag = "n/a " if not r["applicable"] else ("PASS" if r["detected"] else "MISS")
            line = f"    {tag}  {r['corruption']:16s} -> {r['expected_detector']}"
            if r["target_subject"]:
                line += f"  (subj {r['target_subject']})"
            if not r["applicable"]:
                line += f"  [{r['detail']}]"
            print(line)
        if skipped:
            print(f"  skipped (no target)    : {', '.join(skipped)}")
        if misses:
            print(f"\n  {len(misses)} METRIC BLIND SPOT(S): {', '.join(misses)}  -- investigate")
        else:
            print("\n  all applicable corruptions detected")
        print(f"  json -> {OUT_JSON}")

    miss = bool(misses)
    return 0 if (args.soft or not miss) else 1


if __name__ == "__main__":
    sys.exit(main())
