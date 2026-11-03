"""Format-specific document text, table, region, and image extraction."""
from __future__ import annotations

import csv
import io
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from .config import OCR_CROSSCHECK, OCR_LANGUAGE, TESSERACT_CMD


MAX_VISION_PAGES = 6
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_EDGE = 4096
MAX_SHEET_ROWS = 5000
MAX_SHEET_COLUMNS = 50


def _open_image(image_bytes: bytes):
    from PIL import Image, ImageOps
    import warnings

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    with warnings.catch_warnings():
        # Images above the processing target are resized below. Pillow warns
        # during open before we can inspect and downsample their dimensions.
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        image = Image.open(io.BytesIO(image_bytes))
    # Honor camera/phone orientation tags before OCR and visual-model input.
    image = ImageOps.exif_transpose(image)
    if image.width * image.height > MAX_IMAGE_PIXELS * 2:
        raise ValueError(f"Image exceeds the {MAX_IMAGE_PIXELS * 2:,}-pixel safety limit")
    image.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
    return image.convert("RGB")


def _ocr_pass(image: Any, layout_mode: int, include_text: bool = True) -> tuple[str, list[dict[str, Any]]]:
    import pytesseract
    from pytesseract import Output

    width, height = image.size
    config = f"--psm {layout_mode}"
    data = pytesseract.image_to_data(image, lang=OCR_LANGUAGE, config=config, output_type=Output.DICT)
    groups: dict[tuple[str, str, str], list[int]] = {}
    text_groups: dict[tuple[str, str, str], list[str]] = {}
    for i, word in enumerate(data["text"]):
        if word.strip():
            key = (str(data["block_num"][i]), str(data["par_num"][i]), str(data["line_num"][i]))
            text_groups.setdefault(key, []).append(word.strip())
            if int(data["conf"][i]) >= 0:
                groups.setdefault(key, []).append(i)
    regions = []
    for indexes in groups.values():
        left = min(int(data["left"][i]) for i in indexes)
        top = min(int(data["top"][i]) for i in indexes)
        right = max(int(data["left"][i]) + int(data["width"][i]) for i in indexes)
        bottom = max(int(data["top"][i]) + int(data["height"][i]) for i in indexes)
        words = [data["text"][i].strip() for i in indexes]
        confidence = sum(max(0, int(data["conf"][i])) for i in indexes) / len(indexes) / 100
        regions.append({"text": " ".join(words), "bbox": [round(left / width, 4), round(top / height, 4), round(right / width, 4), round(bottom / height, 4)], "confidence": round(confidence, 3)})
    # TSV contains the recognized words and their reading order. Reconstructing
    # lines here avoids a second full Tesseract invocation per layout pass.
    text = "\n".join(" ".join(words) for words in text_groups.values()).strip() if include_text else ""
    return text, regions


def _ocr_item_row_crops(image: Any, candidate_regions: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Re-read quantity-led item rows in a full-width crop to recover faint prices."""
    import pytesseract

    width, height = image.size
    candidates: list[tuple[float, float, float, str]] = []
    for region in candidate_regions:
        text = str(region.get("text", "")).strip()
        bbox = region.get("bbox")
        if not re.match(r"^\d{1,3}[.\s|):-]+[A-Za-z][A-Za-z &'-]{1,}", text) or not isinstance(bbox, list) or len(bbox) < 4:
            continue
        top, bottom = max(0.0, float(bbox[1]) - 0.014), min(1.0, float(bbox[3]) + 0.014)
        center = (top + bottom) / 2
        quantity_match = re.match(r"^(\d{1,3})", text)
        source_quantity = quantity_match.group(1) if quantity_match else ""
        if not any(abs(center - existing[0]) < 0.025 for existing in candidates):
            candidates.append((center, top, bottom, source_quantity))

    row_regions: list[dict[str, Any]] = []
    row_texts: list[str] = []
    for _center, top, bottom, source_quantity in candidates:
        y0, y1 = max(0, int(top * height)), min(height, int(bottom * height))
        if y1 - y0 < 8:
            continue
        crop = image.crop((0, y0, width, y1))
        # Sparse-row mode preserves the description and amount when full-page
        # segmentation treats the receipt row as disconnected text fragments.
        try:
            text, regions = _ocr_pass(crop, layout_mode=11)
        except (OSError, RuntimeError, pytesseract.TesseractError):
            continue
        if not text and not regions:
            continue
        items = [str(region.get("text", "")).strip() for region in regions if str(region.get("text", "")).strip()]
        quantities = [re.match(r"^(\d{1,3})\b", item) for item in items]
        quantity = source_quantity or next((match.group(1) for match in quantities if match), "")
        description = next((item for item in items if re.search(r"[A-Za-z]{2}", item) and not re.match(r"^\d", item) and not re.search(r"\d+[.,]\d{1,2}", item)), "")
        amounts = [match.group(0) for item in items for match in re.finditer(r"(?<!\d)\d{1,4}[.,]\d{2}(?!\d)", item)]
        if quantity and description and amounts:
            combined = f"{quantity} {description} {amounts[-1]}"
            row_regions.append({
                "text": combined,
                "bbox": [0.0, round(y0 / height, 4), 1.0, round(y1 / height, 4)],
                "confidence": min((region.get("confidence", 0.0) for region in regions if region.get("confidence") is not None), default=0.0),
            })
            row_texts.append(combined)
    return "\n".join(row_texts), row_regions


def _enhance_for_ocr(image: Any):
    """Upscale and clarify small printed text without binarizing it."""
    from PIL import Image, ImageFilter, ImageOps

    enhanced = ImageOps.grayscale(image)
    longest_edge = max(enhanced.size)
    scale = min(1.7, max(1.0, 1800 / max(1, longest_edge)))
    if scale > 1.05:
        enhanced = enhanced.resize(
            (round(enhanced.width * scale), round(enhanced.height * scale)),
            Image.Resampling.LANCZOS,
        )
    enhanced = ImageOps.autocontrast(enhanced, cutoff=1)
    return enhanced.filter(ImageFilter.UnsharpMask(radius=1.5, percent=140, threshold=3))


def _ocr_marksheet_table_crop(
    image: Any,
    region_sets: list[list[dict[str, Any]]],
) -> tuple[str, list[dict[str, Any]]]:
    """Locate a coded subject table and re-read the table area row by row."""
    width, height = image.size
    centers: list[float] = []
    for regions in region_sets:
        for region in regions:
            text = str(region.get("text", "")).strip()
            bbox = region.get("bbox")
            if not isinstance(bbox, list) or len(bbox) < 4 or float(bbox[0]) > 0.36:
                continue
            if not 0.24 <= float(bbox[1]) <= 0.86 or not re.match(r"^\D{0,4}\d{3}\b", text):
                continue
            center = (float(bbox[1]) + float(bbox[3])) / 2
            if not any(abs(center - existing) < 0.012 for existing in centers):
                centers.append(center)
    if len(centers) >= 3:
        top = max(0.0, min(centers) - 0.07)
        bottom = min(1.0, max(centers) + 0.06)
    else:
        # Sparse or column-split OCR may not expose three code boxes even when
        # the page is clearly a marksheet. Re-read the central score-table
        # band and let row parsing discard non-row text.
        top, bottom = 0.35, 0.73
    if bottom - top > 0.52:
        top, bottom = 0.35, 0.73
    y0, y1 = round(top * height), round(bottom * height)
    if y1 - y0 < 30:
        return "", []
    crop = image.crop((0, y0, width, y1))
    text, regions = _ocr_pass(crop, layout_mode=6)
    for region in regions:
        box = region.get("bbox")
        if isinstance(box, list) and len(box) == 4:
            region["bbox"] = [box[0], round(top + box[1] * (bottom - top), 4), box[2], round(top + box[3] * (bottom - top), 4)]
    return text, regions


def _ocr_marksheet_identity_crop(image: Any) -> tuple[str, list[dict[str, Any]]]:
    """Enlarge the compact identity block to separate adjacent name fields."""
    import pytesseract
    from .config import OCR_LANGUAGE

    width, height = image.size
    left, top, right, bottom = 0.20, 0.245, 0.75, 0.33
    x0, y0, x1, y1 = round(left * width), round(top * height), round(right * width), round(bottom * height)
    from PIL import Image, ImageFilter, ImageOps
    crop = ImageOps.autocontrast(ImageOps.grayscale(image.crop((x0, y0, x1, y1))), cutoff=1)
    scale = 6
    crop = crop.resize((round(crop.width * scale), round(crop.height * scale)), Image.Resampling.LANCZOS)
    crop = crop.filter(ImageFilter.UnsharpMask(radius=1.2, percent=140, threshold=2))
    text = pytesseract.image_to_string(crop, lang=OCR_LANGUAGE, config="--psm 6").strip()
    _region_text, regions = _ocr_pass(crop, layout_mode=6)
    for region in regions:
        box = region.get("bbox")
        if isinstance(box, list) and len(box) == 4:
            region["bbox"] = [
                round(left + box[0] * (right - left), 4),
                round(top + box[1] * (bottom - top), 4),
                round(left + box[2] * (right - left), 4),
                round(top + box[3] * (bottom - top), 4),
            ]
    return text, regions


def _ocr_marksheet_grade_crop(image: Any) -> tuple[str, list[dict[str, Any]]]:
    """Read the narrow positional-grade column without neighboring marks."""
    from PIL import Image, ImageFilter, ImageOps

    width, height = image.size
    left, top, right, bottom = 0.735, 0.42, 0.83, 0.70
    x0, y0, x1, y1 = round(left * width), round(top * height), round(right * width), round(bottom * height)
    crop = ImageOps.autocontrast(ImageOps.grayscale(image.crop((x0, y0, x1, y1))), cutoff=1)
    crop = crop.resize((crop.width * 5, crop.height * 5), Image.Resampling.LANCZOS)
    crop = crop.filter(ImageFilter.UnsharpMask(radius=1.3, percent=150, threshold=3))
    text, regions = _ocr_pass(crop, layout_mode=11)
    for region in regions:
        box = region.get("bbox")
        if isinstance(box, list) and len(box) == 4:
            region["bbox"] = [
                round(left + box[0] * (right - left), 4),
                round(top + box[1] * (bottom - top), 4),
                round(left + box[2] * (right - left), 4),
                round(top + box[3] * (bottom - top), 4),
            ]
    return text, regions


def _ocr(image_bytes: bytes) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    import pytesseract

    if TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    image = _open_image(image_bytes)
    text, regions = _ocr_pass(image, layout_mode=3)
    alternate_regions: list[dict[str, Any]] = []
    table_terms = (
        "subject", "theory", "marks", "amount", "quantity", "description", "grade", "total",
        "subtotal", "tax", "receipt", "check #", "cashier", "table -", "bill no", "qty", "item",
    )
    if OCR_CROSSCHECK and sum(term in text.casefold() for term in table_terms) >= 2:
        alternate_text_blocks = []
        for layout_mode in (4, 6, 11):
            try:
                alternate_text, regions_for_layout = _ocr_pass(image, layout_mode=layout_mode)
            except (OSError, RuntimeError, pytesseract.TesseractError):
                continue
            if alternate_text:
                alternate_text_blocks.append(f"[Alternative OCR layout PSM {layout_mode}; verify against page image]\n{alternate_text}")
            if alternate_text or regions_for_layout:
                alternate_regions.append({
                    "method": f"tesseract_psm{layout_mode}",
                    "regions": regions_for_layout,
                    "text": alternate_text,
                })
        # Small scans often split a marksheet's subject names, scores, and
        # grades across OCR columns. A contrast-preserving enlarged PSM 6 pass
        # produces a second row-oriented reading; keep it as evidence and let
        # the structurer resolve disagreements instead of replacing the source.
        try:
            enhanced_image = _enhance_for_ocr(image)
            enhanced_text, enhanced_regions = _ocr_pass(enhanced_image, layout_mode=6)
            if enhanced_text or enhanced_regions:
                if enhanced_text:
                    alternate_text_blocks.append(
                        "[Enhanced OCR layout PSM 6; verify against page image]\n" + enhanced_text
                    )
                alternate_regions.append({
                    "method": "tesseract_enhanced_psm6",
                    "regions": enhanced_regions,
                    "text": enhanced_text,
                })
        except (OSError, RuntimeError, pytesseract.TesseractError):
            pass
        marksheet_markers = (
            "marks statement", "subject", "roll no", "marks obtained", "secondary school examination",
        )
        marksheet_text = " ".join([text, *(str(item.get("text", "")) for item in alternate_regions)])
        marksheet_text_folded = marksheet_text.casefold()
        is_marksheet = (
            sum(marker in marksheet_text_folded for marker in marksheet_markers) >= 2
            or "marks statement" in marksheet_text_folded
            or ("subject" in marksheet_text_folded and "secondary school examination" in marksheet_text_folded)
            or ("subject" in marksheet_text_folded and len(re.findall(r"(?<!\d)\d{3}(?!\d)", marksheet_text)) >= 3)
        )
        if is_marksheet:
            identity_text, identity_regions = _ocr_marksheet_identity_crop(image)
            if identity_text or identity_regions:
                if identity_text:
                    alternate_text_blocks.append(
                        "[Targeted marksheet identity OCR; verify against page image]\n" + identity_text
                    )
                alternate_regions.append({
                    "method": "tesseract_marksheet_identity_crop_psm6",
                    "regions": identity_regions,
                    "text": identity_text,
                })
            grade_text, grade_regions = _ocr_marksheet_grade_crop(image)
            if grade_text or grade_regions:
                if grade_text:
                    alternate_text_blocks.append(
                        "[Targeted marksheet grade OCR; verify against page image]\n" + grade_text
                    )
                alternate_regions.append({
                    "method": "tesseract_marksheet_grade_crop_psm11",
                    "regions": grade_regions,
                    "text": grade_text,
                })
            table_text, table_regions = _ocr_marksheet_table_crop(
                image,
                [regions, *(item.get("regions", []) for item in alternate_regions)],
            )
            if table_text or table_regions:
                if table_text:
                    alternate_text_blocks.append(
                        "[Targeted marksheet table OCR PSM 6; verify against page image]\n" + table_text
                    )
                alternate_regions.append({
                    "method": "tesseract_marksheet_table_crop_psm6",
                    "regions": table_regions,
                    "text": table_text,
                })
        receipt_markers = ("invoice", "receipt", "check #", "cashier", "pax", "food tax", "local tax", "subtotal", "total due", "grand total")
        is_receipt = any(marker in text.casefold() for marker in receipt_markers)
        item_rows_text, item_rows_regions = _ocr_item_row_crops(
            image,
            [region for layout in alternate_regions for region in layout.get("regions", [])],
        ) if is_receipt else ("", [])
        if item_rows_text:
            text += "\n\n[Targeted item row OCR; review against page image]\n" + item_rows_text
            alternate_regions.append({"method": "tesseract_item_row_crop", "regions": item_rows_regions, "text": item_rows_text})
        if alternate_text_blocks:
            text = text + "\n\n" + "\n\n".join(alternate_text_blocks)
    return text, regions, alternate_regions


def _pdf_regions(page: Any) -> list[dict[str, Any]]:
    lines: dict[tuple[int, int], list[tuple[float, str]]] = {}
    for word in page.get_text("words"):
        x0, y0, x1, y1, text, block, line, _word = word
        lines.setdefault((block, line), []).append((x0, f"{x0},{y0},{x1},{y1}\t{text}"))
    regions = []
    width, height = page.rect.width, page.rect.height
    for words in lines.values():
        parsed = [item[1].split("\t", 1) for item in sorted(words)]
        boxes = [[float(v) for v in item[0].split(",")] for item in parsed]
        regions.append({"text": " ".join(item[1] for item in parsed), "bbox": [round(min(b[0] for b in boxes) / width, 4), round(min(b[1] for b in boxes) / height, 4), round(max(b[2] for b in boxes) / width, 4), round(max(b[3] for b in boxes) / height, 4)], "confidence": 1.0})
    return regions


def _pdf_text_regions_in_reading_order(page: Any) -> tuple[str, list[dict[str, Any]]]:
    """Return text and normalized bounding boxes ordered top-to-bottom, left-to-right."""
    width, height = page.rect.width, page.rect.height
    words = page.get_text("words", sort=True)
    grouped: dict[tuple[int, int], list[tuple[float, float, float, float, str]]] = {}
    for word in words:
        x0, y0, x1, y1, text, block, line, _word_number = word
        grouped.setdefault((block, line), []).append((x0, y0, x1, y1, text))
    ordered = sorted(grouped.values(), key=lambda line: (round(min(word[1] for word in line), 1), min(word[0] for word in line)))
    regions = []
    text_lines = []
    for line in ordered:
        line.sort(key=lambda item: item[0])
        text = " ".join(item[4] for item in line)
        text_lines.append(text)
        regions.append({
            "text": text,
            "bbox": [
                round(min(item[0] for item in line) / width, 5),
                round(min(item[1] for item in line) / height, 5),
                round(max(item[2] for item in line) / width, 5),
                round(max(item[3] for item in line) / height, 5),
            ],
            "confidence": 1.0,
        })
    return "\n".join(text_lines), regions


def _table(rows: list[list[Any]], table_id: str, source: str) -> dict[str, Any]:
    clean = [["" if value is None else str(value).strip() for value in row] for row in rows]
    return {"id": table_id, "source": source, "headers": clean[0] if clean else [], "rows": clean[1:] if clean else [], "matrix": clean}


def _convert_legacy_office(filename: str, data: bytes) -> bytes:
    executable = shutil.which("soffice") or shutil.which("libreoffice")
    if not executable:
        raise ValueError("Legacy Office files (.doc/.xls/.ppt) require LibreOffice installed and available on PATH; save as DOCX/XLSX/PPTX or PDF instead.")
    suffix = Path(filename).suffix.lower()
    with tempfile.TemporaryDirectory(prefix="docintel-office-") as temporary:
        root = Path(temporary)
        input_dir, output_dir, profile_dir = root / "input", root / "output", root / "profile"
        input_dir.mkdir(); output_dir.mkdir(); profile_dir.mkdir()
        source = input_dir / ("source" + suffix)
        source.write_bytes(data)
        command = [executable, "--headless", "--norestore", f"-env:UserInstallation={profile_dir.as_uri()}", "--convert-to", "pdf", "--outdir", str(output_dir), str(source)]
        try:
            subprocess.run(command, cwd=root, capture_output=True, timeout=90, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("Legacy Office conversion exceeded the 90-second limit.") from exc
        converted = output_dir / "source.pdf"
        if not converted.is_file():
            raise ValueError("LibreOffice could not convert this legacy Office document.")
        return converted.read_bytes()


def _segment(index: int, text: str, method: str, **extra: Any) -> dict[str, Any]:
    return {"index": index, "text": text, "method": method, **extra}


def extract_file(filename: str, data: bytes, ocr_enabled: bool = True) -> dict[str, Any]:
    """Extract text and preserve page/sheet/slide, table, and region coordinates."""
    suffix = Path(filename).suffix.lower()
    if suffix in {".doc", ".xls", ".ppt"}:
        result = extract_file(filename + ".pdf", _convert_legacy_office(filename, data), ocr_enabled)
        result["filename"] = filename
        result["file_type"] = suffix
        result["notes"].append("Legacy Office file converted locally through LibreOffice before extraction.")
        return result
    segments: list[dict[str, Any]] = []
    notes: list[str] = []
    visual_images: list[bytes] = []

    if suffix == ".pdf":
        import fitz

        doc = fitz.open(stream=data, filetype="pdf")
        if doc.is_encrypted:
            raise ValueError("Password-protected PDFs are not supported.")
        if len(doc) > 200:
            raise ValueError("PDF exceeds the 200-page processing limit.")
        for index, page in enumerate(doc):
            text, regions = _pdf_text_regions_in_reading_order(page)
            tables = []
            try:
                finder = page.find_tables()
                for table_index, found in enumerate(finder.tables, 1):
                    parsed = _table(found.extract() or [], f"page_{index + 1}_table_{table_index}", "pdf")
                    x0, y0, x1, y1 = found.bbox
                    parsed["bbox"] = [round(x0 / page.rect.width, 4), round(y0 / page.rect.height, 4), round(x1 / page.rect.width, 4), round(y1 / page.rect.height, 4)]
                    tables.append(parsed)
            except (AttributeError, RuntimeError, ValueError):
                pass
            method = "embedded_text"
            alternate_regions = []
            if not text and ocr_enabled:
                try:
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                    text, regions, alternate_regions = _ocr(pix.tobytes("png"))
                    method = "ocr"
                except (ImportError, OSError, RuntimeError) as exc:
                    notes.append(f"OCR unavailable on PDF page {index + 1}: {exc}")
            if tables:
                table_text = "\n".join("[" + table["id"] + "]\n" + "\n".join(" | ".join(row) for row in table["matrix"]) for table in tables)
                text = (text + "\n" + table_text).strip()
            segments.append(_segment(index + 1, text, method, regions=regions, tables=tables, ocr_alternates=alternate_regions))
            if index < MAX_VISION_PAGES:
                pix = page.get_pixmap(matrix=fitz.Matrix(1.25, 1.25), alpha=False)
                visual_images.append(pix.tobytes("png"))
        if len(doc) > MAX_VISION_PAGES:
            notes.append(f"Visual model input is limited to the first {MAX_VISION_PAGES} PDF pages; text extraction covers all pages.")

    elif suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}:
        source_image = _open_image(data)
        image_out = io.BytesIO()
        source_image.save(image_out, format="PNG")
        normalized_image = image_out.getvalue()
        text, regions, alternate_regions = "", [], []
        if ocr_enabled:
            try:
                text, regions, alternate_regions = _ocr(normalized_image)
            except (ImportError, OSError, RuntimeError) as exc:
                notes.append(f"Image OCR unavailable: {exc}")
        segments.append(_segment(1, text, "ocr" if text else "image", regions=regions, ocr_alternates=alternate_regions, tables=[]))
        visual_images.append(normalized_image)

    elif suffix == ".docx":
        from docx import Document

        doc = Document(io.BytesIO(data))
        chunks = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        tables = []
        for i, table in enumerate(doc.tables, 1):
            parsed = _table([[cell.text for cell in row.cells] for row in table.rows], f"table_{i}", "document")
            tables.append(parsed)
            chunks.append(f"[{parsed['id']}]\n" + "\n".join(" | ".join(row) for row in parsed["matrix"]))
        segments.append(_segment(1, "\n".join(chunks), "document_parser", regions=[], tables=tables))
        for relation in doc.part.rels.values():
            if "image" in relation.reltype and len(visual_images) < MAX_VISION_PAGES:
                visual_images.append(relation.target_part.blob)

    elif suffix in {".xlsx", ".xlsm"}:
        from openpyxl import load_workbook

        book = load_workbook(io.BytesIO(data), data_only=True, read_only=False)
        formula_book = load_workbook(io.BytesIO(data), data_only=False, read_only=False)
        for sheet_index, sheet in enumerate(book.worksheets, 1):
            clipped = sheet.max_row > MAX_SHEET_ROWS or sheet.max_column > MAX_SHEET_COLUMNS
            formula_sheet = formula_book[sheet.title]
            matrix = []
            for row in sheet.iter_rows(max_row=min(sheet.max_row, MAX_SHEET_ROWS), max_col=min(sheet.max_column, MAX_SHEET_COLUMNS)):
                values = []
                for cell in row:
                    value = cell.value
                    if value is None:
                        formula = formula_sheet.cell(cell.row, cell.column).value
                        value = formula if isinstance(formula, str) and formula.startswith("=") else None
                    values.append(value)
                matrix.append(values)
            if clipped:
                notes.append(f"Sheet {sheet.title} exceeded parser limits; extracted at most {MAX_SHEET_ROWS} rows and {MAX_SHEET_COLUMNS} columns.")
            while matrix and not any(value is not None for value in matrix[-1]):
                matrix.pop()
            matrix = [row for row in matrix if any(value is not None for value in row)]
            table = _table(matrix, f"sheet_{sheet_index}_data", sheet.title)
            nonempty = [["" if value is None else str(value) for value in row] for row in matrix]
            merged = [str(rng) for rng in sheet.merged_cells.ranges]
            text = "\n".join(" | ".join(row) for row in nonempty)
            formulas = [f"{cell.coordinate}={cell.value}" for row in formula_sheet.iter_rows(max_row=min(sheet.max_row, MAX_SHEET_ROWS), max_col=min(sheet.max_column, MAX_SHEET_COLUMNS)) for cell in row if isinstance(cell.value, str) and cell.value.startswith("=")]
            if formulas:
                text += "\n[Formulas]\n" + "\n".join(formulas[:1000])
            chart_summaries = []
            for chart_index, chart in enumerate(getattr(sheet, "_charts", []), 1):
                chart_summaries.append({"chart": chart_index, "type": str(chart.type), "series": [{"values": getattr(getattr(series, "val", None), "numRef", None).f if getattr(getattr(series, "val", None), "numRef", None) else None, "categories": getattr(getattr(series, "cat", None), "strRef", None).f if getattr(getattr(series, "cat", None), "strRef", None) else None} for series in chart.series]})
            if chart_summaries:
                text += "\n[Chart series references]\n" + json.dumps(chart_summaries, ensure_ascii=False)
            segments.append(_segment(sheet_index, text, "spreadsheet_parser", label=sheet.title, tables=[table] if matrix else [], merged_ranges=merged))
            for image in getattr(sheet, "_images", []):
                if len(visual_images) >= MAX_VISION_PAGES:
                    break
                try:
                    visual_images.append(image._data())
                except (AttributeError, OSError, ValueError):
                    pass
        book.close()
        formula_book.close()

    elif suffix == ".pptx":
        from pptx import Presentation

        deck = Presentation(io.BytesIO(data))
        for slide_index, slide in enumerate(deck.slides, 1):
            chunks, tables = [], []
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False) and shape.text.strip():
                    chunks.append(shape.text.strip())
                if getattr(shape, "has_table", False):
                    parsed = _table([[cell.text for cell in row.cells] for row in shape.table.rows], f"slide_{slide_index}_table_{len(tables)+1}", "presentation")
                    tables.append(parsed)
                    chunks.append("\n".join(" | ".join(row) for row in parsed["matrix"]))
                if getattr(shape, "has_chart", False):
                    try:
                        chart = shape.chart
                        title = chart.chart_title.text_frame.text if chart.has_title else ""
                        series_data = [{"name": series.name, "values": list(series.values)} for series in chart.series]
                        chunks.append(f"[Chart {title}] " + json.dumps(series_data, ensure_ascii=False))
                    except (AttributeError, TypeError, ValueError):
                        pass
                if getattr(shape, "shape_type", None) == 13 and len(visual_images) < MAX_VISION_PAGES:
                    try:
                        visual_images.append(shape.image.blob)
                    except (AttributeError, ValueError):
                        pass
            segments.append(_segment(slide_index, "\n".join(chunks), "presentation_parser", tables=tables, regions=[]))

    elif suffix == ".csv":
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace"))))
        table = _table(rows, "csv_table", filename)
        segments.append(_segment(1, "\n".join(" | ".join(row) for row in table["matrix"]), "csv_parser", tables=[table], regions=[]))

    elif suffix == ".rtf":
        from striprtf.striprtf import rtf_to_text

        segments.append(_segment(1, rtf_to_text(data.decode("utf-8", errors="replace")), "rtf_parser", tables=[], regions=[]))

    elif suffix in {".txt", ".md", ".json", ".log", ".xml", ".html", ".htm"}:
        decoded = data.decode("utf-8-sig", errors="replace")
        if suffix in {".html", ".htm", ".xml"}:
            decoded = BeautifulSoup(decoded, "html.parser").get_text("\n", strip=True)
        elif suffix == ".json":
            try:
                decoded = json.dumps(json.loads(decoded), ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                notes.append("Input has a .json extension but is not valid JSON.")
        segments.append(_segment(1, decoded, "text_parser", tables=[], regions=[]))

    elif suffix in {".odt", ".ods", ".odp"}:
        from odf import teletype
        from odf.opendocument import load
        from odf.table import Table, TableCell, TableRow
        from odf.text import P

        document = load(io.BytesIO(data))
        if suffix == ".odt":
            chunks = [teletype.extractText(paragraph).strip() for paragraph in document.getElementsByType(P)]
            tables = []
            for i, table_node in enumerate(document.getElementsByType(Table), 1):
                rows = []
                for row_node in table_node.getElementsByType(TableRow):
                    rows.append([teletype.extractText(cell).strip() for cell in row_node.getElementsByType(TableCell)])
                table = _table(rows, f"table_{i}", "document")
                tables.append(table)
                chunks.append("\n".join(" | ".join(row) for row in table["matrix"]))
            segments.append(_segment(1, "\n".join(chunk for chunk in chunks if chunk), "odf_document_parser", tables=tables, regions=[]))
        elif suffix == ".ods":
            for index, table_node in enumerate(document.getElementsByType(Table), 1):
                rows = []
                for row_node in table_node.getElementsByType(TableRow):
                    rows.append([teletype.extractText(cell).strip() for cell in row_node.getElementsByType(TableCell)])
                table = _table(rows, f"sheet_{index}_data", table_node.getAttribute("name") or f"Sheet {index}")
                text = "\n".join(" | ".join(row) for row in table["matrix"])
                segments.append(_segment(index, text, "odf_spreadsheet_parser", label=table["source"], tables=[table], regions=[]))
        else:
            from odf.draw import Page

            for index, slide in enumerate(document.getElementsByType(Page), 1):
                text = "\n".join(teletype.extractText(paragraph).strip() for paragraph in slide.getElementsByType(P) if teletype.extractText(paragraph).strip())
                segments.append(_segment(index, text, "odf_presentation_parser", tables=[], regions=[]))

    else:
        raise ValueError(f"Unsupported file type: {suffix or '(no extension)'}")

    joined_text = "\n\n".join(f"[{item.get('label', 'Page ' + str(item['index']))}]\n{item['text']}" for item in segments)
    return {"filename": filename, "file_type": suffix, "segments": segments, "text": joined_text, "notes": notes, "visual_images": visual_images}
