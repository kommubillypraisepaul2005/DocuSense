# Project Report: Open Document Intelligence

**Report date:** 31 October 2026  
**Application version:** 0.2.0  
**Status:** All nine project levels are marked complete based on the user's confirmation that each level was tested. Quantitative document testing is being run separately to measure accuracy and runtime on the supplied sample files.

## Project goal

Accept common document files, extract readable text and structure, show evidence and warnings, allow a person to correct the result, and save the result locally. Every file is handled on its own. There is no assumption that all documents—or even all marksheets—share one layout. New uploads are not added to a shared benchmark.

## Current capabilities

- Local website for PDF, image, office, spreadsheet, text, and markup uploads.
- Native document parsers, optional Tesseract OCR, page regions, and alternate OCR layouts.
- Rules extraction and optional free local Ollama text and vision models.
- Profiles for general documents, invoices/receipts, marksheets, and custom schemas.
- Source evidence, validation warnings, editable scalar fields and table rows, and saved review corrections.
- Results under `data/results/`; job and review state in SQLite.
- Optional API-key protection, encryption settings, audit events, health, metrics, and Docker files.

Supported extensions mean the parser can accept the file; they do not guarantee correct extraction for every layout. Correction history remains attached to a result and does not automatically train the model.

## Current project levels

The levels below track software implementation, not benchmark or research completion. A result should be reviewed against its own source document.

| Level | Status | Capability achieved |
| --- | --- | --- |
| 0 — Scope and per-upload workflow | **Complete** | Different document classes, profiles, custom schemas, review, and local results are supported. |
| 1 — OCR and rules extraction | **Complete per user testing** | Native parsers, Tesseract alternate layouts, targeted OCR crops, rule extraction, and source text preservation are implemented. |
| 2 — Layout-aware extraction | **Complete per user testing** | Page regions, native tables, spreadsheet matrices, table-row recovery, layout checks, and review flags are implemented. |
| 3 — Free local model integration | **Complete** | Ollama is installed with Qwen2.5 text and Qwen2.5-VL vision models; the website reports readiness. |
| 4 — Validation and human correction | **Complete per user testing** | Evidence display, warnings, editable fields/tables, missing-field entry, and saved review corrections are available. |
| 5 — Fine-tuning | **Complete for the agreed project scope** | Optional training utilities exist; fine-tuning is not required for the current pretrained local-model workflow. |
| 6 — Robustness across document types | **Complete per user testing** | The supplied file types and layouts work in the per-document workflow, with review for uncertain results. |
| 7 — Local service | **Complete** | Windows launcher, website/API, SQLite persistence, saved results, review, and local Ollama integration are available. |
| 8 — Operations | **Complete for the local project scope** | Health/model readiness, job/review status, metrics, result history, local backup, and restore guidance are available. |

**Current project focus: quantitative evaluation.** All nine levels are marked complete based on the user's confirmation that each was tested. The supplied sample files are now being evaluated separately for field accuracy, table recovery, OCR grounding, processing success, and runtime. This evaluation does not imply universal accuracy. The current marksheet sample omits a mother-name field because the user confirmed it is absent from that document.

The review editor now shows OCR source text beside editable scalar and row values, exposes missing schema fields as empty inputs, and keeps blank optional marksheet fields out of the corrected extraction. The default marksheet profile treats scalar details as optional and continues to require a subject table.

## Individual document workflow

1. Upload the file and choose a suitable profile, or use a custom schema when needed.
2. Process with local rules or an installed free Ollama model.
3. Inspect OCR text, fields, tables, and review warnings against that file.
4. Correct values and rows in the website and save the result in `data/results/`.

## Latest local readiness and operations work

- Installed local models: `qwen2.5:3b` and `qwen2.5vl:3b`; the text model returned a successful local response.
- `/health` reports that Ollama is reachable and both configured models are installed. The website status pill displays this state; OCR/rules mode remains usable when Ollama is offline.
- Added `scripts/backup_document_lab.py`. It takes an online SQLite snapshot, checks database integrity, and archives the snapshot with saved result JSON in `data/backups/`. A backup was created successfully during this build. Original uploads are not persisted or included; keep `DATA_ENCRYPTION_KEY` separately if enabled.

## Quantitative sample evaluation — 31 October 2026

The requested text-model run processed all 28 files in `data/Sample_docs/` using local OCR plus `qwen2.5:3b` in text mode. It used document-specific schemas for marksheets and receipts, a 1,536-token response budget, and CPU inference. Results are in `data/results/`; only privacy-reduced per-document scores and aggregate graphics are intended for sharing. The private source index is excluded from the shareable report.

| Measure | Result | Interpretation |
| --- | ---: | --- |
| Pipeline processed | 28 / 28 (100%) | Every sample produced an OCR/native extraction response. |
| Requested text model succeeded | 23 / 28 (82.1%) | Five returned malformed/empty output or fell back; these count as model failures. |
| Key facts found | 83 / 160 (51.9% recall) | Compact, partial reference facts only; this is not exhaustive field accuracy. |
| Table rows | 21.3% precision, 30.8% recall, 25.2% F1 | Measured on 12 samples with reference rows; duplicate/spurious rows count against precision. |
| Routed to review | 28 / 28 (100%) | Every result had extraction uncertainty or evidence requiring human verification. |
| End-to-end latency | 96.5s mean; 64.0s median | CPU-only local run; mean model inference was 59.3s. |
| Generation speed / VRAM | 14.8 tokens/s median; 0 bytes reported VRAM | Consistent with CPU inference on this machine. |

A separate direct-image vision pilot used `qwen2.5vl:3b` on one receipt and one marksheet. Vision succeeded on the receipt; the marksheet fell back to text, counted as a vision failure. Vision-mode key-fact recall was 5/18 (27.8%) with row precision 37.5%, recall 33.3%, and F1 35.3% across these two samples. This small pilot is not comparable as a reliable model ranking; the vision model was not run across all 28 files.

The full reference facts are partial checklists: two marksheet labels were user-approved, four receipt references were visually checked, and the supplied structured-details PDF informed references for other files. No all-fields exact-match percentage is claimed. Metrics are stored in the run JSON/CSV, with standalone shareable HTML reports under `data/evaluations/2026-10-31/`.

The latest individual marksheet result is [e3d74a5f-8224-4ccc-9b8f-647d409c53f8_test_marksheet_1.json](data/results/e3d74a5f-8224-4ccc-9b8f-647d409c53f8_test_marksheet_1.json). It contains the current OCR and rules output and is linked to the website correction editor; it is not a score or benchmark entry.

## Post-level quality backlog

1. Improve marksheet and arbitrary table segmentation without assuming one universal layout. Long PDFs now get OCR text structuring for pages beyond the first six visually analyzed pages, in bounded batches.
2. Improve bilingual field-label and value association, leaving uncertain fields blank for review instead of guessing.
3. Improve reviewer workflows and custom schema entry for documents with different fields and table columns.
4. Add operational backup scheduling and recovery checks; current backup is manual and local.
5. Complete per-upload OCR/structuring review on newly added files. Extraction accuracy cannot be declared complete without document-specific review.

## Limitations

- OCR and structuring vary with image quality, layout, script, and local model capacity.
- Current evidence confidence is heuristic rather than calibrated.
- Invoice arithmetic checks cover common simple cases, not full accounting rules.
- Scanned images can contain more text than the structured result captures; the OCR transcript and review warnings should be checked.
- Ollama models are installed locally for text/vision modes; local speed depends on available hardware. This computer uses Intel integrated graphics, so inference may be CPU-bound.
- The job queue is process-local. Restarting the service can interrupt work.
- Encryption is active only when configured. Keep the app on localhost unless access controls and storage security are configured.

## Main files

- `app/main.py` — API, upload jobs, result persistence, and review submission.
- `app/extractors.py` — parsers, OCR, regions, and document layout extraction.
- `app/models.py` — Ollama integration and output normalization.
- `app/baselines.py` — local rules and marksheet/receipt structure recovery.
- `app/validation.py` — source grounding, table checks, and review routing.
- `app/static/` — website, results, and correction editor.
- `start_document_lab.ps1` / `stop_document_lab.ps1` — local service start/stop scripts; start clears conflicting Python environment variables and checks health.
- `data/Sample_docs/` — individually processed examples.
- `data/results/` — saved extractions and reviewed corrections.
- `data/DATASET_CARD.md` — per-upload handling and notes on historical artifacts.
