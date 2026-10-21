# GLOSSARY.md

Pipeline vocabulary for `clin_longsum`. An agent without these misreads the artifacts:
several terms look like ordinary English and are not, and three pairs (`in_ledger` vs
`is_ledger_member`, `covered` vs `generated`, write-time BAKED vs read-time DERIVED) are
routinely confused.

Authoritative definitions live in code. Where a term is implemented, the file is named --
read it live rather than trusting this summary.

---

## Ordering and time

**`admit_seq`** - 1-based ordinal of an admission within one patient's history, assigned by
`a2_build_cohort`. **Every ordering decision in the pipeline uses this, never a parsed date.**
When a stage needs to know whether admission A precedes admission B, it compares `admit_seq`.

**`admittime` / `dischtime`** --raw cohort timestamps, Singapore-locale DAY-FIRST (`D/M/YYYY`).
`cohort.py` is the single `dayfirst=True` parse point.

**Fold-in ORDER vs fold-in TIME** - admissions are *processed* in `admit_seq` order, 
but an old admission is *compacted* only when a later one is promoted, so the compaction of admission 1 
can run after admission 8 exists in the ledger. 

---

## Windowing

**Hot window** - the most recent `N_HOT_DISPLAY` (7) cached per-admission paragraphs, shown as
Tier 2a. A DISPLAY choice.

**`N_COMPACT_THRESHOLD`** (7) - admissions older than the most recent N are folded into the
compacted Prior Admissions tier. A COMPACTION choice, deliberately decoupled from
`N_HOT_DISPLAY`. Invariant: `N_HOT_DISPLAY <= N_COMPACT_THRESHOLD`, asserted at startup.

**`boundary`** - `n - N_COMPACT_THRESHOLD`, where `n` is the patient's established admission
count. An admission with `admit_seq <= boundary` has AGED OUT and belongs to Tier 3 of the prose.

**Aged out** - past the boundary; a candidate for compaction.

**Established span** - the admissions the record is built over, i.e. everything not still held
out. `holdout.established(df)`. b1 extracts this span only.

---

## Holdout and promotion

**Holdout** - a simulation of incremental update. `a2_build_cohort` marks the latest admission
per patient `is_holdout=True`. The ESTABLISHED record is built over the non-holdout span; the
held-out admission is the "incoming" episode surfaced for physician review. In production
`HOLDOUT_LAST_ADMISSION` is off and there is no holdout - the incoming admission arrives for
real.

**Incoming** - the held-out (or newly arrived) admission awaiting review. Summarised by e1 into
`incoming_summary.jsonl`, served in `tiered_view.incoming_*`. **The ledger does NOT cover it.**
The record is built over the ESTABLISHED span, so a condition documented for the first time in
the incoming admission is correctly absent from the ledger until that admission is promoted -
absence there is expected, not loss. This is the commonest reader's error against the served
record; see `S1e` in `FEEDBACK_RUNBOOK.md`.

**Promotion** - the physician-approval event that folds an incoming admission into the
established record. `holdout.promote()` writes the `promotions` TABLE first, then projects
`promoted_admissions.csv` from it.

**Fold-in** - the per-patient cascade run after a promotion: b1 (single admission, spliced) ->
`holdout.promote` -> b2 -> b4 -> c1 -> c3 -> c4 -> c2 -> d2 -> d_coordinator -> d3, plus the
DB persist stages. Driven by `foldin_patient.py`, which holds a session-level Postgres advisory
lock so only one fold-in runs at a time across all workers.

---

## The ledger

**Retention ledger** - the LOSSLESS layer. Per-patient current-state projection of the pinned
facts, built deterministically by b2 from b1's per-admission facts. Its existence is what makes
lossy prose compaction safe: prose may drift, the ledger may not.

**Pinned** - retained in the ledger. Not a stored flag; see below.

**`in_ledger`** - a DERIVED property on a `RetentionFact` (`contracts.RetentionFact.in_ledger`),
computed on access. In the DB, `retention_facts.in_ledger` is a BOOLEAN column: a snapshot taken
at load time under whatever taxonomy version was in force. The two can disagree after a taxonomy
change, and the live derivation is what the pipeline acts on.

**`is_ledger_member`** - the FUNCTION that makes the decision
(`retention_taxonomy.is_ledger_member(salience_tier, certainty, lifecycle, negative_kind,
retention_class)`). The rule lives here and nowhere else. Never predict its output - execute it
on the fact's exact axes.

**`prune_reason`**  - the companion function naming WHICH branch of `is_ledger_member`
declined a fact. Returns None when the fact is a member. Feeds b2's `dropped[]`.

**`fact_id`** - `sha1(f"{source_hadm_id}|{text}")[:16]`, assigned by
`contracts.RetentionFact.assign_fact_id`. The join key from `retention_facts` through the ledger
to `tiered_ledger.fact_ids`, and the only STABLE anchor for a fact. Two limits worth knowing
together:

- It is TEXT-DERIVED, so a b1 rerun that rewords a fact changes the id. A fact is not traceable
  by id across a re-extraction that touched its wording.
- `tiered_ledger.id` (and `tiered_ledger_raw.id`, `retention_dropped.id`) is NOT an alternative.
  Those are `SERIAL` and d3 deletes-then-reinserts per subject, so every row id for a patient is
  reissued on every fold-in. A stored `tiered_ledger.id` fails in the worse direction: it still
  resolves, to a different fact. A stale `fact_id` merely stops resolving, which is visible.

**Node** - one entry in the ledger's `nodes[]` / `flagged[]`, built by `b2._build_node` from a
CLUSTER of same-condition facts. A `LedgerNode` is NOT a `RetentionFact`; the shapes differ.

**Cluster** - a group of facts within one `retention_class` judged to be the same condition, by
overlap coefficient on content tokens requiring at least one shared non-qualifier token
(`b2._cluster`).

**Content tokens vs identity tokens** - two token sets from the same text, used for different
jobs and easily confused. `b2._content_tokens` lowercases, expands abbreviations via `_ABBR`, and
subtracts `_STOPWORDS`; it drives CLUSTERING. `b2._identity_tokens` is that set minus
`_QUALIFIERS`; it drives the Failure Mode 7 (FM-7; see `KNOWN_FAILURE_MODES.md`) SPLIT guard, which asks whether every member of a cluster shares
an identity token with the member supplying the displayed text. A token that is meaningless but
survives both filters bridges unrelated conditions - the guard is only as good as its tokens.

**Surface normalisation** (`b2._normalize_surface`) - applied by BOTH
`_content_tokens` and `_phrase` before tokenising. Strips the possessive suffix
(`<Eponym>'s` -> `<eponym>`, ASCII apostrophe and U+2019) and drops the `s/p` abbreviation. Exists
because `[A-Za-z0-9]+` shattered both forms into a bare `s`, which is in neither `_STOPWORDS` nor
`_QUALIFIERS`, so a possessive eponym and an `s/p` procedural fact shared exactly that one
specific token and clustered (FM-7 residual). STRIPS rather than folds to `<eponym>s`, so
possessive and non-possessive spellings of an eponym stay on one token. Note that a blanket
single-character filter would NOT be safe: digits are load-bearing ("Type 1" vs "Type 2 diabetes")
and so is `c` ("Hepatitis C").

**Consolidation** - there are TWO consolidation stages, both correct, and attributing a merge to
the wrong one is a common misread:

- **b2 `_consolidate`** (deterministic, LLM-free) - a SECOND, later merge across clusters within
  a compatibility group (diseases / procedures+devices / own-class) via Jaccard, curated
  condition keys, or verbatim phrase containment. Distinct from clustering; both can change a
  node's attributes.
- **b4 `ledger_consolidate.py`** (LLM, tier-aware) - groups T3 chronic-state roll-ups and
  collapses T4 recurrent episodes, merging provenance deterministically and never across
  `current_lifecycle`; T5 recency pruning is deterministic. Its grouping is NOT reproducible,
  which is why b2's output is preserved in `retention_ledger.raw.jsonl`.

When asking whether b2 produced a node at all, read the RAW ledger. A node present in raw and
absent from served was absorbed by b4, not declined by b2.

**`provenance`** (on a node) - attribute -> `fact_id` map recording which member of the cluster
supplied each served attribute. Exists because a node is assembled from DIFFERENT facts: tier
from the strongest mention, text/class/certainty/lifecycle from the latest, `negative_kind` from
the merge-priority winner. Audit only.

**`dropped[]`**  - b2's prune audit trail: every extracted fact that matched no routing
branch, with its axes and a `reason`. Never served, never counted in c2 recall. Persisted to the
`retention_dropped` table.

**`considered_excluded`** - ruled-out facts. A list of BARE STRINGS: the axes and `fact_id` are
discarded, unlike `dropped[]`.

**`amended`** - **a BUCKET, not a status.** This is the single most misread word in the
ledger vocabulary, so read this entry before using it.

> **`amended` means: the RECORD was corrected, so this entry is withheld from the served
> ledger.** ie it is retracted. It says nothing about the patient's condition.

There are exactly FOUR lifecycle values (`tax.LIFECYCLE`): `active`, `resolved`,
`in_remission`, `superseded`. `amended` is none of them. It is one of the six lists on a
`RetentionLedger` - `nodes`, `flagged`, `episodes`, `amended`, `considered_excluded`,
`dropped` - and it holds nodes whose `current_lifecycle` is `superseded`. The bucket is named
for the ACT (someone amended the record) while its contents are labelled with the STATE
(`superseded`), which is why it reads like a fifth status. It is not one.

**Better term, if you are writing prose rather than code: RETRACTED.** The bucket holds
retractions - entries withdrawn because the record was wrong, not because the patient got
better. `retracted` names the state rather than the act, matches the vocabulary `b2._build_node`
already uses ("a retraction wins outright"), and cannot be misheard as a lifecycle value the way
"amended" is. A field rename is a cross-pipeline change (b2, c2, d3, f2, f11, f12, b3, the review
UI and the loss-audit tools all key on the literal string `"amended"`), so it is a deliberate
post-launch decision, not a drive-by. Until then: **the field is `amended`, the concept is
retraction.**

- **The bucket is not served.** d3 does not load it, and c2 counts it as REPRESENTED. A fact
  landing here is invisible to the reader while scoring as retained - that is the FM-8
  signature, and membership here is its ONLY signature.

**Episode rollup** - a T4 `recurrent_episode` collapsed to `{parent_condition, count,
first_date, last_date, source_hadm_ids}`. The finding's own text is consumed as the grouping key
and discarded when `parent_condition` is set (FM-4).

**Raw vs served ledger** - `retention_ledger.raw.jsonl` is b2's output; `retention_ledger.jsonl`
is that output after b4's LLM-assisted consolidation. b4's grouping is not reproducible, so the
raw file is the recoverable one. c2 validates recall against RAW, not served.

---

## Fact axes (taxonomy)

**`salience_tier`** - T1 alert / T2 permanent baseline / T3 chronic / T4 recurrent episode /
T5 transient. The per-class `default_tier` is a FLOOR the extractor may escalate from but not
de-escalate below (`tax.floor_tier`).

**`retention_class`** - one of 12; sets the default tier and groups the ledger.

**`certainty`** - `confirmed | suspected | ruled_out | patient_reported`.

**`lifecycle`** - `active | resolved | in_remission | superseded`. **Exactly four values.** The
first three describe the PATIENT; `superseded` describes the RECORD. That asymmetry is why
`superseded` gets its own hard guard and its own bucket. Note the bucket is called `amended`,
which is NOT a fifth lifecycle value - see `amended` above.

**Retraction** - the plain-English name for `lifecycle=superseded`: the record was withdrawn or
corrected. Used throughout `b2._build_node` / `_merge_group`, where the rule is that the LATEST
mention wins - if the newest member of a cluster or merge group is superseded, it supplies both
`negative_kind` and `current_lifecycle` outright. The test is latest-is-superseded, never
any-member-superseded: a de-label that is OLDER than a live re-assertion has itself been
superseded, and letting it win would suppress a live contraindication.

**`SUPERSEDED_DEFAULT_NK`** (`b2`) - `absent_or_negative`, supplied when a
retraction carries no explicit `negative_kind`. Mechanical rather than clinical: of the three
non-null `NEGATIVE_KIND` values, `resolved_relevant` (happened, then ended) and `surveillance`
(being watched for) are both wrong for a withdrawn record by definition, leaving
`absent_or_negative` as the only coherent value. Exists because b1 sometimes tags a fact
`superseded` without setting `negative_kind`, and the guard used to forward that null - producing
`superseded` + NULL, the incoherent combination `HOW_TO_READ_THE_LEDGER.md` §7 warns physicians
about (a retraction that retracts nothing).

**`occurrence`** - `index | recurrence`. `index` = first time seen across this patient's history.

**`negative_kind`** - `null | absent_or_negative | resolved_relevant | surveillance`. 
`resolved_relevant` is a positive event that HAPPENED and resolved - not an absence. 
Any non-null value pins the fact **unless `certainty=ruled_out`**, whose guard runs first (`prune_reason` then returns `ruled_out`).

**`provisional`** - how the served ledger marks the `flagged[]` bucket (suspected /
patient-reported). A single boolean over four clinically distinct situations.

**Write-time BAKED vs read-time DERIVED** - the third confusable pair, and the one that most often
produces a wrong answer from a correct file. `salience_tier` is BAKED into
`retention_facts.jsonl` by b1 at extraction (`tax.floor_tier`), so changing a class default does
NOT reach existing facts. `retention_class` is DERIVED on every load by `normalize_fact`, so
changing a routing rule reaches them immediately, with no re-extraction. A stored
`retention_class` is therefore the extractor's guess, not the pipeline's answer.

**`normalize_fact(fd)`** - the read-time reclassifier (`retention_taxonomy`), called by b2, b3,
c1 and `db_persist_source`. Mutates the fact dict in place and returns `(promoted,
reclassified)`. Never reason about a stored `retention_class` without running it first - and note
b1 does NOT run it (`b1._normalise_fact` applies only the surveillance promotion and the tier
floor), which is why the two disagree.

**Re-routing net** - a deterministic text predicate that corrects an extractor mis-tag at load
rather than at extraction: `is_surveillance_finding` , `is_disease_course_event`
, `is_sentinel_episode` . Each is consulted only for a specific SOURCE class, so
a net firing on a fact does not mean the fact was re-routed - an over-match outside the source
class is inert. Adding one is change shape G; see `TAXONOMY_EXTENSION.md`.

**`is_safety_robust(class)` / safety-robust class** - public accessor over
`_SAFETY_ROBUST_CLASSES` (`severe_adr_allergy`, `code_status_directive`). These pin regardless of
tier, certainty and lifecycle, with `certainty=ruled_out` the SOLE exit: the safety branch is tested ABOVE 
the `superseded` guard, so a de-labelled allergy stays pinned. See `amended` above for the second consequence.

**`supersedes`** - `fact_id` this fact amends. Usually null at extraction; resolved during b2
projection.

---

## Compaction 

**Prior Admissions** - the single compacted paragraph covering everything past the boundary.

**Two-region model** - when a physician has edited the compacted tier, the served
paragraph is `pinned_prefix_md` + marker + `prior_generated_md`. The prefix is physician bytes,
frozen; the continuation is machine text. `compact()` never sees the prefix.

**`PRIOR_REGION_MARKER`** - `"\n\n---\n\n"`, the separator between the two regions. The UI
renders it as a divider labelled "Added since your review". c4 must reconstruct the served text
byte-identically.

**Pin / freeze** - `tiered_view.prior_pinned` plus `edited_prior_admissions_md`. A pinned span
is never regenerated over.

**Pin-and-requeue** - when a later fold-in ages a new admission into a pinned span, the edit is
KEPT and a concern is queued (`review_queue.source_role='prior_span_drift'`) for the physician to
decide. Never a silent clobber, never silent staleness.

**`covered_hadm_ids` vs `generated_hadm_ids`** - `covered` is the FULL union of admissions the
paragraph represents; `generated` is the continuation's coverage (covered minus the prefix span).
They differ only when a physician prefix exists. c3/c4 judge and correct against `generated`.

**`ledger_facts`** - the span-scoped `RetentionFact` list handed to the compactor and echoed
back on its output. 

**`_ledger_for_span(facts, max_seq)`** - drops facts first documented after `max_seq`, so a
paragraph about old admissions is not informed by later ones. Facts with no resolvable
`admit_seq` are KEPT deliberately: dropping a pinned fact is judged worse than showing one
slightly ahead of the span.

**Seeding** - on a fold-in, c1 starts from the prior result and folds only the newly-aged
delta, so a fold-in is one LLM call regardless of span depth. `seed_source` is
`none | generated | edited`.

**Incremental vs full** (`COMPACTION_MODE`) - incremental folds one admission at a time into
the accumulating paragraph (production path, cheaper, may drift); full rebuilds the whole span
in one call (stable, audit-true, used for offline backfill).

---

## Validation

**Faithfulness (c3)** - no hallucination: every claim in the paragraph must be supported by the
source it was compacted from. Threshold 0.6.

**Retention completeness (c2)** - the opposite guard: no critical OMISSION. Every pinned fact
from the compacted span must be represented somewhere in the ledger. Deterministic, no LLM.

**`pass`** - c3's aggregate score-vs-threshold boolean. A record can carry unsupported
statements and still pass. c4 gates on the PRESENCE of unsupported statements, not on `pass`.

**Judge degraded** - the deepseek judge fell back to no-think mode on some step. A weaker
verdict; a no-think PASS is flagged for review.

**Class separability** - how far a `retention_class` is distinguishable from the fact TEXT
alone, as opposed to from a judgment the note does not make. Measured 2026-08-29 by three
independent methods that agreed on the rank order. HIGH: `code_status_directive`,
`chronic_disease_state`, `implanted_device`, `transient_event`, `anatomy_altering_surgery`. LOW:
`sentinel_event`, `recurrent_episode`, `permanent_condition` - these are separated from
`chronic_disease_state` by reversibility, recurrence or prognostic weight, none of which the text
states. **Where a boundary is absent from the input, no model capacity recovers it**, so class
disagreement inside the low band is expected rather than a defect (FM-9). `severe_adr_allergy` and
`transfusion_obstetric_history` are too small to measure this way - their low scores are an
artefact of class size, not a finding.

**Test-retest reliability vs item-to-item consistency** - distinct quantities, different fixes,
routinely conflated. TEST-RETEST: the same input rerun gives the same label. Varies with the RUN;
fixed by seeding, temperature 0 and batch-invariant kernels; the pipeline's exposure is low
(b1 runs at `temperature=0.0`). ITEM-TO-ITEM: the same clinical concept, phrased differently in
different notes, gets the same label. Varies with the INPUT; fixed by sharper class criteria,
task decomposition or a better model; the pipeline's exposure is HIGH and it is what class
separability measures. Selecting a model for determinism targets the wrong one - a model can be
perfectly reliable and reliably wrong. See `MODEL_SELECTION.md`.

**MISSING vs PINNED_IN_ROLLUP** - c2's two issue types. MISSING = a pinned fact absent from the
ledger entirely (genuine loss). PINNED_IN_ROLLUP = a pinned fact represented ONLY as an episode
count, i.e. a sentinel-grade event filed as a recurrence (mis-tag candidate, not a loss).

---

## Review and governance

**`review_id`** - a concern FINGERPRINT (sha1 of role, subject, hadm_ids, score magnitude), not
a row id. The same concern re-derived on a later fold-in keeps its id and its triage.

**Liveness / auto-clear** - c5 derives whether a concern is still live from the LATEST
`generation_audit` row per group. A pending row the pipeline stops reproducing is set to
`auto_cleared`, never deleted.

**Governance principle** - physician decisions override pipeline output, always. Generated
artifacts a physician can edit get a separate physician-authored column plus provenance
(`<x>_md` generated -> `edited_<x>_md` physician -> `approved_<x>_md` final); UPSERTs refresh
only generated columns; regeneration that would invalidate an edit pins or re-queues rather than
clobbering; decisions are transactional and appended to an immutable history
(`review_decisions`).

**Taxonomy version** - `retention_taxonomy.TAXONOMY_VERSION`. A governance object: any edit
bumps the version and appends to its `CHANGELOG`. Recorded in every `AuditRecord`.

**Prompt version** - `config.PROMPT_VERSIONS[role]`. A prompt change is as auditable as a model
change; bump the string, never silently rewrite.

**Feedback Triage policy version** - `support/policy/triage_policy.yaml` `version`. The THIRD governance
object, with the same discipline as the two above: bump, append to the in-file changelog, obtain
clinical sign-off. Engineering may propose; only the clinical owner ratifies, because "what counts
as urgent" is a clinical call. Recorded on every card as `feedback.policy_version`. Unlike the
other two it can be REPLAYED exactly (`support/replay_policy.py`), so a proposed change is
presented for sign-off with the list of past reports it would have decided differently.

**Change shape (A-G)** - `TAXONOMY_EXTENSION.md` vocabulary classifying a taxonomy edit by BLAST
RADIUS: A pin/prune rule, B class wording, C re-tier, D new class, E new tier, F certainty or
lifecycle value, G re-routing net. It determines re-run cost only. Governance is identical across
shapes - every one runs the same three steps at §4.0 (evidence before proposing, decision record
before code, predict then verify).

**Staged change** - a taxonomy edit that ships in a version bump but does NOT take effect until
the next b1 run, because it lives in the prompt (`EXTRACTION_RULES`, `counter_examples`,
anything rendered by `render_for_prompt`). Contrast a net or gate edit, which re-derives on the
existing corpus at once. The CHANGELOG entry must say which it is; where the gate reorder protected stored data and the prompt tightening was left unexercised.

**Golden case** - a row in `_NET_GOLDEN_CASES` (`retention_taxonomy.py`) asserting a net's
behaviour on a verbatim cohort phrase. Status is `required` (a mismatch is a regression), `xfail`
(a RATIFIED gap that must keep failing - an XPASS means it closed and the row should be promoted)
or `tolerated` (fires wider than clinical intent, harmlessly, because the net is scoped to one
source class). Run `python src/retention_taxonomy.py --selfcheck` before AND after any net edit;
non-zero exit means a required case broke or a gap silently closed. Note `conda run` swallows the
output without `--no-capture-output`.

**Mixed-version cohort** - normal, not corruption. A version bump that needs no re-extraction is
cheap precisely because it leaves `retention_facts.jsonl` alone, so `audit.taxonomy_version`
varies across admissions within one cohort and across cohorts. Read the stamp per record rather
than assuming a cohort has one, and take any before/after distribution diff from re-derived
values, never from the stored field.

**Dominance gate / frontier baseline** - `f12_frontier.py`'s check that a configuration is not
dominated on the quality/cost frontier, evaluated against
`data/output/eval/ablations/frontier_baseline.json`. The baseline is gitignored, so a fresh clone
has none and the gate is inert until one is established. Staleness is keyed on
`taxonomy_version`: any bump marks an existing baseline stale and SKIPS the gate until it is
regenerated.


---

## Physician feedback loop

The channel behind `HOW_TO_READ_THE_LEDGER.md` §8 ("report the row, it gets fixed at source").
Operational detail lives in `FEEDBACK_RUNBOOK.md`; only the terms an agent could MISREAD while
reading pipeline artifacts or cross-document references are defined here.

**Concern vs report (card)** - the confusable pair. Both are "things a physician acts on in the
review UI", and they are unrelated objects:

- A **concern** is PIPELINE-DERIVED. c5 raises it from stage output, fingerprints it as
  `review_id`, and it `auto_clear`s when the pipeline stops reproducing it. Lives in the clinical
  database. The physician acknowledges; they do not author it.
- A **report** (a **card**, table `feedback`) is PHYSICIAN-ORIGINATED. Nothing derives it, nothing
  auto-clears it, and it closes only when a human writes an answer. Lives in the SUPPORT database.

c2's `MISSING` / `PINNED_IN_ROLLUP` rows are a third thing again - deterministic completeness
flags, also acknowledgement rather than authorship.

**Support database** - `longsum_support`, reached ONLY via `PGDATABASE_SUPPORT` /
`database.pg.get_support_conn()`. Ticket lifecycle, triage tags, staff identity, correspondence.
No cross-database foreign keys and no pipeline stage reads it - deliberately, since d3
deletes-and-reinserts `tiered_ledger` per subject and a FK would cascade reports away. The
consequence is that a card carries its own SNAPSHOT of the row and the versions that produced it;
without that it could not be dispositioned later, only re-investigated.

**State vs disposition** - lifecycle position vs verdict, two columns, routinely conflated.
`state` is `new | triaged | contained | in_progress | resolved | closed`. `disposition` is the
answer (`accepted_taxonomy`, `by_design`, `not_reproducible`, ...), set once on the transition
into `closed`. Keeping them separate is what lets a board tool be swapped without remapping
verdicts onto someone else's lane names.

**Symptom / defect class / S1 sub-class** - three levels, distinct questions:

- **Symptom** (an S-code, S1-S12 plus S99) - WHAT THE PHYSICIAN SAW. They are never asked which
  stage is at fault. Deterministically routes the card to an owning role.
- **Defect class** - WHO OWNS THE FIX: `clinical_taxonomy`, `engineering_routing`,
  `prompt_wording`, `documentation`. Set by the owner at disposition.
- **S1 sub-class** - WHICH STAGE LOST IT, resolved deterministically by the evidence collector.

**S1a-S1e** - the sub-classes of "a fact is missing", walked FORWARDS so the first stage that
lost the fact is the one named: **S1a** never extracted by b1 (most expensive - a prompt change
means cohort-wide re-extraction); **S1b** pruned by the taxonomy gate (`retention_dropped.reason`
names the branch); **S1c** absorbed by b4 consolidation (present in `tiered_ledger_raw`, absent
from `tiered_ledger`); **S1d** present after all; **S1e** documented ONLY in the incoming
admission. S1e is NOT a defect - see `Incoming`. A rising S1e rate means the interface, not the
pipeline, is failing to separate the incoming admission from the established record.

**Urgency floor / U-rule** - urgency answers HOW FAST THE WRONG OUTPUT MUST STOP REACHING A
READER, not how wrong it is or how costly the fix. Seven deterministic rules (`U-1`..`U-7`) may
RAISE a card to urgent; only a named human may lower it, with a written reason appended to the
audit trail. Every urgent card names the rule that fired - which is what makes per-gate
calibration readable later. The asymmetry is deliberate: wrongly holding a card costs attention,
wrongly releasing one costs a patient.

**Containment vs repair** - the corollary of the above, and the reason an urgent card can close
with the source fix still pending. Containment stops the row being misread NOW (a Tier 3
pin-and-edit for one patient; a display change if systematic). Repair ships on the normal release
cadence. Decoupling them closes the window between a flag and a taxonomy release during which
wrong nodes keep entering the c1 prompt.

**Dedup key** - `sha1(symptom | retention_class | salience_tier | normalized_fact_text)[:16]`. A
SIGNATURE, not an id. Matching cards are LINKED, never merged away: the count of DISTINCT
SUBJECTS carrying a key is the systematic-defect detector (rule `U-5`) and the scoping number for
a fix, so collapsing duplicates would destroy exactly the signal the loop exists to surface.

**Evidence collector** - `support/evidence.py`, read-only against the clinical cohort. Resolves
the S1 sub-class, screens the source notes, and EXECUTES the taxonomy functions rather than
predicting them. Deterministic throughout; no model is involved anywhere in triage, which is what
makes exact policy replay possible. Its `missing_fact_probe` verdicts (`in_incoming_only`,
`in_prior_unextracted`, `negated_only`, `not_in_source`, `ambiguous`) are token overlap, not a
faithfulness judgement - they miss synonymy and err toward `ambiguous`, which routes to a human.

**Alias (`PT-nnnn`)** - the synthetic patient reference used on every physician-visible surface
outside the clinical UI (board, email, digest). `subject_id` never leaves the clinical database.

---

## Namespacing

**`RUN_NAMESPACE`** (`LONGSUM_NAMESPACE`) - puts all FILE artifacts under `data_<ns>/` so a
parallel cohort never clobbers dev. It does NOT isolate Postgres; pair it with a dedicated
`PGDATABASE` or dev and namespaced rows mix in the same tables.

**`LONGSUM_DATA_ROOT`** - where `data[_<ns>]/` lives. Lets the shipped, data-free repo run
against a cohort stored outside the tree.
