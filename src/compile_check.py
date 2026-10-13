#!/usr/bin/env python3
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
compile_check.py
================
Syntax-check (py_compile) every .py under a tree, with a clean per-file pass/fail
report and a non-zero exit on any failure -- so it drops straight into a pre-push
or CI gate. This is a SYNTAX check only (like `python -m compileall`): it does not
import modules, so a file compiles here even if its third-party deps aren't in the
active env (e.g. fastapi, ollama).

Run it with the interpreter whose syntax you care about (the pipeline env):
    conda activate ollama_HF_py313
    cd ~/clin_longsum/src
    python compile_check.py                 # scans this script's dir (src/)
    python compile_check.py .. --exclude vendor
    python compile_check.py C:/path/to/tree -x vendor -x scratch
    python compile_check.py --quiet         # -q: summary line and failures only

Quick one-liner equivalent (no per-file report, exits non-zero on failure):
    python -m compileall -q src
"""

from __future__ import annotations

import argparse
import py_compile
import sys
from pathlib import Path

# Always skipped (compiled artifacts / VCS dirs); add more via --exclude.
_ALWAYS_SKIP = ("__pycache__", ".git", ".ipynb_checkpoints")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="py_compile every .py under a tree; report failures; exit 1 on any.")
    ap.add_argument("path", nargs="?", default=None,
                    help="root dir to scan (default: the directory this script lives in)")
    ap.add_argument("-x", "--exclude", action="append", default=[],
                    help="path substring to skip (repeatable), e.g. -x vendor -x scratch")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="print only the summary line and any failures")
    args = ap.parse_args()

    root = Path(args.path).resolve() if args.path else Path(__file__).resolve().parent
    if not root.exists():
        print(f"path not found: {root}", file=sys.stderr)
        return 2

    skip = _ALWAYS_SKIP + tuple(args.exclude)
    files = [p for p in sorted(root.rglob("*.py"))
             if not any(s in p.as_posix() for s in skip)]
    if not files:
        print(f"no .py files under {root}", file=sys.stderr)
        return 2

    failed: list[tuple[Path, str]] = []
    for p in files:
        try:
            # doraise -> raise on syntax error instead of printing + returning.
            # cfile=os.devnull would suppress .pyc, but leaving the default keeps the
            # cached bytecode (harmless, gitignored) and matches `python -m py_compile`.
            py_compile.compile(str(p), doraise=True)
            if not args.quiet:
                print(f"  OK    {p.relative_to(root)}")
        except py_compile.PyCompileError as exc:
            # exc.msg carries the traceback text; last line is the concrete error.
            last = (exc.msg or str(exc)).strip().splitlines()[-1]
            failed.append((p, last))
        except SyntaxError as exc:                # belt-and-suspenders
            failed.append((p, f"{exc.__class__.__name__}: {exc}"))

    ok = len(files) - len(failed)
    print(f"\npy_compile: {ok}/{len(files)} OK under {root}")
    if failed:
        print(f"FAILED ({len(failed)}):")
        for p, msg in failed:
            print(f"  {p.relative_to(root)}")
            print(f"      {msg}")
        return 1
    print("all clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
