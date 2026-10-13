### Definition of Salience

> In longitudinal patient-record summarization, **retention salience** of a **concept** is the
> expected cost to a **receiving** clinician, at some future encounter, of that concept being
> absent from the **summary**, **given everything else the summary already carries**.

This definition differs from the usual meaning of salience in patient record summarization which is generally 'belongs in this summary of this episode'. Our definition means 'must survive to the next encounter', thus we name it explicitly 'retention salience'.  

Previously published formulations of clinical salience in patient record summarization define and model salience using a proxy: note frequency (HARVEST), presence in a reference summary (CLINSUM), annotator consensus (Pyramid), harm severity (NOHARM), decoder attention (AgenticSum). In CLIN-LONGSUM we model clinical salience as a retention-oriented **class-based taxonomy**. The taxonomy assigns the *semantic class* and *lifecycle state* of an extracted clinical concept. In a longitudinal patient record context, this approach mimics the clinical reasoning of an experienced physician: it structures patient history by separating permanent, high-consequence physiological truths from transient events. We structure the taxonomy into five primary semantic tiers (Axis 1), refined by twelve retention classes (Axis 2) and modulated by four modifiers (Axis 3).

Prior work have described class-based persistence (US AHRQ's CCIR, Johns Hopkins ACG's EDCs), lifecycle and certainty (FHIR), content retention priority (ISO 27269). Our taxonomy's contribution is in **composing a persistence tier, a retention class, a lifecycle state and a certainty state into a single derived membership rule that decides what survives into a generated longitudinal summary**.


---

## The Clinical Salience Taxonomy


### Tier 1: Critical Operational Alerts & Severe Direct Toxins

This tier contains acute, life-threatening physiological events caused by external agents or idiosyncratic immune responses. These concepts carry an absolute premium on visibility because repeating the exposure can be fatal.

* **Subclasses:**
* **Severe Adverse Drug Reactions (ADRs):** Stevens-Johnson Syndrome (SJS), Toxic Epidermal Necrolysis (TEN), Drug Reaction with Eosinophilia and Systemic Symptoms (DRESS).
* **Anaphylactic Shock & True Allergies:** IgE-mediated cessation of airway/hemodynamics (e.g., Anaphylaxis to Penicillin).
* **Severe Procedural Complications:** Malignant hyperthermia during anesthesia, intraoperative air embolism.


* **Taxonomy Rationale:** These events represent a permanent alteration of the patient’s clinical contraindications. They are structurally *non-recurrent* if the health system functions correctly; therefore, a single historical instance maintains maximum salience for the rest of the patient's life.

### Tier 2: Permanent Pathological/Anatomical Alterations

Concepts in this tier define the permanent physical or genetic architecture of the patient. Once a node enters this state, it establishes a new lifelong baseline that modifies how all future acute symptoms are interpreted.

* **Subclasses:**
* **Major Organ Resections/Transplants:** Status post-transplant, left ventricular assist device (LVAD) implantation, total pneumonectomy.
* **Irreversible Genetic/Structural Diagnoses:** Cystic Fibrosis, Sickle Cell Disease, Type 1 Diabetes Mellitus.
* **Acquired Permanent Insults:** Paraplegia following spinal cord injury, post-stroke expressive aphasia.


* **Taxonomy Rationale:** These concepts modify the physiological baseline of the patient. For example, a future presentation of shortness of breath must be interpreted through the prism of a historical total pneumonectomy. They have high intrinsic salience because they act as permanent context-modifiers for future text generation.

### Tier 3: Chronic Progressive Conditions (Vulnerable Axis)

This tier houses long-term, incurable systemic diseases that fluctuate between stability and acute exacerbation. Rather than treating each encounter as a new event, the taxonomy classifies these as long-running continuous states.

* **Subclasses:**
* **Cardiopulmonary Syndromes:** COPD, Congestive Heart Failure (CHF), Pulmonary Hypertension.
* **Neurodegenerative/Metabolic Axes:** Chronic Kidney Disease (CKD) Stage IV, Alzheimer's Dementia, Atherosclerotic Cardiovascular Disease (ASCVD).
* **Active Malignancies:** Metastatic Adenocarcinoma, Glioblastoma (marked with an active vs. remission state property).


* **Taxonomy Rationale:** These conditions generate massive text volume ("note bloat") across a longitudinal record. Individual instances of these diagnoses are rolled up into a single parent tracking state. The *state* remains high-salience, but the *individual text notes* describing them are structurally compressed into trend data.

### Tier 4: Recurrent Acute Episodes & Utilization Markers

This tier captures discrete, time-bound events that may occur due to the underlying vulnerabilities in Tier 3, representing acute deviations from the chronic baseline; or frequent episodes of non-chronic conditions (eg recurrent UTI). In other taxonomies, this tier is often modeled as a lifecycle modulation of Tier 3 (FHIR, ISO 27269). We modeled this as a separate tier as not all recurrent episodes are necessarily of chronic conditions. 

During implementation, we found that this tier to be thinly populated, and facts extracted into it more prone to loss than those assigned to other tiers. This might because the tier encodes both salience decay and absorption, without distinguishing clearly between the two. In decay, a concept loses value as it recedes temporally - an effect captured also by Tier 5 (transient events). In absorption, a concept loses marginal value because it is absorbed into a retained parent (eg acute exacerbations of chronic disease) - also encoded by Tier 3. 

We are not the only researchers grappling with this - the US AHRQ's CCIR extension of ICD-10-CM attempted a four-way acute/chronic/both split in its 2021 version but reported difficulty distinguishing between these due to combination codes describing both, and reverted to a chronic/non-chronic/no-determination split in the 2023 version.

We may drop this tier in future taxonomy versions, following AHRQ's example, and absorb it following FHIR into a lifecycle state; or may split this tier into separate decay and absorption tiers if feasible. For now, we are retaining the tier to capture non-chronic recurrent events (bell-clapper testicular torsion, spontaneous pneumothoraces etc...) while awaiting more real-world data. 

* **Subclasses:**
* **Vulnerability Exacerbations:** 7th episode of COPD exacerbation, acute-on-chronic diastolic heart failure.
* **Cyclical Manifestations:** Gout flares, recurrent urinary tract infections (UTIs) in an neurogenic bladder, diabetic ketoacidosis (DKA) admissions.


* **Taxonomy Rationale:** Individual notes in this tier have **low independent salience** but **high aggregate salience**. The 7th episode of COPD tells a clinician very little as an isolated narrative, but when aggregated by the taxonomy, it yields a crucial utilization metric (e.g., *"Patient averages 2.3 hospitalizations per year for respiratory failure"*). This shifts the text processing from *narrative copying* to *statistical trend collapsing*.

### Tier 5: Transient, Self-Limiting Events (The Noise Layer)

This tier encompasses minor acute illnesses, symptoms, or administrative events that resolve completely without altering the patient's long-term physiological trajectory.

* **Subclasses:**
* **Self-Limiting Infections:** Acute viral rhinopharyngitis (common cold), uncomplicated gastroenteritis.
* **Transient Symptoms:** Isolated headache (resolved), localized muscle strain, benign positional vertigo.
* **Administrative/Operational Events:** Routine screening colonoscopy (with normal findings), social admission for placement.


* **Taxonomy Rationale:** This is the baseline noise of an EHR. Once the time delta exceeds the resolution window of the condition (e.g., 30 days after a cold), the salience score drops to zero. The ontology explicitly permits the summarization engine to drop these concepts entirely from a multi-year longitudinal timeline.

---

## Ontological Rule Engine for a Longitudinal Pipeline

When processing a patient's historical stream (an inpatient EHR corpus), the pipeline maps extracted facts/concepts to this taxonomy to execute structural rules during context compression:

| Taxonomy Tier | Temporal Rule | Context Selection Action |
| --- | --- | --- |
| **Tier 1: Critical Alert** | `Never Forget` | Bypass all chunking/merging trees. Pin directly to the primary, uncompressed context header indefinitely. |
| **Tier 2: Permanent Baseline** | `Carry Forward` | Maintain as active structural metadata in the patient profile. Do not re-summarize source text. |
| **Tier 3: Chronic Axis** | `State Tracking` | Group all related encounters into a single multi-year state. Extract only severe changes in severity or staging. |
| **Tier 4: Recurrent Episode** | `Graph Collapse` | Convert individual text narratives into a statistical tracking property (count, frequency, date ranges). |
| **Tier 5: Transient Noise** | `Prune Past T` | Drop from text representation if the encounter date is older than a specified threshold (>90 days). Facts carrying a `negative_kind` are still PINNED into the ledger regardless of age; of those, only `surveillance` also survives the serving-layer prune. See AXIS 1 below. |

By routing concepts through this taxonomy, you create a system where a single sentence about an SJS episode from 2006 is preserved with maximum resolution, while megabytes of text regarding repetitive COPD admissions are elegantly summarized into a single, highly informative metrics statement.

---

## Implemented model: three axes (`retention_taxonomy.py`)

The five tiers above are **AXIS 1** of the implemented taxonomy (`retention_taxonomy.py`, the governance object, currently `v2.12`). The running pipeline tags every extracted fact on **three** axes. Ledger membership (whether a fact is pinned into the must-retain ledger) is **derived** from the three axes by `is_ledger_member`.

### AXIS 1 — `salience_tier` (T1–T5)

As narrated above: T1 operational alert (pin) / T2 permanent baseline (carry forward) / T3 chronic axis (state track) / T4 recurrent episode (graph collapse) / T5 transient (prune past the window, unless carrying a `negative_kind`).

**The T5 clause is split across two stages, and the distinction matters.** "Unless carrying a `negative_kind`" governs the PIN decision, at B2: `is_ledger_member` admits any T5 fact with a non-null `negative_kind` into the ledger, and B2 applies no recency window at all -- the safer half, since nothing is lost at build time. The 90-day window is applied later and separately, at B4 (`ledger_consolidate._prune_t5`, `config.CONSOLIDATE_T5_PRUNE_DAYS`), as a **serving-layer** reduction of what a clinician reads. There the exemption is narrower: only `surveillance` survives.

| `negative_kind` | Pinned at B2? | Survives the B4 90-day prune? | Why |
| --- | --- | --- | --- |
| `surveillance` | yes | **yes - exempt** | an unconfirmed finding still being watched; ageing it out drops the only standing record that something needs a repeat scan |
| `absent_or_negative` | yes | no | a dated screening/workup negative is a historical statement; past the window it is stale, not wrong |
| `resolved_relevant` | yes | no | a resolved event with genuinely lifetime weight should be carrying a T2 class (`sentinel_event`, `permanent_condition`, `transfusion_obstetric_history`), not sitting at T5 |
| `null` | no | n/a | declined at B2 with `prune_reason = not_pinned_tier` |

Nothing pruned at B4 is lost: the pre-consolidation ledger is preserved at `retention_ledger.raw.jsonl`, and C2 validates recall against that file rather than the served one, so the reduction does not distort the recall metric. The exempt set is `ledger_consolidate.T5_PRUNE_EXEMPT_KINDS`; widening it is a clinical call.

> Cohort evidence (100-patient test cohort): 474 T5 nodes, **all 474** carrying a `negative_kind` - necessarily, since at T5 the `negative_kind` branch is the only route through `is_ledger_member` (the `confirmed` and `suspected` paths are gated to T1-T3, and `floor_tier` keeps the safety classes at T1/T2). Before the exemption, the B4 prune took 364 of them on date alone, including 8 `surveillance` findings.

### AXIS 2 — `retention_class` (12 classes)

Each fact is assigned exactly one class. The class sets a **default tier** (which the extractor may escalate for safety), and the ledger groups facts by class. The 12 classes:

| Class | Default tier | What it captures | NOT (counter-example) |
| --- | --- | --- | --- |
| `severe_adr_allergy` | T1 | Life-/organ-threatening drug or substance reactions; IgE anaphylaxis; malignant hyperthermia. Repeating the exposure could be fatal. | Transient nausea on metronidazole; an allergy de-labelled on challenge (→ superseded). |
| `infection_control_status` | T1 | Multi-drug-resistant organisms and notifiable infections with isolation / antibiotic implications (MRSA, CRE, active TB, VRE). | Uncomplicated sensitive *E. coli* UTI, treated (→ transient_event). |
| `code_status_directive` | T1 | Resuscitation status, ceiling of care, advance directives, transfusion/treatment refusals. LATEST-WINS; often patient-reported and still binding. | Full code with no special directive. |
| `sentinel_event` | T2 | A major acute event with lasting prognostic weight (stroke, ICH, cardiac arrest, ventilated ICU admission, major haemorrhage, VTE/PE). A future clinician must know it happened. | Single uncomplicated ward admission; overnight observation. |
| `implanted_device` | T2 | Permanent implants with lifetime management implications (pacemaker/ICD, prosthetic valve, coronary stents, dialysis access). Escalate to T1 if safety-critical (e.g. MRI). | Temporary urinary catheter; PICC line. |
| `anatomy_altering_surgery` | T2 | Procedures that permanently change anatomy or remove an organ (splenectomy, bowel resection/colostomy, transplant, amputation). | Diagnostic endoscopy with no resection; incision and drainage. |
| `permanent_condition` | T2 | Genetic, structural, or acquired irreversible states that reset the baseline (cystic fibrosis, sickle cell, T1DM, paraplegia, post-stroke deficit). | Reversible AKI that resolved (→ transient_event). |
| `altered_immune_status` | T2 | Persistent immunocompromise or asplenia altering infection risk / prophylaxis (HIV, asplenia, transplant immunosuppression, active chemotherapy). | Short completed prednisolone course. |
| `transfusion_obstetric_history` | T2 | Permanent relevance to cross-matching, alloimmunisation, or future pregnancy (red-cell alloantibodies, prior transfusion reaction, previous caesarean, PPH). | Uncomplicated vaginal delivery with no sequelae. |
| `chronic_disease_state` | T3 | Long-term incurable systemic disease tracked as a continuous state (COPD, CHF, CKD/ESRD, metastatic malignancy, IBD, epilepsy, AF, cirrhosis). The INDEX diagnosis establishes the state node. | The 7th COPD exacerbation (→ recurrent_episode). |
| `recurrent_episode` | T4 | Discrete, time-bound exacerbation of a chronic (T3) parent; low individual, high aggregate salience. `parent_condition` is set; collapses to a frequency metric. | FIRST presentation of a chronic disease (→ chronic_disease_state, occurrence=index); malignancy recurrence/relapse/progression or graft/organ rejection (→ sentinel_event). |
| `transient_event` | T5 | Minor acute illness, symptom, lab abnormality, or administrative event that resolves. Prunable past the recency window UNLESS it carries a `negative_kind` -- this exempts it from the B2 PIN decision; at the B4 serving prune only `negative_kind=surveillance` is exempt (see AXIS 1). | Normal screening colonoscopy with date (→ negative_kind=absent_or_negative); incidental lung nodule (→ negative_kind=surveillance + certainty=suspected). |

Two deterministic safety nets re-route already-extracted facts at load (B2/B3/C1), independent of the extractor: `is_disease_course_event` promotes a malignancy recurrence/relapse/progression or graft/organ rejection mis-tagged as `recurrent_episode` to `sentinel_event` (T2); `is_sentinel_episode` does the same for status epilepticus, gangrene, and graft/bypass/stent occlusion or thrombosis. If you intend to add further conditions into the safety nets, `TAXONOMY_EXTENSION.md ` is the runbook to refer to.

### AXIS 3 — status modifiers

Four per-fact modifiers refine the tier/class and drive the ledger-membership decision:

| Modifier | Values | Role |
| --- | --- | --- |
| `certainty` | `confirmed` \| `suspected` \| `ruled_out` \| `patient_reported` | `ruled_out` is never pinned (kept as a negative in `considered_excluded`); `suspected + active` (T1–T3) pins as a PROVISIONAL open problem; `patient_reported` pins for allergy / code-status / infection-control (the patient is often the only source). |
| `lifecycle` | `active` \| `resolved` \| `in_remission` \| `superseded` | `superseded` is never pinned. Note it is the odd value out: `active` / `resolved` / `in_remission` describe the PATIENT, whereas `superseded` describes the RECORD (the fact was wrong or has been replaced -- a de-labelled allergy, an overwritten code status). It therefore gets its own hard guard in `is_ledger_member` and its own ledger bucket (`amended`). Otherwise `lifecycle` is consulted only on the `suspected` path: `suspected + active` pins, `suspected + resolved / in_remission` does not (except `sentinel_event`, v2.10). |
| `occurrence` | `index` \| `recurrence` | `index` = first time the condition is seen across this patient's history; distinguishes a first presentation (T3 state) from a later flare (T4 episode). |
| `negative_kind` | `null` \| `absent_or_negative` \| `resolved_relevant` \| `surveillance` | A clinically important negative / resolved-event / surveillance finding. Any non-null value PINS the fact (it must not silently drop). See below. |

#### `negative_kind` — disambiguating the old `significant_negative`

A single boolean `significant_negative` carried four incompatible meanings, which caused a real historical event (e.g. a resolved provoked PE) to render identically to a documented absence ("PE absent") — a clinician-facing display-safety risk and a source of false contradictions in evaluation. v2.9 replaces the boolean with an enum so the **sense** is explicit and set at the SOURCE (B1 extraction), where the text actually disambiguates it:

* `null` — an ordinary positive, currently-present condition (not a significant negative).
* `absent_or_negative` — a documented ABSENCE or a screening/workup negative: the patient does NOT (currently) have X. e.g. "no known drug allergies", "HIV negative", "colonoscopy normal 2024", "EGD ruled out varices". The verbatim finding text is preserved so the exclusion reads true.
* `resolved_relevant` — a POSITIVE event the patient DID have, now resolved, but with lasting prognostic weight: provoked VTE/PE, first unprovoked seizure, significant fracture. **This is not an absence — the event happened.**
* `surveillance` — an unconfirmed finding being watched: incidental nodule/mass/indeterminate cyst, a lesion "concerning for" malignancy, a rising tumour marker. Set deterministically by `is_surveillance_finding` (with `certainty=suspected`) so a ?malignancy-under-surveillance cannot drop on a prompt miss.

When B2/B4 consolidation merges same-condition facts carrying different kinds, `merge_negative_kind` resolves the node's kind by priority **`null` (present positive) > `resolved_relevant` > `surveillance` > `absent_or_negative`** -- a present-positive sibling is never masked by an "absent" sibling, and a real resolved event outranks a surveillance finding, which outranks a plain absence.

#### Pruning is audited

Ledger membership is derived, so a fact that is not pinned simply does not appear in any ledger array. A confirmed T5 `transient_event` with no `negative_kind` matched none of B2's routing branches -- not `ruled_out`, not `superseded`, not `recurrent_episode`, not a ledger member -- and fell out of the chain into no list at all, with no counter and no log line. Absence from every array is indistinguishable from a fact that was never extracted, which makes a genuine mis-label impossible to tell from a correct prune. Thus `prune_reason()` was added. This which names WHICH branch of `is_ledger_member` declined the fact. B2 records each declined fact in a `dropped[]` array on the ledger, carrying its axes, its `fact_id`, its source admission and that reason; D3 persists it to the `retention_dropped` table. Reasons:

| Reason | Meaning |
| --- | --- |
| `not_pinned_tier` | confirmed, but T4/T5 with no `negative_kind` -- the ordinary transient prune |
| `suspected_inactive` | `suspected` + `resolved` / `in_remission`, outside `sentinel_event` |
| `patient_reported_other_class` | `patient_reported` outside allergy / code-status / infection-control |
| `ruled_out`, `superseded` | reachable from the helper, though B2's own chain catches both earlier |
| `unclassified` | the gate declined for a combination none of the above names -- worth investigating |

This changes nothing about what is pinned or served: `is_ledger_member` is untouched, so no re-extraction is required. It makes an existing, usually correct, decision visible.

Three limits worth stating. 
1. The `dropped[]` array is **audit only** -- never served, never counted in C2 recall. 
2. It does not cover the separate B4 T5 recency prune (`CONSOLIDATE_T5_PRUNE_DAYS`), which removes nodes from the served ledger; those remain recoverable from `retention_ledger.raw.jsonl`, whereas a B2 fall-through is recoverable only from `retention_facts.jsonl`.
3. `dropped[]` does not cover facts that B2 **admitted** and then routed to `amended`. Those were never declined by the gate, so no `prune_reason` fires -- yet `amended` is not loaded to the database and IS counted as retained by C2, so such a fact is absent from the served ledger with no trace anywhere. See the next section.

#### Superseded does not un-pin a safety class 

`superseded` is a statement about the **record**, not about the patient: the earlier assertion has been withdrawn or replaced. It is distinct from `resolved` (the condition occurred and ended; the earlier record is still true) and from `ruled_out` (considered, then disproven; never true). **A de-labelled allergy is SERVED as an explicit negative** (`negative_kind=absent_or_negative`) rather than disappearing. The reader sees "penicillin allergy - de-labelled on challenge" instead of nothing, which is the safer default when the mechanism producing the silence has a demonstrated false-positive rate. This is also how ISO 27269 handles allergies.

One consequence for node assembly: when the latest mention supersedes, it now supplies `negative_kind` outright instead of going through merge priority. Merge priority ranks `None` (condition present) highest, which is right in general and wrong for a retraction -- a de-label clustered with the original allergy would otherwise resolve to `None`, lose its "absent" marker, and read as a live allergy.

---

## How a ledger node is assembled

**This section describes B2 mechanics, not taxonomy rules.** The taxonomy decides whether a fact is pinned; `b2_build_ledger.py` decides how the pinned facts become the entries a clinician reads. Changes here carry no `TAXONOMY_VERSION` bump, but they change what is displayed, so they are documented alongside the axes they consume.

A patient mentions the same condition across several admissions. Those mentions are grouped and collapsed into ONE node, twice over:

1. **Clustering** (`_cluster`, within a single `retention_class`) - overlap coefficient on content tokens, requiring at least one shared token that is not generic boilerplate. Two distinct conditions phrased "Chronic and stable X" and "Chronic and stable Y" share only `{chronic, stable}` and must not chain.
2. **Consolidation** (`_consolidate`) -- a second merge ACROSS clusters, restricted to a compatibility group (diseases together; procedures and devices together; every other class only with itself), and only on Jaccard >= 0.6, a curated condition key, or verbatim phrase containment. The group restriction exists because an ungated merge once buried an active hepatocellular carcinoma inside a transplant-history node.
3. **Identity guard** (`_split_cluster` after step 1, `_split_group` after step 2) - neither grouping step may produce an entry whose displayed text names a condition that some of its own members do not share. A member that shares no identity token (content tokens minus qualifier boilerplate) with the entry's chosen wording is re-grouped around its own wording instead.

   The guard exists because both grouping steps merge on a MENTION of a condition. A COMPOUND fact naming two conditions -- "Coronary artery disease and peripheral vascular disease", "Chronic thrombocytopenia likely secondary to cirrhosis" - matches both, bridges them into one group, and the latest-wins wording rule then lets one condition speak for the other. The absorbed condition is not pruned, so no `dropped[]` entry and no `prune_reason` fires: its `fact_id` stays inside the node while its identity disappears. The guard can only ever INCREASE the entry count -- it creates no merge that clustering or consolidation did not already make - so it cannot introduce a new swallow. 

   **`code_status_directive` is exempt** (`_SPLIT_EXEMPT`). Unlike an allergy, a code-status directive legitimately mutates over time, and the most recent statement is the one that reflects the patient's wishes - so latest-wins collapse is correct there, and splitting would surface every superseded directive as its own entry. 

### Split provenance is deliberate

The resulting node does not take all its attributes from one mention:

| Attribute | Taken from | Why |
| --- | --- | --- |
| `salience_tier` | the STRONGEST mention across the cluster | a chronic condition mis-tagged T4/T5 at a later mention must not fall out of the ledger |
| `text`, `retention_class`, `certainty`, `current_lifecycle` | the LATEST mention | the node describes CURRENT state |
| `negative_kind` | the merge-priority winner (`merge_negative_kind`) | a present-positive sibling must never be masked by an "absent" sibling |
| `onset_date` / `last_confirmed_date` | earliest / latest across the cluster | the span, not a single mention |

The tier rule and the latest-wins rule are each defensible alone. In combination they mean a node can be displayed at a stronger tier than the mention its wording came from - which is correct for safety and confusing for a reader trying to work out where the entry came from.

So the node records **per-attribute provenance**: a map from attribute to the `fact_id` that supplied it. `fact_ids[]` says which facts went into the node; `provenance` says which fact is responsible for each thing you can see. Audit only - nothing routes or displays on it, and it needs no re-extraction, being derived from `fact_id`s already stored. 

The tier is attributed to the most RECENT mention holding that tier, not the earliest, so the trail points at a live justification.

### Which mention's wording survives

When consolidation collapses several nodes, the representative is chosen by strongest tier, then preferring `confirmed` over provisional, then **most recent**. 

Note the `confirmed`-preferring step has a clinical consequence worth being aware of: if "possible HCC" and "HCC" cluster together, the merged node is confirmed and carries the confirmed sibling's wording. That is usually right, but it means the provisional marker can change as a side effect of a merge.


---


> Governance: any edit to the implemented taxonomy bumps `TAXONOMY_VERSION` and appends to `CHANGELOG` in `retention_taxonomy.py`. This document is the conceptual companion; `retention_taxonomy.py` is authoritative. Node-assembly mechanics above live in `b2_build_ledger.py` and are versioned only by the repo history.
