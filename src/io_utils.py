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
io_utils.py
===========
Shared JSONL helpers :

  read_jsonl(path)                 tolerant line reader: [] if the file is missing;
                                   malformed lines are skipped with a warning,
                                   never crash a stage.
  write_jsonl(path, rows)          atomic full rewrite (tmp + os.replace).
  merge_jsonl(path, rows, key_fn)  the --subject merge-in-place pattern: read the
                                   existing file, drop every row whose key matches
                                   a new row's key, append the new rows, sort by
                                   key, rewrite atomically.

key_fn maps a plain dict row -> a sortable key (int subject_id, or a tuple like
(subject_id, hadm_id)). Rows whose key cannot be extracted are PRESERVED (written
first, original order) - a malformed line is never silently dropped by a merge.
Callers pass plain dicts (json.loads(model.model_dump_json()) for pydantic).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, Iterable

logger = logging.getLogger(__name__)


def read_jsonl(path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("skipping malformed JSONL line in %s", path.name)
    return out


def write_jsonl(path, rows: Iterable[dict]) -> None:
    """Atomic full rewrite: write to a sibling tmp file, then replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    tmp.replace(path)


def merge_jsonl(path, new_rows: list[dict], key_fn: Callable[[dict], object]) -> int:
    """Replace-in-place merge (the fold-in --subject pattern). Returns the total
    number of rows written. Keys must be homogeneous within one file (all ints,
    or all like-shaped tuples) so the sort is well defined."""
    def _key(r: dict):
        try:
            return key_fn(r)
        except Exception:
            return None

    new_keys = {k for k in (_key(r) for r in new_rows) if k is not None}
    rows = [r for r in read_jsonl(path) if _key(r) not in new_keys]
    rows += new_rows
    unkeyed = [r for r in rows if _key(r) is None]
    keyed = sorted(((_key(r), r) for r in rows if _key(r) is not None), key=lambda kr: kr[0])
    write_jsonl(path, unkeyed + [r for _, r in keyed])
    return len(rows)
