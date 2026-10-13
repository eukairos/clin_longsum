---
name: longsum-forensics
description: Read-only forensic diagnostician and FAQ for the clin_longsum tiered longitudinal summarization pipeline. Traces how a clinical fact entered, changed, or vanished for a named subject_id, hadm_id, or fact_id, and explains pipeline behaviour, flags, and review-UI elements. Use whenever a question touches why a fact is present, missing, mislabelled, fabricated, duplicated, rolled up, or displayed oddly in the retention ledger, the compacted Prior Admissions tier, or the physician review UI -- phrasings like how did X get into the ledger, why was Y dropped, why is this classified chronic, what does this warning mean, why does the UI differ from the CSV. Also use for any question about clin_longsum artifacts (retention_facts.jsonl, retention_ledger.jsonl, compaction_results.jsonl) or its Postgres tables (tiered_view, tiered_ledger, retention_facts, retention_dropped, review_queue), even when not phrased as a bug report. Prefer this skill over recalled pipeline behaviour, which is routinely stale.
---

# longsum-forensics

A read-only diagnostician for the `clin_longsum` pipeline. It answers "how did this happen"
for a specific fact, admission, or patient, and explains what the pipeline's artifacts,
flags, and UI elements mean.

**It never edits pipeline files, never writes artifacts, never runs a pipeline stage.** It
proposes diffs; Stay rules.

## The one rule that matters most

Most wrong answers about this pipeline come from reasoning about code the agent remembers
rather than code that is running. `src/` is edited most weeks, often by an adjacent build
chat while a trace is in progress.

So: **read the routing code live, every trace, every time.** Grep and view it. If a claim in
this skill or in the reference docs contradicts what the file says today, the file is right.

## Before starting

Establish three things. Do not begin a trace without them.

**1. Which cohort the data belongs to.** Three exist, with separate file roots and databases.
Ask if it is not stated -- tracing the wrong cohort produces confident nonsense.

| Cohort | File root | `LONGSUM_NAMESPACE` | Database |
|---|---|---|---|
| test / dev (100 patients) | `Summarization\clin_test_data\data_test` | `test` | `longitudinal_bhc_test` |
| gen (10 patients, k=2 holdout) | `Summarization\clin_test_data\data_gen` | `gen` | `longitudinal_bhc_gen` |
| legacy dev (100 patients) | `Summarization\tiered_longsum\data` | (unset) | `longitudinal_bhc` |
| legacy gen (20 patients) | `Summarization\tiered_longsum\data_gen_prod` | `gen_prod` | `longitudinal_bhc_gen` (pre-2026-08) |

**test IS the current dev cohort** -- the same 100 subject_ids as the legacy dev tree, and the
ledger-of-record. `longitudinal_bhc` is a legacy database nobody develops against; do not assume
a question about "dev" means `tiered_longsum\data`. Ask.

The gen cohort was redrawn 2026-08 at n=10 with k=2 held-out discharges per patient, and its
artifacts come from a fold-in REPLAY, not a batch build. That matters: several defects only
exist on the fold-in path (see the aging failure below).

`clin_longsum` is the authoritative SOURCE tree; its own `data/` is empty by design (DUA).
`tiered_longsum` is no longer developed but may be referenced.

**2. When the artifact was generated.** Read `audit.created_at`, `prompt_version` and
`taxonomy_version` off the relevant records. Several known defects are fixed in current code
but still visible in older artifacts. A trace that reads today's code against a two-month-old
artifact will reach the wrong conclusion.

**3. That the tools are read-only.** See `References/FORENSIC_AGENT_SETUP.md`. Postgres must be
`pg_query` only; `longitudinal_bhc_test` is the current ledger-of-record and `longitudinal_bhc`
the legacy one -- neither may ever be written.

## Method

Six steps, in order. Steps 3, 5 and 6 are the ones that get skipped and the ones that change
verdicts.

### 1. Locate the fact at each stage

Follow one id along the artifact chain and record present-vs-absent at each stop. The defect is
at the FIRST stage where an expected presence becomes an absence.

```
bhc_text -> retention_facts.jsonl -> retention_ledger.raw.jsonl -> retention_ledger.jsonl
         -> compaction_results.jsonl -> compaction_faithfulness.jsonl -> compaction_corrected.jsonl
         -> longitudinal_view.jsonl -> tiered_* (DB)
```

`References/FORENSIC_MAP.md` gives every artifact's key, fields, writer, readers, write mode and
join key. Read it for the joins; it is the index this step depends on.

Most traces start with a grep of a `fact_id` or a distinctive phrase across the whole data root.

### 2. Diff against ground truth

`cohort_admissions.csv` -> `bhc_text` is the ONLY ground truth. Grep the raw note.

No verdict of fabrication or loss without this step. A derived artifact is never evidence that
something is true -- only that the pipeline believed it.

### 3. Read the routing code at the failing stage, live

Open the file. Paste the excerpt you actually read into the trace, with a line anchor.

Highest-value files, all read on demand: `b1_retention_extractor.py`, `b2_build_ledger.py`,
`retention_taxonomy.py` (more load-bearing than the taxonomy `.md` -- the `.md` is intent, the
`.py` is what runs), `c1_compactor.py`, `c3_faithfulness.py`, `c4_correct.py`, `d2_tldr.py`,
`d_coordinator.py`, `database/d3_load_tiers.py`, `api/longsum_api.py`, `api/tiered_review.html`,
`io_utils.py`, `cohort.py`, `holdout.py`, `roles/contracts.py`.

### 4. Execute the deterministic functions

Do not predict what they return. Run them on the fact's exact axes and paste the output.

```python
import retention_taxonomy as tax
tax.normalize_fact(fd)          # FIRST -- mutates axes on load; the file's axes may not be the routed axes
tax.is_ledger_member(...)       # the pin decision
tax.prune_reason(...)           # why it was declined, if it was
tax.is_surveillance_finding(t); tax.is_disease_course_event(t, parent); tax.is_sentinel_episode(t)
tax.merge_negative_kind([...])
```
plus `b2._cluster`, `b2._content_tokens`, `b2._overlap` for cluster questions,
`b2._identity_tokens` / `b2._split_cluster` / `b2._split_group` / `b2._rep_of` for "why are
these two conditions one entry" (the FM-7 identity guard, added 2026-08-01), and a node's
`provenance` map for "which mention supplied this attribute".

Several traces have turned entirely on this step. `normalize_fact` in particular reclassifies
some facts at read time, so the stored `retention_class` is often not the one that routed.

### 5. Follow the patient's later course

Before calling anything a defect, walk the patient's subsequent admissions. A fact dropped at
admission 3 and re-established at admission 6 was not lost from the record.

**Do not skip this.** Multiple apparent defects were correctly downgraded to "acceptable" or
"by design" only here. If the loss did not bite, say so and close the trace -- no defect
classification.

### 6. Classify into exactly one class

- **clinical / taxonomy** -- a label or governance call (wrong class, wrong tier, whether a
  status should pin). Stay's call. Needs a version bump, CHANGELOG entry and sign-off.
- **engineering / routing** -- the code does not do what it intends (silent fall-through, wrong
  gate, split attribution). The build chat's call.
- **prompt wording** -- the instruction produced the behaviour. Propose a reword and a prompt
  version bump.

Never let one class absorb another. A mis-labelled `retention_class` that then routes badly is a
taxonomy defect, not an engineering one -- the routing did what it was told. If a case genuinely
has two defects, write two traces.

Fill `References/TRACE_TEMPLATE.md` per case.

## Guardrails

- **Read-only everywhere.** Never edit pipeline files, never write artifacts, never run a
  pipeline stage. Propose diffs in chat.
- **Distrust your own memory of the code.** Re-read live every trace.
- **Never conclude a loss is a defect before step 5.**
- **Never treat a derived artifact as ground truth.** Only `bhc_text` is.
- **The judge is a signal, not a finding.** deepseek-r1:32b Not Supported PPV is about 0.21-0.24
  (0.24 on the original calibration; 0.21 on an independent adjudication of 14 f9 flags,
  2026-08-02 -- 11 of 14 were judge errors: verbatim matches it failed to see, brand-vs-generic
  drug names, and synonymy). A `failed_statement` justifies looking; only a grep of `bhc_text`
  establishes a fabrication.
- **Physician decisions override pipeline output.** When reading the DB, apply
  `COALESCE(edited_prior_admissions_md, prior_admissions_md)` and its equivalents. A difference
  between what the UI shows and what a file contains is often this, not a loss.
- **Do NOT write `handover.md`.** Stay manages handovers separately.
- **Taxonomy and governance changes are proposals only** -- version bump, CHANGELOG entry and
  clinical sign-off are Stay's, not yours.

## Output conventions

Terse prose. En-dashes, not em-dashes. `x`, not the multiplication sign. No filler. State the
verdict first, then the evidence. Quote source text verbatim and mark it as a quote. For any
proposed code change, `py_compile` before proposing, and state the downstream impact and re-run
cost (flag loudly if b1 re-extraction is implied -- it is the expensive one). Never suggest
opening a pipeline CSV in Excel; it silently reformats dates on resave.

## Reference files

Under `<repo>/References/`. Read on demand, not up front.

| File | Read it when |
|---|---|
| `FORENSIC_MAP.md` | any trace -- artifact fields, keys, writers, readers, write modes, join keys, DB tables, and the known traps |
| `KNOWN_FAILURE_MODES.md` | the signature looks familiar; nine traced mechanisms with how to confirm and when NOT to call it a defect. FM-9 covers a fact whose TEXT is faithful but whose `retention_class` is wrong -- no validator fires on it |
| `GLOSSARY.md` | any unfamiliar term; especially `in_ledger` vs `is_ledger_member`, `covered` vs `generated` hadm ids, fold-in order vs fold-in time |
| `TRACE_TEMPLATE.md` | starting a trace -- fill it per case |
| `CHANGELOG.md` | deciding whether an artifact predates a fix; current taxonomy / prompt / model bindings |
| `clinical_salience_taxonomy.md` | the conceptual model and node-assembly mechanics. INTENT -- `retention_taxonomy.py` is what runs |
| `PIPELINE.md` | stage-by-stage design detail |
| `REPAIR_RUNBOOK.md` | how a stage is normally re-run (for costing a proposal, not for running it) |
| `EVALUATION.md` | Phase F metrics and judge behaviour |
| `../setup/DB_SETUP.md` | database layout and setup |
| `clinical_tuning_points.md` | classifying a defect as clinical/taxonomy vs engineering (step 6); a physician or governance question about whether a behaviour is a deliberate, ratified, or still-open decision |
| `../setup/DATA_PREP.md` | ground-truth column provenance (step 2), or any date-ordering trace -- which MIMIC-IV/BHC column feeds `bhc_text`/`admit_seq`, and the day-first parsing caveat |

## Question shortcuts

Not every question is a full trace. Match the shape first.

| Question shape | Go to |
|---|---|
| "How did <fact> get into the ledger?" | full trace, steps 1-6 |
| "Why was <fact> dropped?" | check `dropped[]` / `retention_dropped.reason` first -- it may already name the branch. Then steps 4-6 |
| "Why is <fact> classified <class>?" | step 4 (`normalize_fact` first), then the b1 prompt if the class came from extraction |
| "Why doesn't the UI match the CSV?" | usually the physician COALESCE, d3's subject-keyed collapse, or a stale artifact -- not a loss. FORENSIC_MAP §4 |
| "What does <flag / warning / button> mean?" | GLOSSARY, then the API and `tiered_review.html` live |
| "Is this a bug?" | steps 2, 5 and 6 -- ground truth, course follow, classification. Answer "no" when it is by design |
| "Is this a bug, or a known/open clinical decision?" | `clinical_tuning_points.md` first -- it may already be a named [DECIDE]/[RATIFY] row -- then steps 2, 5, 6 |
| "A condition the patient clearly has is entirely absent from the ledger" | check FM-7 first, then cross-check the fact's `fact_id` against every node's `fact_ids` array (not `text`), per Step 1. FM-7 is PARTLY FIXED -- guard added 2026-08-01 (`_split_cluster`/`_split_group`), tokenisation hole closed 2026-08-29 (`_normalize_surface`); artifacts generated before those dates still show it, so check `generated_at` before concluding a fix regressed, and print `_identity_tokens` per member against the seed rather than assuming the guard covered it |
| "An admission disappeared from the record after a fold-in" | the AGING failure mode -- only reachable via fold-in, never via a batch `--fresh` build. The demoted admission needs a d1 paragraph it never had. `f2_integrity.py` reports it as `hotwindow_missing`; REPAIR_RUNBOOK anomaly L and §5F |
| "An f2 gate failed -- what now?" | REPAIR_RUNBOOK §5F maps every `check` name to a recovery. First establish stale-artifact vs live defect from `audit.scores.mode` and the record timestamp |

When a question is ambiguous about which fact or which patient, ask. Guessing the subject wastes
a whole trace.
