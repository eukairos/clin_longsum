# Production EHR ingest (`src/ingest/`)

A daily job that pulls newly signed discharge summaries from your EHR and feeds them into
clin_longsum, so they appear in the physician review queue and, once approved, fold into the
patient's longitudinal record.

**Status: prototype pattern.** The core has been tested end-to-end against a scratch database with
the production models (see [Testing](#testing)). The EHR adapter is a reference implementation only:
**you should write your own** for your EHR/ADT interface.

---

## What you change vs what you don't

| File | Role | Change it? |
|---|---|---|
| `ingest_source.py` | EHR adapter: fetches signed summaries | **Yes.** Write an `EHRSource` for your system |
| `run_ingest.py` | Daily driver, CLI | Scheduling/flags only |
| `ingest_derive.py` | Per-record decision + derived fields | No |
| `ingest_db.py` | Postgres reads/writes | No |
| `ingest_project.py` | DB -> cohort CSV projection | No |
| `ingest_surface.py` | Review-queue management | No |
| `ingest_schema.sql` | Ingest tables | Apply once; don't edit |

Everything after `fetch()` depends on clin_longsum's internal conventions (holdout mechanism,
cohort CSV format, fold-in order, review state). Changing it will change the pipeline.

---

## How it fits the pipeline

The pipeline stages read `cohort_admissions.csv`, not Postgres. The ingest therefore writes new
admissions to Postgres (source of truth) and then **appends** them to the CSV.

```
EHR --fetch--> admissions table (is_holdout=TRUE, no embedding)
     --project--> cohort_admissions.csv (append-only)
     --> patient's FIRST admission : auto fold-in (no review)
     --> otherwise                 : oldest pending admission shown for review; newer ones queued
physician approves in review UI --> existing fold-in (extract, ledger, compaction, embed, ...)
next daily run --> next queued admission (if any) shown for review
```

Per run (`run_ingest.py`):

0. **Guards:** holdout mode on; pandas parses ISO dates correctly; not a protected database; tables exist.
1. **Heal:** project any row a previous run wrote to the DB but failed to append to the CSV.
2. **Fetch:** records signed since the last successful run, minus a 3-day overlap.
3. **Plan + write:** each record becomes one of:
   - `insert`: new admission. `admit_seq` = patient max + 1; `bhc_delta` computed with the pipeline's own dedup.
   - `duplicate`: already stored, identical text. No-op (normal with the overlap window).
   - `amendment`: already stored, text changed. **Never applied**; logged for manual review.
   - `out_of_order`: admitted before the patient's latest stored admission. Logged for manual review.
   - `after_death`: patient already has an in-hospital death. Logged for manual review.
4. **Project:** append new rows to the cohort CSV (existing rows untouched, byte for byte).
5. **Auto fold-in:** a patient's first-ever admission is folded in directly (nothing to review against).
   A failed attempt is retried on every run.
6. **Surface:** each patient's oldest pending admission gets a draft summary and is loaded into the
   review queue; newer ones stay hidden until it is approved and folded in.

**Embedding is deliberately not done at ingest.** The fold-in embeds an admission only after
physician approval, so unreviewed text never enters RAG similarity search.

---

## Writing your adapter

Implement the `EHRSource` protocol in `ingest_source.py` and register it in `get_source()`:

```python
class MyEHRSource:
    name = "my_ehr"

    def fetch(self, since: datetime | None) -> tuple[list[EHRDischarge], list[dict]]:
        ...   # return (records signed after `since`, rejects as {"reason": ..., ...})
```

Your adapter **must** guarantee:

- **Signed/final summaries only.** ie not drafts.
- **Pipeline identifiers.** `subject_id` / `hadm_id` are integers owned by your mapping layer.
  Do not pass MRN, national ID or other direct identifiers into this database unless you're cleared to share patients' personal data.
- **ISO 8601 dates** (`2026-09-26T09:00:00`). The pipeline's cohort loader parses day-first, so any
  non-ISO date would be read with day and month swapped. `parse_row()` rejects non-ISO; reuse it.
- **`signed_at`** on every record. It drives the watermark.
- **One record per `hadm_id`**, latest signed version. `latest_per_hadm()` does this for you.
- `bhc_text` = the discharge summary's hospital-course narrative.

Return malformed rows as rejects rather than raising; they are logged to `ingest_log`.

The reference `CsvDropSource` reads `*.csv` from `LONGSUM_EHR_DROP_DIR` with columns:
`subject_id, hadm_id, admittime, dischtime, signed_at, bhc_text` (or `target`),
optional `service, admission_type, discharge_location, hospital_expire_flag`.

---

## Setup

0. Keep `HOLDOUT_LAST_ADMISSION = True` in `src/config.py`. Despite the name, it is the physician
   review gate: new admissions stay out of the record until approved. `run_ingest.py` refuses to run
   with it off. (False is only for unreviewed batch runs: no review queue, no fold-ins, and each
   patient's latest admission is never embedded.)
1. Pipeline database built as usual (`a4_load_and_embed.py`, then `src/database/schema.sql`).
2. Apply `src/ingest/ingest_schema.sql` in PgAdmin to the production database. Check the
   database you are connected to first: PgAdmin cannot see `LONGSUM_PROTECTED_DBS`.
3. Environment:
   - `PGDATABASE`: the production database
   - `LONGSUM_PROTECTED_DBS`: comma-separated databases no stage may write to (e.g. your
     reference or analytics copies). Every stage, the ingest included, refuses them unless
     `LONGSUM_ALLOW_PROTECTED_DB=1` is set for that one run. Default: `longitudinal_bhc`.
   - `LONGSUM_EHR_SOURCE`: your adapter's name (default `csv_drop`)
   - `LONGSUM_EHR_DROP_DIR`: for `csv_drop` only
4. Python env with **pandas 2.x**. pandas 3.x mis-parses ISO dates under the pipeline's loader, and the
   ingest refuses to run on it.
5. Rehearse: `python ingest/run_ingest.py --dry-run` (no writes, no model calls).
6. Schedule the daily run (Task Scheduler / cron) from `src/`:
   `python ingest/run_ingest.py`. Exit code 0 = ok, 1 = partial or error.

Run it on the inference host: steps 5-6 call the local LLMs (a first-admission fold-in takes several
minutes). Runs serialise with fold-ins through a Postgres advisory lock, so a physician approval
during the run just waits.

### Flags

| Flag | Use |
|---|---|
| `--dry-run` | Plan only |
| `--since 2026-09-01T00:00` | Override the watermark (backfill / replay) |
| `--overlap-days N` | Re-read window before the watermark (default 3) |
| `--skip-foldin` / `--skip-surface` | Ingest + project only; no model calls |
| `--source NAME` | Pick an adapter |

---

## Operations

**Manual review of anomalies:** nothing in the review UI; check Postgres:

```sql
SELECT * FROM ingest_amendments WHERE review_status = 'pending_review' ORDER BY detected_at;
```

Resolve by setting `review_status` (`resolved` / `dismissed`), `reviewed_by`, `reviewed_at`,
`review_notes`. Applying an amendment to the record is a clinical decision and a manual operation.

**Monitoring:**

```sql
SELECT run_id, status, started_at, n_fetched, n_rejected, n_inserted, error
FROM ingest_runs ORDER BY run_id DESC LIMIT 10;                 -- run history
SELECT action, count(*) FROM ingest_log WHERE run_id = <id> GROUP BY 1;  -- per-run decisions
SELECT * FROM ingest_log WHERE action IN ('rejected','auto_foldin_failed','inconsistent');
```

Alert on `status <> 'ok'`, and on repeated `auto_foldin_failed` for the same admission.

**Tables:**

| Table | Contents |
|---|---|
| `ingest_runs` | One row per run: watermark, counts, status |
| `ingest_log` | One row per record decision |
| `ingest_amendments` | Records withheld for manual review |
| `ingest_review_history` | Previous review decision archived when the next pending admission is shown |

### Things that will bite you

- **Do not run `a2_build_cohort.py` in production.** It rebuilds the cohort CSV from the batch
  source and would drop every ingested admission. It refuses while `interim/INGEST_ACTIVE` exists.
- **Never open the cohort CSVs in Excel.** It rewrites dates on save.
- **Review reset:** `tiered_view` holds one review decision per patient. When the next pending
  admission is shown, the ingest archives the previous decision to `ingest_review_history` and resets
  the row to `pending_review`. This is the only place the ingest writes physician review columns.
- **Known edge case:** a first admission from which no clinical facts can be extracted cannot fold in
  (the ledger stage has nothing to build). It is retried and logged as `auto_foldin_failed` each run.
- The DB and the cohort CSV must agree on each patient's admission set. If they diverge (e.g. manual
  edits), the run skips that patient (`inconsistent`) or stops; fix the divergence, then re-run.

---

## Testing

Offline (no database, no models):

```
cd src
python ingest/tests/test_ingest_offline.py
```

Tested 2026-09-27 against a scratch database with the production models:

- inserts and derived fields (`bhc_delta` matches the batch pipeline)
- duplicate, amendment, out-of-order and rejected-date handling
- byte-preserving CSV append; idempotent re-run
- auto fold-in of a first admission
- queueing of a newer admission behind a pending one
- full review cycle: approve -> fold-in -> next run shows the queued admission, review reset and archived

Not exercised live: retry of a failed auto fold-in.

Re-test with your adapter against a **scratch database and namespace** before production
(`PGDATABASE` + `LONGSUM_NAMESPACE`); never against a ledger-of-record database.
