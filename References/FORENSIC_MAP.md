# FORENSIC_MAP.md

Index of every artifact and table the `clin_longsum` pipeline reads or writes: fields, key,
writer stage, reader stages, write mode, and the join key to the next artifact.

This is the map the forensic agent uses for step 1 of the trace method (locate the fact at
each stage). It is an INDEX, not a snapshot of behaviour. Field lists and routing rules can
drift; before concluding anything about a stage, re-read that stage's code live. Use this file
to know WHERE to look, not to decide WHAT the code does.

Ground truth for every fabrication/loss verdict is `cohort_admissions.csv` -> `bhc_text`.
Nothing derived from it is truth.

---

## 0. Where the data lives

Path resolution (`src/config.py`, read it if anything below looks wrong):

```
DATA_ROOT    = $LONGSUM_DATA_ROOT   (default: repo root)
RUN_NAMESPACE= $LONGSUM_NAMESPACE   ("" = dev)
DATA_DIR     = DATA_ROOT / ("data" | "data_<ns>")
INTERIM_DIR  = DATA_DIR / "interim"
OUTPUT_DIR   = DATA_DIR / "output"
```

The namespace isolates FILES ONLY. Postgres is isolated separately, by `PGDATABASE`.

| Cohort | n | File root | Namespace | Database |
|---|---|---|---|---|
| gen | 20 patients | `Summarization\tiered_longsum\data_gen_prod` | `gen_prod` | `longitudinal_bhc_gen` |
| test | 100 patients | `Summarization\clin_test_data\data_test` | `test` | `longitudinal_bhc_test` |

`clin_longsum\data\` is empty by design (DUA: no patient data in the shipped tree). A run
against the shipped code always points `LONGSUM_DATA_ROOT` at one of the roots above.

### Schema and artifact drift 

`longitudinal_bhc_test` is the ONLY database at the current `schema.sql`. Treat it as the
schema reference.

---

## 1. Stage chain

```
a1 profile -> a2 build cohort ─┬─> cohort_admissions.csv  (GROUND TRUTH: bhc_text)
                               ├─> cohort_patients.csv
                               └─> cohort_holdout.csv
                                       |
a4 load+embed ─> patients, admissions (DB, + pgvector embedding)

b1 extract ──> retention_facts.jsonl
b2 project ──> retention_ledger.jsonl                (deterministic, LLM-free)
b3 audit   ──> loss_audit.md                          (read-only digest)
b4 consolidate ─> retention_ledger.jsonl (overwritten) + retention_ledger.raw.jsonl (b2 backup)

c1 compact ──> compaction_results.jsonl
c2 completeness ─> retention_completeness.jsonl + retention_review.csv
c3 faithfulness ─> compaction_faithfulness.jsonl
c4 correct   ──> compaction_corrected.jsonl + compaction_corrections_audit.csv
c5 triage    ──> review_queue (DB) + review_queue.jsonl (cache)

d1 paragraph ─> episode_paragraphs.jsonl
d2 tldr      ─> tldr_results.jsonl
d_coordinator ─> longitudinal_view.jsonl
d3 load tiers ─> tiered_view / tiered_episode / tiered_ledger / tiered_ledger_raw (DB)

e1 incoming  ─> incoming_summary.jsonl
e2 embed incoming ─> admissions.embedding (DB)

all LLM stages append ─> generation_audit.jsonl ─(db_persist_source)─> generation_audit (DB)
foldin_patient.py runs b1(--hadm) -> holdout.promote -> b2 -> b4 -> c1 -> ... -> d3 for ONE
subject, under a session-level Postgres advisory lock (key 7704140419).
```

---

## 2. Write-mode vocabulary

| Mode | Meaning | Forensic consequence |
|---|---|---|
| `full-rewrite` | `write_jsonl` / `to_csv`: atomic tmp + replace | one row per key, always current |
| `merge` | `io_utils.merge_jsonl`: drop matching keys, append new, sort, atomic rewrite | one row per key; fold-in replaces in place |
| `append` | `open("a")` | duplicates accumulate; NOT deduped on disk |
| `resume-append` | `open("w" if --fresh else "a")`, deduped + rewritten on clean finish | duplicates survive an INTERRUPTED run; readers dedupe last-wins |
| `cache-rewrite` | read existing into a dict keyed by id, merge, rewrite `"w"` | idempotent; equivalent to merge |
| `splice` | read all, replace/insert one record, sort, atomic rewrite | the fold-in single-record path |
| `spool` | append-only write-ahead log, rotated into segments downstream | never truncate |

---

## 3. File artifacts

### 3.1 `interim/cohort_admissions.csv` - GROUND TRUTH

| | |
|---|---|
| Key | `hadm_id` (unique); sort key `(subject_id, admit_seq)` |
| Writer | `a2_build_cohort.py`; `studies/generalization/draw_cohort_gen.py` under a namespace |
| Write mode | full-rewrite |
| Loader | `cohort.py` - the single `dayfirst=True` parse point; every stage derives ISO dates from here |
| Readers | b1, b2, b4, c1, c3, d1, d2, d_coordinator, e1, e2, d3, a4, f2, f3, f4, f10, f12, ablation studies |

Columns: `hadm_id, subject_id, admit_seq, admittime, dischtime, service, admission_type,
discharge_location, hospital_expire_flag, is_holdout, bhc_text, bhc_delta,
dedup_jaccard_to_prev`

- `bhc_text` - the raw clinician note. THE only ground truth. Every fabrication verdict must
  be grounded in a grep of this field.
- `bhc_delta` - text new relative to the previous admission (dedup). c1 compacts from
  `bhc_delta`, falling back to `bhc_text`. A fact present only in the deduped-away portion is
  invisible to c1 but visible to b1.
- `is_holdout` - a2 marks the latest admission per patient. Never open this file in Excel; it corrupts date/time columns.

### 3.2 `interim/cohort_patients.csv`

Key `subject_id`. Writer a2 / draw_cohort_gen (full-rewrite). Columns `subject_id,
n_admissions, conditions_json`. Readers a4, draw_cohort_gen.

### 3.3 `interim/cohort_holdout.csv`

Key `hadm_id`. Writer a2 / draw_cohort_gen (full-rewrite). Same columns as
`cohort_admissions.csv`, restricted to held-out rows. Reader
`studies/generalization/replay_production.py`. 

### 3.4 `interim/promoted_admissions.csv` - CACHE, not a record

Key `hadm_id`. Columns `subject_id, hadm_id, promoted_at`.
Writer `holdout.promote()` - writes the `promotions` TABLE first (INSERT ON CONFLICT DO
NOTHING), then appends here. `holdout.project_csv_from_db()` and
`db_persist_promotions.py --rebuild-csv` regenerate it from the table.
Write mode: append (projection). Readers `holdout.established` / `holdout.split`,
`replay_production.py`.
The `promotions` table is source of truth. If the CSV and the table disagree, the table wins.

### 3.5 `interim/retention_facts.jsonl` - b1 output, the most fundamental derived record

| | |
|---|---|
| Key | `(subject_id, hadm_id)`, one row per admission |
| Writer | `b1_retention_extractor.py`; repair path `b1_rerun_hadms.py` |
| Write mode | batch `append`; `--fresh` unlinks first; `--subject` strips that subject's rows then appends; `--subject --hadm` = `splice` (`extract_one_admission`) |
| Readers | b2, b3, c1 (attaches facts per admission), c2 (via c1's loader), f1, f6, `inspect_ledger.py`, `db_persist_source.py` |
| Join out | `facts[].fact_id` -> ledger `nodes[].fact_ids`; `hadm_id` -> `cohort_admissions.hadm_id` |

Row: `subject_id, hadm_id, admit_seq, facts[], audit`

`facts[]` element (`roles/contracts.RetentionFact`):
`text, retention_class, salience_tier, certainty, lifecycle, occurrence, negative_kind,
parent_condition, supersedes, rationale, fact_id, source_hadm_id, admit_seq`

- `fact_id = sha1(f"{source_hadm_id}|{text}")[:16]`. Text-derived: any rewording changes the
  id, so a fact is not traceable by id across a b1 rerun that changed its wording.
- `in_ledger` is NOT stored. It is DERIVED at read time by
  `retention_taxonomy.is_ledger_member(salience_tier, certainty, lifecycle, negative_kind,
  retention_class)`. Execute that function on the fact's exact axes; never predict it.
- b1 extracts the ESTABLISHED span only (`holdout.established`), so a still-held-out admission
  is absent from this file by design, not by loss.
- b1 is patient-sequential with `known_conditions` fed forward from earlier admissions'
  in-ledger T1-T3 facts (`_update_known`, and `_rebuild_known_from_stored` for the splice
  path). A fabrication in admission N therefore propagates into the prompt for N+1.

### 3.6 `output/retention_ledger.jsonl` - SERVED ledger

| | |
|---|---|
| Key | `subject_id`, one row per patient |
| Writers | `b2_build_ledger.py` (merge), then `b4_consolidate_ledger.py` OVERWRITES the same path with the consolidated ledger (merge) |
| Readers | c1, c2, c3, d_coordinator, d3, b3, f1, f2, f6, f9, review tools |
| Join in | `nodes[].fact_ids` -> `retention_facts.facts[].fact_id` |

Row: `subject_id, n_admissions, taxonomy_version, generated_at, nodes[], flagged[],
episodes[], amended[], considered_excluded[], dropped[], consolidated`

`nodes[]` / `flagged[]` element:
`retention_class, salience_tier, text, certainty, current_lifecycle, negative_kind,
onset_date, last_confirmed_date, n_mentions, source_hadm_ids[], fact_ids[]`

`episodes[]` element: `parent_condition, count, first_date, last_date, source_hadm_ids[]`
-- NOTE the element's own `text` is not carried. T4 recurrent findings are rolled up by
`parent_condition`; the finding's wording is consumed as a grouping key and discarded. A fact
that entered `episodes[]` cannot be recovered from this file.

`consolidated: true` means b4 has run and the b2 form is in `retention_ledger.raw.jsonl`.

`dropped[]` is the PRUNE AUDIT TRAIL: one entry per extracted fact that
matched no routing branch, with `fact_id, text, retention_class, salience_tier, certainty,
lifecycle, negative_kind, source_hadm_id, admit_seq, reason`. `reason` comes from
`tax.prune_reason()`. Audit only - never served, never counted in c2 recall. Persisted by d3
to `retention_dropped`.

Routing: `_project_patient` is a four-way chain - `ruled_out` -> `considered_excluded`,
`recurrent_episode` -> `episodes`, `_is_candidate` -> clustered into `nodes`/`flagged`/
`amended`, else -> `dropped`. 

The `amended` branch tests `node.current_lifecycle == "superseded"` DIRECTLY - it does not
consult `is_ledger_member`, so it is a second, independent gate. `amended` is NOT loaded by d3
and IS counted as full retention by c2, so anything routed there leaves no trace in
`tiered_ledger`, `dropped[]`, `considered_excluded` or `retention_review`. Membership in
`amended` is the only signature. Safety classes are exempted from this branch so a
de-labelled allergy is served as an explicit negative; see FM-8. **A safety-class fact
missing from the served ledger with no `dropped[]` row: look in `amended` first.**

### 3.7 `output/retention_ledger.raw.jsonl` - LOSSLESS b2 ledger

Key `subject_id`. Writer b4 (merge; backs up b2's output before consolidating, and
reconsolidates FROM this file on reruns). Same row shape as 3.6 minus `consolidated`.
Readers b3, c2, d3, f1, f9, `trace_retention_gap.py`.
Use this, not the served ledger, when asking whether b2 produced a node at all.

### 3.8 `interim/compaction_results.jsonl` - c1 output (Tier 3)

| | |
|---|---|
| Key | `subject_id` |
| Writer | `c1_compactor.py`. `--subject` = `merge` (fold-in, and it also evicts that subject from `compaction_corrected.jsonl`). Cohort run = `resume-append` (`"w" if --fresh else "a"`), skipping subjects already present - so an interrupted cohort run can leave more than one row per subject; read last-wins. |
| Readers | c2, c3, c4, d2, d_coordinator, f1, f11, f12, `studies/position_bias` |

Row: `subject_id, prior_admissions_md, pinned_prefix_md, prior_generated_md,
generated_hadm_ids[], ledger_facts[], n_admissions_compacted, covered_hadm_ids[], audit`

Two-region model:
- `pinned_prefix_md` - physician-authored block, frozen byte-exact, prepended by the runner
  AFTER compaction. `compact()` never sees or rewrites it.
- `prior_generated_md` - machine continuation. This is the region c3 judges and c4 corrects.
- `prior_admissions_md` - what is SERVED: prefix + marker + continuation (or continuation
  alone when there is no prefix).
- `covered_hadm_ids` - FULL union of admissions represented. `generated_hadm_ids` - the
  continuation's coverage (`covered` minus prefix). These differ only when a prefix exists.
- `seed_source` (`none|generated|edited`) is an INPUT field; it is recorded to
  `audit.scores`, not persisted as a row field.
- Pre-seeding records lack all four two-region fields; readers fall back to
  `prior_admissions_md` + `covered_hadm_ids`.

`ledger_facts[]` is a list of `RetentionFact` objects, not the `nodes[]` shape of 3.6. It is
the SPAN-SCOPED set the model was actually shown (`_ledger_for_span`), and `audit.scores`
carries `ledger_scope_max_seq` / `n_ledger_scoped` / `n_ledger_total` to confirm it. Records
written before this fix use the key `retention_ledger` and hold the patient's WHOLE
current-state ledger, which is NOT what the prompt saw - readers fall back to that key but
should treat it as unscoped.

### 3.9 `interim/retention_completeness.jsonl` - c2 output

Key: `subject_id`, now a TOP-LEVEL field. Rows written before that fix carry it ONLY inside
`audit`, and `merge_jsonl` keys on the top-level value with a fallback to `audit.subject_id`.
Row: `subject_id, complete, score, missing_facts[], audit`.

Writer c2 (merge). Reader `db_persist_retention.py`. `score` = represented / total pinned.
`missing_facts[]` elements are `RetentionFact` objects.

### 3.10 `output/retention_review.csv` - c2 item rows

Writer c2. Columns `issue, subject_id, salience_tier, retention_class, certainty, text,
source_hadm_id`. `issue` is `MISSING` (a pinned fact absent from the ledger) or
`PINNED_IN_ROLLUP` (a sentinel event filed only as a recurrence count). Reader
`db_persist_retention.py` -> `retention_review` table.

### 3.11 `interim/compaction_faithfulness.jsonl` - c3 gate

| | |
|---|---|
| Key | `subject_id` |
| Writer | `c3_faithfulness.py`, `resume-append` (deduped + sorted rewrite on clean finish) |
| Readers | c4, `db_persist_compaction.py`, f10, `c3_nothink_review.py` |

Row: `faithfulness_score, n_total_statements, n_supported, pass, judge_think_mode,
judge_degraded, judge_attempts, failed_statements[], entailment_detail, statement_counts,
flags, validation_metadata, subject_id, covered_hadm_ids[], generated_hadm_ids[]`

- `pass` is an AGGREGATE over `faithfulness_score` vs threshold 0.6. A record can carry
  unsupported statements in `failed_statements` and still have `pass=true`. c4 does NOT gate
  on `pass`: it gates on the PRESENCE of unsupported statements. (Gating on aggregate `pass`
  is superseded behaviour - it is the mechanism behind failure mode 3, and any trace over an
  artifact generated before that fix must assume the old gate.)
- `statement_counts`: `extracted, independent, dependent_total, dependent_relevant,
  final_evaluated`.
- `validation_metadata`: `model, temperature, think_mode, judge_degraded, threshold,
  ctx_step4, num_predict_step4, inactivity_s, wall_clock_s`.
- `flags`: `dosage_review_needed, below_threshold`.
- Judge health: deepseek-r1:32b Not Supported PPV is about 0.24. A `failed_statement` is a
  triage signal, not a finding.

### 3.12 `interim/compaction_corrected.jsonl` - c4 output

Key `subject_id`. Writer c4 (`resume-append`, then a deduped full rewrite). Same row shape as
3.8 (a corrected `CompactorOutput`). Readers d2 and d_coordinator, both of which PREFER this
file over `compaction_results.jsonl` when a subject is present in it; c1 also reads it.
Per-claim audit goes to `output/compaction_corrections_audit.csv`.

### 3.13 `interim/episode_paragraphs.jsonl` - d1 output (Tier 2a)

Key `(subject_id, hadm_id)`. Writer d1, `cache-rewrite`. Readers d2, d_coordinator.
Row: `subject_id, hadm_id, admit_seq, admit_date, service, paragraph_md, audit`.
Cached once per admission and retrieved thereafter - d1 does not re-run on fold-in.

### 3.14 `interim/tldr_results.jsonl` - d2 output (Tier 2b)

Key `subject_id`. Writer d2 (merge). Reader d_coordinator.
Row: `subject_id, tldr_md, n_total_admissions, audit`.
Re-synthesized every admission (`TLDR_REGENERATE_EVERY_ADMISSION=True`) over the prior tier +
hot-window paragraphs. A physician edit to the prior tier therefore leaves the stored TL;DR
describing superseded text - flagged by `tiered_view.tldr_stale`.

### 3.15 `interim/incoming_summary.jsonl` - e1 output (Phase E)

Key `(subject_id, hadm_id)`. Writer e1, `cache-rewrite`. Readers d3, f6.
Row: `subject_id, hadm_id, admit_seq, admit_date, service, bhc_text, summary_md, audit`.
Carries the held-out admission's raw `bhc_text` - f6 uses it as ground truth.
`e1 --preview` is print-only and does NOT persist (formerly `--subject`; renamed to kill the
silent no-persist footgun). Known open bug: `dayfirst=True` in e1's `pd.to_datetime` swaps
`incoming_date` for ISO interim files, cohort-wide.

### 3.16 `output/longitudinal_view.jsonl` - assembled view

Key `subject_id`. Writer `d_coordinator.py` (merge). Readers d3, f2, f4, f5, f11.
Row: `subject_id, n_total_admissions, tldr_md, current_episode_md, hot_window[],
prior_admissions_md, prior_generated_md, ledger_facts[]`
`hot_window[]` elements are `EpisodeParagraph` rows (3.13 shape).
`ledger_facts[]` is a `RetentionFact` list (was `retention_ledger` before the rename).

### 3.17 `output/generation_audit.jsonl` - provenance spool

Append-only. Written by every LLM-invoking stage (b1, b4, c1, c2, c3, c4, d1, d2, e1) plus
error records. Read by `db_persist_source.py`, which dedupes on `audit_sha` and rotates the
spool into monthly segments. c5 derives concern liveness from the LATEST `generation_audit`
row per group - so a concern the pipeline stops reproducing stops being re-offered.

Row (`roles/contracts.AuditRecord`): `role, provider, model_id, model_version, prompt_version,
taxonomy_version, params, subject_id, input_hadm_ids[], output_sha256, scores, status, error,
created_at`. `status` is `ok | error | flagged`.
This is the only record of WHICH prompt and taxonomy version produced a given artifact. The
model's reasoning trace is NOT persisted anywhere - inference cannot be distinguished from
hedging retrospectively.

### 3.18 `output/review_queue.jsonl` - c5 cache

Writer c5, `full-rewrite`. A DB-projected cache of the `review_queue` table, not a record. The
table is authoritative.

### 3.19 `interim/loss_audit.md` - b3 digest

Writer b3 (full-rewrite). Read-only human digest of potential must-retain losses in
`retention_facts.jsonl`, compared against the RAW ledger when the served one is consolidated.
Not consumed by any stage.

---

## 4. Database tables

Reference DB: `longitudinal_bhc_test`. Column lists below are that database as of
2026-07-29; `src/database/schema.sql` is the authoritative definition.

### Base (created by `a4_load_and_embed.py`, before `schema.sql`)

| Table | Key | Columns |
|---|---|---|
| `patients` | `subject_id` | `subject_id, n_admissions, conditions_json jsonb, snomed_total_occurrences, snomed_unique_codes, snomed_dedup_ratio` |
| `admissions` | `hadm_id` | `hadm_id, subject_id, admit_seq, admit_date, discharge_date, service, admission_type, discharge_location, hospital_expire_flag, is_holdout, bhc_text, bhc_delta, dedup_jaccard_to_prev, embedding vector` |

The SNOMED columns on `patients` are residue of the closed SNOMED subproject.
`admissions.admit_date` / `discharge_date` are real `date` columns - not locale strings.
Confirm a date question here before hypothesising a parse bug upstream.

### Served review surface (writer `database/d3_load_tiers.py`)

| Table | Key | Write mode |
|---|---|---|
| `tiered_view` | `subject_id` | UPSERT: refreshes pipeline + `incoming_*` columns, PRESERVES `review_*` and every physician column |
| `tiered_episode` | `(subject_id, hadm_id)` | per-subject reload |
| `tiered_ledger` | `id` serial, indexed on `subject_id` | per-subject reload from `retention_ledger.jsonl` |
| `tiered_ledger_raw` | `id` serial | per-subject reload from `retention_ledger.raw.jsonl` |
| `retention_dropped` | `id` serial, indexed on `subject_id` / `reason` / `fact_id` | per-subject reload from the raw ledger's `dropped[]` |

`tiered_view` (34 cols) - pipeline: `subject_id, n_total_admissions, first_date, last_date,
current_hadm_id, current_seq, current_service, tldr_md, current_episode_md,
prior_admissions_md, prior_generated_md, taxonomy_version, generated_at`;
Phase E incoming: `incoming_hadm_id, incoming_seq, incoming_date, incoming_service,
incoming_bhc_text, incoming_summary_md`;
physician review: `review_status, reviewed_by, reviewed_at, edited_summary_md,
physician_notes, approved_summary_md, summary_version, folded_in_at`;
Phase C5 prior-tier edit: `edited_prior_admissions_md, prior_edited_by, prior_edited_at,
prior_edit_version, prior_pinned, prior_pinned_hadm_ids, tldr_stale`.

Governance: readers must use `COALESCE(edited_prior_admissions_md, prior_admissions_md)`.
d3 must never write the physician columns. A UI/CSV order mismatch is usually this COALESCE,
or `d3`'s subject-keyed collapse, not a pipeline loss.

`tiered_ledger` / `tiered_ledger_raw`: `id, subject_id, retention_class, salience_tier, text,
certainty, current_lifecycle, negative_kind, provisional, onset_date, last_confirmed_date,
n_mentions, source_hadm_ids int4[], fact_ids text[]`. `provisional=true` marks the `flagged[]`
bucket (suspected / patient-reported). `tiered_ledger` is the served set; `tiered_ledger_raw`
is the recoverable pre-b4 set, since b4's LLM grouping is not reproducible.

**`id` IS NOT AN IDENTIFIER FOR ANYTHING OUTSIDE THIS TABLE.** It is `SERIAL`, and d3 reloads
per subject with `DELETE FROM tiered_ledger WHERE subject_id = %s` followed by re-INSERT - so
every row for a patient is destroyed and reissued on EVERY fold-in. The number is stable only
between fold-ins. Anything that stores it (an external record, a UI bookmark, a ticket, a
spreadsheet of rows to re-check) silently comes to point at a different fact, or at nothing, the
next time that patient is folded in. The failure is silent in the worst way: the id still
resolves, to the wrong row.

Use `fact_ids` instead - `sha1(f"{source_hadm_id}|{text}")[:16]`, stable by construction. Note
its own limit (trap 7): it is text-derived, so a b1 rerun that rewords a fact changes the id.
That is a DIFFERENT failure - the anchor stops resolving, rather than resolving to the wrong
thing - and it is the one you want, because it is visible.

`retention_dropped`: `id, subject_id, fact_id, source_hadm_id, admit_seq,
text, retention_class, salience_tier, certainty, lifecycle, negative_kind, reason,
taxonomy_version, loaded_at`. b2's prune audit trail - every extracted fact the routing chain
declined, with the reason from `tax.prune_reason()`. `reason` has no CHECK constraint: the
vocabulary belongs to the taxonomy, which versions independently of the schema. Join
`fact_id` back to `retention_facts` to recover the full fact. Audit only; never served.
The useful query is `SELECT reason, count(*) FROM retention_dropped GROUP BY reason` - an
`unclassified` row means the gate declined for a combination the helper does not name.

### Source of truth (writer `database/db_persist_source.py`)

| Table | Key | Write mode |
|---|---|---|
| `retention_facts` | `id`; natural `(subject_id, hadm_id, fact_id)` | DELETE-then-INSERT per `(subject_id, hadm_id)` |
| `generation_audit` | `audit_sha` UNIQUE | INSERT ON CONFLICT DO NOTHING |

`retention_facts` carries `in_ledger BOOLEAN` - the pin decision, computed at LOAD time by
`tax.is_ledger_member`. It is a snapshot under the taxonomy version in force at load. If the
taxonomy has since changed, the stored boolean and a live recomputation can disagree; the live
computation is what the pipeline acts on.

### Compaction provenance (writer `database/db_persist_compaction.py`)

`compaction_review` (27 cols), key `subject_id`, UPSERT: `covered_hadm_ids[]`, C3 block
(`c3_score, n_total_statements, n_supported, c3_pass, c3_judge_think_mode, c3_judge_degraded,
c3_judge_attempts, c3_error, failed_statements jsonb, entailment_detail jsonb, c3_loaded_at`),
C4 block (`corrected, c4_original_score, c4_corrected_score, c4_pass_after, c4_n_changes,
c4_unresolved, c4_judge_think_mode, c4_judge_degraded, c4_judge_attempts, c4_status,
c4_loaded_at`), physician override (`physician_edited, physician_edited_by,
physician_edited_at`).

`compaction_change`, DELETE-then-INSERT per subject, FK -> `compaction_review` ON DELETE
CASCADE: `id, subject_id, action, claim, correction, model_reason, validator_evidence,
resolved_after, judge_degraded, source_hadm_ids[], edited_by, edited_at`.
`action` is `dropped | corrected | physician_edit`.

### Fold-in and triage

| Table | Key | Writer | Notes |
|---|---|---|---|
| `promotions` | `hadm_id` | `holdout.promote()`; repair via `db_persist_promotions.py` | source of truth for promotions; `promoted_admissions.csv` is its projection |
| `retention_review` | `id`; UNIQUE `(subject_id, issue, COALESCE(source_hadm_id,-1), fact_text)` | `db_persist_retention.py` | UPSERT refreshes recall/counts, PRESERVES `review_status` |
| `review_queue` | `review_id` = sha1(role\|subject\|hadm_ids\|score magnitude) | `c5_review_queue.py` | UPSERT refreshes `last_seen_at`/severity/reason/detail, PRESERVES `review_status`; stale pending rows -> `auto_cleared`, never deleted |
| `review_decisions` | `id` | API submit endpoints | APPEND-ONLY immutable decision history; nothing UPDATEs or DELETEs it |

`review_queue.review_id` is a concern FINGERPRINT, not a row id: the same concern re-derived
on a later fold-in keeps its id and its physician triage.

`db_persist_retention.py` aborts on write if `retention_visibility_gaps()` finds a gap; the API
returns an advisory `visibility_gaps` on read.

---

## 5. Join keys - the trace path

```
bhc_text (cohort_admissions.csv, hadm_id)
   |  grep the raw note - the ONLY ground truth step
   v
retention_facts.jsonl        row key (subject_id, hadm_id); facts[].fact_id
   |  fact_id
   v
retention_ledger.raw.jsonl   nodes[].fact_ids  (did b2 build a node at all?)
   |  b4 grouping (not reproducible; check .raw for the pre-consolidation form)
   v
retention_ledger.jsonl       nodes[] / flagged[] / episodes[] / amended[] / considered_excluded[]
   |  subject_id
   v
compaction_results.jsonl     prior_generated_md | pinned_prefix_md; covered vs generated_hadm_ids
   |  subject_id  (NOTE: compaction_faithfulness.hadm_id is str(subject_id))
   v
compaction_faithfulness.jsonl -> compaction_corrected.jsonl
   |  subject_id
   v
longitudinal_view.jsonl      hot_window[] joins episode_paragraphs by (subject_id, hadm_id)
   |  subject_id
   v
tiered_view / tiered_episode / tiered_ledger   (served; apply the COALESCE)
```

Cross-cutting: `generation_audit` joins on `subject_id` + `input_hadm_ids[]` and pins the
prompt and taxonomy version for every artifact above.

---

## 6. Traps

1. `compaction_faithfulness.jsonl` -> the `hadm_id` key is gone in current rows; in older rows
   it held `str(subject_id)`. Never join on it.
2. `retention_completeness.jsonl` -> `subject_id` is top-level now; older rows have it only
   under `audit`.
3. `in_ledger` is derived, not stored, in the files; the DB column is a load-time snapshot.
4. `ledger_facts` inside `compaction_results.jsonl` and `longitudinal_view.jsonl` is a
   `RetentionFact` list; `retention_ledger.jsonl` `nodes[]` is a different shape. Older
   artifacts call that field `retention_ledger` - same collision, and unscoped.
5. `episodes[]` discards the finding's own text - a T4 rollup loss is unrecoverable from the
   ledger and must be traced back to `retention_facts.jsonl`.
6. Absence from `nodes/flagged/episodes/amended/considered_excluded` is now recorded in
   `dropped[]` with a reason (v2.11). In ledgers built before v2.11 it is still silent.
   PRESENCE in `amended` is equally silent and is NOT covered by `dropped[]` - the fact was
   re-admitted by `_is_candidate`, so no `prune_reason` fires. See FM-8.
   `amended` + `n_mentions == 1` is incoherent by construction (a singleton has no earlier
   mention to supersede) and is the tripwire for a b1 `superseded` mis-tag.
7. `fact_id` is text-derived; it does not survive a rewording across b1 reruns.
8. dev and gen artifacts and databases predate the two-region model and the Phase C5 columns.
   Fields resolve via `.get(...) or <fallback>`, so the old behaviour appears, silently.
9. `c3.pass` is an aggregate; unsupported statements can coexist with `pass=true`. c4 gates on
    the presence of unsupported statements, not on `pass` - but artifacts predating that fix
    shipped through the old aggregate gate.
10. Pipeline ordering is always `admit_seq`, never a parsed date. Fold-in ORDER and fold-in
    TIME are different things: an admission can be compacted after later admissions were
    promoted, which is how future knowledge reaches c1.
11. `promoted_admissions.csv` is a cache; `promotions` is the record.
12. Never open a pipeline CSV in Excel - it silently reformats dates on resave.
13. `tiered_ledger.id` / `tiered_ledger_raw.id` are `SERIAL` and are REISSUED on every fold-in
    (d3 deletes and re-inserts per subject). Never store one, never join to one from outside
    the table, never anchor an external record to one - it will resolve to a different fact
    rather than failing. `fact_ids` is the stable anchor. Same rule for `retention_dropped.id`.
14. Grep `bhc_text` on WORD BOUNDARIES, and read the surrounding clause. A substring search
    over-matches (`adrenal` inside `hypoadrenalism`) and may land inside a negation ("no
    evidence of"), both of which read as SUPPORT and wrongly close a fabrication enquiry.
    Score each admission's note separately - see KNOWN_FAILURE_MODES themes 7 and 8.
15. The ledger is built from PRIOR admissions. A fact documented only in the incoming
    admission is correctly absent from `tiered_ledger` until fold-in; establish which
    admission documents it before treating absence as loss.
