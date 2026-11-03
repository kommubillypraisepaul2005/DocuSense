"""Run a reproducible held-out document benchmark and enforce an F1 gate.

Manifest JSONL: one object per line with {"file":"relative/path.pdf",
"expected":{"invoice_number":"INV-1","total":"100.00"}}.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.processor import process_document  # noqa: E402
from app.config import OLLAMA_TEXT_MODEL, OLLAMA_VISION_MODEL  # noqa: E402


def val(value: Any) -> Any:
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def norm_cell(value: Any) -> str:
    normalized = norm(value)
    if re.fullmatch(r"\d+", normalized):
        return str(int(normalized))
    return normalized


def edit_distance(a: list[str], b: list[str]) -> int:
    previous = list(range(len(b) + 1))
    for i, left in enumerate(a, 1):
        current = [i]
        for j, right in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (left != right)))
        previous = current
    return previous[-1]


async def run(manifest: Path, mode: str) -> dict[str, Any]:
    entries = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
    tp = fp = fn = exact = total_fields = numeric_exact = numeric_count = 0
    numeric_abs_errors = []
    cer_dist = cer_chars = wer_dist = wer_words = 0
    table_expected = Counter()
    table_predicted = Counter()
    table_shape_correct = table_shape_count = 0
    grounded_total = extracted_total = 0
    review_total = review_errors = auto_total = auto_correct = total_errors = missed_errors = 0
    latencies, results = [], []
    for entry in entries:
        file_path = (manifest.parent / entry["file"]).resolve()
        if not file_path.is_file():
            raise FileNotFoundError(f"Benchmark document does not exist: {file_path}")
        start = time.perf_counter()
        result = await process_document(file_path.name, file_path.read_bytes(), mode, entry.get("schema"))
        latency = time.perf_counter() - start
        latencies.append(latency)
        prediction = result.get("extraction") or {}
        predicted_fields = prediction.get("fields") if isinstance(prediction.get("fields"), dict) else {}
        expected = entry.get("expected", {})
        document_correct = True
        for key in set(expected) | set(predicted_fields):
            total_fields += 1
            if key not in predicted_fields:
                fn += 1
                document_correct = False
                continue
            if key not in expected:
                fp += 1
                document_correct = False
                continue
            expected_value, predicted_value = norm(expected[key]), norm(val(predicted_fields[key]))
            if expected_value == predicted_value:
                tp += 1
            else:
                fp += 1
                fn += 1
                document_correct = False
            try:
                exp_num, pred_num = float(re.sub(r"[^\d.-]", "", expected_value)), float(re.sub(r"[^\d.-]", "", predicted_value))
                numeric_count += 1
                numeric_exact += exp_num == pred_num
                numeric_abs_errors.append(abs(exp_num - pred_num))
            except (TypeError, ValueError):
                pass
        grounded = result.get("validation", {}).get("grounded_fields", {})
        grounded_total += sum(bool(item.get("grounded")) for item in grounded.values())
        extracted_total += len(grounded)
        for table in result.get("validation", {}).get("grounded_tables", []):
            grounded_total += int(table.get("grounded_rows", 0))
            extracted_total += int(table.get("total_rows", 0))
        expected_doc_cells = Counter()
        for row in entry.get("expected_table_cells", []):
            cells = [norm_cell(cell) for cell in row if str(cell).strip()]
            table_expected.update(cells)
            expected_doc_cells.update(cells)
        predicted_doc_cells = Counter()
        for table in prediction.get("tables", []) if isinstance(prediction.get("tables"), list) else []:
            matrix = table.get("matrix") or table.get("rows") or [] if isinstance(table, dict) else table
            for row in matrix if isinstance(matrix, list) else []:
                if isinstance(row, list):
                    cells = [norm_cell(cell) for cell in row if str(cell).strip()]
                    table_predicted.update(cells)
                    predicted_doc_cells.update(cells)
        table_cells_match = not expected_doc_cells or expected_doc_cells == predicted_doc_cells
        expected_shape = entry.get("expected_table_shape")
        shape_matches = True
        if expected_shape:
            table_shape_count += 1
            actual_shapes = [{"rows": len(table.get("rows", [])), "columns": len(table.get("headers", []))} for table in prediction.get("tables", []) if isinstance(table, dict)]
            shape_matches = expected_shape in actual_shapes
            table_shape_correct += shape_matches
        document_correct = document_correct and table_cells_match and shape_matches
        exact += document_correct
        review_required = bool(result.get("validation", {}).get("review_required"))
        if review_required:
            review_total += 1
            review_errors += not document_correct
        else:
            auto_total += 1
            auto_correct += document_correct
        total_errors += not document_correct
        missed_errors += (not document_correct) and not review_required
        if "expected_text" in entry:
            expected_text, actual_text = entry["expected_text"], "\n".join(part["text"] for part in result["segments"])
            cer_dist += edit_distance(list(expected_text), list(actual_text))
            cer_chars += len(expected_text)
            expected_words, actual_words = expected_text.split(), actual_text.split()
            wer_dist += edit_distance(expected_words, actual_words)
            wer_words += len(expected_words)
        results.append({"file": entry["file"], "latency_seconds": round(latency, 3), "exact_document_match": bool(document_correct), "table_cells_match": table_cells_match, "table_shape_match": shape_matches, "review_required": result.get("validation", {}).get("review_required"), "runtime": result.get("runtime"), "model_error": result.get("model_error")})
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "pipeline_version": "0.2.0",
        "dataset_version": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "models": {"text": OLLAMA_TEXT_MODEL, "vision": OLLAMA_VISION_MODEL},
        "mode": mode,
        "manifest": manifest.name,
        "documents": len(entries),
        "metrics": {
            "field_precision": precision, "field_recall": recall, "field_f1": f1,
            "document_exact_match": exact / len(entries) if entries else 0.0,
            "numeric_exact_accuracy": numeric_exact / numeric_count if numeric_count else None,
            "numeric_mean_absolute_error": statistics.mean(numeric_abs_errors) if numeric_abs_errors else None,
            "table_cell_accuracy": sum((table_expected & table_predicted).values()) / sum(table_expected.values()) if table_expected else None,
            "table_structure_consistency": table_shape_correct / table_shape_count if table_shape_count else None,
            "groundedness_score": grounded_total / extracted_total if extracted_total else None,
            "hallucination_rate": 1 - grounded_total / extracted_total if extracted_total else None,
            "auto_processing_accuracy": auto_correct / auto_total if auto_total else None,
            "human_review_precision": review_errors / review_total if review_total else None,
            "missed_error_rate": missed_errors / total_errors if total_errors else 0.0,
            "cer": cer_dist / cer_chars if cer_chars else None,
            "wer": wer_dist / wer_words if wer_words else None,
            "mean_latency_seconds": statistics.mean(latencies) if latencies else None,
            "throughput_documents_per_second": len(entries) / sum(latencies) if sum(latencies) else None,
        },
        "documents_detail": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path, help="Held-out benchmark JSONL")
    parser.add_argument("--mode", choices=["rules", "text", "vision_zero_shot", "vision", "auto"], default="auto")
    parser.add_argument("--output", type=Path, default=Path("runs/latest.json"))
    parser.add_argument("--min-f1", type=float, default=None, help="Fail with exit code 2 if field F1 is below this threshold")
    args = parser.parse_args()
    report = asyncio.run(run(args.manifest.resolve(), args.mode))
    target = args.output if args.output.is_absolute() else ROOT / args.output
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(target), **report["metrics"]}, indent=2))
    if args.min_f1 is not None and report["metrics"]["field_f1"] < args.min_f1:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
