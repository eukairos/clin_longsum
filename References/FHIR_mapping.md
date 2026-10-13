# CLIN_LONGSUM retention taxonomy mapping to ISO 27269 (International Patient Summary)

Reference only. Nothing in the pipeline imports or enforces this mapping; it exists for the
physician-facing documentation, for interoperability discussion, and to record where the local
model and the standard genuinely diverge. Companion to `src/fhir_interop.py`, which maps the
STATUS axes (`certainty`, `current_lifecycle`) to FHIR R5 `Condition` and is executable. This
document maps the CLASS axis to IPS document sections.

Mapped Taxonomy: `retention_taxonomy.v2.12` (12 retention classes, 5 salience tiers).

---

## 1. Source caveat -- read before citing

**ISO 27269 is paywalled and has not been read in full.** This mapping is assembled from the ISO
Online Browsing Platform preview, the HL7 FHIR IPS Implementation Guide, a European Commission
presentation on the standard, and national adaptations (NZ HISO 10099, India ABDM). Section names
and obligation levels are reliable. Per-attribute detail is indicative and MUST be verified
against the purchased standard before it appears in any published document.

Two version traps that will otherwise produce a wrong citation:

| | |
|---|---|
| **ISO 27269 vs the FHIR IG** | ISO 27269 makes three CLINICAL sections mandatory: Allergies and Intolerances, Medication Summary, Problems. The FHIR IPS IG derived from IPS 2.0.0 adds Immunizations, making four. |
| **Edition drift** | ISO 27269:2021 is the commonly cited edition; EN ISO 27269:2025 now exists. The 2021 conformance clause counts five mandatory IPS DATA BLOCKS plus one conditional on cross-border use - data blocks include administrative ones (Patient Attributes, Provenance), so that count is not the count of clinical sections |

---

## 2. Scope exclusion: Medications, Investigations, Procedures

**Excluded by design from the outset.** In most EMR systems these already exist as discrete, coded, directly
retrievable structured data. `clin_longsum` targets the UNSTRUCTURED PROSE of the Brief Hospital
Course, which is where longitudinally salient information is otherwise unrecoverable. Re-deriving
a medication list from narrative text would duplicate a structured feed at strictly lower fidelity.

Where such an item appears in the ledger, **it is there because of a
salience property, not because it belongs to that data block.** The class captures why it must
survive; it does not attempt to represent the item as a Medication, Result or Procedure entry. For example,

| ledger item | admitted under | admitted BECAUSE |
|---|---|---|
| Splenectomy | `anatomy_altering_surgery` | anatomy is permanently changed, altering future infection risk |
| Tacrolimus / active chemotherapy | `altered_immune_status` | persistent immunocompromise changes prophylaxis |
| "Colonoscopy normal 2024" | `transient_event` + `negative_kind=absent_or_negative` | a dated negative with care implications, not a result value |
| Prosthetic valve, pacemaker | `implanted_device` | the device constrains future care (MRI, anticoagulation) |

So a reader should NOT expect the ledger to be a source for medication reconciliation, results
review, or a procedure history. 

---

## 3. Retention class to IPS section

IPS obligation levels are the FHIR IG's (mandatory / recommended / optional). Confidence is this
document's, not the standard's.

| retention class | tier | IPS section | IPS obligation | confidence |
|---|---|---|---|---|
| `severe_adr_allergy` | T1 | Allergies and Intolerances | mandatory | high |
| `infection_control_status` | T1 | Problems, or Alerts | mandatory / optional | **ambiguous** |
| `code_status_directive` | T1 | Advance Directives | **optional** | high |
| `sentinel_event` | T2 | Problems (with sequelae) or History of Past Problems | mandatory / optional | splits |
| `implanted_device` | T2 | Medical Devices | recommended | high |
| `anatomy_altering_surgery` | T2 | History of Procedures | recommended | high |
| `permanent_condition` | T2 | Problems | mandatory | high |
| `altered_immune_status` | T2 | Problems; active chemotherapy -> Medication Summary | mandatory | splits |
| `transfusion_obstetric_history` | T2 | History of Pregnancy; alloantibodies -> Results | optional / recommended | splits |
| `chronic_disease_state` | T3 | Problems | mandatory | high |
| `recurrent_episode` | T4 | **none** (see 5.3) | -- | -- |
| `transient_event` | T5 | History of Past Problems; negatives -> Results | optional / recommended | splits |

"Splits" means one retention class distributes across more than one IPS section depending on the
instance. The mapping is many-to-many.

## 4. IPS sections with no retention class

| IPS section | obligation | status here |
|---|---|---|
| Medication Summary | mandatory | **out of scope** (section 2) |
| Immunizations | mandatory in IPS 2.0.0 FHIR IG | **out of scope** (structured data) |
| Results / Diagnostics | recommended | out of scope; captured only as dated NEGATIVES via `negative_kind` |
| History of Procedures | recommended | out of scope; captured only where anatomy-altering |
| Functional Status, Social History, Vital Signs, Plan of Care | optional | no class; not modelled |

---

## 5. Findings

### 5.1 Tier and IPS obligation are uncorrelated

`code_status_directive` is T1 -- the tier defined as "a clinician must see this BEFORE any clinical
action" -- and maps to Advance Directives, an OPTIONAL IPS section. `transient_event` is T5, the
prunable tier, and can map to Results, which is recommended. The orderings are not rank-compatible
in either direction.

This is not a defect in either scheme. They answer different questions:

* **IPS asks:** what must a cross-border summary CONTAIN?
* **The retention taxonomy asks:** what must SURVIVE to the next admission?

### 5.2 T4 has no IPS home

No IPS section represents an episode-count rollup. The nearest expressible form is a Problems entry
with FHIR `clinicalStatus = recurrence`. This is the same asymmetry already recorded in
`fhir_interop.UNMAPPED_FHIR`: FHIR carries `recurrence` and `relapse` as LIFECYCLE STATES of a
persisting condition, whereas `clin_longsum` models recurrence as a retention CLASS
(`recurrent_episode`) routed down a separate branch in `b2._project_patient` before candidacy is
consulted -- the mechanism behind FM-4.

### 5.3 The status axes map at ATTRIBUTE level, and IPS has a partial salience analogue

The 2025 edition's Allergies and Intolerances section carries clinical status of the condition,
onset date, end date, **criticality**, **certainty**, and type of propensity as fields. So
`certainty` and `current_lifecycle` have IPS counterparts as attributes, consistent with the
executable FHIR crosswalk in `src/fhir_interop.py`.

`criticality` is the closest thing IPS has to a salience weight - and it exists ONLY on allergies,
not as a general axis across sections. 

### 5.4 IPS independently supports CLIN_LONGSUM's handling of allergies across lifecycle.

IPS specifies that the allergies section should at minimum list currently active AND any relevant
historical allergies and adverse reactions - historical entries retained irrespective of current
status. That is T1 pin-forever behaviour written into a standard. CLIN_LONGSUM's handling of a de-labelled allergy (`severe_adr_allergy` +
`lifecycle=superseded` + `negative_kind=absent_or_negative`, served as an explicit negative so the
reader sees the de-labelling rather than nothing) is IPS-conformant behaviour.

---

## 6. Related

* `src/fhir_interop.py` -- executable crosswalk, `certainty` / `current_lifecycle` to FHIR R5
  `Condition`. Drift-guarded against the taxonomy at import.
* `References/clinical_salience_taxonomy.md` -- salience definition, literature position, novelty
  claim. Section "Revision 2026-08-08" carries the taxonomy-vs-standards comparison table.
* `References/TAXONOMY_EXTENSION.md` -- Shape F, why `certainty` / `lifecycle` values are mapped
  rather than renamed.
* `References/KNOWN_FAILURE_MODES.md` -- FM-4, the `recurrent_episode` routing behind 5.3.
