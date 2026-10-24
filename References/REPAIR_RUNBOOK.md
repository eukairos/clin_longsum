# REPAIR_RUNBOOK.md — diagnosing & repairing a faulty clin_longsum dataset

**Audience:** DevOps / engineering supporting the physician review service.
**When to use:** a physician (or an eval gate) flags an anomaly in a served longitudinal
record — wrong/missing/duplicated ledger node, wrong or hallucinated prose, a patient
missing from the review queue, stale content after a change. This guide localizes the
fault, repairs it, propagates the fix through the coupled cascade, reconciles the state
the cascade does **not** rebuild, re-persists to Postgres, and verifies.

**Read alongside:** `PIPELINE.md` (authoritative stage I/O), `CLINICAL_TUNING_POINTS.md` (governance),
`EVALUATION.md` (gates).

---

## 0. Guardrails (non-negotiable)

- **Back up before destructive ops.** Copy `data/interim/` + `data/output/` and dump the
  affected Postgres tables before any `--fresh`, `DELETE`, or `TRUNCATE`. Wrap multi-row
  SQL deletes in a transaction and eyeball the row count before `COMMIT`.
- **Clinical / taxonomy changes need sign-off**, a version bump, and a CHANGELOG entry
  (`retention_taxonomy.py` / `clinical_salience_taxonomy.md`). A version bump invalidates
  the whole cohort → full rebuild. Do not silently re-tier or re-merge.
- **Database selection:** `PGDATABASE` picks the DB (a shell value beats `.env`), and `database/pg.py`
  REFUSES any name in `LONGSUM_PROTECTED_DBS` (default `longitudinal_bhc`) unless
  `LONGSUM_ALLOW_PROTECTED_DB=1`. Every caller routes through it — stages, the FastAPI app, the
  fold-in lock — so each prints `[pg] database: <db>  namespace: <ns>` on first connect. CHECK THAT
  LINE. `LONGSUM_NAMESPACE` isolates FILES ONLY; pair it with a dedicated `PGDATABASE`
  (generalisation → `longitudinal_bhc_gen`, regression test → `longitudinal_bhc_test`) or namespaced
  and dev rows mix in the same tables. `LONGSUM_DATA_ROOT` moves the file tree out of the repo.
  cmd trap: `set VAR = value` with spaces creates a variable named `VAR ` — the real one stays unset.
- **Promotions live in two places** (table = truth, CSV = cache the pipeline reads). Touch
  one, reconcile the other (§5C). This is the single most common repair mistake.

---

## 1. Mental model — why repairs are subtle

The pipeline is a chain of **file artifacts** feeding **Postgres tier tables** that the UI
reads. Two properties make repair non-obvious.

**(a) Coupled, forward-only artifacts.** Fixing an upstream artifact means re-running
everything downstream of it, or the DB keeps serving stale output:

```
b1 facts → b2 raw ledger → b4 consolidated ledger ┐
                                                   ├→ c1 compaction → c3 faithfulness → c4 corrected prose ┐
                          d1 episode paragraphs ───┤                                                       ├→ d_coordinator view → d3 tier tables → UI
                                                   └→ d2 TL;DR ──────────────────────────────────────────┘
e1 incoming summary ─────────────────────────────────────────────────────────────────────→ d3 (incoming_* columns)
```

**(b) Two "sticky" state layers the cascade does NOT rebuild.** These survive a reload and
routinely hide an otherwise-correct fix:

1. **Holdout / promotion state.** `promotions` table = source of truth; `promoted_admissions.csv`
   = the cache the file-based stages actually read (`holdout.holdout_hadms/established/split`).
   Governs which admission is **incoming** (appears in the review queue) vs **established**
   (folded into the record). `still_held = is_holdout AND NOT promoted`.
2. **Physician review state** (`tiered_view.review_status / reviewed_* / approved_summary_md /
   folded_in_at`). `d3` **deliberately preserves** these (its `tiered_view` UPSERT omits them
   from the update set) so a reload never clobbers a decision. A stale `approved` keeps a
   patient **off** the pending queue even after you fix their data.

Also: `d3` is **per-subject** UPSERT / DELETE-then-INSERT — no global truncate. If the cohort
shrinks, old subjects linger until `d3_load_tiers.py --prune`.

**Fix → what to re-run** (downstream closure; QA stages in parentheses):

| Repaired at | Re-run, in order |
|---|---|
| `b1` (facts) | `db_persist_source --facts` → `b2` → `b4` → `d1` → `c1` → `c3` → `c4` → `db_persist_compaction` → `d2` → (`e1` if the held-out set changed) → `d_coordinator` → `d3` → `db_persist_source --audit` |
| `b2`/`b4` (ledger/consolidation) | `c1` → `c3` → `c4` → `db_persist_compaction` → `d2` → `d_coordinator` → `d3` |
| `c1` (compaction) | `c3` → `c4` → `db_persist_compaction` → `d2` → `d_coordinator` → `d3` |
| `c3`→`c4` (prose faithfulness) | `db_persist_compaction` → `d_coordinator` → `d3` |
| `d1` (paragraph) | `d2` → `d_coordinator` → `d3` |
| `d2` (TL;DR) | `d_coordinator` → `d3` |
| `e1` (incoming) | `d3` |
| `d_coordinator` (view) | `d3` |
| holdout / promotion flip | `b1` (re-extract affected subjects) → `b2` → `b4` → `d2` → `d_coordinator` → `d3`, **plus** §5C reconciliation |

`c3`/`c4` are **served** (d_coordinator takes corrected Prior Admissions prose from c4), so
they are not optional on any path that touches compaction. `b3`, `c2`, `c5`, `f*` are QA/eval
(not served) — run them to verify, not to fix.

---

## 2. Reference incident (worked example — the shape to generalize)

**Symptom:** three chronic conditions present in the source BHC — CKD, COPD+CAD, and
alcohol-use disorder — were absent from the ledger and invisible in the UI.
**Root cause:** consolidation **over-merge** (aggressive cross-class containment merge in an
earlier `b2`/`b4` variant) buried active HCC nodes inside unrelated nodes.
**The trap:** the rebuild cascade fixed the ledger, but two sticky-state layers made the fix
**invisible until surgically repaired**:
1. Stale `promotions` rows (from earlier fold-in testing) marked 5 subjects' last admissions
   as promoted → those subjects were treated as fully-established → they had **no incoming
   admission** → they never entered the review queue.
2. `d3` preserved those 5 subjects' prior `approved` review state, which would have kept them
   off the pending queue even once incoming was restored.

**Resolution (the generic procedure below, applied surgically):** clear the stale promotions
(table + CSV cache), re-extract + rebuild the 5 affected subjects so their last admission
flipped established→incoming, reset their `review_status` to `pending_review`, re-persist,
verify the 4 recovered nodes render. Lesson: **a data fix is not done until (a) it has
propagated through the whole downstream chain, and (b) the promotion and review state have
been reconciled to match the intended served view.**

---

## 3. Anomaly taxonomy — map the symptom to the owning stage

| # | What the physician sees | Owning stage / artifact | Distinguish by |
|---|---|---|---|
| A | Condition IS in the source BHC but missing from the ledger | consolidation **over-merge** (`b2._consolidate` / `b4`) **or** extraction miss (`b1`) **or** a deliberate prune | **Check `dropped[]` FIRST** (`retention_ledger.jsonl`, or `SELECT reason FROM retention_dropped WHERE subject_id=<SID>`): a prune records WHY the taxonomy declined the fact, so "pruned by design" is distinguishable from "lost" without a trace. If not there: in `retention_ledger.raw.jsonl` (b2 pre-consolidation) but NOT in the consolidated `retention_ledger.jsonl` → over-merge (b4/b2 consolidate). Absent from `.raw` too → **check `fact_ids` before concluding extraction miss**: an absorbed condition is present in `.raw` inside another node's `fact_ids` while its own text is nowhere (Failure mode 7: see 'KNOWN_FAILURE_MODES.md'). Grep the fact_id across every node's `fact_ids` array, not the text. Only if the fact_id appears in no node is it an extraction miss (b1). `c2`/`f1` quantify. |
| B | Same condition split into duplicate / fragmented nodes | consolidation **under-merge** (`b2`/`b4`) | Two+ nodes for one condition in the consolidated ledger. |
| B2 | Node's tier looks inconsistent with its wording | **split provenance** in `b2._build_node` (by design) | A node takes `salience_tier` from the STRONGEST mention in its cluster but `text`/`class`/`certainty`/`lifecycle` from the LATEST. Read the node's `provenance` map (attribute → fact_id) instead of tabulating `fact_ids` by hand. Rare — 0.7% of nodes on the dev cohort. Not a defect on its own; the tier rule is a deliberate safety net. |
| C | Node present but wrong tier / lifecycle / negation sense | taxonomy + extraction (`retention_taxonomy.py`, `b1`) | **Governance** — needs sign-off + version bump. |
| D | Prior Admissions prose asserts something unsupported / hallucinated | compaction faithfulness (`c1`→`c3`/`c4`) | Compare `compaction_results` vs source. requires c1→c4 re-run if indeed hallucination. |
| E | TL;DR wrong / missing a key thread | `d2` (reads paragraphs + compaction + ledger) | The fact is correct in the ledger/paragraphs but absent/wrong in `tldr_md`. |
| F | Current-episode or a hot-window paragraph is wrong | `d1` | The raw BHC is fine but the paragraph misreads it. |
| G | Incoming (held-out) summary is wrong | `e1` | `incoming_summary_md` wrong while the incoming `bhc_text` is fine. |
| H | Patient entirely absent from the review queue | promotion/holdout state **or** `d3` load **or** stale `review_status` | See §4 decision tree. |
| I | Whole cohort stale after a taxonomy / prompt / model change | version drift (`config.py` model/prompt versions, taxonomy) | Any bump invalidates the cohort → **full rebuild** (§5A). |
| J | A folded-in patient shows a stale QA artifact | fold-in coverage  | Fold-in re-runs the **full per-subject chain** — b1/b2/b4/c1/**c3/c4**/**d1**/**c2**/d2/d_coordinator/d3/**e2** — and persists facts + **compaction_review** + **retention_review** + audit + a **c5** notify, so served prose, faithfulness, retention flags, and the incoming RAG embedding are all fresh. Only **b3** and the **f\*** evals stay outside the fold-in → run them on the next full pass.  |
| K | RETENTION tab says "no flags outstanding" but c2 found losses | `db_persist_retention` never ran / wrote nothing / ran on stale c2 output | RETENTION is the ONLY surface for c2 losses. `pg.retention_visibility_gaps()` detects it: the stage aborts on write, and the tab shows a warning row instead of the empty state. |
| L | An admission vanishes from the served record after a fold-in | `d1` never produced a paragraph for the newly-demoted admission | The AGING failure mode, unique to fold-in: promoting admission *n* demotes *n-1* into the hot window, where it now needs a d1 paragraph it never had. Batch `--fresh` builds cannot produce it. `f2.gate_hotwindow` reports it as `hotwindow_missing` – so a run could go green while an admission had silently disappeared. Recovery in §5F. |

---

## 4. Diagnosis workflow — localize before you touch anything

1. **Reproduce** in the UI (`/tiered/review/patient/{subject_id}`) and note the exact tier
   (TL;DR / current episode / hot window / Prior Admissions / ledger / incoming).
2. **Pull the on-disk view** for that subject: `data/output/views/{subject_id}.md`
   (`d_coordinator.py --write-md`, or `--render {sid}` to print one). This is the assembled
   view before Postgres — if the fault is here, it's upstream of `d3`; if the md is correct
   but the UI is wrong, it's `d3`/DB/`review_status`.
3. **Walk the chain to the FIRST artifact where the info is wrong** (that stage is your repair
   entry point in §3's table):
   - source truth: `admissions.bhc_text` / `cohort_admissions.csv` (`bhc_text`, `bhc_delta`)
   - facts: `retention_facts.jsonl` (one line per admission)
   - ledger: `retention_ledger.raw.jsonl` (b2, lossless) vs `retention_ledger.jsonl` (b4, served).
     Both carry `dropped[]` (why a fact was pruned) and per-node `provenance` (which fact supplied
     which attribute).
   - prose: `compaction_results.jsonl` → `compaction_corrected.jsonl`; `episode_paragraphs.jsonl`; `tldr_results.jsonl`
   - incoming: `incoming_summary.jsonl`
   - DB: `tiered_ledger` / `tiered_view` / `tiered_episode` / `retention_dropped`
4. **For "missing from queue" (class H), run this tree:**
   - `SELECT subject_id, incoming_hadm_id, review_status, folded_in_at FROM tiered_view WHERE subject_id = <SID>;`
     - No row → the view was never loaded → re-run `d_coordinator --subject <SID>` then `d3 --subject <SID>`.
     - `incoming_hadm_id IS NULL` → the subject is treated as established → **promotion/holdout** (next bullet).
     - `review_status <> 'pending_review'` (e.g. `approved`) → stale review state → §5C reset.
   - Promotion check: `SELECT * FROM promotions WHERE subject_id = <SID>;` (note: column order is
     **`hadm_id, subject_id, …`** — `SELECT *` shows hadm first; do not transpose them). If a row
     exists that should not, it's a stale promotion → §5C clear + rebuild.
   - Cache coherence: compare the `promotions` table against `data/interim/promoted_admissions.csv`.
     Mismatch means the file-based stages ran on a stale cache.

### 4.1 Diagnostic toolkit (`src/clinical_review_tools/`)

Read-only helpers — no GPU/DB writes; each writes only its own report CSV/HTML. They cluster on
the B-stage (facts → ledger → consolidation) and c3 faithfulness, where most "missing / wrong
ledger content" faults live. Run them by their real path under `src/clinical_review_tools/<subdir>/`;
re-run after any rebuild or fold-in, since they read whatever artifacts are on disk now.

| Tool (subdir) | Diagnoses | Stage → class | Scope | Needs on disk | Writes |
|---|---|---|---|---|---|
| `inspect_ledger.py` (b3_loss_audit_tool) | Is condition X in the served/raw ledger? Was it ever extracted? → extracted-but-dropped vs never-extracted | first-line b1 vs b2 vs b4 split → **A** | one subject + grep terms | facts, ledger(.raw) | stdout |
| `dump_clusters.py` (b1_b4) | b2 clustering for one subject: members, chosen node text, `RELABEL-COLLISION`; previews alt cluster metrics without touching b2 | **b2._cluster** over/under-merge → **A / B** | one subject | facts | stdout |
| `trace_retention_gap.py` (b1_b4) | Attributes each verified-missing fact to a precise stage: `b1_extraction / taxonomy_not_member / b2_construction / b2_rollup / b4_consolidation / b4_rollup / present_in_served` | full-chain attributor → **A / B / C** + metric FP | cohort (verified miss-list) | `missing_ledger_facts_verified.csv`, **raw + served ledger**, cohort | `retention_gap_trace.csv` |
| `count_same_condition_dupes.py` (b1_b4) | Cohort count of same-condition chronic-node **under-merge**; curated-key candidates | b2/b4 under-merge → **B** | cohort | facts | `same_condition_dupes.csv` |
| `dryrun_cluster_metric.py` (b1_b4) | Cohort **blast-radius** of a proposed b2 `_cluster` metric change (recovery vs regression) | b2._cluster **fix validation** → **A** (feeds §5B/§6) | cohort | facts | stdout |
| `build_review_ui.py` (b3_loss_audit_tool) | Bakes a self-contained HTML triage UI: each b3 loss item vs the subject's full ledger; clinician marks covered / needs-BHC | **b3 loss-audit** clinician triage → **A / B** | subjects in `loss_audit.md` | `loss_audit.md`, ledger(.raw) | `loss_review.html` |
| `c3_nothink_review.py` (c3_nothink_cases_for_review) | C3 faithfulness passes rubber-stamped by a degraded no-think judge, for review vs source BHC | c3 prose faithfulness / judge health → **D** | passes (opt. per subject) | `compaction_faithfulness.jsonl`, cohort | optional jsonl |

**Chaining for a class-A "missing from UI" fault:** `inspect_ledger.py <sid> --grep <term> --facts`
(in facts + raw but not served → b4 over-merge; in facts but not raw → b2; not in facts → b1) →
`dump_clusters.py --subject <sid>` to see the exact collision and preview `overlap_gen1` →
`trace_retention_gap.py` for the whole verified miss-list → before applying a b2 fix,
`dryrun_cluster_metric.py` + `count_same_condition_dupes.py` for blast radius, re-run after to confirm.

**Coverage gap.** These cover **A/B/C** and part of **D**. Nothing here diagnoses `d1` paragraphs
(**F**), `d2` TL;DR (**E**), `e1` incoming (**G**), or the holdout/promotion/queue layer (**H**) —
which is what the reference incident *looked* like on the surface; class **H** stays SQL + `views/{sid}.md`
per §4, and these tools diagnose the class-A cause underneath it. For general prose hallucination beyond
judge-health, use the `c3`/`c4` outputs and `f4` directly.

**Prefer `f10_nothink_audit.py`** (Phase-F eval) over `c3_nothink_review.py` for the standing
no-think-judge audit: same review logic, but it emits `eval/nothink_audit.json`/`.jsonl` and tracks the
no-think pass **rate** as a judge-health metric. `c3_nothink_review.py` is the ad-hoc self-contained
variant. Both also cover folded subjects, since fold-in refreshes their c3 verdict.

---

## 5. Repair playbooks

Pick **5A (full)** for version/taxonomy/model changes or when a large fraction of the cohort
is affected; **5B (surgical)** for a handful of data-only patients. Both finish with **5C**
(state reconciliation), **5D** (persistence), and **§6** (verification).

### 5A. Full cohort rebuild (bulletproof)

Backup first (§0). Then run the `PIPELINE.md` "Full rebuild — run order" verbatim:

```
# 0 schema (only on first run / schema change) — PgAdmin: run src/database/schema.sql
#   (single idempotent file; creates tiered_view/episode/ledger/ledger_raw + retention_facts +
#    generation_audit + compaction_review/change + promotions + retention_review + review_queue +
#    review_decisions + retention_dropped (B2 prune audit); ledger rows carry a `provenance` JSONB
#    column. FRESH-CREATION form -- every column lives in its CREATE TABLE (clean build in
#    one pass). To upgrade an EXISTING db, apply the missing columns as a one-off ADD COLUMN IF NOT
#    EXISTS migration. Run AFTER a4 (it FKs to patients).
# (run everything below from the repo root)
# A cohort (only if the cohort changed)
python src/a1_profile_cohort.py
python src/a2_build_cohort.py
python src/database/a4_load_and_embed.py             # [DB W patients/admissions]
# B facts → ledger
python src/b1_retention_extractor.py --fresh         # [DB R admissions BHC]  (long pole: ~1–2 days)
python src/database/db_persist_source.py --facts --fresh # [DB W retention_facts]
python src/b2_build_ledger.py                        # raw ledger + dropped[] prune audit + node provenance
python src/b3_loss_audit.py                          # QA
python src/b4_consolidate_ledger.py                  # ALWAYS after a fresh b2 -- see the b4/raw gotcha in §7
# C + D1 prose tiers
python src/d1_paragrapher.py --fresh
python src/c1_compactor.py --fresh
python src/c3_faithfulness.py --fresh                # served
python src/c4_correct.py --fresh                     # served
python src/database/db_persist_compaction.py --fresh # [DB W compaction_review/change] (after c3+c4)
python src/c2_retention_validator.py --fresh         # QA
python src/database/db_persist_retention.py --fresh  # [DB W retention_review] persist c2 MISSING/PINNED_IN_ROLLUP flags (after c2)
# D2 / coordinator + E incoming
python src/d2_tldr.py --fresh
python src/e1_incoming_summary.py --fresh            # [DB R held-out BHC]  MUST precede d3
python src/e2_embed_incoming.py                      # [DB W admissions.embedding]
python src/d_coordinator.py --write-md
python src/database/d3_load_tiers.py                 # [DB W tiered_view/episode/ledger]
python src/database/db_persist_source.py --audit --fresh # [DB W generation_audit]  run before c5
python src/c5_review_queue.py --fresh                # QA: review queue from PERSISTED generation_audit flags (MQTT notify)
```

Then **§5C** (reconcile promotions + review state), **§6** (verify). A full rebuild does not
reset stale promotions or review state on its own — those are DB state, untouched by `--fresh`.

### 5B. Surgical per-subject repair (fast)

For each affected subject, re-run **from the repair-entry stage downstream** (§1 fix→re-run
table), scoped with per-subject flags. Every fold-in stage (`b2 b4 c1 d1 c2 d2 d_coordinator d3`)
accepts `--subject S` (for `d1`/`c2` this is the WRITE mode, vs `--preview S` = print only, no
write); `b1` takes `--subject S` (whole subject, established span) or `--subject S --hadm H` (one
admission); `d3` takes `--subject S`; `e2` takes `--subject S --hadm H`.

**Example — consolidation over-merge (class A, the incident):**
```
# (state reconciliation from §5C first if a holdout flip is involved)
python src/b2_build_ledger.py                     # deterministic; rebuilds all ledgers incl. the fix
python src/b4_consolidate_ledger.py --subject S   # per affected subject
python src/c1_compactor.py --subject S
python src/c3_faithfulness.py --subject S          # if Prior Admissions prose is served/affected
python src/c4_correct.py --subject S
python src/database/db_persist_compaction.py --subject S    # [DB W]
python src/d2_tldr.py --subject S
python src/d_coordinator.py --subject S            # merges this subject into longitudinal_view.jsonl
python src/database/d3_load_tiers.py --subject S   # [DB W] DELETE-then-INSERT this subject's tier rows
```

**Example — hallucinated Prior Admissions prose (class D):**
```
python src/c1_compactor.py --subject S             # only if the compaction itself is regenerated
python src/c3_faithfulness.py --subject S
python src/c4_correct.py --subject S
python src/database/db_persist_compaction.py --subject S
python src/d_coordinator.py --subject S
python src/database/d3_load_tiers.py --subject S
```

**Example — wrong paragraph / TL;DR (classes F/E):** enter at `d1`/`d2` respectively, then
`d_coordinator --subject S` → `d3 --subject S`.

> `tiered_ledger`, `tiered_ledger_raw` and `retention_dropped` need **no** manual reset — `d3`
> DELETE-then-INSERTs them per subject on every load. Only `tiered_view.review_*` and the
> physician `prior_*` columns are sticky (§5C).

### 5C. State reconciliation (the step everyone forgets)

**Holdout / promotion.** To add/remove a promotion, edit the **table** in SQL, then reproject
the **cache**, then rebuild the affected subjects (their established/incoming split changed):

```sql
-- remove stale promotions (targeted). columns are (hadm_id, subject_id, ...).
BEGIN;
DELETE FROM promotions WHERE hadm_id IN (<HADM>, ...);   -- check ROW COUNT before COMMIT
COMMIT;
-- or clear all:  TRUNCATE promotions;
```
```
python src/database/db_persist_promotions.py --rebuild-csv    # DB → CSV; writes header-only if empty
```
Then flip the affected subjects established→incoming by re-extracting and rebuilding
(the held-out last admission must leave the established facts):
```
python src/b1_retention_extractor.py --subject S     # per subject; excludes the now-held-out last admission
python src/b2_build_ledger.py
python src/b4_consolidate_ledger.py --subject S
python src/e1_incoming_summary.py --subject S         # regenerate the incoming summary for the now-incoming admission
python src/d2_tldr.py --subject S
python src/d_coordinator.py --subject S
python src/database/d3_load_tiers.py --subject S
```
(Alternative to the SQL delete: empty/remove `promoted_admissions.csv`, then
`python src/database/db_persist_promotions.py --fresh` — `--fresh` TRUNCATEs then reloads from the CSV.)

**Physician review state.** `d3` preserves `review_*`. To return a repaired patient to the
pending queue (governance: this discards a prior sign-off — confirm it is stale/test first):
```sql
UPDATE tiered_view
SET review_status='pending_review', reviewed_by=NULL, reviewed_at=NULL,
    edited_summary_md=NULL, approved_summary_md=NULL, physician_notes=NULL,
    summary_version=0, folded_in_at=NULL
WHERE subject_id IN (<SID>, ...);
```

**Orphans** (cohort shrank): `python src/database/d3_load_tiers.py --prune` (verify with `--prune --dry-run`).

### 5D. DB persistence — make the DB match the files

| What changed | Persist step |
|---|---|
| facts (`b1`) | `python src/database/db_persist_source.py --facts --fresh` (or `--subject S --hadm H`) |
| any generation stage ran (audit trail) | `python src/database/db_persist_source.py --audit --fresh` — **run last** |
| compaction re-judged (`c3`+`c4`) | `python src/database/db_persist_compaction.py --fresh` (or `--subject S`) |
| retention flags re-checked (`c2`) | `python src/database/db_persist_retention.py --fresh` (or `--subject S`) |
| tier tables (view/episode/ledger, + `retention_dropped`) | the `d3_load_tiers.py` load itself |
| promotions | `holdout.promote()` in a real fold-in is authoritative; for a manual repair use SQL + `src/database/db_persist_promotions.py --rebuild-csv` |
| incoming RAG embeddings | `python src/e2_embed_incoming.py --force` |

### 5E. Sanctioned admission promotion (fold-in) — for reference

The normal promotion path is the physician clicking **Approve**, which spawns
`foldin_patient.py` as a background task. To run it manually (e.g. to replay a stuck fold-in
— the endpoint also exposes `POST /tiered/review/reconcile-foldins`):

```
python src/foldin_patient.py --subject S --hadm H            # real
python src/foldin_patient.py --subject S --hadm H --dry-run  # no Postgres writes
```

It holds a **cross-process Postgres advisory lock** for the whole cascade and runs, in order:
`b1 (one adm) → db_persist_source --facts [DB W] → holdout.promote() [DB W: promotions + CSV
projection] → b2 → b4 → c1 → c3 → c4 → d1 → c2 → d2 → d_coordinator → d3 --set-folded-in [DB W:
tiered_*, folded_in_at, one txn] → e2 --hadm [DB W: admissions.embedding] → db_persist_compaction
--subject [DB W] → db_persist_retention --subject [DB W: retention_review] → db_persist_source
--audit [DB W] → c5 --subject (best-effort MQTT notify)`. The fold-in runs the **full per-subject
chain**: d1 re-paragraphs the just-promoted admission (so the NEXT discharge finds its hot-window
paragraph), c2 re-checks retention over the newly-compacted span (persisted
to `retention_review` and surfaced in the physician re-review queue), e2 embeds the incoming
admission (`--hadm`-scoped so unapproved holdouts are never pre-embedded), and c5 notifies from the
PERSISTED flags (best-effort, last — a fold-in that already committed is not failed by a notify
error). So served prose, `compaction_review` provenance, `retention_review` flags, and the RAG
embedding are all **fresh** per fold-in (no class-J staleness); only b3 and the f\* evals stay
outside. Each stage is atomic + idempotent, so a failed fold-in is safe to re-run. A `--dry-run` still mutates the intermediate
JSONL and `promoted_admissions.csv` (only Postgres commits and `folded_in_at` are withheld) — so
run it under an isolated `LONGSUM_NAMESPACE` on a throwaway `data_<ns>/` copy if you must keep the
live tree pristine; otherwise revert by removing the subject from `promoted_admissions.csv`,
re-running `b1 --fresh` (or `b1_rerun_hadms` for the spliced hadm), and rebuilding.

---

### 5F. f2 gate failures – recovery by check name

`f2_integrity.py` names every violation. Map the `check` column of
`integrity_violations.csv` to a recovery here. Seven gates:
`gate_config`, `gate_holdout`, `gate_span`, `gate_consistency`, `gate_current`,
`gate_ledger`, `gate_hotwindow`.

**Step 0, always: stale artifact or live defect?** Read `audit.scores.mode` and the record's
timestamp and compare against the last code change to the owning stage. A `consistency`
failure on a record is the c1 `n_folded` bug and needs a metadata repair only. The same failure
on a freshly generated record - investigate, do not repair.

**Scope ruling:** `holdout_bleed` and `provenance_stray` **block the whole
build**, not just the flagged subjects. Both indicate a misconfigured run rather than a
per-patient accident, so the other patients are presumed affected until shown otherwise.
Every other check may be repaired per subject.

| `check` | Cause | Recovery |
|---|---|---|
| `config` | `N_HOT_DISPLAY > N_COMPACT_THRESHOLD` | Config only, no data damage. Fix, then `d_coordinator --write-md` → `d3`. |
| `consistency`, `consistency_missing_field` | Metadata only – prose and spans are correct | In-place repair: set `n_admissions_compacted = len(covered_hadm_ids)` in `compaction_results.jsonl`, taking a `.bak` first. Do NOT re-run c1: hours of GPU to change one integer. |
| `provenance_mismatch` (unpinned subject) | The paragraph does not describe the span the record claims it covers | Not repairable by metadata edit. `c1 --subject <SID> --mode full` to rebuild the span deterministically → c3 → c4 → `d_coordinator --write-md` → `d3`. |
| `span_future_leak`, `span_gap` | Threshold changed between runs, or a partial c1 | `c1 --subject <SID> --mode full` (recomputes `covered` from the current aged span) → c3 → c4 → coordinator → d3. |
| `span` (non-contiguous `admit_seq`) | Cohort build defect | Inspect `cohort_admissions.csv`; re-run a2 for the patient. |
| `hotwindow_missing` | **Anomaly L** – a demoted admission has no d1 paragraph | `d1_paragrapher.py --subject <SID>` → `d_coordinator --write-md` → `d3`. No c1 involvement; the compacted span is unaffected. |
| `current_missing` | Newest admission unrendered after promotion | Same chain as above. If d1 still produces nothing, check the admission actually has `bhc_text` in `cohort_admissions.csv`. |
| `current_duplicated`, `hotwindow_unexpected`, `hotwindow_size`, `n_total` | Coordinator assembly disagrees with the artifacts | `d_coordinator --write-md` → `d3`. For `n_total`, reconcile the promotion cache FIRST (§5C): `promoted_admissions.csv` is a cache, the `promotions` table is truth. |
| `ledger_schema` | Taxonomy drift between the artifact and current `retention_taxonomy` | `b2` then **immediately** `b4` (§7). Re-run b1 only if a class is genuinely unknown rather than renamed. |

#### The three that need care

**`holdout_bleed`** – a still-held-out admission sits inside a compacted span. This is future
information in the record, so the artifact must be **regenerated, never patched**. Reconcile
the promotion cache from the DB (`holdout.project_csv_from_db()`), then `c1 --fresh` and the
full downstream chain for the whole cohort. Before doing any of that, ask WHY: c1 running with
the holdout switch off is the usual cause and would affect every patient, which is why this
blocks globally.

Note this gate is **vacuous after a complete fold-in replay** – `holdout.holdout_hadms()`
subtracts promoted admissions, so once everything is folded in there is nothing left to bleed.
To make it mean something, evaluate at a mid-replay checkpoint
(`replay_production.py --stop-after N`).

**`provenance_stray`** – generated prose references an admission outside the covered span.
Same severity as a bleed, same global block, same regenerate-don't-patch rule. Inspect the
seeded fold-in path for the subject rather than assuming a one-off.

**`pin_region_broken`** – `prior_admissions_md` no longer starts with `pinned_prefix_md`; the
physician-edited region was mutated. **Do not re-run c1 to fix this, and never with
`--force-repin`** – that discards the edit by design. Order is restore-then-regenerate, because
a physician edit outranks any generated artifact:

1. Recover the prefix text from the DB audit trail (`compaction_change` / `compaction_review`).
   Ruled authoritative for restoration – no need to return to the physician
   to re-enter it.
2. Restore it into the compaction record.
3. Re-run c1 for the subject WITH the pin intact, then c3 → c4 → coordinator → d3.
4. Confirm `f2` clears `pin_region_broken` before releasing the subject back to review.

Note that for a pinned subject `generated_hadm_ids` is legitimately a strict subset of
`covered_hadm_ids` – the continuation covers only the span below the pin. f2 knows this and
applies the weaker `provenance_stray` check instead of equality. A pinned subject failing
`provenance_mismatch` would itself be a bug in f2.

---

## 6. Post-repair verification checklist

- **Queue count** via `GET /tiered/review/queue` equals expected (`= is_holdout − promoted`).
  Do **not** use `/health` `n_pending_review` — that counts the legacy `longitudinal` cohort.
- **The specific fix renders**: the recovered node / corrected prose is visible in the UI and
  in `data/output/views/{SID}.md` for each affected subject.
- **DB ↔ file consistency** for each affected subject: `tiered_ledger` matches
  `retention_ledger.jsonl`; `incoming_hadm_id` set **iff** the subject is still held out;
  `review_status = 'pending_review'` for anything meant to be re-reviewed.
- **Prune audit landed** (after any b2/b4 re-run):
  `SELECT reason, count(*) FROM retention_dropped GROUP BY reason ORDER BY 2 DESC;`
  Expect `not_pinned_tier` to dominate, then `suspected_inactive`. **Zero rows after a b2 re-run
  means the audit did not propagate** — usually b4 reconsolidated from a pre-v2.11
  `retention_ledger.raw.jsonl` (§7). An `unclassified` row means the taxonomy gate declined a
  fact for a combination `prune_reason()` does not name — investigate rather than ignore.
- **Promotion coherence**: `SELECT count(*) FROM promotions;` equals the data-row count of
  `promoted_admissions.csv` (`--rebuild-csv` if not).
- **Tier-0 gates green** (run-time, per subject, or full): `f1_salience_recall.py`,
  `f2_integrity.py`, `f3_compression.py`. For a consolidation change also check
  `f9_merge_faithfulness.py` (raw-vs-consolidated).
- **No unintended orphans**: `python src/database/d3_load_tiers.py --prune --dry-run` reports 0 (unless a
  shrink was intended).

---

## 7. Gotchas quick-reference

- **`promotions` column order is `(hadm_id, subject_id, …)`** — `SELECT *` shows hadm first.
  Transposing them makes every downstream `WHERE subject_id IN (…)` silently match nothing.
- **Table vs CSV** — the pipeline reads the CSV cache; the table is truth. Change one,
  `--rebuild-csv` the other.
- **`d3` preserves `review_*`** and does **no global truncate** (per-subject only; use
  `--prune` for orphans). A stale `approved` hides a fixed patient from the queue.
- **Compaction targets the OLDEST span**; holding out the LATEST admission never touches it,
  so `c1`/`c3`/`c4` can be skipped when only the holdout flag flips (not when the ledger or
  prose content changed).
- **`c3`/`c4` are served** — never skip them on a path that regenerates compaction.
- **`c4` runs on ANY unsupported statement** , not only when c3's `pass` is false —
  a paragraph can score well above threshold and still carry a named fabrication.
- **Physician columns are never regenerated** — `tiered_view.edited_prior_admissions_md`,
  `prior_edited_*`, `prior_pinned*`, `review_*` are absent from `d3`'s UPSERT by design. If a repair
  must clear them, do it explicitly and record why; `review_decisions` is append-only for audit.
- **`b4` reconsolidates from `retention_ledger.raw.jsonl`, not from b2's fresh output**, whenever
  the served ledger is already marked `consolidated`; it only re-backs-up raw when the served ledger
  is UNconsolidated. **After any b2 change, run `b2` first
  and let it rewrite the served ledger, then `b4`.** No error is raised either way; the tell is zero
  rows in `retention_dropped` (§6).
- **`setup/smoke_test_schema.py` CREATEs and DROPs the database it names.** `DEV_GUARD_DBNAME` is a
  RESERVED SENTINEL (`longitudinal_bhc_dev`) that must never be a real DB — do not "align" it with
  the real dev name to match schema.sql's denylist, which holds both names precisely so this test
  has a safe name to destroy.
- **c1's echoed ledger is SPAN-SCOPED and named.** `CompactorOutput.ledger_facts` (was
  `retention_ledger`) holds only the facts the model was actually shown, with
  `audit.scores.ledger_scope_max_seq` / `n_ledger_scoped` / `n_ledger_total` to confirm it. 
- **`compaction_faithfulness.jsonl` has no `hadm_id`.** It used to carry one holding
  `str(subject_id)` (echoed from the vendored validator's trace key), which made the row look
  joinable on an admission id. Removed; join on `subject_id`, scope via `covered_hadm_ids` /
  `generated_hadm_ids`. 
- **Schema drift between databases is real.** `schema.sql` is fresh-creation form and does not tell
  you what a given DB has. Check before blaming a stage:
  `SELECT table_name, count(*) FROM information_schema.columns WHERE table_schema='public' GROUP BY 1;`
- **Version bumps invalidate the cohort** — a `config.py` model/prompt or taxonomy change is a
  full rebuild, not a surgical patch, and taxonomy changes need sign-off. **Exception: an ADDITIVE
  taxonomy bump.** v2.11 left `is_ledger_member` untouched, so nothing about what is pinned or served
  changed and no re-extraction was needed — `b2` onward re-derives. Check the CHANGELOG entry for
  whether a bump changed STORED values (v2.9 and v2.10 did, and needed a full `b1` re-run).
  **v2.12 is the in-between case:** it DID change `is_ledger_member` (and b2's `amended` routing),
  so what is served changed — safety-class de-labels now appear in `nodes`/`flagged` — but only
  DERIVED values moved, so `b2` onward still suffices and no `b1` re-run is required. Its b1 prompt
  change is staged and unexercised.
- **A stale f12 baseline does not gate silently** — `frontier_baseline.json` carries a
  `taxonomy_version`, and f12 checks it. On a mismatch the dominance gate is SKIPPED
  (reported as `baseline_stale` in the JSON and on stdout), the absolute floor gate still applies,
  and a passing run rewrites the baseline at the current version. So after a taxonomy bump the
  first f12 run re-baselines itself — do not hand-edit the file, and do not read a `SKIPPED`
  dominance gate as a pass.
- **VRAM**: one ~35B model at a time; stages unload on exit; one judge job at a time.
- **Fold-in ≈ full per-subject pass** — runs d1/c2/e2 + `db_persist_compaction`/
  `db_persist_retention` + a c5 notify (served prose, faithfulness, retention flags, and the incoming
  RAG embedding all stay fresh). Only b3 and the f\* evals are outside it; run those on the next full pass.
