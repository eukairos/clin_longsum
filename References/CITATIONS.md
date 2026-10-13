# Citations


---

## 1. Data

### 1.1 MIMIC-IV-Ext-BHC (the source dataset)

Aali, A., Van Veen, D., Arefeen, Y., Hom, J., Bluethgen, C., Reis, E. P., Gatidis, S.,
Clifford, N., Daws, J., Tehrani, A., Kim, J., & Chaudhari, A. (2025). *MIMIC-IV-Ext-BHC: Labeled
Clinical Notes Dataset for Hospital Course Summarization* (version 1.2.0). PhysioNet.
RRID:SCR_007345. <https://doi.org/10.13026/5gte-bv70>

Aali, A., Van Veen, D., Arefeen, Y. I., et al. (2024). A dataset and benchmark for hospital course
summarization with adapted large language models. *Journal of the American Medical Informatics
Association.* <https://doi.org/10.1093/jamia/ocae312>

### 1.2 MIMIC-IV-Note 

Johnson, A., Pollard, T., Horng, S., Celi, L. A., & Mark, R. (2023). *MIMIC-IV-Note: Deidentified
free-text clinical notes* (version 2.2). PhysioNet. <https://doi.org/10.13026/1n74-ne17>

### 1.3 MIMIC-IV 

Johnson, A., Bulgarelli, L., Pollard, T., Gow, B., Moody, B., Horng, S., Celi, L. A., & Mark, R.
(2024). *MIMIC-IV* (version 3.1). PhysioNet. <https://doi.org/10.13026/kpb9-mt58>

Johnson, A. E. W., Bulgarelli, L., Shen, L., et al. (2023). MIMIC-IV, a freely accessible
electronic health record dataset. *Scientific Data*, 10(1), 1.
<https://doi.org/10.1038/s41597-022-01899-x>

### 1.4 PhysioNet 

Pollard, T., Moody, B. E., Lehman, L., Gow, B., Fernandes, C., Xie, C., Johnson, A., Mark, R. G.,
& Heldt, T. (2026). PhysioNet as a global platform for biomedical research. *Nature Health.*
<https://doi.org/10.1038/s44360-026-00096-z>


---

## 2. Methods and instruments adapted in this project

### 2.1 SynthMedic — source of the vendored faithfulness validator

Adapted in `src/vendor/faithfulness_validator.py`: the 4-step LLM faithfulness validation of
§3.4.1. Facts in the discharge summary must be traceable back to the generated admission H&P and progress notes.

Grazhdanski, G., Vasilev, V., Vassileva, S., Taskov, D., Antova, I., Koychev, I., & Boytcheva, S.
(2025). SynthMedic: Utilizing large language models for synthetic discharge summary generation,
correction and validation. *Journal of Biomedical Informatics*, 170, 104906.
<https://doi.org/10.1016/j.jbi.2025.104906> PMID: 40962129. Retrieved via PubMed.

```bibtex
@article{grazhdanski2025synthmedic,
  author  = {Grazhdanski, Georgi and Vasilev, Vasil and Vassileva, Sylvia and
             Taskov, Dimitar and Antova, Izabel and Koychev, Ivan and Boytcheva, Svetla},
  title   = {{SynthMedic}: Utilizing large language models for synthetic discharge summary
             generation, correction and validation},
  journal = {Journal of Biomedical Informatics},
  volume  = {170},
  pages   = {104906},
  year    = {2025},
  month   = oct,
  doi     = {10.1016/j.jbi.2025.104906},
  pmid    = {40962129}
}
```



### 2.2 PDSQI-9 — the rubric behind `f5_pdsqi.py`

Croxford, E., Gao, Y., Pellegrino, N., Wong, K. K., Wills, G., First, E., Schnier, M., Burton, K.,
Ebby, C. G., Gorskic, J., Kalscheur, M., Khalil, S., Pisani, M., Rubeor, T., Stetson, P., Liao, F.,
Goswami, C., Patterson, B., & Afshar, M. (2025). *Development and Validation of the Provider
Documentation Summarization Quality Instrument for Large Language Models.* arXiv.
<https://doi.org/10.48550/ARXIV.2501.08977>

Licensed **CC BY 4.0** 
**Statement of changes (required by CC BY) :** this project uses a
**10-item adaptation of the 9-item PDSQI**, adding a *temporal coherence* item on the grounds that
a longitudinal multi-admission summary must order events correctly. 

### 2.3 Faithfulness-metric selection

Adams, G., et al. (2023). *A Meta-Evaluation of Faithfulness Metrics for Long-Form Hospital-Course
Summarization.* arXiv. <https://arxiv.org/abs/2303.03948>


### 2.4 Models

All served locally through Ollama; none redistributed by this project. 

- **Ornith-1.0-35B** (`ornith:35b`, generation) — MIT, © 2026 deepreinforce.ai.
  <https://huggingface.co/ornith-ai/Ornith-1.0-35B> · post-trained on `Qwen/Qwen3.5-35B-A3B`
  (Apache-2.0), inferred from the `qwen3_5_moe` architecture rather than stated per-member on the
  model card.
- **DeepSeek-R1 distill 32B** (`deepseek-r1:32b`, judge) — MIT; Qwen2.5-32B base (Apache-2.0).

### 2.5 Embedding model

`Snowflake/snowflake-arctic-embed-l-v2.0` — **Apache-2.0**, 568M parameters, xlm-roberta
architecture. <https://huggingface.co/Snowflake/snowflake-arctic-embed-l-v2.0>

Yu, P., et al. (2024). *Arctic-Embed 2.0: Multilingual Retrieval Without Compromise.* arXiv.
<https://arxiv.org/abs/2412.04506>

---




