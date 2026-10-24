# MODEL_SELECTION.md

Guidance for choosing and configuring the models behind `clin_longsum` when forking it onto your
own institution's notes.

**Audience.** An engineer standing this pipeline up on hospital data, with either hosted model
access permitted by local policy or hardware capable of running models substantially larger than
the reference configuration.

**Scope.** Four roles, in descending order of how much damage a bad choice does:
generation (b1, b4, c1, c4, d1, d2, e1), the **judge at c3**, the **judge in the f-series**, and
the **embedding model**. Each section follows the same template: what the role decides, whether it
is on the build path, what failure looks like, what metric governs the choice, what to pin, and
what a swap costs.

**Reference configuration.** All five generation roles are bound to a 35B-parameter MoE served
locally at 4-bit through Ollama; the judge is a separate-lineage 32B reasoning model; the
embedding model is `Snowflake/snowflake-arctic-embed-l-v2.0` at 1024 dimensions. Treat the reference config as a
floor, not a recommendation.


---

## 0. Where the models actually sit

`src/config.py` is the only file you edit to swap a model. Every role is bound to a `ModelSpec`,
and `config.model_for(role)` is the sole lookup. Six roles are live:

| `MODELS` key | Stages | Bound to | Temp |
|---|---|---|---|
| `retention_extractor` | b1 (+ `b1_rerun_hadms`) | generation model | 0.0 |
| `compactor` | c1, c4 | generation model | 0.2 |
| `single_summary` | e1 | generation model | 0.2 |
| `episode_paragrapher` | d1 | generation model | 0.2 |
| `tldr_synthesizer` | d2 | generation model | 0.2 |
| `faithfulness_validator` | c3, and f4 / f5 / f6 / f9 / `eval_fact_presence` | judge model | 0.0 |

`b4` reaches the generation model through `ledger_consolidate.py`; `f12 --sweep` uses the
compactor. 

**The single most useful thing to know before changing anything: five generation roles currently
share one `model_id`.** So "try a better extraction model" in practice means changing b1, b4, c1,
c4, d1, d2 and e1 at once, and revalidating all of them. Nothing in the architecture requires
that. Per-role binding already works, and per-role *tuning* is already in use (extraction at
T=0.0, narrative synthesis at T=0.2), so binding a different `model_id` to one role is a
config-only change.

**Recommendation: bind per role, and change one role at a time.** Extraction under a constrained
schema and TL;DR synthesis are different tasks; there is no reason they need the same weights. A
single global binding makes every experiment expensive enough that it does not get run.

---

## 1. The failure this guide answers

An internal audit of the reference configuration found that the extractor reliably produces
*schema-valid* output and *content-faithful* fact text, while still assigning the wrong taxonomy
class sometimes - for instance a drug-reaction fact filed under a procedural or
immune-status class. Registered as FM-9 in `KNOWN_FAILURE_MODES.md`.

Two properties of that failure shape everything below:

- **It is semantic, not structural.** Constrained decoding guarantees a member of the enum. It
  guarantees nothing about which member. This matches the published finding that semantic errors
  persist even when structural validity is achieved [7].
- **It is stable, not random.** At `temperature=0.0` a rerun will usually reproduce it. The
  variance is across *inputs* - the same clinical concept, phrased differently in different
  notes, receiving different classes - not across runs. See section 6.

---

## 2. Generation roles

| | |
|---|---|
| Decides | what facts exist, their taxonomy axes, and every word of served prose |
| Build path | yes, all of it |
| Failure | silent and permanent - wrong output is indistinguishable from right output downstream |
| Governing metric | class agreement across paraphrases of one concept (section 6); faithfulness against `bhc_text` |
| Pin | `model_id` + `version` in `MODELS`; `PROMPT_VERSIONS`; `TAXONOMY_VERSION`. All three are stamped on every `AuditRecord` |
| Swap costs | re-extraction (b1) or re-generation (c1/d1/d2) for every affected role, plus revalidation |

### Dense or MoE?

**Short answer:** at matched *active* parameters, prefer dense for extraction. Expect a modest
gain. Do not expect it to solve the problem.

The relevant published result is the split between memorization and reasoning. Switch Transformers
observed that at fixed pretraining perplexity, the sparse model does worse than its dense
counterpart on downstream reasoning-heavy tasks such as SuperGLUE, while performing
disproportionately well on knowledge-heavy tasks such as TriviaQA [1]. *Mixture of Parrots*
sharpens this: holding active parameters fixed and increasing the number of experts, memorization
improves consistently while reasoning capability saturates, with a proof that graph problems exist
which no number of experts of a given width can solve but a slightly wider dense model solves
easily [2]. The same paper is careful that the per-parameter disadvantage is task-dependent - it
holds on reasoning tasks, but on memory-intensive tasks equally-sized MoEs match dense
transformers [2].

Class assignment in b1 is a 12-way judgment over a long instruction containing definitions,
decision rules, examples and counter-examples. That is instruction-following and boundary
reasoning, not recall, so the split predicts a dense advantage.

**Three cautions.**

1. **Match on active parameters, not total parameters or file size.** A 4-bit 35B MoE and a 4-bit
   35B dense model are not comparable; the MoE activates a fraction of its weights per token.
2. **Instruction tuning narrows the gap.** A fine-tuned dense T5 beat its MoE equivalent, but the
   instruction-tuned Flan-MoE performed significantly better than the dense Flan-T5 [1].
3. **Architecture may not be the biggest lever.** LLMStructBench benchmarked 22 models across
   parsing scenarios with five prompting strategies and found that prompting strategy matters more
   than model size [7].

**On quantization, we advise nothing.** The evidence is contradictory. MoQE reports an MoE holding
translation accuracy to within -0.3 BLEU down to 3-bit while the dense comparator preserves
accuracy only to 4-bit and collapses at 2-bit [8]. Later work reports the opposite direction [9]
and identifies routing instability, where small quantization perturbations change top-k expert
selection and alter the computation path [10]. Validate on your own data.

### Medical-domain model or general-purpose?

Do not assume a medically fine-tuned model wins. Without task-specific fine-tuning a general-purpose
Llama 3.1 8B outperformed Llama-3-8B-UltraMedical at structuring radiology and pathology
reports [14]. The same review notes that evidence for a consistent benefit from
medical-specialised models in *structured information extraction* is limited, and that where
reports contain structured elements reducing the need for specialised medical knowledge, a
general-purpose model is sufficient [14]. b1 is closer to schema-constrained extraction than to
clinical reasoning: the note states the facts and the model segments and labels them.

### Prompt and schema design will likely beat model choice

**Constrained decoding helps classification and hurts reasoning.** *Let Me Speak Freely?* found
that format restrictions, particularly JSON-mode constrained decoding, hinder reasoning ability
while enhancing classification-task accuracy, with stricter constraints producing greater
degradation on reasoning tasks [15]. b1's axis assignment is nominally classification, so the enum
constraint is justified. The step that precedes it - deciding what the facts *are*, and splitting
compound statements - is closer to reasoning and is the part exposed to this effect.

**Schema field order was tested here and does not matter.** The reference schema emits
`retention_class` before `rationale`, and `rationale` is not in `required`, which suggests the
label is committed before any justification exists. An A/B on one admission, identical prompt,
`rationale` moved ahead of the label and marked required, produced **byte-identical output**. With
a thinking phase enabled the model reasons before the JSON begins, so the rationale is a post-hoc
summary and its position is irrelevant. Do not spend effort here. (Caveat: the comparison parsed
the JSON, so key order was not inspected - "no effect" and "the backend did not honour the
reorder" are not distinguished.) The real gap is that **thinking traces are not persisted**, so
the reasoning behind a wrong class cannot be audited afterwards. That is a logging change.

**Beware small-sample prompt tuning.** In one study a few-shot revision that looked effective on a
small validation sample reduced full-scale F1 from 0.948 to 0.914 [6]. Validate at full scale.

**Atomicity is a prompt problem before it is a model problem.** The largest class of defects found
in the reference cohort was compound facts - one fact carrying two separately class-worthy
concepts, so whichever class is assigned is arbitrary between them. The b1 prompt already forbids
this explicitly, and nothing downstream splits them. A model that follows the rule it was given
fixes more than a larger model that does not.

---

## 3. The judge at c3 - on the build path

| | |
|---|---|
| Decides | which compacted statements are unsupported |
| Build path | **yes.** `c4_correct.py` is gated on the PRESENCE of `failed_statements` |
| Failure | a false positive causes c4 to soften a CORRECT statement, automatically, with no human in the loop |
| Governing metric | chance-corrected agreement against clinician labels, and recall of true unsupported statements |
| Pin | `model_id` + `version`, `num_ctx`, the judge prompt version, and the 0.6 threshold |
| Swap costs | full recalibration - the existing agreement figures do not transfer |

This is the role most likely to be under-scrutinised, because "the judge" sounds like an evaluator
sitting outside the system. At c3 it is not. Its verdicts trigger automatic edits to text a
physician reads.

Two facts make that consequential. First, the judge's exact-label metrics on the external calibration (VeriFact) 
are Not-Supported PPV **0.24** and Not-Supported recall **0.40** (calibration below) --
but read those with the confusion matrix, not alone. Two-thirds of the recall misses land on
Not Addressed rather than Supported, so the judge declined to endorse 12 of the 15 genuinely
unsupported claims and waved through 3. Roughly a third of the false positives are Not-Addressed
claims called Not Supported, a boundary disagreement rather than a wrong call. The over-flagging
is real; the under-detection is smaller than the headline recall suggests.
Second, FM-6: the correction steps cannot see the source note, so c4 can only *soften* a
statement, never delete it. The realistic failure is therefore not a fabrication surviving review;
it is **correct statements being hedged into vagueness at scale by a judge that was wrong about
them**. Any judge you bind here should be selected with that asymmetry in front of you.

**Optimise recall, not accuracy.** Because a human reviews the positives, a false positive costs
review minutes while a false negative lets a fabrication through unseen. A judge with better
overall accuracy but worse recall is a worse judge for this pipeline.

**Report chance-corrected agreement, never raw agreement.** Raw agreement overstates
discrimination badly - one large evaluation found kappa deflation of 33-41 percentage points
across 21 judges [13].

**Lineage independence is right, and it is a property of the (generator, judge) PAIR.** The
reference judge was chosen partly because it comes from a different lineage than the generation
model. That is well founded: judges exhibit self-preference bias and family bias, systematically
assigning higher ratings to outputs from models of their own family [16], with one analysis
attributing the mechanism to perplexity - models score low-perplexity text higher, and their own
family's output is low-perplexity to them [17]. **The practical consequence: re-check independence
whenever you change the GENERATION model, not only when you change the judge.** Swapping the
generator into the judge's family silently destroys the property you paid for.

**Check the BACKBONE, not the model name.** Distilled reasoning models are named after the
distillation source, not the base they were distilled onto. The reference judge, `deepseek-r1:32b`
served by Ollama, is almost certainly **DeepSeek-R1-Distill-Qwen-32B** - a Qwen-32B backbone
carrying DeepSeek-R1 reasoning behaviour. If your generation model is also Qwen-derived, then
generator and judge share a backbone family and the independence you thought you had is partly
illusory, whatever the model names suggest. **Verify the backbone of both before relying on this
property.**

**Full independence may not be affordable, and that is a legitimate trade.** In the reference
configuration the generator and judge share a backbone family, and this was accepted knowingly:
the models that would have given true independence did not fit the single-GPU budget alongside
everything else the pipeline loads. Record the compromise rather than assume it away - a
partially independent judge is still worth more than none, provided the calibration below is done
and the residual family bias is stated when the numbers are reported.

**Independence is necessary, not sufficient.** What governs is measurement against clinician
labels.

### How to calibrate your judge against clinician labels

The reference implementation qualified its judge using **VeriFact** [20] (MIT licensed), run as a
separate self-contained subproject that is not shipped here. You do not need to build a labelled
set from scratch to do the same thing - VeriFact-BHC is an existing clinician-labelled benchmark
of propositions from MIMIC-III brief hospital courses, each rated Supported / Not Supported /
Not Addressed against the patient's EHR.

**Route A - qualify the judge on VeriFact-BHC (cheap, what the reference did).**

1. Obtain VeriFact-BHC from PhysioNet under the usual credentialing, and clone the upstream repo
   [20].
2. Reproduce the pipeline shape - decompose -> retrieve -> judge -> score - against **your own**
   serving stack. The upstream repo assumes a multi-GPU Docker deployment (vLLM, qdrant, redis/rq,
   containerised embedding and rerank services). If you run something simpler, reimplement the
   shape rather than the infrastructure: hybrid dense+sparse retrieval over sentence-level EHR
   facts, a reranker, then your judge. Keep VeriFact's judge prompt, verdict schema and verdict
   labels byte-for-byte; only the transport should differ.
3. Use their best published hyperparameters as your defaults - `retrieval_method=rerank`,
   `top_n=100`, `reference_format=absolute_time`, admission-scoped reference, sentence-level EHR
   facts - so your numbers sit on the same axis as theirs.
4. Score with **Gwet's AC1 and multiclass MCC**, not percent agreement, and report per-label
   sensitivity / specificity / PPV / NPV. Their own published metric emphasis is the same. Gate on
   AC1; the reference used a provisional threshold of 0.6.
5. Treat any comparison to VeriFact's published agreement figure as a direction-and-magnitude
   sanity check, not a reproduction, unless you are running their exact judge model.

**Route B - build your own labelled set on your own notes.** More expensive but the only thing
that establishes site-specific validity. Worth doing once Route A shows the judge is in the right
region.

**What the reference calibration actually produced** (n=183 stratified propositions, adopted
`clarified_v2` prompt):

| | verbatim prompt | adopted prompt |
|---|---|---|
| Percent agreement | 0.689 | 0.781 |
| Gwet's AC1 | 0.595 (gate FAIL) | **0.720** [0.631-0.798] |
| Multiclass MCC | 0.372 | **0.514** |
| Not Supported - PPV | 0.18 | **0.24** |
| Not Supported - recall | 0.60 | **0.40** |
| Not Addressed - recall | 0.296 | 0.667 |

Three things to take from that table rather than from the headline AC1.

- **Read the recall column with the confusion matrix, not alone.** The drop from 0.60 to 0.40 is
  not a regression: it is the cost of Not-Addressed recall rising from 0.296 to 0.667. The
  clarification taught the judge to use Not Addressed properly, which moved borderline cases out
  of Not Supported. Of the 15 gold Not-Supported propositions the adopted prompt missed 9, but 6
  of those went to Not Addressed and only 3 to Supported - so it failed to endorse 12 of 15, and
  waved through 3. On the PPV side, 19 of the 25 predicted Not Supported were errors, of which 12
  were genuinely Supported and 7 were Not Addressed. The over-flagging is real; the exact-label
  recall overstates the under-detection. **The Not-Supported / Not-Addressed boundary is the hard
  part of this task, and it is a labelling-scheme distinction more than a safety one** - decide
  which of the two your downstream stage actually acts on before you tune for either.
- **Prompt wording moved the judge more than any model change would have.** The verbatim prompt
  failed the AC1 gate outright; appending one paragraph clarifying the Not-Addressed versus
  Not-Supported distinction took AC1 from 0.595 to 0.720 and Not-Addressed recall from 0.296 to
  0.667. A third variant scored worse (AC1 0.699, MCC 0.489) and was rejected - so tune the
  prompt, measure every variant, and keep the losers on record.
- **Not-Supported support was 15 of 183.** The PPV figure rests on 25 predicted positives, of
  which 6 were correct. Small-n; widen the labelled set before treating 0.24 as precise.

> **If you fork this, you inherit a judge, a prompt and a threshold with no calibration of your
> own.** Given that c3 feeds c4 automatically, qualifying the judge is the highest-value thing you
> can do in this role. Until then, treat c4's corrections as unvalidated edits.

**Structured-output reliability is a selection criterion here specifically**, because verdicts are
parsed rather than read. The reference judge is a reasoning model that tends to emit malformed
JSON; f4/f5 wrap every call in graceful recovery (empty thinking budget -> retry with thinking
off; malformed -> reparse; otherwise `judge_error`, logged), and `f10_nothink_audit` exists as a
deterministic judge-health audit. Budget for that machinery, or choose a judge that does not need
it.

**The judge sees `bhc_text`.** A hosted judge falls under exactly the same governance as a hosted
generator - see section 5.

---

## 4. The judge in the f-series - and why it cannot check c3

| | |
|---|---|
| Decides | Tier-1 evaluation numbers (f4 faithfulness, f5 PDSQI-10, f6 incoming reference, f9 merge faithfulness, `eval_fact_presence`) |
| Build path | no - nothing served changes |
| Failure | a wrong evaluation number, visible and recoverable |
| Governing metric | same as section 3 |

**Caveat: f4 is not an independent check on c3.** Both bind the same
`faithfulness_validator` role, so the same model with different prompts sits on both sides of the
question. A good f4 faithfulness score does not establish that c3 judged well; it partly reflects
the same model's dispositions.

`config.py` binds ONE model per role, and there is no second judge role. So today there is no supported way to run f4 against a
different judge than c3 without adding a role. Two options:

- Treat Tier-1 numbers as **provisional and correlated with c3**, which is what the pipeline
  already does, and lean on `f10_nothink_audit` (deterministic) plus human review for independent
  signal.
- If you want a genuinely independent evaluation judge, add a second role to `MODELS` and bind
  f4/f5/f6/f9 to it. That is a code change, not a config change, and it doubles judge VRAM
  pressure - see section 7.

---

## 5. Hosted or local

The reference implementation is **local-only by DUA constraint**: PhysioNet bars sending
credentialed data to external APIs [11]. This section therefore applies to a fork running its own
hospital data, not to the reference build.

**Settle before sending any note text off-premises**, regardless of vendor marketing:

- **Zero data retention, no training on your data, no human review.** Verify each separately; they
  are different settings at most vendors, and many retain by default.
- **A signed BAA or local equivalent**, plus the processing region and sub-processor list.
- **Written confirmation, not inference.** PhysioNet's guidance is a useful template even when
  your data is not MIMIC: it warns that a claimed zero-retention guarantee may still be
  insufficient because of internal processing, logging or caching, that platform policies change
  without notice, and that a service whose practices cannot be fully verified should not be used
  [11].
- **De-identification is not a licence.** Treat note text as identifiable until your DPO rules
  otherwise in writing.
- **Both the generator and the judge see `bhc_text`.** There is no "safe" role to host.

**If you benchmark against MIMIC while running production on your own notes, the two corpora fall
under different deployment rules in the same project.** Keep the configurations separate and
record which model touched which corpus - `AuditRecord` already stamps `model_version`.

**Reproducibility cost.** Hosted endpoints are versioned by the vendor and change under you; the
reliability literature documents unannounced service degradations posing tangible risks to
downstream applications [12]. Pin the version string and re-validate on a held-out set after any
vendor change.

---

## 6. Consistency: measure the right thing

An intuitive response to unstable labelling is to select a "less stochastic" model. That is
usually the wrong target. Two quantities get confused:

| | test-retest reliability | item-to-item consistency |
|---|---|---|
| What varies | the run, input held fixed | the input wording, concept held fixed |
| Typical metric | 1-flip consistency, ICC, Fleiss kappa across repeated runs | label agreement across paraphrases of one concept |
| Fixed by | fixed seed, temperature 0, batch-invariant kernels | sharper class criteria, task decomposition, better model |
| Exposure here | low | **high** |

**Test-retest, for completeness.** Temperature 0 does not guarantee identical outputs, and the
cause is not the folk explanation. The Thinking Machines analysis attributes it to a lack of batch
invariance in reduction kernels: a prompt dispatched under a different dynamic batch size follows
a different reduction tree. They report 1,000 temperature-0 completions from Qwen3-235B producing
80 distinct outputs, diverging at token 103, and bit-identical outputs once batch-invariant
kernels are enabled, at roughly 61.5% throughput cost in the reference implementation [3]; a later
SGLang integration with CUDA graphs reduced that to about 34% [4]. Independent work on code review
found responses still varied to differing degrees at minimised temperature [5].

**MoE-specific.** MoE models add routing variability that can depend on server load and tiny
numerical perturbations, on top of the batch-size effect [10]. An MoE at temperature 0 therefore
has one nondeterminism source a dense model does not. This matters on a loaded multi-tenant
endpoint and largely disappears at batch size 1 on a single-user local server.

**Self-consistency voting is probably poor value.** One study of LLM annotation consistency found
99.7% mean self-agreement across 5 runs at temperature 0.7, with 99% of items identical across all
5 runs, and majority voting improving F1 by only 0.005 at 5x inference cost [6]. Ensembling does
not find a stable wrong answer, and the errors observed here are stable.

**The trap.** The judge-reliability literature documents a consistency-bias paradox: high
test-retest reliability often coexists with severe systematic bias, so a model that answers
identically every time can be identically wrong [13]. Reliability is not validity.

**Measure instead:** paraphrase groups - one clinical concept, several real phrasings drawn from
different notes - scored for label agreement within the group. No human labels needed, because
the grouping is the label. Report it per class; see the separability caveat below.

### A limit no model choice will move

An internal audit measured how far each taxonomy class is separable from the fact text alone, by
three independent methods (the taxonomy's own written examples, vocabulary mined from class
members, and neural embeddings). All three produced the same rank order.

- **High separability** (kNN neighbour purity 0.60-0.93): code status, chronic disease, implanted
  device, transient event, anatomy-altering surgery.
- **Low separability** (0.25-0.44): permanent condition, recurrent episode, sentinel event.

The low band is separated from chronic disease by a judgment about reversibility, recurrence or
prognostic weight that the note text does not make. **Where a boundary is not present in the
input, model capacity does not recover it.** Budget accordingly: sharpen the written criteria,
accept a known error rate, or merge the classes. Do not expect a model upgrade to move these.

---

## 7. The embedding model

| | |
|---|---|
| Decides | which prior admissions are shown as "similar" by `POST /search/similar` |
| Build path | **no.** Written by `a4_load_and_embed` (bulk) and `e2_embed_incoming` (incremental) to `admissions.embedding`; read by one API endpoint. It never touches stored clinical content |
| Failure | a poor neighbour list, immediately visible to the user and corrupting nothing |
| Governing metric | retrieval quality on YOUR OWN held-out pairs, not a leaderboard rank |
| Pin | model name AND version, recorded per stored vector (see the caution below) |
| Swap costs | `ALTER TABLE` + full re-embed + HNSW rebuild |

Because this role is off the build path, **efficiency-first selection is the correct posture**, and
the reference choice (a 1024-dimension general-purpose multilingual model, shortlisted on
medical-MTEB standing and sized to fit alongside everything else) is a reasonable default.

**MTEB rank is a shortlist, not a decision.** The headline number is an unweighted average across
task categories, and a model that clusters documents cleanly can rank passages poorly [18].
Two further caveats bite here: MTEB corpora are public, so models trained after 2023 may have seen
them, and the retrieval suite is dominated by general web data, making its scores a directional
signal at best for a clinical corpus [19]. Test candidates on a held-out slice of your own notes
before committing.

**The task here is document-to-document, not query-to-passage.** `/search/similar` compares one
admission narrative against others - symmetric, closer to clustering than to retrieval. MTEB
retrieval nDCG is an asymmetric short-query benchmark and transfers weakly.

**The domain-specific premium is small, measured on this data.** An internal comparison embedded
10,724 extracted facts with two biomedical models and one general-purpose control and measured how
well nearest neighbours agreed on the taxonomy class: mean k=10 purity **0.683 / 0.693 / 0.691** --
indistinguishable - with anomaly rankings correlating at Spearman 0.82-0.85. Benchmark a
general-purpose model before assuming a biomedical one is required.

### Cautions when swapping an existing embedding model

1. **Vectors from two models are not comparable, and nothing in the schema stops you mixing them.**
   `admissions` carries `embedding` and no `embedding_model` / `embedding_version` column. The
   model name is a module constant, and the bulk embed loop only processes rows
   `WHERE embedding IS NULL`. So changing the model and re-running re-embeds **nothing**, and the
   HNSW index silently ends up holding vectors from two incompatible spaces. Cosine distances
   across them are meaningless and no error is raised. **Before any swap: add a model-stamp
   column, or truncate `embedding` and re-embed the whole cohort.** Treat a version bump of the
   same model as a swap.
2. **The dimension is baked into the database.** `vector(1024)` plus an HNSW index means a change
   of dimension is a DDL migration and an index rebuild, not a config edit. If you only need to
   reduce storage, prefer a model supporting Matryoshka truncation over changing model.
3. **Similarity thresholds do not transfer.** Any cutoff tuned on one model must be re-tuned.
4. **Mind query/document asymmetry.** The reference model expects a prefix on the QUERY side.
   Current usage is document-to-document, so no prefix is correct. If you add free-text search --
   a clinician typing a phrase - the query side must get the prefix or retrieval degrades
   silently, with no error.
5. **Keep normalisation and the distance operator consistent.** The reference embeds with
   `normalize_embeddings=True` and indexes with `vector_cosine_ops`. Changing one without the
   other produces plausible-looking nonsense.
6. **Multilinguality is a real requirement at non-English sites** and is a reason to keep a
   multilingual model rather than substitute a stronger English-only one.
7. **Re-embedding is cheap; discovering a mixed index is not.** When in doubt, re-embed.

---

## 8. Operational constraints that limit model choice

These are not footnotes; they decide what is choosable.

- **One large model resident at a time.** `config.py` states the VRAM budget explicitly and
  requires runners to unload and clear the GPU between roles. A larger judge or generator is not
  only a VRAM cost - it changes what can be co-resident.
- **Stage order is arranged to minimise model swaps.** The generation model stays resident across
  b4 -> c1 -> d1 (with a CPU-only c2 in between); the judge enters at c3. Binding a *different*
  model to one of those roles inserts a swap and costs wall-clock time on every run. This is the
  main practical reason the per-role binding recommended in section 0 has not been exercised, and
  it should be weighed against the revalidation savings.
- **Run one judge job at a time.** f4 / f5 / f9 share the judge and one GPU.
- **A second judge role doubles judge pressure**, which is the operational half of the argument in
  section 4.

---

## 9. Recommendations

| Decision | Recommendation | Confidence |
|---|---|---|
| Model binding | Bind per role; change one role at a time. Weigh against added swap cost (section 8) | High - architecture already supports it |
| Generator architecture | Dense over MoE at matched *active* parameters | Moderate - literature is directional, not task-specific [1][2] |
| Generator size | Largest you can serve; evaluate against a hosted frontier model to establish the ceiling | Moderate |
| Quantization | No advice. Validate on your own data | Low - evidence contradictory [8][9][10] |
| Medical-specialised model | Test, do not assume. Same for biomedical embeddings | Moderate [14] + own measurement |
| Schema field order | Do not spend effort; measured to have no effect here | Moderate - one admission, key order uninspected |
| Prompt work vs model upgrade | Prompt work first | Moderate [7] |
| Judge at c3 | Build a clinician-labelled calibration set before trusting c4's automatic corrections | High |
| Judge lineage | Keep generator and judge in different families; check the BACKBONE not the model name, and re-check when the GENERATOR changes | High [16][17] |
| Judge calibration | Qualify on VeriFact-BHC first (section 3); build your own labelled set second | High [20] |
| Judge prompt | Tune and measure it before changing model - prompt wording moved AC1 from 0.595 to 0.720 here | High - own measurement |
| f4 as a check on c3 | Do not treat it as independent - same bound role | High |
| Selecting for determinism | Do not. Measure paraphrase-group agreement instead | High [6][13] |
| Self-consistency voting | Low expected value against stable errors | Moderate [6] |
| Embedding model | Efficiency-first is correct; shortlist on MTEB, decide on your own held-out pairs | Moderate [18][19] + own measurement |
| Embedding swap | Stamp the model per vector or re-embed everything; never mix spaces | High - schema does not protect you |
| Hosted anything | Governance decides. Verify retention, training and human-review settings independently; pin the version | High [11][12] |

---

## 10. Deployment caution

This pipeline produces physician-facing summaries. Two constraints hold regardless of model
choice.

**The judge is a triage signal, not a finding.** Not-Supported PPV of roughly 0.24 means fully
automated gating on judge output is inappropriate; human-reviewed triage is the supported use.
This is consistent with the general finding that raw agreement overstates chance-corrected
discrimination [13]. Note that c4 *does* act on judge output automatically - see section 3.

**Clinical deployment is a regulatory question, not an accuracy threshold.** Rigorous validation,
documentation of intended use, and adherence to applicable regulatory requirements are necessary
for any clinical deployment of an LLM-based system [14]. Improving the model does not discharge
that.

---

## References

1. Sanseviero et al., *Mixture of Experts Explained*, Hugging Face, 2023.
   https://huggingface.co/blog/moe (summarising Switch Transformers and ST-MoE findings)
2. *Mixture of Parrots: Experts improve memorization more than reasoning*, arXiv:2410.19034.
3. He et al., *Defeating Nondeterminism in LLM Inference*, Thinking Machines Lab, 2025.
   https://thinkingmachines.ai/blog/defeating-nondeterminism-in-llm-inference/
4. *Reproducibility is the New Copyleft*, arXiv:2606.03019 (reports the SGLang overhead figure)
5. *Measuring Determinism in Large Language Models for Software Code Review*, arXiv:2502.20747
6. *When Do LLMs Actually Help With Data Quality?*, arXiv:2608.18158
7. Tenckhoff et al., *LLMStructBench*, arXiv:2602.14743, as summarised in
   *The Structured Output Benchmark*, arXiv:2604.25359
8. *Mixture of Quantized Experts (MoQE)*, arXiv:2310.02410
9. *Efficient Quantization of Mixture-of-Experts with Theoretical Generalization Guarantees*,
   arXiv:2604.06515; *MiLo: Efficient Quantized MoE Inference*, MLSys 2025
10. *Verifying LLM Inference to Detect Model Weight Exfiltration*, arXiv:2511.02620;
    *Value-and-Structure Alignment for Routing-Consistent Quantization of MoE*, arXiv:2606.05688
11. PhysioNet, *Use of MIMIC Data with Large Language Models and Online Services*, 24 Sept 2025.
    https://physionet.org/news/post/llm-responsible-use/
12. *How to Calibrate Your LLM Judge With Human Annotations*, Galileo, 2026
13. *Reliability without Validity: A Systematic, Large-Scale Evaluation of LLM-as-a-Judge Models
    Across Agreement, Consistency, and Bias*, arXiv:2606.19544
14. *Automatic Extraction of Structured Information from Brain MRI Reports Using an Open-Weight
    Large Language Model*, arXiv:2606.07721 (citing Zhu et al. and Liu et al.)
15. Tam et al., *Let Me Speak Freely? A Study on the Impact of Format Restrictions on Performance
    of Large Language Models*, EMNLP Industry Track 2024, arXiv:2408.02442
16. *Play Favorites: A Statistical Method to Measure Self-Bias in LLM-as-a-Judge*,
    arXiv:2508.06709; *Self-Preference Bias in Rubric-Based Evaluation*, arXiv:2604.06996
17. *Self-Preference Bias in LLM-as-a-Judge*, arXiv:2410.21819; Panickssery et al.,
    *LLM Evaluators Recognize and Favor Their Own Generations*, arXiv:2404.13076
18. Modal, *Top embedding models on the MTEB leaderboard*, 2026.
    https://modal.com/blog/mteb-leaderboard-article
19. *Evaluating Embedding Models in 2026*, futureagi.com, May 2026;
    *8 Embedding Models Compared for Production RAG*, tensoria.fr, May 2026
20. Chung et al., *VeriFact*, NEJM AI 2026;3(1); arXiv:2501.16672;
    https://github.com/philipchung/verifact (MIT licence). Dataset: VeriFact-BHC, PhysioNet

Sources retrieved 2026-08-29. Items 4, 7, 9, 10, 12, 14, 16, 18 and 19 were read via search
summaries rather than in full; verify before citing them in a publication.
