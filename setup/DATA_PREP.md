# CLIN-LONGSUM: Preparing the source data (from MIMIC-IV + MIMIC-IV-Ext-BHC)

**JOB (one-time, per new deployment).** The pipeline runs against two patient-derived
input CSVs that you build from your PhysioNet-credentialed copies of MIMIC-IV and
MIMIC-IV-Ext-BHC. Everything downstream (`cohort_admissions.csv`, `cohort_patients.csv`, `cohort_holdout.csv`,
the ledgers, the DB rows) is **generated** by the pipeline.

You should hold current PhysioNet credentialing for both source datasets. Keep
these files under `data/` (gitignored); never commit them or any derived artifact.

---

## What you build

Both files go in the **same directory** as `config.SOURCE_CSV` - by default
`data/input/` (the loader reads `mimic_diagnoses_small.csv` from `SOURCE_CSV.parent`).
Point `LONGSUM_SOURCE_CSV` in `.env` at the BHC file.

### 1. BHC source CSV  (`config.SOURCE_CSV`, e.g. `data/input/bhc_source.csv`)

One row per admission. `a1_profile_cohort.py` / `a2_build_cohort.py` read these columns:

| Column | Source | Notes |
|---|---|---|
| `subject_id` | MIMIC-IV | patient id |
| `hadm_id` | MIMIC-IV | admission id (rows are de-duped on this) |
| `target` | MIMIC-IV-Ext-BHC | the Brief Hospital Course text (the summarization target). The column name is configurable via `config.SOURCE_TEXT_FIELD`, default `target` |
| `admittime` | MIMIC-IV `admissions` | parsed as a date; used to order admissions and assign `admit_seq` |
| `dischtime` | MIMIC-IV `admissions` | parsed as a date |
| `curr_service` | MIMIC-IV `services` | renamed to `service` downstream |
| `admission_type` | MIMIC-IV `admissions` | |
| `discharge_location` | MIMIC-IV `admissions` | |
| `hospital_expire_flag` | MIMIC-IV `admissions` | |

Build it by joining the MIMIC-IV-Ext-BHC brief-hospital-course text to the MIMIC-IV
`admissions` and `services` tables on `hadm_id`.

Date format note: `a2` parses `admittime`/`dischtime` with pandas. If your export is
day-first (D/M/YYYY), verify the parse (or write ISO `YYYY-MM-DD`) so `admit_seq`
orders correctly. Downstream stages order strictly on the numeric `admit_seq`, never
on the raw dates.

### 2. Diagnoses map  (`mimic_diagnoses_small.csv`, same directory)

One row per (admission, code). `a2` reads exactly three columns:

| Column | Source | Notes |
|---|---|---|
| `subject_id` | MIMIC-IV | patient id (already present - do NOT re-merge from the cohort) |
| `hadm_id` | MIMIC-IV `diagnoses_icd` | admission id |
| `snomed` | ICD->SNOMED map | numeric SNOMED code; non-numeric rows are dropped |

Build it from MIMIC-IV `diagnoses_icd`, mapping each ICD code to a SNOMED concept id
(e.g. via the UMLS/official ICD-10-CM -> SNOMED CT map). Used only to populate
`patients.conditions_json` for parity; if the file is absent, `a2` proceeds and leaves
`conditions_json` empty (a warning is printed).
- SNOMED CT mapping is optional for CLIN_LONGSUM; it is not done in our reference repo. You'll
need a SNOMED source, either from your organization, or you can obtain a license from UMLS.

---

## Then run (builds everything else)

```
cd src
python a1_profile_cohort.py          # profile: how many patients per admission-count band
python a2_build_cohort.py            # draw cohort -> data/interim/cohort_*.csv (+ bhc_delta, holdout)
cd database
python a4_load_and_embed.py          # load patients + admissions to Postgres, embed (pgvector)
```

`a2` is READ-ONLY on the source. After `a4`, apply
the DDL (see `DB_SETUP.md`) and continue with Phase B (`b1_retention_extractor.py`).

---

## Cohort-size knobs (optional)

`a2_build_cohort.py` samples a stratified cohort. To change size/shape, edit its
constants: `BANDS` (admission-count bands and how many patients per band), `CAP_MAX`
(max admissions per patient), `SEED` (reproducible draw). Defaults draw 100 patients
across two bands.
