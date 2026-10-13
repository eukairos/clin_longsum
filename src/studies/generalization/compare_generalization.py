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
compare_generalization.py  --  EVAL S11 redux, item 5: dev-vs-unseen gap report
===============================================================================
After the frozen pipeline + f1/f4/f5 have run over BOTH the dev cohort (data/) and
the unseen generalisation cohort (data_gen/), this reads the two eval namespaces and
reports the generalisation gap per metric. A large gap (dev much better than unseen)
is overfitting to the dev cohort; a small gap is external validity.

Read-only. no DB, no LLM.

    cd ~/clin_longsum/src
    python studies/generalization/compare_generalization.py
    python studies/generalization/compare_generalization.py --gen-namespace gen
    python studies/generalization/compare_generalization.py --dev-namespace "" --gen-namespace gen
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config

# Outputs land in the UNSEEN cohort's data tree, not next to this script. The gap report
# describes the gen cohort, and the repo is not a DUA-compliant location for cohort-derived
# output even when it is aggregate-only. Resolved from --gen-namespace, NOT from the ambient
# LONGSUM_NAMESPACE, so the destination is the same whichever namespace the shell is set to.
def _out_dir(gen_ns: str) -> Path:
    return config.data_dir(gen_ns) / "output" / "studies" / "generalization"

# (label, file, json key, "higher_is_better")
METRICS = [
    ("swr_lenient", "salience_recall.json", "swr_lenient_weighted", True),
    ("swr_strict", "salience_recall.json", "swr_strict_weighted", True),
    ("faithful_fraction", "faithfulness.json", "faithful_fraction", True),
    ("pdsqi_overall", "pdsqi10.json", "overall", True),     # best-effort; skipped if absent
]


def _load(eval_dir: Path, fname: str) -> dict:
    p = eval_dir / fname
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _get(d: dict, key: str):
    if key in d:
        return d[key]
    # shallow search one level down (e.g. nested aggregates)
    for v in d.values():
        if isinstance(v, dict) and key in v:
            return v[key]
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Dev-vs-unseen generalisation gap.")
    ap.add_argument("--dev-namespace", default="", help="dev data dir suffix (default '' -> data/)")
    ap.add_argument("--gen-namespace", default="gen", help="unseen data dir suffix (default 'gen' -> data_gen/)")
    args = ap.parse_args()

    def _eval_dir(ns: str, root=None) -> Path:
        return config.data_dir(ns, root) / "output" / "eval"

    dev_dir = _eval_dir(args.dev_namespace, config.DEV_DATA_ROOT)
    gen_dir = _eval_dir(args.gen_namespace)

    if not dev_dir.exists():
        raise SystemExit(f"dev eval dir not found: {dev_dir}")
    if not gen_dir.exists():
        raise SystemExit(f"unseen eval dir not found: {gen_dir} "
                         f"-- run the frozen pipeline + f1/f4/f5 under LONGSUM_NAMESPACE={args.gen_namespace}")

    rows = []
    for label, fname, key, hib in METRICS:
        dev_v = _get(_load(dev_dir, fname), key)
        gen_v = _get(_load(gen_dir, fname), key)
        if dev_v is None or gen_v is None:
            rows.append({"metric": label, "dev": dev_v, "unseen": gen_v,
                         "gap": None, "note": "missing in one namespace -- skipped"})
            continue
        gap = round(dev_v - gen_v, 4) if hib else round(gen_v - dev_v, 4)
        rows.append({"metric": label, "dev": round(float(dev_v), 4),
                     "unseen": round(float(gen_v), 4), "gap": gap,
                     "note": "dev advantage (positive = possible overfitting)"})

    report = {"study": "generalization_dev_vs_unseen",
              "dev_eval_dir": str(dev_dir), "unseen_eval_dir": str(gen_dir),
              "metrics": rows}
    out_dir = _out_dir(args.gen_namespace)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_json = out_dir / "generalization_gap.json"
    out_md = out_dir / "FINDINGS.md"
    out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")

    lines = ["# Generalisation -- dev vs unseen\n",
             f"dev: `{dev_dir}`  |  unseen: `{gen_dir}`\n",
             "| metric | dev | unseen | gap (dev - unseen) |",
             "|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['metric']} | {r['dev']} | {r['unseen']} | {r['gap']} |")
    lines += ["\n## Interpretation (complete after the run)\n",
              "_Small gap -> external validity (the pipeline holds on unseen patients). "
              "Large positive gap -> overfitting to the dev cohort._\n"]
    out_md.write_text("\n".join(lines), encoding="utf-8")

    print("\n" + "=" * 60)
    print("GENERALISATION GAP (dev vs unseen)")
    for r in rows:
        print(f"  {r['metric']:18s}: dev={r['dev']}  unseen={r['unseen']}  gap={r['gap']}")
    print(f"  json -> {out_json}")
    print(f"  md   -> {out_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
