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
ollama_utils.py
===============
Minimal GPU / Ollama lifecycle helpers, vendored into the self-contained repo so
the pipeline has no reach-back into an external source tree.

Lifecycle (matching the call sites in faithfulness_validator.py and the stages):
  gpu_cleanup()        -- force Python GC and free the CUDA allocator caches.
  ollama_unload(model) -- ask Ollama to evict a resident model (keep_alive=0),
                          then run gpu_cleanup(). Honors LONGSUM_SKIP_UNLOAD=1
                          (set per stage by foldin_patient.py when the NEXT stage
                          uses the same model): the eviction is skipped so the
                          model stays resident across the subprocess boundary;
                          gpu_cleanup still runs. Safe - if a later stage needs a
                          different model, Ollama evicts on demand.

Shared chat resilience (one implementation of the retry cascades formerly
copy-pasted across b1/c4/d1/d2/e1):
  chat_text(...)       -- plain-prose call; empty output (thinking consumed the
                          num_predict budget) -> one automatic think=False retry.
  chat_json(...)       -- JSON call; cascade configured-think -> no-think ->
                          no-think + explicit JSON-only nudge, with loose parsing.
  parse_json_loose(..) -- fence-stripping, brace-scanning tolerant JSON parse.

All are best-effort where cleanup is concerned and never raise on unload: a
cleanup failure must not abort a batch. torch is optional. 
On a CPU-only box the CUDA branch is simply skipped.
"""

from __future__ import annotations

import gc
import json
import logging
import os
import re

import ollama

try:
    import torch
    _TORCH = True
except ImportError:                # CPU-only environment
    _TORCH = False

logger = logging.getLogger(__name__)


def gpu_cleanup() -> None:
    """Force GC and free CUDA allocator caches so the next model has room."""
    gc.collect()
    gc.collect()
    if _TORCH and torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.ipc_collect()
        torch.cuda.synchronize()
        free, total = torch.cuda.mem_get_info()
        logger.info("VRAM after cleanup: %.1f / %.1f GB free",
                    free / 1024 ** 3, total / 1024 ** 3)


def ollama_unload(model: str) -> None:
    """Evict a resident Ollama model (keep_alive=0), then clear VRAM. With
    LONGSUM_SKIP_UNLOAD=1 the eviction is skipped (model stays resident for the
    next same-model stage in a fold-in cascade); VRAM caches are still cleared."""
    if os.environ.get("LONGSUM_SKIP_UNLOAD") == "1":
        logger.info("LONGSUM_SKIP_UNLOAD=1 -- leaving '%s' resident for the next stage.", model)
        gpu_cleanup()
        return
    try:
        ollama.generate(model=model, prompt="", keep_alive=0)
        logger.info("Ollama model '%s' unloaded.", model)
    except Exception as exc:        # best-effort: never abort a batch on unload
        logger.warning("ollama_unload failed: %s", exc)
    gpu_cleanup()


# ── shared chat resilience ─────────────────────────────────────────────────────────

def chat_text(model: str, messages: list[dict], think: bool = False,
              options: dict | None = None, keep_alive=-1) -> str:
    """Plain-prose chat. A thinking model can spend the whole generation budget on
    reasoning and return EMPTY content; retry once with think=False so the budget
    goes to the answer. Returns the (possibly still empty) stripped content --
    the caller decides how to flag a persistent empty."""
    resp = ollama.chat(model=model, messages=messages, think=think,
                       options=options or {}, keep_alive=keep_alive)
    text = (resp.message.content or "").strip()
    if not text and think:
        logger.warning("'%s': empty output (thinking consumed the budget); "
                       "retrying with think=False", model)
        resp = ollama.chat(model=model, messages=messages, think=False,
                           options=options or {}, keep_alive=keep_alive)
        text = (resp.message.content or "").strip()
    return text


def parse_json_loose(text: str) -> dict:
    """Tolerant JSON parse: strip markdown fences, then plain loads, then the
    outermost {...} span. Raises ValueError if nothing parses."""
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Could not parse JSON:\n{text[:400]}")


def chat_json(model: str, messages: list[dict], think: bool = False,
              options: dict | None = None, keep_alive=-1, label: str = "") -> dict:
    """JSON chat with the full recovery cascade: configured-think -> no-think ->
    no-think + explicit JSON-only instruction (format='json' throughout). Raises
    ValueError only if NO attempt produced parseable JSON."""
    attempts = [
        (think, messages),
        (False, messages),
        (False, messages + [{"role": "user",
                             "content": "Output ONLY the JSON object. "
                                        "No reasoning, no markdown, no prose."}]),
    ]
    lbl = label or model
    last = "unknown"
    for i, (tk, msgs) in enumerate(attempts, 1):
        try:
            resp = ollama.chat(model=model, messages=msgs, think=tk, format="json",
                               options=options or {}, keep_alive=keep_alive)
            content = (resp.message.content or "").strip()
            if not content:
                last = "empty content (thinking consumed the budget)"
                logger.warning("%s: %s (think=%s, attempt %d) -- retrying no-think",
                               lbl, last, tk, i)
                continue
            return parse_json_loose(content)
        except ValueError as exc:
            last = str(exc).splitlines()[0]
            logger.warning("%s: parse failed (think=%s, attempt %d) -- retrying", lbl, tk, i)
        except Exception as exc:
            last = str(exc)
            logger.warning("%s: chat error (think=%s, attempt %d): %s -- retrying",
                           lbl, tk, i, exc)
    raise ValueError(f"{lbl}: no parseable JSON after {len(attempts)} attempts ({last})")
