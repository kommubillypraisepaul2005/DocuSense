"""Run every file in data/Sample_docs through the local extraction service.

The service saves each full extraction to data/results/. This script writes a
private index and a privacy-reduced run summary under data/evaluations/.
"""
from __future__ import annotations

import asyncio
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "data" / "Sample_docs"
OUTPUT = ROOT / "data" / "evaluations" / "2026-10-31"
BASE_URL = "http://127.0.0.1:8000"


def classify(name: str) -> str:
    lower = name.casefold()
    if "bill " in lower:
        return "receipt"
    if "mark sheet" in lower or "marksheet" in lower or "mark sheet" in lower or "lilly _page-0003" in lower or "chatgpt image" in lower:
        return "marksheet"
    if "resume" in lower:
        return "resume"
    if "afcat" in lower or "gate application" in lower:
        return "application"
    if "iocl" in lower:
        return "admit_card"
    if "caste" in lower or "cast certificates" in lower:
        return "identity_certificate"
    if "smarted" in lower or "certificate_1473" in lower:
        return "certificate"
    if "vit payment" in lower:
        return "payment_receipt"
    if "2022543044" in lower:
        return "bank_passbook"
    if "lilly _page-0001" in lower:
        return "identity_card"
    if "lilly _page-0002" in lower:
        return "identity_information"
    return "general_document"


def choose_schema(name: str, category: str) -> dict[str, Any] | None:
    lower = name.casefold()
    if category == "marksheet":
        if "2007" in lower:
            return {"document_type": "marksheet", "fields": {
                "student_name": {"type": "string", "required": False},
                "examination": {"type": "string", "required": False},
                "exam_year": {"type": "string", "required": False},
                "result": {"type": "string", "required": False},
                "date_of_issue": {"type": "string", "required": False},
                "subjects_and_marks": {"type": "table", "required": True, "columns": ["subject", "max_marks", "marks_awarded", "total_marks", "grade"]},
            }}
        return {"document_type": "marksheet", "fields": {
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
            "subjects_and_marks": {"type": "table", "required": True, "columns": ["subject_code", "subject", "theory_marks", "practical_marks", "total_marks", "total_in_words", "grade"]},
        }}
    if category in {"receipt", "payment_receipt"}:
        return {"document_type": "invoice_receipt", "fields": {
            "merchant_name": {"type": "string", "required": False},
            "merchant_address": {"type": "string", "required": False},
            "receipt_number": {"type": "string", "required": False},
            "date": {"type": "string", "required": False},
            "currency": {"type": "string", "required": False},
            "subtotal": {"type": "number", "required": False},
            "discount": {"type": "number", "required": False},
            "total": {"type": "number", "required": False},
            "other_text": {"type": "string", "required": False},
            "line_items": {"type": "table", "required": False, "columns": ["description", "quantity", "unit_price", "amount"]},
            "taxes": {"type": "table", "required": False, "columns": ["tax_name", "rate", "amount"]},
        }}
    return None


def result_counts(result: dict[str, Any]) -> dict[str, Any]:
    extraction = result.get("extraction") if isinstance(result.get("extraction"), dict) else {}
    fields = extraction.get("fields") if isinstance(extraction.get("fields"), dict) else {}
    populated = 0
    for item in fields.values():
        value = item.get("value") if isinstance(item, dict) and "value" in item else item
        if value not in (None, "", [], {}):
            populated += 1
    tables = extraction.get("tables") if isinstance(extraction.get("tables"), list) else []
    rows = sum(len(table.get("rows", [])) for table in tables if isinstance(table, dict) and isinstance(table.get("rows"), list))
    ocr_chars = sum(len(str(segment.get("text", ""))) for segment in result.get("segments", []) if isinstance(segment, dict))
    validation = result.get("validation") if isinstance(result.get("validation"), dict) else {}
    runtime = result.get("runtime") if isinstance(result.get("runtime"), dict) else {}
    return {
        "model": result.get("model"),
        "model_mode": result.get("model_mode"),
        "processing_status": validation.get("status", "unvalidated"),
        "review_required": bool(validation.get("review_required", False)),
        "review_reason_count": len(validation.get("review_reasons", [])),
        "ocr_characters": ocr_chars,
        "populated_fields": populated,
        "table_count": len(tables),
        "table_rows": rows,
        "extracted_fields": len(fields),
        "parse_ms": runtime.get("parse_ms"),
        "inference_ms": (runtime.get("inference") or {}).get("wall_ms") if isinstance(runtime.get("inference"), dict) else None,
        "page_continuation": result.get("page_continuation"),
        "saved_result": (result.get("saved_result") or {}).get("filename"),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("auto", "text", "vision", "rules"), default="text")
    parser.add_argument("--base-url", default=BASE_URL, help="Local extraction service URL (default: %(default)s).")
    parser.add_argument("--limit", type=int, default=0, help="Process only the first N sorted files (0 means all).")
    parser.add_argument("--file", action="append", default=[], help="Process a filename from Sample_docs; may be repeated.")
    args = parser.parse_args()
    run_id = f"{args.mode}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    run_directory = OUTPUT / run_id
    run_directory.mkdir(parents=True, exist_ok=True)
    master_files = sorted(path for path in SAMPLES.iterdir() if path.is_file())
    files = master_files
    if args.file:
        by_name = {path.name: path for path in master_files}
        missing = set(args.file) - set(by_name)
        if missing:
            raise SystemExit(f"Files not found in Sample_docs: {', '.join(sorted(missing))}")
        files = [by_name[name] for name in args.file]
    if args.limit > 0:
        files = files[:args.limit]
    if not files:
        raise SystemExit(f"No sample documents found in {SAMPLES}")
    private_index: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    timeout = httpx.Timeout(1200.0, connect=10.0)
    async with httpx.AsyncClient(base_url=args.base_url.rstrip("/"), timeout=timeout) as client:
        health = await client.get("/health")
        health.raise_for_status()
        health_payload = health.json()
        print(f"Service ready; Ollama status: {health_payload.get('ollama')}", flush=True)
        for index, path in enumerate(files, start=1):
            doc_id = f"S{master_files.index(path) + 1:02d}"
            category = classify(path.name)
            schema = choose_schema(path.name, category)
            print(f"[{index}/{len(files)}] {doc_id} ({category})", flush=True)
            try:
                with path.open("rb") as source:
                    response = await client.post(
                        "/extract",
                        params={"mode": args.mode},
                        data={"schema_json": json.dumps(schema, ensure_ascii=False)} if schema else None,
                        files={"file": (path.name, source, "application/octet-stream")},
                    )
                response.raise_for_status()
                result = response.json()
                counts = result_counts(result)
                summary.append({"doc_id": doc_id, "category": classify(path.name), "status": "completed", **counts})
                private_index.append({"doc_id": doc_id, "source_filename": path.name, "saved_result": counts["saved_result"]})
                print(f"  done: fields={counts['populated_fields']} rows={counts['table_rows']} status={counts['processing_status']} model={counts['model']}", flush=True)
            except Exception as exc:  # preserve progress and report per-file failures
                summary.append({"doc_id": doc_id, "category": classify(path.name), "status": "failed", "error_type": type(exc).__name__})
                private_index.append({"doc_id": doc_id, "source_filename": path.name, "error_type": type(exc).__name__})
                print(f"  failed: {type(exc).__name__}: {exc}", flush=True)
    run = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "sample_count": len(files),
        "completed": sum(item.get("status") == "completed" for item in summary),
        "failed": sum(item.get("status") == "failed" for item in summary),
        "mode": args.mode,
        "model_policy": "local Ollama only; no cloud API",
        "model_configuration": {
            "text_model": (health_payload.get("ollama") or {}).get("text_model"),
            "vision_model": (health_payload.get("ollama") or {}).get("vision_model"),
            "output_token_budget": 1536 if args.mode in {"text", "vision"} else None,
            "inference_hardware": "CPU-only for this run (Ollama runtime reported 0 bytes VRAM)",
            "schema_policy": "marksheet and receipt profiles use document-specific schemas; other samples use general extraction",
        },
        "summary": summary,
    }
    (run_directory / "run_summary.json").write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_directory / "private_source_index.json").write_text(json.dumps(private_index, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved privacy-reduced summary to {run_directory / 'run_summary.json'}", flush=True)
    print(f"Completed {run['completed']}/{run['sample_count']}; failed {run['failed']}.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
