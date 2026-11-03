"""Shared sync/async processing path for direct extraction and background jobs."""
from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from typing import Any

from .baselines import extract_parser_tables, extract_rules, merge_rule_supplements
from .config import OCR_ENABLED
from .config import OLLAMA_TEXT_MODEL, OLLAMA_VISION_MODEL
from .extractors import extract_file
from .models import extract_with_ollama, unload_ollama_model
from .validation import complete_table_grades_from_ocr, validate_extraction


def _ocr_line_key(line: str) -> str:
    """Normalize a line for duplicate checks without discarding non-Latin text."""
    return "".join(
        char for char in line.casefold()
        if unicodedata.category(char)[0] in {"L", "N", "M"}
    )


def _deduplicated_ocr_text(segments: list[dict[str, Any]]) -> str:
    """Merge OCR layouts while removing repeated lines from alternate passes."""
    pages: list[str] = []
    for segment in segments:
        candidates = [str(segment.get("text", ""))]
        candidates.extend(
            str(item.get("text", ""))
            for item in segment.get("ocr_alternates", [])
            if isinstance(item, dict)
            and item.get("method") != "tesseract_marksheet_grade_crop_psm11"
        )
        lines: list[str] = []
        seen: set[str] = set()
        skip_targeted_block = False
        for candidate in candidates:
            for raw_line in candidate.splitlines():
                line = re.sub(r"\s+", " ", raw_line).strip()
                if skip_targeted_block:
                    if not line.startswith("["):
                        continue
                    skip_targeted_block = False
                if re.match(r"^\[Targeted marksheet (?:identity|grade) OCR", line, re.IGNORECASE):
                    skip_targeted_block = True
                    continue
                if not line or re.match(r"^\[(?:alternative OCR layout|enhanced OCR layout|targeted (?:item row|marksheet (?:table|identity|grade)) OCR)", line, re.IGNORECASE):
                    continue
                key = _ocr_line_key(line)
                if not key or key in seen:
                    continue
                seen.add(key)
                lines.append(line)
        page_label = str(segment.get("label") or f"Page {segment.get('index', '?')}")
        pages.append(f"[{page_label}]\n" + "\n".join(lines))
    return "\n\n".join(pages)


def _ocr_conflicts(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Record conflicting reads for identically labeled lines without guessing."""
    conflicts: list[dict[str, Any]] = []
    for segment in segments:
        reads = [
            ("primary", str(segment.get("text", ""))),
            *(
                (str(item.get("method", "alternate")), str(item.get("text", "")))
                for item in segment.get("ocr_alternates", [])
                if isinstance(item, dict) and item.get("text")
            ),
        ]
        by_label: dict[str, dict[str, str]] = {}
        labels: dict[str, str] = {}
        for method, content in reads:
            for line in content.splitlines():
                match = re.match(r"^\s*([^:：=|]{2,48})\s*[:：=|]\s*(\S.*)$", line)
                if not match:
                    continue
                label, value = match.group(1).strip(), re.sub(r"\s+", " ", match.group(2)).strip()
                key = _ocr_line_key(label)
                if not key or not value or len(key) < 2:
                    continue
                labels.setdefault(key, label)
                by_label.setdefault(key, {})[method] = value
        for key, readings in by_label.items():
            distinct = list(dict.fromkeys(value for value in readings.values()))
            if len(distinct) > 1:
                conflicts.append({
                    "page": segment.get("index"),
                    "label": labels[key],
                    "readings": [{"method": method, "value": value} for method, value in readings.items()],
                    "status": "unresolved",
                })
    return conflicts


def _compact_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep useful readable OCR in saved JSON; drop duplicated layout diagnostics."""
    compact: list[dict[str, Any]] = []
    for segment in segments:
        primary = str(segment.get("text", "")).split("[Alternative OCR layout PSM", 1)[0]
        primary_lines: list[str] = []
        seen: set[str] = set()
        for raw_line in primary.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            if not line or line.startswith("[Targeted item row OCR"):
                continue
            key = _ocr_line_key(line)
            if key and key not in seen:
                seen.add(key)
                primary_lines.append(line)
        item = {
            "index": segment.get("index"),
            "method": segment.get("method"),
            "text": "\n".join(primary_lines),
        }
        if segment.get("label"):
            item["label"] = segment["label"]
        compact.append(item)
    return compact


def _continuation_text_batches(segments: list[dict[str, Any]], first_page_to_skip: int, limit: int = 9000) -> list[str]:
    """Pack later PDF pages into model-sized OCR-only batches, preserving page labels."""
    batches: list[str] = []
    current = ""
    for segment in segments:
        page = int(segment.get("index") or 0)
        if page <= first_page_to_skip:
            continue
        text = str(segment.get("_clean_text", "")).strip()
        if not text:
            continue
        if len(text) <= limit:
            pieces = [text]
        else:
            pieces, piece = [], ""
            for line in text.splitlines():
                if piece and len(piece) + len(line) + 1 > limit:
                    pieces.append(piece)
                    piece = ""
                piece = f"{piece}\n{line}" if piece else line
            if piece:
                pieces.append(piece)
        for piece in pieces:
            if current and len(current) + len(piece) + 2 > limit:
                batches.append(current)
                current = ""
            current = f"{current}\n\n{piece}" if current else piece
    if current:
        batches.append(current)
    return batches


def _merge_continuation(primary: dict[str, Any], continuation: dict[str, Any]) -> list[str]:
    """Fill missing scalar values and append new OCR-only table rows without overwriting disagreements."""
    disagreements: list[str] = []
    fields = primary.setdefault("fields", {})
    for name, field in continuation.get("fields", {}).items():
        incoming = field.get("value") if isinstance(field, dict) else field
        if incoming in (None, "", [], {}):
            continue
        existing = fields.get(name)
        current = existing.get("value") if isinstance(existing, dict) else existing
        if current in (None, "", [], {}):
            fields[name] = field
        elif str(current).strip() != str(incoming).strip():
            disagreements.append(f"Later-page OCR gave a different value for '{name}'; the first extracted value was kept for review.")
    tables = primary.setdefault("tables", [])
    for incoming_table in continuation.get("tables", []):
        if not isinstance(incoming_table, dict):
            continue
        target = next((item for item in tables if isinstance(item, dict) and item.get("name") == incoming_table.get("name")), None)
        if target is None:
            tables.append(incoming_table)
            continue
        if not target.get("headers") and incoming_table.get("headers"):
            target["headers"] = incoming_table["headers"]
        target.setdefault("row_evidence", [])
        target["row_evidence"] = list(target["row_evidence"]) + [""] * max(0, len(target.get("rows", [])) - len(target["row_evidence"]))
        for index, row in enumerate(incoming_table.get("rows", [])):
            if not isinstance(row, list):
                continue
            if not any(str(cell).strip() for cell in row):
                continue
            target.setdefault("rows", []).append(row)
            evidence = incoming_table.get("row_evidence", [])
            target["row_evidence"].append(evidence[index] if index < len(evidence) else "")
    for name in ("checks", "uncertainties"):
        primary[name] = list(dict.fromkeys([*primary.get(name, []), *continuation.get(name, [])]))
    return disagreements


async def process_document(filename: str, data: bytes, mode: str = "auto", schema: dict[str, Any] | None = None) -> dict[str, Any]:
    total_started = time.perf_counter()
    parse_started = time.perf_counter()
    parsed = await asyncio.to_thread(extract_file, filename, data, OCR_ENABLED)
    document_text = _deduplicated_ocr_text(parsed["segments"])
    ocr_conflicts = _ocr_conflicts(parsed["segments"])
    if ocr_conflicts:
        conflict_lines = ["[OCR readings that conflict; resolve only against page image]"]
        for conflict in ocr_conflicts:
            readings = " | ".join(f"{item['method']}: {item['value']}" for item in conflict["readings"])
            conflict_lines.append(f"Page {conflict['page']} / {conflict['label']}: {readings}")
        document_text += "\n\n" + "\n".join(conflict_lines)
    for segment, page_text in zip(parsed["segments"], document_text.split("\n\n")):
        segment["_clean_text"] = page_text
    parse_ms = (time.perf_counter() - parse_started) * 1000
    extraction = None
    inference_mode = mode
    inference_metrics = None
    result: dict[str, Any] = {
        "filename": filename,
        "file_type": parsed["file_type"],
        "segments": parsed["segments"],
        "notes": parsed["notes"],
        "extraction": None,
        "mode": mode,
        "requested_schema": schema,
    }
    if ocr_conflicts:
        result["ocr_conflicts"] = ocr_conflicts
    if mode == "rules":
        extraction = extract_rules(document_text, schema)
        extraction["tables"].extend(extract_parser_tables(parsed["segments"], schema))
        result["model"] = "local OCR and rules (no Ollama)"
    else:
        if mode in {"vision", "vision_zero_shot"} and not parsed["visual_images"]:
            inference_mode = "text"
            parsed["notes"].append("No embedded or page image was available; used the local text model for this file.")
            result["notes"] = parsed["notes"]
        try:
            model_started = time.perf_counter()
            model_text = "" if inference_mode == "vision_zero_shot" else document_text
            extraction, inference_metrics = await extract_with_ollama(model_text, filename, parsed["visual_images"], inference_mode, schema)
            inference_metrics["wall_ms"] = round((time.perf_counter() - model_started) * 1000, 2)
            result["model"] = "local Ollama"
        except Exception as exc:
            can_retry_with_ocr = mode in {"vision", "auto"} and bool(parsed["visual_images"]) and bool(document_text.strip())
            if can_retry_with_ocr:
                try:
                    try:
                        await unload_ollama_model(OLLAMA_VISION_MODEL)
                    except Exception:
                        # A stopped/unreachable Ollama server will report a clearer
                        # failure on the text retry below.
                        pass
                    fallback_started = time.perf_counter()
                    extraction, inference_metrics = await extract_with_ollama(document_text, filename, [], "text", schema)
                    inference_metrics["wall_ms"] = round((time.perf_counter() - fallback_started) * 1000, 2)
                    inference_mode = "text"
                    result["model"] = "local Ollama (OCR text fallback)"
                    result["model_fallback"] = {"from": "vision", "reason": f"{type(exc).__name__}: {exc}"}
                    result["notes"].append("Vision extraction failed; retried locally using OCR text. Review is still required when values or table rows are not grounded.")
                except Exception as fallback_exc:
                    if mode == "auto":
                        extraction = extract_rules(document_text, schema)
                        extraction["tables"].extend(extract_parser_tables(parsed["segments"], schema))
                        inference_mode = "rules"
                        result["model"] = "local OCR and rules (Ollama unavailable)"
                        result["model_fallback"] = {"from": "auto", "reason": f"{type(fallback_exc).__name__}: {fallback_exc}"}
                        result["notes"].append("Ollama was unavailable, so the document was structured locally from OCR and document rules.")
                    else:
                        result["model_error"] = (
                            f"Vision extraction failed ({type(exc).__name__}: {exc}); "
                            f"OCR text fallback also failed ({type(fallback_exc).__name__}: {fallback_exc})."
                        )
            else:
                if mode == "auto":
                    extraction = extract_rules(document_text, schema)
                    extraction["tables"].extend(extract_parser_tables(parsed["segments"], schema))
                    inference_mode = "rules"
                    result["model"] = "local OCR and rules (Ollama unavailable)"
                    result["model_fallback"] = {"from": "auto", "reason": f"{type(exc).__name__}: {exc}"}
                    result["notes"].append("Ollama was unavailable, so the document was structured locally from OCR and document rules.")
                else:
                    result["model_error"] = f"Local Ollama extraction failed: {type(exc).__name__}: {exc}"
    result["model_mode"] = inference_mode
    continuation_disagreements: list[str] = []
    if (
        extraction is not None
        and inference_mode in {"vision", "vision_zero_shot", "text"}
        and parsed["file_type"] == ".pdf"
        and len(parsed["segments"]) > 6
    ):
        continuation_started = time.perf_counter()
        continuation_completed = 0
        batches: list[str] = []
        try:
            batches = _continuation_text_batches(parsed["segments"], first_page_to_skip=6)
            for batch_text in batches:
                continuation, _ = await extract_with_ollama(batch_text, filename, [], "text", schema)
                continuation_disagreements.extend(_merge_continuation(extraction, continuation))
                continuation_completed += 1
            result["page_continuation"] = {
                "pages_from": 7,
                "batches_planned": len(batches),
                "batches_completed": continuation_completed,
                "total_ms": round((time.perf_counter() - continuation_started) * 1000, 2),
            }
            if batches:
                if inference_mode in {"vision", "vision_zero_shot"}:
                    note = f"Vision reviewed the first 6 PDF pages; OCR text structuring also covered the remaining pages in {len(batches)} local batches."
                else:
                    note = f"OCR text structuring continued beyond page 6 in {len(batches)} local batches."
                result["notes"].append(note)
        except Exception as exc:
            result["page_continuation"] = {
                "pages_from": 7,
                "batches_planned": len(batches),
                "batches_completed": continuation_completed,
                "total_ms": round((time.perf_counter() - continuation_started) * 1000, 2),
            }
            result["notes"].append(f"Later-page OCR structuring did not finish ({type(exc).__name__}); extracted OCR text is still available for review.")
    if extraction is not None and mode != "rules" and inference_mode != "rules":
        rule_extraction = extract_rules(document_text, schema)
        rule_extraction["tables"].extend(extract_parser_tables(parsed["segments"], schema))
        supplement = merge_rule_supplements(extraction, rule_extraction, schema)
        result["rule_supplementation"] = supplement
    result["extraction"] = extraction
    complete_table_grades_from_ocr(extraction, parsed["segments"])
    result["validation"] = validate_extraction(extraction, parsed["segments"], schema)
    for disagreement in continuation_disagreements:
        if disagreement not in result["validation"]["review_reasons"]:
            result["validation"]["review_reasons"].append(disagreement)
    if continuation_disagreements:
        result["validation"]["review_required"] = True
        result["validation"]["status"] = "needs_review"
    if ocr_conflicts:
        result["validation"]["review_reasons"].append(f"{len(ocr_conflicts)} OCR label/value conflict(s) need visual review.")
        result["validation"]["review_required"] = True
        result["validation"]["status"] = "needs_review"
    for disagreement in result.get("rule_supplementation", {}).get("disagreements", []):
        if disagreement not in result["validation"]["review_reasons"]:
            result["validation"]["review_reasons"].append(disagreement)
    if result.get("rule_supplementation", {}).get("disagreements"):
        result["validation"]["review_required"] = True
        result["validation"]["status"] = "needs_review"
    result["runtime"] = {
        "pipeline_version": "0.3.0",
        "model_version": "rules-v1" if inference_mode == "rules" else (OLLAMA_VISION_MODEL if inference_mode in {"vision", "vision_zero_shot"} or (inference_mode == "auto" and parsed["visual_images"]) else OLLAMA_TEXT_MODEL),
        "parse_ms": round(parse_ms, 2),
        "inference": inference_metrics,
        "total_ms": round((time.perf_counter() - total_started) * 1000, 2),
        "input_bytes": len(data),
    }
    # Validation uses every OCR variant above; the saved output needs only one
    # deduplicated text view per page, not repeated PSM layout copies and boxes.
    result["segments"] = _compact_segments(parsed["segments"])
    return result
