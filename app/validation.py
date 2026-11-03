"""Evidence matching, deterministic checks, and review routing."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any


def _field_parts(field: Any) -> tuple[Any, str | None, int | None]:
    if isinstance(field, dict):
        value = field.get("value")
        evidence = field.get("evidence") or field.get("quote")
        page = field.get("page") or field.get("page_number")
    else:
        value, evidence, page = field, None, None
    try:
        page = int(page) if page is not None else None
    except (TypeError, ValueError):
        page = None
    if page is not None and page <= 0:
        page = None
    return value, str(evidence).strip() if evidence else None, page


def _money(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).strip()
    negative = raw.startswith("(") and raw.endswith(")")
    cleaned = re.sub(r"[^0-9,.\-]", "", raw).replace(",", "")
    try:
        result = float(cleaned)
        return -abs(result) if negative else result
    except ValueError:
        return None


def _fold(text: str) -> str:
    return re.sub(r"[\W_]+", " ", text, flags=re.UNICODE).strip().casefold()


def _column_key(label: str) -> str:
    text = _fold(label)
    if "code" in text:
        return "subject_code"
    if "subject" in text:
        return "subject"
    if "theory" in text:
        return "theory"
    if any(token in text.split() for token in ("practical", "internal", "ia", "pr")) or text == "ao":
        return "practical"
    if "total" in text and "word" not in text:
        return "total"
    if "grade" in text:
        return "grade"
    if "word" in text:
        return "total_in_words"
    return text.replace(" ", "_")


def _anchor_matches(anchor: str, text: str) -> bool:
    anchor_tokens = set(_fold(anchor).split())
    text_tokens = set(_fold(text).split())
    if not anchor_tokens:
        return False
    overlap = len(anchor_tokens & text_tokens)
    return overlap >= max(1, (len(anchor_tokens) + 1) // 2)


def _table_cell_in_region(cell: str, header: str, region_text: str) -> bool:
    column = _column_key(header)
    candidate = region_text
    if column in {"subject_code", "theory", "practical", "total", "number", "marks_obtained", "amount", "price", "unit_price", "subtotal", "tax", "discount", "quantity"}:
        candidate = re.sub(r"\bo(?=\d)", "0", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"(?<=\d)o(?=\d)", "0", candidate, flags=re.IGNORECASE)
        target_text = re.sub(r"[^0-9.,+-]", "", cell).replace(",", "")
        try:
            target_number = Decimal(target_text)
            source_numbers = []
            for token in re.findall(r"(?<![A-Za-z])[-+]?\d[\d,]*(?:\.\d+)?", candidate):
                try:
                    source_numbers.append(Decimal(token.replace(",", "")))
                except InvalidOperation:
                    continue
            return target_number in source_numbers
        except (InvalidOperation, ValueError):
            return False
    if column == "grade":
        candidate = re.sub(r"\b([A-E])[lI]\b", r"\g<1>1", candidate, flags=re.IGNORECASE)
    return _fold(cell) in _fold(candidate)


def _row_region_conflicts(row: list[Any], headers: list[Any], region_text: str) -> bool:
    """Only call OCR disagreement when a layout read a complete numeric row."""
    numeric_headers = {"subject_code", "theory", "practical", "total", "number", "marks_obtained", "amount", "price", "unit_price", "subtotal", "tax", "discount", "quantity"}
    expected = []
    for index, cell in enumerate(row):
        if index >= len(headers) or _column_key(str(headers[index])) not in numeric_headers or cell in (None, ""):
            continue
        raw = re.sub(r"[^0-9.,+-]", "", str(cell)).replace(",", "")
        try:
            expected.append(Decimal(raw))
        except InvalidOperation:
            pass
    if not expected:
        return False
    source = []
    for token in re.findall(r"(?<![A-Za-z])[-+]?\d[\d,]*(?:\.\d+)?", region_text):
        try:
            source.append(Decimal(token.replace(",", "")))
        except InvalidOperation:
            continue
    if len(source) < len(expected):
        return False
    return not all(value in source for value in expected)


def _ocr_item_row_count(segments: list[dict[str, Any]]) -> int | None:
    centers: list[tuple[Any, float]] = []
    for segment in segments:
        region_sets = [segment.get("regions", [])]
        region_sets.extend(item.get("regions", []) for item in segment.get("ocr_alternates", []) if isinstance(item, dict))
        for regions in region_sets:
            for region in regions:
                text = str(region.get("text", "")).strip()
                bbox = region.get("bbox")
                if not re.match(r"^\d{1,3}[.\s|):-]+[A-Za-z][A-Za-z &'-]{1,}", text) or not isinstance(bbox, list) or len(bbox) < 4:
                    continue
                center = (float(bbox[1]) + float(bbox[3])) / 2
                page = segment.get("index", segment.get("label"))
                if not any(existing_page == page and abs(center - existing_center) <= 0.012 for existing_page, existing_center in centers):
                    centers.append((page, center))
    return len(centers) if centers else None


def complete_table_grades_from_ocr(extraction: dict[str, Any] | None, segments: list[dict[str, Any]]) -> None:
    """Fill a missing grade only when independent OCR layouts agree on the printed code."""
    if not isinstance(extraction, dict):
        return
    tables = extraction.get("tables") if isinstance(extraction.get("tables"), list) else []
    for table in tables:
        if not isinstance(table, dict):
            continue
        headers = table.get("headers") if isinstance(table.get("headers"), list) else []
        subject_index = next((i for i, header in enumerate(headers) if _column_key(str(header)) == "subject"), None)
        grade_index = next((i for i, header in enumerate(headers) if _column_key(str(header)) == "grade"), None)
        if subject_index is None or grade_index is None:
            continue
        rows = table.get("rows", [])
        if rows and all(isinstance(row, list) and len(row) > grade_index for row in rows):
            for segment in segments:
                grade_regions = next((
                    item.get("regions", []) for item in segment.get("ocr_alternates", [])
                    if isinstance(item, dict) and item.get("method") == "tesseract_marksheet_grade_crop_psm11"
                ), [])
                readings = []
                for region in grade_regions:
                    token = str(region.get("text", "")).strip()
                    match = re.fullmatch(r"([A-E6])\s*([12lI|!TEZ])", token, re.IGNORECASE)
                    numeric_match = re.fullmatch(r"0\s*([12])", token)
                    if not match and not numeric_match:
                        continue
                    if numeric_match:
                        grade = "A" + numeric_match.group(1)
                    else:
                        letter, suffix = match.group(1).upper(), match.group(2).upper()
                        grade = ("C" if letter == "6" else letter) + ("2" if suffix in {"2", "Z", "E"} else "1")
                    bbox = region.get("bbox", [])
                    center = (float(bbox[1]) + float(bbox[3])) / 2 if isinstance(bbox, list) and len(bbox) >= 4 else 0.0
                    readings.append((center, grade))
                readings.sort()
                if len(readings) == len(rows) and all(re.fullmatch(r"[A-E][12]", grade) for _, grade in readings):
                    used_readings: set[int] = set()
                    assignments: list[tuple[list[Any], str, int]] = []
                    code_index = next((i for i, header in enumerate(headers) if _column_key(str(header)) == "subject_code"), None)
                    for row in rows:
                        anchor = str(row[subject_index]).strip()
                        code = str(row[code_index]).strip() if code_index is not None and len(row) > code_index else ""
                        centers = []
                        for layout in [segment.get("regions", []), *(
                            item.get("regions", []) for item in segment.get("ocr_alternates", []) if isinstance(item, dict)
                        )]:
                            for region in layout:
                                bbox = region.get("bbox")
                                region_text = str(region.get("text", ""))
                                code_match = bool(code and re.search(rf"(?<!\d){re.escape(code)}(?!\d)", region_text))
                                subject_match = _anchor_matches(anchor, region_text)
                                if (code_match or subject_match) and isinstance(bbox, list) and len(bbox) >= 4:
                                    centers.append((float(bbox[1]) + float(bbox[3])) / 2)
                        if not centers:
                            continue
                        row_center = sorted(centers)[len(centers) // 2]
                        # Text boxes sit slightly below the grade glyph boxes
                        # in this narrow printed column; compensate for that
                        # baseline offset before pairing by vertical position.
                        target_center = row_center - 0.01
                        nearest = min(range(len(readings)), key=lambda i: abs(readings[i][0] - target_center))
                        if abs(readings[nearest][0] - target_center) <= 0.04 and nearest not in used_readings:
                            assignments.append((row, readings[nearest][1], nearest))
                            used_readings.add(nearest)
                    if len(assignments) == len(rows):
                        for row, grade, _index in assignments:
                            row[grade_index] = grade
                        row_order = {id(row): reading_index for row, _grade, reading_index in assignments}
                        order = sorted(range(len(rows)), key=lambda i: row_order.get(id(rows[i]), i))
                        table["rows"] = [rows[i] for i in order]
                        for key in ("row_evidence", "row_provenance"):
                            values = table.get(key)
                            if isinstance(values, list) and len(values) == len(rows):
                                table[key] = [values[i] for i in order]
                    else:
                        for row, (_center, grade) in zip(rows, readings):
                            row[grade_index] = grade
                    break
        for row in table.get("rows", []):
            if not isinstance(row, list) or max(subject_index, grade_index) >= len(row):
                continue
            if re.fullmatch(r"[A-E][1-2]", str(row[grade_index]).strip(), re.IGNORECASE):
                continue
            anchor = str(row[subject_index]).strip()
            if not anchor:
                continue
            layout_codes: list[set[str]] = []
            for segment in segments:
                region_sets = [("tesseract_psm3", segment.get("regions", []))]
                region_sets.extend((str(item.get("method", "tesseract_alternate")), item.get("regions", [])) for item in segment.get("ocr_alternates", []) if isinstance(item, dict))
                for _method, regions in region_sets:
                    matched_regions = [region for region in regions if _anchor_matches(anchor, str(region.get("text", "")))]
                    if not matched_regions:
                        continue
                    codes = set()
                    for region in matched_regions:
                        for match in re.finditer(r"\b([A-E])([12lI])\b", str(region.get("text", "")), re.IGNORECASE):
                            digit = "1" if match.group(2).casefold() in {"l", "i"} else match.group(2)
                            codes.add(match.group(1).upper() + digit)
                    if codes:
                        layout_codes.append(codes)
            if len(layout_codes) >= 2 and all(len(codes) == 1 for codes in layout_codes):
                consensus = {next(iter(codes)) for codes in layout_codes}
                if len(consensus) == 1:
                    row[grade_index] = next(iter(consensus))


def _fold_with_offsets(text: str) -> tuple[str, list[int], list[int]]:
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    index = 0
    while index < len(text):
        if text[index].isspace():
            end = index + 1
            while end < len(text) and text[end].isspace():
                end += 1
            if chars and chars[-1] != " ":
                chars.append(" ")
                starts.append(index)
                ends.append(end)
            index = end
            continue
        folded = text[index].casefold()
        if not text[index].isalnum():
            if chars and chars[-1] != " ":
                chars.append(" ")
                starts.append(index)
                ends.append(index + 1)
            index += 1
            continue
        for char in folded:
            chars.append(char)
            starts.append(index)
            ends.append(index + 1)
        index += 1
    while chars and chars[-1] == " ":
        chars.pop(); starts.pop(); ends.pop()
    return "".join(chars), starts, ends


def _segment_match(evidence: str, segment: dict[str, Any]) -> dict[str, Any] | None:
    folded_evidence = _fold(evidence)
    if not folded_evidence:
        return None
    region_sets = [("tesseract_psm3", segment.get("regions", []))]
    region_sets.extend((str(item.get("method", "tesseract_alternate")), item.get("regions", [])) for item in segment.get("ocr_alternates", []) if isinstance(item, dict))
    for method, regions in region_sets:
        for region in regions:
            if folded_evidence in _fold(region.get("text", "")):
                return {"segment_index": segment["index"], "label": segment.get("label"), "bbox": region.get("bbox"), "region_text": region.get("text"), "method": method}
    text = segment.get("text", "")
    folded_text, starts, ends = _fold_with_offsets(text)
    position = folded_text.find(folded_evidence)
    if position >= 0:
        end_position = position + len(folded_evidence) - 1
        return {"segment_index": segment["index"], "label": segment.get("label"), "start": starts[position], "end": ends[end_position], "bbox": None}
    return None


def validate_extraction(extraction: dict[str, Any] | None, segments: list[dict[str, Any]], schema: dict[str, Any] | None = None) -> dict[str, Any]:
    if not extraction:
        return {"status": "not_run", "checks": [], "grounded_fields": {}, "review_required": True, "review_reasons": ["Model extraction was not available."]}

    fields = extraction.get("fields") if isinstance(extraction.get("fields"), dict) else {}
    grounded: dict[str, dict[str, Any]] = {}
    reasons: list[str] = []
    schema_fields = schema.get("fields", {}) if isinstance(schema, dict) else {}
    if not isinstance(schema_fields, dict):
        schema_fields = {}
    for name, spec in schema_fields.items():
        field_type = str(spec.get("type", "") if isinstance(spec, dict) else spec).lower()
        required = not isinstance(spec, dict) or spec.get("required", True)
        if required and field_type != "table" and (name not in fields or _field_parts(fields.get(name))[0] in (None, "")):
            grounded[name] = {"value": None, "evidence": None, "source": None, "confidence": 0.0, "grounded": False}
            reasons.append(f"Requested field '{name}' is missing.")
    for name, raw in fields.items():
        value, evidence, page = _field_parts(raw)
        match = None
        match_quote = evidence
        if evidence:
            for segment in segments:
                if page is not None and segment.get("index") != page and segment.get("label") != str(page):
                    continue
                match = _segment_match(evidence, segment)
                if match:
                    break
        if not match and isinstance(value, (str, int, float)) and str(value).strip():
            match_quote = str(value).strip()
            for segment in segments:
                if page is not None and segment.get("index") != page and segment.get("label") != str(page):
                    continue
                match = _segment_match(match_quote, segment)
                if match:
                    break
        score = 1.0 if match and match.get("bbox") else (0.9 if match else (0.35 if evidence else 0.1))
        grounded[name] = {"value": value, "evidence": match_quote if match else evidence, "source": match, "confidence": score, "grounded": bool(match)}
        if value not in (None, "") and not match:
            reasons.append(f"Field '{name}' does not have an exact source quote match.")

    currency_field = fields.get("currency")
    currency_value, _, _ = _field_parts(currency_field)
    currency_symbol_value, _, _ = _field_parts(fields.get("currency_symbol"))
    if not currency_value:
        if currency_symbol_value:
            reasons.append(f"Currency symbol '{currency_symbol_value}' is present, but its ISO currency code is ambiguous or unreadable; confirm it manually.")
        elif "currency" in schema_fields:
            reasons.append("A printed currency code or unambiguous currency symbol could not be established; confirm the currency manually.")
    elif currency_value and not grounded.get("currency", {}).get("grounded", False):
        reasons.append(f"Currency code '{currency_value}' is not supported by printed source evidence; confirm it against the transaction amounts.")

    expected_tables = []
    expected_columns: dict[str, list[str]] = {}
    if isinstance(schema_fields, dict):
        expected_tables = [name for name, spec in schema_fields.items() if str(spec.get("type", "") if isinstance(spec, dict) else spec).lower() == "table" and (not isinstance(spec, dict) or spec.get("required", True))]
        expected_columns = {name: spec.get("columns", []) for name, spec in schema_fields.items() if isinstance(spec, dict) and isinstance(spec.get("columns"), list)}
    tables = extraction.get("tables") if isinstance(extraction.get("tables"), list) else []
    table_names = {str(table.get("name", "")) for table in tables if isinstance(table, dict)}
    grounded_tables: list[dict[str, Any]] = []
    for expected_name in expected_tables:
        if expected_name not in table_names:
            reasons.append(f"Requested table '{expected_name}' was not returned.")
        elif expected_columns.get(expected_name):
            table = next((item for item in tables if isinstance(item, dict) and item.get("name") == expected_name), None)
            available = {_column_key(str(header)) for header in table.get("headers", [])} if isinstance(table, dict) else set()
            missing = [column for column in expected_columns[expected_name] if _column_key(str(column)) not in available]
            if missing:
                reasons.append(f"Table '{expected_name}' is missing requested columns: {', '.join(missing)}.")
            if isinstance(table, dict) and not table.get("rows"):
                reasons.append(f"Requested table '{expected_name}' has no rows.")
    for table_index, table in enumerate(tables):
        if not isinstance(table, dict):
            reasons.append(f"Table {table_index + 1} has an invalid structure.")
            grounded_tables.append({"name": None, "grounded_rows": 0, "total_rows": 0})
            continue
        rows = table.get("rows") if isinstance(table.get("rows"), list) else []
        for conflict in table.get("row_conflicts", []) if isinstance(table.get("row_conflicts"), list) else []:
            indexes = conflict.get("row_indexes", []) if isinstance(conflict, dict) else []
            reasons.append(f"Table '{table.get('name', table_index + 1)}' contains conflicting readings for row {', '.join(str(int(i) + 1) for i in indexes if isinstance(i, int))}; review the source.")
        quotes = table.get("row_evidence") if isinstance(table.get("row_evidence"), list) else []
        provenances = table.get("row_provenance") if isinstance(table.get("row_provenance"), list) else []
        page = table.get("page")
        try:
            page = int(page) if page and int(page) > 0 else None
        except (TypeError, ValueError):
            page = None
        grounded_rows = 0
        row_sources = []
        for row_index, row in enumerate(rows):
            cells = [str(cell).strip() for cell in row if cell is not None and str(cell).strip()] if isinstance(row, list) else []
            quote = str(quotes[row_index]).strip() if row_index < len(quotes) and quotes[row_index] else ""
            if not quote and cells:
                quote = " ".join(cells)
            match = None
            variant_support: list[bool] = []
            if isinstance(row, list) and table.get("headers"):
                subject_index = next((i for i, header in enumerate(table["headers"]) if _column_key(str(header)) == "subject"), None)
            else:
                subject_index = None
            anchor = str(row[subject_index]).strip() if subject_index is not None and subject_index < len(row) else next((cell for cell in cells if re.search(r"[^\W\d_]{2}", cell, re.UNICODE) and not re.fullmatch(r"[A-E][1-2]", cell, re.IGNORECASE)), "")
            grade_indexes = [i for i, header in enumerate(table.get("headers", [])) if _column_key(str(header)) == "grade"]
            grade_values_ok = all(i < len(row) and bool(re.fullmatch(r"[A-E][1-2]", str(row[i]).strip(), re.IGNORECASE)) for i in grade_indexes) if isinstance(row, list) else True
            if cells and anchor:
                for segment in segments:
                    if page is not None and segment.get("index") != page and segment.get("label") != str(page):
                        continue
                    region_sets = [("tesseract_psm3", segment.get("regions", []))]
                    region_sets.extend((str(item.get("method", "tesseract_alternate")), item.get("regions", [])) for item in segment.get("ocr_alternates", []) if isinstance(item, dict))
                    for method, regions in region_sets:
                        candidates = [region for region in regions if _anchor_matches(anchor, str(region.get("text", "")))]
                        if not candidates:
                            continue
                        supporting = [
                            region for region in candidates
                            if isinstance(row, list)
                            and all(
                                _table_cell_in_region(str(cell), str(table["headers"][i]) if i < len(table.get("headers", [])) else "", str(region.get("text", "")))
                                for i, cell in enumerate(row) if cell is not None and str(cell).strip()
                            )
                        ]
                        if supporting:
                            variant_support.append(True)
                        elif any(_row_region_conflicts(row, table.get("headers", []), str(region.get("text", ""))) for region in candidates):
                            variant_support.append(False)
                        if supporting and match is None:
                            region = supporting[0]
                            match = {"segment_index": segment["index"], "label": segment.get("label"), "bbox": region.get("bbox"), "region_text": region.get("text"), "method": method}
            row_width_ok = not table.get("headers") or not isinstance(row, list) or len(row) == len(table["headers"])
            variants_agree = not variant_support or all(variant_support)
            if row_index < len(provenances) and provenances[row_index] == "document_parser" and quote:
                match = {"segment_index": segment.get("index"), "label": segment.get("label"), "bbox": None, "region_text": quote, "method": "document_parser"}
            row_grounded = bool(match and row_width_ok and grade_values_ok and variants_agree)
            if row_grounded:
                grounded_rows += 1
            else:
                detail = "conflicting OCR layouts or no exact source quote match"
                if not grade_values_ok:
                    detail = "invalid or missing grade value"
                reasons.append(f"Table '{table.get('name', table_index + 1)}' row {row_index + 1} has {detail}.")
            row_sources.append({"row": row_index + 1, "grounded": row_grounded, "evidence": match.get("region_text") if match else quote, "source": match, "ocr_variants_agree": variants_agree})
        if len(quotes) != len(rows):
            reasons.append(f"Table '{table.get('name', table_index + 1)}' has a different number of evidence quotes and rows.")
        source_codes: set[str] = set()
        expected_table_columns = expected_columns.get(str(table.get("name")), [])
        has_subject_code = any(_column_key(str(column)) == "subject_code" for column in expected_table_columns) or any(_column_key(str(header)) == "subject_code" for header in table.get("headers", []))
        if has_subject_code:
            for segment in segments:
                # Whole-page OCR commonly mistakes marks such as 083 or 055
                # for subject codes. Only count row-shaped evidence from the
                # targeted table crop, where the code and subject are on one
                # aligned row.
                region_sets = [
                    item.get("regions", [])
                    for item in segment.get("ocr_alternates", [])
                    if isinstance(item, dict)
                    and item.get("method") == "tesseract_marksheet_table_crop_psm6"
                ]
                for regions in region_sets:
                    for region in regions:
                        text = str(region.get("text", "")).strip()
                        match = re.search(r"(?<!\d)([0-9OISLZABG]{3})[|\])]?\s+[^\d\W_]{2,}", text, re.UNICODE | re.IGNORECASE)
                        has_marks = len(re.findall(r"(?<!\w)(?:\d{2,3}|X{1,3})(?!\w)", text, re.IGNORECASE)) >= 2
                        if match and has_marks:
                            digits = {"O": "0", "I": "1", "S": "5", "L": "1", "Z": "2", "A": "8", "B": "8", "G": "6"}
                            source_codes.add("".join(digits.get(char.upper(), char) for char in match.group(1)))
        actual_codes: set[str] = set()
        code_index = next((i for i, header in enumerate(table.get("headers", [])) if _column_key(str(header)) == "subject_code"), None)
        if code_index is not None:
            actual_codes = {str(row[code_index]).strip() for row in rows if isinstance(row, list) and len(row) > code_index and str(row[code_index]).strip()}
        missing_codes = sorted(source_codes - actual_codes)
        if source_codes and missing_codes:
            reasons.append(f"Table '{table.get('name', table_index + 1)}' is missing source rows with subject codes: {', '.join(missing_codes)}.")
        source_row_count = len(source_codes) if source_codes else None
        if any(token in str(table.get("name", "")).casefold() for token in ("item", "line")):
            ocr_row_count = _ocr_item_row_count(segments)
            if ocr_row_count is not None:
                source_row_count = max(source_row_count or 0, ocr_row_count)
                if ocr_row_count > len(rows):
                    missing_count = ocr_row_count - len(rows)
                    reasons.append(f"Table '{table.get('name', table_index + 1)}' may be missing {missing_count} printed item row(s) detected by OCR.")
        grounded_tables.append({"name": table.get("name"), "grounded_rows": grounded_rows, "total_rows": len(rows), "source_rows": source_row_count, "rows": row_sources})

    checks: list[dict[str, Any]] = []
    normalized = {key.lower().replace(" ", "_"): _field_parts(val)[0] for key, val in fields.items()}
    subtotal = _money(normalized.get("subtotal"))
    tax = _money(normalized.get("tax") or normalized.get("tax_amount"))
    total = _money(normalized.get("total") or normalized.get("invoice_total"))
    if subtotal is not None and tax is not None and total is not None:
        passed = abs((subtotal + tax) - total) <= 0.02
        checks.append({"name": "subtotal_plus_tax_equals_total", "passed": passed, "expected": round(subtotal + tax, 2), "actual": round(total, 2)})
        if not passed:
            reasons.append("Invoice arithmetic check failed: subtotal plus tax does not equal total.")

    line_items = normalized.get("line_items") or normalized.get("items")
    if isinstance(line_items, list) and total is not None:
        line_sum = 0.0
        count = 0
        for item in line_items:
            if not isinstance(item, dict):
                continue
            quantity = _money(item.get("quantity") or item.get("qty"))
            unit_price = _money(item.get("unit_price") or item.get("price"))
            amount = _money(item.get("amount") or item.get("line_total"))
            calculated = quantity * unit_price if quantity is not None and unit_price is not None else amount
            if calculated is not None:
                line_sum += calculated
                count += 1
        if count and subtotal is not None:
            passed = abs(line_sum - subtotal) <= max(0.02, 0.001 * abs(subtotal))
            checks.append({"name": "line_items_sum_equals_subtotal", "passed": passed, "expected": round(line_sum, 2), "actual": round(subtotal, 2)})
            if not passed:
                reasons.append("Line-item arithmetic check failed: line items do not sum to subtotal.")

    if extraction.get("uncertainties"):
        reasons.append("The model reported one or more uncertain fields.")

    if any(value.get("confidence", 0) < 0.85 for value in grounded.values()):
        reasons.append("One or more fields have weak or missing source grounding.")

    if not fields:
        reasons.append("The model returned no fields to validate.")
    review_required = bool(reasons)
    return {
        "status": "needs_review" if review_required else "validated",
        "checks": checks,
        "grounded_fields": grounded,
        "grounded_tables": grounded_tables,
        "review_required": review_required,
        "review_reasons": reasons,
        "confidence_method": "evidence match heuristic; not a calibrated probability",
    }
