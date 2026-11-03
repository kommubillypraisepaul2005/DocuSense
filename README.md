# Open Document Intelligence

A local-first document understanding service. It extracts searchable text and tables, can use locally hosted text and vision models, returns structured results with evidence, validates supported checks, and routes uncertain outputs to human review. Ollama is optional; the rules-only path works without a model, and there are no paid API dependencies.

## Supported input formats

- PDF, including scanned pages (PyMuPDF plus optional Tesseract OCR)
- PNG, JPEG, TIFF, BMP, WebP (OCR and local vision)
- DOCX, XLSX/XLSM, PPTX and OpenDocument ODT/ODS/ODP
- Legacy DOC/XLS/PPT when LibreOffice is installed and available on PATH (converted locally to PDF)
- CSV, RTF, TXT, Markdown, JSON, XML, HTML, and LOG

Password-protected files are unsupported. Excel parsing is capped at 5,000 rows and 50 columns per sheet; PDF parsing is capped at 200 pages; images are capped at 40 million pixels; uploads are capped at 30 MB by default. Up to six PDF pages or embedded office images are sent to the vision model per extraction. For longer PDFs, remaining pages are OCR-structured in bounded local text-model batches, with batch status and duration included in the result. All PDF pages are text-extracted. LibreOffice conversion is optional and is required only for legacy binary Office files.

## Run locally on Windows

1. Install Python 3.11 or newer. Install Tesseract OCR if scanned-page and image OCR is needed. Install LibreOffice if you need `.doc`, `.xls`, or `.ppt` support.
2. In PowerShell, create and activate a virtual environment, then install dependencies:

   ```powershell
   Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
   Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
   py -3.11 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env`. For local development, `APP_ENV=development` and an empty `APP_API_KEY` are allowed. Set a long random `APP_API_KEY` and generate a Fernet key for encrypted results:

   ```powershell
   .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
   .\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   Put those values in `.env` and preserve the Fernet key because encrypted data cannot be read without it.
4. Install Ollama and pull the default free-to-run local models:

   ```powershell
   ollama pull qwen2.5:3b
   ollama pull qwen2.5vl:3b
   ```

   Qwen2.5-VL supports image input; its Ollama library entry lists the 3B variant at about 3.2 GB. Actual RAM/VRAM needs vary by context and runtime. [Ollama Qwen2.5-VL model library](https://ollama.com/library/qwen2.5vl). Review the model license that applies to the exact checkpoint and intended use; the Qwen2.5-VL 3B model card currently lists Apache-2.0. [Qwen2.5-VL-3B model card](https://huggingface.co/Qwen/Qwen2.5-VL-3B-Instruct)

5. Start the local website from PowerShell with `powershell -ExecutionPolicy Bypass -File .\start_document_lab.ps1`. The launcher clears conflicting `PYTHONHOME`/`PYTHONPATH` settings, starts the local service in the background if needed, checks `/health`, and opens `http://127.0.0.1:8000`. Stop a service started by the launcher with `powershell -ExecutionPolicy Bypass -File .\stop_document_lab.ps1`.

If you use non-English documents, install the corresponding Tesseract language data and set `OCR_LANGUAGE` (for example, `eng+hin`). For Ollama with a different host or model tag, change `OLLAMA_BASE_URL`, `OLLAMA_TEXT_MODEL`, or `OLLAMA_VISION_MODEL` in `.env`.

The website's service indicator reports whether Ollama is reachable and whether the configured text and vision models are installed. The local rules/OCR mode remains available when Ollama is offline. Create a database and results backup with `.\.venv\Scripts\python.exe .\scripts\backup_document_lab.py`; it writes a ZIP under `data/backups/`. The backup contains the SQLite database and saved result JSON, not original uploads. If `DATA_ENCRYPTION_KEY` is configured, preserve that key separately. To restore, stop the app, extract the ZIP into the project root (preserving its `data/` paths), then start the app again.

The browser app supports general extraction, invoice/receipt and marksheet field presets, custom JSON schemas, drag-and-drop uploads, an editable review form for scalar fields and table rows, and recent saved results. It renders all extracted tables, including receipt items and marksheet subjects, and shows an empty table when expected rows are missing. Use **Review and correct** to edit or fill missing requested fields, compare scalar and row OCR evidence beside editable values, add or remove table rows, and save a review note. Each completed extraction and saved correction is written as readable JSON under `data/results/`; use the Download JSON or View actions in the page to retrieve it. Original uploaded documents remain in memory only.

`OCR_CROSSCHECK=true` runs multiple Tesseract layout passes on table-like scans and re-reads detected quantity-led receipt rows in narrow crops. OCR/rules fill blank model values and recover missed receipt items and coded marksheet subject rows. Tables parsed directly from PDF, Office, spreadsheet, and CSV inputs supplement the model output. Non-empty disagreements are reported for human review rather than silently overwritten. Receipt currency symbols are preserved as printed; ISO codes are assigned only when explicit or unambiguous, while `$`, `¥`, and other ambiguous symbols require review. The browser result includes the OCR text read from the source for inspection.

## Docker

Copy `.env.example` to `.env`, set `APP_ENV=production`, set a strong `APP_API_KEY`, and set `DATA_ENCRYPTION_KEY`. Production startup refuses to run without both keys. Then run:

```powershell
docker compose up --build -d
docker compose exec ollama ollama pull qwen2.5:3b
docker compose exec ollama ollama pull qwen2.5vl:3b
```

The API is bound to `127.0.0.1:8000`; Ollama is only exposed to the Compose network. The Docker image includes English Tesseract. Hardware acceleration for Ollama depends on Docker and host GPU configuration.

## Extraction modes

- `rules`: OCR/parser text plus transparent invoice regular-expression baseline
- `text`: local text LLM on parser/OCR output
- `vision_zero_shot`: VLM receives document images without OCR text
- `vision`: prompted VLM plus OCR/parser text
- `auto`: combines text and visual input when visual pages exist; uses text model for spreadsheet/text-only sources

An optional `schema_json` form field can specify fields, for example:

```json
{"document_type":"invoice","fields":{"invoice_number":"string","date":"date","vendor":"string","subtotal":"number","tax":"number","total":"number"}}
```

For table extraction, include requested columns, for example: `{"document_type":"marksheet","fields":{"student_name":"string","roll_number":"string"},"tables":[{"name":"subjects","columns":["subject_code","subject","theory_marks","practical_marks","total_marks","grade"]}]}`.

The model is instructed to return values with short evidence quotes and page numbers. Grounding matches those quotes to OCR/native text regions and returns normalized bounding boxes where available. Office tables retain row/column matrices and spreadsheet sheet names. Arithmetic checks are performed in Python, not delegated to the model.

## API

Every endpoint other than `/`, `/health`, and OpenAPI docs requires `X-API-Key` when `APP_API_KEY` is configured.

- `POST /documents`: queue in-process async extraction; multipart `file`, optional `schema_json`, query `mode`
- `POST /extract`: synchronous multipart extraction
- `GET /jobs/{id}` and `GET /results/{id}`: job status and result
- `GET /saved-results` and `GET /saved-results/{filename}`: list and download JSON result files in `data/results/`
- `POST /validate`: revalidate extraction JSON with source segments
- `GET /reviews` and `POST /reviews/{id}`: review queue and human decision/correction record
- `DELETE /jobs/{id}`: remove a completed or failed result
- `GET /metrics`: aggregate job and review counts
- `GET /health`: liveness

Job state and results live in SQLite. Uploaded originals stay in memory and are not persisted. Result and review JSON are encrypted at rest only when `DATA_ENCRYPTION_KEY` is set. The audit log stores event names and job IDs, not document text. Configure TLS and network access controls if exposing the service beyond localhost.

The async queue is process-local: jobs interrupted by a server restart are marked failed and must be resubmitted. This keeps source documents out of persistent storage; multi-worker deployment would need a durable encrypted object store and queue.

## Per-upload review workflow

Documents vary in layout and in the fields they contain, so the application does not assume that a marksheet or another document class has one universal structure. Process each file with the closest profile, or provide a custom schema when you know the fields you need. Inspect the OCR text and extracted tables, correct that upload in the review editor, and save its result under `data/results/`.

Review corrections are saved with that result. They do not automatically train the model or become a reusable template. Treat every new upload as its own document unless you explicitly create and approve a reusable schema for a particular workflow. The existing evaluation scripts and labeled marksheet files are historical development artifacts; do not add new uploads to benchmark manifests. No accuracy claim is made for all documents or all layouts.

The optional `scripts/evaluate.py` and `scripts/compare_models.py` utilities remain available for controlled engineering work, but benchmark creation is not part of the application's document-by-document workflow.

## LoRA/QLoRA fine-tuning

Training is optional and requires a CUDA GPU. Install a CUDA-compatible PyTorch build first, then `pip install -r training-requirements.txt`. `scripts/finetune_vlm.py` trains a Qwen2.5-VL LoRA adapter (or 4-bit NF4 QLoRA) from image/answer JSONL records and a separate validation set. It writes trainable parameter count, training time, peak GPU memory, learning rate, and resolution metadata beside the adapter. Fine-tuning is deferred; corrections saved from the website are not training data by default.

Example JSONL line:

```json
{"images":["images/invoice-001.png"],"prompt":"Extract invoice fields as JSON and cite exact evidence.","answer":"{\"document_type\":\"invoice\",\"fields\":{...}}"}
```

Run with `python scripts/finetune_vlm.py --train-jsonl data/train.jsonl --validation-jsonl data/validation.jsonl --qlora`. Fine-tuning is optional and is not part of the current per-upload workflow. The service selects the served Ollama model via environment variables; converting a trained HF adapter to a serving format is a separate step dependent on the target runtime.

## Current limitations

Confidence scores are evidence-match heuristics, not calibrated probabilities. Model extraction quality, OCR quality, and table structure vary with scans, language data, model size, and hardware. DOCX/PPTX images are passed as visual inputs but their original page/slide layout may not be retained in region coordinates. Validation is deterministic for supported invoice arithmetic, not a full accounting rules engine. Review decisions are recorded, but they do not automatically retrain a model.

## Implementation status and verification

### Completion against the supplied 9-level project plan

The project is being built as a document-by-document tool. Different files can have different fields and table layouts, including within marksheets. There is no universal document benchmark or accuracy gate in the current workflow. Completion refers to software capabilities; every extracted result still needs review when its warnings or evidence indicate uncertainty.

| Level | Status | Capability and evidence |
| --- | --- | --- |
| 0 — Scope and per-upload workflow | Complete | Common file formats, profiles, custom schemas, individual result review, and local saving are available. |
| 1 — OCR and rules extraction | Complete per user testing | Native parsers, Tesseract alternate layouts, targeted OCR crops, rules, and source text preservation are implemented. |
| 2 — Layout-aware extraction | Complete per user testing | Page regions, native tables, spreadsheet matrices, scanned row recovery, layout checks, and review routing are implemented. |
| 3 — Free text and vision models | Complete for the local pilot | Ollama is installed with the configured Qwen2.5 3B text and Qwen2.5-VL 3B vision models. The UI now shows Ollama/model readiness. Speed and extraction quality depend on the computer and source; cloud services are not configured. |
| 4 — Validation and human correction | Complete per user testing | Evidence, warnings, editable fields and rows, missing-field entry, and saved corrections are available. |
| 5 — Fine-tuning | Complete for the agreed project scope | Optional training utilities exist; fine-tuning is not required for the current pretrained-model workflow. |
| 6 — Robustness across documents | Complete per user testing | The supplied formats and layouts are handled by the per-document workflow, with review for uncertain results. |
| 7 — Local service | Complete for the local pilot | The Windows launcher, FastAPI UI/API, SQLite job state, result persistence, review editor, and Ollama model readiness indicator work locally. Public or multi-user deployment remains out of scope. |
| 8 — Operational visibility | Complete for the local project scope | Health/model readiness, job and review status, metrics, result history, backup, and restore guidance are available. |

**All nine levels are marked complete based on the user's confirmation that each was tested.** Current work is quantitative testing across the supplied sample files. The evaluation is stored separately; existing historical benchmark files remain unchanged.

## Sample model evaluation

All 28 files in `data/Sample_docs/` completed the local OCR + `qwen2.5:3b` text-mode pipeline. The model produced structured output for 23/28 documents (82.1%); five outputs were malformed/empty or used fallback. Against partial reference checklists, key-fact recall was 83/160 (51.9%). On 12 samples with reference rows, table precision was 21.3%, recall 30.8%, and F1 25.2%. All 28 results required human review. Mean end-to-end time was 96.5 seconds (median 64.0 seconds), with CPU-only inference.

A separate two-image `qwen2.5vl:3b` pilot succeeded on one receipt and fell back to text on one marksheet. Its model-only key-fact recall was 5/18 (27.8%); row precision/recall/F1 were 37.5%/33.3%/35.3%. This sample is too small to compare models reliably. The vision model was not run on all 28 files.

Shareable HTML charts and privacy-reduced CSV/JSON scores are in `data/evaluations/2026-10-31/`. The accuracy figures use partial references, not exhaustive labels; do not present key-fact recall as all-field accuracy or as a guarantee on new documents. Full extraction JSON and a private source index stay local and can include personal information. No documents were added to the historical benchmark manifests.

The current workspace implements the local prototype across these project stages. Marksheet extraction additionally normalizes model output, retries text extraction when local vision inference fails, checks table rows against OCR regions, compares OCR layouts when inventorying source rows, honors embedded image orientation metadata, and routes disagreements or missing rows to review:

1. **Document ingestion:** common text, image, PDF, Office, OpenDocument, CSV, and markup formats have format-specific extraction paths. Legacy binary Office conversion needs LibreOffice; scanned-image OCR needs Tesseract.
2. **OCR and layout signals:** native PDF word regions and normalized boxes are extracted; table and spreadsheet structure is preserved. Tesseract OCR is optional. Document parsers do not yet provide full page-layout segmentation for DOCX/PPTX, diagrams, signatures, handwriting, or rotated scans.
3. **Baseline ladder:** rule baseline, local text model, zero-shot VLM, prompted VLM, and automatic routing are wired. The free local Ollama text and vision models are installed; the website health status reports when configured models are unavailable.
4. **Structured extraction and checks:** schema prompting, quote grounding, evidence metadata, invoice arithmetic, confidence heuristics, and human review routing are implemented.
5. **Serving:** FastAPI direct and job endpoints, SQLite status/results, review records, API-key checks, optional result encryption, audit events, and metrics are implemented. The queue runs in-process and is intended for a single local service instance.
6. **Evaluation and adaptation:** optional historical evaluation and LoRA/QLoRA utilities exist. They are not part of the per-upload workflow; no new user files should be added to their historical manifests. Training has not been run.
7. **Deployment and operations:** Docker files and setup guidance are included. The local website is used for individual uploads; container deployment and production operations remain unfinished.

Run the local parser and validation regression checks with:

```powershell
Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The tests create sample PDF, DOCX, XLSX, and PPTX inputs in memory and check text formats, tables, evidence grounding, and arithmetic validation. They do not substitute for a labeled benchmark or a live VLM/OCR integration run.

### Sample documents

Files in `data/Sample_docs/` are individual examples for manual use through the website. Review and save each upload on its own. Do not assign benchmark splits or copy uploads into the historical labeled manifests.


