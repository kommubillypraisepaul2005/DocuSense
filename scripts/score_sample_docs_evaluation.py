"""Score the latest sample-doc run against privacy-local reference facts.

Metrics are key-fact recall and table-row precision/recall, not a universal
document accuracy claim. The shareable CSV contains IDs and aggregate scores,
never extracted personal values.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import unicodedata
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "data" / "evaluations" / "2026-10-31"
RESULTS = ROOT / "data" / "results"
DRAFTS = ROOT / "data" / "benchmark" / "drafts"

# Compact reference facts distilled from the user's structured-details PDF or
# checked directly against the supplied document image. Values stay local.
REFERENCE: dict[str, dict[str, Any]] = {
    "S01": {"source": "structured_details_pdf", "fields": ["20207119", "2020", "15 July 2020", "PASS", "Central Board of Secondary Education"], "rows": [
        ["English Language & Literature", "50", "19", "69", "C1"], ["Hindi Course-A", "54", "19", "73", "C1"], ["Mathematics Standard", "58", "19", "77", "B1"], ["Science", "46", "19", "65", "B2"], ["Social Science", "70", "19", "89", "A2"], ["Information Technology", "40", "50", "90", "A2"], ["German", "36", "19", "55", "D2"]]},
    "S02": {"source": "structured_details_pdf", "fields": ["20608204", "2022", "22 July 2022", "PASS", "Central Board of Secondary Education"], "rows": [
        ["English Core", "66", "20", "86", "B1"], ["Mathematics", "48", "20", "68", "B2"], ["Physics", "51", "30", "81", "A2"], ["Chemistry", "54", "30", "84", "B1"], ["Computer Science", "53", "30", "83", "B2"]]},
    "S03": {"source": "structured_details_pdf", "fields": ["Indian Bank", "Suryaraopet", "IDIB000S142", "6 September 2018", "SELF"]},
    "S04": {"source": "structured_details_pdf", "fields": ["AFCAT02260111891", "PaymentSuccess", "649", "80.4%", "Appearing", "31 August 2026"]},
    "S05": {"source": "structured_details_pdf_and_source_image", "fields": ["8.21", "Vellore Institute of Technology", "Python", "Java", "Computer Science Engineer"]},
    "S06": {"source": "structured_details_pdf", "fields": ["8.35", "VIT-AP University", "Python", "Java", "Ethnus", "AWS Certified Cloud Practitioner"]},
    "S07": {"source": "source_image_visual_reference", "fields": ["Chinese Plate", "622967", "75.48", "2.90", "1.28", "79.66"], "rows": [["Chinese Buffet", "4", "51.96"], ["Soda", "4", "7.96"], ["Desserts", "4", "15.56"]]},
    "S08": {"source": "source_image_visual_reference", "fields": ["IKEA Food Place", "27.92", "2.90", "1.28", "32.10", "409751125811"], "rows": [["Cheese Burger", "4", "5.99", "23.96"], ["Soda", "4", "0.49", "1.96"], ["Cinnamon Bun", "2", "1.00", "2.00"]]},
    "S09": {"source": "source_image_visual_reference", "fields": ["Sri Krishna", "65.00", "6.00", "59.00", "5.31", "70"], "rows": [["Medu Wada", "1", "65", "65"]]},
    "S10": {"source": "source_image_visual_reference", "fields": ["Donne Biryani Palace", "14/07/23", "13937", "517.00", "₹517.00"], "rows": [["Chicken Biryani", "1", "99.00", "99.00"], ["Mutton Biryani", "1", "229.00", "229.00"], ["Chicken Lolipop", "1", "169.00", "169.00"], ["Mineral Water", "1", "20.00", "20.00"]]},
    "S11": {"source": "structured_details_pdf", "fields": ["Community, Nativity & Date of Birth Certificate", "Madiga", "21 January 2005", "27 December 2013", "Government of Andhra Pradesh"]},
    "S12": {"source": "structured_details_pdf", "fields": ["A. Suresh Kumar", "Gandhinagar", "Vijayawada Urban"]},
    "S13": {"source": "structured_details_pdf", "fields": ["Community, Nativity & Date of Birth Certificate", "Madiga", "11 June 2008", "27 December 2013", "Government of Andhra Pradesh"]},
    "S14": {"source": "structured_details_pdf", "fields": ["A. Suresh Kumar", "Gandhinagar", "Vijayawada Urban"]},
    "S15": {"source": "structured_details_pdf", "fields": ["Madiga", "Christhurajapuram", "Vijayawada Urban", "21 January 2005", "R. Siva Rao", "27 December 2013"]},
    "S16": {"source": "structured_details_pdf", "fields": ["SASIGD", "13–14 August 2026", "Chaitanya Bharathi Institute of Technology", "68127", "NOT FOUND"]},
    "S17": {"source": "structured_details_pdf_and_source_image", "fields": ["2613213155", "March 2026", "792", "A Grade", "15 April 2026", "English"], "rows": [["English", "80", "87"], ["Sanskrit", "88", "70"], ["Botany", "60", "39", "30"], ["Zoology", "45", "33", "28"], ["Physics", "40", "38", "30"], ["Chemistry", "56", "40", "28"]]},
    "S18": {"source": "structured_details_pdf", "fields": ["GATE 2027", "M121B77", "Indian Institute of Technology Madras", "CS", "1000", "3 September 2026"]},
    "S19": {"source": "structured_details_pdf", "fields": ["Indian Oil Corporation Limited", "24 September 2026", "12:30 PM", "1:00 PM", "Rajamahendravaram", "533105"]},
    "S20": {"source": "structured_details_pdf", "fields": ["Unique Identification Authority of India", "11 June 2008", "Female", "520008"]},
    "S21": {"source": "structured_details_pdf", "fields": ["proof of identity", "not proof of citizenship", "QR code", "consent"]},
    "S22": {"source": "structured_details_pdf", "fields": ["28110899", "11 June 2008", "13 May 2024", "PASS"], "rows": [
        ["English Language & Literature", "53", "20", "73", "B2"], ["Hindi Course-A", "44", "20", "64", "C2"], ["Mathematics Standard", "21", "20", "41", "D1"], ["Science", "34", "20", "54", "C1"], ["Social Science", "59", "20", "79", "B2"], ["Artificial Intelligence", "39", "50", "89", "B2"], ["German", "32", "20", "52", "D2"]]},
    "S25": {"source": "structured_details_pdf", "fields": ["SmartED Innovations", "Artificial Intelligence", "AI1385", "9 October 2025", "Sai Sreekar"]},
    "S26": {"source": "source_image_visual_reference", "fields": ["Central Board of Secondary Education", "2004", "6145109", "5th November 1988", "PASS", "28 May 2004"], "rows": [
        ["English Comm.", "83", "XXX", "83", "A1"], ["Hindi Course-B", "75", "XXX", "75", "B1"], ["Mathematics", "51", "XXX", "51", "C2"], ["Science & Tech", "32", "23", "55", "C1"], ["Social Science", "81", "XXX", "81", "A2"], ["Introductory IT", "16", "58", "74", "C2"]]},
    "S27": {"source": "source_image_visual_reference", "fields": ["High School Examination", "2007", "SHAYKARI", "15 December 1992", "SECOND", "5th June 2007"]},
    "S28": {"source": "structured_details_pdf", "fields": ["VIT-AP University", "21 June 2026", "₹250", "₹150", "₹400"], "rows": [["COE transcripts consolidated", "250"], ["COE transcripts semester-wise", "150"], ["Grand Total", "400"]]},
}


def canon(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("&", " and ")
    return re.sub(r"[^\w]+", "", text, flags=re.UNICODE)


def field_value(value: Any) -> Any:
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def numeric_values(value: Any) -> set[str]:
    text = unicodedata.normalize("NFKC", str(value or ""))
    output = set()
    for token in re.findall(r"(?<!\w)-?\d[\d,]*(?:\.\d+)?", text):
        cleaned = token.replace(",", "")
        if "." in cleaned:
            cleaned = cleaned.rstrip("0").rstrip(".")
        integer, dot, decimal = cleaned.partition(".")
        integer = integer.lstrip("0") or "0"
        output.add(integer + (dot + decimal if dot else ""))
    return output


def fact_matches(fact: str, actual: str) -> bool:
    target = canon(fact)
    if not target:
        return False
    expected_numbers = numeric_values(fact)
    if expected_numbers and len(expected_numbers) == 1 and re.fullmatch(r"[\d,]+(?:\.\d+)?", fact.strip(" ₹$€£,%")):
        return next(iter(expected_numbers)) in numeric_values(actual)
    return target in canon(actual)


def get_result(saved_name: str) -> dict[str, Any]:
    path = RESULTS / saved_name
    record = json.loads(path.read_text(encoding="utf-8"))
    return record.get("result", record)


def model_texts(result: dict[str, Any]) -> list[str]:
    extraction = result.get("extraction") if isinstance(result.get("extraction"), dict) else {}
    texts: list[str] = []
    fields = extraction.get("fields", {})
    if isinstance(fields, dict):
        for value in fields.values():
            texts.append(str(field_value(value) or ""))
    for table in extraction.get("tables", []) if isinstance(extraction.get("tables"), list) else []:
        if not isinstance(table, dict):
            continue
        texts.extend(str(header) for header in table.get("headers", []))
        for row in table.get("rows", []):
            texts.append(" ".join(map(str, row)) if isinstance(row, list) else str(row))
    return texts


def predicted_rows(result: dict[str, Any]) -> list[list[str]]:
    extraction = result.get("extraction") if isinstance(result.get("extraction"), dict) else {}
    output = []
    for table in extraction.get("tables", []) if isinstance(extraction.get("tables"), list) else []:
        if not isinstance(table, dict) or str(table.get("name", "")).casefold() == "document_lines":
            continue
        for row in table.get("rows", []):
            if isinstance(row, list) and any(str(cell).strip() for cell in row):
                output.append([str(cell) for cell in row])
    return output


def row_matches(expected_row: list[Any], predicted_row: list[str]) -> bool:
    used: set[int] = set()
    for cell in expected_row:
        if not str(cell).strip():
            continue
        expected_numbers = numeric_values(cell)
        numeric_target = bool(expected_numbers) and re.fullmatch(r"[\d,]+(?:\.\d+)?", str(cell).strip(" ₹$€£,%"))
        match = next((index for index, predicted in enumerate(predicted_row) if index not in used and (
            next(iter(expected_numbers)) in numeric_values(predicted) if numeric_target else canon(str(cell)) in canon(predicted)
        )), None)
        if match is None:
            return False
        used.add(match)
    return True


def load_draft_reference(doc_id: str) -> dict[str, Any] | None:
    mapping = {
        "S23": "marksheet_new_1_visual_review.json",
        "S24": "marksheet_new_2_visual_review.json",
    }
    if doc_id not in mapping:
        return None
    path = DRAFTS / mapping[doc_id]
    if not path.is_file():
        return None
    draft = json.loads(path.read_text(encoding="utf-8"))
    if not draft.get("is_gold_label"):
        return None
    facts = [str(field_value(v)) for v in draft.get("candidate_fields", {}).values() if field_value(v)]
    tables = draft.get("candidate_tables", [])
    rows = [row for table in tables for row in table.get("rows", [])]
    return {"source": "user_approved_visual_label", "fields": facts, "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_summary", type=Path, help="Path to a run_summary.json from run_sample_docs_evaluation.py")
    args = parser.parse_args()
    run_path = args.run_summary.resolve()
    run = json.loads(run_path.read_text(encoding="utf-8"))
    index = json.loads((run_path.parent / "private_source_index.json").read_text(encoding="utf-8"))
    by_id = {item["doc_id"]: item for item in index}
    records: list[dict[str, Any]] = []
    fact_hits = fact_total = 0
    row_tp = row_fp = row_fn = 0
    model_success = model_failed = process_failed = 0
    reference_documents = reference_row_documents = review_count = 0
    ocr_document_count = 0
    total_latencies: list[float] = []
    inference_latencies: list[float] = []
    token_rates: list[float] = []
    vram_values: list[int] = []
    category_totals: dict[str, Counter] = {}
    for item in run.get("summary", []):
        doc_id = item["doc_id"]
        private = by_id.get(doc_id, {})
        result = get_result(private["saved_result"]) if private.get("saved_result") else {}
        prediction = result.get("extraction") if isinstance(result.get("extraction"), dict) else {}
        model_mode_requested = str(run.get("mode", ""))
        actual_model_mode = str(result.get("model_mode", ""))
        model_ok = (
            isinstance(result.get("extraction"), dict)
            and not result.get("model_error")
            and (actual_model_mode == "vision" if model_mode_requested == "vision" else actual_model_mode == "text" if model_mode_requested == "text" else bool(result.get("model")))
        )
        process_ok = item.get("status") == "completed"
        model_success += model_ok
        model_failed += process_ok and not model_ok
        process_failed += not process_ok
        reference = load_draft_reference(doc_id) or REFERENCE.get(doc_id)
        reference_documents += reference is not None
        # The headline accuracy is for the requested model/mode. A rules or
        # text fallback stays visible as a model failure and is not credited
        # as if the requested model had produced that extraction.
        texts = model_texts(result) if model_ok else []
        matched = expected = 0
        if reference:
            for fact in reference.get("fields", []):
                target = canon(fact)
                if not target:
                    continue
                expected += 1
                matched += any(fact_matches(fact, value) for value in texts)
        pred_rows = predicted_rows(result) if model_ok else []
        expected_rows = reference.get("rows", []) if reference else []
        reference_row_documents += bool(expected_rows)
        matched_rows: set[int] = set()
        row_true_positive = 0
        for predicted in pred_rows:
            match_index = next((i for i, expected_row in enumerate(expected_rows) if i not in matched_rows and row_matches(expected_row, predicted)), None)
            if match_index is not None:
                matched_rows.add(match_index)
                row_true_positive += 1
        row_false_positive = max(0, len(pred_rows) - row_true_positive) if expected_rows else 0
        row_false_negative = max(0, len(expected_rows) - row_true_positive)
        fact_hits += matched
        fact_total += expected
        row_tp += row_true_positive
        row_fp += row_false_positive
        row_fn += row_false_negative
        category = item.get("category", "other")
        category_totals.setdefault(category, Counter()).update({
            "documents": 1,
            "processed": int(process_ok),
            "model_success": int(model_ok),
            "review_required": int(bool((result.get("validation") or {}).get("review_required"))),
            "facts_correct": matched,
            "facts_expected": expected,
            "rows_tp": row_true_positive,
            "rows_fp": row_false_positive,
            "rows_fn": row_false_negative,
        })
        validation = result.get("validation") if isinstance(result.get("validation"), dict) else {}
        inference = (result.get("runtime") or {}).get("inference") if isinstance(result.get("runtime"), dict) else {}
        runtime = result.get("runtime") if isinstance(result.get("runtime"), dict) else {}
        review_required = bool(validation.get("review_required"))
        review_count += review_required
        ocr_document_count += bool(sum(len(str(segment.get("text", ""))) for segment in result.get("segments", []) if isinstance(segment, dict)))
        if isinstance(runtime.get("total_ms"), (int, float)):
            total_latencies.append(runtime["total_ms"] / 1000)
        if isinstance(inference, dict):
            if isinstance(inference.get("wall_ms"), (int, float)):
                inference_latencies.append(inference["wall_ms"] / 1000)
            if isinstance(inference.get("generation_tokens_per_second"), (int, float)):
                token_rates.append(inference["generation_tokens_per_second"])
            memory = inference.get("runtime_memory")
            if isinstance(memory, dict) and isinstance(memory.get("vram_bytes"), int):
                vram_values.append(memory["vram_bytes"])
        records.append({
            "doc_id": doc_id,
            "category": category,
            "run_status": "processed" if process_ok else "failed",
            "model_status": "success" if model_ok else "failed_or_fallback",
            "model": result.get("model") or "none",
            "reference_source": reference.get("source", "not_scored") if reference else "not_scored",
            "key_facts_correct": matched,
            "key_facts_expected": expected,
            "key_fact_recall": matched / expected if expected else None,
            "expected_rows": len(expected_rows),
            "predicted_rows": len(pred_rows),
            "row_true_positive": row_true_positive,
            "row_false_positive": row_false_positive,
            "row_false_negative": row_false_negative,
            "review_required": review_required,
            "ocr_characters": sum(len(str(segment.get("text", ""))) for segment in result.get("segments", []) if isinstance(segment, dict)),
            "runtime_total_ms": runtime.get("total_ms"),
            "runtime_inference_ms": inference.get("wall_ms") if isinstance(inference, dict) else None,
            "generation_tokens_per_second": inference.get("generation_tokens_per_second") if isinstance(inference, dict) else None,
            "model_error": bool(result.get("model_error")),
        })
    fact_recall = fact_hits / fact_total if fact_total else None
    row_precision = row_tp / (row_tp + row_fp) if row_tp + row_fp else None
    row_recall = row_tp / (row_tp + row_fn) if row_tp + row_fn else None
    row_f1 = 2 * row_precision * row_recall / (row_precision + row_recall) if row_precision is not None and row_recall is not None and row_precision + row_recall else None
    scored = {
        "run_summary": str(run_path),
        "mode": run.get("mode"),
        "sample_documents": run.get("sample_count"),
        "processed": run.get("completed"),
        "processing_failures": process_failed,
        "ocr_documents_with_text": ocr_document_count,
        "reference_scored_documents": reference_documents,
        "reference_documents_with_table_rows": reference_row_documents,
        "model_successes": model_success,
        "model_failures_or_fallbacks": model_failed,
        "review_required_documents": review_count,
        "review_required_rate": review_count / len(records) if records else None,
        "mean_latency_seconds": sum(total_latencies) / len(total_latencies) if total_latencies else None,
        "median_latency_seconds": statistics.median(total_latencies) if total_latencies else None,
        "mean_inference_seconds": sum(inference_latencies) / len(inference_latencies) if inference_latencies else None,
        "median_generation_tokens_per_second": statistics.median(token_rates) if token_rates else None,
        "reported_max_vram_bytes": max(vram_values) if vram_values else None,
        "key_fact_correct": fact_hits,
        "key_fact_expected": fact_total,
        "key_fact_recall": fact_recall,
        "table_row_true_positive": row_tp,
        "table_row_false_positive": row_fp,
        "table_row_false_negative": row_fn,
        "table_row_precision": row_precision,
        "table_row_recall": row_recall,
        "table_row_f1": row_f1,
        "model_configuration": run.get("model_configuration", {}),
        "categories": {name: dict(counts) for name, counts in category_totals.items()},
        "documents": records,
        "metric_notes": [
            "Key-fact score is recall against compact reference facts, not an all-fields accuracy rate.",
            "Table-row precision/recall is computed only for documents with row references; every nonempty output row in their structured tables contributes, including duplicate or spurious rows.",
            "Reference facts come from the user-provided structured-details PDF, user-approved marksheet visual labels, or direct visual checks of supplied sample images.",
            "Documents without a reference are reported as not scored, not assigned zero accuracy.",
        ],
    }
    output_json = run_path.parent / "scored_summary.json"
    output_csv = run_path.parent / "document_scores.csv"
    output_json.write_text(json.dumps(scored, ensure_ascii=False, indent=2), encoding="utf-8")
    with output_csv.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(records[0]) if records else [])
        if records:
            writer.writeheader()
            writer.writerows(records)
    print(json.dumps({key: scored[key] for key in ("sample_documents", "processed", "model_successes", "model_failures_or_fallbacks", "key_fact_correct", "key_fact_expected", "key_fact_recall", "table_row_precision", "table_row_recall", "table_row_f1")}, indent=2))
    print(f"Saved privacy-safe score files: {output_json} and {output_csv}")


if __name__ == "__main__":
    main()
