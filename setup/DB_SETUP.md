# CLIN-LONGSUM: Database setup

One PostgreSQL database (`longitudinal_bhc_test`) backs the pipeline. It is the
**source of truth**: after each stage, the clinical/audit records that must be
retained are persisted to the DB (not left only in the on-disk JSONL). Postgres
needs the **pgvector** extension (to create embeddings for future RAG retrieval).

Project policy: **DDL is applied manually in PgAdmin (or `psql`).** Apply the scripts in the order below.

> **Never open a pipeline CSV (source CSV, `cohort_admissions.csv`, or any
> `data*/interim`/`output` CSV) directly in Excel.** Excel auto-reformats on
> open/save: dates get rewritten (locale-dependent, ambiguous D/M vs M/D,
> seconds dropped), and any cell over its 32,767-character limit is silently
> truncated - for this pipeline that risk falls on `bhc_text`/`bhc_delta`. A
> resave can corrupt data upstream of every downstream fact, ledger, and
> metric without an error. If you need to eyeball a CSV, make a copy first and
> open the copy; never resave a working pipeline file from Excel.

---

## 1. Prerequisites

- PostgreSQL 14+ with the `pgvector` extension available.
- The Python environment installed (`pip install -r setup/requirements.txt`).
- Ollama running locally with the models pulled (see `requirements.txt` header).

## 2. Create the database and extension

```sql
CREATE DATABASE longitudinal_bhc_test;
\c longitudinal_bhc_test
CREATE EXTENSION IF NOT EXISTS vector;   -- pgvector, required by a4 / e2 / search
```

## 3. Configure `.env` (repo root)

Copy `.env.example` to `.env` at the repo root and fill in:

```
# Postgres connection (read by a4, d3, db_persist_*, e2, holdout.promote, the API)
PGHOST=localhost
PGPORT=5432
PGDATABASE=longitudinal_bhc_test
PGUSER=your_user
PGPASSWORD=your_password

# Credentialed BHC source CSV (patient-derived; NEVER committed). Field `target`
# holds the BHC text, one row per admission. Defaults to data/input/bhc_source.csv.
LONGSUM_SOURCE_CSV=C:/path/to/your/bhc_source.csv

# Optional: non-default Ollama endpoint (else Ollama's own default is used).
# OLLAMA_HOST=http://127.0.0.1:11434

# Optional: file-artifact namespace for a parallel cohort (e.g. the generalization
# run). UNSET = default data/. Set (e.g. gen) => data_gen/. NEVER set this for
# DB-writing stages (a4 / db_persist_* / d3 / e2 / the API) - the DB is not namespaced.
# LONGSUM_NAMESPACE=
```

## 4. Apply the schema (PgAdmin)

The base tables (`patients`, `admissions`) are created programmatically by
`a4_load_and_embed.py` (with `IF NOT EXISTS`), so run **a4 first**, then apply the
single schema file. It lives in `src/database/schema.sql`.

```
# Step 0: load base tables (creates patients + admissions + HNSW index)
cd src/database
python a4_load_and_embed.py

# Step 1: create every other table (tier / record / provenance / promotions /
#         retention-review / review-queue), in dependency order. Open
#         src/database/schema.sql in PgAdmin against your database and run it.
#   schema.sql
```

`schema.sql` is idempotent (`CREATE ... IF NOT EXISTS` throughout) and
non-destructive - re-running it never drops a table or wipes data, so it is safe
to run again if setup is interrupted. It must run **after** a4, because
`tiered_view` foreign-keys to `patients`.

> The file opens with a guard that raises an error if it is run against a database
> literally named `longitudinal_bhc_dev` (a placeholder for a separate dev DB you
> want to protect). It does **not** guard your configured `PGDATABASE` - edit the
> guard in `schema.sql` if you keep a dev database under a different name.


## 5. Table map (what each holds)

All tables below (except the base `patients`/`admissions`) are created by
`schema.sql`.

| Table(s) | Purpose |
|---|---|
| `patients`, `admissions` | shared base tables (created by a4_load_and_embed.py); `admissions.embedding` (pgvector) |
| `tiered_view`, `tiered_episode`, `tiered_ledger`, `tiered_ledger_raw` | served review surface (d3 loads them); `tiered_view` includes the review + approval columns (`review_status`, `approved_summary_md`, `summary_version`, `folded_in_at`) |
| `retention_facts`, `generation_audit` | source-of-truth facts + per-stage audit |
| `compaction_review`, `compaction_change` | C3 gate + C4 correction provenance |
| `promotions` | fold-in approval log (source of truth for promotions) |
| `retention_review` | C2 MISSING / PINNED_IN_ROLLUP re-review flags |
| `review_queue` | C5 clinician triage queue - one row per LIVE flagged concern (`review_id` = role+subject+admissions+score-magnitude fingerprint). c5 UPSERTs and preserves `review_status`; concerns the pipeline stops reproducing go `auto_cleared`; physicians resolve/dismiss via `POST /tiered/review-queue/resolve` |

## 6. Verify

Start the review service and hit `/health`:

```
cd src/api
uvicorn longsum_api:app --host 127.0.0.1 --port 8000
# GET http://127.0.0.1:8000/health  -> {"status":"ok","db":"connected", ...}
```

A green `/health` confirms DB connectivity and that `patients` / `admissions` /
`tiered_view` are reachable.
