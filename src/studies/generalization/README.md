# Generalisation run (EVAL S11 redux, item 5)

External-validity capstone: run the FROZEN pipeline over a DISJOINT cohort of unseen
patients (no tuning) and compare dev vs unseen. A large gap is overfitting to the dev
cohort. This is a BETWEEN-patient holdout - distinct from e1's within-patient incoming
admission (the word "holdout" is reserved for the latter; this is the "generalisation"
namespace).

## How isolation works

`config.py` reads an opt-in `LONGSUM_NAMESPACE` env var. When set (e.g. `gen`), ALL file
artifacts live under `data_gen/` instead of `data/`, so the unseen run never clobbers the
dev artifacts. Unset (the default) is byte-identical to before.

**This namespaces FILES ONLY. The Postgres DB is shared.** Run ONLY the file-only stages
under the namespace. Do NOT run `a4_load_and_embed`, `db_persist_*`, `d3_load_tiers`,
`e2_embed_incoming`, or the API under the namespace -- they write the shared DB and would
mix dev + unseen rows in the same tables. f1/f4/f5 are file-based, so the eval needs no DB.

## Recipe (Windows)

```bat
cd ~\clin_longsum\src
set LONGSUM_NAMESPACE=gen
:: (PowerShell:  $env:LONGSUM_NAMESPACE="gen"  ;  to clear later:  set LONGSUM_NAMESPACE= )

:: 1. draw the disjoint unseen cohort into data_gen\interim\
python studies\generalization\draw_cohort_gen.py --n 16 --seed 2026

:: 2. frozen FILE-ONLY pipeline (same flags as PIPELINE.md "Full rebuild run order",
::    MINUS the DB stages a4 / db_persist_* / d3 / e2). Confirm each prints
::    "RUN NAMESPACE ACTIVE: 'gen' -> data_gen/".
python b1_retention_extractor.py --fresh
python b2_build_ledger.py --fresh
python b4_consolidate_ledger.py
python c1_compactor.py --fresh
python c3_faithfulness.py --fresh
python c4_correct.py
python c2_retention_validator.py --fresh
python d1_paragrapher.py --fresh
python d2_tldr.py --fresh
python d_coordinator.py --fresh

:: 3. score the unseen cohort (file-based; no DB)
python evaluation\f1_salience_recall.py
python evaluation\f4_faithfulness.py --fresh --units all
python evaluation\f5_pdsqi.py

:: 4. clear the namespace, then compare dev vs unseen (reads both data\ and data_gen\)
set LONGSUM_NAMESPACE=
python studies\generalization\compare_generalization.py --gen-namespace gen
```

Adjust the stage flags to match the exact invocations in `PIPELINE.md` if any differ for
your build; the only requirement is that the env var is set for steps 1-3 and that the DB
stages are omitted.

## Outputs

- `data_gen/output/eval/salience_recall.json`, `faithfulness.json`, `pdsqi10.*` -- unseen scores
- `studies/generalization/generalization_gap.json` + `FINDINGS.md` -- dev-vs-unseen gap

## Sign-off note

The `LONGSUM_NAMESPACE` mechanism is a one-time `config.py` change (opt-in; unset =
byte-identical). It is the only pipeline-wide change the generalisation run required.
If you prefer a different isolation approach (separate config, copied tree), say so and
this can be reworked -- nothing else in the redux depends on it.
