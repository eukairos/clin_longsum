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
config.py
=========
Central configuration for the clin_longsum pipeline (tiered longitudinal
summary with compaction).

THIS IS THE ONLY FILE YOU EDIT TO SWAP MODELS, CHANGE PATHS, OR RETUNE THE
WINDOW. Pipeline logic reads everything from here.

Design principle :
  "the model is a config parameter." Every role below is bound to a ModelSpec.
  Swapping a model - local Ollama today, an HCC REST endpoint tomorrow - is a
  change to this file only. Each swap is captured in every AuditRecord the role
  emits (see roles/contracts.py), so the provenance of any generated text is
  always recoverable.

Scope note :
  The pipeline does NOT generate structured data "Known Conditions", "Imaging", or
  "Major Procedures". Here we produce only the narrative text and the retention ledger.

Narrative component is TWO artifacts :
  - hot-window paragraph list: the last N_HOT_DISPLAY cached per-admission
    paragraphs, retrieved.
  - hot-window TL;DR: re-synthesized over the Prior Admissions paragraph PLUS
    all hot-window paragraphs.
  Roles: episode_paragrapher (one per admission) and tldr_synthesizer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ── PATHS ──────────────────────────────────────────────────────────────────────

# Repo root = parent of src/ (this file lives in src/). Fully relocatable: the
# repo can live anywhere and no absolute paths are baked in.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Data root. Defaults to the repo root (data/ inside the repo, as before). Override with
# LONGSUM_DATA_ROOT to put ALL file artifacts outside the tree - keeps the repo
# data-free/git-ready and lets a test cohort run against the real code in place.
DATA_ROOT = Path(os.environ.get("LONGSUM_DATA_ROOT", str(PROJECT_ROOT))).resolve()
# Dev-cohort root. Studies that must reach the DEV cohort while running under a
# different DATA_ROOT/namespace - draw_cohort_gen's dev-subject_id exclusion,
# compare_generalization's dev-vs-unseen diff - resolve it from HERE, not from
# DATA_ROOT (which points at the cohort currently running). Override with
# LONGSUM_DEV_DATA_ROOT; defaults to DATA_ROOT, so behaviour is unchanged when
# the dev cohort IS the running one.
DEV_DATA_ROOT = Path(os.environ.get("LONGSUM_DEV_DATA_ROOT", str(DATA_ROOT))).resolve()
SRC_DIR = PROJECT_ROOT / "src"
#SUBPROJECT_DIR = PROJECT_ROOT         # back-compat alias: the root that CONTAINS data/ 
SUBPROJECT_DIR = DATA_ROOT           # root that CONTAINS data/ 

# Vendored helpers (self-contained; no reach-back to an external source tree). Kept
# under the historical name SYNTH_DIR so existing `config.SYNTH_DIR` call sites are
# unchanged (b1 text_cleaner; c3/c4 faithfulness_validator).
VENDOR_DIR = SRC_DIR / "vendor"      # text_cleaner, ollama_utils, faithfulness_validator
SYNTH_DIR = VENDOR_DIR
LONGSUM_DIR = SRC_DIR / "api"        # FastAPI service + review UI

# Primary data source. Field `target` holds the BHC text (one row per admission).
# DUA: patient-derived; NEVER committed (see .gitignore). Override with the
# LONGSUM_SOURCE_CSV env var, else defaults to data/input/bhc_source.csv under the repo.
SOURCE_CSV = Path(os.environ.get("LONGSUM_SOURCE_CSV",
                                 str(DATA_ROOT / "data" / "input" / "bhc_source.csv")))
SOURCE_TEXT_FIELD = "target"

# Curated abbreviation dictionary (UMLS-built; carries NO patient data, so it ships
# with the repo). Used by b2 for abbreviation expansion during clustering.
ABBREV_TSV = SRC_DIR / "resources" / "dictionary_abbreviations.tsv"

# Optional RUN NAMESPACE (opt-in via the LONGSUM_NAMESPACE env var). When set (e.g.
# "gen"), ALL file artifacts live under data_<ns>/ instead of data/, so a parallel
# cohort, e.g. the EVAL S11 generalisation run on unseen patients, never clobbers
# the dev artifacts. UNSET (the default) is byte-identical to before. This namespaces
# FILES ONLY: the namespace does NOT isolate Postgres. Either run only file-only stages
# under it, or pair it with a dedicated PGDATABASE (as the test cohort does) - otherwise
# dev and namespaced rows mix in the same tables.
RUN_NAMESPACE = os.environ.get("LONGSUM_NAMESPACE", "").strip()
_DATA_DIRNAME = f"data_{RUN_NAMESPACE}" if RUN_NAMESPACE else "data"

DATA_DIR = DATA_ROOT / _DATA_DIRNAME
INTERIM_DIR = DATA_DIR / "interim"
OUTPUT_DIR = DATA_DIR / "output"

def data_dir(namespace: str = "", root: Path | None = None) -> Path:
    """Resolve any cohort's data dir: <root>/data[_<namespace>]. namespace '' = dev,
    root None = DATA_ROOT. Lets a study address a cohort other than the running one."""
    base = Path(root) if root is not None else DATA_ROOT
    return base / (f"data_{namespace}" if namespace else "data")

# Per-stage JSONL artifacts (crash-safe, append-only).
RETENTION_FACTS_JSONL = INTERIM_DIR / "retention_facts.jsonl"
EPISODE_PARAGRAPHS_JSONL = INTERIM_DIR / "episode_paragraphs.jsonl"
COMPACTION_JSONL = INTERIM_DIR / "compaction_results.jsonl"
TLDR_JSONL = INTERIM_DIR / "tldr_results.jsonl"
AUDIT_JSONL = OUTPUT_DIR / "generation_audit.jsonl"

# ── TIERING ────────────────────────────────────────────────────────────────────

# Two decoupled knobs (kept equal at 7 for this POC):
#
# N_COMPACT_THRESHOLD - admissions older than the most recent N are folded into
#   the compacted "Prior Admissions" tier. This is the COMPACTION boundary.
#
# N_HOT_DISPLAY - how many cached per-admission paragraphs Tier 2 displays as
#   the hot-window list. This is a DISPLAY choice only.
#
# Decoupled on purpose: you may later want to compact beyond 10 but still show
# only the last 7 paragraphs (display 7, compact-beyond 10), or the reverse.
# Keep them equal unless there is a clinical/UI reason to diverge.
#
# Invariant the assembler must hold: N_HOT_DISPLAY <= N_COMPACT_THRESHOLD.
# If display > threshold, the list would need paragraphs already compacted away.
# coordinator/assemble should assert this at startup.
N_COMPACT_THRESHOLD = 7
N_HOT_DISPLAY = 7

# TL;DR is re-synthesized on EVERY admission (always reflects the latest full
# picture). Cost is one short paragraph per discharge. Set False to regenerate
# only when the Prior Admissions tier changes (an admission ages out).
TLDR_REGENERATE_EVERY_ADMISSION = True

# Compaction strategy for the Prior Admissions tier:
#   "incremental" - fold each newly aged-out admission into the existing
#                   paragraph. Cheap; prose may drift over many cycles.
#   "full"        - recompact the whole prior span from source each time.
#                   Stable and audit-true; more expensive.
# The retention ledger is ALWAYS re-derived by extraction (deterministic),
# independent of this setting, so critical facts cannot drift either way.
COMPACTION_MODE = "incremental"

# ── HOLDOUT = PHYSICIAN REVIEW GATE (keep True in production) ────────────────────
# "Held out" means "not yet approved by a physician". A held-out admission is shown for
# review as the incoming episode and enters the established record only when approved
# (fold-in: holdout.promote). This is the same mechanism in dev and in production:
#   dev/eval    a2_build_cohort / draw_cohort_gen mark the latest k admissions per patient
#               is_holdout=True to SIMULATE incoming discharges;
#   production  src/ingest inserts every new EHR admission with is_holdout=True.
# Set False ONLY for an unreviewed batch run (summarise every admission, no review queue).
# With False: no incoming, empty review queue, no fold-ins, e1/f6/run_ingest refuse to run,
# and each patient's latest admission is never embedded (a4 and e2 read the is_holdout
# COLUMN, not this flag). See src/ingest/README.md.
#
# a2_build_cohort marks the latest admission per patient is_holdout=True (and writes
# cohort_holdout.csv). When HOLDOUT_LAST_ADMISSION is True, the ESTABLISHED record
# (ledger, TL;DR, current episode, hot window) is built over the NON-holdout span
# only; the held-out admission is the "incoming" episode surfaced for physician
# review. Holding out the LATEST admission never touches the compacted Prior
# Admissions tier (compaction targets the OLDEST span) or the cached paragraphs, so
# enabling this only requires rerunning the cheap stages - b2 (ledger), d2 (TL;DR),
# the coordinator, and d3 (reload). b1 extraction and d1 paragraphs need no rerun.
# Read-path helpers live in holdout.py so every stage applies it identically.
HOLDOUT_LAST_ADMISSION = True
HOLDOUT_MIN_ADMISSIONS = 2          # >=1 established + 1 incoming

# ── LEDGER CONSOLIDATION (b4 - LLM-assisted, tier-aware; runs AFTER b2/b3) ──────
# b2 builds the ledger deterministically (LLM-free). b4 is a SEPARATE, isolated LLM
# refinement of the per-patient confirmed nodes, mirroring the salience taxonomy:
#   T3 chronic   -> LLM same-condition state roll-up (merges wording variants b2's
#                   token/phrase rules miss, e.g. 3 'essential hypertension' lines -> 1)
#   T4 recurrent -> LLM groups by condition; deterministic clinician metric line
#   T5 transient -> deterministic recency prune vs the established last_date
# T1/T2 and the flagged/provisional bucket are never touched. b4 backs up b2's raw
# ledger to retention_ledger.raw.jsonl and reconsolidates from it on reruns.
CONSOLIDATE_LEDGER = True
CONSOLIDATE_T3 = True
CONSOLIDATE_T4 = True
CONSOLIDATE_T5_PRUNE_DAYS = 90      # 0 disables T5 pruning

# ── MODEL BINDING ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelSpec:
    """
    A swappable model binding for one role.

    provider:  "ollama" (local, today) | "openai_rest" (HCC national LLM, later).
               The runner picks the transport from this field; role code is
               provider-agnostic.
    model_id:  exact model string ("qwen3.6:35b-a3b", "claude-opus-4-8", ...).
    version:   provider-side version/tag pinned for audit. For Ollama use the
               digest or tag; for a hosted API use the dated model string.
    endpoint:  base URL. Local Ollama default shown; set per machine.
    params:    decoding params recorded verbatim in every AuditRecord.
    think:     pass-through for Ollama reasoning models (deepseek-r1, ornith).
    """
    role: str
    provider: str
    model_id: str
    version: str
    endpoint: str = "http://localhost:11434"
    params: dict = field(default_factory=dict)
    think: bool = True


# 5090 VRAM budget is 31.8 GB; never load two large models at once. Runners must
# call ollama_unload + cleanup_gpu between roles (see vendor/ollama_utils.py).

GENERATION_ENDPOINT = "http://localhost:11434"
VALIDATION_ENDPOINT = "http://localhost:11434"

# Temperatures grounded : extraction T=0.0, narrative synthesis
# T=0.2, validation T=0.0/0.1.
# Clinical NLP literature basis:
# EXTRACT_TEMP = 0.0: extraction is a deterministic lookup; sampling noise directly
#   introduces hallucination risk in structured IE tasks. Aali et al. (JAMIA 2025)
#   used T=0 for structured clinical output. SynthMedic (Grazhdanski et al. JBI 2025)
#   used T=0.3 over raw noisy source notes; bhc_delta is pre-cleaned so T=0 is
#   appropriate and safer here.
# GENERATE_TEMP = 0.2: minimal warmth for fluent narrative synthesis while limiting
#   hallucination. Van Veen et al. (2023, clinical LLM summarization) found T>0.3
#   significantly increased hallucination rate in clinical summaries. SynthMedic
#   explicitly distinguished summarization (T=0.3) from note generation (T=0.7);
#   T=0.2 is a conservative clinical-safety midpoint.
# VALIDATE_TEMP = 0.0: faithfulness verdicts must be reproducible and auditable --
#   the same summary should receive the same verdict across runs. T=0 is standard
#   for LLM-as-judge evaluations and required for clinical safety auditing.

MODELS: dict[str, ModelSpec] = {
    # Tier 1: latest single-episode summary (drafted for physician review).
    "single_summary": ModelSpec(
        role="single_summary",
        provider="ollama",
        model_id="ornith:35b",
        version="ollama:ornith:35b",
        endpoint=GENERATION_ENDPOINT,
        params={"temperature": 0.2, "num_ctx": 32768},
        think=True,
    ),
    # Retention ledger: extract atomic facts + retention class from one BHC.
    # Extraction-style task -> generation model, T=0.0, non-thinking.
    "retention_extractor": ModelSpec(
        role="retention_extractor",
        provider="ollama",
        model_id="ornith:35b",
        version="ollama:ornith:35b",
        endpoint=GENERATION_ENDPOINT,
        params={"temperature": 0.0, "num_ctx": 32768},
        think=True,
    ),
    # Tier 2a: one approved single-episode summary -> one cached longitudinal-
    # grade paragraph. Runs once per admission; result is cached and retrieved thereafter.
    "episode_paragrapher": ModelSpec(
        role="episode_paragrapher",
        provider="ollama",
        model_id="ornith:35b",
        version="ollama:ornith:35b",
        endpoint=GENERATION_ENDPOINT,
        params={"temperature": 0.2, "num_ctx": 32768},
        think=True,
    ),
    # Tier 2b: TL;DR re-synthesized over Prior Admissions paragraph + all hot-
    # window paragraphs. Runs every admission (see TLDR_REGENERATE_EVERY_ADMISSION).
    "tldr_synthesizer": ModelSpec(
        role="tldr_synthesizer",
        provider="ollama",
        model_id="ornith:35b",
        version="ollama:ornith:35b",
        endpoint=GENERATION_ENDPOINT,
        params={"temperature": 0.2, "num_ctx": 32768},
        think=True,
    ),
    # Tier 3: fold aged-out admissions' bhc_delta into the Prior Admissions
    # paragraph. Compression task -> generation model; a cheaper model can be
    # bound here later without touching role code.
    "compactor": ModelSpec(
        role="compactor",
        provider="ollama",
        model_id="ornith:35b",
        version="ollama:ornith:35b",
        endpoint=GENERATION_ENDPOINT,
        params={"temperature": 0.2, "num_ctx": 32768},
        think=True,
    ),
    # Faithfulness validator / Phase F LLM JUDGE (no hallucination).
    #
    # EVALUATION-MODEL:
    #   deepseek-r1:32b is the bound judge: a long-context, NON-generator model
    #   (the generation model cannot judge its own output - self-eval bias). Its
    #   earlier short-context weakness is mitigated two ways: num_ctx is raised to
    #   32768, AND f4/f5 verify each unit against its generator's bounded input
    #   (compositional sourcing), so the judge never needs the whole record at once.
    #   f4/f5 wrap every call in graceful recovery (thinking-budget empty ->
    #   think=False retry; malformed -> reparse; else judge_error, logged). Judge
    #   calibration against an external clinician-labelled benchmark is a separate
    #   subproject; until the judge clears that bar, f4/f5 numbers stay provisional.
    #   Swappable here, no code change.
    "faithfulness_validator": ModelSpec(
        role="faithfulness_validator",
        provider="ollama",
        model_id="deepseek-r1:32b",
        version="ollama:deepseek-r1:32b",
        endpoint=VALIDATION_ENDPOINT,
        params={"temperature": 0.0, "num_ctx": 32768, "num_predict": 16384},
        think=True,
    ),
    # "retention_validator": RETIRED - C2 is fully deterministic (rule-based; its
    # AuditRecord hardcodes provider="deterministic"), so no model is bound. Kept
    # here commented out in case a generative completeness check returns.
    # "retention_validator": ModelSpec(
    #     role="retention_validator",
    #     provider="ollama",
    #     model_id="deepseek-r1:32b",
    #     version="ollama:deepseek-r1:32b",
    #     endpoint=VALIDATION_ENDPOINT,
    #     params={"temperature": 0.1, "num_ctx": 32768},
    #     think=True,
    # ),
}


def model_for(role: str) -> ModelSpec:
    """Return the ModelSpec bound to a role. Raises if the role is unknown."""
    if role not in MODELS:
        raise KeyError(f"No ModelSpec bound for role '{role}'. Known: {sorted(MODELS)}")
    return MODELS[role]


# ── PROMPT REGISTRY VERSIONS ────────────────────────────────────────────────────

# A prompt change must be as auditable as a model change. Each role's prompt
# template carries a version string, recorded in the AuditRecord. Bump the
# string here when a prompt is edited; do not silently rewrite a prompt.
PROMPT_VERSIONS: dict[str, str] = {
    "single_summary": "single_summary.v1",
    "retention_extractor": "retention_extractor.v3",
    "episode_paragrapher": "episode_paragrapher.v1",
    "tldr_synthesizer": "tldr_synthesizer.v1",   
    "compactor": "compactor.v1",
    "faithfulness_validator": "faithfulness_validator.v3",
    # "retention_validator": retired with the ModelSpec above; C2's deterministic
    # audit pins its own prompt_version string ("retention_completeness.v1").
}


def prompt_version(role: str) -> str:
    return PROMPT_VERSIONS.get(role, f"{role}.unversioned")


# ── STARTUP CHECK ────────────────────────────────────────────────────────────────

def check_paths() -> None:
    """Warn on missing source data; ensure output dirs exist; assert invariant."""
    if RUN_NAMESPACE:
        print(f"RUN NAMESPACE ACTIVE: '{RUN_NAMESPACE}' -> {DATA_DIR.name}/ "
              f"| PGDATABASE={os.environ.get('PGDATABASE', '(from .env)')}")
              #f"(FILE artifacts only; Postgres is shared - do not run DB stages here)"
    if not SOURCE_CSV.exists():
        print(f"WARNING: source CSV not found -> {SOURCE_CSV}")
        print("Update SOURCE_CSV in config.py and rerun.")
    else:
        print(f"Config OK. Source: {SOURCE_CSV.name}  (text field: '{SOURCE_TEXT_FIELD}')")
    for d in (INTERIM_DIR, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)
    assert N_HOT_DISPLAY <= N_COMPACT_THRESHOLD, (
        f"N_HOT_DISPLAY ({N_HOT_DISPLAY}) must be <= N_COMPACT_THRESHOLD "
        f"({N_COMPACT_THRESHOLD}); otherwise the display list needs paragraphs "
        f"that have already been compacted away."
    )
    print(f"DATA_ROOT: {DATA_ROOT}")
    print(f"Compact threshold N = {N_COMPACT_THRESHOLD} | hot display N = {N_HOT_DISPLAY}"
          f" | compaction mode = {COMPACTION_MODE}")
    print(f"TL;DR regenerate every admission: {TLDR_REGENERATE_EVERY_ADMISSION}")
    print(f"Roles bound: {', '.join(sorted(MODELS))}")


if __name__ == "__main__":
    check_paths()
