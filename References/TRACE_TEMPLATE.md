# TRACE_TEMPLATE.md

One form per case. Fill all sections - an empty section is indistinguishable from a skipped step.

Rules that the form enforces:
- **Section 3 is the only source of truth.** No verdict of fabrication or loss without a grep
  of `bhc_text`.
- **Section 4 is read LIVE.** Paste the excerpt you actually read this session, with the file
  and a line anchor. 
- **Section 5 is executed, not predicted.** Run the function on the fact's exact axes and paste
  the output.
- **Section 6 cannot be skipped.** Several apparent defects were correctly downgraded to
  "by design" only after following the patient's later course.
- **Section 7 picks exactly ONE class.** A taxonomy call is not an engineering call. If the case
  really has two defects, write two traces.

---

## The form

```markdown
# TRACE <id> -- <one-line statement of the concern>

Date: <YYYY-MM-DD>
Cohort / data root: <dev | gen | test>  ->  <path>   |   DB: <database>
Asked by: <the question as posed>

## 1. Fact identity

| | |
|---|---|
| subject_id | |
| hadm_id(s) | |
| admit_seq | |
| fact_id | |
| fact text (verbatim) | |
| axes as extracted | tier / class / certainty / lifecycle / occurrence / negative_kind |

Artifact generation dates (from `audit.created_at` on the relevant records), so the trace is
read against the code that produced them, NOT today's code:
| Stage | Generated at | Prompt / taxonomy version |
|---|---|---|
| b1 | | |
| c1 | | |

## 2. Per-stage presence

Tick each stage the fact is present at. Present-vs-absent localises the failing stage: the
defect is at the FIRST stage where an expected presence becomes an absence.

| Stage / artifact | Present? | Where / under what shape | Notes |
|---|---|---|---|
| `cohort_admissions.csv` `bhc_text` | | | ground truth |
| `retention_facts.jsonl` | | `facts[]` entry, fact_id | |
| `retention_ledger.raw.jsonl` (b2) | | nodes / flagged / episodes / amended / considered_excluded / dropped | |
| `retention_ledger.jsonl` (b4 served) | | | b4 T3/T4 grouping, T5 prune |
| `compaction_results.jsonl` | | `prior_generated_md` / `ledger_facts[]` | |
| `compaction_faithfulness.jsonl` | | `failed_statements` / `entailment_detail` | |
| `compaction_corrected.jsonl` | | | only if c4 ran |
| `episode_paragraphs.jsonl` | | | |
| `longitudinal_view.jsonl` | | | |
| `tiered_ledger` / `tiered_view` (DB) | | | apply the COALESCE |

Commands used:
```
<the actual greps / pg_query calls, so the trace is reproducible>
```

## 3. Ground truth

Grep of the raw note. Quote verbatim, with the `hadm_id` it came from.

> hadm <id>: "<exact text from bhc_text>"

If ABSENT: state which admissions were searched and confirm the search covered all of them.

Verdict on this section (one of):
- [ ] SUPPORTED -- the note says this
- [ ] FABRICATED -- no support in any `bhc_text` for this patient
- [ ] DISTORTED -- the note says something related but materially different
- [ ] LOST -- the note says this and no downstream artifact carries it

## 4. Failing stage -- code read live

File: `src/<file>.py`, function `<name>`, read <date>.

```python
<paste the excerpt that makes the decision>
```

What this code does with THIS fact, in one or two sentences:

## 5. Executed functions

Run the deterministic functions on the fact's exact axes. Paste real output. Normalise first --
`tax.normalize_fact()` mutates axes on load, so the axes in the file may not be the axes the
routing saw.

```python
import retention_taxonomy as tax
fd = {...}                      # the fact dict, verbatim from retention_facts.jsonl
print(tax.normalize_fact(fd))   # (promoted, reclassified) -- did the axes change on load?
print(fd)                       # axes AFTER normalisation: these are what routing saw
print(tax.is_ledger_member(fd["salience_tier"], fd["certainty"], fd["lifecycle"],
                           fd["negative_kind"], fd["retention_class"]))
print(tax.prune_reason(fd["salience_tier"], fd["certainty"], fd["lifecycle"],
                       fd["negative_kind"], fd["retention_class"]))
print(tax.is_surveillance_finding(fd["text"]),
      tax.is_disease_course_event(fd["text"], fd.get("parent_condition")),
      tax.is_sentinel_episode(fd["text"]))
```

Output:
```
<paste>
```

For a node-shaped question also run `b2._cluster` / `b2._content_tokens` /
`tax.merge_negative_kind` on the actual cluster, and read the node's `provenance` map to see
which fact supplied which attribute.

## 6. Course follow -- did the loss bite?

Walk the patient's LATER admissions. A fact that dropped at admission 3 and was re-extracted at
admission 6 was not lost from the record.

| Later admission (seq / hadm) | Condition mentioned? | Present in the served record? |
|---|---|---|
| | | |

Verdict (one of):
- [ ] BIT -- the fact is absent from the current served record and a clinician would need it
- [ ] DID NOT BITE -- re-established later, or represented elsewhere (name where)
- [ ] BY DESIGN -- the prune/rollup is correct for this fact; nothing to fix

**If DID NOT BITE or BY DESIGN, stop here.** Record the finding and close the trace. Do not
proceed to a defect classification.

## 7. Defect classification

Exactly one:

- [ ] **clinical / taxonomy** -- a label or governance call (wrong `retention_class`, wrong tier,
      whether a status should pin). -> Stay's call. Requires a version bump + CHANGELOG +
      sign-off.
- [ ] **engineering / routing** -- the code does not do what it intends (silent fall-through,
      wrong gate, split attribution). -> the build chat's call.
- [ ] **prompt wording** -- the instruction produced the behaviour (inference from a redaction,
      hedging under a do-not-contradict rule). -> propose a reword + prompt version bump.

Justification in one sentence:

Register entry: <existing FM-n, or NEW>

## 8. Proposed action

Read-only diagnosis. Propose; Clinician/developer rules.

- Change: <file, function, what>
- Downstream impact: <stages that read the changed artifact/field>
- Re-run cost: <which stages; flag if b1 re-extraction is needed>
- Risk if not done:

## 9. Not recoverable

What this trace COULD NOT establish, and why. (Reasoning traces are not persisted, so
"did the model infer X or merely hedge" is never answerable retrospectively. Say so rather
than guessing.)
```

---

## Worked fragment

An abbreviated example of sections 2, 5 and 6 done properly -- the three that get skimped.

**Section 2**, on a fact present in `retention_facts.jsonl` but in no ledger array:

| Stage | Present? | Where | Notes |
|---|---|---|---|
| `bhc_text` hadm 24··· | yes | "...hypercalcemia of malignancy, resolved..." | |
| `retention_facts.jsonl` | yes | fact_id `a3f1...`, class `recurrent_episode`, parent `Lymphoma` | |
| `retention_ledger.raw.jsonl` | **no** | not in nodes/flagged/amended | |
| ...`episodes[]` | yes-ish | `{parent_condition: "Lymphoma", count: 1}` | own text GONE |

First absence is at b2 -> the failing stage is b2, not b1. The fact is not missing, it is
rolled up.

**Section 5**, showing why the executed call matters:

```
tax.normalize_fact(fd) -> (False, True)     # RECLASSIFIED on load
fd["retention_class"]  -> 'sentinel_event'  # NOT 'recurrent_episode' as stored
```
The stored class is not the routed class. A trace that read the file and stopped would have
chased the wrong branch entirely.

**Section 6**, the step that most often reverses the verdict:

| Later admission | Mentioned? | In served record? |
|---|---|---|
| seq 5 (hadm 25···) | yes, "hypercalcemia recurred" | yes -- `nodes[]`, T3 chronic |

-> DID NOT BITE. The condition re-established as its own node two admissions later. The rollup
at seq 3 lost the wording, not the fact. Trace closes without a defect classification.

---

## Filing

One markdown file per trace. Keep them alongside the register; when a trace establishes a new
mechanism, add a `KNOWN_FAILURE_MODES.md` entry and cross-reference the trace id both ways. A
trace that closed as BY DESIGN is still worth keeping -- it stops the same case being
re-investigated.
