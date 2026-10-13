# How to Read the Retention Ledger

A guide for physicians using the review interface.

---

## 1. What the ledger is

When a patient has many prior admissions, the older ones are compressed into a single
narrative paragraph (**Prior Admissions (compacted)**). Compression loses detail. 
This loss is mitigated by the **Retention Ledger** - an independent, structured list of the facts
that were judged too important to lose, extracted directly from the source discharge summaries
before any compression happened. It is built by a separate process from the one that writes
the prose. 

- **The ledger is not a problem list.** It is not curated, it is not deduplicated
  against your own clinical judgement, and it does not claim to be complete. 
- **The ledger is read-only.** You cannot edit or delete rows. Section 9 explains why, and what
  to do instead when a row is wrong.
- **The ledger covers PRIOR admissions only.** It is the established record. Anything documented
  for the first time in the admission you are reviewing now is not in it yet, and appears once
  that admission is approved and folded in.

---

## 2. How the ledger was built

The ledger is built from the **discharge summaries alone**. It does not read the EMR
problem list, the medication list, or investigations. Each admission is processed on its
own, in four steps.

**1. Each note is broken into single-idea facts.** A large language model (LLM) reads one discharge
summary at a time and emits one fact per clinical idea, so a compound phrase
("COPD/asthma", "wound infection secondary to endocarditis") becomes separate facts. Each fact 
is tagged on fixed axes - kind of fact, certainty, lifecycle, first presentation vs
recurrence - chosen from closed vocabularies, so a category cannot be invented. The model
is instructed to take only what the note states: it does not infer an unnamed diagnosis
from a drug or a complication, and it never fills in a de-identification blank ("___").
It then re-reads its own output against the note, looking specifically for invented facts,
missed Tier 1 (see Section 5) alerts and mis-set certainty, and corrects them.

**2. What to keep is decided by rule, not by judgement.** Retained facts are selected by a
fixed, versioned rule set - the retention taxonomy - which assigns the tiers in §5. It is
deterministic: the same fact always produces the same decision, and no LLM takes part.
Safety-critical kinds, severe drug reactions and code status, are pinned regardless of
tier, certainty or lifecycle, which is why a suspected or resolved anaphylaxis still sits
at T1. Facts that are not kept are not discarded silently - each is recorded with the rule
that declined it, so "why is X not here" has an answer.

**3. Mentions are grouped into one row per condition.** A condition documented at six
admissions becomes one row with n=6, its onset date, and the date it was last confirmed;
the most recent mention sets the current status. Grouping is deliberately conservative.
Entries merge only within the same kind of fact and only when they share specific wording
- two conditions sharing nothing but boilerplate ("chronic", "stable") are never joined,
and a phrase naming two conditions is split so that one cannot speak for the other. A
second, model-assisted pass then rolls repeated flares up into their parent chronic entry
(§5, T4) and drops stale transients. Nothing is merged across a lifecycle boundary, so an
active entry and a resolved one stay apart.

**4. The wording stays as documented.** T1 and T2 entries pass through untouched, and fact
text is taken from the source note rather than rephrased.

Every row carries the pipeline, prompt and taxonomy version that produced it, so a report about a
row can be answered against the exact code that built it.

**Two consequences to note for the rest of this guide:**

- **A wrong row is usually a rule behaving as written, not a one-off slip.** That is why
  rows are read-only, and why reporting one is worth your time - any fix will apply to every
  patient (§9).
- **Extraction is a model reading prose, so it can miss things.** The ledger is a
  retention aid with an audit trail, not a complete or curated problem list (§1).

---

## 3. Where it is on screen

Open a patient and scroll to **Retention Ledger**. It has two parts:

| Section | What it holds |
|---|---|
| Main table | Confirmed facts |
| **Flagged - provisional** | Facts that are not confirmed. Shaded, listed separately. See §6 |

The count beside the section heading is the total of both.

Above it you will usually see **Recent Admissions** (the most recent admissions, shown in full,
uncompressed) and **Prior Admissions (compacted)** (the compressed paragraph, which you *can*
edit).

---

## 4. Reading a row

| Column | Meaning |
|---|---|
| **Tier** | How important the fact is, and how it is meant to be carried forward. See §5 |
| **Fact** | The retained text, taken from the source note. May carry a coloured badge (§7) |
| **Class** | What kind of fact it is (allergy, device, chronic disease, and so on) |
| **Cert.** | How certain the source note was: confirmed, suspected, or patient-reported |
| **Lifecycle** | Whether it is currently active, resolved, in remission, or superseded (§8) |
| **Onset** | When it first appeared in this patient's record |
| **Last** | When it was last confirmed in a note |
| **n** | How many separate admissions mentioned it |

**A note on `n`.** A high count means the fact was repeatedly documented, which is usually
reassuring. A count of 1 does not mean the fact is doubtful, only that it was stated once. Some
of the most important entries (an anaphylaxis, a resuscitation directive) appear exactly once.

**Rows are ordered by tier first, then chronologically within each tier.** So read top-down:
the things you must know before acting come first.

---

## 5. The tiers

### T1 - Operational alert
**A clinician must see this before any clinical action.**

Severe drug reactions and true allergies, infection-control status (MRSA, CRE, active TB), and
code status or goals of care. These are pinned permanently and shown verbatim.

T1 entries are deliberately hard to remove. A severe drug reaction stays at T1 even if the note
recorded it as resolved or only suspected, because repeating the exposure could be fatal and
the cost of wrongly dropping it is far higher than the cost of wrongly keeping it. The same
applies to a resuscitation directive. **Read T1 as: act on this, or confirm it is wrong before
acting against it.**

### T2 - Permanent baseline
**Irreversible anatomy, genetics or devices, or a past event with lasting prognostic weight.**

Implanted devices (pacemaker, prosthetic valve, stents, dialysis access), anatomy-altering
surgery (splenectomy, resection, transplant, amputation), permanent conditions (sickle cell,
paraplegia, post-stroke deficit), altered immune status (HIV, asplenia, immunosuppression),
transfusion and lifetime obstetric history, and sentinel events (stroke, cardiac arrest, ICU
admission requiring ventilation).

These do not go away, so they are carried forward indefinitely.

### T3 - Chronic axis
**Incurable systemic disease that fluctuates between stability and exacerbation.**

COPD, heart failure, CKD, atrial fibrillation, cirrhosis, IBD, epilepsy, metastatic malignancy.
The ledger tracks these as a continuing state, with current stage or severity where the notes
gave one.

The **first** presentation of a chronic disease creates the T3 entry. Later flares of the same
disease do not create new entries - see T4.

### T4 - Recurrent episode
**A discrete flare of an existing chronic condition.**

The seventh COPD exacerbation, an acute-on-chronic heart failure admission, a DKA admission.

**You will not usually see these as separate ledger rows.** By design they are collapsed into
the parent T3 entry as a count or frequency, because thirty individual exacerbations are less
informative than "COPD, frequently exacerbating". If you need the individual episodes, look at
the admission list rather than the ledger.

### T5 - Transient
**Self-limiting events that resolve without changing the long-term trajectory.**

A viral URTI, a resolved AKI, uncomplicated gastroenteritis.

**These are normally pruned and will not appear**, unless they are older than the retention
window and carry a significant negative (§7) - for example a normal screening colonoscopy with
a date, which is kept precisely because it documents an absence.

---

## 6. "Flagged - provisional"

This is the most important section to understand, because the name describes the *evidence*,
not a problem with the entry.

A row appears here when the source note did **not** state the fact as confirmed. Two cases:

- **Suspected** - the note hedged. "Presumed aspiration pneumonia", "possible seizure".
- **Patient-reported** - the fact came from the patient, without documentary confirmation. "The
  patient reports an allergy to warfarin."

These are shown separately, and shaded, so you know the evidence is weaker before you rely on
it. They are **not** errors, and they are **not** lower priority. A patient-reported warfarin
reaction is still a warfarin reaction until somebody establishes otherwise. A patient-reported
resuscitation directive is still binding.

**What "provisional" is asking of you:** treat the fact as real for safety purposes, and
confirm it when you can. Provisional status is a statement about how well documented something
is, not about whether it is true.

Note that a fact can be provisional and still sit at T1. That combination - important and
unconfirmed - is the one that needs your attention most.

---

## 7. Coloured badges on the Fact text

Some facts describe an **absence** or a **negative finding**. Because "no X" and "X" read
almost identically at a glance, these carry an explicit coloured badge:

| Badge | Colour | Meaning |
|---|---|---|
| **(absent)** | red | The patient does **not** have this. A documented absence or a negative test: "no known drug allergies", "HIV negative", "colonoscopy normal 2024" |
| **(resolved)** | amber | The patient **did** have this, and it is now resolved, but it still carries prognostic weight: a provoked PE, a first unprovoked seizure, a significant fracture |
| **(surveillance)** | blue | An unconfirmed finding being watched: an incidental nodule, an indeterminate cyst, a rising tumour marker |

The amber **(resolved)** case matters most. It means a real event that happened, not an
absence. Read "Pulmonary embolism (resolved)" as *this patient has had a PE*, not as *this
patient does not have a PE*.

A row with no badge is an ordinary present condition.

---

## 8. Lifecycle

| Value | Meaning |
|---|---|
| **active** | Ongoing |
| **resolved** | Occurred and ended. The record of it remains true |
| **in_remission** | Ongoing condition, currently quiescent |
| **superseded** | The earlier *record* has been withdrawn or replaced - for example an allergy de-labelled after formal challenge, or a resuscitation directive replaced by a newer one |

**`superseded` is a statement about the record, not about the patient.** It does not mean the
condition went away; it means an earlier entry has been retracted or overwritten.



---

## 9. Why you cannot edit the ledger, and what to do instead

The ledger is deliberately read-only. The reasoning:

- It is a **derivation, not a document**. It records what the system found in the notes. Editing
  it would change the record of what the system did, which is not the same as correcting the
  patient's record.
- It is an **input to the compressed paragraph**, not just a display. An edit would silently
  change machine-generated prose at future admissions, in text you never saw.
- It is the **independent check** on that prose. Once it is partly yours, you would be checking
  the summary against your own earlier judgement rather than against the source.
- A wrong row is almost always a **systematic** fault producing the same wrong row for other
  patients. Correcting it at source fixes it for everyone; editing it here fixes one patient
  and hides the fault.

**You have full authority over everything a reader actually sees.** The compressed paragraph
(**Prior Admissions**) and the **incoming summary** are both directly editable, and your text is
never overwritten by the system. If a ledger row is wrong and it matters clinically, correct
the paragraph - that is what the next clinician reads.

Then report the row, so it gets fixed at source. There are two ways in, depending on whether
there is a row to point at.

**A row that is wrong.** Every ledger row has a **report** button at its right-hand end (it is
faint until you hover over the row). Use it for anything the row itself gets wrong - the
wording, the tier, the certainty, the status, the dates, the merging.

**Something that is missing.** A missing fact has no row to click, so it is reported from the
**+ Report something missing from this record** button at the top of the Retention Ledger
section. Describe what you expected to see, in your own words.

Either way you will be asked three things: what kind of problem it is, a free-text description,
and two yes/no questions - whether a clinical decision was made or nearly made on the row, and
whether you have seen the same thing on other patients. Those two are questions of fact, not a
severity rating; they are what decides how fast the report is looked at.

The two acknowledgement tabs do something different:

| Tab | Use it for |
|---|---|
| **Completeness** | A fact from an older admission that did not survive into the summary. Rows marked **MISSING** did not make it through; **ROLLUP** means it was collapsed into a count. Mark each ✓ resolved or ✕ dismissed if it was correctly dropped, else use the "Report something missing from this record" button.|
| **Concerns** | Anything the pipeline could not settle by itself: unsupported summary text, a failed auto-correction, or a new admission folded in underneath an edit you made |

Both are acknowledgement, not editing. Your decision is recorded permanently and is never
overwritten. Each row in those tabs also carries its own **report** button, for when
acknowledging it is not enough and the underlying behaviour is wrong.

### Two things not worth reporting

- **Duplicate or near-duplicate rows.** The same condition appearing more than once is a known
  limitation and is already on the list. Reporting it costs you time and tells us nothing new.
- **A fact that only appears in the admission you are reviewing now.** The ledger is the
  *established* record and is built from prior admissions only, so anything first documented in
  the current admission is correctly absent until that admission is folded in. 

### What happens after you report

Every report is logged with the row, the patient, and the exact pipeline version that produced
it - you do not need to record any of that yourself, and you should not spend time describing
where in the interface you were.

It is then routed automatically to a clinician or an engineer, or to both when the cause could
be either. Reports that could affect patient safety - a missing T1 fact, a row that reads as the
opposite of the truth, a wrong-patient entry, or anything where you told us a clinical decision
turned on it - are marked urgent and jump the queue.

**You will get a written answer by email in every case**, including when the answer is "this is
working as designed" or "we could not reproduce it". A report that is not a defect is still a
useful report: if the record read wrongly to you, it will read wrongly to someone else, and the
fix is often to the interface or to this guide rather than to the data.

Fixes ship on the normal release cycle rather than one at a time, so a correction may not appear
immediately. **If a row is wrong and it matters for this patient now, correct the paragraph as
well as reporting the row** - the report fixes it for future patients, the paragraph fixes it for
this one.

---

## 10. What the ledger does not show

Keep in mind, so absence is not over-read:

- **The current admission.** The ledger is the established record, built from PRIOR admissions.
  A condition first documented in the admission under review appears only after fold-in.
- **T4 flares and most T5 transients** - by design (§5).
- **Facts the notes formally ruled out** - excluded rather than displayed.
- **Superseded non-safety entries** - for example a device replaced by a newer one; the current
  device is shown instead.
- **Anything not in the discharge summaries.** The ledger is built only from those. It is not
  connected to the EMR problem list, medication list, or investigations.

The ledger is an aid to help you grasp rapidly the patient's longitudinal record. 
It is not a substitute for reading the record, and it should not be used as a standalone clinical document.

---

## 11. Quick reference

- **Read top-down.** T1 first - those are act-before-anything-else facts.
- **"Flagged - provisional" means weakly evidenced, not unimportant.** Treat as real, confirm
  when you can.
- **Check the badge.** (absent) is red and means *does not have*. (resolved) is amber and means
  *did have*.
- **`superseded` on a T1 allergy with no (absent) badge** - treat as active, and report it.
- **The paragraph is yours to correct. The ledger is not.** Fix the prose, flag the row.
- **If ledger and paragraph disagree, trust the ledger** and correct the paragraph, unless you know otherwise.
- **The ledger is the prior record.** Something documented only in today's admission is not
  missing - it has not been folded in yet.
- **To report:** the faint **report** button on any row, or **+ Report something missing** at the
  top of the ledger. You get an emailed answer either way.
