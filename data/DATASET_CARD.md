# Document handling notes

## Current workflow

Documents are processed individually. Their fields and tables can differ even when their broad type is the same; marksheets from different boards and years can have distinct layouts. The application supports user-selected profiles and custom schemas, then exposes OCR text, extracted values, warnings, and an editor for correcting the current result.

No new upload should be added to a shared benchmark or treated as representative of every document. A review correction belongs to that saved result and does not automatically train the model or become a reusable schema.

## Historical development artifacts

`benchmark_marksheets.jsonl`, `benchmark/drafts/`, and `runs/marksheets.json` were created in an earlier development phase. They are retained as historical project files, not as an active benchmark, release gate, or template for new uploads. Do not add current user documents to those files.

The previous held-out candidate list has been retired. Documents remain available in `Sample_docs/` for individual website use, and each processed result is saved under `data/results/`.

## Handling personal documents

Mark sheets, certificates, and forms can contain sensitive personal information. Keep source files and result JSON local, and do not publish unmasked documents or extracted values. Configure encryption and access controls before exposing the service beyond localhost.
