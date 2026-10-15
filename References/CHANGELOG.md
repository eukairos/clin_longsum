# CHANGELOG.md

Repo-level index of what is CURRENT and what changed, so an agent can orient without
re-reading the whole tree.

This file is an INDEX, not the record. Each versioned object carries its own authoritative
history in code; this file says where that history lives, what the current value is, and what
non-versioned decisions have been taken. Where the two disagree, the code wins.


---

## 1. Where each versioned object lives

| Object | Current | Authoritative source | Recorded in |
|---|---|---|---|
| Clinical salience taxonomy | `retention_taxonomy.v2.12` | `src/retention_taxonomy.py` -- `TAXONOMY_VERSION` + inline `CHANGELOG` list | every `AuditRecord.taxonomy_version`; `retention_ledger.taxonomy_version`; `tiered_view.taxonomy_version` |
| Prompts | see table below | `src/config.py` -- `PROMPT_VERSIONS` | every `AuditRecord.prompt_version` |
| Model bindings | see table below | `src/config.py` -- `MODELS` | every `AuditRecord.model_id` / `model_version` / `params` |
| DB schema | fresh-creation form | `src/database/schema.sql` | not versioned -- compare column sets against the reference DB |
| Data contracts | -- | `src/roles/contracts.py` | -- |
| Conceptual taxonomy doc | companion to v2.12 | `References/clinical_salience_taxonomy.md` | -- |

**The taxonomy is the governance object.** Any edit to it bumps `TAXONOMY_VERSION` and appends
to `CHANGELOG` in the same commit. This is the pattern the other objects follow loosely and the
taxonomy follows strictly.

---

## 2. Current bindings

### Prompts (`config.PROMPT_VERSIONS`)

| Role | Version |
|---|---|
| `single_summary` | `single_summary.v1` |
| `retention_extractor` | `retention_extractor.v3` |
| `episode_paragrapher` | `episode_paragrapher.v1` |
| `tldr_synthesizer` | `tldr_synthesizer.v1` |
| `compactor` | `compactor.v1` |
| `faithfulness_validator` | `faithfulness_validator.v3` |

Two prompt versions are pinned in stage code rather than the registry: c2 emits
`retention_completeness.v1` (deterministic, no model bound), and c4 emits `compactor.v1+correct`.

### Models (`config.MODELS`)

| Role | Model | Temp | num_ctx | think |
|---|---|---|---|---|
| `single_summary` | `ornith:35b` | 0.2 | 32768 | yes |
| `retention_extractor` | `ornith:35b` | 0.0 | 32768 | yes |
| `episode_paragrapher` | `ornith:35b` | 0.2 | 32768 | yes |
| `tldr_synthesizer` | `ornith:35b` | 0.2 | 32768 | yes |
| `compactor` | `ornith:35b` | 0.2 | 32768 | yes |
| `faithfulness_validator` | `deepseek-r1:32b` | 0.0 | 32768 (`num_predict` 16384) | yes |

Embeddings: `Snowflake/snowflake-arctic-embed-l-v2.0` (`EMBED_MODEL` in `.env`).

### Tuning knobs (`config.py`)

`N_COMPACT_THRESHOLD` 7 | `N_HOT_DISPLAY` 7 | `COMPACTION_MODE` incremental |
`TLDR_REGENERATE_EVERY_ADMISSION` True | `HOLDOUT_LAST_ADMISSION` True |
`CONSOLIDATE_LEDGER` True | `CONSOLIDATE_T5_PRUNE_DAYS` 90 |
faithfulness threshold 0.6 (`vendor/faithfulness_validator.FAITHFULNESS_THRESHOLD`).

---

## 3. Taxonomy history (summary - read the .py for the full text)

| Version | Date | What changed |
|---|---|---|
| v1 | 2026-06-02 | Flat retain/compressible. |
| v2 | 2026-06-03 | Three-axis model; ledger membership becomes DERIVED. |
| v2.1 | 2026-06-04 | `significant_negative` pins even when suspected. |
| v2.2 | 2026-06-04 | Re-added `sentinel_event` (T2); extraction rules. |
| v2.3 | 2026-06-04 | Certainty gate made class/lifecycle-aware: `suspected + active` T1-T3 pins PROVISIONAL; `patient_reported` pins for allergy / code-status / infection-control. Added `is_surveillance_finding`. |
| v2.4 | 2026-06-06 | Widened `is_surveillance_finding` recall after a 100-case loss audit. |
| v2.5 | 2026-06-06 | Safety classes robust to mis-tagging: `severe_adr_allergy` and `code_status_directive` pin unless ruled_out/superseded. |
| v2.6 | 2026-06-06 | `is_disease_course_event` -- malignancy recurrence / graft rejection reclassified to `sentinel_event`, not a frequency rollup. |
| v2.7 | 2026-06-06 | Same rule pushed at-source into the extraction prompt. |
| v2.8 | 2026-06-06 | `is_sentinel_episode` -- status epilepticus, gangrene, graft/stent occlusion promoted out of rollup. |
| v2.9 | 2026-06-24 | **BREAKING.** `significant_negative` (bool) -> `negative_kind` (enum). Required full re-extraction. |
| v2.10 | 2026-06-26 | Tier FLOOR (`floor_tier`); suspected `sentinel_event` pins at any lifecycle. Required re-extraction for the stored tier. |
| v2.11 | 2026-07-29 | Audit the prune: `prune_reason()` + b2 `dropped[]`. Additive; no re-extraction. |
| v2.12 | 2026-08-19 | FM-8. Safety classes robust to `lifecycle=superseded`: the safety branch moves ABOVE the superseded guard, `ruled_out` becomes the sole exit from a safety pin. b2 exempts safety classes from the `amended` route so a de-labelled allergy is SERVED as an explicit negative; a superseded latest mention wins `negative_kind` outright. `prune_reason` suspected-branch drift fixed (a `suspected`+`active` T4/T5 decline was reported as `suspected_inactive` when the real reason was tier). f12 now enforces the baseline's `taxonomy_version` stamp. b1 prompt distinguishes record retraction from drug substitution (STAGED, not re-extracted). Re-derives from b2 onward; no re-extraction. |

**Evaluation baseline:** f12's stored gate point (`frontier_baseline.json`) has always carried
a `taxonomy_version` stamp, but nothing read it - a v2.11 point was silently compared against a
v2.10 run for two versions. v2.12 adds the guard: on a version mismatch the dominance gate is
SKIPPED and reported (`baseline_stale` in the JSON, a loud line on stdout), the absolute floor
gate still applies, and a passing run re-baselines automatically. All four stored baselines were
stale when the guard landed (`clin_test_data/data_gen` and `data_test` at v2.11, `data/` and
`tiered_longsum/data/` at v2.10) - they self-heal on the next passing f12 run.

The other f-suite thresholds (f4 0.6/0.9, f5, f6, f9) are hard-coded and marked PROVISIONAL in
their own docstrings; they are not version-stamped and are unaffected by this guard.

---

## 4. Decisions and changes not carried by a version string; newest first

### 2026-08-29 - b2 surface-form normalisation (possessive / `s/p`)

- **`_normalize_surface()` in `b2_build_ledger.py`**, applied by both `_content_tokens` and
  `_phrase`. Strips the possessive suffix (`Crohn's` -> `crohn`, ASCII and U+2019) and drops the
  `s/p` abbreviation before tokenising.
- Cause: `re.findall(r"[A-Za-z0-9]+", ...)` shattered both forms into a bare `s`, which is in
  neither `_STOPWORDS` nor `_QUALIFIERS`. `"Crohn's disease"` -> `{crohn, s, disease}` and
  `"Coronary artery disease s/p LAD stent placement"` -> `{..., s, p, ...}` shared the specific
  token `{s}` at overlap 0.667 >= `CLUSTER_THRESH`, so two unrelated conditions clustered.
  `_split_cluster` could not separate them because `_identity_tokens` carried the same `s`, and
  latest-wins wording then served one condition under an unrelated condition's name. The FM-7
  guard is present and does not catch this -- executed live at v2.12 it returns a single
  partition.
- **Strip, do not fold.** Folding `<Eponym>'s` to `<eponym>s` was tested and REJECTED: in two
  subjects it split the possessive spelling of an eponymous condition from its apostrophe-free
  spelling, which had previously clustered. Adding `s`/`p` to `_STOPWORDS` was also tested and
  rejected - it fragmented an unrelated cluster. Stripping keeps possessive and non-possessive
  spellings of an eponym on one token.
- Measured effect, test cohort, full `_project_patient` path (cluster -> split -> build_node ->
  consolidate -> merge -> prune) run over all 100 subjects before and after: **6835 rows before
  and after, 3489 nodes before and after, one subject changed.** That subject's node for one
  condition sheds the two members belonging to an unrelated condition (15 -> 13 facts) and the
  correct node absorbs them (8 -> 10). No `dropped[]` row moved; no episode rollup changed.
- Incidence: 52 facts carry a possessive and 14 carry `s/p` in the test cohort, but only 2 node
  members were bridged by a single-character token alone - both in the one node above. The
  mechanism is general:any possessive eponym against any `s/p` fact, but the incidence here
  is low.
- `b3_loss_audit` and `c2_retention_validator` import `b2._content_tokens` and will see the same
  normalisation on their next run.
- Found by the class mis-assignment sweep; full trace in
  `src/studies/ledger_studies/taxonomy_self_collision/` (gitignored).

### 2026-08-01 - b2 identity guard (FM-7 fixed)
- **`_split_cluster` / `_split_group` in `b2_build_ledger.py`.** No ledger entry may display a
  wording that names a condition some of its own members do not share. Fixes the compound-fact
  bridge: a fact naming two conditions ("Coronary artery disease and peripheral vascular
  disease") matched both, merged them, and latest-wins wording erased whichever condition the
  newest phrasing omitted. The absorbed condition kept its `fact_id` inside the node, so it was
  never a prune and never appeared in `dropped[]`.
- Scan before the fix: 106 affected nodes across the three cohorts, **compound bridge present in
  106 of 106**. Test cohort: 58 entries / 39 patients with an orphaned member; 16 conditions /
  11 patients with no representation anywhere. Included `Code status: Full` absorbed into a
  DNR/DNI directive (dev cohort), and cirrhosis erased for two test-cohort subjects.
- Measured effect, test cohort: 3777 -> 3814 entries (+1.0%), node text +0.6%, 45 conditions
  recovered, 8 phrasings replaced by a shorter one for the same condition. `fact_ids`,
  `dropped[]` and episode rollups verified byte-identical per patient.
- **`_SPLIT_EXEMPT = {"code_status_directive"}`** - clinical decision. A directive
  legitimately mutates; the most recent one is the patient's wish, so latest-wins collapse is
  correct there. Verified: 133 code-status entries before and after.
- `_merge_group`'s representative rule extracted to **`_rep_of`** so `_split_group` shares it.
  No behaviour change. Reverting the tiebreak to longest-text was tested and REJECTED: it
  rescued 1 orphan and lost 65 canonical labels.
- Residual: not complete cover - in one subject a pancytopenia fact still sits inside the HBV
  cirrhosis node. `fact_ids` stays single-home; multi-home membership was considered and deferred because
  `f9_merge_faithfulness._members_of` infers membership by subset and would mis-attribute.

### 2026-07-29 - forensic register, prune audit, span-scope fix
- `References/FORENSIC_MAP.md`, `KNOWN_FAILURE_MODES.md`, `GLOSSARY.md`, this file: written as
  the knowledge base for the forensic/FAQ agent.
- **b2 `dropped[]`** + `retention_dropped` table (taxonomy v2.11). Prune is audited; routing
  unchanged. ~26 dropped facts per patient on the 100-patient cohort.
- **b2 per-attribute `provenance`** on `LedgerNode` + `provenance JSONB` on `tiered_ledger` /
  `tiered_ledger_raw`. Records which `fact_id` supplied each served attribute. Audit only.
  Measured: 28 of 3777 nodes (0.7%) take tier and text from different facts.
- **b2 `_merge_group` tiebreak changed** from longest-text to MOST RECENT. Behaviour change:
  a consolidated node may now carry different wording than before. Verbosity was standing in
  for fidelity.
- **c1 persists the SPAN-SCOPED ledger** (`ledger_facts`), not the patient's whole ledger.
  Matters because c4 read that field to rebuild the correction context, so correction was
  running against facts documented after the span (FM-3 at the fix step). `audit.scores` gains
  `ledger_scope_max_seq` / `n_ledger_scoped` / `n_ledger_total`.
- **Field rename** `retention_ledger` -> `ledger_facts` on `CompactorInput` / `CompactorOutput` /
  `RetentionCompletenessInput` / `LongitudinalView`. Readers fall back to the old key. The old
  name collided with `retention_ledger.jsonl`, whose `nodes[]` are a different shape.
- **c3 drops the misleading `hadm_id`** field (it held `str(subject_id)`, echoed from the
  vendored validator's trace key) and adds `generated_hadm_ids`.
- **c2 gains a top-level `subject_id`** on `RetentionCompletenessOutput`; it was previously
  reachable only inside `audit`.

### Earlier (undated in-repo; reconstructed from code and handover logs)
- **c4 gate**: keys on the PRESENCE of unsupported statements, not the aggregate `pass`
  boolean. Supersedes aggregate gating, which let a 0.88-scoring paragraph ship a fabrication.
- **Option A two-region compaction**: `pinned_prefix_md` / `prior_generated_md` /
  `generated_hadm_ids` / `seed_source`. Physician edits to the Prior-Admissions tier are frozen
  byte-exact; machine continuation kept separate. Makes fold-in one LLM call regardless of span
  depth.
- **Pin-and-requeue**: `prior_pinned` / `prior_pinned_hadm_ids` / `tldr_stale` on `tiered_view`;
  `prior_span_drift` concerns in `review_queue`. Severity currently `high` -- open call.
- **DB as source of truth for promotions**: `holdout.promote()` writes the `promotions` table
  first, then projects `promoted_admissions.csv`. `db_persist_promotions.py` is backfill/repair
  only.
- **Fold-in concurrency**: session-level Postgres advisory lock (key `xxxxxxxx`) in
  `foldin_patient.py`, around the whole cascade. The API's `threading.Lock` is an in-process
  fast path only.
- **c5 review queue**: redesigned from an append-only file to a Postgres liveness-keyed queue
  with UPSERT and an auto-clear watermark. `review_queue.jsonl` is now a projected cache.
- **`review_decisions`**: append-only physician decision history, written in the same
  transaction as the `review_queue` update.
- **Retention-visibility guard**: `retention_visibility_gaps()` aborts the write in
  `db_persist_retention` and returns an advisory `visibility_gaps` on read.
- **e1 `--subject` renamed to `--preview`**: the flag was print-only and returned before the
  write block.
- **b2 clustering**: aggressive cross-class merge (containment + bigram union-find, no
  class-group gating) REJECTED - it buried an active HCC under a transplant-history node.
  Current rule is class-group gated. Do not re-introduce the predecessor.
- **b1 prompt**: anti-inference counterweight and an explicit REDACTIONS rule added by Stay
  after the fabrication trace (FM-1).
- **SNOMED grounding subproject**: CLOSED. The MedCAT hybrid added complexity without resolving
  the structural failure modes. Residue: the `snomed_*` columns on `patients`.
- **f7 judge calibration**: `clarified_v2` prompt adopted (Gwet's AC1 0.721, MCC 0.514).
  `Not Addressed` verdicts are not reviewed downstream.

---

## 5. Schema drift

`schema.sql` is the FRESH-CREATION form: every column lives in its `CREATE TABLE`, and there is
no `ALTER` section. An older database is upgraded by a one-off migration against that database,
never by re-accreting ALTERs into the file.

Consequence: the file does not tell you what any given database actually has. Check.


```sql
-- what a given database actually has
SELECT table_name, count(*) AS n_cols
FROM information_schema.columns
WHERE table_schema = 'public'
GROUP BY table_name ORDER BY table_name;
```

---

## 6. Open items

- `prior_span_drift` severity: currently `medium` under the two-region model
  since added material is machine-validated and clearly demarcated. Clinical-visibility call.
- FM-2 flagged-bucket rendering: `provisional` is one boolean over four clinically distinct
  situations (open problem / surveillance / presumed past event / patient-reported binding
  directive). Proposed `flag_kind()` + `provisional_kind` column, not implemented.
- FM-4 T4 rollup still discards the finding's own text when `parent_condition` is set.
- FM-6: correction steps (b1 `_UPDATE_TMPL`, c1, c4) cannot see the source note, so they can
  only soften a fabrication, never delete it. Shared fix candidate.
- b4's T5 recency prune has no per-node reason recorded, only aggregate counts. Recoverable
  from the raw ledger, but not explained.
- e1 `dayfirst=True` date-swap: `incoming_date` is swapped cohort-wide. Fix is removing
  `dayfirst` for ISO interim files, then `e1 --fresh` + `d3`.

