# Taxonomy extension cookbook

How to adapt `clin_longsum`'s clinical salience taxonomy to a different institution's patient
profile: what to change, what it costs to change, and what else must move in lockstep.

Self-contained. Read `clinical_salience_taxonomy.md` for the conceptual model behind the taxonomy
and `clinical_tuning_points.md` for the decisions themselves. This document is the mechanics.

> **`retention_taxonomy.py` is what runs.** The `.md` files record intent. Where they disagree, the
> `.py` is right. Every line reference here was read against v2.12; re-check them before relying on
> a line number, since `src/` changes often.

---

## 1. The mental model

Every extracted fact carries three axes:

| Axis | Values | Set by |
|---|---|---|
| **Salience tier** | T1..T5 | b1 at extraction, floored to the class default |
| **Retention class** | 12 ids, e.g. `severe_adr_allergy` | b1 at extraction, from the prompt |
| **Status modifiers** | `certainty`, `lifecycle`, `occurrence`, `negative_kind` | b1 at extraction, some derived on read |

Three derived functions turn those axes into behaviour:

- **`is_ledger_member(...)`** - the pin decision. Does this fact reach the retention ledger?
- **`prune_reason(...)`** - if declined, which branch declined it. Recorded in `dropped[]`.
- **`is_safety_robust(class)`** - public accessor over `_SAFETY_ROBUST_CLASSES`, so a
  consumer (b2's `amended` route) can test the safety exemption without reaching for the
  private tuple.

Everything else - merging, rollup, compaction, gating, display order - keys off the outputs of
the first two functions plus the tier.

### The one distinction that determines cost

| Evaluated | Where | Consequence for a change |
|---|---|---|
| **At b1 write time** | `default_tier_for_class` and `floor_tier`, `b1_retention_extractor.py:382-399` | The tier is baked into `retention_facts.jsonl`. Changing a class default does **not** reach existing facts. |
| **On read, by every consumer** | `is_ledger_member`, `prune_reason`, `normalize_fact` - called by b2, b3, c1, `db_persist_source`, `roles/contracts.py` (plus the read-only
  `clinical_review_tools/evidence.py` and `b1_rerun_hadms.py`) | Changing the rule takes effect from b2 onward. No re-extraction. |

`normalize_fact` states this deliberately in its docstring: reclassification is *"intentionally
derived on read so the stored facts stay raw and re-derivation tracks the current taxonomy without
re-extraction."*

**b1 re-extraction is the most expensive stage.** 

### What helps you

- **The b1 prompt is generated from the taxonomy.** `tax.render_for_prompt()` is injected at
  `b1_retention_extractor.py:446`, and again at `f6_incoming_ref.py:214`. There is no prompt file to
  hand-edit and no way for the prompt to drift from the enforced taxonomy.
- **The database does not constrain the vocabulary.** `retention_class` and `salience_tier` are bare
  `TEXT` in `schema.sql` (lines 167, 194, 231, 259), deliberately without CHECK constraints (see the
  comment at line 219). New classes and tiers need no DDL migration.
- **Unknown values fail safe at extraction.** An unrecognised class logs a warning and falls back to
  `transient_event`; an unrecognised tier falls back to the class default (`b1:382-399`).

---

## 2. The current taxonomy (v2.12)

### Tiers

| Id | Label | Action | Representation |
|---|---|---|---|
| T1 | Operational alert | `pin` | verbatim fact, always surfaced |
| T2 | Permanent baseline | `carry_forward` | structured state metadata |
| T3 | Chronic axis | `state_track` | current stage/severity + trend |
| T4 | Recurrent episode | `graph_collapse` | count / frequency / date range under parent |
| T5 | Transient | `prune` | dropped past the window unless a significant negative |

### Classes and their default tiers

| Class id | Default tier |
|---|---|
| `severe_adr_allergy` | T1 |
| `infection_control_status` | T1 |
| `code_status_directive` | T1 |
| `sentinel_event` | T2 |
| `implanted_device` | T2 |
| `anatomy_altering_surgery` | T2 |
| `permanent_condition` | T2 |
| `altered_immune_status` | T2 |
| `transfusion_obstetric_history` | T2 |
| `chronic_disease_state` | T3 |
| `recurrent_episode` | T4 |
| `transient_event` | T5 |

The class default tier is a **floor**, not a fixed value. The model may escalate a fact above
it for safety - an MRI-relevant `implanted_device` to T1 - but may not de-escalate below it. See
§7 for why this matters when migrating.

---

## 3. Choose your change shape

| # | Change | Blast radius | Re-run |
|---|---|---|---|
| **A** | Pin or prune rule (`is_ledger_member`, `prune_reason`) | taxonomy module only | b2 onward. Cheap. |
| **B** | Class description, decision rule, examples, counter-examples | taxonomy module; propagates to the prompt automatically | b1 re-extraction (the model's labelling changes) |
| **C** | Re-tier an existing class | taxonomy module + lockstep sites (§5) | b1 re-extraction, **or** a migration pass (§7, lossy) |
| **D** | Add or remove a class | taxonomy module + lockstep sites (§5) | b1 re-extraction. Unavoidable. |
| **E** | Add or remove a tier | taxonomy module + lockstep sites + UI styling | b1 re-extraction. Rare; think hard first. |
| **G** | Add a deterministic re-routing net (reclassifies an already-extracted fact on read) | taxonomy module + a matched at-source extraction rule | (a) net: b2 onward, cheap. (b) at-source rule: b1 re-extraction, deferrable |

Every shape is a clinical decision and every shape runs the same three governance steps (§4.0);
they differ only in blast radius and re-run cost.

Shapes A and B are the common ones. Most institutional adaptation is A (what pins) and B (what
counts as this class here), not D.

---

## 4. Procedures

### 0. Applies to every shape

Shapes A-G differ in blast radius, not in governance. Every one of them is a clinical decision, and
all three steps below run for all of them. Only the specifics differ.

**Evidence, before proposing.** Quantify the change on your largest cohort first (in this repo,
`test`) - read-only, no pipeline stage runs. A change is not proposable on a single case. Load
`interim/retention_facts.jsonl`, run `tax.normalize_fact()` on every fact FIRST (the stored class is
not the routed class, and cohort stamps are mixed-version, §9), then count what the change would move
and what it would move that you did not intend. Write the number down: it is the prediction the
verification step tests. For shapes B, C, D and E the count is only obtainable by simulating the
new rule over stored facts, since the real answer needs a re-extraction you have not run yet - do
the simulation anyway, because discovering you were wrong after a 1-2 day b1 run is the expensive
way to find out.

Then the question that actually decides it: **is the thing you are about to rescue already carried
elsewhere?** A fact captured by a sibling node, or already pinned by another branch, is not a loss
and does not justify a rule. Record the answer per fact family, not in aggregate.

**Decision record, before code.** Open a `[DECIDE]` row in `clinical_tuning_points.md` carrying that
evidence. No code lands before clinical sign-off, and the sign-off names the specific behaviour
being changed - for a class change, its neighbours and its counter-examples; for a re-tier, the old
and new tier; for a net, its source class, target class **and target tier**. This is the gate on
starting the work, not a box ticked during the version bump (§6).

**Predict, then verify.** The change must move the count you predicted in step one. §8 lists the
mechanics; the discipline is that "unexplained jump" only means something if you had a prediction to
compare against. **A larger move than predicted means the rule is leaking - revert it, do not tune
it in place.** Tuning a leaking rule against the cohort until the number looks right is how a
taxonomy acquires rules nobody can later justify.

---

### A. Change a pin or prune rule

1. Edit `is_ledger_member` in `retention_taxonomy.py`.
2. **Edit `prune_reason` in the same commit.** Its docstring is explicit: *"Mirrors the gate's branch
   order exactly - edit both together, or the recorded reason will drift from the decision it claims
   to explain."* Add a new value to `PRUNE_REASONS` if you add a branch.
3. Bump the version and add a CHANGELOG entry (§6).
4. Re-run b2 -> b4 -> c1 -> c3 -> c4 -> d2 -> d_coordinator -> d3 (§8).

Nothing in `retention_facts.jsonl` changes; the pin decision is re-derived on read.

### B. Reword a class definition

1. Edit the `RetentionClass` entry - `description`, `decision_rule`, `examples`,
   `counter_examples`. These feed `render_for_prompt()` directly, so this *is* the prompt edit.
2. Bump the version and CHANGELOG.
3. Re-run b1 (full re-extraction) then the whole cascade.
4. Diff the new `retention_facts.jsonl` class distribution against the old one before trusting it. A
   reworded counter-example can move hundreds of facts.

### C. Re-tier a class

1. Change `default_tier` on the `RetentionClass`.
2. Work the §5 checklist - `f1_salience_recall.py` gates and weights especially.
3. Either re-run b1, or run a migration (§7) and accept its lossiness.
4. Re-run the cascade.

### D. Add a class

1. Append a `RetentionClass` with a distinct `id` (snake_case), a `default_tier`, a decision rule
   that distinguishes it from its neighbours, and at least two counter-examples. Counter-examples do
   more work than examples in practice - they are what stop the model over-applying a new label.
2. Decide the safety questions:
   - Should it be in `_SAFETY_ROBUST_CLASSES`? Those pin regardless of tier, certainty, or lifecycle
     mis-tagging. Currently `severe_adr_allergy` and `code_status_directive` only. Reserve this for
     classes where a mis-tag is dangerous, not merely inconvenient.
   - Should it be in `_PATIENT_REPORTED_RETAIN`? Those pin on patient report alone.
   - Should it be in `b2._SPLIT_EXEMPT`? Exempt classes are not split by the FM-7 compound-identity
     guard. Currently `code_status_directive` only, because latest-wins is correct for directives.
3. Add it to the b2 merge-group map (§5) or it will never merge with anything.
4. Bump version, CHANGELOG, re-extract, re-run.

### E. Add a tier

Everything in D, plus: `SALIENCE_TIERS` ordering (which drives `_TIER_RANK` and `floor_tier`), the
`f1` weight table, the `f6` comparable-tier band, and the UI CSS classes. Consider whether an
existing tier's `action` covers your need before adding one - `pin`, `carry_forward`, `state_track`,
`graph_collapse`, `prune` is a fairly complete set of behaviours.

---

### G. Add a deterministic re-routing net

The shape of `is_surveillance_finding`, `is_disease_course_event` and `is_sentinel_episode`: 
a high-precision text rule that re-routes a systematically
mis-classified fact at read time. Use it when real-world data shows a class being mis-routed and a
deterministic rule can catch it. This is clinical governance, not configuration.

**Do not append to an existing regex.** Each approved rule lands as its own named predicate, so it
can be versioned, cited in a trace, and reverted independently.

§4.0 applies as it does to every shape. Two points below are specific to nets, plus these
net-specific notes on the §4.0 evidence step: count candidate hits **within the source class
only**, count hits in every other class as the leakage estimate if scope is ever widened, and
read every hit for negation ("without radiographic evidence of ...") and for
mention-without-event ("inadequate BiPAP settings" is not an ICU escalation).

#### G1. Landing - BOTH halves are required, and they are not alternatives

b1 does not run the nets. `_normalise_fact` (`b1_retention_extractor.py:382-399`) applies only the
surveillance promotion and `floor_tier`; the reclassification happens solely in
`tax.normalize_fact`, called by b2, b3, c1 and `db_persist_source`. So:

| Half | Where | Bump | Effect | Re-run |
|---|---|---|---|---|
| **(a) read-time net** | new `is_<rule>()`, OR'd into `normalize_fact`'s reclassification branch | taxonomy minor | re-derives on existing facts at load | **no b1**: b2 -> b4 -> c1 -> c3 -> c4 -> d1 -> d2 -> d_coordinator -> d3 |
| **(b) at-source rule** | `EXTRACTION_RULES` + the source class's `counter_examples` (both render into `render_for_prompt()`) | taxonomy minor + `prompt_version("retention_extractor")` | changes what b1 EMITS and how it words it | **full b1 re-extraction** |

Why (a) alone is insufficient: the net is a regex over `text`, so it cannot fire on a fact b1 never
emitted as its own fact (only `EXTRACTION_RULES` governs emission - cf. "SURFACE THE SUBSTRATE",
"ACUTE-ON-CHRONIC: emit TWO facts"), cannot supply a missing `parent_condition`
(`is_disease_course_event` reads it), and cannot see a trigger token the model never wrote.

Why (b) alone is insufficient: it leaves the entire existing corpus mis-routed until b1 re-runs.

**So: land (a) now, stage (b).** Do not trigger a b1 run for (b) alone unless the extraction MISS,
not merely the mis-label, is the defect. Say so in the CHANGELOG entry - v2.12 is the worked
example: *"The prompt tightening is STAGED and unexercised on the current cohort; the gate reorder
is what protects the existing data."*

**Tier trap.** `normalize_fact`'s reclassification branch hardcodes `fd["salience_tier"] = "T2"`.
Any predicate OR'd into it silently inherits T2. If the approved target tier is not T2, give the
branch a per-predicate tier BEFORE adding the rule.

#### G2. Golden cases

Every approved rule adds its exemplars to the `_NET_GOLDEN_CASES` table in `retention_taxonomy.py`
-- MUST-catch and MUST-NOT-catch phrases, verbatim from the cohort, including the negation and
near-miss cases found at the §4.0 evidence step - and `python src/retention_taxonomy.py --selfcheck` is run before and
after any net edit. This is what stops a later recall widening from silently breaking an earlier
approved rule. A live example of why: `_SENTINEL_EPISODE_RE`'s 30-char window misses "thrombosis
around upper extremity hemodialysis lines and AV fistula" - an in-scope v2.8 case, uncaught, with
nothing in the pipeline able to detect it.

The §4.0 verification for a net is concrete: diff the `(promoted, reclassified)` counts
`normalize_fact` returns, before vs after, against the predicted number.

---

## 5. Lockstep sites

About a dozen places restate taxonomy policy outside the taxonomy module. Work this list on every
change of shape C, D, or E. **The first group fails silently** - it mis-scores rather than raising.

| Site | What it hardcodes | If you miss it |
|---|---|---|
| `evaluation/f1_salience_recall.py:86` | `TIER_WEIGHT` | New tier scores as absent; recall weights wrong |
| `evaluation/f1_salience_recall.py:101` | `FLOOR_TOP_CLASSES` | Class not floored to top weight |
| `evaluation/f1_salience_recall.py:105` | `SAFETY_CLASSES` | New safety class not treated as safety-critical |
| `evaluation/f1_salience_recall.py:108` | `GATE_TIERS` | **New T1/T2-equivalent class is not gated at all** |
| `b2_build_ledger.py:107-111` | class -> merge-group map (`disease` / `procedure`) | New class lands in its own group, never merges, ledger fragments |
| `b2_build_ledger.py:128` | `_SPLIT_EXEMPT` | FM-7 identity guard splits something that should stay collapsed |
| `b2_build_ledger.py:521, 532, 545` | `recurrent_episode` branch tests | Episode routing misses a new episode-like class |
| `b3_loss_audit.py:67-70` | pinned-class list | Loss audit stops seeing the class |
| `evaluation/f6_incoming_ref.py:201` | `COMPARABLE_TIERS` | New tier excluded from incoming comparison |
| `evaluation/f6_incoming_ref.py:483-484` | tier whitelist, unknown -> T3 | New tier silently coerced to T3 |
| `evaluation/f11_metric_validation.py:171-175, 330` | tier and class names in mutation tests | Validation harness breaks or tests the wrong thing |
| `api/tiered_review.html:704, 728` | CSS classes for T1/T2/T3, else `other` | New tier renders unstyled. Cosmetic |
| `studies/generalization/export_review_html.py:133-134, 384, 411` | SQL `CASE` tier ordering + CSS | Study tool, not part of the pipeline - only relevant if you run the generalization study. New tier sorts last. Cosmetic |
| `b2_build_ledger.py:576` | `tax.is_safety_robust()` exemption from the `amended` route | A new safety class is routed to `amended`, which `d3` does not load and `c2` counts as REPRESENTED - the FM-8 signature: no `dropped[]` entry, no flag, silent loss |
| `evaluation/f12_frontier.py:383` | baseline staleness keyed on `taxonomy_version` | **Correct behaviour you must act on, once you have a baseline.** The gate compares against `data/output/eval/ablations/frontier_baseline.json`, which is gitignored - a fresh clone has none, so the dominance gate is inert until you establish one. Where a baseline does exist, any version bump marks it stale and SKIPS the gate until you regenerate it |
| `clinical_review_tools/b1_b4/dump_clusters.py:108` | `recurrent_episode` filter | Dev tool only |
| `fhir_interop.py` | `VERIFICATION_STATUS` / `CLINICAL_STATUS` crosswalks keyed on `CERTAINTY` / `LIFECYCLE` | **Fails LOUDLY, not silently** - `_assert_total_coverage()` raises at import on any added or removed certainty/lifecycle value. Only shape F below touches it |

**Shape F - add or remove a `certainty` / `lifecycle` value.** Not in the section 3 table
because it is the most expensive shape available and is almost never the right change.
`render_for_prompt()` builds b1's extraction vocabulary directly from the `CERTAINTY` and
`LIFECYCLE` tuples, so touching them changes the prompt and forces full re-extraction --
invalidating `retention_facts.jsonl` and everything downstream, including published f-metrics.
Section 7's re-tier-without-re-extraction trick does NOT apply. Note the DB columns are plain
`TEXT` with no CHECK constraints and `contracts.py` types them as `str`, so nothing will
stop you; the damage surfaces only as mixed-vocabulary artifacts. If the goal is interoperability
rather than semantics, add a crosswalk in `fhir_interop.py` instead - additive, no version bump,
no re-run.

**Verification grep.** After any change, search `src/` for the old class id and every tier literal:

```powershell
Select-String -Path (Get-ChildItem src -Recurse -Include *.py,*.html,*.sql) `
  -Pattern '"old_class_id"|"T[1-5]"'
```

---

## 6. Version bump and CHANGELOG

`TAXONOMY_VERSION` is stamped into artifacts and DB rows by about twenty modules, and it is how a
stale artifact is later distinguished from a live defect. Never change taxonomy behaviour without
bumping it.

1. `TAXONOMY_VERSION = "retention_taxonomy.vX.Y"` at the top of the module.
2. Append to the module's own `CHANGELOG` list - the existing entries are the model. State: what
   changed, why, **whether re-extraction is required**, and what is explicitly untouched. The v2.12
   entry is the model: it names the failure mode (FM-8), quantifies the cohort audit (110 pts /
   11,930 facts; 6 amended nodes, 4 of them T1 allergies absent from the served ledger), states
   that no re-extraction is required, and explicitly separates the gate reorder (which protects
   existing data) from the STAGED prompt tightening (which does not fire until the next b1 run).
   That separation is the pattern - see shape G in §3 / §4.
3. Add a matching entry in `References/CHANGELOG.md`.
4. Update `clinical_salience_taxonomy.md` if the conceptual model moved, and
   `clinical_tuning_points.md` if the change resolves or opens a decision point.

Clinical sign-off happens **before** any of this, at §4.0 - the bump records a decision already
taken, it is not where the decision gets made. Taxonomy changes are clinical governance, not
configuration.

---

## 7. Migrating tiers without re-extraction

For shape C, a migration pass can rewrite `salience_tier` in place instead of re-extracting. **It is
lossy, and the loss is not recoverable.**

Because `b1:399` floors the model's tier to the class default and keeps escalations, a stored fact at
T1 in a T2-default class may be either an escalation the model made deliberately (an MRI-relevant
device) or an artifact of an older default. **Nothing in the stored record distinguishes them.** A
migration that recomputes tiers from class defaults flattens every escalation.

So:

- **Escalations matter for your classes** (devices, anything safety-escalatable) -> re-extract.
- **They do not** (a purely administrative re-tier of a class the model never escalates) -> migrate.

A migration must cover, in this order:

1. `retention_facts.jsonl` for every cohort - the source of truth on disk.
2. `retention_facts` and `retention_dropped` in Postgres, or re-run `db_persist_source --facts`.
3. Re-run b2 -> b4 and everything downstream; the ledger is derived and must not be patched directly.

Sketch, mirroring `b1:382-399` exactly so the migration and the extractor cannot diverge:

```python
import retention_taxonomy as tax

def remigrate_tier(fd: dict) -> bool:
    rc = fd.get("retention_class")
    if not tax.is_known_class(rc):
        return False
    old = fd.get("salience_tier")
    new = tax.floor_tier(old if tax.is_known_tier(old) else
                         (tax.default_tier_for_class(rc) or "T5"),
                         tax.default_tier_for_class(rc))
    fd["salience_tier"] = new
    return new != old
```

Note this only ever raises a tier to the new floor. To *lower* a class's tier you must reset to the
class default unconditionally, which is where escalations are lost. Write the counts of changed facts
to a log and keep it - it is the only record of what the migration did.

No migration script ships with the repo. This is a sketch, not a tested tool.

---

## 8. Re-run order and verification

Cold path after a taxonomy change, from `src/`:

```
b1 (only if the change requires re-extraction)
b2 -> b4                      # never let anything read the ledger between these two
c1 -> c3 -> c4
d1 -> d2 -> d_coordinator
database/d3_load_tiers.py
```

**`b2` must be followed immediately by `b4`.** `b4`'s backup logic keys off a `consolidated` marker;
an interleaved read gets an unconsolidated ledger. `c3 -> c4` must precede `d_coordinator`, or the
served Prior Admissions prose is c1's uncorrected version.

Then verify:

1. `python -m py_compile` on every edited file.
2. `python b3_loss_audit.py` - class distribution and pinned-fact counts before vs after.
3. `evaluation/f2_integrity.py` - Tier 0, blocks the build. Must pass.
4. `evaluation/f1_salience_recall.py` - advisory. Compare against the pre-change baseline **and
   against the count you predicted at §4.0**; a move larger than predicted means the rule is
   leaking or a §5 site was missed, not that the taxonomy improved. Revert; do not tune in place.
5. Spot-check `dropped[]` reasons for the classes you touched. A new `unclassified` prune reason
   means `prune_reason` has drifted from `is_ledger_member`.
6. Open the review UI and confirm tier styling and ordering on a patient with the changed class.
7. **Regenerate the `f12` baseline, if you have one.** `f12_frontier.py:383` compares the stored
   baseline's `taxonomy_version` against the running one and skips the dominance gate when they
   differ, so a bump leaves f12 unable to gate until it is re-baselined. The baseline lives under
   `data/` and is not committed, so on a fresh clone there is nothing to regenerate and nothing
   to gate - establish a baseline first if you want the gate.

---

## 9. Traps

- **`prune_reason` mirrors `is_ledger_member` by hand.** They drift silently. Edit together.
- **`normalize_fact` reclassifies at read time.** A stored `retention_class` is not necessarily the
  one that routed. Run `normalize_fact` first when reasoning about any specific fact.
- **`_SAFETY_ROBUST_CLASSES` bypasses tier, certainty, and lifecycle entirely.** Adding a class here
  makes it pin unconditionally except when `ruled_out` - v2.12 (FM-8) moved the safety branch
  ABOVE the `superseded` guard, so `lifecycle=superseded` no longer un-pins these. That is a
  deliberate safety choice with a false-positive cost; `infection_control_status` is excluded
  precisely because it spans temporary and permanent states. Adding a class here now has a SECOND
  consequence: `b2_build_ledger.py:576` exempts safety classes from the `amended` route via
  `tax.is_safety_robust()`, so the class is served as an explicit negative rather than
  disappearing into a list `d3` does not load.
- **`recency_window_days=90` on T5 is declared but not wired to a consumer.** b4's T5 prune is by
  recency rank, not by days. Do not assume changing that field changes behaviour.
- **Singleton `recurrent_episode` groups are currently invisible** - they route to `episodes_raw`,
  appearing in neither `dropped[]` nor node text. Known open item; relevant if you add an
  episode-like class.
- **Never open a pipeline CSV in Excel.** It silently rewrites dates and booleans on save. Source
  CSVs are day-first; `src/cohort.py` is the single `dayfirst`-parsed loader, and ordering uses the
  numeric `admit_seq`.
- **Validate on your largest cohort.** A ten-subject cohort cannot detect a distribution shift;
  it will show you nothing and reassure you anyway. Size the validation cohort before trusting a
  before/after diff, and say which cohort the numbers came from whenever you report one.
- **Cohort artifacts go mixed-version the moment you bump without re-extracting.** A version bump
  that requires no re-extraction is cheap precisely because it leaves `retention_facts.jsonl`
  alone - so `audit.taxonomy_version` then varies across admissions within a single cohort, and
  across cohorts. This is normal, not corruption. Two consequences: read
  `audit.taxonomy_version` per record rather than assuming a cohort has one, and take any
  before/after distribution diff from re-derived values, never from the stored field.
- **A net and its extraction rule are not alternatives.** b1 runs neither
  `is_disease_course_event` nor `is_sentinel_episode` - `_normalise_fact` applies only the
  surveillance promotion and `floor_tier`. The net repairs stored data; the `EXTRACTION_RULES`
  change prevents recurrence. Landing one half leaves either the existing corpus or every future
  run wrong. §4G.

---

## 10. Rollback

Taxonomy changes are reversible only to the extent that artifacts are. Before starting:

1. Copy the whole data root, or confirm `retention_ledger.raw.jsonl` exists - it holds what the T5
   recency prune removed.
2. Note the current `TAXONOMY_VERSION`. Artifacts carry it, so a mixed-version data root is
   diagnosable after the fact.
3. If b1 re-extraction is involved, keep the old `retention_facts.jsonl`. Regenerating it costs
   GPU-hours and the model is not deterministic - you will not get the same file back.
