"""Create OCR-derived annotation drafts for the two unlabelled marksheets.

Drafts are not gold labels and are deliberately excluded from the benchmark.
Every value must be checked against its source scan before promotion.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.baselines import extract_rules
from app.config import OCR_ENABLED
from app.extractors import extract_file
from app.processor import _deduplicated_ocr_text


SAMPLES = ROOT / "data" / "Sample_docs"
DRAFTS = ROOT / "data" / "benchmark" / "drafts"
ANNOTATIONS = ROOT / "data" / "annotation_template.csv"
SCHEMA = {
    "document_type": "marksheet",
    "fields": {
        "student_name": {"type": "string", "required": False},
        "roll_number": {"type": "string", "required": False},
        "registration_number": {"type": "string", "required": False},
        "date_of_birth": {"type": "string", "required": False},
        "father_name": {"type": "string", "required": False},
        "mother_name": {"type": "string", "required": False},
        "school_name": {"type": "string", "required": False},
        "board": {"type": "string", "required": False},
        "examination": {"type": "string", "required": False},
        "exam_year": {"type": "string", "required": False},
        "result": {"type": "string", "required": False},
        "date_of_issue": {"type": "string", "required": False},
        "subjects_and_marks": {
            "type": "table",
            "required": True,
            "columns": ["subject_code", "subject", "theory_marks", "practical_marks", "total_marks", "total_in_words", "grade"],
        },
    },
}
DOCUMENTS = {
    "marksheet_new_1": "marksheet uk 3.webp",
    "marksheet_new_2": "marksheet uk1.webp",
}


def main() -> None:
    DRAFTS.mkdir(parents=True, exist_ok=True)
    draft_paths: dict[str, str] = {}
    for document_id, filename in DOCUMENTS.items():
        path = SAMPLES / filename
        parsed = extract_file(filename, path.read_bytes(), OCR_ENABLED)
        text = _deduplicated_ocr_text(parsed["segments"])
        extraction = extract_rules(text, SCHEMA)
        draft = {
            "document_id": document_id,
            "source_file": f"Sample_docs/{filename}",
            "annotation_status": "draft_needs_human_review",
            "is_gold_label": False,
            "split": None,
            "schema": SCHEMA,
            "candidate_fields": extraction.get("fields", {}),
            "candidate_tables": extraction.get("tables", []),
            "ocr_text_for_comparison": text,
            "warnings": [
                "OCR-generated candidates only; verify every scalar and table cell against the scan.",
                "Do not use this draft as held-out evaluation until labels are verified and the split is frozen.",
                "Bounding-box labels have not been annotated.",
            ],
        }
        safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(filename).stem).strip("_")
        draft_path = DRAFTS / f"{document_id}_{safe_stem}.json"
        draft_path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
        draft_paths[document_id] = draft_path.relative_to(ROOT / "data").as_posix()

    with ANNOTATIONS.open("r", encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))
        columns = list(rows[0]) if rows else []
    for row in rows:
        if row.get("document_id") in draft_paths:
            if row.get("annotation_status") == "labeled":
                continue
            row["annotation_status"] = "draft_needs_user_verification"
            row["split"] = ""
            note = row.get("reviewer_notes", "").strip()
            draft_note = f"Unverified OCR candidate draft: {draft_paths[row['document_id']]}; visual review draft ready: benchmark/drafts/{row['document_id']}_visual_review.json"
            if draft_note not in note:
                row["reviewer_notes"] = f"{note}; {draft_note}".strip("; ")
    with ANNOTATIONS.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

    print(json.dumps({"drafts": draft_paths, "status": "needs_human_review; not added to benchmark"}, ensure_ascii=True))


if __name__ == "__main__":
    main()
