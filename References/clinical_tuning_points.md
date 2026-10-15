# Clinical tuning points for governance — clin_longsum

**Purpose.** This document lists every setting in the pipeline that encodes a
**clinical judgment call** - as opposed to a plain engineering choice. Every row here should be *owned*
by clinicians or their governance board. 

Two ownership tags are used throughout:
- **[DECIDE]** — there is no "correct" engineering answer here. This is a
  judgment call only clinicians can make.
- **[RATIFY]** — the build team picked a working default to get the pilot
  running. It needs your review: confirm it, or change it.

Expect some of these to differ by site — a tertiary oncology centre and a
community hospital will reasonably set different tolerances for how much
missing information or how much AI uncertainty is acceptable. 

A few terms used throughout, for anyone newer to how this kind of system
works:
- **AI model / "the model"** — the language model doing the reading and
  writing. There are two in this pipeline: one that drafts and summarizes, and
  a separate one whose main job is to check the first one's work.
- **Fabrication ("hallucination")** — the model writing something that isn't
  actually supported by the source notes.
- **Judge** — the second AI model, used to check the first one's output for
  fabrication or quality. It is not infallible and is itself periodically
  checked against real clinician judgments (Section D).
- **Gate** — an automatic pass/fail checkpoint. If a batch of summaries fails
  a gate, that version of the pipeline is blocked from going live.
- **Flag** — an individual item put in front of a clinician for a decision,
  short of blocking the whole system.

---

## A. The salience taxonomy — what matters, and how much

This is the clinical heart of the system: which facts the AI is told to treat
as important, and how important. Everything downstream depends on these
judgments.

| Setting | The judgment behind it | What goes wrong if it's set poorly |
|---|---|---|
| **[DECIDE] How the ~12 fact categories are ranked**, from "must never be missed" down to "safe to eventually drop" | Deciding which kinds of clinical information are sacrosanct versus safely summarized away | Rank something too low and it gets silently dropped; rank everything high and every summary bloats, defeating the purpose |
| **[DECIDE] How strongly the top rank should dominate** the overall "did we retain what matters" score | Should one missed critical fact swamp the whole score, or can it be averaged away by dozens of minor facts retained correctly? | Get the balance wrong and either the score is meaningless noise, or one edge case blocks every release |
| **[DECIDE] Which categories are always kept no matter what**, regardless of context (e.g. a severe drug allergy) | Which facts carry top priority even if the AI mis-labels their usual category | A category that should never be lost gets treated as routine, or over-protecting everything makes the system impossible to satisfy |
| **[DECIDE] "Safety-net" categories** — allergy status, code status (resuscitation wishes), infection-control status | Which categories are safety-critical enough to survive even if the AI mislabels other details about them | A missed allergy or resuscitation-status entry reaching a clinician is a direct patient-safety risk |
| **[RATIFY] How a documented *absence* is handled** — e.g. "no evidence of TB" — including which kinds of absence matter enough to keep | The system distinguishes: a true absence/negative test; a resolved-but-still-relevant past event (e.g. a prior blood clot); and an incidental finding under active surveillance. | A clinician re-investigates something already settled, or a meaningful "ruled out" finding is lost entirely |
| **[DECIDE] When a repeat episode is really a red flag, not routine recurrence** | Should a cancer recurrence, organ rejection, or similar severe repeat event be shown as its own story, or folded into a routine "happened again" tally? | A serious recurrence gets compressed into a frequency count and loses its narrative |
| **[DECIDE]** *How complete is that recurrence protection list?* | The system now has a fixed, developer-maintained list of severe events (cancer recurrence, organ rejection, status epilepticus, gangrene, graft/stent blockage) that are automatically protected from being buried in a recurrence tally. Less severe *chronic-disease* flare-ups are not on this list — such as recurrent COPD flareups, hypoglycemic/hyperglycemic episodes. | Should clinicians be able to nominate additional conditions for this protected list, or is case-by-case triage (dismiss/escalate per instance) an acceptable long-term answer? Currently: case-by-case only |
| **[DECIDE]** *Should a "suspected but resolved" major event ever stop needing review?* | A suspected major event that later turned out to have resolved (e.g. a possible heart attack that turned out not to be one) still stays flagged for physician review indefinitely, with no automatic expiry | If left unbounded, minor historical false alarms may accumulate as permanent review items with no way to age them out |

---

## B. What gets dropped or shortened over time — compaction

As more admissions accumulate, older ones are compressed into a shorter
narrative so the whole record doesn't become unreadable. These settings
control that "forgetting curve."

| Setting | The judgment behind it | What goes wrong if it's set poorly |
|---|---|---|
| **[DECIDE] Recency window** (currently: the most recent 7 admissions stay in full detail) | How many recent admissions a clinician sees written out in full before older ones get compressed | Too short loses recent detail; too long defeats the point of compressing anything |
| **[DECIDE] How long a minor, resolved issue is kept** before being dropped (currently: 90 days) | The point at which a one-off, resolved issue (a cold, a one-off lab blip) is judged no longer clinically relevant | A still-relevant issue is dropped too early; or clinically trivial detail is kept forever, cluttering the record |
| **[RATIFY] When repeat episodes get folded into a frequency count** rather than shown individually | When does "this happened four times" become more useful than four separate write-ups? | An individually important episode gets hidden inside a "happened 4x" bucket |
| **[RATIFY] How AI decides two write-ups describe the same underlying condition** before merging them | Whether two different phrasings of a condition should be treated as the same thing — now itself an AI judgment, not a simple text-matching rule | Merge too aggressively and an active condition gets buried under an older one; merge too little and duplicate clutter remains. Because this merge is itself AI-judged, it needs occasional spot-checking |
| **[RATIFY] How many recent admissions the physician sees written out in full**, at the point of care (currently: 7) | Balancing enough recent context against overwhelming the reviewer | Too few hides recent context; too many overwhelms |
| **[DECIDE]** *What to do with facts that silently vanish* | Facts that didn't fit neatly into any retention bucket are caught and logged for audit. | It is currently recorded for audit only. |

---

## C. Quality gates — what's "good enough" to reach a clinician

These decide what counts as an acceptable AI-written summary, and what blocks
a version of the system from being used at all.

| Setting | The judgment behind it | What goes wrong if it's set poorly |
|---|---|---|
| **[DECIDE] Acceptable rate of genuinely losing an important fact** (currently: zero tolerance) | How much silent loss of a flagged-important fact is acceptable before blocking a release | Too lenient tolerates silent clinical omission; too strict blocks safe releases over noise |
| **[DECIDE] Which failures block a release outright** vs. which are just tracked | What counts as a release-blocking safety failure vs. a logged-but-shippable issue | A failure that should block release instead just raises an alert, and a flawed version ships |
| **[DECIDE] Should a lost active-cancer fact always block a release?** (currently unresolved either way) | Whether losing an active-malignancy fact is severe enough to be release-blocking on its own | Either a cancer-related omission ships with just an alert, or the entire chronic-disease category becomes impossible to release |
| **[DECIDE] Acceptable rate of AI fabrication** in generated text — *note: there are two separate numbers in play here, worth distinguishing.* The system running live today allows a paragraph to keep 0.6 (60%) of its claims verified before it's treated as failing (calibrated against human review; the apparently lax cutoff is because of high false-positive rate). A separate, offline benchmarking script (used for periodic study, not live patient records) currently defaults to stricter numbers (0.9–0.95) in its code. **The number that governs live patient records today is the first one (0.6).** No real-world clinical threshold has been formally established for either | The acceptable hallucination rate in generated prose — expected to reasonably vary by site | Too lenient tolerates fabrication reaching a clinician; too strict blocks usable output for no clinical benefit |
| **[DECIDE] Minimum documentation-quality bar** (currently: an average score of 4/5, with no attribute allowed below 3.5/5, across ten quality attributes rated by the AI judge) | The floor for what counts as acceptable clinical writing | A summary that reads as organized but is unhelpful, or subtly stigmatizing in tone, could still pass on average |
| **[DECIDE] Should accuracy outrank style?** (currently: all ten quality attributes are weighted equally) | Whether being accurate and well-sourced should matter more than being succinct or well-organized | A high style score could mask an accuracy problem when everything is averaged together |
| **[DECIDE] Tolerance for stigmatizing language** reaching the record | How much tolerance there is for phrasing that stigmatizes a patient | Both an ethical/equity harm and a documentation-standards issue |
| **[RATIFY] How much compression is "enough"** (a target range for how much shorter the compressed record should be) | Mostly operational, but the upper end of the range asks: is *enough* still being kept? | Drift toward too little compression (record re-bloats) or too much (over-compression) goes unflagged |

---

## D. How much to trust the AI's own quality-checking

A second AI model ("the judge") grades the first model's fabrication rate and
writing quality. How much to trust *that* judge is itself a governance
decision, separate from trusting the model that writes the summaries.

*Note: this section is about the fabrication/writing-quality judge only. The
separate check for whether important facts were retained at all (Sections A
and B) is fully rule-based — no AI judgment is involved in that check.*

| Setting | The judgment behind it | What goes wrong if it's set poorly |
|---|---|---|
| **[DECIDE] How closely the judge must match real clinician opinion** before its scores count as evidence | The bar the judge model must clear against real clinician-labelled examples before its output counts as quality assurance | An unqualified judge's scores get reported and trusted as real quality assurance — false confidence |
| **[DECIDE] How often clinicians manually re-check the judge** (currently: an occasional spot-check of 20–30 patients) | How much, and how frequently, real clinicians must re-verify the automated grader | The judge could quietly drift off-target between spot-checks without anyone noticing |
| **[RATIFY] Who decides when a new admission's summary conflicts with the existing record** | Whether the AI, a rule, or a clinician has final say when new information seems to contradict what's already on file | A genuine clinical change gets dismissed as noise, or a stale fact persists unchallenged |

---

## E. The physician review and update workflow

| Setting | The judgment behind it | What goes wrong if it's set poorly |
|---|---|---|
| **[DECIDE] Depth of review required, and who may approve it** | What level of physician review a new AI-written summary needs, and who is authorized to approve it | Too little review lets errors into the permanent record; too much makes the system impractical to use |
| **[RATIFY]** *A physician's "dismiss" or "resolved" decision on a flagged item is now permanent.* Once you dismiss or resolve a flagged concern, that decision sticks — even if the exact same underlying issue would otherwise resurface after the record is regenerated. There is currently no expiry or automatic re-confirmation requirement. | Should a physician's dismissal of a flagged concern ever need re-confirming — e.g. after a long time, or after the underlying AI system is updated? | Currently: never expires. A decision made once, potentially years ago, remains binding indefinitely |
| **[RATIFY] Which safety checks must pass before a freshly updated summary is shown at all** | The minimum automatic checks required before a newly regenerated view is served to a clinician | A weak automatic check lets an unsafe per-patient view through |
| **[DECIDE] Who can override a failed safety check, and how that's recorded** | Governance control over who may waive a safety stop, and the audit trail for doing so | Unaccountable or silent overrides of a safety-critical stop |

---

## F. How the AI is allowed to fill gaps

| Setting | The judgment behind it | What goes wrong if it's set poorly |
|---|---|---|
| **[RATIFY] "A summary of a summary"** — the AI-written narrative for older admissions is built from the discharge summary, not the original clinical notes | Accepting that each layer of summarization loses some detail, and understanding where the accuracy ceiling actually sits | Governance assumes a level of original-note-level detail the system was never designed to preserve |
| **[RATIFY] The structured fact list isn't shown to the tool that writes the one-paragraph TL;DR** | Relies on chronic conditions naturally being mentioned in the narrative text anyway, rather than double-checking against the fact list | A chronic condition that only exists in the structured fact list (not mentioned in any narrative) could be left out of the TL;DR |
| **[DECIDE] Whether the AI may guess a redacted/blanked-out word** (e.g. a masked diagnosis name being confidently filled back in) | Is it acceptable for the AI to reconstruct a masked medical term? Usually harmless for a generic clinical term, but a real risk if the blank was a specific name, date, or place | A confidently wrong guess fills in a redacted personal detail |
| **[RATIFY] How much wording is allowed to vary** between runs on identical input | The acceptable degree of run-to-run wording variation for the same underlying facts | Too much variation undermines reproducibility and makes review harder |

---

## G. Which patients this has been tested on

| Setting | The judgment behind it | What goes wrong if it's set poorly |
|---|---|---|
| **[DECIDE] The population used to tune all of the above** (currently: a single credentialed US research dataset) | Whether thresholds tuned on this dataset will transfer sensibly to your patient population | The taxonomy and thresholds may not fit local case-mix, coding conventions, or language patterns |
| **[DECIDE] How many admissions per patient this has actually been tested on** (currently validated on patients with roughly 3–7 admissions; behaviour on patients with hundreds of admissions is untested) | The admission-count range over which the system's behaviour is actually known | Untested behaviour once real, lifetime-scale patient records are used in production |

---

## Not for clinical sign-off (engineering defaults)

For clarity on what this board does *not* need to own: technical context-window
sizes, decoding parameters, GPU memory management, file locking during
concurrent updates, file formats, date-parsing conventions, which exact AI
model version is bound to which role, and the internal prompt-version
tracking. These are all recorded for audit in the background, but none of
them represent a clinical value judgment.

---

## How to use this with governance

1. Start with the **[DECIDE]** rows — these have no engineering-correct
   answer and require clinical sign-off, full stop.
2. For each one, record the *value chosen*, the *clinician who owns that
   decision*, and the *reasoning* — this is what makes the choice auditable
   for the eventual national health-cloud governance path.
3. Flag the rows likely to need **site-specific recalibration** before any
   new-site deployment: the fabrication tolerance, the retention floor, the
   documentation-quality bar, and the "how long before a minor issue is
   dropped" window.
4. Re-review this document whenever the taxonomy version changes (see the
   date and version stamped at the top) — Sections A and B in particular
   ripple through every gate downstream.
5. **Bring a developer to this meeting for the [RATIFY] rows.** Several of
   these describe *how* the AI system behaves mechanically (the two-region
   edit-protection design, the recurrence-vs-relapse detection list, the new
   silent-drop audit trail). You own the decision; the build team should be
   on hand to explain the mechanism behind it before you ratify or change it.

---

## Appendix — for the dev team: where each tuning point lives in code

Every row above maps to a specific file and, in most cases, a specific named
constant or function. This appendix is the pointer from a governance decision
to the exact place it should be changed — verified live against `src/`.


| Doc section / row | Script | Knob | Notes |
|---|---|---|---|
| **A** — the 12 retention classes and their tier | `retention_taxonomy.py` | `RETENTION_CLASSES` tuple (each entry's `default_tier` field) | Tier is a *floor*, not fixed (`floor_tier()`) |
| **A** — how strongly the top tier dominates the score | `src/evaluation/f1_salience_recall.py` | `TIER_WEIGHT = {"T1":5.0,"T2":4.0,"T3":3.0,"T4":2.0,"T5":1.0}`; `FLOOR_TOP = TIER_WEIGHT["T1"]` | `is_critical()` uses `salience_weight(f) >= FLOOR_TOP` — "critical" in practice means "T1" |
| **A** — always-kept floor classes | `retention_taxonomy.py`: `RETENTION_CLASSES` entries with `default_tier="T1"` | `severe_adr_allergy`, `infection_control_status`, `code_status_directive` | `anatomy_altering_surgery` is T2, but kept as well |
| **A** — safety-net categories | `retention_taxonomy.py` → `_SAFETY_ROBUST_CLASSES`; separately `f1_salience_recall.py` → `SAFETY_CLASSES` and `gate_safety` | two-item vs. three-item list — see drift note above | |
| **A** — documented-absence handling (`negative_kind`) | `retention_taxonomy.py` | `negative_kind` field (enum: `None` / `'absent_or_negative'` / `'resolved_relevant'` / `'surveillance'`); `merge_negative_kind()` function |  |
| **A** — negative-kind gate | `src/evaluation/f1_salience_recall.py` | `FLOOR_SIGNEG = TIER_WEIGHT["T3"]`; `gate_signeg` | Fires if any fact with a non-null `negative_kind` is lost or rollup-collapsed |
| **A** — sentinel-recurrence protection (named list) | `retention_taxonomy.py` | `is_sentinel_episode()`, `is_disease_course_event()`, `is_surveillance_finding()`; pattern match via `_SENTINEL_EPISODE_RE` | Currently covers malignancy recurrence/relapse/progression, graft/organ rejection, status epilepticus, gangrene, graft/stent occlusion or thrombosis (v2.6–v2.8). Diabetes decompensation and similar chronic-disease flares are **not** on this list |
| **A** — routing enforcement (B1/B2) | `b1_retention_extractor.py` (at-source prevention), `b2_build_ledger.py` (deterministic net) | prompt + routing rule for sentinel episodes | |
| **A** — suspected-but-resolved pinning | `retention_taxonomy.py` | `is_ledger_member()` | `PROVISIONAL` suspected-sentinel now applies regardless of `current_lifecycle`  |
| **B** — recency window | `config.py` | `N_COMPACT_THRESHOLD = 7` | |
| **B** — minor-issue prune window | `config.py` | `CONSOLIDATE_T5_PRUNE_DAYS = 90` | applied in `ledger_consolidate.py` / `b4_consolidate_ledger.py` |
| **B** — recurrent-episode metric consolidation (T4) | `config.py` → `CONSOLIDATE_T4` flag; logic in `ledger_consolidate.py` | | |
| **B** — same-condition merge, deterministic pass | `b2_build_ledger.py` | `_consolidate()` — merges at Jaccard ≥ 0.6, curated condition-key match, or verbatim phrase-containment ≥ 2 tokens | class-group gated; never merges across `current_lifecycle`. every resulting group is passed through `_split_group` (next row) |
| **B** — identity guard on grouping (FM-7) | `b2_build_ledger.py` | `_split_cluster()` (after `_cluster`), `_split_group()` (inside `_consolidate_group`); `_identity_tokens()` = content tokens minus `_QUALIFIERS` | No entry may display a wording that names a condition some of its own members do not share. Prevents a COMPOUND fact ("Coronary artery disease and peripheral vascular disease") bridging two conditions so that latest-wins wording erases one. Can only increase entry count. No knob – always on except for `_SPLIT_EXEMPT` |
| **A/B** — code-status directives exempt from the identity guard | `b2_build_ledger.py` | `_SPLIT_EXEMPT = frozenset({"code_status_directive"})` | Unlike an allergy, a directive legitimately mutates over time and the most recent statement best reflects the patient's wishes, so latest-wins collapse is correct and splitting would surface every superseded directive as its own entry. Do not extend this set without a clinical decision |
| **B** — same-condition merge, LLM-judged pass (T3) | `config.py` → `CONSOLIDATE_T3` flag; logic in `ledger_consolidate.py` | | runs after `_consolidate()`, catches what the deterministic pass misses |
| **B** — hot-window display size | `config.py` | `N_HOT_DISPLAY = 7` | |
| **B** — silent-drop audit trail | `retention_taxonomy.py` | `prune_reason()`; `RetentionLedger.dropped[]` field | Persisted via `retention_dropped` table, not yet surfaced as a physician-facing flag |
| **C** — retention-loss tolerance | `src/evaluation/f1_salience_recall.py` | `gate_swr`, `--threshold` (default `0.995`) | overall SWR-lenient floor |
| **C** — per-class release-blocking gates | `src/evaluation/f1_salience_recall.py` | `gate_T1`, `gate_T2`, `gate_safety`, `gate_signeg` | these already exist as separate named gates - reported, and decision to block/release to be made by dev team in consultation with clinical governance |
| **C** — fabrication tolerance (production, live records) | `vendor/faithfulness_validator.py` (used by `c3_faithfulness.py`) | `FAITHFULNESS_THRESHOLD = 0.6`; `_STEP4_MIN_COVERAGE = 0.9` | this is the number governing what a clinician actually sees |
| **C** — fabrication tolerance (offline benchmark) | `src/evaluation/f4_faithfulness.py` | CLI default: `0.9` synth / `0.95` all (line ~295) vs. its own docstring, which still says `0.6`/`0.9` (line ~42, stale) | not live-record-governing; benchmarking only |
| **C** — documentation-quality floor | `src/evaluation/f5_pdsqi.py` | `SCORE_FLOOR = 3.5` (per-attribute, **advisory only, not gating**); `--threshold` default `4.0` (overall mean, this IS the gate) | |
| **C** — equal weighting across the 10 quality attributes | `src/evaluation/f5_pdsqi.py` | `KEYS` list / scoring loop | no per-attribute weight variable found — currently a flat average |
| **C** — stigmatizing-language tolerance | `src/evaluation/f5_pdsqi.py` | one of the 10 PDSQI rubric attributes | exact sub-threshold not separately broken out from the overall floor above |
| **C** — compression target band | `src/evaluation/f3_compression.py` | `COMP_LO, COMP_HI = 0.05, 0.45`; `AGGRESSIVE = 0.05` | |
| **D** — spot-check cadence | *(process, not code)* | — | operational practice, not a config value |
| **D** — new-vs-existing-record conflict handling | `c5_review_queue.py` | `prior_span_drift` role in `_describe()` | severity currently hardcoded `"high"` in that function, not a named config constant — see next row |
| **E** — review depth / who may approve | `api/longsum_api.py` | `/tiered/review/patient/{id}/submit` endpoint; `foldin_patient.py` chain gating | |
| **E** — physician-edit protection (two-region model) | `c1_compactor.py` | `pinned_prefix_md`, `seed_source` fields (`roles/contracts.py`) | display-side: `database/d3_load_tiers.py`, `d_coordinator.py` via `COALESCE(edited_prior_admissions_md, prior_admissions_md)` |
| **E** — physician dismiss/resolve permanence | `database/db_persist_retention.py` | UPSERT on `(subject_id, issue, source_hadm_id, fact_text)` — never overwrites `review_status` once non-`'pending_review'` | same pattern for TRIAGE in `c5_review_queue.py` / `review_queue` table |
| **E** — `prior_span_drift` severity | `c5_review_queue.py` | `_describe()` — hardcoded `"high"`, no config knob | flagged in project notes as possibly better at `"medium"` under the two-region model; currently unresolved and not exposed as a setting |
| **E** — pre-serve safety checks | `foldin_patient.py` | cascade order: `c3_faithfulness.py` → `c4_correct.py` run before `d3_load_tiers.py` serves | |
| **E** — override of a failed safety check | no code lever  | — | re-run the failed stage |
| **F** — "summary of a summary" | `setup/DATA_PREP.md`; `config.py` | `config.SOURCE_TEXT_FIELD = "target"` (the BHC field, not the raw note) | |
| **F** — ledger not shown to the TL;DR writer | `d2_tldr.py` | does not import/read `ledger_facts` | relies on narrative redundancy instead |
| **F** — AI filling redacted terms | `b1_retention_extractor.py` | extraction prompt | "anti-inference counterweight" added post-forensic-trace on the fabricated-diagnosis case (FM-1) |
| **F** — wording variability | `config.py` | `temperature = 0.2` per role (`single_summary`, `episode_paragrapher`, `tldr_synthesizer`, `compactor`) | |
| **G** — validation population | `setup/DATA_PREP.md`, `cohort.py` | MIMIC-IV + MIMIC-IV-Ext-BHC only | |
| **G** — tested admission-count range | `a2_build_cohort.py` | `BANDS`, `CAP_MAX` constants | controls the stratified draw shape/size |


