# EVALUATION.md -- evaluation runbook

How `clin_longsum` is evaluated, what each evaluation (F-series) script does, how to run the suite, and what
the results are worth. Companion to `PIPELINE.md` (what the generation stages do) and to
`EVALUATION_CLINICAL.md` (the same framework for a clinical reader and a governance board).

**Audience.** The engineer standing this pipeline up on their own notes. A clinician
co-signing the gate thresholds should read `EVALUATION_CLINICAL.md` first and use this one as
reference.

**Contract.** Phase F reads generation artifacts read-only and writes only under
`data/output/eval/` and `data/output/studies/`. Evaluation never writes a generation artifact
and never mutates production config.

**If you fork this, none of the numbers below apply.** They come from a MIMIC-IV-Ext-BHC
reference run. The harness ships so you can regenerate them on your own corpus. Regenerate
before you cite anything.

---

## 1. What is being evaluated, and why it is hard

The artifact is a per-patient `LongitudinalView`: TL;DR, current episode, hot-window
paragraphs, a compacted Prior Admissions narrative, and the retention ledger, built over the
established (non-holdout) span.

Two properties remove the obvious shortcuts.

1. **Longitudinal.** Multi-admission, so there is no gold reference summary to score with
   ROUGE or BERTScore. No public corpus pairs a multi-admission record with a curated
   longitudinal summary.
2. **Compaction.** Distant and non-salient events are deliberately dropped or collapsed. Any
   metric that rewards reproducing the source punishes correct behaviour: a pruned Tier-5 cold
   is a success, not a loss.

`d1` (script creating summary for one admission) is not single-admission BHC synthesis either. It summarises the already-summarised BHC
`target` field into one paragraph, a summary of a summary, so the published BHC benchmark is
not a valid reference target for it.

Three evaluation levels, only the lowest of which has any external gold:

| Level | Artifact | Reference available | Methods |
|---|---|---|---|
| L1 fact | atomic claims in each tier | source notes (entailment) | faithfulness; salience-weighted recall |
| L2 compaction | ledger + Prior Admissions | the lossless raw ledger (self) | salience-weighted recall; compression-retention frontier |
| L3 longitudinal whole | the assembled view | none | PDSQI-9+1; the incoming clinician summary; ablations |

Because no gold longitudinal reference exists, the defensible spine is **fact-grounded
intrinsic metrics, a judge calibrated against clinician labels, and ablations against deliberately-applied
degraded variants**. 

### Intrinsic and extrinsic

**Intrinsic** metrics score the system against its own inputs and artifacts. They need no
external data, are reproducible, and are where the hard gates live.

- f1 salience-weighted retention recall (ledger vs the pinned fact set)
- f2 structural and temporal integrity (assembled view vs the coordinator's window contract)
- f3 compression ratio (prose vs the source span it condensed)
- f4 atomic-claim faithfulness (each unit vs exactly what its generator consumed)
- f9 merge faithfulness (a consolidated ledger line vs its member entries)
- f10 judge-health audit (c3's own output, deterministic)
- f11 metric validation (the metrics vs known-answer corruptions)
- f12 compression-retention frontier (the operating point vs the previous release's)

**Extrinsic** metrics bring in something the system did not produce.

- f6 incoming reference: the real, unedited clinician BHC of the current admission, used as a
  free gold standard for "what a physician thought worth carrying forward". The only
  human-authored reference inside the repo.
- Judge calibration against VeriFact-BHC: clinician-labelled propositions, external corpus.
  **Not shipped here** (section 9).
- The generalisation run: unseen patients, frozen config. External validity, not external
  ground truth.

**f5 PDSQI-9+1 sits between them, and only partly.** Nine of its ten attributes are the
published, externally validated PDSQI-9. The tenth, `temporal_coherence`, is ours: PDSQI-9 has
no temporal dimension, and event order is a first-class quality property of a longitudinal
summary, so it was added here. That attribute carries no external validation and should be
reported as a local extension, not as part of the published instrument. In either case the
ratings are produced by our own judge, so treat LLM-scored PDSQI-9+1 as intrinsic until a human
panel scores the same patients.

---

## 2. The three-tier CI framework

Split by determinism and cost so it can run in a pipeline.

**Tier 0 -- deterministic gates. Every run. Blocks the build.**
No LLM, no GPU, reproducible bit-for-bit on a frozen cohort. Non-zero exit stops promotion.
f1, f2, f3.

**Tier 1 -- LLM-judge metrics. Nightly, or on a model or prompt change. Tracked, soft-gated.**
Non-deterministic, so run at `temperature=0` and gate on a tolerance band against a stored
baseline rather than absolute equality. A breach raises an alert, not necessarily a stop.
f4, f5, f6, f9, f10 (deterministic itself, but audits a Tier-1 judge).

**Tier 2 -- periodic validation. Off the build path. On release, quarterly, or on judge swap.**
Validates the harness rather than the build. f11, f12, the `studies/` ablations, judge
calibration, and any human PDSQI-9+1 panel.

**Reproducibility contract for Tiers 0 and 1.** A frozen golden cohort (fixed subject ids,
pinned input artifacts), pinned model tags and digests recorded in every `AuditRecord`, pinned
`TAXONOMY_VERSION`, `temperature=0` for judges. CI runs the golden subset; the full cohort runs
nightly.

### Build-time and run-time are different things

The same deterministic code runs at two points in the lifecycle. Conflating them is a source of confusion.

**Build-time qualifies a VERSION.** Trigger: a change to model, prompt, taxonomy, or pipeline
code, or a scheduled regression. Scope: batch, the full frozen cohort with each patient's
latest admission held out. Question: is this software safe to deploy. Tiers 0 and 1, plus 2 on
a judge swap.

**Run-time qualifies one patient's OUTPUT.** Trigger: a physician approves an incoming
admission in the review UI, which promotes it and re-runs the established stages for that one
subject (`foldin_patient.py`). Scope: a single `--subject`. Question: is this rebuilt view safe
to serve. **Tier 0 only.** LLM-judge and calibration never run inline on an approval.

The fold-in guard sits between `d_coordinator` and `d3`:

```
physician approves
  -> holdout.promote(subject, hadm)          # incoming rejoins established
  -> b2 ... d_coordinator --subject S        # rebuild the file artifacts
  -> f1 --subject S  +  f2 --subject S       # Tier-0 guard on the rebuilt artifacts
        pass -> d3_load_tiers --subject S    # commit to tiered_*, notify
        fail -> do not serve; keep the prior view; flag for review
```

`d3` is per-patient atomic and idempotent, so a failed guard leaves the last-known-good served
view untouched.

One codebase serves both because the batch eval holds out each patient's latest admission,
which is a simulation of the runtime fold-in. Batch pre-plays the per-patient fold-in across
the cohort as a regression test; the run-time guard is the same check on one real approval.
That is why every f-script takes `--subject`.

---

## 3. Tier 0: the deterministic gates

### 3.1 f1_salience_recall.py -- salience-weighted retention recall

The metric no prior BHC evaluation needed, because none had a compaction stage that is
*supposed* to drop content. It is the salience-aware, tier-stratified, hard-gated
generalisation of the `c2` validator.

**Scope.** The compacted (aged-out) span per patient, mirroring `c2`'s `covered_hadm_ids`
exactly. That is the only place narrative compaction can drop a fact.

**Target.** The lossless ledger (`retention_ledger.raw.jsonl` when the served ledger is
consolidated). b4's T5 prune is a serving reduction, not a loss.

**The salient set S.** Distinct pinned facts (`f.in_ledger`, after the same surveillance,
disease-course and sentinel promotions b2 applies) drawn from the compacted span. Pinning
already encodes "should be retained", so every member of S is expected-retained, and facts the
taxonomy intends to drop never enter S. Correct dropping is therefore never penalised.

**Homing.** Reuses `c2`'s representation-homing, so if the homing rule changes this metric
follows automatically:

- `node` -- retained in nodes, flagged or amended (full fidelity)
- `excluded` -- retained as a ruled-out significant negative
- `episode` -- present only as a frequency rollup: a resolution loss
- `none` -- absent from the ledger: a genuine loss

`represented(f) = home in {node, excluded}`.

**Weights**, by `retention_class` id only, no content detection:

```
base:   T1=5  T2=4  T3=3  T4=2  T5=1
floors: retention_class in FLOOR_TOP_CLASSES
          {severe_adr_allergy, chronic_disease_state, anatomy_altering_surgery} -> w = max(w, 5)
        significant_negative = true                                             -> w = max(w, 3)
```

The floor classes are the *closest* available ids to the categories that must carry top weight,
each broader than the category itself. `chronic_disease_state` is the closest id for active
malignancy, so flooring it also elevates COPD, CHF, CKD, AF, cirrhosis, IBD, epilepsy and
bipolar. `anatomy_altering_surgery` is the closest id for major organ transplant, so flooring
it also elevates splenectomy, bowel resection and amputation. Narrowing would require content
detection, which is deliberately not used. Edit `FLOOR_TOP_CLASSES` to change this; it is a
clinical decision.

```
SWR_strict  = sum w(f) * represented(f)                      / sum w(f)
SWR_lenient = sum w(f) * [home in {node, excluded, episode}] / sum w(f)
```

`SWR_lenient` is the `c2`-comparable number. `SWR_strict` is the headline: a sentinel-grade
event collapsed to a count is a real loss of resolution. Report both, plus recall stratified by
tier and by class, weighted and unweighted.

**Hard gates (block the build).**

| Gate | Condition |
|---|---|
| `gate_T1` | every T1 fact represented (strict) |
| `gate_T2` | every T2 fact represented (strict); major organ transplant is hard-gated here |
| `gate_safety` | every `severe_adr_allergy`, `code_status_directive`, `infection_control_status` fact represented |
| `gate_signeg` | every `significant_negative` fact represented |
| `gate_swr` | `SWR_lenient >= --threshold` |

`--threshold` defaults to 0.995 rather than 1.0, as a documented tolerance for FM-4 (a
clinically distinct finding labelled `recurrent_episode` and collapsed into a rollup whose
parent label shares no content tokens with it). Read `KNOWN_FAILURE_MODES.md` FM-4 before
changing it. The tolerance buys attribution, not a green build: `gate_signeg` fails
independently on the same facts.

**Not tier-gated: active malignancy.** It lives in `chronic_disease_state` (T3), so a lost
malignancy fact is caught by `gate_swr` and flagged `critical` in the misses CSV, but has no
per-class hard gate unless you add the whole class to the gate set. That is broad. Open
decision, section 13.

**Advisory.** A T3 fact landing `episode`-only is a mis-tag candidate (the `PINNED_IN_ROLLUP`
signal). Extend the sentinel or disease-course net rather than fail the build.

```
python src/evaluation/f1_salience_recall.py
python src/evaluation/f1_salience_recall.py --subject <id>
python src/evaluation/f1_salience_recall.py --ledger-path P --comp-path P --out-dir D
```

Outputs `eval/salience_recall.json` (gate booleans, stratified scores) and
`eval/salience_recall_misses.csv` (the offending facts, weight-sorted). Exit 1 on any gate
failure. The path overrides exist so f11 can point the real metric at a perturbed copy.

### 3.2 f2_integrity.py -- structural and temporal integrity

Asks whether the view is *assembled* correctly: the right admissions in the right tiers, in
order, holdout excluded, ledger well-formed. Distinct from f1 (retention) and f4 (faithfulness).

The contract is `d_coordinator`'s window math, re-derived independently from
`cohort_admissions.csv` and checked against the artifacts:

```
n        = established admissions (holdout removed, promoted kept)
boundary = n - N_COMPACT_THRESHOLD
compacted (Tier 3)  : admit_seq <= boundary
hot window (Tier 2a): admit_seq in (boundary, n-1], last N_HOT_DISPLAY
current (Tier 1)    : admit_seq == n
```

| Gate | Checks |
|---|---|
| `gate_config` | `N_HOT_DISPLAY <= N_COMPACT_THRESHOLD` |
| `gate_holdout` | no still-held-out hadm appears in any compacted span |
| `gate_span` | `covered_hadm_ids` equals the expected oldest span; established `admit_seq` contiguous 1..n |
| `gate_consistency` | `n_admissions_compacted == len(covered_hadm_ids)`; `generated_hadm_ids == covered_hadm_ids` |
| `gate_current` | the current admission is not also shown compacted or hot, and is rendered |
| `gate_ledger` | served entries well-formed: known class and tier, text present |
| `gate_hotwindow` | hot window equals the expected set, within `N_HOT_DISPLAY`, no compacted admission shown hot |

**`gate_holdout` is vacuous after a complete replay.** `holdout.holdout_hadms()` subtracts
promoted admissions, so once every admission has been folded in the still-held set is empty and
the gate can catch nothing. To make it mean something, run it at a mid-replay checkpoint
(`replay_production.py --stop-after N`). `gate_consistency` and `gate_current` are the
fold-in-sensitive ones: a seeded fold-in builds `covered_hadm_ids` and `generated_hadm_ids` by
different routes, so they can diverge.

Outputs `eval/integrity.json` and `eval/integrity_violations.csv`. Exit 1 on any hard-gate
failure.

### 3.3 f3_compression.py -- compression against retention

Characterises the trade-off the compaction stage exists to make.

```
x = chars(prior_admissions_md) / chars(source of the compacted span)
    primary denominator   bhc_delta (the deduped novel content c1 had to condense)
    secondary denominator bhc_text  (the full BHC)
y = SWR over the compacted span (imports f1, single source for weighting and homing)
```

Chars, not tokens, to match the pipeline convention (`d1`'s audit ratio is `chars_out/chars_in`)
and to stay deterministic and dependency-free.

Hard gate `gate_compression_band`: `COMP_LO <= median(primary ratio) <= COMP_HI`. This catches
degenerate compaction in both directions -- near 1.0 means compaction did nothing, near 0 means
it destroyed the content. Tune the band from your first run's distribution (`--lo`, `--hi`); the
shipped values are calibrated to the reference corpus and will not transfer.

Advisory, never gating: a patient with aggressive compression *and* `swr_lenient < 1.0`, where
loss concentrates exactly where compression is hardest. f1 already gates the real loss.

Outputs `eval/compression_retention.csv` (the per-patient scatter) and `.json`. Exit 1 on the
band gate.

---

## 4. Tier 1: the LLM-judge metrics

All bind `config.model_for("faithfulness_validator")`. Swapping that one spec changes every
judge in the suite, and invalidates the calibration (section 9).

### 4.1 f4_faithfulness.py -- atomic-claim faithfulness

Decompose each generated unit into atomic claims, label each Supported, Not-Supported or
Not-Addressable.

```
faithful_fraction = supported / (supported + not_supported)
```

Not-Addressable is excluded from the denominator and logged.

**Compositional sourcing is the design decision that matters.** Each unit is judged against
**exactly what its generator consumed**:

| Unit | Source = its generator's input | Bounded |
|---|---|---|
| `tldr` | admission count + Prior Admissions paragraph + all established per-admission paragraphs (d2's exact input) | yes |
| `prior_admissions` | the retention ledger + the covered admissions' `bhc_delta` (c1's exact input) | grows with the aged span |
| `current_episode` | the current admission's `bhc_text` (d1's input) | yes |
| `hot_<seq>` | that admission's `bhc_text` (d1's input) | yes |

Every source is bounded, and faults localise: a TL;DR error is a d2 fault, a paragraph error a
d1 fault. By transitivity, if every tier is faithful to its input, the assembled view is
faithful to source.

Two source choices to preserve if you modify this:

- d1 units are judged against `bhc_text`. 
- The TL;DR source **excludes the ledger**, because d2 never receives it. Adding it would let
  ledger-only facts pass that d2 could not have known, masking a real d2 hallucination.

**Modes.** `--units synth` (default) judges `tldr` and `prior_admissions`; the TL;DR is checked
against the paragraphs, so its score is *conditional* on those paragraphs being faithful.
`--units all` adds the paragraph-vs-`bhc_text` checks, discharging that condition and closing
the loop to compositional ground truth. Report which mode produced any number you quote.

**Scaling boundary.** Only `prior_admissions` has an unbounded source. `n_truncated` flags when
it hits the cap; that single unit is where per-fold or per-claim retrieval goes at production
depth. The rest of f4 stays bounded at any patient depth.

**Judge robustness.** A thinking judge needs a recovery cascade: empty output (thinking budget
exhausted) retries with thinking off; malformed JSON gets one reparse; a unit that still fails
is flagged `judge_error`, excluded from the metric and logged, never crashing the run. A rising
`n_judge_errors` is the signal to swap the judge.

**Synthesis is not hallucination.** The prompt tells the judge that faithful summarisation,
aggregation and counts that follow from the source are Supported, reserving Not-Supported for a
specific assertion the source contradicts or has no basis for.

Thresholds are mode-aware (`synth` 0.6, `all` 0.9) and **provisional** -- re-baseline from your
own clean pilot. By-unit-type fractions are reported so the synthesised tiers, where
hallucination risk concentrates, track separately from the near-verbatim paragraphs. Each
record carries `source_admit_seqs` and `source_hadm_ids`, and source blocks are delimited
`[admission seq=N hadm=H]`, so a Not-Supported claim maps back to its candidate admissions.
Resumable: skips already-judged `(subject, unit)` pairs unless `--fresh`.

Outputs `eval/faithfulness.jsonl` and `.json`.

### 4.2 f9_merge_faithfulness.py -- consolidation precision

b4's LLM consolidation folds several raw ledger entries into one line. "Faithfulness" here is
**not** "no facts lost" -- correct consolidation drops duplicates by design. It splits three
ways, and only one of the three is a new metric:

- **Recall (no critical loss)** is already covered by f1. SWR counts only pinned facts, so a
  correct duplicate-drop is never penalised and a buried salient fact surfaces as a loss. Do
  not build a second recall metric; for any recall check the denominator must be the salient
  set, which is f1's.
- **Precision (no fabrication)** is f9. Does a consolidated line assert anything its source
  members did not?
- **Grouping validity (did it merge two distinct conditions?)** is Tier 2 calibration, not a
  build metric. See section 13.

Members are recovered without an LLM re-run: `_merge_node` unions its members' `fact_ids`, and
b4 keeps the pre-consolidation ledger at `retention_ledger.raw.jsonl`, so a consolidated node's
members are exactly the raw nodes whose `fact_ids` are a subset of its own. Only nodes with two
or more members are real merges; singletons are skipped. The source is the member texts plus a
deterministic provenance line (member count, admission count), so a T4 count or date verifies
against provenance rather than reading as fabrication.

Gate 0.95, provisional. Shares the judge GPU with f4, so run it after f4. Outputs
`eval/merge_faithfulness.jsonl` and `.json`.

### 4.3 f5_pdsqi.py -- PDSQI-9 plus one

**PDSQI-9 is the published instrument.** It has nine attributes and no temporal dimension.
Event order is a first-class quality property of a longitudinal summary -- a correct fact in
the wrong place in the patient's course can be read as current when it is historical -- so a
tenth attribute, `temporal_coherence`, was added here. `synthesized` is also redefined from its
published sense to mean cross-admission integration.

**Report the provenance of the two parts.** Nine attributes carry external
validation; the tenth is a local extension that is not validated. 
Ten attributes, Likert 1-5, 5 best on every attribute including `stigmatizing`:

```
cited              claims traceable to source notes
accurate           no factual errors or hallucinations
thorough           all clinically significant events captured
useful             actionable for a physician seeing this patient
organized          logical structure and flow
comprehensible     clear language, appropriate abbreviations
succinct           appropriately concise, no unnecessary repetition
synthesized        integrates information across admissions coherently
stigmatizing       5 = no stigmatizing language, 1 = highly stigmatizing
temporal_coherence events presented in clinically logical order
```

The judge sees the rendered summary a physician sees, plus the ledger as the evidence base for
the source-dependent attributes. `accurate` and `temporal_coherence` overlap the deterministic
gates and f4 deliberately: **divergence between the judge and the deterministic checks is a
judge-reliability signal**, and the deterministic side is ground truth.

Outputs `eval/pdsqi10.jsonl` and `.json`. Exit 1 if the overall mean falls below `--threshold`.

### 4.4 f6_incoming_ref.py -- the incoming clinician summary as reference

The only human-authored reference in the repo. Every patient's current admission
carries a real clinician-written BHC: what a physician thought worth recording. f6 checks the
established ledger, built from prior admissions, against it on two axes from one comparison.

- **Coherence.** The established record must not be contradicted by the incoming summary. A
  contradiction is either a stale established fact or a genuine change, and is surfaced for
  review.
- **Salience validation.** Facts the clinician carried forward into the incoming summary's PMH
  or problem list are salient *by clinician judgment*. Did the ledger retain them? This is an
  external signal that tests the taxonomy, rather than a judge grading the taxonomy that
  produced its own input.

**Ground truth is `incoming_summary.jsonl`'s `bhc_text`**, the real unedited clinician text. 
The established side is the **retention ledger**, not `retention_facts.jsonl`: we
measure what the pipeline actually surfaces to a clinician doing an audit. This also keeps f6
orthogonal to f4, so a generation fault is not double-counted as a taxonomy fault.

**Why there is a tiering step.** The two sides speak different languages. The ledger is already
tiered; the BHC side is free prose. To compare like with like, the BHC's facts are tiered with
the *same* taxonomy instructions b1 used (`tax.render_for_prompt()` seeded into the extraction
prompt), and each BHC fact is tagged with its origin, `pre_existing` or
`arose_this_admission`. A fact that legitimately arose this admission could never have been in
a ledger built before it; comparing it is a category error. The comparable set keeps T1-T3 with
`origin=pre_existing`, and drops all T4 (implicit in their T3 parent) and T5.

In development, "current admission" is the held-out latest one. In production there is no
holdout: it is the new admission being folded in. f6 is not holdout-specific in concept, only
in its dev-time source of the BHC.

Track only, never a hard gate. Outputs `eval/incoming_reference.jsonl` and `.json`.

### 4.5 f10_nothink_audit.py -- judge-health audit of the c3 gate

Deterministic (no LLM, no GPU) but audits a Tier-1 judge, so it sits in both tracks.

c3 is the **data-path** faithfulness gate: a judge over the compacted Prior Admissions
paragraph, whose failures route to c4 for automatic correction. Its hang-containment cascade
can fall back to `think=False`, and a no-think judge rubber-stamps `supported: true` more
readily. Given the measured Not-Supported PPV of roughly 0.24 (section 9), automated gating on
this judge is inappropriate and human-reviewed triage is the supported use. f10 is the standing
mechanism for that.

It reads `compaction_faithfulness.jsonl` and does two things:

- **Tracks the no-think pass rate**, and separately the **step-4 (ENTAIL) no-think rate**. The
  latter is the dangerous kind, because the verdicts themselves were rubber-stamped. Band
  `--max-step4-rate`, default 0.05. A rising rate means the judge is degrading or the cohort is
  hardening. Track and alert, never block: a no-think pass is a review flag, not a failure.
  `--strict` opts into a non-zero exit for CI alerting.
- **Emits a human-review worklist**, one record per no-think pass, with a review focus that
  depends on which step degraded:

| Degraded step | What it means | Review focus |
|---|---|---|
| step 4, ENTAIL | the verdicts were rubber-stamped (worst) | verify each accepted statement against source |
| step 2/3, classify/filter | statement *selection* used a weak step | check what was filtered out |
| step 1, extract | the statement list came from weak extraction; verdicts were judged with reasoning | check the source for MISSED facts |

`--source` prints the covered admissions' BHC inline for review. Outputs
`eval/nothink_audit.json` and `.jsonl`.

---

## 5. Tier 2: validating the harness

### 5.1 f11_metric_validation.py -- known-answer perturbation harness

**The highest-leverage script in the suite.** Every other number rests on the metrics actually
detecting the failures they claim to. f11 injects one labelled corruption into a sandboxed copy
of the system's own artifacts, runs the real metric against the copy, and asserts the expected
gate or label fires. A miss is a metric blind spot. It needs no baseline system.

| Corruption | Applied to | Expected detector |
|---|---|---|
| delete a T1 node | ledger | f1 `gate_T1` fails |
| delete a T2 node (`anatomy_altering_surgery` preferred) | ledger | f1 `gate_T2` fails |
| delete a safety-class node | ledger | f1 `gate_safety` fails |
| collapse a gated node to rollup | ledger | f1 tier gate fails, `n_rollup_only` rises |
| drop a `negative_kind` fact | ledger | f1 `gate_signeg` fails |
| inject a fabricated prose claim | view | f4 labels it `not_supported` (`--with-f4`, GPU) |

Two design points worth preserving if you extend it.

**Targets are load-bearing, not arbitrary.** The harness replicates f1's pinned-fact set and
c2's homing in-process to find a node some pinned compacted-span fact actually homes to, then
strips exactly that representation, escalating until the fact no longer homes to
`node`/`excluded`. That is what makes the known answer *known*.

**Detection is per-fact, not per-gate.** The stripped fact must appear as a new row in the
perturbed run's `salience_recall_misses.csv` -- present perturbed, absent at baseline. This is
immune to whatever the cohort-wide aggregate gate was already doing, so a pre-existing
`gate_signeg = False` cannot mask the signeg case. The aggregate value is still recorded for
context.

Nothing real is mutated: perturbed copies and the metric's JSON all live under a sandbox dir,
reached through f1/f4's `--ledger-path` / `--view-path` / `--out-dir` overrides. Confirmation
runs the real f1/f4 as a subprocess, not a reimplementation.

Outputs `eval/ablations/known_answer.json`. Exit 1 if any applicable corruption was missed;
`--soft` forces 0.

### 5.2 f12_frontier.py -- compression-retention frontier and operating-point gate

Replaces "we beat baseline X" with "here is our operating point, and it degrades gracefully",
then gates regressions on that point.

```
x = median compression ratio (f3-style)
y = cohort SWR_lenient (f1 weighting and homing)
```

**Default mode: gate, deterministic, no GPU.** Scores the existing production compaction at the
production `N_COMPACT_THRESHOLD`. Two parts:

- **Floor.** `SWR_lenient` at the production compression must clear the floor. The floor is
  **pinned to f1's measured `swr_lenient_weighted`**, not to a literal 1.0. f1 and f12 compute
  the same quantity over the same ledger, so the appropriate bar is "f12 must not report worse
  retention than f1 just measured". A hardcoded 1.0 can never pass while any genuine loss
  exists and turns the gate into a permanent red light. `--floor` overrides.
- **No-dominance.** The release's `(compression, SWR)` point must not be Pareto-dominated by
  the previous accepted release's stored point: dominated iff the previous had compression
  lower-or-equal and SWR higher-or-equal, strictly better on at least one. A dominated point
  bought compression by sacrificing salient retention, or bought nothing.

On a pass the current point becomes the new baseline, unless `--no-update-baseline`.

**The stored baseline carries a `taxonomy_version`, and f12 reads it.** On a mismatch the
dominance gate is skipped and reported as `baseline_stale`; the absolute floor still applies;
a passing run re-baselines. This is not fussiness. A taxonomy bump can change what is served
, so cross-version dominance comparison is not a weaker signal, it is a
meaningless one. Before this check, a stale baseline silently gated two taxonomy versions. See
`KNOWN_FAILURE_MODES.md` cross-cutting theme 4: **one side of every staleness comparison must
be the live code version, never a second stored value.**

**`--sweep` mode: study, GPU.** Re-compacts at each `--thresholds` boundary (re-runs c1 only,
never d2/d_coordinator/d3), scores compression, SWR and prose metrics, and emits the curve.
Sample-scoped.

The sweep scores the **prose**, not just the ledger, and this is the point that makes the curve
readable. Ledger-homed SWR is computed by looking each pinned fact up in the ledger, which the
compaction threshold cannot change, so SWR is flat near 1.0 at every threshold and cannot
express a tradeoff. The sweep therefore also scores per-fact presence and contradiction against
`prior_admissions_md` via `eval_fact_presence`. Those move.

Sweep the threshold **in the harness only**; never mutate production config, and preserve the
`N_HOT_DISPLAY <= N_COMPACT_THRESHOLD` invariant. Because it re-runs c1, `--sweep` is a
nightly/release job, not a per-commit gate.

Outputs `eval/ablations/frontier.json`, `frontier.csv` and `frontier_baseline.json`.

### 5.3 eval_fact_presence.py -- shared prose fact-presence judge

Given a text and a list of candidate facts, decide for each whether the text states it. One
judge call per (patient, text) covers all facts for that text.

Distinct from `f4.judge_unit`: f4 *decomposes* a summary into its own atomic claims and labels
each; this checks a list of specific, externally supplied facts against a text. Two verbs share
the judge and recovery cascade: `fact_presence` (does the text state it) and
`fact_contradiction` (does the text assert something incompatible; silence is not a
contradiction).

Consumers: `f12 --sweep`, the ledger-context ablation, the position-bias study. Kept in one
module so the judge cannot drift between them. Run the file directly for a two-fact smoke test.

### 5.4 The studies

Code under `src/studies/`. One-off characterisations, not
standing gates: each answers "does this component earn its place" once. The reference-run
numbers below are quoted so a fork knows what shape of answer to expect, not as targets.

**`ledger_context_ablation/run_ledger_ablation.py` -- does the in-context ledger earn its
place?** Re-runs c1 with the ledger removed from the compactor *prompt* (`ledger_facts=[]`),
holding span, model, mode and temperature constant. The ledger artifact is untouched; only what
the writer may see changes. Judges every pinned fact for contradiction and for presence against
both paragraphs, and scores faithfulness and compression alongside.

Reference run, 15 deepest-span patients, 603 pinned facts, mode `full`: **contradicted pinned
facts rose from 17 to 28 when the ledger was removed from the prompt, roughly a 39% reduction
attributable to supplying it** (9 patients worse without, 4 better, 2 unchanged). Faithfulness
0.984 against 0.963; compression effectively unchanged, so the guard costs nothing in length.
**The premise is validated and is also shown to be insufficient**: 17 contradictions survive
with the ledger supplied, so the prompt reduces the problem and does not eliminate it. The
guarantee is the ledger as a *stored record*, not as a prompt ingredient.

Two readings to avoid. The prose fact-presence rate is near-identical across arms (0.392 against
0.388), which is **not** the ledger failing: the compactor is explicitly told not to re-list
facts the ledger already holds, so an equal or lower mention rate with the ledger supplied is
the instruction working. And faithfulness is scored against a source that itself includes the
ledger, so it is confounded across arms and is not an independent check. Contradiction is the
clean measure here, which is why `eval_fact_presence` carries a `fact_contradiction` verb.
Flagged pairs land in `flagged_contradictions.csv` and are one judge's opinion, unadjudicated.

**`position_bias/run_position_bias.py` -- is retention driven by salience or by recency?**
Logistic regression of prose fact-presence on `salience_tier` (ordinal) and normalised source
position (`admit_seq` mapped 0 = oldest to 1 = newest within the patient). Uses **prose**
retention because the ledger retains pinned facts near-trivially, leaving no variance to
explain. This gives the salience-versus-recency contrast without building a recency baseline.

Reference run, 1736 pinned facts across all 75 compacted patients: **position dominated and tier
did not register.** Position coefficient 1.347 (p = 0.0002), tier -0.024 (p = 0.64), the two
uncorrelated at 0.033, so the contrast is genuine and not one variable standing in for the
other. Modelled survival rises from about 41% for the oldest admissions to about 57% for the
most recent, over the observed position range. Overall presence rate 0.464.

Three qualifications the write-up carries, and any citation should carry them too. Pseudo
R-squared is 0.006, so **both predictors together explain very little** -- recency is real,
clear and modest, and most of what decides prose survival is captured by neither variable. The
observed position range is only 0.00 to 0.46, because only admissions old enough to have been
compacted are eligible, so the newest admissions are structurally absent and the figures are not
projected beyond that range. And per-tier cells are thin (T4 n=12, T1 n=124), so the per-tier
table must be read with its n column.

**This is the most pivotal empirical result in the framework.** Clinical importance does not
decay with time, but prose retention does. A system serving only the narrative would quietly
drop older salient facts. The ledger carries facts forward on clinical class rather than
position, which is why the two exist side by side. 

**`generalization/`.** The external-validity capstone: `draw_cohort_gen.py` draws a disjoint
unseen cohort, `replay_production.py` replays the fold-in sequence, `compare_generalization.py`
emits the dev-versus-unseen gap. See section 6.

**`weight_sensitivity/`.** Sweeps `TIER_WEIGHT` and `FLOOR_TOP_CLASSES` to test how much SWR
depends on the weighting choice. **The reference run returned NOT ESTABLISHED.** Across flat, linear, 
geometric and no-harm schemes, cohort SWR moved by only 0.0017 and the gate verdicts were identical 
- but every loss in that cohort sat in one tier (T4, 8 events, with T1/T2/T3/T5 clean). A weighting scheme 
cannot be shown insensitive on a cohort with no losses to re-weight. 
**Do not cite it as calibration; re-run it against an f11 perturbed ledger with
losses spread across tiers.**

What holds independently of the sweep: f1's `gate_T1`, `gate_T2`, `gate_safety` and
`gate_signeg` are count-based and weight-independent. Only `gate_swr` depends on the tier
weights, so **catastrophe detection does not rest on the weighted scalar at all.** The tier
*label* is load-bearing throughout the pipeline; the numeric weights are evaluation-only.

**`ledger_studies/`.** Contains patient information and is gitignored. Section 10.

### 5.5 Why there is no external baseline arm

Four comparison arms were initially considered and dropped. The reasoning is kept if a reviewer 
ask for them again: each was "us against a deliberately weaker system", and a strawman's result
is known in advance. 
1. **No-compaction** (concatenate the span verbatim) scores 1.0 on retention
and faithfulness *by construction*, since verbatim source cannot drop or hallucinate.
2. **Recency-only** is answered better by the position-bias diagnostic on real output than by
building the baseline. 
3. **Relevance-RAG** adds chunk-to-fit, a scaling trick, not a content-selection principle, 
and otherwise merely pits semantic relevance-to-today against the clinical taxonomy. 
4. **long-context dump** demonstrates only that context width is a technical constraint, which is already known.

The replacement is five angles with unknown outcomes: ablate our own components
(5.4), meta-test whether the metrics detect what they claim (5.1), characterise the tradeoff
frontier (5.2), diagnose whether retention is salience- or position-driven (5.4), and test
generalisation to unseen patients (6).

### 5.6 The clinician review tools (`src/clinical_review_tools/`)

Not metrics and not gates. Deterministic, no GPU, read-only over the artifacts, and built to
put a bounded amount of text in front of a clinician. They exist because the consolidation
grouping axis has no automatic signal (section 11), so the only way to say anything about it is
to make a human read a sample, and the only way to make that affordable is to rank what they
read. Output goes to `<data>/output/review_tools/`.

There are **three merge surfaces**, with different mechanisms and different failure modes, and
they must be sampled and reported separately rather than pooled:

| Surface | Mechanism | Merges (reference cohort) | Merges across class? |
|---|---|---|---|
| `b2_cluster` | token overlap within one `retention_class`, then identity split; latest member supplies the text | 1326 | no, structurally |
| `b2_consolidate` | Jaccard / condition key / phrase containment within a `_MERGE_GROUP` bucket | 220 | **yes, by design (126 of 220)** |
| `b4` | LLM T3 state roll-up | 213 | no |

**`b2_merge_worksheet.py` and `b4_merge_worksheet.py`** emit adjudication sheets, one row per
merge member with the merged line repeated, plus blank verdict and note columns. b2's is
recomputed by calling b2's own functions, because the stored ledger cannot tell clustering from
consolidation: a node with `n_mentions=5` may be five facts in one cluster or 2+3 from
consolidation. b4's members are recovered from the artifacts by the `fact_ids` subset rule f9
uses, and the sheet asserts that recovery is exact before it can be trusted.

**`merge_loss_triage.py`** is the one that makes the axis tractable. For every merge on all
three surfaces it asks whether a high-salience concept carried by a member is absent from the
merged text, then whether it was recovered anywhere else the clinician is served -- any node in
the ledger, or anywhere in the served prose, and finally whether it is in `bhc_text` at all. 
Only the intersection (dropped, unrecovered, and in source) is surfaced for review. 
Reference cohort: 1759 merges, 115 loss events, **10 surfaced, of which 2 were rows with salient true losses
clinical salience** (a coronary stent buried in a 'coronary artery disease' node, a VRE bacteraemia hidden among
multiple bacteremia episodes).

Two cautions carried in the tool's own docstring:

- **A `REVIEW` row is a candidate, not a defect.** Follow the patient's later course and read
  the source before calling anything a loss. Of the reference cohort's 10, four were
  `code_status_directive` supersessions that read exactly like a lost DNR/DNI and were in fact
  correct latest-wins behaviour on a documented reversal -- `_SPLIT_EXEMPT` working as intended.
  Two more were artefacts of the keyword screen.
- **`CHECK_SOURCE` rows are not fabrications.** A concept in no served artifact *and* not found
  in `bhc_text` looks like an invention, but b1 paraphrases, so a legitimate rewording is
  indistinguishable from one. Read the note.

---

## 6. The generalisation run

The development cohort drove all taxonomy and prompt tuning, so scores on it are not evidence of
generalisation. Draw a cohort of new patients (disjoint `subject_id`s, same stratified
deep-admission tail, new seed), run the **full pipeline with frozen config** -- taxonomy
version, prompts, thresholds, no tuning -- and compare. A large gap is overfitting.

This is a **between-patient** comparison. e1's incoming admission is only a within-patient one.
"holdout" means the within-patient incoming admission, "generalisation" means the unseen cohort.

**Isolation.** `config.py` reads an opt-in `LONGSUM_NAMESPACE` env var. When set, all *file*
artifacts live under `data_<ns>/` instead of `data/`. Unset is byte-identical to a normal run.

**This namespaces files only. Postgres is shared.** Run only the file-only stages under a
namespace. Do not run `a4_load_and_embed`, `db_persist_*`, `d3_load_tiers`, `e2_embed_incoming`
or the API under one; they write shared tables and would mix cohorts. f1/f4/f5 are file-based,
so scoring needs no DB.

```bat
cd clin_longsum\src
set LONGSUM_NAMESPACE=gen

python studies\generalization\draw_cohort_gen.py --n 16 --seed 2026

:: frozen file-only pipeline: same order as PIPELINE.md's full rebuild,
:: minus the DB stages. Confirm each prints  RUN NAMESPACE ACTIVE: 'gen'
python b1_retention_extractor.py --fresh
python b2_build_ledger.py --fresh
python b4_consolidate_ledger.py
python c1_compactor.py --fresh
python c3_faithfulness.py --fresh
python c4_correct.py
python c2_retention_validator.py --fresh
python d1_paragrapher.py --fresh
python d2_tldr.py --fresh
python d_coordinator.py --fresh

python evaluation\f1_salience_recall.py
python evaluation\f4_faithfulness.py --fresh --units all
python evaluation\f5_pdsqi.py

set LONGSUM_NAMESPACE=
python studies\generalization\compare_generalization.py --gen-namespace gen
```

**Known gap.** e1 is not in `foldin_patient.CHAIN`, so a straight replay does not exercise the
incoming-summary stage and f6 has nothing to score in the generalisation namespace. The
`--stop-after 1` checkpoint recipe in `replay_production.py`'s docstring is the workaround, and
it is also what makes `gate_holdout` non-vacuous (section 3.2).

**Reference result**, for shape only, not as a target:

| Metric | Dev | Unseen | Gap |
|---|---|---|---|
| SWR lenient | 0.9965 | 1.0000 | -0.0035 |
| SWR strict | 0.9960 | 0.9955 | 0.0005 |
| Faithful fraction | 0.991 | 0.989 | 0.002 |

Gaps near zero, in the direction of no overfitting. **The unseen cohort was 10 patients.** That
is too small to be more than a sanity check. Run a larger one.

---

## 7. Execution

### Run order

Tier 0 first, cheapest and blocking. Then Tier 1, sharing the judge GPU serially. Tier 2 on
release.

```
f1  ->  f2  ->  f3                      Tier 0, deterministic, blocks
f4  ->  f9  ->  f5  ->  f6              Tier 1, judge; f9 after f4 (shares GPU)
f10                                     Tier 1/2, deterministic, no GPU, any time
f11 ->  f12                             Tier 2, release; f11 --with-f4 needs GPU
f12 --sweep, studies/                   Tier 2, GPU-heavy, nightly or release
```

f3 imports f1 and f9 imports f4's judge, so those pairs must stay consistent. There is no
`f7`, and `f8` was superseded by f11/f12 plus the studies; both names survive in older
docstrings and mean the judge-calibration subproject and the dropped ablation suite
respectively.

A top-level `f0_run_eval.py` orchestrator -- take a `--tier`/`--trigger`, run the relevant
scripts, aggregate their JSON into one report, return non-zero iff a Tier-0 gate failed -- is
**not shipped**. That single exit code is what CI should block on, so it is the first thing to
write when wiring this into your own CI.

### Run modes

| Trigger | Runs | Blocks |
|---|---|---|
| per-commit / pre-promote | Tier 0, frozen golden subset | yes |
| fold-in (physician approval) | f1, f2 for one `--subject` | yes, that patient's serve |
| nightly | Tier 0 + Tier 1, full cohort | Tier 0 only; Tier 1 alerts |
| model / prompt / taxonomy change | Tier 0 + Tier 1; if the judge changed, recalibrate first | Tier 0 only |
| release / quarterly | + f11, f12, the studies | f11 and f12 exit non-zero |

### GPU discipline

Generation model and judge must never be co-resident. Use `keep_alive=-1` for the duration of a
batch, then unload and clear VRAM at batch end; the shipped f-scripts do this and the pattern
should be copied into anything new. In `f12 --sweep` and the ledger ablation, unload the
compactor before the judge pass.

### Reading the outputs

Everything lands under `data/output/eval/` (or `data_<ns>/output/eval/`):

```
salience_recall.json        gate booleans + stratified scores        <- start here on a red
salience_recall_misses.csv  the offending facts, weight-sorted
integrity.json              structural gates
integrity_violations.csv
compression_retention.csv   the (compression, SWR) scatter
faithfulness.jsonl/.json    per (subject, unit) claims + labels
merge_faithfulness.*        per merged node
pdsqi10.*                   per patient, scores + rationales
incoming_reference.*        coherence + salience validation
nothink_audit.json/.jsonl   judge-health rates + the human worklist
ablations/known_answer.json metric detection rates
ablations/frontier.*        operating point, curve, stored baseline
```

The studies write alongside, under `data/output/studies/<name>/`, each as a machine-readable
JSON plus a `FINDINGS.md` aimed at a non-specialist reader:

```
position_bias/           position_bias.json + FINDINGS.md
ledger_context_ablation/ ledger_context.json + flagged_contradictions.csv + FINDINGS.md
weight_sensitivity/      weight_sensitivity.json + FINDINGS.md + _runs/
generalization/          generalization_gap.json + FINDINGS.md
```

---

## 8. Gate-failure runbook

A Tier-0 failure is a **clinical-safety stop, not an infra flake**. The deterministic metrics
reproduce exactly on a frozen cohort, so a red gate means a real regression in what the system
would put in front of a clinician.

**Fires automatically on a red gate.**

- **Promotion halts.** The candidate version is not loaded to serving; the last-known-good
  `tiered_view` stays live. `d3` is per-patient atomic and idempotent, so serving is untouched
  by a blocked candidate.
- **CI run marked failed**, with `salience_recall.json`, `salience_recall_misses.csv` and the
  run's provenance (model tag, `prompt_version`, `TAXONOMY_VERSION` from the AuditRecords)
  attached.
- **Alert to the on-call channel** carrying which gates failed, `n_genuine_losses` versus
  `n_rollup_only`, the top weighted critical violations, and a diff of the gate booleans
  against the last-known-good baseline.

**Actions, in order.**

1. **Confirm; do not retry to green.** Re-run on the frozen cohort. It is deterministic, so it
   reproduces. If it does *not* reproduce, the input artifacts drifted (stale ledger or
   compaction). Rebuild those, not the gate.
2. **Classify** from the JSON: `n_genuine_losses` (home `none`, fact absent from the ledger)
   versus `n_rollup_only` (a pinned fact collapsed to a frequency rollup). Different root
   cause, different owner.
3. **Localise** via the AuditRecord triple (model, prompt, taxonomy version): a model swap, a
   prompt bump, a taxonomy bump, or an input-data change.
4. **Route.**
   - genuine loss (extraction or b2) -> pipeline owner: fix b1 extraction or b2 consolidation,
     then re-run `b1/b2 -> ... -> d3 --subject`.
   - rollup-collapse or mis-tag, a sentinel-grade event filed as `recurrent_episode` -> taxonomy
     owner: extend `is_sentinel_episode` / `is_disease_course_event`, bump `TAXONOMY_VERSION`,
     re-run. This is the `PINNED_IN_ROLLUP` signal.
   - `gate_swr` red with no per-class gate red -> aggregate regression: check whether it is one
     high-weight loss or many small ones (the misses CSV is weight-sorted), then route by class.
5. **Remediate and re-run.** Promotion proceeds only when green.
6. **Escalate** a confirmed T1, safety-class, or floored-weight loss (active malignancy or
   transplant) to the clinical owner as a safety incident before any further action.

**Waivers.** No silent skip. A gate may be waived only by an explicit, audited, time-boxed
waiver signed by the clinical owner, recorded with rationale and expiry. The gate log is part of
the evidence trail.

**Rollback.** If a bad version reached serving, re-load last-known-good artifacts via `d3`
(per-patient atomic, idempotent).

**Tier-1 breaches differ.** Tolerance-band alerts, not hard stops. Re-run at `temperature=0`,
check for judge drift, cross-check against the Tier-0 gates, which are ground truth for
accuracy and retention. Promote-with-alert is acceptable for a Tier-1 wobble; a Tier-0 red is
not.

In a small team the MLOps, pipeline, taxonomy and clinical-owner roles collapse to one or two
people. The routing above still says which fix to reach for.

---

## 9. Judge calibration -- the Tier-2 prerequisite

**Every Tier-1 number is unqualified until the judge behind it has been measured against
clinician labels.** Calibration converts "trust my LLM judge" into an agreement number. It is
run as a **separate self-contained subproject and is not shipped in this repo**, because it
depends on an externally credentialed corpus and on a different serving stack.

**Why this is not optional.** The judge bound to `faithfulness_validator` is not only an
evaluator: it also sits at **c3, on the build path**, where `c4_correct.py` is gated on the
presence of `failed_statements`. Its verdicts trigger automatic edits to text a physician
reads, with no human in the loop. Combined with FM-6 (the correction steps cannot see the
source note, so c4 can only *soften* a statement, never delete it), the realistic failure is
not a fabrication surviving review. It is **correct statements being hedged into vagueness at
scale by a judge that was wrong about them.**

**Optimise recall, not accuracy.** Because a human reviews the positives, a false positive
costs review minutes while a false negative lets a fabrication through unseen. A judge with
better overall accuracy but worse recall is a worse judge for this pipeline.

**Report chance-corrected agreement, never raw agreement.** One large evaluation found kappa
deflation of 33 to 41 percentage points across 21 judges. Raw agreement badly overstates
discrimination.

### How to calibrate yours

**Route A -- qualify against VeriFact-BHC.** Cheap, and what the reference implementation did.
VeriFact-BHC is an existing clinician-labelled benchmark of propositions from MIMIC-III brief
hospital courses, each rated Supported / Not Supported / Not Addressed against the patient's
EHR, so you do not need to build a labelled set from scratch.

1. Obtain VeriFact-BHC from PhysioNet under the usual credentialing and clone the upstream repo
   (MIT licensed).
2. Reproduce the pipeline *shape* -- decompose, retrieve, judge, score -- against **your own**
   serving stack. Upstream assumes a multi-GPU Docker deployment (vLLM, qdrant, redis/rq,
   containerised embed and rerank services). If you run something simpler, reimplement the shape
   rather than the infrastructure: hybrid dense-plus-sparse retrieval over sentence-level EHR
   facts, a reranker, then your judge. Keep VeriFact's judge prompt, verdict schema and verdict
   labels byte-for-byte; only the transport should differ.
3. Use their published best hyperparameters as defaults (`retrieval_method=rerank`,
   `top_n=100`, `reference_format=absolute_time`, admission-scoped reference, sentence-level EHR
   facts) so your numbers sit on the same axis as theirs.
4. Score with **Gwet's AC1 and multiclass MCC**, and report per-label sensitivity, specificity,
   PPV and NPV. Gate on AC1; the reference used a provisional threshold of 0.6.
5. Treat any comparison to VeriFact's published agreement figure as a direction-and-magnitude
   sanity check, not a reproduction, unless you run their exact judge model.

**Route B -- build your own labelled set on your own notes.** More expensive, and the only
thing that establishes site-specific validity. Worth doing once Route A shows the judge is in
the right region.

### What the reference calibration produced

n = 183 stratified propositions. Adopted the `clarified_v2` prompt.

| | verbatim prompt | adopted prompt |
|---|---|---|
| Percent agreement | 0.689 | 0.781 |
| Gwet's AC1 | 0.595 (gate FAIL) | **0.720** [0.631-0.798] |
| Multiclass MCC | 0.372 | **0.514** |
| Not Supported, PPV | 0.18 | **0.24** |
| Not Supported, recall | 0.60 | **0.40** |
| Not Addressed, recall | 0.296 | 0.667 |

Three things to take from the table rather than from the headline AC1.

- **Read the recall column with the confusion matrix, not alone.** The drop from 0.60 to 0.40 is
  not a regression: it is the cost of Not-Addressed recall rising from 0.296 to 0.667. The
  clarification taught the judge to use Not Addressed properly, moving borderline cases out of
  Not Supported. Of the 15 gold Not-Supported propositions the adopted prompt missed 9, but 6
  went to Not Addressed and only 3 to Supported. It failed to *endorse* 12 of 15 and waved
  through 3. On the PPV side, 19 of 25 predicted Not Supported were errors, of which 12 were
  genuinely Supported and 7 were Not Addressed. The over-flagging is real; the exact-label
  recall overstates the under-detection. **The Not-Supported / Not-Addressed boundary is the
  hard part of this task, and it is a labelling-scheme distinction more than a safety one.**
  Decide which of the two your downstream stage acts on before tuning for either.
- **Prompt wording moved the judge more than any model change would have.** The verbatim prompt
  failed the gate outright; one added paragraph clarifying the Not-Addressed distinction took
  AC1 from 0.595 to 0.720. A third variant scored worse (AC1 0.699, MCC 0.489) and was
  rejected. Tune the prompt, measure every variant, and keep the losers on record.
- **Not-Supported support was 15 of 183.** The PPV rests on 25 predicted positives, of which 6
  were correct. Small-n. Widen the labelled set before treating 0.24 as precise.

**The operational consequence, which propagates through the whole framework:** at PPV ~0.24,
**a flagged statement justifies looking; it does not establish a fabrication.** Only a check
against the source note does. Automated gating on this judge is inappropriate; human-reviewed
triage is the supported use. That is why f10 exists, why f4/f5/f6/f9 are tracked rather than
gated, and why the deterministic Tier-0 gates are the safety floor.

### Two judge-selection points that bear directly on the metrics

**f4 is not an independent check on c3.** Both bind the same `faithfulness_validator` role, so
the same model with different prompts sits on both sides of the question. A good f4 score does
not establish that c3 judged well. Either treat Tier-1 numbers as provisional and correlated
with c3, leaning on f10 plus human review for independent signal, or add a second judge role to
`MODELS` and bind f4/f5/f6/f9 to it. The latter is a code change, not a config change, and it
doubles judge VRAM pressure.

**Lineage independence is a property of the (generator, judge) PAIR, and of their backbones
rather than their names.** Re-check it whenever you change the GENERATION model, not only the
judge. In the reference configuration the two share a backbone family; this was accepted
knowingly on VRAM grounds and is recorded rather than assumed away.

Selection criteria in full, including structured-output reliability and the recovery cascade it
forces: `MODEL_SELECTION.md` sections 3 and 4.

---

## 10. What the gates cannot see

The suite is well covered for **content** faults: a fabricated claim is caught by f4 or c3, a
dropped salient fact by f1, a mis-assembled view by f2. It is structurally blind to one class of
fault, and a fork should know this before presenting the numbers.

**Axis mis-assignment on faithful text (FM-9).** b1 extracts a fact whose text is supported by
the note and assigns it a `retention_class` from an unrelated part of the taxonomy. The schema
is enum-constrained, so the label is always *valid*; nothing in the pipeline checks whether it
is *right*. `normalize_fact` repairs only surveillance promotion and `recurrent_episode`
reclassification, so no taxonomy function reaches a wrong-but-valid class. `floor_tier` then
applies the wrong class's default tier, compounding it. No validator fires, `prune_reason` is
often None, nothing lands in `dropped[]`, and every downstream stage carries the label without
question.

**Why no gate catches it.** f1 scores whether a *pinned* fact is represented, using the stored
class both to decide pinning and to weight it. A fact given the wrong class is scored against
the wrong expectation, consistently, on both sides. The error is invisible to a metric that
takes the label as given.

**The consequence is asymmetric.**

- **Into a safety class:** the fact gains a T1 pin that the safety-robust branch makes
  unreachable by any exit but `certainty=ruled_out`, so it is physician-immovable. Measured on
  the reference dev cohort: 10 of 69 `severe_adr_allergy` facts (14.5%) were not drug or
  substance reactions -- PICC lines, an LVAD, a CABG, a hip replacement, an ileocecectomy. This
  is the accepted cost of the FM-8 fix. It errs safe.
- **Out of a safety class:** the fact loses the pin and can be pruned by a branch that reads
  only `retention_class`. A drug-intolerance fact routed to `altered_immune_status` was pruned
  for a reason that had nothing to do with its tier, and was never restated across eight
  admissions.

**How much of the taxonomy is separable at all.** Three independent detectors -- taxonomy
self-collision against the classes' own written examples, mined-anchor recall, and embedding
kNN label purity across three embedding models -- converged on the same rank order. Condensed
kNN purity, k=10:

| Class | Purity (range across 3 models) | Reading |
|---|---|---|
| `code_status_directive` | 0.91-0.93 | cleanly separable |
| `chronic_disease_state` | 0.82-0.83 | separable |
| `implanted_device`, `transient_event` | 0.67-0.71 | usable |
| `altered_immune_status`, `anatomy_altering_surgery` | 0.60-0.69 | usable |
| `infection_control_status` | 0.42-0.46 | weak |
| `sentinel_event`, `recurrent_episode` | 0.37-0.44 | **not lexically separable from `chronic_disease_state`** |
| `permanent_condition` | 0.25-0.27 | **not separable** |

The control matters as much as the finding: a DNR/DNI probe filed to `code_status_directive` in
42 of 42 cases. **Perfect single-class consistency is achievable on this model and this prompt**,
so scatter elsewhere is a property of the concepts, not an artefact of the method.

Three consequences for how you report your numbers.

1. **Disagreement inside the low band is expected, not a defect.** Concept scatter across
   `permanent_condition` / `recurrent_episode` / `sentinel_event` / `chronic_disease_state` is
   accepted in the reference implementation. Do not gate on it.
2. **Per-class recall is only as meaningful as the class is separable.** Quote f1's per-class
   stratification alongside the separability band, or a governance reader will over-read a
   number attached to a class the extractor cannot reliably identify.
3. **The three embedding models tested -- two biomedical, one general-purpose -- ranked the same facts
   as anomalous in nearly the same order** (Spearman rho 0.82-0.85), with the general model
   matching the clinical ones. There is no measurable advantage to domain-specific embeddings
   on this task, so do not spend the swap cost expecting one.

The lever here is **model selection and prompt design at b1, not a new gate.** Schema field
reordering was probed A/B and produced byte-identical output; with a thinking phase the model
reasons before the JSON begins, so field order is irrelevant and the rationale is post-hoc.
Thinking traces are not persisted, so the reasoning behind a wrong class cannot be audited
afterwards -- that is a logging change worth making early. Full detail: `KNOWN_FAILURE_MODES.md`
FM-9 and `MODEL_SELECTION.md` sections 1 and 2. The row-level detector write-ups contain patient
information and are gitignored.

---

## 11. Limitations

**No external human gold standard for the longitudinal artifact.** Nothing in the literature pairs a
multi-admission record with a curated longitudinal summary. Every number about the assembled whole (L3 in Section 1) 
is either judge-produced or self-referential. The one human-authored reference in the repo, f6's incoming
clinician BHC, covers a single admission and only tests *retention*, not summary quality.

**The only external clinician-labelled calibration is for the JUDGE, not the OUTPUT.** VeriFact
qualifies the instrument that scores the artifact; it never scores the artifact. It is also
**not shipped in this repo** -- it depends on separately credentialed data and a different
serving stack, so a fork must stand it up independently or knowingly report uncalibrated
numbers.

**The calibrated judge is not good enough to gate alone.** Not-Supported PPV of roughly 0.24
means roughly three in four flags are wrong. Human-reviewed triage is the supported use.

**c3 and f4 are correlated.** The same model on both sides of the build path (section 9). A
good f4 score partly reflects the judge's own dispositions.

**No human PDSQI panel has been run**, and one of the ten attributes is not a published one.
Nine attributes are PDSQI-9; `temporal_coherence` is a local extension with no external
validation (section 4.3). The design calls for periodic human ratings on 20-30 patients with an
error taxonomy (hallucination, omission, temporal error, salience mis-classification),
reporting judge-versus-human correlation and inter-rater agreement. Until that exists, PDSQI-9+1
is one model's opinion of another model's prose, on a scale that is nine-tenths validated.

**Consolidation grouping validity has been triaged, not measured.** f9 covers precision (does a
merge fabricate) and f1 covers recall (was a salient fact lost). Whether a merge put two
*distinct* conditions on one line is a third axis with no metric, because nothing in the
metadata separates a good merge from a bad one. What exists instead is a screen, `merge_loss_triage.py` 
(section 5.6):- 
- **It is a keyword screen over roughly forty curated concepts** (devices, surgeries,
  infection-control organisms, malignancy, immunosuppression, ADR, code status, major
  bleeding), not coverage. 
- **It detects omission, not conflation.** A merge that puts two distinct conditions on one
  line while naming both passes the screen. Those need the adjudication worksheets and a
  clinician. 
- **Minor caveat: There are actually two merges being surfaced: at b2 and b4** b2 merges are recomputed
  from `retention_facts.jsonl` with live code, so they reflect the current taxonomy (v2.12); b4
  numbers were computed from stored artifacts built at the previous version (v2.11). The risk is low and runs
  toward under-reporting, since the newer taxonomy pins more facts. Re-run both on any taxonomy bump before quoting them.

**The in-context ledger reduces contradictions but does not eliminate them.** 17 pinned facts
were still contradicted by the prose with the ledger supplied to the compactor, against 28
without it. The prompt is a mitigation, not a guarantee.

**The tier weighting is uncalibrated.** The weight-sensitivity sweep returned NOT ESTABLISHED
because the reference cohort's losses all sat in one tier, so there was nothing to re-weight
(section 5.4). The hard gates are count-based and unaffected, but any claim that the SWR scalar
is robust to the weighting choice is currently unsupported.

**Class labels are unvalidated where the classes are not separable** (section 10). Roughly a
seventh of the reference cohort's safety-class facts were not drug reactions.

**A dropped fact and a correctly pruned fact look identical from outside.** f1 only counts
*pinned* facts, by design, so anything the taxonomy declined to pin is out of scope for every
retention metric. If your taxonomy under-pins, no gate in this suite will tell you.

**Metric coverage is validated only for the corruptions f11 injects.** A blind spot outside that
list stays invisible. Extend the corruption table when you add a metric.

**Cohort scale.** The reference dev cohort is 100 patients, the unseen cohort 10, with roughly 3
to 15 admissions each. Behaviour at lifetime scale (hundreds of admissions) is untested, and the
one place that breaks first is f4's `prior_admissions` unit, whose source grows with the aged
span.

**Corpus.** The taxonomy was tuned against one corpus's BHC notes. The tier structure should
transfer; class definitions and thresholds may not.

**Thresholds are provisional and mostly unversioned.** f12's stored baseline carries a
`taxonomy_version` and skips the dominance gate on a mismatch. The f4, f5, f6 and f9 thresholds
are hard-coded constants with no such stamp, so a taxonomy bump that changes what is served
moves them silently. Same class of problem, same treatment wanted.

**`gate_holdout` is vacuous after a full replay** (section 3.2), and f6 has no data in a plain
generalisation replay because e1 is not in the fold-in chain (section 6).

---

## 12. Assembling the evidence pack

What to have in hand before a governance review. The clinical framing of the same list is in
`EVALUATION_CLINICAL.md`.

**Reproduce on your own data.**

1. Freeze a golden cohort: fixed subject ids, pinned input artifacts, pinned
   `TAXONOMY_VERSION`, pinned model tags and digests.
2. Run Tier 0 and record the gate booleans plus the stratified recall table.
3. Run Tier 1 at `temperature=0` and store the result as your baseline, with the mode
   (`--units synth` or `all`) recorded next to every faithfulness number.
4. Run f11. A detection rate below 1.0 is a metric blind spot; fix or document it before
   quoting any Tier-1 number.
5. Run f12 to establish an operating point and a baseline for future no-dominance checks.
6. Calibrate your judge (section 9). Until this is done, label every Tier-1 number provisional
   in the pack itself, not in a footnote.
7. Run a generalisation cohort larger than the reference 10.

**The pack.**

| Item | Source | What it establishes |
|---|---|---|
| Gate definitions and thresholds, with clinical sign-off | this doc, section 3 | what blocks a release and who agreed |
| Gate results on the frozen cohort | `salience_recall.json`, `integrity.json`, `compression_retention.json` | current state |
| Stratified recall by tier and class | `salience_recall.json` | that safety-critical classes are covered |
| Miss register | `salience_recall_misses.csv` | every known loss, weight-ranked, with attribution |
| Merge-loss triage and its adjudication | `review_tools/merge_grouping/` | that consolidation was screened for silent concept loss, and what the residual is |
| Metric-validation detection rates | `ablations/known_answer.json` | that the gates detect what they claim |
| Operating point and frontier | `ablations/frontier.*` | graceful degradation, no regression |
| Judge calibration report | your Route A or B run | what the Tier-1 numbers are worth |
| Judge-health worklist and its review record | `nothink_audit.*` | that degraded verdicts are reviewed, not trusted |
| Generalisation gap | `studies/generalization/FINDINGS.md` | not overfitted to the tuning cohort |
| Architecture ablations | `studies/position_bias/`, `studies/ledger_context_ablation/` | that the ledger earns its place, and why prose alone would not |
| Failure register | `KNOWN_FAILURE_MODES.md` | known defects, mechanisms, status |
| Clinical decision register | `clinical_tuning_points.md` | which choices were clinical and who made them |
| Waiver log | your CI | that no gate was silently skipped |
| Provenance | AuditRecords | model, prompt and taxonomy version behind every number |

**Three claims to make, and one not to.** Make: the deterministic gates are reproducible and
block promotion; the metrics have themselves been validated against known-answer corruptions;
the judge's agreement with clinicians is measured and its limits stated. Do not claim the output
has been validated against an external human gold standard -- it has not, and none exists for
this artifact. The honest position is that *retention* is deterministic and auditable, the
*judge* is calibrated with its error rate published, and the *quality* claim rests on a
nine-tenths published instrument scored by an imperfect judge, pending a human panel.

---

## 13. Open decisions

- **Active-malignancy hard gate.** Keep it threshold-gated via `gate_swr`, or add the whole
  `chronic_disease_state` class to the hard-gate set? The latter is broad -- it gates all
  chronic disease. Clinical call.
- **`--span all` for SWR.** Whole-established-record recall needs a cheap `hadm -> subject`
  digest. It would also dilute the signal: recent and hot facts sit in the ledger
  near-trivially, while loss only happens in the compacted span. The current decision is that
  compacted-span SWR stays the headline and whole-record would be a secondary, ungated number.
- **Tolerance-band widths for Tier 1.** Set empirically from your own baseline variance.
- **Promoting part of `temporal_coherence` to a deterministic Tier-0 check.** Some of it already
  is: future-leak and ordering live in f2.
- **Consolidation grouping validity (axis 3).** The omission half is now screened
  (`merge_loss_triage.py`, section 5.6); the conflation half still needs per-merge clinician
  verdicts on a sampled gold set, sampled separately per surface, to calibrate a grouping judge.
- **Version-stamping the remaining thresholds** (f4, f5, f6, f9), as f12's baseline already is.
- **Re-running the weight-sensitivity sweep against an f11 perturbed ledger**, with losses
  seeded across tiers, so the result is evidence rather than arithmetic (section 5.4).
- **Whether `temporal_coherence` should be validated in its own right**, given it is the one
  PDSQI attribute with no external validation and the one most specific to this artifact.
- **A second judge role**, to make f4 an independent check on c3 (section 9).
- **Persisting thinking traces at b1**, so the reasoning behind a wrong class is auditable
  (section 10).
- **`f0_run_eval.py`**, the orchestrator and single CI exit code (section 7).

---

## 14. References

- PDSQI-9, the published instrument: Croxford et al., *Development and Validation of the
  Provider Documentation Summarization Quality Instrument for LLMs*, JAMIA 2025
  (arXiv:2501.08977); Epic's open-source `evaluation-instruments` repo. "PDSQI-10" throughout
  this repo means PDSQI-9 plus the locally added `temporal_coherence` attribute (section 4.3);
  it is not a published instrument under that name.
- VeriFact and VeriFact-BHC (judge calibration, MIT licensed; PhysioNet-credentialed data).
- BHC benchmark, for context only and not a valid `d1` reference: Aali et al., *A dataset and
  benchmark for hospital course summarization with adapted LLMs*, JAMIA 2024 (arXiv:2403.05720);
  MIMIC-IV-Ext-BHC (PhysioNet).
- Faithfulness-metric selection for long-form clinical text: Adams et al., *A Meta-Evaluation of
  Faithfulness Metrics for Long-Form Hospital-Course Summarization* (arXiv:2303.03948).

Full citation list, including those required by the PhysioNet DUA: `CITATIONS.md`.

### Where the retention classes are defined

The 12 Axis-2 `retention_class` ids live in `src/retention_taxonomy.py` (`RETENTION_CLASSES`),
**not** in `clinical_salience_taxonomy.md`, which is the Axis-1 five-tier salience narrative.
List them with `python src/retention_taxonomy.py` (prints `render_for_prompt()`) or
`tax.class_ids()`. Default tiers: `severe_adr_allergy` T1, `infection_control_status` T1,
`code_status_directive` T1, `sentinel_event` T2, `implanted_device` T2,
`anatomy_altering_surgery` T2, `permanent_condition` T2, `altered_immune_status` T2,
`transfusion_obstetric_history` T2, `chronic_disease_state` T3, `recurrent_episode` T4,
`transient_event` T5.

### Related documents

| Document | For |
|---|---|
| `EVALUATION_CLINICAL.md` | the same framework for a clinical reader and a governance board |
| `PIPELINE.md` | what each generation stage does, in what order, reading and writing what |
| `MODEL_SELECTION.md` | choosing the generation model, the judge and the embedding model |
| `KNOWN_FAILURE_MODES.md` | catalogued defects, their mechanisms and status |
| `clinical_tuning_points.md` | every site-specific clinical decision, with trade-offs |
| `TAXONOMY_EXTENSION.md` | changing the taxonomy, and what it costs to re-run |
| `REPAIR_RUNBOOK.md` | fixing things when they break |
