# Support – physician feedback loop

Captures, triages, routes and answers physician reports about the Retention Ledger and prose
summaries served by the review UI. Runs against its own Postgres database (`longsum_support`),
separate from every clinical cohort database.

**Audience:** support engineers and clinicians who work the triage board. For the day-to-day
procedure, read `References/FEEDBACK_RUNBOOK.md` after this file. For what physicians were told,
read `References/HOW_TO_READ_THE_LEDGER.md` §8.

---

## 1. Intended function

`HOW_TO_READ_THE_LEDGER.md` §8 tells physicians to report ledger problems so they are fixed at
source, and promises a written answer to every report. This package makes that promise true.

A report goes through five steps:

1. **Capture** in the review UI. The physician picks a symptom (what they *see*, never which
   pipeline stage failed) and answers two yes/no questions of fact.
2. **Triage**, deterministic and inline: urgency, owner role, dedup signature.
3. **Evidence**, collected in the background from the clinical DB (read-only).
4. **Disposition** by a named clinician or engineer, with a written reply.
5. **Delivery** of the reply by email through an outbox.

What the package guarantees:

- Nothing reaches `closed` without a disposition and a non-empty reply (service check **and** DB
  `CHECK` constraint).
- Urgency is a floor. Rules raise it; only a named human lowers it, with a reason, logged.
- Every urgent card names the rule that fired.
- Dual-owned cards (S1, S99) cannot close until the second owner acknowledges.
- The audit trail (`feedback_events`) is append-only.

What it does **not** do: repair data (see `References/REPAIR_RUNBOOK.md`), change the taxonomy,
or modify any clinical table.

---

## 2. How it works

### 2.1 Layout

| Path | Role |
|---|---|
| `policy/triage_policy.yaml` | Governance object: symptoms, urgency rules U-1..U-7, S1 sub-classes, states, dispositions, notification. Versioned and clinically signed off. |
| `policy_loader.py` | Loads and validates the policy. Cached on (path, mtime). Named `policy_loader`, not `policy`: a `policy.py` beside the `policy/` directory is silently shadowed by it. |
| `triage.py` | One function per urgency rule. Parameters come from the YAML. Import fails if the two sets differ. |
| `service.py` | Domain core. Every state transition goes through here, one transaction each. |
| `evidence.py` | Evidence collector. The only module that touches both databases. |
| `replies.py` | Pre-writes a disposition and draft reply where the evidence settles the question. |
| `dispatcher.py` | Drains the outbox. The only caller of adapters. |
| `adapters/` | `base.py` (contracts), `mailer.py` (file / SMTP / none), `email_templates.py`, `local_board.py` (no-op board). |
| `metrics.py` | Loop-health and improvement metrics. |
| `replay_policy.py` | Replays a candidate policy over all historical cards and diffs decisions. |
| `schema_support.sql` | Fresh-creation DDL. |
| `migrations/` | The only way an existing DB gains a column. |
| `bootstrap_support_db.py` | Create DB, apply schema and migrations, verify columns, project the symptom catalog. |

HTTP lives outside this package: `api/feedback_routes.py` (`/tiered/feedback/*`, capture) and
`api/board_routes.py` + `api/board.html` (`/board`, triage). Both are mounted by
`api/longsum_api.py`.

### 2.2 Data model (`longsum_support`)

| Table | Holds |
|---|---|
| `feedback` | The card: reporter, symptom, report text, row snapshot, artifact snapshot, triage output, `state`, `disposition`, `reply_text`. |
| `feedback_events` | Append-only transition log. Never updated, never deleted. |
| `feedback_evidence` | Collector output (JSONB), versioned by `collector_version`. Appended, never overwritten. |
| `outbox` | Every outbound side effect (board, email, digest), with an idempotency key. |
| `physicians` | Staff directory: name, email, role (`clinician` / `engineer` / `admin`), notify mode. |
| `subject_alias` | `PT-nnnn` aliases. Board, email and digest use these, never `subject_id`. |
| `symptom_catalog` | Projection of the policy file for the UI picker. Replaced wholesale on each bootstrap. |
| `schema_migrations` | Applied migration filenames. |

`state` is lifecycle position (`new → triaged → in_progress / contained → resolved → closed`).
`disposition` is the verdict, set once on closing. They are separate so a board tool can be
swapped without remapping verdicts onto its lane names.

### 2.3 Triage is deterministic, with no LLM

Every decision is a boolean over captured fields or an indexed query. Consequences:

- A proposed policy change can be replayed over every historical card and diffed before adoption.
- Every decision is explainable (`python -m support.replay_policy --explain <id>`).
- No GPU contention with the fold-in cascade.

`triage.TriageEngine` is a seam: an LLM assist for paraphrase-level dedup could be added as a
second implementation without touching the core.

**Urgency means how fast the wrong output must stop reaching a reader.** It is not how wrong the
row is or how costly the fix is. Urgent cards are *contained* (Tier 3 pin-and-edit for one
patient, display suppression if systematic) while the source fix ships on the release cycle.
Physicians do not declare urgency; they answer `clinical_action_flag` and `seen_other_patients`.

| Rule | Fires when |
|---|---|
| U-1 | T1 fact reported missing |
| U-2 | Polarity inversion on a T1/T2 row |
| U-3 | Disputed `current_lifecycle` at T1 (e.g. superseded allergy shown active) |
| U-4 | Row belongs to another patient (also a privacy incident) |
| U-5 | Same defect signature on 2+ distinct subjects within 30 days at T1/T2 |
| U-6 | Reporter states a clinical decision was made or nearly made on the row |
| U-7 | Text not in the source notes on a T1/T2 row |

### 2.4 Evidence and the S1 decision tree

S1 ("a fact is missing") is the commonest and most ambiguous report. `evidence.py` resolves it by
walking the pipeline **forwards**, so the first stage that lost the fact is the one named:

| Sub | Test | Meaning | Class |
|---|---|---|---|
| S1a | absent from `retention_facts` | b1 extraction miss (most expensive) | prompt |
| S1b | in `retention_dropped` | pruned by taxonomy gate | taxonomy |
| S1c | in `tiered_ledger_raw`, not `tiered_ledger` | absorbed by b4 consolidation | engineering |
| S1d | in `tiered_ledger` | present after all | documentation |
| S1e | only the **incoming** admission's note scores >= 0.6 | not yet folded in; not a defect | documentation |

The collector also runs a source-note probe (word-boundary tokens plus negation screen; naive
substring matching treats `hypoadrenalism` as a hit for `adrenal`), checks the taxonomy, and
flags artifact staleness. It produces **evidence, not a verdict**. The probe is token overlap and
misses synonymy; it errs toward `ambiguous`, which routes to a human.

Where the evidence settles a card, `replies.py` pre-fills the disposition and reply. No draft is
offered on ambiguous cards, because a plausible draft invites a rubber stamp.

### 2.5 Outbox and delivery

Every outbound effect is an `outbox` row written in the same transaction as the state change,
then delivered by `dispatcher.py`. A dead mail host cannot fail a report, a crash cannot lose a
reply, and a re-drain cannot double-send. Adapters return `Delivery(ok, skipped, ...)`; they never
raise or touch the feedback tables.

**The default mail sink is `file`**: `.eml` files in `mail_out/`, nothing transmitted. Set
`LONGSUM_MAIL_SINK=smtp` only for a deliberate live send. Emails reference patients by alias only
and carry a distribution banner and an `X-Longsum-Sensitivity` header.

### 2.6 Governance

The policy file is governed like `retention_taxonomy.py`: version bump, CHANGELOG entry, clinical
sign-off. Engineering may propose; only the clinical owner ratifies, because "what counts as
urgent" is a clinical call. Replay (`python -m support.replay_policy --candidate <path>`) exits 1
if any card would be decided differently, so it can gate a release.

Symptom ids are never renumbered or reused. S8 is retired; the next new symptom is S13.

---

## 3. Why it is a separate database

Support state is ticket lifecycle, triage tags, staff identity and correspondence. It does not
belong in a patient-oriented clinical database. Concrete reasons, and what depends on each:

1. **Different subject, different audience.** Staff PII (names, emails) and workflow state should
   not sit alongside, or be backed up, retained or access-controlled with, patient data.
2. **No cross-database foreign keys, which is protective.** `d3_load_tiers.py` runs
   `DELETE FROM tiered_ledger WHERE subject_id = %s` on every fold-in. A cascading FK from a
   report to a ledger row would delete reports. `tiered_ledger.id` is `SERIAL` and changes on
   every fold-in, so it is useless as an anchor anyway. Cards anchor on `fact_ids[]`
   (`sha1(source_hadm_id|text)[:16]`) or, for Completeness-tab reports, on the retention-review
   natural key.
3. **Cards carry their own snapshot.** With no join back, everything needed to read a card months
   later is copied at capture: `taxonomy_version`, `prompt_version`, `artifact_generated_at`, the
   axes, `fact_ids`, `fact_text`. A large share of reports are against artifacts that predate a
   fix; without these a card cannot be dispositioned.
4. **Separate environment variable as a safety property.** Support connections use
   `PGDATABASE_SUPPORT`, never `PGDATABASE`. A shell that forgets `PGDATABASE` cannot write
   support rows into a clinical cohort. Do not import `database.pg.get_conn()` in support code.
5. **Independent failure.** The feedback loop is an addition to the review surface, not a
   dependency. The routers are mounted defensively and return 503 if the support DB is absent,
   un-bootstrapped or behind on migrations. Sign-off, Tier 3 edits and concern triage keep working.
6. **No pipeline stage reads it.** Adding a join from a clinical stage to support (proposed once,
   for a feedback count in c5's MQTT digest) was rejected: it would give a clinical stage a new
   failure mode owned by a support system. The dispatcher publishes its own topic instead.
7. **Removable and swappable.** The board is a projection, not the record. A workflow product
   (Planka, Jira, ServiceNow) can replace `LocalBoardAdapter` with one line in `dispatcher.py`
   plus a re-drain of the outbox.
8. **One support DB, many cohorts.** `cohort` on each card names the clinical DB it refers to.
   The cohort is resolved from `current_database()` at capture, not from `PGDATABASE`.

**The separation is not a privacy boundary.** `fact_text` and `report_text` are clinical text
copied out of the cohort. Under a data use agreement, apply access controls to the support DB
equivalent to the clinical one.

---

## 4. Setup and operation

Run from `src/` unless stated. Environment comes from `PROJECT_ROOT/.env`:

```
PGHOST  PGPORT  PGUSER  PGPASSWORD
PGDATABASE_SUPPORT            # default: longsum_support
LONGSUM_MAIL_SINK             # file (default) | smtp | none
LONGSUM_MAIL_DIR              # default: <project>/mail_out
SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASSWORD SMTP_FROM
```

```
python support/bootstrap_support_db.py --create-db     # first run (needs CREATEDB)
python support/bootstrap_support_db.py                 # schema + migrations + verify + policy
python support/bootstrap_support_db.py --seed-demo     # + synthetic staff (example.invalid)
python support/bootstrap_support_db.py --check         # report only, writes nothing
```

Day-to-day:

```
python -m support.dispatcher --status                  # queued / stuck
python -m support.dispatcher                           # drain once
python -m support.dispatcher --watch 60                # drain on a timer
python -m support.dispatcher --digest engineer         # queue weekly cc digest
python -m support.evidence <feedback_id> --attach      # (re)collect evidence, re-triage
python -m support.metrics --window 30
python -m support.replay_policy --explain <feedback_id>
```

Endpoints: `GET /tiered/feedback/health`, `POST /tiered/feedback/report`, `GET /board`.
Pick who you are acting as (top right of the board) first; every action needs a named person.

**Use `psycopg2` via these scripts, or `postgres_mcp`. Never `psql` from a shell MCP**: it prompts
for a password interactively and hangs the server. `CREATE DATABASE` cannot run in a
transaction, which is why bootstrap exists rather than a `.sql` file.

---

## 5. Orientation for support engineers

**Read first (in order):** this file → `FEEDBACK_RUNBOOK.md` → `triage_policy.yaml` header and
changelog → `service.py` docstring → `HOW_TO_READ_THE_LEDGER.md` §8.

**Invariants. Do not break these:**

- Schema changes need a file in `migrations/`. `schema_support.sql` is fresh-creation form, so
  re-running it on an existing DB is a no-op, including for new columns. `verify_schema()` fails
  bootstrap if the live DB is behind (`EXPECTED_COLUMNS`); update that dict with the migration.
  Skipping this once surfaced as an opaque HTTP 500 on a physician's send button.
- Never edit `feedback_events`. Corrections are new events.
- Never call an adapter or external system inline. Enqueue an outbox row.
- Never merge duplicate cards away. Link them. The distinct-patient count is the U-5 signal.
- Never add an urgency rule in `triage.py` without a YAML entry and sign-off (or the reverse).
- Never let a generated column or suggestion close a card. `replies.py` drafts; owners decide.
- Never put `subject_id` in an email or board payload. Use `alias_for()`.
- Evidence is appended. A card keeps the finding it was routed on, which distinguishes "we were
  wrong then" from "the record changed since".

**Gotchas:**

- `.env` names the protected dev database. Start the API with `PGDATABASE` set explicitly, or
  `database.pg` refuses the connection and every clinical endpoint 500s, including `/health`.
- An unmatched reporter (no directory match) still files, but the reply is recorded and not
  emailed. Watch the count in metrics.
- Outbox `skipped` is correct behaviour (no-op board, no address), not a failure. `failed` means
  5 attempts exhausted.
- Duplicate rows are a known limitation. They will arrive as S99 or S10; rising volume means the
  guide is failing, not the policy.
- A Completeness-tab report carries no `fact_id`. The collector reconstructs it with a
  normalised-text fallback; without it, whitespace differences would diagnose S1a.

**Loop health.** The failure this loop cannot survive is quietly ceasing to answer. Check
`service.loop_gaps()` (untriaged > 24 h, unanswered > 14 d, any urgent open) and
`service.outbox_gaps()` (stuck > 60 min), or the "loop gaps" badge on the board. Both never raise
and return empty when healthy.

**Metrics worth reading.** Each has a bad reading that indicts something:

| Metric | Bad reading means |
|---|---|
| `by_design` share rising | Guide is failing |
| Urgent share rising | Policy is miscalibrated |
| Urgent closed `by_design` / `not_reproducible`, by rule | That gate is over-firing |
| S1e rate rising | UI fails to separate the incoming admission from the established record |
| Unmatched reporters / closed with no email sent | Replies are undeliverable |

---

## 6. Orientation for clinicians

**Your job on the board** is to decide what is real and what it means clinically, and to make the
answer to the physician true and useful.

- **You own** S3, S4, S6, S12 cards (clinician-only) and share S1 / S99 with an engineer.
  Polarity (S4) is a clinical judgement first; if evidence shows a rendering fault, reassign to
  engineer (logged).
- **You ratify** triage policy and taxonomy changes. Engineering proposes; replay output shows the
  past reports a change would have decided differently. That diff is what you sign.
- **Urgent cards come first.** Read the named rule before assuming why. Urgent means *contain*
  (protect the patient in front of the physician now), not necessarily *fix now*.
- **Do not lower urgency lightly.** Wrongly holding a card costs attention; wrongly releasing one
  costs a patient. A lowering needs your name and a written reason.
- **`by_design` means change the guide or the UI**, not explain it to one reporter.
- **S1e is not a defect.** The ledger covers prior admissions only; a fact documented only in the
  admission under review is correctly absent until fold-in. Thank the reporter. A rising S1e rate
  is a finding about the interface.
- **Wording of replies.** Lead with the verdict; say what to do about this patient now; never
  explain the pipeline; thank the reporter even when it is not a defect. Unwelcome answers ("no
  change", "could not reproduce") decide whether a physician reports again.
- **Nothing closes without a written reply.** That is the product promise, not paperwork.

**Symptom picker (what physicians see):** S1 fact missing, S2 stated but not in notes, S3 wrong
label, S4 opposite of truth, S5 merged/split rows, S6 rollup too coarse, S7 date/order wrong,
S9 paragraph and ledger disagree, S10 display fault, S11 wrong patient, S12 intent is wrong,
S99 not sure. Authoritative source: `policy/triage_policy.yaml`.

---

## 7. Related documents

| Document | Use |
|---|---|
| `References/FEEDBACK_RUNBOOK.md` | Triage, answer, scope and close a card |
| `References/REPAIR_RUNBOOK.md` | Repair the data once the fault class is known |
| `References/HOW_TO_READ_THE_LEDGER.md` | What physicians were told, incl. §8 |
| `References/PIPELINE.md` | Stage inputs and outputs |
| `References/KNOWN_FAILURE_MODES.md` | FM-n references used in S5 etc. |
| `References/CHANGELOG.md` | Release history; policy changes are logged here too |

Licence: AGPL-3.0-only. See `LICENSING.md`.
