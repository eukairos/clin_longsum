# Consolidation grouping worksheets (b2 and b4)

Two read-only scripts that build clinician adjudication worksheets for the third
consolidation quality axis: **did a merge put two distinct conditions on one line?**

## Why

Consolidation has three axes. Two are already measured; the third is not.

| Axis | Question | Covered by |
|---|---|---|
| recall | was a salient fact lost? | `f1_salience_recall.py` (SWR) |
| precision | did the merged line invent anything? | `f9_merge_faithfulness.py` |
| **grouping** | **were two distinct conditions merged?** | **addressed by these sheets** |

`EVALUATION.md` section 11 lists the gap. No automatic signal separates a good
merge from a bad one, so it needs per-merge clinician verdicts.

## The three merge surfaces

Merging happens at three points, with different mechanisms and different failure
modes. **Estimate a rate for each separately.**

| Surface | Script | Mechanism | Can merge across class? |
|---|---|---|---|
| `b2_cluster` | `b2_merge_worksheet.py` | token overlap within one `retention_class`, then identity split; latest member supplies the text | no, structurally |
| `b2_consolidate` | `b2_merge_worksheet.py` | Jaccard / curated condition key / phrase containment within a `_MERGE_GROUP` bucket | **yes, by design** |
| `b4` | `b4_merge_worksheet.py` | LLM T3 state roll-up | no |

`b2` merges are recomputed by calling b2's own functions on
`retention_facts.jsonl`, because the stored ledger cannot tell clustering from
consolidation: a node with `n_mentions=5` may be five facts in one cluster or
2+3 from consolidation. `b4` merges are recovered from the artifacts by the same
`fact_ids` subset rule `f9` uses.

## Running

Both honour `LONGSUM_DATA_ROOT` and `LONGSUM_NAMESPACE` like every other stage.

```bat
cd clin_longsum
python src\clinical_review_tools\b2_b4_consolidation\b4_merge_worksheet.py
python src\clinical_review_tools\b2_b4_consolidation\b2_merge_worksheet.py

:: one patient, or one b2 stage
python src\clinical_review_tools\b2_b4_consolidation\b4_merge_worksheet.py --subject 90000035
python src\clinical_review_tools\b2_b4_consolidation\b2_merge_worksheet.py --stage consolidate
```

Deterministic, no LLM. `b4_merge_worksheet.py` needs
`retention_ledger.raw.jsonl`, so b4 must have run with `CONSOLIDATE_LEDGER` on.

Output lands in `<data>/output/review_tools/merge_grouping/` - the data output
folder for `src/clinical_review_tools` scripts, outside the repo by design:

```
merge_grouping_adjudication.csv / .json   b4
b2_merge_adjudication.csv / _summary.json b2, both stages, labelled by `stage`
```

Long format, one row per member with the merge-level columns repeated, sorted
suspicious-first. Two blank columns per row for the reviewer:

- `verdict_same_condition` -- `Y` one condition, fairly represented; `N` distinct
  conditions merged (a grouping defect); `P` same condition but a clinically
  needed qualifier was lost; `?` undecidable from the text
- `reviewer_note`

## Reading the flags

Flags are review hints, never defect calls.

| Flag | Meaning |
|---|---|
| `multi_class` | members disagree on `retention_class`. Only ever fires on `b2_consolidate`, where it is by design |
| `multi_tier` | members disagree on tier, so the merge changed the node's tier |
| `multi_lifecycle` | members disagree on lifecycle. Expected at b2 (no guardrail); at **b4 it should be impossible**, so investigate |
| `multi_negative_kind` | `merge_negative_kind` ranks `None` (present) highest, so a `surveillance` or `resolved` qualifier can yield to a sibling asserting presence. Check the direction |
| `identical_text` | pure duplicate collapse, safe to skip when sampling |
| `large_merge` | 5 or more members |

## Two things the summaries check for you

**b4 - member recovery.** Asserts every consolidated node's `fact_ids` are fully
covered by recovered members. A non-zero `recovery_incomplete` means the sheet is
missing merges; fix that before adjudicating.

**b2 - the FM-8 residual.** The FM-8 fix (a retraction wins outright; see `KNOWN_FAILURE_MODES.md`) lives in
`_build_node` and has no counterpart in `_merge_group`. A safety-robust superseded
node absorbed into an active sibling would silently lose its de-label.
`fm8_residual_check.superseded_absorbed_into_active` counts it directly. It was 0
on the dev cohort, where only one such node reaches consolidation at all - re-check
on any cohort with more de-labelling.

## Caveat that matters for governance

`b2_merge_worksheet.py` recomputes with **current** code and taxonomy, which may be
newer than the taxonomy that built the ledger on disk. That is normally what you
want - you are judging what b2 does now - but a verdict here does not necessarily
describe the **served** ledger. The summary records the recompute version; compare
it against `retention_ledger.jsonl`'s `taxonomy_version`, and re-run b2 if they need
to agree.

## Output contains patient text

Both CSVs carry verbatim clinical fact text and `subject_id`. They are written under
the data tree, outside the repo, and should never be committed. Adjudicated copies
belong with the other gitignored study material.
