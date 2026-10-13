# KNOWN_FAILURE_MODES.md

Register of failure modes traced to root cause in the `clin_longsum` pipeline. Each entry is a
RECOGNITION AID: it tells you what the signature looks like in the data and
which code to read live. Confirm the mechanism against the current source every time - several
of these have been fixed since they were traced, but a trace over old artifacts may
still show the old behaviour.

Defect classes (never fold one into another):
- **clinical / taxonomy** -- a label or governance call. 
- **engineering / routing** -- code does not do what it intends. 
- **prompt wording** -- the instruction produced the behaviour. Propose a reword.

Status vocabulary: `open` | `partly fixed` | `fixed` | `by design`.
Line references are anchors for grepping.

---

## FM-1 Inference from redaction

A disease named in the note only as a de-identification placeholder (`___ disease`) is
completed to its base-rate diagnosis. Traced case: a redacted CMT neuropathy became a
fabricated "Diabetes mellitus" ledger entry.

| | |
|---|---|
| Class | prompt wording (primary) + engineering (repair loop, feed-forward) |
| Status | partly fixed -- b1 extraction prompt carries an explicit REDACTIONS rule; the repair loop and the feed-forward remain open |
| Stages | b1 `_EXTRACT_TMPL` -> b1 `_UPDATE_TMPL` -> b1 `_update_known` -> b2 `_build_node` |

**Signature in data**
- A ledger fact with NO support anywhere in any `bhc_text` for that patient.
- The originating `retention_facts.jsonl` fact's `rationale` hedges: "inferred from redacted
  documentation" or similar.
- The note contains `___` adjacent to the disease slot.

**Mechanism**
1. `_EXTRACT_TMPL` instructs the model to surface a chronic/permanent substrate named in the
   note. Without a counterweight this reads as licence to name an unnamed substrate. The REDACTIONS rule (`"___" is a removed de-identification token, not a fact`) and the "SURFACE THE SUBSTRATE THE NOTE STATES" clause have since been added.
2. `_FEEDBACK_TMPL` (step B) DOES see the note and has check 5(b) for exactly this -- an
   inferred substrate that is not in the note. It can detect the fabrication.
3. `_UPDATE_TMPL` (step C) DOES NOT see the note. It receives only the issue list and the
   current facts. A self-critique that can compare only against facts can DEMOTE a fabricated
   fact (lower tier, lower certainty) but has no evidence with which to DELETE it. This is
   FM-6.
4. `_update_known` then feeds the surviving fact forward into the prompt for admission N+1 as
   an established condition, where it is no longer questioned.
5. b2 `_build_node` gives it a permanent ledger slot (see FM-2).

**Confirm it**
- Grep the fabricated string across every `bhc_text` for that `subject_id`. Absent = fabricated.
  **Match on WORD BOUNDARIES, and read the surrounding clause before crediting a hit** -- a
  substring search over-matches, and it over-matches in the direction that hides this mode. A
  spurious hit reads as support and closes the investigation. See cross-cutting theme 7; the
  traced case is `adrenal` matching `hypoadrenalism` inside "no evidence of hypoadrenalism".
- Find the fact in `retention_facts.jsonl`, read its `rationale`.
- Check whether the same text appears in a LATER admission's fact set with
  `occurrence=recurrence` -- that is the feed-forward, not independent confirmation.

**Open**
- Give `_UPDATE_TMPL` the note (shared fix with FM-6).
- Decide whether `_update_known` should carry only facts that survived the feedback step
  unmodified.

**a fabrication can hold a permanent T1 pin (see FM-8)**

The FM-8 fix makes `severe_adr_allergy` and `code_status_directive` robust to tier, certainty
AND lifecycle mis-tagging, on the asymmetric-cost argument that a missed contraindication is
worse than a spurious one. The cost of that argument is that an FM-1 fabrication landing in a
safety class is now pinned for life, and `superseded` no longer un-pins it. The only remaining
exits are `certainty=ruled_out` - which b1 will not assign to something it invented - and a
versioned taxonomy fix by the maintainer. The physician cannot remove it: the ledger is
read-only by design.

Traced case, `data_gen` subject 90000012:
`"Intolerance to amiloride due to gynecomastia"`, T1 `severe_adr_allergy`, confirmed.
The `bhc_text` redacts the causative drug; the model imputed amiloride from the discharge
medication list. Clinically self-refuting: gynecomastia is a **spironolactone** effect
(anti-androgen at the mineralocorticoid receptor), and amiloride is the potassium-sparing
substitute chosen precisely BECAUSE it does not cause it. The imputation is the FM-1 signature
exactly - redacted slot, plausible filler drawn from later context.

Harm here is low (a spurious amiloride intolerance costs a diuretic substitution). The pattern
is not: FM-1 can fabricate an ADR against a drug the patient needs.

**RESOLVED by re-extraction with updated _EXTRACT_TMPL.**
`b1_rerun_hadms.py` on the affected admissions, re-extracted this fact as `"Diuretic intolerance with gynecomastia"` - 
the imputed drug name dropped, the drug CLASS stated instead. That is the correct behaviour under redaction: the note supports a
diuretic-associated gynecomastia, and nothing more specific. 

**Do not read this as an argument to reverse FM-8.** Three real contraindications were restored
against one fabrication pinned, and the asymmetry still favours the fix. It is an argument for
short queue turnaround, and for prioritising the two open FM-1 items above.

**short queue turnaround.** A physician who sees a fabricated pin can report the row from the review UI,
and it is routed, tracked and answered in writing. See `FEEDBACK_RUNBOOK.md`. the physician still cannot remove the entry, the only exits remain `certainty=ruled_out` and a versioned taxonomy fix by the maintainer, and re-extraction
remains the practical remedy.

---

## FM-2 Split-provenance labelling in b2

`_build_node` assembles one ledger node from attributes taken from DIFFERENT facts in the
cluster. A fabricated or downgraded fact can end up displayed at permanent-baseline weight.

| | |
|---|---|
| Class | engineering / routing + clinical (the PROVISIONAL routing rule) |
| Status | open |
| Stages | b2 `_build_node`, then `_merge_group` during `_consolidate`, then `_project_patient` routing |

**Signature in data**
- A ledger node whose `salience_tier` is stronger than the tier of the fact its `text` came
  from -- compare the node against each `fact_id` in `fact_ids`.
- A node in `flagged[]` (rendered `provisional=true`) carrying a T1/T2 tier.
- A node whose `text` matches the LATEST mention but whose `n_mentions` implies earlier ones
  with different wording.

**Mechanism**
`_build_node(cluster, dates)`:
- `cluster` is sorted by `admit_seq`; `latest = cluster[-1]`.
- `salience_tier` = STRONGEST across the whole cluster (`min` by `_TIER_RANK`).
- `retention_class`, `text`, `certainty`, `current_lifecycle` = from `latest` ONLY.
- `negative_kind` = `tax.merge_negative_kind(...)` across the cluster.
- `onset_date` / `last_confirmed_date` = min / max across the cluster.

So tier comes from one mention, everything else from another. A fact downgraded at its latest
mention keeps the strongest tier it ever held.

There is a SECOND, later split in `_merge_group` (called from `_consolidate`): the
representative node is picked by `(tier rank, certainty != "confirmed", -len(text))` -- so
consolidation can again swap `text` / `certainty` / `current_lifecycle` to a different member
than `_build_node` chose, this time preferring the LONGEST text. Check both when a node's
attributes look mismatched.

Routing in `_project_patient`, on the built node:
```
current_lifecycle == "superseded"  -> amended
certainty == "confirmed"           -> nodes
otherwise                          -> flagged   (provisional / patient-reported)
```
`lifecycle` is NOT consulted except for `superseded`, so any non-confirmed CANDIDATE is served
`provisional=true`. Note what that set actually is: `is_ledger_member` has already excluded
`suspected + resolved / in_remission` (outside `sentinel_event`), so those never become
candidates -- they fall through to `dropped[]` with reason `suspected_inactive` (FM-5). The
flagged bucket is therefore `suspected + active` T1-T3, `suspected sentinel_event` at any
lifecycle, `patient_reported` in the three retained classes, and non-confirmed facts carrying a
`negative_kind` or in a safety-robust class.

**Confirm it**
- Pull the node's `fact_ids`, look each up in `retention_facts.jsonl`, and tabulate
  `salience_tier` / `certainty` / `lifecycle` / `text` per fact. The mismatch is visible
  directly.
- Re-run `_cluster` on the patient's facts for that `retention_class` to confirm the cluster
  membership is what you think it is.

**Downgrade conditions**
Taking the strongest tier is DELIBERATE: it prevents a chronic condition mis-tagged T4/T5 at a
later mention from falling out of the ledger (the same intent as `tax.floor_tier` in b1). The
defect is the COMBINATION with latest-wins text, not the tier rule alone. Do not propose
removing the tier rule without addressing what replaces the safety it buys.

**Open**
- Whether a node should carry per-attribute provenance (which `fact_id` each attribute came
  from) rather than a single `fact_ids` union.
- Whether the flagged bucket should distinguish `suspected + active` from `suspected sentinel`
  at any lifecycle, rather than serving both as one PROVISIONAL class -- clinical call.
- `dropped[]` does NOT address this mode. It records facts that were never
  candidates; FM-2 is about how candidates are assembled and routed. Both split points and the
  PROVISIONAL rule are untouched.

---

## FM-3 Temporal leak / future knowledge in compaction

c1, summarising an early admission, asserts a condition first documented years later:
"would later progress to <future dx>", or present-tense description of a future state.

| | |
|---|---|
| Class | engineering / routing (three separate mechanisms) |
| Status | fixed in `clin_longsum` -- verify against the artifact's generation date before reusing the diagnosis |
| Stages | c1 `_node_to_fact` / `_ledger_for_span` / `_ledger_lines`; c3 gate; fold-in timing |

**Signature in data**
- `compaction_results.jsonl` `prior_generated_md` (or `prior_admissions_md` on older records)
  contains prospective language, or names a condition whose earliest `source_hadm_id` in the
  ledger has an `admit_seq` greater than any admission in `covered_hadm_ids`.
- The corresponding `compaction_faithfulness.jsonl` row flags it in `failed_statements` while
  `pass` is `true`.

**Mechanism (four contributing defects, three now fixed)**
1. **Fold-in timing.** An admission ages out of the hot window only when a later admission is
   promoted. For a setup with hot window of seven admissions, the compaction of admission 1 therefore runs AFTER 
   admission 8 exists in the ledger. Ordering by `admit_seq` does not prevent this - fold-in ORDER and fold-in TIME are
   different things. Structural; not fixed, and not fixable by ordering.
2. **b2 clustering.** Phrase-containment clustering can merge a T3 chronic-state node with the
   T2 sentinel event that ended it, applying latest-wins for text/class/lifecycle and
   earliest-wins for `onset_date`. The merged node then reads as a single condition spanning
   both. See FM-2; the `_QUALIFIERS` gate and `_MERGE_GROUP` compatibility groups now bound
   this, but do not eliminate it.
3. **Unanchored ledger in the c1 prompt.** FIXED. `_node_to_fact` now anchors each node to its
   EARLIEST `source_hadm_id` and populates `admit_seq`; `_ledger_for_span(facts, max_seq)`
   drops facts first documented after the span; `_ledger_lines` renders `[adm N]` per fact.
   Incremental mode scopes PER FOLD (`_ledger_for_span(ledger_facts, a.admit_seq)`), not per
   patient. Residual: facts with unresolvable `admit_seq` are KEPT deliberately -- dropping a
   pinned fact is judged worse than showing one slightly ahead of the span.
4. **c3/c4 gate.** FIXED. c4 gates on the PRESENCE of unsupported statements, not on the
   aggregate `pass` boolean. 

Note the prompt asymmetry that remains: `_RULES_COMMON` tells the model not to CONTRADICT the
ledger. Combined with an unanchored or over-scoped ledger, the safest way to obey is to hedge
forward.

FIXED since the trace: `CompactorOutput` now persists `ledger_facts` = the SPAN-SCOPED list the
prompt saw, and `audit.scores` carries `ledger_scope_max_seq` / `n_ledger_scoped` /
`n_ledger_total`. Previously the output echoed the patient's whole unscoped ledger under the key
`retention_ledger`, which (a) made the record useless as evidence of what c1 was shown and
(b) fed c4's re-compaction a set including facts first documented AFTER the span --
reintroducing the leak at the correction step. 

**Confirm it**
- Read the record's `audit.scores` for `mode` and `n_delta`, and `audit.created_at`.
- Compare `covered_hadm_ids` / `generated_hadm_ids` against the `admit_seq` of the offending
  condition's earliest source admission.
- Check `promotions.promoted_at` for the later admission versus the compaction's
  `audit.created_at`. In the traced case admission 1 aged out 41 seconds after admission 8 was
  promoted.

**Not recoverable**
The model's reasoning trace is not persisted. Whether the model INFERRED the future condition
or merely HEDGED cannot be distinguished retrospectively.

---

## FM-4 T4 lossy rollup

A recurrent-episode finding's own text is consumed as a grouping key and discarded. Traced
case: hypercalcaemia became a bare "Lymphoma count:1".

| | |
|---|---|
| Class | engineering / routing (the rollup shape) + clinical (whether T4 is the right class) |
| Status | open |
| Stages | b1 class assignment -> b2 `_project_patient` episode rollup -> b4 T4 collapse |

**Signature in data**
- A fact present in `retention_facts.jsonl` with `retention_class=recurrent_episode` and a
  populated `parent_condition`, absent from `nodes[]` / `flagged[]`, with an `episodes[]` entry
  whose `parent_condition` matches -- and no trace of the fact's own wording anywhere in the
  ledger.
- Whether such a fact reads MISSING or ROLLUP_ONLY in f1 reduces to LEXICAL OVERLAP between the
  instance text and the episode's `parent_condition` label: `c2._where_represented`'s episode
  test clusters `_content_tokens(f.text)` against `_content_tokens(ep["parent_condition"])`.
  Its node test is CLASS-GATED (`by_class.get(f.retention_class)`), so it cannot see the T3
  `chronic_disease_state` or T2 `sentinel_event` node that actually carries the concept, and
  its fact_id test can never fire because `EpisodeRollup` stores no fact_ids.

**Confirmed instances (test cohort, 75 patients / 1742 pinned facts)**
All eight of the cohort's f1 losses, sharing one signature: T4 / `recurrent_episode` /
`confirmed` / `negative_kind=resolved_relevant`. Method step 5 was run on all eight.

| subject | fact | retained instead as |
|---|---|---|
| 1 | Acute glycemic decompensation, glucose >500 | T3 `chronic_disease_state` T2DM x2; episode rollup parent "Diabetes mellitus" from the same hadm |
| 2 | Worsening ascites requiring paracentesis | T3 `chronic_disease_state` "Ascites" (n=6) |
| 3 | Left-sided pleural effusion | T2 `sentinel_event` "Pleural effusion (hydrothorax)..." |
| 4 | Acute renal failure (Cr 1.7) from hypovolemia, resolved | 7 x T2 renal transplant / ESRD / immunosuppression nodes |
| 5 | Acute hyperkalemia episode | T3 `chronic_disease_state` "Chronic hyperkalemia" |
| 6 | Hyperphosphatemia | T3 "CKD baseline GFR ~30" (n=8) |
| 7 | Painless rectal bleeding, resolved | 3 x T2 `sentinel_event` + T3 diverticulosis + T3 hemorrhoids |
| 8 | ARF on chronic renal failure (ROLLUP_ONLY) | episode parent "Chronic renal failure (CRF)" -- by design |

Six of eight are also restated in later clinician-authored BHCs. NO CLINICAL LOSS in any case:
concept-level retention held throughout; only instance-level retention failed. Case 1 is
the sharpest -- an episode rollup exists for that exact hadm and still reads MISSING, because
"Acute glycemic decompensation with glucose exceeding 500 mg/dL" and "Diabetes mellitus" share
zero content tokens.

Two consequences. (a) `gate_signeg` is red on this cohort for these eight and fails at ANY
`--threshold`; `f1`'s default was lowered to 0.995 as an FM-4 tolerance, which buys
attribution, not a green build. (b) `is_ledger_member` pins on `negative_kind is not None`,
but `_project_patient` branches on class BEFORE consulting `_is_candidate` - so for a T4 fact
the pin is UNREACHABLE BY CONSTRUCTION. Not a drop; a pin that can never be honoured.

Do NOT exempt T4/`recurrent_episode` from `gate_signeg` to clear the red. At fact level the
gate is a true positive; at concept level it is a false positive. The fix belongs in the homing
rule or the rollup shape, or upstream in what earns `resolved_relevant` at b1 -- cases 90000013
and 90000014/Hyperphosphatemia look over-assigned, both being lab derangements of an already-pinned
chronic state.

**Mechanism**
`_project_patient` routes `retention_class == "recurrent_episode"` to `episodes_raw` BEFORE
`_is_candidate` is consulted, so a T4 fact never reaches clustering. Rollup grouping key:
```
(f.parent_condition or f.text).strip().lower()
```
`EpisodeRollup` carries `parent_condition, count, first_date, last_date, source_hadm_ids` 
and nothing else. The finding's `text` survives ONLY when `parent_condition` is null (the key
then falls back to the text, and `parent_condition` is set from it). When `parent_condition`
IS set, the text is unrecoverable from the ledger.

**Confirm it**
- `source_hadm_ids` on the rollup gives you the admissions; go back to
  `retention_facts.jsonl` and read the original `text` values that were grouped.
- `tax.normalize_fact` reclassifies some `recurrent_episode` facts to `sentinel_event` on load
  (b2 prints a `disease-course reclassification` count). Check whether the fact you are
  chasing was reclassified before you conclude it was rolled up.

**Downgrade conditions**
Rollup is the intended behaviour for genuine recurrences - five COPD exacerbations SHOULD
collapse to a count. The defect is that a clinically distinct finding was labelled
`recurrent_episode` in the first place, or that the rollup has no room for a per-episode note.
Follow the patient's later course before calling it a loss (method step 5).

---

## FM-5 T5 silent fall-through

A confirmed fact that is not a ledger member and matches none of the three routing branches
lands in NO list, with no audit trace.

| | |
|---|---|
| Class | engineering / routing (the missing audit list) -- the pruning itself is often by design |
| Status | FIXED : the prune is recorded. The routing decision is unchanged |
| Stages | b2 `_project_patient` / `_is_candidate` / `tax.is_ledger_member` / `tax.prune_reason` |

**Signature in data**
- An entry in `dropped[]`, or a row in the
  `retention_dropped` table. `SELECT reason, count(*) FROM retention_dropped GROUP BY reason`
  is the starting query; `unclassified` is the one that warrants investigation.
- Typically `retention_class=transient_event`, `certainty=confirmed`, `negative_kind=None`
  (reason `not_pinned_tier`), or `suspected` + `resolved`/`in_remission` (reason
  `suspected_inactive`).

**Mechanism**
`_project_patient`'s branch chain:
```
certainty == "ruled_out"                  -> considered_excluded
retention_class == "recurrent_episode"    -> episodes_raw
_is_candidate(f)                          -> candidates
(else)                                    -> dropped        
```
`_is_candidate` returns `f.in_ledger` for anything not superseded and not ruled out. A confirmed
transient event with `negative_kind=None` is not a ledger member, so all three tests fail. It then falls to `dropped[]` with
`tax.prune_reason()` naming the branch of `is_ledger_member` that declined it.

Note `considered_excluded` is a list of BARE STRINGS (`sorted({f.text for f in excluded})`) -
even the ruled-out branch loses the fact's axes and `fact_id`. `dropped[]` does not have this
problem; it carries the full axes.

**Confirm it**
- Do not predict membership. Execute
  `tax.is_ledger_member(salience_tier, certainty, lifecycle, negative_kind, retention_class)`
  on the fact's exact axes, and `tax.normalize_fact(fact_dict)` first - b2 normalises on load,
  so the axes you see in the file may not be the axes routing saw.
- Then walk the three branch tests in order.
- **Start here, then verify.** `support/evidence.py` automates the same walk read-only, forwards
  through `retention_facts` -> `retention_dropped` -> `tiered_ledger_raw` -> `tiered_ledger`, and
  names the FIRST stage that lost the fact (S1a/S1b/S1c/S1d). It executes the taxonomy functions
  rather than predicting them, and `retention_dropped.reason` carries `prune_reason()` output plus
  the `taxonomy_version` that decided it. Treat its answer as a starting point:
  the standing instruction to confirm the mechanism against live source still applies, and the
  collector's own version may lag the code.

**Downgrade conditions**
Often correct: a prunable transient event that never recurs SHOULD not occupy a lifetime
ledger slot. It is a defect only when the fact was mis-labelled `transient_event`, or when the
patient's later course shows it mattered. Method step 5 is mandatory here - several apparent
FM-5 cases were correctly reclassified as by-design. The volume is substantial (~26 dropped
facts per patient on the 100-patient cohort), so treat `dropped[]` as a haystack to query by
reason, not a worklist.

**Remaining gap**
b4's T5 recency prune (`CONSOLIDATE_T5_PRUNE_DAYS`) removes nodes from the SERVED ledger and is
not covered by `dropped[]`. Those are recoverable from `retention_ledger.raw.jsonl`, so the
prune is reversible, but there is no per-node reason recorded - only aggregate before/kept/
pruned counts in b4's console output.

---

## FM-6 Verify-against-derived, not against source

A correction step that cannot see ground truth can only soften a fabrication, never delete it.
Shared root of FM-1 and FM-3.

| | |
|---|---|
| Class | prompt wording / engineering (what is passed to the step) |
| Status | open -- shared fix candidate |
| Stages | b1 `_UPDATE_TMPL`; c1 `_INCR_TMPL` / `_FULL_TMPL`; c4 correction |

**Signature in data**
- A fabricated or unsupported claim that PERSISTS across a repair or correction step but
  appears with weaker language, lower certainty, or a lower tier afterwards.
- `compaction_change.action = 'corrected'` with `resolved_after = false`.

**Mechanism**
- b1: `_FEEDBACK_TMPL` receives the note; `_UPDATE_TMPL` receives only issues + facts. The step
  that can DETECT has evidence; the step that ACTS does not.
- c1: the compactor is given the retention ledger and the aged-out `bhc_delta`, and is told not
  to contradict the ledger. When the ledger and the source disagree, the ledger wins by
  instruction - but the ledger is derived.
- c1 incremental mode compounds this: the seed for fold k is the PROSE output of fold k-1
  (`existing_prior_md`), not the source. Errors are inherited and re-endorsed.

**Confirm it**
- Diff the pre- and post-repair fact sets. Softening without deletion is the signature.
- For c4, read `compaction_change.claim` / `correction` / `validator_evidence` for the subject
  and check `resolved_after`.

**Fix candidate**
Pass the source note (or the relevant `bhc_delta` span) to every correction step, so the step
that acts has the same evidence as the step that detects. Cost: context budget, and for b1 a
second full-note pass per repair.

---

## FM-7 Curated condition-key over-match swallows a root diagnosis

A confirmed chronic-disease fact is absent by keyword from every ledger bucket
(nodes/flagged/considered_excluded/dropped/amended) for a patient who plausibly has
the condition -- but its fact_id is alive inside a *different* node, merged and
relabelled under one of its own known complications.

| | |
|---|---|
| Class | engineering / routing |
| Status | **PARTLY FIXED.** Guard - (`_split_cluster` / `_split_group`). Tokenisation hole closed  (`_normalize_surface`) - see residual below. Artifacts generated before those dates still show it: check `generated_at` on the ledger record. |
| Stages | b1 (produces free text with causal qualifiers) -> b2 `_cluster` (compound-fact bridge, the dominant path) -> b2 `_build_node` (recency-wins text attribution) -> b2 `_CONDITION_KEYS` / `_consolidate` (the same bridge one stage later) |

**Signature in data**
- A T1-T3 confirmed fact in `retention_facts.jsonl` matches no node/flagged/dropped/amended
  entry BY TEXT in `retention_ledger.jsonl` for that subject.
- Its `fact_id`, however, IS present inside another node's `fact_ids` array -- one whose
  own `retention_class`/`salience_tier`/`text` describe a related but distinct condition
  (typically a well-known complication of the swallowed one).

**Mechanism**
1. `_CONDITION_KEYS` in `b2_build_ledger.py` is a curated regex -> canonical-key list
   (e.g. `\bcirrhosis\b -> "cirrhosis"`), matched against a fact's ENTIRE free text --
   not scoped to whether the condition is that fact's own subject.
2. A fact describing a different condition that merely references the curated term
   causally (e.g. "Pancytopenia secondary to splenic sequestration from cirrhosis")
   matches the same canonical key as the actual diagnosis fact.
3. `_consolidate()` merges same-key facts, gated only by the coarse `_MERGE_GROUP`
   compatibility class ("disease"), which does not distinguish a root diagnosis from
   its own complications.
4. `_build_node()` takes text/certainty/lifecycle/class from the LATEST-dated cluster
   member. The index admission (often the oldest) can never win this contest once
   absorbed into a cluster with any later admission.
5. Net: fact_id preserved, identity erased. Not caught by FM-5's `dropped[]` audit --
   the fact was never dropped, so no `prune_reason` fires.

**When NOT to call it a defect:** if the curated key correctly merged two mentions
that are genuinely the SAME diagnosis in different wording, that's the intended,
safe behaviour `_CONDITION_KEYS` exists for. It's only this failure mode when the
match is a causal/etiological MENTION of the condition inside a fact about something
else - check whether the "swallowing" node's own text plausibly IS the same
diagnosis before concluding FM-7.

**Confirm:** get the fact_id from `retention_facts.jsonl`; grep every node's
`fact_ids` array (not text) for it; if found inside a node whose own text/class/tier
doesn't match, check `_CONDITION_KEYS` and `_cluster`'s token overlap in
`b2_build_ledger.py` for why they were judged the same condition.

**Corrected mechanism.** The original write-up above blamed `_CONDITION_KEYS`
and `_consolidate`. A cohort scan of all three cohorts found 106 affected nodes and a
compound bridge in **106 of 106** - the curated key is one route to it, not the cause, and
49 of the 53 orphaning events in the test cohort happen at `_cluster` / `_build_node`, one
stage earlier than `_consolidate`. The driver is a fact naming two conditions in one string
("Coronary artery disease and peripheral vascular disease"; "Chronic thrombocytopenia likely
secondary to cirrhosis"). It matches both, bridges them into one group, and latest-wins
wording then drops whichever condition is absent from the newest phrasing.

**Fix.** `_split_cluster` (after `_cluster`) and `_split_group` (inside `_consolidate_group`)
partition a group so no member is left sharing zero identity tokens with the displayed text;
the latest member seeds each partition, so latest-wins is preserved within an identity. Can
only increase entry count. `code_status_directive` is exempt (`_SPLIT_EXEMPT`) - a directive
legitimately mutates and the most recent one is the patient's wish.

**Detector**, still useful on pre-fix artifacts: for each node with >= 2 `fact_ids`, compare
each member fact's specific tokens (`_content_tokens` minus `_QUALIFIERS`) against the node's
own text. An empty intersection is the signature. Then check whether the orphaned condition
appears in any OTHER node for that patient before calling it an erasure - roughly a third of
orphans were duplicates of a condition represented elsewhere.

**Residual.** Not complete cover. `Pancytopenia secondary to hypersplenism` remains
inside the `Hepatitis B virus cirrhosis` node after the fix, because it shares an identity
token with the seed.

---

**Residual: the guard is only as good as the tokens**
`_split_cluster` partitions on `_identity_tokens`, so any junk token shared by two unrelated
conditions defeats it. `_content_tokens` split on `[A-Za-z0-9]+`, which shattered the possessive
and the `s/p` abbreviation into a bare `s` -- absent from both `_STOPWORDS` and `_QUALIFIERS`, so
it survived `_specific()`. `"Crohn's disease"` and `"Coronary artery disease s/p LAD stent
placement"` shared exactly `{s}` at overlap 0.667 >= `CLUSTER_THRESH`, clustered, and the guard
returned a SINGLE partition because the seed's identity tokens carried the same `s`. A CAD fact
was served as "Crohn's disease". Executed live the guard still returned one partition - this was not a stale-artifact case.

Closed by `_normalize_surface()` in `b2_build_ledger.py`, applied by BOTH `_content_tokens` and
`_phrase`: strips the possessive suffix (ASCII and U+2019) and drops `s/p`. Verified over the full
`_project_patient` path on all 100 `data_test` subjects -- 6835 rows and 3489 nodes unchanged, one
subject changed. Folding to `crohns` was tested and rejected (it split "Crohn's disease" from
"Crohn disease" and "Alzheimer's dementia" from "Alzheimer disease dementia").

**The general lesson stands: single-character and morphological artefact tokens bridge unrelated
conditions.** `data_test` carries 332 `2`, 254 `1`, 140 `c`, 66 `x` single-char content tokens.
Digits are load-bearing ("Type 1" vs "Type 2 diabetes") and `c` is load-bearing ("Hepatitis C"),
so a blanket single-char filter is NOT safe. When tracing a cross-condition merge, print
`_identity_tokens` for every member against the seed and check whether the shared set is
meaningful -- do not assume the guard covered it.

---

## FM-8 Superseded safety-class fact silently unserved

A T1 contraindication tagged `lifecycle=superseded` is un-pinned by the gate, routed to
`amended` by b2, and disappears from the served ledger leaving no trace in ANY audit list.

| | |
|---|---|
| Class | clinical / taxonomy (branch order) + prompt wording (b1 mis-assigns `superseded`) |
| Status | FIXED at `_build_node`. Prompt fix STAGED, not yet exercised. **Two residuals OPEN at `_merge_group` and in the guard's null handling - patches written and sandbox-tested** |
| Stages | `tax.is_ledger_member` / b2 `_project_patient` / b2 `_build_node` / `tax.render_for_prompt` |

**Signature in data**
Membership in `amended` is the ONLY signature. The fact is absent from `nodes`, `flagged`,
`tiered_ledger`, `dropped[]` and `considered_excluded`, and c2 does not flag it because
`_ledger_index` (c2:74) counts `amended` as full retention. Nothing positive to look for
anywhere else.

Starting query on a pre-fix ledger:
```
amended node WHERE retention_class IN (severe_adr_allergy, code_status_directive)
```
Aggravated when `n_mentions == 1` -- a singleton has no earlier mention to supersede, so
`superseded` cannot be a retraction and must be a mis-tag.

**Mechanism**
Two independent gates, both wrong in the same direction:
1. `is_ledger_member` tested `lifecycle == "superseded"` at row 2, ABOVE the
   `_SAFETY_ROBUST_CLASSES` branch at row 3 -- contradicting that branch's own docstring
   claim of robustness to *lifecycle* mis-tagging. The fact un-pins.
2. b2 `_project_patient` routes on `node.current_lifecycle == "superseded"` DIRECTLY, never
   consulting `is_ledger_member`. So even a pinned safety node still landed in `amended`.
   Fixing the taxonomy alone does not serve the fact.

`_is_candidate`'s explicit `if lifecycle == "superseded": return True` re-admits the fact so a
de-label can amend the cluster it retracts - which is why it becomes a node at all rather
than falling to `dropped[]` and firing `prune_reason`. The FM-5 audit trail does not cover it.

**Audit, `clin_test_data`, 110 patients / 11,930 facts**
- 6 `amended` nodes total, **all `n_mentions == 1`**. The de-label cancel path has NEVER
  fired in either cohort - the mechanism is entirely unexercised.
- 4 of 6 are T1 `severe_adr_allergy`, all absent from the served ledger:
  `data_test` 90000015 (lisinopril); `data_gen` 90000016 (amiodarone), 90000012 (amiloride),
  90000018 (warfarin, `patient_reported`).
- `data_gen`: 3 of 14 `severe_adr_allergy` facts affected (21%). `data_test`: 2 of 69 (2.9%).
- 4/4 are drug SUBSTITUTION, not record retraction. For 90000015 the source order settles it:
  seq 1 records hypertension treated with lisinopril, seq 6 the adverse reaction - the ADR is
  the LATER event and its lifecycle should be `active`.
- Genuine de-labels were instead expressed as `certainty=ruled_out` (3 cases, incl. 90000019
  penicillin-associated haemolysis) and routed correctly to `considered_excluded`. B1 had the
  two senses exactly inverted relative to the taxonomy.
- The other 2 are benign: 90000020 PEG tube superseded by a served G-J tube (device
  replacement, working); 90000021 a T5 `transient_event` that `not_pinned_tier` would have
  pruned anyway.

**Fix**
- `is_ledger_member`: safety-class branch moved ABOVE the superseded guard.
  `certainty=ruled_out` is now the SOLE exit from a safety-class pin.
- `tax.is_safety_robust()` added as a public accessor.
- b2 `_project_patient`: safety classes exempt from the `amended` route, so a de-labelled
  allergy is SERVED as an explicit negative rather than vanishing.
- b2 `_build_node`: when the latest mention is `superseded`, it wins `negative_kind` outright
  instead of going through `merge_negative_kind`. Merge priority ranks `None` (present)
  highest, so a de-label cluster (original `nk=None` + de-label `nk=absent_or_negative`) would
  have resolved to `None`, dropped the review-UI "(absent)" badge, and rendered a de-labelled
  allergy as LIVE. Latent while `paired == 0`; fires the moment the prompt fix makes de-labels
  cluster with their targets.
- `render_for_prompt`: `lifecycle` was injected as a bare pipe-joined list with no guidance at
  all. Now states that `superseded` withdraws the RECORD not the treatment, that a first
  appearance is never superseded, and that a drug stopped for a reaction is `active`.

**Residual.** `paired == 0` means the de-label cancel path remains unproven end-to-end even
after the fix. Until a genuine clustered de-label appears in cohort data, that path is
untested in production. The `_build_node` retraction rule is likewise unexercised.

**Residual 1b - the retraction guard has no counterpart at `_merge_group`.**
The fix put the retraction rule in `_build_node` only. `_merge_group`, which runs one
stage later inside `_consolidate`, still resolved `negative_kind` through
`tax.merge_negative_kind` - which ranks `None` (present) HIGHEST. So the same de-label cluster
that `_build_node` now protects can have its negative stripped again the moment the node is
consolidated with a live sibling, rendering a de-labelled allergy as LIVE. This is FM-8 exactly,
one stage later.

Scope is bounded by construction: only `severe_adr_allergy` and `code_status_directive` can
reach `_consolidate` while `superseded`, because `_project_patient` routes every other class's
superseded node to `amended` first. For those two it is a real hole.

**Not observed in cohort data, and demonstrable in isolation.** Zero merges on `data_test`
absorb a superseded node (only ONE safety-robust superseded node reaches `_consolidate`
cohort-wide), so no artifact shows it. Constructed directly against `_merge_group`, a correctly
labelled de-label that is the newest member of its group is served with `negative_kind=None`
today. Do not read the clean cohort as evidence the path is safe - read it as the same
`paired == 0` problem above.

**Residual 1c - the `_build_node` guard propagates a null the extractor never set.**
The guard takes `latest.negative_kind` outright. When b1 tags a fact `superseded` without
setting `negative_kind`, that value is `None`, and the guard faithfully forwards it - producing
`current_lifecycle=superseded` with `negative_kind=NULL`, which is the incoherent combination
§7 of `HOW_TO_READ_THE_LEDGER.md` already warns physicians about. The guard does not create the
mis-tag, but it is the reason the mis-tag survives to the served row.

Live count, `data_test` : **1 served node** -- `Adverse reaction to lisinopril presenting as breathlessness`, T1 `severe_adr_allergy`,
`confirmed`, `superseded`, `negative_kind` NULL. Plus 2 in `amended` (not served): a
lisinopril nephrotoxicity and a PEG tube.

**Patches written and sandbox-tested; NOT APPLIED (code freeze for release).**
- (a) `_merge_group`: when the NEWEST member is `superseded`, it supplies both `negative_kind`
  and `current_lifecycle`, matching `_build_node`'s latest-wins retraction rule. Provenance is
  re-attributed to it (FM-2).
- (b) `SUPERSEDED_DEFAULT_NK = "absent_or_negative"`, used by both `_build_node` and
  `_merge_group` when a retraction carries no explicit kind. **Mechanical, not a clinical
  judgment:** `NEGATIVE_KIND` has three non-null values and two are wrong for a retraction by
  definition -- `resolved_relevant` means the thing happened and ended, `surveillance` means it
  is being watched for. A withdrawn record is neither, so `absent_or_negative` is the only
  coherent value. The default is entailed by the taxonomy's own definitions.

**The test condition is LATEST-is-superseded.** The first draft used
"any", and a synthetic case caught it: an allergy de-labelled in 2020 and re-documented in 2023
was served as absent, because a stale retraction outranked a live re-assertion. That is a worse
failure than the one being fixed. Preserve the `ordered[0]` test if this is ever rewritten.

Sandbox result over all 100 `data_test` subjects: node identity unchanged everywhere
(3819 nodes before and after, identical bucket and `fact_ids` sets), **3 attribute changes, all
from (b)**, all `negative_kind: None -> absent_or_negative`; 1 served, 2 in `amended`. Patch (a)
fires zero times, as predicted. The invariant `superseded AND negative_kind IS NULL` on served
nodes goes 1 -> 0. So applying both is a near-no-op on current artifacts - which is the argument
for landing it cheaply, and the reason it will stay unexercised.

**One of the three changed nodes is an upstream mis-tag, not a case against the default.** `Prior lisinopril-associated nephrotoxicity, previously discontinued then resumed at lower dose`. A drug that was RESUMED is drug SUBSTITUTION, not record retraction, so
`lifecycle=superseded` is wrong at b1 -- the exact confusion the audit found in 4 of 4
`amended` nodes and that `render_for_prompt` was reworded to prevent. The default propagates a
wrong premise faithfully. Remedy is re-extraction of that admission, not a different default.
Unserved today (it is in the `amended` bucket).

**Terminology, since this trips readers.** `amended` is a BUCKET in `RetentionLedger`
(alongside `nodes`, `flagged`, `episodes`, `considered_excluded`, `dropped`), not a status value.
There are exactly four lifecycle values - `active`, `resolved`, `in_remission`, `superseded`
(`tax.LIFECYCLE`). The bucket is named for the ACT (amending) while its contents carry the STATE
(`superseded`), which reads like a fifth status but isn't - interpret `amended` as 'RETRACTED' instead.

**No gate would have caught either.** `negative_kind` on a NODE drives the review-UI badge and
the c1 prompt rendering; f1 pins and scores on FACT-level `negative_kind` and homing, neither of
which moves. So `gate_signeg` is green on the affected subject both before and after. Same
blindness that let FM-8 run undetected - the fix rests on reasoning, not on a failing test.

**Cheap tripwire** (evaluation only, does not correct the ledger): flag any `amended` node
with `n_mentions == 1` in a safety class. Zero cost, catches recurrence of the prompt-side
defect without waiting for a re-extraction.

**Detector.** A report control on every ledger row in the physician UI; the support-ticket triage policy 
escalates this exact combination and the card is marked urgent. This fires only once a clinician has already read the misleading row.

**Derive at SERVE time; do not persist a column.** The axes needed are already stored on
`tiered_ledger`, so a persisted flag would add nothing queryable (the worklist query below works
today), while costing a migration and leaving every pre-migration artifact unfixed. Deriving it
when the row is served instead corrects EVERY existing record immediately, with no re-run and no
schema change:

1. **Taxonomy** (version bump + CHANGELOG + clinical sign-off). Add
   `is_incoherent_retraction(retention_class, current_lifecycle, negative_kind)` - true when the
   class is safety-robust, lifecycle is `superseded`, and `negative_kind` is NULL. One definition,
   testable, versioned with the gate it belongs to, and shared by the API and the f-suite
   tripwire so the two cannot disagree.
2. **API.** `longsum_api` computes it per row and returns it as a derived field alongside the
   ledger node. Python-side, so the single definition is actually reused rather than
   re-implemented in JS.
3. **Render distinctly** in `ledgerTable` -- not the bare word `superseded`. State the reading
   rule at the point of use, e.g. `superseded (mis-tagged - read as ACTIVE)`, in the red badge
   style already used for `(absent)`. Do NOT silently rewrite the lifecycle to `active`: that
   hides a live data defect behind a correct-looking display, and the mis-tag still needs fixing
   at b1.

This is containment, not repair -- the same split the feedback loop uses. The display stops the
row being misread today; the b1 re-extraction fixes the record on the normal cadence.

**Worklist query for a batched re-extraction** (no schema change needed -- the axes are stored):

```sql
SELECT subject_id, hadm_id, fact_id, salience_tier, retention_class, text
  FROM retention_facts
 WHERE lifecycle = 'superseded'
   AND negative_kind IS NULL
   AND certainty <> 'ruled_out'
   AND retention_class IN ('severe_adr_allergy','code_status_directive');
```

---

## FM-9 Axis mis-assignment on faithful text

b1 extracts a fact whose TEXT is supported by the note and assigns it a `retention_class` from an
unrelated part of the taxonomy. Structurally valid (the schema is enum-constrained), content
faithful, axis wrong. Every downstream stage carries the label without question.

| | |
|---|---|
| Class | prompt wording / model judgment at b1. NOT engineering -- routing does exactly what the label tells it |
| Status | OPEN. No fix landed. One instance did not reproduce at current code (see below) |
| Stages | b1 `_FACTS_SCHEMA` (L119 enum) / b1 `_normalise_fact` / `tax.normalize_fact` (no-op here) / everything downstream |

**Why the register needed this**
Cross-cutting theme 1 already states that b1 class assignment is the highest-leverage step in the
pipeline, then registers no mode for it failing. FM-1 is b1-origin but covers FABRICATED content;
FM-2 covers a wrong label produced by b2 assembly. "Correct content, wrong axis, straight out of
b1" had no entry.

**Signature in data**
There is no single grep. The label is valid, so no validator fires, `prune_reason` is often None,
and nothing lands in `dropped[]`. Three sub-signatures, each found by a different instrument:

1. **Isolated slip** -- one fact in a call, class semantically unrelated to the text, other facts
   in the same call correct. Found only by embedding kNN (D3). Example "Lisinopril-induced cough" -> 
   `anatomy_altering_surgery`, T2, in ledger.
2. **Template block** -- several facts sharing a syntactic template fail together. Example
   : three "X performed in the past" surgical and device facts ->
   `severe_adr_allergy` T1, while the other 13 facts in the call are correct. Rationales were
   CORRECT and contradicted their own labels.
3. **Severity-gated substitution** -- the model reads `severe_adr_allergy` as a severity gate and,
   when severity is unstated, substitutes a class suggested by a surface word. Example
   "Tylenol (acetaminophen) allergy documented" ->
   `altered_immune_status`, rationale "...severity not specified", while the magnesium allergy in
   the SAME call (documented hives, throat swelling) went correctly to `severe_adr_allergy` T1.
   Directly violates b1 prompt L180: a mild reaction is `severe_adr_allergy` at a LOW tier, not a
   new class. The class NAME is a plausible contributing cause.

Starting queries (`retention_facts`):
```sql
-- reaction-shaped text outside the safety class
SELECT * FROM retention_facts
WHERE text ~* '\m(allergy|allergic|intoleran|hypersensitiv|anaphyla)'
  AND retention_class <> 'severe_adr_allergy';
-- procedure-shaped text with no procedure morphology
SELECT * FROM retention_facts
WHERE retention_class = 'anatomy_altering_surgery'
  AND text !~* '(ectomy|ostomy|otomy|plasty|resect|amputat|transplant|bypass|graft|s/p)';
```

**Mechanism**
`_FACTS_SCHEMA` constrains `retention_class` to `tax.class_ids()`, so the model must return a
valid member -- it cannot return an invalid one, and `_normalise_fact`'s unknown-class fallback
never fires. `tax.normalize_fact` repairs only surveillance promotion and `recurrent_episode`
reclassification, so no taxonomy function can correct a wrong-but-valid class. `floor_tier` then
applies the WRONG class's default tier, compounding it.

**Consequence depends on direction**
- INTO a safety class: the fact gains a T1 pin that `_SAFETY_ROBUST_CLASSES` makes unreachable by
  any exit but `certainty=ruled_out`. 10 of 69 `severe_adr_allergy`
  facts (14.5%) are not drug or substance reactions -- PICC lines, an LVAD, CABG, hip replacement,
  an ileocecectomy. All `confirmed`, so physician-immovable. This is the cost of the FM-8 fix,
  argued at FM-8 lines 68-88 and now measured. 
- OUT OF a safety class: the fact loses the pin and can be pruned. ("Aspirin intolerance preventing aspirin use") went to   `altered_immune_status` and was pruned `patient_reported_other_class` - NOT a tier prune. 
  The fact is T2; the `patient_reported` branch reads only `retention_class`. `severe_adr_allergy` 
  IS in `_PATIENT_REPORTED_RETAIN`, so the class error alone caused the loss. Never restated across 8 admissions.

**Confirming**
Read the RAW b1 record in `interim/retention_facts.jsonl`, not the DB, and run
`tax.normalize_fact` on it - if it returns `(False, False)` and the DB matches, b1 is the origin.
Then diff the fact text against `bhc_text`: if the text is supported and only the axis is wrong,
this is FM-9 and not FM-1. Check the other facts in the same call to tell sub-signature 1 from 2.

**When it is NOT a defect**
- Where the class boundary is not present in the note text. `permanent_condition` /
  `recurrent_episode` / `sentinel_event` are not lexically separable from `chronic_disease_state`
  (D2 mined-anchor recall 18.6% / 30.5% / 35.7%; D3 kNN purity 0.25 / 0.37-0.44 / 0.42-0.44,
  against 0.91-0.93 for `code_status_directive`). Disagreement inside that band is expected.
- Compound facts. One fact naming two class-worthy concepts must be filed under one of them, so
  the loser is arbitrary. That is a b1 ATOMICITY failure (prompt L163-178), not an axis failure.
- Follow the subsequent course before calling a prune a loss. Of five apparently-lost drug
  intolerances, three were followed by documented re-exposure to the same drug.

---

## Cross-cutting themes

1. **In b2, the class label alone determines survival.** `_project_patient` is a disjoint
   three-way switch on `certainty=='ruled_out'` / `retention_class=='recurrent_episode'` /
   `_is_candidate`. A single mis-labelled `retention_class` at b1 decides whether a fact is
   pinned, rolled up, or dropped. This makes b1 class assignment the highest-leverage step in
   the pipeline.
2. **This exit lose information silently.** The T4 rollup (FM-4).
3. **Corrections cannot reach ground truth** (FM-6). Every self-critique in the pipeline
   compares a derived artifact against another derived artifact.
4. **Derived state is snapshotted in three places with different currency.** `in_ledger` is
   derived live in the files, snapshotted at load in `retention_facts.in_ledger` (DB), and
   snapshotted again as `tiered_ledger.provisional`. Under a taxonomy change these can disagree.
   **Rule: one side of every staleness comparison must be the LIVE code version**
   (`retention_taxonomy.TAXONOMY_VERSION`). Only artifact-vs-code answers "is this record current".
5. **The judge is a triage signal, not a finding.** deepseek-r1:32b Not Supported PPV is about
   0.24. A `failed_statement` justifies looking; it does not establish a fabrication. Only a
   grep of `bhc_text` does -- see theme 7 for how to run that grep safely.
6. **Reasoning traces are not persisted.** For any question of the form "did the model infer X
   or merely hedge", the answer is not recoverable from the artifacts.
7. **Substring matching over-matches, in the direction that hides defects.** The register's
   primary fabrication test is a grep of `bhc_text` (FM-1, theme 5). Run naively it produces
   false SUPPORT: `adrenal` matches `hypoadrenalism`; a matched term may sit inside a negation
   ("no evidence of"), or in an admission unrelated to the one under investigation. Each of
   those reads as "the note supports it" and closes the enquiry. Match on word boundaries, fold
   plurals, check a window before the hit for negation cues, and score each admission's note
   SEPARATELY rather than the patient's notes as one blob. The pipeline's own code already does
   this -- `c2._where_represented` clusters on `_content_tokens` -- so the hazard is in manual
   analysis and in prose instructions that say "grep", not in the pipeline.
8. **Absence from the ledger is EXPECTED for the incoming admission.** The ledger is the
   established record, built from PRIOR admissions. A fact documented for the first time in the
   admission under review is correctly absent until fold-in, and looks identical to a retention
   loss from the reader's seat. Establish WHICH admission documents a fact before treating its
   absence as a defect: scoring each admission's note separately (theme 7) settles it in one
   step, and `support/evidence.py` reports it as sub-class S1e.

---

## Adding a mode

A new entry earns its place only after a full trace (method steps 1-6), including step 5 --
following the patient's subsequent course to check the loss actually bit. Give it:
an ID, a defect class, a status, a signature stated in terms of what to grep, the mechanism
with the function names to read live, how to confirm it, and the conditions under which it is
NOT a defect. Note the artifact generation date if the mechanism has since changed.
