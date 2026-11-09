# DocuSense synthetic demo walkthrough

Use this guide for a short, repeatable demonstration. All values below are fictional. Do not replace them with personal or customer information.

## Prepare a synthetic invoice

Create a one-page document in Word or Excel and export it as `synthetic-invoice.pdf`.

**NORTHSTAR OFFICE SUPPLIES**  
18 Example Road, Sample City  
Invoice: NS-2026-104  
Date: 10 November 2026  
Customer: Demo Customer

| Description | Qty | Unit price | Amount |
| --- | ---: | ---: | ---: |
| Notebook pack | 2 | 120.00 | 240.00 |
| Blue pen set | 3 | 40.00 | 120.00 |

Subtotal: 360.00  
Tax: 18.00  
Total: 378.00  
Currency: INR

These values are intentionally simple so you can check the output manually. They are demo data, not an accuracy benchmark.

## Run the demo

1. Confirm Ollama is running if you plan to demonstrate a model-backed mode.
2. Start the app with `powershell -ExecutionPolicy Bypass -File .\start_document_lab.ps1`.
3. Open `http://127.0.0.1:8000`.
4. Upload `synthetic-invoice.pdf`.
5. Select **Invoice or receipt**.
6. Choose **Automatic** for the local model/fallback path, or **OCR + local structuring** to show the no-Ollama baseline.
7. Run extraction and wait for the result.

## What to point out

- The selected profile/schema tells the system which fields and table columns to request.
- OCR/native parsers provide source text and tables; Ollama can structure the content into JSON.
- The result shows extracted fields, tables, OCR text, evidence, and validation/review warnings.
- The application does not silently treat every model output as correct. Missing values, disagreements, or unsupported evidence should be checked by a person.

## Demonstrate human review

1. Select **Review and correct**.
2. Change one deliberately incorrect or blank demo value to the value shown in the synthetic PDF.
3. If needed, correct a line-item cell or add/remove a row.
4. Add a short note such as `Verified against synthetic source PDF`.
5. Select **Save corrections**.
6. Select **Download JSON**.
7. Open the downloaded file and confirm the corrected value is present.

This demonstrates the complete path: upload → extraction → evidence/validation → human correction → saved JSON.

## Interview explanation

A concise explanation:

> DocuSense is a local-first document extraction prototype. It parses common document formats, uses OCR for scanned content, and can call local Ollama text or vision models to produce schema-based JSON. I added evidence and validation so uncertain or conflicting values are flagged for review instead of being blindly trusted. A reviewer can edit fields and table rows, save the correction, and download the resulting JSON. The important design choice is human-in-the-loop extraction: model output is a draft that must be checked against its source.

## Be honest about limitations

- Extraction quality varies by document layout, scan quality, language, and model.
- A model can miss rows or misread columns. The demo is not proof of general accuracy.
- Evidence/confidence indicators are heuristics, not calibrated probabilities.
- The queue is process-local and the app is intended for a single local instance, not production multi-user traffic.
- Do not claim the model learns automatically from review corrections; saved corrections do not automatically fine-tune it.

## Before sharing the repository

- Use only synthetic demo inputs and screenshots.
- Do not commit `.env`, database files, saved result JSON, logs, local model files, or personal documents.
- Review `git status --short` and `git diff --check` before committing.
- Run the regression suite and report the exact result.
