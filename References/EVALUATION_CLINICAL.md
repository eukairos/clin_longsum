# EVALUATION_CLINICAL.md -- what the evaluation proves, for clinicians and governance

Companion to `EVALUATION.md` (the engineering runbook). Same framework, written for the
clinician.

**What this document is for.** Deciding three things: whether the evaluation asks the right
clinical questions, which of its numbers you can rely on, and what to put in front of a
governance board.

---

## 1. What the system produces, and what could go wrong

The system builds one longitudinal view per patient from their past admissions: a TL;DR, the
current episode, recent admissions in full prose, older admissions compacted into a short
narrative, and a **retention ledger** -- an itemised, auditable list of the facts judged worth
carrying forward for life.

Three things can go wrong:

| Failure | What it looks like | Why it matters |
|---|---|---|
| **Omission** | a penicillin anaphylaxis is not carried forward | The one that kills. |
| **Fabrication** | a diagnosis appears that no note supports | the clinician acts on something untrue |
| **Distortion** | correct facts, wrong emphasis, wrong order, unreadable | slower, more error-prone reading |

The evaluation is built around that ranking. Omission is gated deterministically and blocks
release. Fabrication is measured by an AI judge and tracked, because the judge is not reliable
enough to block on. Distortion is scored on a mostly published quality instrument and tracked.

**One structural point to bear in mind.** Compaction deliberately drops content. So "did it keep
everything?" is the wrong question. The question that the system actually gates
on, is **"did it keep everything it was supposed to keep?"** -- where "supposed to" is defined
by the clinical taxonomy. 

---

## 2. The three evaluation tiers

**Tier 0 -- the hard gates. Run every time. A failure stops the release.**
No AI involved. These are arithmetic checks over the ledger and the assembled view, and they
give the same answer every time on the same data. This is the safety floor.

**Tier 1 -- the AI-judge measures. Run nightly. Tracked, not blocking.**
An AI model reads the output and rates it. Useful for spotting a regression, not reliable
enough to block a release. Section 4 explains why.

**Tier 2 -- periodic validation. Off the release path.**
Checks the measuring instruments: does the AI judge agree with clinicians, 
do the gates actually detect the failures they claim to, does the system still work
on patients it has never seen.

**Deliberate asymmetry:** the check that can *block* is the one that involves no AI judgment.
Everything that depends on a model's opinion advises a human instead of overriding one.

---

## 3. Which gate protects what

These are the checks that stop a release. You set the threshold for each.

| Gate | The clinical promise |
|---|---|
| `gate_T1` | No Tier-1 fact is lost. Allergies, code status, infection-control status. |
| `gate_T2` | No Tier-2 fact is lost. Sentinel events, implanted devices, anatomy-altering surgery including major organ transplant, permanent conditions, altered immune status. |
| `gate_safety` | No severe adverse drug reaction, code-status directive, or infection-control status is lost, regardless of anything else about the record. |
| `gate_signeg` | No significant negative is lost. A documented *absence* -- ruled-out, resolved, no longer present -- is clinical information and is retained as such. |
| `gate_swr` | The salience-weighted total retention does not fall below the agreed floor. |
| Integrity gates | The right admissions appear in the right tiers, in the right order, with nothing from the future leaking into the past and nothing shown twice. |
| Compression band | The compaction neither did nothing nor destroyed the content. |

**Weighting.** Every retained fact is weighted by tier: a lost Tier-1 fact counts five times a
lost Tier-5 one. Three classes are floored to top weight regardless of tier, because they stand
in for categories that must never slip:

- `severe_adr_allergy` -- the category itself
- `chronic_disease_state` -- the closest available class to **active malignancy**
- `anatomy_altering_surgery` -- the closest available class to **major organ transplant**

**This is approximate on purpose, and it is a decision for you.** There is no
malignancy-only or transplant-only class. Flooring the closest class means flooring everything
in it: elevating `chronic_disease_state` also elevates COPD, heart failure, CKD, AF, cirrhosis,
IBD, epilepsy and bipolar disorder. Narrowing it would require the system to read the *content*
of a fact rather than its label, which is deliberately not done, because content matching is
where silent errors come from. Decide whether you accept the over-inclusion, or want a new class
added to the taxonomy. `TAXONOMY_EXTENSION.md` is the how-to, discuss with your AI engineer.

**A known asymmetry to decide on.** Major organ transplant is hard-gated, because it sits in a
Tier-2 class. **Active malignancy is not**, because it sits in a Tier-3 class. A lost malignancy
fact is caught by the aggregate score and flagged critical in the miss register, but no
per-class gate blocks on it. Adding the whole `chronic_disease_state` class to the hard gates
would fix that and would also gate all chronic disease. This is an open clinical decision.

---

## 4. What the numbers mean, and what they do not

### The retention numbers are trustworthy, within their scope

They are arithmetic, reproducible, and auditable to the individual fact. Every loss appears in a
register with the fact text, its class, its weight and where it went.

**Their scope is what the taxonomy pinned.** The metric counts only facts the taxonomy said to
keep. If the taxonomy declines to pin something clinically important, no retention gate will
ever tell you. **That is why reviewing the taxonomy is a clinical task and not a technical
one** -- see `clinical_tuning_points.md`.

**"Lost" has two meanings, and the register distinguishes them.** A fact can be gone entirely,
or it can survive only as a count in a rollup: "5 COPD exacerbations" where the fifth was the
one requiring intubation. The second is a loss of resolution, not of existence. Both are
reported; the strict score treats a rollup as a loss and the lenient score does not. Ask for
both.

### The faithfulness numbers need a caveat stated out loud

The AI judge that scores faithfulness was measured against clinician labels on an external
benchmark (VeriFact). The result:

| | |
|---|---|
| Agreement with clinicians | 78% |
| Chance-corrected agreement (Gwet's AC1) | 0.72 |
| **When it flags a statement as unsupported, it is right about** | **1 time in 4** |

Of 25 statements the judge flagged as unsupported, 6 were genuinely unsupported. 
Of the 15 genuinely unsupported statements in the sample, it declined to endorse 12 and waved 3 through. 
So - (1) it over-flags heavily and (2) under-detects modestly. Much of the over-flagging is a boundary dispute 
between "not supported" and "not addressed by the source", which is a labelling distinction more than a safety one.

**Three consequences.**

1. **A flag means "a clinician should look", never "this is a fabrication."** Check the
   source note to establish that.
2. **The judge cannot be allowed to block a release on its own**, and in this system it does
   not.
3. **The judge also sits inside the pipeline**, deciding which compacted statements get
   automatically corrected when judged unsupported. Its false positives therefore cost something real: 
   some correct statements will be hedged into vagueness at scale. The correction step cannot see the source note, so it
   can only soften a statement, never delete one. Budget review time for this. 

**These figures came from a 183-proposition sample using one specific model.** If you fork this
system, you should re-run the calibration against your own judge model.

### The quality score is one model's opinion

PDSQI-9 is a published, validated instrument for rating clinical summaries: 9 attributes
covering accuracy, thoroughness, usefulness, organisation, readability, conciseness,
cross-admission synthesis, and absence of stigmatising language. We added a tenth, temporal
coherence, to the published 9 to evaluate the model on handling admission episodes over time.
**Nine of the ten attributes are externally validated; the tenth is ours and is not.** Say which
is which when you report the scores. 

**And in every case the ratings are not external.** They come from the same AI judge.

**A human panel has not been run.** The design calls for 20 to 30 patients rated by clinicians
against an error taxonomy (hallucination, omission, temporal error, salience
mis-classification), reported as judge-versus-human correlation and inter-rater agreement. Until
that exists, treat PDSQI-9+1 as a regression tracker, not as evidence of quality. **Running that
panel is the single highest-value clinical contribution to this evaluation** -- and it is the
only thing that would externally validate (or otherwise) the temporal-coherence attribute at all.

---

## 5. The one place a real clinician's judgment enters

Every patient's current admission carries a real, clinician-written discharge summary. Whatever
that clinician chose to carry forward into the past-medical-history and problem list is salient
*by clinical judgment*, not by our taxonomy's opinion of itself.

The system checks its ledger against that summary, on two axes:

- **Did we keep what the clinician thought worth keeping?** An external test of the taxonomy,
  rather than an AI grading the taxonomy that produced its own input.
- **Does the established record contradict the new summary?** A contradiction is either a stale
  fact in our record or a genuine change in the patient. Either way it is surfaced for review.

Two design points a reviewer will ask about. It compares against the **real, unedited clinician
text**, never the system's own condensation of it -- otherwise it would be testing the model
against the model. And it compares against the **ledger**, not the raw extraction, because the
ledger is what a clinician actually sees.

**Its limit:** it covers one admission, and it tests *retention* only. It does not study
whether the prose is well written or safe to read.

---

## 6. Three findings a clinical reviewer should know

### The narrative alone is not safe. The ledger is what makes it safe.

A diagnostic study regressed whether a fact survives into the compacted prose against two
things: how clinically salient it is, and how recent it is. Over 1736 retained facts across 75
patients, **recency predicted survival and salience did not register at all.** Survival rose
from about 41% for the oldest admissions to about 57% for the most recent. The language model
favours recent content, as language models do. Clinical importance does not decay with time; a
code-status decision or a severe allergy from five admissions ago matters exactly as much today.

One qualification: the two factors together explain only a small share of what decides
survival, so recency is a real and statistically clear effect but not the whole story. The
direction is what matters here, not the size.

A second study tested the obvious remedy -- showing the writer the retained-fact list and
telling it not to contradict the record. **It helps, but is not enough.** Removing the list
from the writer's prompt raised contradicted facts from 17 to 28, so the instruction is doing
real work. But 17 facts were still contradicted with the list supplied. 

Together these are the empirical justification for the architecture. A system that produced only
prose would silently drop older salient facts, and no instruction to the writer would fully stop
it. The ledger is a separate, itemised, auditable store that does not depend on the model
choosing to mention something. **When reviewing output, treat the ledger as the record and the
prose as the reading aid.**

### Some taxonomy classes are not reliably distinguishable, and this is measured

The extractor sometimes assigns a fact a class from an unrelated part of the taxonomy while the
fact's *text* is perfectly accurate. Nothing catches this: the label is valid, so no check
fires, and every later stage trusts it.

Three independent methods agreed on which classes are cleanly separable and which are not:

| | Classes | What to do |
|---|---|---|
| **Reliable** | code status directive, chronic disease state | quote per-class numbers with confidence |
| **Usable** | implanted device, transient event, altered immune status, anatomy-altering surgery | quote with the caveat |
| **Weak** | infection control status | treat per-class numbers as indicative |
| **Not separable** | permanent condition, recurrent episode, sentinel event -- all overlap chronic disease state | do not read a per-class number here as meaningful |

The control result matters as much: a do-not-resuscitate probe was filed to the code-status class
in 42 of 42 cases. **Perfect consistency is achievable**, so the scatter elsewhere reflects
real blurred clinical concepts, not a broken system.

**The clinical consequence, measured.** In the reference cohort, 10 of 69 facts filed under
"severe adverse drug reaction" were not drug reactions at all -- they were lines, devices and
operations. Because safety-class facts are deliberately made hard to remove, those entries are
permanent and a physician cannot delete them. **This was reviewed and accepted: it errs in the
safe direction.** The opposite direction is the dangerous one -- a genuine drug intolerance
filed under the wrong class loses its protection and can be pruned. Both directions occur.

The fix is a better extraction model and a better prompt, not another gate. Your review of the
class definitions is what moves this most.

### Merging entries occasionally drops a concept, and the ledger usually catches it

The system merges repeated mentions of the same condition into one line, in three places: twice
when it builds the ledger deterministically, and once more when it rolls up chronic conditions.
The risk is that a short line absorbs a longer, more specific one -- "coronary artery disease
status post stent placement" becoming plain "coronary artery disease", with the stent gone. This
is silent: no check fires, because the underlying fact is still filed inside the merged entry.

Every merge in the reference cohort was screened for exactly this: was a high-salience concept
(a device, an operation, a resistant organism, a cancer, an allergy, a code-status decision)
present in one of the merged entries but missing from the line that replaced it? And if so, was
it recovered anywhere else the clinician actually sees -- another ledger entry, or the prose?

| | |
|---|---|
| Merges examined | 1759 |
| Concepts dropped at a merge | 115 |
| **Recovered elsewhere in the ledger or prose** | **101** |
| Reaching review | 10 |
| **Confirmed genuine losses on clinician reading** | **4** |

**The recovery number is the finding.** The ledger's redundancy absorbs almost all of these,
which is why a screen that only asked "was something dropped" would have over-reported roughly
twenty-fold. It is also why reviewing a merge in isolation is misleading: the question is
always whether the concept survives *somewhere* in what is served.

The four genuine losses: a coronary stent, a VRE bacteraemia absorbed into a different
organism's line, a bladder-cancer resection with its remission status, and one hepatitis B entry
where "resolved" became "chronic" and the reactivation-risk warning was lost. The last is the
most instructive -- not an omission but a change of clinical meaning.

Two cautions if you run this yourself. **Four of the ten review rows were correct behaviour, not
losses**: a patient documented DNR/DNI four times whose code status was later documented as Full
twice. The system is right to carry the most recent directive, and it reads exactly like a lost
DNR until you check the source. Always read the source note before calling a row a defect. And
**the screen only knows the concepts on its list** - about forty of them - so it is a triage
filter, never proof that nothing else was lost.

One technical caveat for completeness: the ledger-building merges were screened against the
current taxonomy version, the roll-up merges against artifacts built at the previous one. The
newer version pins more facts, so if anything this under-reports. Re-run both after any
taxonomy change before quoting the numbers.

---

## 7. What you can review yourself

Four artifacts are built for a human reader, and reviewing them is real clinical work.

**The miss register** (`salience_recall_misses.csv`). Every fact the system was supposed to keep
and did not, ranked by weight, with where it went. Read the top rows. If a loss is clinically
harmless, that is a signal the taxonomy over-pins. If it is dangerous, it is an incident.

**The judge-health worklist** (`nothink_audit.jsonl`). On rare occasions, the internal judge auto-degrades to a
faster, less careful mode when it fails to parse a statement on first try. If it still passes a statement, 
that statement lands here with a review focus telling you what to check. 

**The ledger itself**, through the review user interface (UI). `HOW_TO_READ_THE_LEDGER.md` explains the fields,
the badges, and the known reading traps.

**The report button on every ledger row.** , also through the review UI. A fact that looks wrong can be reported from the UI
and is routed, tracked and answered in writing. See `FEEDBACK_RUNBOOK.md`. 

---

## 8. Limitations

Bring these to the board yourself. 

1. **There is no external human gold standard for a longitudinal summary, anywhere.** No corpus
   pairs a multi-admission record with a curated longitudinal summary. Every whole-artifact
   number is either AI-produced or measured against the system's own inputs.
2. **The only external clinician-labelled benchmark (VeriFact) validates the JUDGE** but
   it is not shipped with this repository - it depends on separately credentialed data and
   different infrastructure. You should nenchmark your judge model against VeriFact.
3. **The judge is right about roughly 1 in 4 of its unsupported flags.** Human triage, not
   automated gating.
4. **The judge that evaluates and the judge inside the pipeline are the same model.** A good
   evaluation score partly reflects the model's own dispositions rather than the output's
   quality.
5. **No human quality panel has been run.** PDSQI-9+1 numbers are one model rating another
   model's prose, and one of the ten attributes -- temporal coherence -- is a local addition to
   the published nine, with no external validation of its own.
6. **Whether the system merged two genuinely different conditions into one ledger line is
   screened but not measured.** It is checked for inventing things, and for losing things. For
   merging things wrongly there is now a screen rather than a metric (section 6), which found
   4 genuine losses in 1759 merges. The screen catches a merge that silently *drops* a concept;
   it cannot catch a merge that names two distinct conditions on one line while keeping both.
   That still needs clinician verdicts on a sample.
7. **The tier weighting has not been validated.** A sweep across four weighting schemes moved
   the aggregate score barely at all, a linearly scaling scheme was chosen arbitrarily. But the hard gates
   are count-based - **catastrophe detection does not depend on the
   weights** - but do not present the weighting as validated.
9. **Some class labels are unreliable where the classes overlap** (section 6), and roughly one
   in seven safety-class entries in the reference cohort was not a drug reaction.
10. **Anything the taxonomy declines to pin is invisible to every retention metric.** Under-pin,
    and no gate will tell you.
11. **The gates are proven only against the specific corruptions they were tested with.** A
    failure outside that list would go undetected.
12. **Scale.** The reference cohort was 100 patients with roughly 3 to 15 admissions; the unseen validation
    cohort was 10 patients. Lifetime-scale records with hundreds of admissions are untested.
13. **Corpus.** The taxonomy was tuned on one corpus. The tier *structure* should transfer; the
    class definitions and thresholds may not.

---

## 9. Taking it to a governance board

### Do this first

1. **Reproduce every number on your own data.** The figures in these documents come from a
   research corpus. The harness ships precisely so you can regenerate them on your own data. Nothing from the
   reference run should appear in your submission.
2. **Run a clinician pilot before the submission, not after.** Have clinician champions read
   real output for real patients and record what they find. `clinical_tuning_points.md`
   catalogues every decision they should be forming a view on.
3. **Calibrate your judge**, or state plainly that you have not and that all AI-judged numbers
   are provisional.
4. **Run the generalisation cohort at a defensible size.** The reference used 10 patients, which
   is a sanity check, not evidence.
5. **Name the owners.** Who signs a taxonomy change, who signs a threshold, who can waive a
   gate, who receives a safety escalation. In a small team one person may hold several of these;
   the roles still need to be distinct on paper.

### The evaluation pack

| Item | What it establishes |
|---|---|
| Gate definitions and thresholds, with clinical sign-off | what blocks a release, and who agreed to it |
| Gate results on a frozen cohort | the current state, reproducibly |
| Retention broken down by tier and class | that safety-critical categories are covered |
| The miss register | every known loss, ranked, with attribution |
| Metric-validation results | that the gates detect what they claim to |
| Operating point and degradation curve | that compression has not been bought with retention |
| Judge calibration report | what the AI-judged numbers are worth |
| Judge-health worklist and its review record | that degraded verdicts were reviewed, not trusted |
| Generalisation result | that it was not tuned to its own test set |
| Merge-loss screen and its adjudication | that consolidation was checked for silently dropped concepts, with the residual named |
| The two architecture studies (recency, ledger ablation) | why the ledger exists, in evidence rather than assertion |
| Failure register (`KNOWN_FAILURE_MODES.md`) | known defects, mechanisms and status, disclosed. Most have been fixed, partially fixed, or mitigated, but are listed in case you encounter them in your dataset|
| Clinical decision register (`clinical_tuning_points.md`) | which choices were clinical and who made them |
| Feedback-loop design (`FEEDBACK_RUNBOOK.md`) | that a clinician who finds an error is heard, tracked and answered |
| Waiver log | that no gate was silently skipped |

### Three claims to make, and one to avoid

**Claim:** the safety-critical retention checks are deterministic, reproducible and auditable to
the individual fact, and they block release. **Claim:** those checks have themselves been
validated by injecting known failures and confirming they fire. **Claim:** the AI judge's
agreement with clinicians has been measured, and its limits are stated rather than assumed.

**Do not claim that the output has been validated against an external human gold standard.** It
has not, and no such standard exists for this artifact. The honest position is stronger than it
sounds: retention is deterministic and auditable, the judge is calibrated and its error rate
published, the failure modes are catalogued rather than hidden, and a clinician who finds a
defect has a tracked route to a written answer.

---

## 10. Where to go next

| Document | Answers |
|---|---|
| `clinical_salience_taxonomy.md` | the five-tier system and its clinical reasoning |
| `clinical_tuning_points.md` | every site-specific clinical decision, with trade-offs |
| `HOW_TO_READ_THE_LEDGER.md` | how to read a ledger row, including the known traps |
| `FEEDBACK_RUNBOOK.md` | how a clinician reports a defect and how it is answered |
| `KNOWN_FAILURE_MODES.md` | catalogued defects, mechanisms and current status |
| `EVALUATION.md` | the engineering runbook: every script, gate and command |
| `TAXONOMY_EXTENSION.md` | what it costs to change a class or a tier |
