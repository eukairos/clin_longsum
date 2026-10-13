# FORENSIC_AGENT_SETUP.md

How to install and wire the `longsum-forensics` skill -- the read-only forensic/FAQ agent for
this pipeline.

The skill itself lives at `clin_longsum/skills/longsum-forensics/SKILL.md` and ships with the
repo. Its knowledge base is this `References/` directory. Nothing about it is machine-specific
except the tool wiring below.

---

## 1. What it is

A read-only diagnostician. It answers "how did this fact get here / why did it disappear / why
is it labelled that" for a named `subject_id`, `hadm_id` or `fact_id`, and explains what the
pipeline's artifacts, flags and UI elements mean.

It does not edit pipeline files, write artifacts, or run pipeline stages. It proposes diffs;
the clinician rules.

Its method is six steps -- locate the fact per stage, diff against the source note, read the
routing code live, execute the deterministic functions, follow the patient's later course,
classify into exactly one defect class. The value is in running many small real queries against
real artifacts rather than reasoning from memory, so the tool access in §3 is not optional.

---

## 2. Install

The skill is a folder with a `SKILL.md` carrying YAML frontmatter (`name`, `description`).

**Claude.ai / Claude Desktop** - present or upload `SKILL.md` and use the *Save skill* button,
or copy the folder into the user skills directory. It then appears in `available_skills` and
triggers on its description.

**Claude Code** -- copy `skills/longsum-forensics/` into the skills directory Claude Code reads.

**Verify** it is live by asking something that should trigger it, e.g. *"why is a fact in
retention_facts.jsonl missing from the ledger for subject 90000001"*. If the skill does not
trigger, the description needs tightening rather than the body.

---

## 3. Tool access -- read-only, but real

The agent needs to actually run queries. An agent without these will hand-wave step 4, which is
the step that most often decides a trace.

### Filesystem read, project-scoped

`Windows-MCP` (preferred) or the `filesystem` MCP, scoped to your repo.
Needs read access to:

- `clin_longsum/src/**` -- the authoritative source tree, read live
- `clin_longsum/References/**` -- this knowledge base
- whichever cohort data root is in play (see §4)

Write access is not required and should not be granted. If the environment only offers combined
read/write, the read-only contract is enforced by the skill's guardrails and by review -- state
this limitation to the user rather than assuming it away.

### Postgres, read-only

`pg_query` only. No `pg_execute`, no DDL, no writes.

`longitudinal_bhc_test` is the dev ledger-of-record and MUST be read-only at the connection level --
not merely by convention. 

Confirm before first use:

```
pg_status   ->  expect  {"connected": true, "read_only": true}  when on longitudinal_bhc
```

Typical uses: confirming a column is a real `date` and not a locale string, checking whether a subject has physician edits, reading
`retention_dropped.reason` counts.

### Sandboxed Python, read-only

Conda env, with `clin_longsum/src` importable, so the agent can execute
`retention_taxonomy` and `b2_build_ledger` helpers on a fact's exact axes.

```powershell
cd ~\clin_longsum\src
$env:LONGSUM_DATA_ROOT = "<cohort root>"
$env:LONGSUM_NAMESPACE = "<namespace or empty>"
& "$env:USERPROFILE\anaconda3\envs\your-conda-env\python.exe" -c "import retention_taxonomy as tax; ..."
```

Note `conda run -n <your-env>` swallows stdout in some shells -- call the env's
`python.exe` directly.

The agent must not invoke a pipeline stage's `main()`. Importing a module and calling its pure
helpers is fine; running `b2_build_ledger.py` is not.

### grep across the tree

`Select-String` via PowerShell. Most traces begin here - a `fact_id` or a distinctive phrase
across every artifact in the data root.

---

## 4. Cohorts

The agent must establish which cohort a question is about before tracing. Files and databases
are separate per cohort; the namespace isolates FILES only.

| Cohort | File root | `LONGSUM_NAMESPACE` | Database |
|---|---|---|---|
|   gen (20 patients) | `Summarization\tiered_longsum\data_gen_prod` | `gen_prod` | `longitudinal_bhc_gen` |
| test | `Summarization\clin_test_data\data_test` | `test` | `longitudinal_bhc_test` |

`clin_longsum/data/` is empty by design -- the shipped repo carries no patient data. A run
always points `LONGSUM_DATA_ROOT` at one of the roots above.

**Schema drift matters here.**  `longitudinal_bhc_test` matches
`src/database/schema.sql`; See `CHANGELOG.md` §5.

---

## 5. Knowledge base

The skill reads these on demand. Keep them current and accurate.

| File | Contents | Update when |
|---|---|---|
| `FORENSIC_MAP.md` | every artifact and table: fields, key, writer, readers, write mode, join key; the traps | an artifact gains/loses a field, or a write mode changes |
| `KNOWN_FAILURE_MODES.md` | six traced mechanisms, each with signature, mechanism, how to confirm, when NOT to call it a defect | a new trace establishes a mechanism, or one is fixed |
| `GLOSSARY.md` | pipeline vocabulary | a new term enters the codebase |
| `TRACE_TEMPLATE.md` | the per-case form | the method changes |
| `CHANGELOG.md` | what is current; decisions not carried by a version string | any version bump or governance decision |
| `clinical_salience_taxonomy.md` | conceptual model + node-assembly mechanics | taxonomy or b2 assembly changes |

`src/` is deliberately NOT pre-loaded into the skill. It is large and churns; baking a
snapshot into the knowledge base would reintroduce exactly the "trusted stale memory of the
code" failure the method exists to prevent. The map tells the agent WHERE to look; the code
tells it WHAT happens.

---

## 6. Maintenance

The map and register are the parts that rot. Two habits keep them honest:

- When a pipeline change lands that alters an artifact's shape, a write mode, or a routing rule,
  update `FORENSIC_MAP.md` in the same session. The `CHANGELOG.md` §4 decisions list is the
  place for changes that carry no version string.
- When a trace closes, file it. A trace that closed as "by design" is worth keeping - it stops
  the same case being re-investigated. When a trace establishes a NEW mechanism, add a
  `KNOWN_FAILURE_MODES.md` entry and cross-reference the trace id both ways.

If the agent ever contradicts a reference doc after reading the live code, the code is right and
the doc needs fixing. That is the expected direction of correction, not a malfunction.
