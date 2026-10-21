# FEEDBACK_RUNBOOK.md — triaging & answering physician ledger reports

**Audience:** the support team (clinical + engineering) working the triage board.
**When to use:** a physician has reported a problem with a served longitudinal record from the
review interface, and you are the person who has to decide what it is, whether it is real, who
owns it, and what to tell them.

This guide covers the loop from report to answer. It does **not** cover repairing the data — once
you know the fault class, hand off to `REPAIR_RUNBOOK.md`, which owns diagnosis and repair of the
pipeline itself. This runbook is about the conversation with the physician, that one is about the artifacts.

**Portability** Generic ops guide — placeholder IDs only. Never paste real subject or
admission IDs, patient text, or dataset names into it. Read alongside `HOW_TO_READ_THE_LEDGER.md`
(what the physician was told), `REPAIR_RUNBOOK.md` (repair), `PIPELINE.md` (stage I/O),
`CLINICAL_TUNING_POINTS.md` (governance).

---

## 0. Guardrails (non-negotiable)

- **Nothing closes without a written answer to the reporter.** Enforced twice — in
  `support/service.py` and as a `CHECK` constraint on the table — so it holds even against a
  caller that bypasses the service. 
- **Urgency is a floor.** Rules may raise a card to urgent; only a **named human** may lower it,
  with a written reason, appended to the audit trail. 
- **Dual-owned cards need both owners.** An S1 or S99 card carries clinician *and* engineer. One
  classifies, the other acknowledges. It exists to stop an ambiguous report being closed unilaterally 
  into the wrong class, which is the specific risk on S1 where an extraction miss and a taxonomy prune 
  look identical from the physician's seat.
- **The triage policy is a governance object.** `support/policy/triage_policy.yaml` changes need
  a version bump, a CHANGELOG entry and clinical sign-off — same as `retention_taxonomy.py`.
  Engineering may propose; only the clinical owner ratifies. "What counts as urgent" is a
  clinical call.
- **Never edit a card's history.** `feedback_events` is append-only. Corrections are new events.
- **Support state lives in its own database.** `longsum_support`, reached only via
  `PGDATABASE_SUPPORT` / `database.pg.get_support_conn()`. It holds no patient data beyond the
  reported row text, and no clinical stage reads it. Do not add a join from the pipeline to it.
- **Patients are referenced by alias (`PT-nnnn`) outside the clinical UI.** Board, email and
  digest all use the alias. Do not paste `subject_id` into an email.

---

## 1. Mental model — what happens before you see a card

A report is triaged, routed and acknowledged **before the physician leaves the page**. By the
time a card reaches you, three things have already happened:

```
physician clicks "report"
   │
   ├─ capture      snapshot of the row + cohort, namespace, taxonomy_version,
   │               artifact generated_at  (server-side; the client is not trusted
   │               to report the version of what it is displaying)
   ├─ triage       deterministic: urgency rules U-1..U-7, symptom→owner routing,
   │               dedup signature                              [inline, ~ms]
   ├─ acknowledge  email queued to the reporter
   └─ evidence     collector runs against the clinical DB       [background]
                   → S1 sub-class, source-note probe, taxonomy check, staleness
                   → re-triage with what it found
```

**Triage is deterministic.** That is a design choice with a consequence
you rely on: a proposed policy change can be replayed over every historical card and diffed
before it is adopted (§6). It also means a triage decision is always explainable — every urgent
card names the rule that fired.

**The evidence collector is the reason most cards answer themselves.** It is read-only against
the cohort and it executes the deterministic taxonomy functions rather than predicting them.
Where its finding settles the question, the board pre-fills a disposition and a draft reply.

**What the physician was promised** (`HOW_TO_READ_THE_LEDGER.md` §8): every report gets a written
answer by email, including "working as designed" and "could not reproduce"; safety-relevant
reports jump the queue; fixes ship on a release cycle, not one at a time. Your job is to make
that true.

---

## 2. Reference incident (worked example — the shape to generalize)

**Symptom:** a physician reported that bilateral adrenal nodules, documented in the notes, were
missing from the ledger. Reported as S1 (a fact is missing), dual-owned, routine.

**What made it non-obvious:** the finding *was* in a source note, so "not in the source" was
wrong. But the ledger was also correct.

**Root cause:** the finding appeared only in the **incoming** admission — the one under review.
The ledger is built from *prior* admissions, so it was correctly absent and would appear on
fold-in. Not a defect. Classified **S1e**.

**The trap:** a naive substring search for the term also matched `hypoadrenalism` in an earlier
admission, inside the phrase "no evidence of hypoadrenalism" — a different concept, negated. Had
that matched, the card would have read `in_prior_unextracted` → **S1a**, an extraction miss,
which is the most expensive disposition in the system: a prompt change and a cohort-wide
re-extraction. The probe rejects it because it tokenises on word boundaries and screens for negation cues.

**Resolution:** closed `by_design` with the standing S1e answer, and the *guide* changed — §1 and
§8 of `HOW_TO_READ_THE_LEDGER.md` state that the ledger covers prior admissions only.

**Lesson, and the one to generalize: a report that is not a defect is still a finding.** It
told us the interface fails to distinguish the incoming admission from the established record.
The fix belonged in the documentation, not the data. Expect this class to be common.

---

## 3. Report taxonomy — map the symptom to the owning role

Physicians pick a **symptom** (S-code) describing what they saw. Routing is deterministic from the symptom; the **defect class** is
your call at disposition.

| Code | What the physician saw | Owner | Usual class | Notes |
|---|---|---|---|---|
| **S1** | A fact is missing that should be there | **both** | ambiguous | The commonest and most ambiguous report. Do not classify by hand — the collector resolves it to S1a–S1e (§3.1) |
| **S2** | Something stated that is not in the notes | engineer | engineering | Most reported fabrications are synonymy. Check the `bhc_support` finding before believing it |
| **S3** | A fact is labelled wrongly (tier, class, certainty, status) | clinician | taxonomy | Governance — needs sign-off + version bump. `disputed_field` says which attribute |
| **S4** | A fact reads as the opposite of the truth | clinician | taxonomy | Polarity is a clinical judgement first. If the evidence shows a rendering fault, **reassign** to engineer explicitly |
| **S5** | Two conditions merged, or one split across rows | engineer | engineering | FM-7 territory. Check artifact `generated_at` against the 2026-08-01 split fix before investigating |
| **S6** | A rollup is too coarse — I need the episodes | clinician | taxonomy | Usually by design (T4 `recurrent_episode`). A repeat S6 means the guide is failing |
| **S7** | A date is wrong, or rows are out of order | engineer | engineering | Files opened in Excel or pandas 3.0 can exhibit day-month swap |
| **S9** | The paragraph and the ledger disagree | engineer | engineering | Ledger is the reference; the paragraph is what needs correcting |
| **S10** | Something displayed wrongly or unreadable | engineer | engineering | Cheapest tier — display/routing fixes |
| **S11** | This does not belong to this patient | engineer | engineering | **Always urgent (U-4).** Also a privacy incident, not only a defect — escalate as one |
| **S12** | Working as intended, and the intent is wrong | clinician | taxonomy | A design argument, not a bug report. Treat it as one |
| **S99** | Not sure — something is wrong with this row | **both** | ambiguous | Deliberate escape hatch. Without it a physician picks the nearest wrong code and defeats the routing |

> **S8 was retired in policy v1.1.** Duplicate rows are a known, widespread limitation, and a
> picker option answered "we already know" every time teaches physicians that reporting is
> pointless. Expect duplicates to arrive as S99 or S10 instead. If that volume rises, the *guide*
> is failing — `HOW_TO_READ_THE_LEDGER.md` §8 says duplicates need not be reported.

### 3.1 S1 sub-classification — resolved for you, deterministically

Four indexed queries plus a source-note probe separate the five cases. **Walk forwards through
the pipeline, so the first stage that lost the fact is the one named** — reversing the order
attributes every loss to the last stage that could have carried it.

| Sub | Test | Meaning | Class | Cost |
|---|---|---|---|---|
| **S1a** | in a prior admission's notes, absent from `retention_facts` | b1 extraction miss | prompt | **Highest.** Prompt change + cohort-wide re-extraction (~1–2 days) |
| **S1b** | present in `retention_dropped` | pruned by the taxonomy gate | taxonomy | Cheap — `reason` names the branch; `retention_facts` need not be re-extracted |
| **S1c** | in `tiered_ledger_raw` but not `tiered_ledger` | absorbed by b4 consolidation | engineering | Cheap |
| **S1d** | in `tiered_ledger.fact_ids` | present after all | documentation | Not a miss. Display problem, or read past |
| **S1e** | only the **incoming** admission's note scores ≥ 0.6 | not folded in yet | documentation | **Not a defect.** Standing answer `by_design` |

Verdicts that are *not* retention failures and deliberately map to no sub-class:

- **`negated_only`** — the only mentions in the notes are negated. The notes deny it.
- **`not_in_source`** — no mention anywhere. The pipeline cannot retain what the record lacks.
- **`ambiguous`** — partial signal, or a similar fact *was* extracted. A human reads the notes.

**Probe limits, stated plainly.** It is token overlap, not a faithfulness judgement. It will miss
synonymy and paraphrase ("adrenal masses" for "adrenal nodules"). It errs toward `ambiguous`,
which routes to a human — the safe direction, since the costly error is wrongly exonerating the
pipeline. If a card reads `not_in_source` and the physician is adamant, read the notes yourself
before answering.

---

## 4. Triage workflow — what to do with a card

Open the board at `/board`. Lanes read left to right as a lifecycle. **Pick who you are acting as
(top right) first** — every action requires a named person, and the service refuses otherwise.

### 4.1 Work urgent first, and know why it is urgent

Urgent cards carry the rule that fired. Read it before assuming:

| Rule | Fires when |
|---|---|
| **U-1** | A T1 fact reported missing |
| **U-2** | Polarity inversion on a T1/T2 row |
| **U-3** | Disputed `current_lifecycle` at T1 — a superseded allergy shown active |
| **U-4** | Wrong patient |
| **U-5** | Same defect signature on 2+ distinct patients within 30 days at T1/T2 — systematic |
| **U-6** | The reporter states a clinical decision was made, or nearly made, on this row |
| **U-7** | Unsupported text on a T1/T2 row |

**Urgent means contain, not necessarily fix now.** The two are decoupled on purpose. Containment
for one patient is the Tier 3 pin-and-edit the physician already has; for a systematic row it is
a display suppression, the cheapest fix tier. The source fix still ships on the release cycle.
That is what closes the window between a flag and a taxonomy release during which wrong nodes
keep entering the c1 prompt.

### 4.2 Read the evidence before forming a view

The detail panel shows the collector's finding, the per-admission coverage table, and any
staleness warning. Two staleness signals, and the second catches more than the first:

- **regenerated since the report** — this patient's record has been rebuilt.
- **artifact behind code** — nothing about this patient changed, but the taxonomy in the code
  moved on. Common when the whole cohort is stale, and invisible if you only compare against
  `tiered_view`. Consider `already_fixed`, but **verify** — staleness means the defect *may* be
  gone, not that it is.

If evidence is missing or the card predates a collector improvement, use **re-collect evidence**.
Evidence is appended, never overwritten, so the card keeps the finding it was originally routed
on alongside the new one — which is how you tell "we were wrong then" from "the record changed
since".

### 4.3 Decide the class, then hand off

| If the evidence says | Class | Then |
|---|---|---|
| S1b, or S3/S6/S12 confirmed | clinical/taxonomy | Pipeline owner's queue. Versioned taxonomy release, not a per-patient edit |
| S1c, S5, S7, S9, S10 | engineering/routing | `REPAIR_RUNBOOK.md` §3 anomaly class, then §5B surgical repair |
| S1a confirmed | prompt wording | Most expensive path. Scope it first (§5) before committing |
| S1d, S1e, S6-by-design | documentation | Fix the **guide or the UI**, never by explaining it to one reporter |

For the repair itself, cross to `REPAIR_RUNBOOK.md`. Its anomaly classes map onto ours: our S1
is its class **A**, S5 its **B**, S3/S4 its **C**, S2/S9 its **D**, S7 partly its **G**.

### 4.4 Answer and close

Where the evidence settles it, the board pre-fills a disposition and a draft reply. **Edit it if
you disagree; verify it when it is marked amber.** No suggestion is offered on an ambiguous card
— a plausible draft there invites a rubber stamp, which is worse than a blank box.

| Disposition | Use when |
|---|---|
| `accepted_taxonomy` | Real, and the fix is a taxonomy release |
| `accepted_engineering` | Real, and the fix is code |
| `accepted_prompt` | Real, and the fix is the b1 prompt — say so, since it will not be quick |
| `by_design` | Working as intended. **Also change the guide or the UI** |
| `not_reproducible` | Could not confirm. Say what you checked |
| `already_fixed` | The reviewed record predates the fix |
| `duplicate` | Same defect as an existing card. Link it — it still gets a reply |

**Wording rules for the reply**, from the templates in `support/adapters/email_templates.py`:
lead with the verdict; say what to do about *this* patient now; never explain the pipeline
("not yet folded in" is a fact about the record, "b4 consolidation absorbed the node" is not the
reporter's problem); and thank them for a report that turns out not to be a defect, and mean it —
the alternative is a physician who stops reporting borderline cases, which is where the real
defects hide.

Closing queues the email. **Nothing is sent until the dispatcher runs** (§7).

---

## 5. Scoping a fix — the part a ticketing system cannot do

Before committing to a fix, find out how many patients carry it. This is the difference between
a complaint and a piece of scoped work.

- The card face shows **`n patients`** when a defect signature appears on more than one — that is
  the dedup key, and it is the U-5 systematic detector.
- The **metrics** panel lists every signature affecting more than one patient.
- For a taxonomy defect, `retention_dropped.reason` plus the dedup key resolve the affected set
  in one query. Re-extraction becomes proportional to prevalence, not cohort size.

**Never merge duplicate cards away.** Link them. The count of distinct patients carrying a
signature is the systematic signal; collapsing duplicates destroys exactly what the loop exists
to surface.

---

## 6. Changing the triage policy — governed like the taxonomy

`support/policy/triage_policy.yaml` holds symptoms, urgency rules, routing, states and
dispositions. Parameters live there; the logic for each `U-*` rule lives in `support/triage.py`,
and the two sets are asserted equal **at import** — a rule declared but unimplemented, or
implemented but undeclared, is fatal. The second is the dangerous direction: a gate firing that
nobody signed off on.

Procedure:

```
1. Edit a COPY of the policy file, with a new `version:`.
2. Replay it over every historical card and read the diff:
       python -m support.replay_policy --candidate <path>
       python -m support.replay_policy --candidate <path> --json out/replay_v1.3.json
   Exit status is 1 if any card would be decided differently, so this can gate a release.
3. Present the diff for sign-off. "We bumped the version" is ceremony; "here are the eleven
   past reports this would have decided differently" is a decision.
4. Adopt: replace the live file, append a CHANGELOG entry, re-run bootstrap to reproject
   `symptom_catalog`.
```

Replay is point-in-time correct: it reconstructs each card's dedup context **as of its own
`created_at`**, so U-5 does not retroactively fire on everything. To understand one decision:
`python -m support.replay_policy --explain <feedback_id>` prints every rule, fired or not.

**Schema changes need a migration.** `schema_support.sql` is fresh-creation form, so re-running it
against an existing database is a no-op — including for columns added to the file later. Add a
file under `support/migrations/`; `verify_schema()` fails the bootstrap if the live table is
behind. This is not theoretical: it once surfaced as an opaque HTTP 500 on a physician's send
button.

---

## 7. Delivery — the outbox

Nothing is sent inline. Every outbound effect is a row written in the same transaction as the
state change, drained separately under an idempotency key. So a report cannot fail because a mail
host is down, a crash cannot lose a reply, and a re-drain cannot double-send.

```
python -m support.dispatcher                    # drain once
python -m support.dispatcher --watch 60         # drain on a timer
python -m support.dispatcher --status           # what is queued, what is stuck
python -m support.dispatcher --digest engineer  # queue the weekly cc digest
python -m support.dispatcher --mqtt             # retained queue-depth nudge
```

**The default mail sink is `file`.** `LONGSUM_MAIL_SINK=file` writes `.eml` to `mail_out/` and
transmits nothing, while exercising the whole path — template, recipient resolution, idempotency,
outbox state. Set `smtp` only for a deliberate live send. A test cohort that emails a real person
is a defect with consequences.

| Outbox state | Meaning |
|---|---|
| `sent` | Delivered |
| `skipped` | **Correctly** not sent — no-op board adapter, or a reporter with no directory match. Not a failure; retrying cannot conjure an inbox |
| `pending` | Awaiting a drain, or retrying after a failure |
| `failed` | Gave up after 5 attempts. Investigate |

The **cc role gets a weekly digest.** 

---

## 8. Monitoring — is the loop actually working?

The failure this loop cannot survive is **quietly ceasing to answer**. Physicians do not file a
second report after an unanswered first, so a dead loop looks exactly like a loop with nothing to
do. Two checks, both following the `retention_visibility_gaps()` idiom — pure SQL, never raise,
empty when healthy:

- `service.loop_gaps()` — untriaged past 24h, unanswered past 14d, any urgent still open.
- `service.outbox_gaps()` — deliveries that never left.

Both surface in the board header as **loop gaps**, and at `GET /tiered/feedback/health`.

`python -m support.metrics` gives the rest. Each number has a reading that indicts something — a
dashboard that can only look good is decoration:

| Metric | What a bad reading means |
|---|---|
| `by_design` share rising | The **guide** is failing, not the pipeline. Physicians are reporting designed behaviour because nothing told them it was designed |
| urgent share rising | The **policy** is miscalibrated. If everything is urgent, nothing is |
| urgent closed as `by_design`/`not_reproducible`, by rule | That specific **gate** is over-firing. Readable only because every urgent card names its rule |
| S1e rate rising | The **UI** fails to distinguish the incoming admission from the established record |
| `unresolved` S1 share rising | The deterministic probes are losing ground; each card now costs a human trace |
| unmatched reporters | Replies are **undeliverable**. The board reads healthy and the physician hears nothing |
| closed with no email sent | Same, worse — the loop believes it answered |
| time to disposition rising | The backlog is growing faster than it is worked |

---

## 9. Gotchas quick-reference

- **`tiered_ledger.id` is not an anchor.** It is `SERIAL`, and `d3` deletes-then-reinserts per
  subject on every fold-in. Cards anchor on `fact_ids` (`sha1(source_hadm_id|text)[:16]`).
- **A report filed against a Completeness flag carries no `fact_id`** — only text and
  `source_hadm_id`. The collector reconstructs it, with a normalised-text fallback. Without that
  fallback, trivial whitespace differences would diagnose S1a — the most expensive disposition.
- **Substring search is a trap.** `%adrenal%` matches `hypoadrenalism`. The probe tokenises on
  word boundaries and screens negation for exactly this reason. If you grep by hand, do the same.
- **The cohort comes from `current_database()`, not `PGDATABASE`.** The API loads `.env` at
  import, so a shell without the override silently resolves to whatever `.env` names. A card
  stamped with the wrong cohort sends the collector to the wrong database — where the queries
  succeed and answer about a different patient population.
- **`.env` names the protected dev database.** Start the API with `PGDATABASE` set explicitly, or
  `database.pg` refuses the connection and every clinical endpoint 500s — including `/health`,
  which is the tell that the fault is the connection and not the feature.
- **An unmatched reporter still files.** Refusing would lose the observation. The reply is
  recorded on the card but not emailed, and the response says so. Watch the count.
- **Evidence is appended, never overwritten.** A card keeps the finding it was routed on. That is
  deliberate — it is how you distinguish a wrong call from a changed record.
- **Do not add a join from a pipeline stage to the support database.** It was proposed once, to
  put a feedback count in c5's MQTT digest, and rejected: it would give a clinical stage a new
  failure mode owned by a support system. The dispatcher publishes its own topic instead.
- **The board is a projection, not the record.** It holds no policy and cannot bypass the service.
  If a real workflow tool is ever adopted, write a `BoardAdapter`, change one line in
  `dispatcher.py`, and re-drain the outbox to backfill history — which works only because board
  rows are queued today even though `LocalBoardAdapter` discards them.
