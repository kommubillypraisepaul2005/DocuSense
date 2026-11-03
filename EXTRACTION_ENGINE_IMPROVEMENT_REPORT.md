# Extraction Engine Improvement Report

**Project:** Open Document Intelligence  
**Evaluation date:** 2026-10-31  
**Models:** local Ollama `qwen2.5:3b`; text mode; CPU-only; 1,536-token output cap  
**Scope:** same 28 sample documents and the existing partial-reference scorer; no new samples were added to historical benchmark manifests.

## Executive result

The updated engine produces valid model outputs more often and recalls more of the reference facts. Table-row F1 did not improve in the final run, and mean latency increased. The results support targeted gains in response validity and key-fact recall, not a claim of universal extraction accuracy or runtime improvement.

| Metric | Baseline | Final | Change |
|---|---:|---:|---:|
| Structured model responses | 23/28 (82.1%) | 25/28 (89.3%) | +2 documents |
| Key-fact recall | 83/160 (51.9%) | 93/160 (58.1%) | +6.2 percentage points |
| Table-row precision | 21.3% | 23.5% | +2.2 percentage points |
| Table-row recall | 30.8% | 24.6% | −6.2 percentage points |
| Table-row F1 | 25.2% | 24.1% | −1.1 percentage points |
| Mean end-to-end latency | 96.5 s | 118.1 s | +22.4% slower |
| Median end-to-end latency | 64.0 s | 64.4 s | +0.4 s |
| Mean model inference time | 59.3 s | 91.5 s | +54.3% slower |

Key-fact recall is measured against 160 facts from compact, partial references; it is not exhaustive document accuracy. Table metrics are measured on the subset with referenced rows. Every nonempty predicted row, including spurious rows, contributes to precision/recall. All 28 documents remain subject to human review.

## Root-cause analysis

1. **Missing facts and malformed responses:** The previous text path sent one large OCR string and broad instructions to a small model, then expected one complete JSON response. Long inputs and exhaustive field/table output could exceed the output budget or produce malformed JSON. The old parser rejected such responses without a structured retry.
2. **Table reconstruction:** Multiple same-name/same-page table reads were reduced to whichever had more populated cells. That could discard unique rows. Generated table labels and columns could also diverge from custom schema names.
3. **OCR conflicts:** Alternate OCR layouts were retained for evidence validation, but the text passed to the model was deduplicated by keeping the first matching line. Conflicting label/value reads were not exposed as a first-class review item.
4. **Entity association:** A long generic instruction set placed many tasks in one prompt. Important owner/issuer/guardian/merchant/cashier role distinctions and label-to-value associations competed with long table content.
5. **Runtime:** OCR and model inference are CPU-only on the current machine. The previous single-call approach also spent time on responses that failed JSON parsing; the added repair attempt can recover some outputs but adds work on unrecovered cases.

## Implemented changes

- **`app/models.py`**
  - Replaced the broad prompt with shorter, schema-first extraction instructions for exact evidence, roles, bilingual labels, table rows, and conflict handling.
  - Added a dynamic JSON Schema to Ollama's `format` parameter. It constrains the response envelope and, for custom schemas, the requested scalar keys and table names.
  - Added one bounded repair attempt after empty, incomplete, or malformed model output. JSON normalization still validates and repairs the envelope, fills absent requested fields, aligns known columns, pads short rows, and preserves unexpected trailing cells under explicit `unmapped_column_*` headers.
  - Missing values keep the editor-compatible empty-string value and gain `extraction_status: "missing"`.
  - Same-page table reads merge exact duplicate rows and their evidence; distinct rows are retained. Competing rows with the same first-cell identity are retained and marked in `row_conflicts`.
- **`app/processor.py`**
  - Detects different values read under the same OCR label across the primary and alternate layouts. It passes the conflict summaries to the model and adds an unresolved OCR conflict to the review reasons.
  - Preserves the original OCR/native text and existing result envelope. New conflict metadata is additive.
- **`app/validation.py`**
  - Routes conflicting same-identity table rows to human review with row references.
- **`scripts/run_sample_docs_evaluation.py`**
  - Added `--base-url` so evaluation can use an isolated local app instance while leaving the user's existing website running.
- **`tests/test_pipeline.py`**
  - Added regression coverage for OCR disagreement capture, custom-schema JSON Schema creation, missing-value status, table name aliasing, exact-row merging, conflict retention, and bounded response repair.

No endpoint was removed. Existing top-level result and table keys remain; new metadata is additive. The original document handling and local-only model architecture are unchanged. No paid APIs, automatic training, or benchmark-manifest updates were introduced.

## Controlled evaluation

- **Baseline run:** `data/evaluations/2026-10-31/text_20261031T061044Z/`
- **Intermediate run (before constrained JSON Schema):** `data/evaluations/2026-10-31/text_20261031T083227Z/`
- **Final run:** `data/evaluations/2026-10-31/text_20261031T092518Z/`
- All runs used the same 28 files, text mode, `qwen2.5:3b`, CPU inference, and 1,536-token output cap. The scorer used the same existing 160 partial key-fact references and row references.
- Final run: 28/28 requests completed; 25 model outputs succeeded and 3 failed or fell back. Baseline: 23 model successes and 5 failures/fallbacks.
- Baseline failures were S04, S05, S06, S19, S26. Final failures were S17, S19, S26. The constrained response recovered S04, S05, and S06 but did not recover S17; S19 and S26 remained failures. S27 also failed in the intermediate run but succeeded in the final run.
- Final model failures are not scored as if a model had extracted the facts. Documents without references remain unscored.

The final table score is slightly lower than baseline, even though precision increased: the model emitted fewer correctly matched rows overall. This remains an open quality issue. The runtime increase is driven in part by CPU-only inference and long-form outputs; response validity alone did not make the system faster.

## Verification

- `python -m unittest discover -s tests -v`: **11 tests passed**.
- `python -m py_compile` on modified Python modules, evaluation script, and tests: **passed**.
- Final score files and privacy-reduced HTML report were generated by the project scripts. The HTML is intended for sharing; the private source index and full JSON results should stay local.

## Artifacts

- Final chart report: `data/evaluations/2026-10-31/text_20261031T092518Z/evaluation_report.html`
- Final metrics: `data/evaluations/2026-10-31/text_20261031T092518Z/scored_summary.json`
- Per-document metrics: `data/evaluations/2026-10-31/text_20261031T092518Z/document_scores.csv`
- Final run summary: `data/evaluations/2026-10-31/text_20261031T092518Z/run_summary.json`

