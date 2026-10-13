# Ledger case studies

Teaching aids for clinical readers. Three worked traces showing how a fact moves
from b1 extraction to what a physician finally sees, and what changes when one
field is different.

**All clinical text in this document is invented.** 

---

## How to read these

Every extracted fact from discharge summaries carries six taxonomy fields:

| Field | Values |
|---|---|
| `retention_class` | one of 12 (`severe_adr_allergy`, `chronic_disease_state`, `recurrent_episode`, `transient_event`, ...) |
| `salience_tier` | T1 (never drop) .. T5 (prune past window) |
| `certainty` | `confirmed` / `suspected` / `ruled_out` / `patient_reported` |
| `lifecycle` | `active` / `resolved` / `in_remission` / `superseded` |
| `occurrence` | `index` / `recurrence` |
| `negative_kind` | `null` / `absent_or_negative` / `resolved_relevant` / `surveillance` |

`negative_kind = null` means **the condition is present**. It does not mean unknown.

### The buckets a fact can land in

The pipeline sorts every fact into exactly one of six places:

| Bucket | Meaning | Reaches the served summary? | 
|---|---|---|
| `nodes` | confirmed current state | yes | 
| `flagged` | suspected / patient-reported | yes | 
| `episodes` | recurrent-episode frequency roll-ups | **no** | 
| `amended` | RETRACTED - the record was withdrawn | **no** | 
| `considered_excluded` | ruled-out, kept as a bare text string | **no** | 
| `dropped[]` | pruned, with the taxonomy's reason | **no** | 

Only `nodes` and `flagged` are read by the scripts generating the longitudinal summaries.
Everything else is audit surface. Four of the six buckets are "hidden" from the clinician *reading the summary*, 
but visible to the clinician *reviewing the pipeline*.



---

## Case 1 - The de-labelled penicillin allergy

**Teaches:** why `retention_class` alone decides whether a retraction is shown or
withheld; what `SUPERSEDED_DEFAULT_NK` is for; why the safety classes are special.

### The record

- Admission 3: `Penicillin allergy`
- Admission 9: `Penicillin allergy de-labelled` (graded oral challenge, tolerated)

### b1 output

| | F1 (adm 3) | F2 (adm 9) |
|---|---|---|
| `retention_class` | `severe_adr_allergy` | `severe_adr_allergy` |
| `salience_tier` | T1 | T1 |
| `certainty` | `confirmed` | `confirmed` |
| `lifecycle` | `active` | `superseded` |
| `negative_kind` | `null` | `null` |

Note F2's `negative_kind` is `null`. The extractor recorded a de-labelling but did
not label it as a negative. This is the ordinary case, not a defect.

### Trace

**1. `tax.normalize_fact` (on read, every consumer).** Neither text matches
`is_surveillance_finding`; neither class is `recurrent_episode`. No change.

**2. `tax.is_ledger_member(F2)`.** Branch order matters:

```
certainty == "ruled_out"?                       no
retention_class in _SAFETY_ROBUST_CLASSES?      YES -> return True
```

The safety branch sits **above** the `lifecycle == "superseded"` guard. 

**3. `_project_patient` routing.** Not `ruled_out`, not `recurrent_episode`,
`_is_candidate` returns True (it returns True for *any* superseded fact, so a
retraction can reach its cluster). Both facts -> `candidates`.

**4. `_cluster`, within `retention_class = severe_adr_allergy`.**

```
_content_tokens(F1) = {penicillin, allergy}
_content_tokens(F2) = {penicillin, allergy, de, labelled}
shared               = {penicillin, allergy}
_specific(shared)    = {penicillin, allergy}      -> 2 >= _MIN_SHARED (1)   OK
_overlap             = 2 / min(2, 4) = 1.00       -> >= CLUSTER_THRESH (0.6) OK
```

They cluster. Note the min-denominator: a short phrase fully inside a longer one
scores 1.00 however much extra the longer one carries.

**5. `_split_cluster`.** `severe_adr_allergy` is not in `_SPLIT_EXEMPT` (only
`code_status_directive` is), so the guard runs. F1's identity tokens intersect
F2's, so no split.

**6. `_build_node`.** Latest by `admit_seq` is F2:

```
text, class, certainty, current_lifecycle  <- F2 (latest wins)
salience_tier                              <- strongest across cluster = T1
negative_kind: latest.lifecycle == "superseded"
               -> nk = F2.negative_kind or SUPERSEDED_DEFAULT_NK
               -> nk = "absent_or_negative"
```

The retraction wins **outright** rather than going through
`merge_negative_kind`. If it did not, `null` (rank 4, "present") would beat
`absent_or_negative` (rank 1) and the node would render as a live allergy. That
is FM-8 residual (a). And `SUPERSEDED_DEFAULT_NK` is FM-8 residual (b): without
it the guard would propagate F2's `null` and produce the exact state it exists to
prevent.

**7. The `amended` test.**

```
current_lifecycle == "superseded"  AND  not tax.is_safety_robust(class)
```

Second condition is False. **Not** routed to `amended`.

**8. `_consolidate` / b4 / c1.** Single node in its compatibility group; passes
through. T1, so b4 leaves it untouched. `c1._node_to_fact` picks it up from
`nodes`.

### What the clinician sees

> Penicillin allergy de-labelled

carried as an explicit negative (`negative_kind = absent_or_negative`), which the
review UI renders with an "(absent)" badge. The de-labelling is stated. The
patient can receive penicillin.

### Flip one field

Suppose b1 assigns F2 `retention_class = chronic_disease_state` instead - an axis
error, not a content error. F2's text is unchanged and faithful.

| Step | What changes |
|---|---|
| `is_ledger_member` | not safety-robust -> falls to `lifecycle == "superseded"` -> False |
| `_is_candidate` | still True (superseded facts always are) |
| `_cluster` | runs per class, so F2 no longer clusters with F1 - two separate nodes |
| `amended` test | F2's node: superseded AND not safety-robust -> **`amended`** |
| F1's node | untouched: `active`, `negative_kind = null` |

The clinician now sees **`Penicillin allergy`**, live, with no mention of the
de-labelling. The retraction is in `amended`, which `d3` does not load and which
C2 counts as represented. No `dropped[]` entry, no prune reason, no flag.

**Retain this.** A single wrong class turns a resolved allergy into a permanent,
invisible antibiotic restriction. `retention_class` is not a label on the fact -
it selects the routing rule.

---

## Case 2 - The lymph node biopsy

**Teaches:** the `ruled_out` vs `absent_or_negative` boundary, the two very
different places those land, and how a pinned negative can still be aged out.

### The record

- Admission 4: `Cervical lymphadenopathy, excisional biopsy negative for lymphoma`

One clinical event, two defensible extractions.

### Reading A - `certainty = ruled_out`

| Field | Value |
|---|---|
| `retention_class` | `transient_event` |
| `salience_tier` | T5 |
| `certainty` | `ruled_out` |
| `lifecycle` | `resolved` |
| `negative_kind` | `null` |

Trace: `normalize_fact` finds no `_SURVEILLANCE_RE` match (there is no
"concerning for", no growth term). `is_ledger_member` returns False at the first
line. `_project_patient` branch 1 catches `certainty == "ruled_out"` and appends
to `excluded`, which becomes:

```python
considered_excluded = sorted({f.text for f in excluded})
```

A **set of bare strings**. No node, no `fact_id`, no provenance, no
`source_hadm_id`, no date, and no `dropped[]` entry. Duplicates across nine
admissions collapse to one string.

Downstream: not read by `c1` or `d_coordinator`, so it is absent from the served
summary. It does appear in the review UI and in the f6 incoming reference, as
"ruled out (worked up, excluded): ...".

### Reading B - `negative_kind = absent_or_negative`

| Field | Value |
|---|---|
| `retention_class` | `transient_event` |
| `salience_tier` | T5 |
| `certainty` | `confirmed` |
| `lifecycle` | `resolved` |
| `negative_kind` | `absent_or_negative` |

Trace: `is_ledger_member` -> not `ruled_out`, not a safety class, not
`superseded`, then `negative_kind is not None` -> **True**. Pinned, at T5, purely
on the strength of the negative flag. It becomes a node with full provenance and
reaches the served summary.

### Which is correct

The taxonomy answers this directly. `render_for_prompt` states that a lymphoma
considered then excluded is `certainty = ruled_out` - record it, do not pin it.
The `transient_event` counter-examples point the other way for screening results
("normal screening colonoscopy with date -> `absent_or_negative`").

The distinction is **what was being asked**. A differential entertained and
disproved is `ruled_out`. A test performed whose negative result carries forward
("HIV negative", "colonoscopy normal 2024") is `absent_or_negative`. The biopsy
above is the first, because the lymphoma was a hypothesis under test.

### Flip one field

Change the text to `Cervical lymphadenopathy, node concerning for lymphoma,
biopsy pending`, `negative_kind = null`.

`normalize_fact` now matches `_SURVEILLANCE_RE` on "concerning for ... lymphoma"
and sets `negative_kind = "surveillance"` deterministically, regardless of what
b1 said. This is the asymmetric-cost net: a missed possible cancer is the
expensive error, so a regex overrides the LLM in one direction only.

Note it fires on Reading A's fact too if the text contains a trigger phrase - but
branch 1 of `_project_patient` tests `ruled_out` first, so the promotion is inert
there. The net cannot rescue something the extractor called `ruled_out`.

**Retain this.** `ruled_out` and `absent_or_negative` both describe a negative,
and they route to opposite ends of the pipeline: one to a de-duplicated string
list outside the summary, one to a fully provenanced pinned node inside it.

### What happens to Reading B's node afterwards

Reading B pins a node at T5. Pinning is not the end of its life: T5 is the one
tier that faces a second, later decision at b4.

**The T5 rule is split across two stages.** The taxonomy's phrase - "prunable
past the recency window UNLESS flagged with a `negative_kind`" - is implemented
in two halves:

| Stage | Implements | Effect on Reading B |
|---|---|---|
| b2 `is_ledger_member` | the **exemption**. Any non-null `negative_kind` pins, at any tier. No recency window exists at b2. | pinned into `nodes` |
| b4 `ledger_consolidate._prune_t5` | the **90-day window**, against the patient's established last discharge. A serving-layer reduction. | pruned if older than 90 days |

Nothing is lost at build time, which is the safer split: the raw ledger keeps
everything, and `c2_retention_validator.py` deliberately validates recall against
`retention_ledger.raw.jsonl` rather than the served file, so the reduction does
not distort the recall metric.

**Which kinds survive b4.**  `_prune_t5` consults `T5_PRUNE_EXEMPT_KINDS`:

| `negative_kind` | Pinned at b2 | Survives b4 | Why |
|---|---|---|---|
| `surveillance` | yes | **yes** | still being watched; ageing it out drops the only standing record that something needs a repeat scan |
| `absent_or_negative` | yes | no | a dated screening negative goes stale, not wrong |
| `resolved_relevant` | yes | no | a resolved event with genuinely lifetime weight belongs in a T2 class, not at T5 |
| `null` | no | n/a | declined at b2, `prune_reason = not_pinned_tier` |

So Reading B's biopsy node (`absent_or_negative`) is pinned, then ages out of the
served summary after 90 days while remaining in the raw ledger. A surveillance
nodule in the same position would not.

**Why the exemption is narrow.** Every T5 node in the test cohort - 474 of 474 -
carries a `negative_kind`, necessarily: at T5 that branch is the only route
through `is_ledger_member`, since the `confirmed` and `suspected` paths are gated
to T1-T3 and `floor_tier` holds the safety classes at T1/T2. Exempting all
non-null kinds would therefore not narrow the prune, it would delete it.



---

## Case 3 - Recurrent COPD exacerbations

**Teaches:** that `episodes` is a second ledger with its own mechanics; that a
fact routed there can never be pinned whatever its tier; and that b4's "T4
collapse" is not what processes it.

### The record

- Admissions 2, 5, 7, 11: `COPD exacerbation` (parent condition COPD)
- Admission 1: `COPD, GOLD III` as its own fact

### b1 output (the four exacerbations)

| Field | Value |
|---|---|
| `retention_class` | `recurrent_episode` |
| `salience_tier` | T4 |
| `certainty` | `confirmed` |
| `lifecycle` | `resolved` |
| `parent_condition` | `COPD` |
| `negative_kind` | `null` |

The chronic state is a separate fact - the ACUTE-ON-CHRONIC extraction rule
requires b1 to emit both.

### Trace

**1. `normalize_fact`.** Class is `recurrent_episode`, so the reclassification
net runs: `is_disease_course_event` needs an event term plus malignancy or graft
context - "COPD exacerbation" has neither. `is_sentinel_episode` needs status
epilepticus, gangrene, or a graft/stent occlusion term. No match. Stays
`recurrent_episode`.

**2. `_project_patient` branch 2.**

```python
elif f.retention_class == "recurrent_episode":
    episodes_raw.append(f)
```

Branch 2 fires before `_is_candidate` is ever called. These facts never reach
`is_ledger_member`, never cluster, never build a node, never enter `_consolidate`,
and never produce a `dropped[]` entry. **Tier is irrelevant here** - a T1
`recurrent_episode` would take the same branch.

**3. Roll-up.** Grouped by `(parent_condition or text).strip().lower()`:

```python
EpisodeRollup(parent_condition="COPD", count=4,
              first_date=..., last_date=..., source_hadm_ids=[...])
```

Sorted by count descending. Note the group key is a bare lowercased string - no
tokenization, no abbreviation expansion, no threshold. "COPD" and "chronic
obstructive pulmonary disease" as `parent_condition` values would form two
separate roll-ups.

**4. b4.** `b4_consolidate_ledger.py` rewrites **only the confirmed `nodes`
bucket**; `flagged`, `episodes`, `amended` and `considered_excluded` pass through
untouched. The T4 branch inside `ledger_consolidate.consolidate_nodes` operates on
*nodes* whose `salience_tier` is T4 - a different population entirely. The
EpisodeRollups are not what it collapses.

**5. c1 / d_coordinator.** Both read `nodes + flagged`. `episodes` is not
included.

### What the clinician sees

In the served summary: `COPD, GOLD III` (the chronic state, from admission 1).
The four exacerbations do not appear as facts.

In the review UI: the episode roll-up, rendered from `p.episodes`.

### Flip one field

Same patient, different event. Admission 7 reads `Acute cellular rejection of
renal allograft`, and b1 tags it `recurrent_episode`, T4, `parent_condition =
renal transplant`.

`normalize_fact` runs `is_disease_course_event`: `_REJECTION_RE` matches
"rejection" in the text, `_GRAFT_CTX_RE` matches "allograft" and "renal". True.

```python
fd["retention_class"] = "sentinel_event"
fd["salience_tier"] = "T2"
```

Now branch 2 does not fire. The fact goes to `candidates`, clusters within
`sentinel_event`, becomes a node at T2, consolidates in the `disease`
compatibility group, and is served.

The correction happens **on read**, in every consumer, not at extraction. b1's
stored fact is left raw so re-derivation tracks the current taxonomy without
re-extraction.

**Retain this.** `recurrent_episode` is the only class that bypasses the
retention gate entirely. That is correct for benign exacerbations of a stable
chronic state - and catastrophic for anything that permanently changes prognosis,
which is why two deterministic nets sit in front of it.

---

## Case 4 - Three negatives that look alike

**Teaches:** that `negative_kind` is not a display flag but a rank in a merge
lattice; that the pin decision is kind-agnostic while the merge is not; and that
the lattice only ever fires on facts the identity machinery has already decided
are the same condition.

### The record

Admission 2 carries three negatives of different kinds:

- `No known drug allergies`
- `Provoked pulmonary embolism, six months anticoagulation completed`
- `Incidental 8mm indeterminate right lower lobe pulmonary nodule, repeat CT in 6 months`

Admission 12: `Recurrent pulmonary embolism, now on lifelong apixaban`

### b1 output

| | allergies | PE | nodule | recurrent PE |
|---|---|---|---|---|
| `retention_class` | `transient_event` | `sentinel_event` | `transient_event` | `sentinel_event` |
| `salience_tier` | T5 | T2 | T5 | T2 |
| `certainty` | `confirmed` | `confirmed` | `suspected` | `confirmed` |
| `lifecycle` | `active` | `resolved` | `active` | `active` |
| `negative_kind` | `absent_or_negative` | `resolved_relevant` | `surveillance` | `null` |

### Trace

**1. The pin decision does not care which kind it is.** All three negatives reach
the same branch of `is_ledger_member`, and at T5 that branch is the only way
through:

```
is_ledger_member("T5", "confirmed", "resolved", None,                 "transient_event") = False
is_ledger_member("T5", "confirmed", "resolved", "absent_or_negative", "transient_event") = True
is_ledger_member("T5", "confirmed", "resolved", "resolved_relevant",  "transient_event") = True
is_ledger_member("T5", "confirmed", "resolved", "surveillance",       "transient_event") = True
```

A T5 fact with `negative_kind = null` is declined with
`prune_reason = not_pinned_tier`. The kind decides nothing at this point; merely
having one is what pins.

**2. The deterministic net can supply the kind b1 missed.** Suppose b1 left the
nodule's `negative_kind` null. `normalize_fact` fires on read, in every consumer:

```python
normalize_fact({"text": "Incidental 8mm indeterminate right lower lobe pulmonary nodule", ...})
  -> (promoted=True, reclassified=False), negative_kind = "surveillance"
```

`_SURVEILLANCE_RE` matches this text twice over - on `indeterminate[^.]{0,25}nodule`
and on `incidental[^.]{0,60}nodule`. The net runs one way only: it can add a kind,
never remove one. Asymmetric cost - a missed possible cancer is the expensive error.

**3. The lattice.** When a cluster's members disagree, `merge_negative_kind`
resolves it by rank, highest wins:

```python
_NEGATIVE_KIND_RANK = {None: 4, "resolved_relevant": 3, "surveillance": 2, "absent_or_negative": 1}
```

```
merge_negative_kind(["resolved_relevant", None])                            -> None
merge_negative_kind(["absent_or_negative", "surveillance"])                 -> "surveillance"
merge_negative_kind(["absent_or_negative", "surveillance", "resolved_relevant"]) -> "resolved_relevant"
```

`None` ranks highest, which is the point: **`null` means present, not unknown.**
If any sibling mention says the condition is currently there, the node reads as
present and is never masked by an "absent" sibling. Among the negatives, a real
resolved event outranks a surveillance finding, which outranks a plain absence.

The same function is used again one stage later, by `ledger_consolidate._merge_node`
at b4, so a T3 roll-up resolves kinds by the identical rule.

### What the clinician sees

Three separate entries, each rendered by its kind: an explicit absence for the
allergies, a resolved event with lasting weight for the PE, a watch item for the
nodule. And, separately, the recurrent PE.

### Flip one field - the lattice never gets a chance

The natural reading of this record is that admission 12 supersedes the resolved
PE: `resolved_relevant` meets `null`, `null` wins, one node reads present. That
is not what happens.

```
A = "Provoked pulmonary embolism, six months anticoagulation completed"
B = "Recurrent pulmonary embolism, now on lifelong apixaban"
    A toks  : [anticoagulation, completed, embolism, months, provoked, pulmonary, six]
    B toks  : [apixaban, embolism, lifelong, now, pulmonary, recurrent]
    shared  : [embolism, pulmonary]   specific: [embolism, pulmonary]   overlap: 0.33
    clusters: False
```

0.33 is below `CLUSTER_THRESH`, so the two facts never meet. The patient gets
**two** PE nodes: one reading resolved with anticoagulation completed, one
reading active on lifelong apixaban. Both are true, both are served, and nothing
reconciles them.

Shorten the first fact to the bare condition and the arithmetic inverts:

```
A = "Pulmonary embolism"                    (2 tokens)
B = "Recurrent pulmonary embolism, now on lifelong apixaban"
    shared: [embolism, pulmonary]   overlap: 2/min(2,6) = 1.00   clusters: True
```

Now they cluster, `merge_negative_kind(["resolved_relevant", None])` returns
`None`, and one node reads present.

**Retain this.** The lattice is downstream of the identity machinery, and has no
opinion of its own about whether two facts are the same condition. Whether the
resolved PE is reconciled with the recurrent one is decided by token overlap on
the extracted phrasing, before `negative_kind` is ever consulted. The next two
cases are about that machinery.

---

## Case 5 - Heart failure through three merges

**Teaches:** that "consolidation" is three different mechanisms with three
different metrics at two stages, only one of which is an LLM; and that b2's
clustering is order-dependent in a way that can leave work for b4.

### The record

Four mentions across nine admissions, all `chronic_disease_state`, T3:

| Admission | Text |
|---|---|
| 1 | `Congestive heart failure` |
| 2 | `CHF` |
| 3 | `Acute on chronic systolic heart failure` |
| 4 | `HFrEF with ejection fraction 25%` |

### Merge 1 - b2 `_cluster` (facts, within one `retention_class`)

Tokenization first. `_content_tokens` expands curated abbreviations from a
472-entry table, case-sensitively, so `all` never expands to `ALL`:

```
"Congestive heart failure"                -> {congestive, failure, heart}
"CHF"                                     -> {congestive, failure, heart}      (expanded)
"Acute on chronic systolic heart failure" -> {acute, chronic, failure, heart, systolic}
"HFrEF with ejection fraction 25%"        -> {25, ejection, failure, fraction, heart, reduced}
```

The gate is a conjunction of a specificity floor and an overlap threshold:

```python
if len(_specific(toks & ct)) >= _MIN_SHARED and _overlap(toks, ct) >= CLUSTER_THRESH:
```

`_overlap` is containment, `len(a & b) / min(len(a), len(b))`, **not** Jaccard.
Walking the four facts in order, with `ct` accumulating as members join
(`cluster_tokens[i] = ct | toks`):

| Step | Incoming | `ct` before | shared | overlap | joins? |
|---|---|---|---|---|---|
| 1 | Congestive heart failure | - | - | - | seeds cluster |
| 2 | CHF | {congestive, failure, heart} | 3 | 3/min(3,3) = **1.00** | yes |
| 3 | Acute on chronic systolic HF | {congestive, failure, heart} | 2 | 2/min(5,3) = **0.67** | yes |
| 4 | HFrEF, EF 25% | {acute, chronic, congestive, failure, heart, systolic} | 2 | 2/min(6,6) = **0.33** | **no** |

Result: `[[Congestive heart failure, CHF, Acute on chronic systolic heart failure], [HFrEF with ejection fraction 25%]]`

**The accumulating token set cut both ways.** Pairwise, `HFrEF` against
`Congestive heart failure` alone scores 2/min(6,3) = 0.67 and would have joined.
Against the *grown* cluster the denominator rose to 6 and it scored 0.33. Had the
admissions arrived in a different order, the ledger would differ. This is the
order-dependence named in Case 1; here it changes the outcome.

`_split_cluster` then runs on each cluster and finds nothing to separate - every
member shares an identity token with the latest.

### Merge 2 - b2 `_consolidate_group` (nodes, within a compatibility group)

Different objects, different scope, different metric. Nodes are first bucketed by
`_group(retention_class)`, then union-found within the bucket on three branches:

```python
if (_jaccard(toks[i], toks[j]) >= JACCARD_THRESH        # true Jaccard, |a&b|/|a|b|
        or (keys[i] is not None and keys[i] == keys[j])  # curated _CONDITION_KEYS
        or _phrase_contained(nodes[i].text, nodes[j].text)):
```

Against our two surviving nodes:

```
jaccard("Acute on chronic systolic heart failure", "HFrEF with ejection fraction 25%") = 0.22
_condition_key(...)   = None for both (the curated list is HCC and cirrhosis only)
_phrase_contained(...) = False
-> no merge
```

`JACCARD_THRESH` is also 0.6, but it is a stricter metric than `_overlap` at the
same number: the denominator is the union, so a short phrase inside a long one
scores low rather than 1.00. Two nodes leave b2.

### Merge 3 - b4 `_roll_up` at T3 (LLM groups, script merges)

Only now does a model get involved, and only to group and name:

> These are chronic conditions. Group entries that refer to the SAME underlying
> condition. For each group write canonical_text = the most complete, clinically
> useful single phrasing drawn from its members.

The merge itself stays deterministic in `_merge_node`: representative is the
most-corroborated member (`n_mentions`, then longest text); `source_hadm_ids` and
`fact_ids` are unioned; `n_mentions` summed; `onset_date` is the min, and
`last_confirmed_date` the max; certainty is the strongest; tier the strongest;
`negative_kind` via `merge_negative_kind` from Case 4.

Two guardrails sit around it. A group whose members disagree on
`current_lifecycle` is never merged - the members are emitted unchanged. And any
node the LLM failed to place is appended verbatim, so a parse failure or a
dropped index degrades to no-change rather than to loss.

### What the clinician sees

One line, canonically phrased, carrying provenance from all four admissions -
but only because the b4 model recognised what the b2 arithmetic could not.

**Retain this.** Three mechanisms, and the names are not interchangeable:
`_cluster` groups **facts** by containment within a class; `_consolidate_group`
groups **nodes** by Jaccard, curated key or phrase containment within a
compatibility group; `_roll_up` groups nodes by **LLM judgment** within a tier.
Only the third can see that `HFrEF, EF 25%` and `congestive heart failure` are
the same condition, because only the third is reading the words rather than
counting them.

---

## Case 6 - Two conditions that must stay apart

**Teaches:** what the identity guards are for, since a guard that is working
leaves no trace; and where the coverage currently stops.

### Guard 1 - `_specific`: qualifier tokens cannot carry a merge

The clinical qualifiers (`chronic`, `stable`, `acute`, `severe`, `disease`,
`history`, `mild`, ... 38 in all) are stripped before the specificity floor is
tested. Verified cases where this is the **binding** gate - overlap passes, the
guard refuses:

```
A = "Chronic obstructive pulmonary disease"   toks {chronic, disease, obstructive, pulmonary}
B = "Chronic disease"                          toks {chronic, disease}
    shared = {chronic, disease}   overlap = 2/min(4,2) = 1.00   PASSES
    _specific(shared) = {}                                      REFUSES

A = "Essential hypertension, chronic and stable"
B = "Chronic stable disease"
    shared = {chronic, stable}    overlap = 0.67                PASSES
    _specific(shared) = {}                                      REFUSES
```

Without it, two distinct conditions each phrased "chronic and stable ..." share
only boilerplate and chain together. The comment in `b2_build_ledger.py` records
what that did before the guard: COPD and CAD chained into a hypertension node,
and alcohol use into a seizure node.

### Guard 2 - `_normalize_surface`: the bare `s` token

Possessives and `s/p` are rewritten before either tokenizer runs:

```
"Crohn's disease"       raw tokens {crohn, disease, s}       -> normalized "Crohn disease",  identity {crohn}
"Cirrhosis s/p TIPS"    raw tokens {cirrhosis, p, s, tips}   -> normalized "Cirrhosis TIPS", identity {cirrhosis, transjugular, intrahepatic, portosystemic, shunt}
```

A bare `s` is not in `_QUALIFIERS`, so it survives `_specific` and satisfies
`_MIN_SHARED`. Any eponymous disease and any post-procedure phrase would have
shared it. 

### Guard 3 - `_split_cluster`: the compound fact

A fact naming two conditions bridges them, because `_cluster` accumulates tokens
as members join:

```
"Coronary artery disease"                                identity {artery, coronary}
"Coronary artery disease and peripheral vascular disease" identity {artery, coronary, peripheral, vascular}
"Peripheral vascular disease"                            identity {peripheral, vascular}

_cluster -> [[CAD, CAD-and-PVD, PVD]]        all three, one cluster
```

`_build_node` takes text, class, certainty and lifecycle from the **latest**
member, so CAD would lose its identity while its `fact_id` survived inside the
node - not a prune, so no `dropped[]` entry and no `prune_reason`. The guard
re-partitions around the member that would supply the displayed text:

```
_split_cluster -> [[CAD-and-PVD, PVD], [CAD]]
```

Two nodes. It can only ever increase the node count, so it cannot create a merge
`_cluster` did not already make.

### Guard 4 - `_MERGE_GROUP`: a procedure cannot absorb a disease

Consolidation is bucketed first. `chronic_disease_state`, `permanent_condition`
and `sentinel_event` share the group `disease`; `anatomy_altering_surgery` and
`implanted_device` share `procedure`; every other class merges only with itself.

```
"Hepatocellular carcinoma"                                 chronic_disease_state    -> "disease"
"Orthotopic liver transplant for hepatocellular carcinoma" anatomy_altering_surgery -> "procedure"
_condition_key both = "hepatocellular_carcinoma"           (curated key MATCHES)
_consolidate -> 2 nodes
```

The curated key matches and the merge is still refused, because the two nodes are
never in the same bucket. Re-tag the transplant as `chronic_disease_state` - an
axis error, the text unchanged - and the guard is bypassed:

```
_consolidate -> 1 node
```

Which is the rejected cross-class predecessor's behaviour: an active malignancy
collapsed into a transplant-history line.

### Where the coverage stops

`_split_group` is the same identity guard one stage later, applied to
union-found node groups. It re-groups members that share **no** identity token
with the representative. That condition is why it does not catch the case its own
docstring names:

```
"Cirrhosis"                                             key=cirrhosis  identity {cirrhosis}
"Decompensated cirrhosis with ascites and varices"      key=cirrhosis  identity {ascites, cirrhosis, varices}
"Chronic thrombocytopenia likely secondary to cirrhosis" key=cirrhosis  identity {cirrhosis, thrombocytopenia}

_rep_of      -> "Chronic thrombocytopenia likely secondary to cirrhosis"
_split_group -> [[all three]]                 no split: every member shares `cirrhosis`
_consolidate -> 1 node: "Chronic thrombocytopenia likely secondary to cirrhosis"
```

All three share `cirrhosis`, so the guard sees a coherent group and leaves it
alone. The merged node's text mentions cirrhosis only as an attribution for the
thrombocytopenia, and the decompensation - ascites, varices - is gone from the
displayed text, surviving only as `fact_ids` inside the node.

The guard catches a member orphaned by the representative. It cannot catch a
member **swallowed** by a representative that names it in passing, which is the
harder half of FM-7. Registered here; not yet fixed.

**Retain this.** Three of the four guards were verified refusing merges that the
raw arithmetic would have made, and they leave no trace when they work - the
clinician simply sees two correct lines instead of one wrong one. The fourth
shows the shape of what is still open: a condition can be absorbed by a sentence
that mentions it.

---

## Summary table

| | Case 1 | Case 2 | Case 3 |
|---|---|---|---|
| Field that decides | `retention_class` | `certainty` vs `negative_kind` | `retention_class` + regex nets |
| Correct destination | `nodes` (served) | `considered_excluded` | `episodes` |
| Failure destination | `amended` (withheld) | pinned node instead of `considered_excluded` | pinned node it should not be |
| Detectable? | no - C2 counts `amended` as represented | partially | yes - text is visibly a fact |
| Registered as | FM-8 | - | Finding 1 nets |

| | Case 4 | Case 5 | Case 6 |
|---|---|---|---|
| Field that decides | `negative_kind` | token overlap, then LLM judgment | `retention_class` + identity tokens |
| Mechanism | `merge_negative_kind` rank lattice | `_cluster` / `_consolidate_group` / `_roll_up` | `_specific`, `_normalize_surface`, `_split_cluster`, `_MERGE_GROUP` |
| What goes wrong | two nodes for one condition, one resolved and one active | a merge b2 cannot make, left for b4 | a condition swallowed by a node that names it in passing |
| Detectable? | no - both nodes are individually correct | no - b2 output is valid either way | no - no prune, no `dropped[]`, no reason |
| Registered as | - | - | FM-7 (residual, open) |

---

## Known gaps in these traces

- The FM-7 residual shown at the end of Case 6 - `_split_group` cannot separate a
  member swallowed by a representative whose text mentions it - is open. A fix
  would need a rule about whether a mention counts as representation, which is a
  clinical question, not an arithmetic one.
- The single-episode case (`count == 1` routing to `episodes` rather than
  `dropped[]`) is a deferred question and is not illustrated here.

