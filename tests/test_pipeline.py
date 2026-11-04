import io
import asyncio
import json
import unittest
from unittest.mock import patch

from app.baselines import _adjustment_rows, _line_items, _tax_rows, extract_rules
from app.extractors import extract_file
from app.models import _normalize_extraction, _response_json_schema
from app.processor import _ocr_conflicts
from app.validation import validate_extraction


class ExtractorTests(unittest.TestCase):
    def test_receipt_line_items_support_both_quantity_orders(self):
        headers, rows, evidence = _line_items([
            "2 Coffee 3.50 7.00",
            "Cheese Burger 4 x 5.99 23.96",
            "Mineral Water 2 1.25 2.50",
        ])
        self.assertEqual(headers, ["description", "quantity", "unit_price", "amount"])
        self.assertEqual(rows, [
            ["Coffee", "2", "3.50", "7.00"],
            ["Cheese Burger", "4", "5.99", "23.96"],
            ["Mineral Water", "2", "1.25", "2.50"],
        ])
        self.assertEqual(evidence, [
            "2 Coffee 3.50 7.00",
            "Cheese Burger 4 x 5.99 23.96",
            "Mineral Water 2 1.25 2.50",
        ])

    def test_wrapped_line_item_description_is_joined_with_evidence(self):
        _, rows, evidence = _line_items([
            "2 Organic eggs 0.50 1.00",
            "free range, locally sourced",
            "Subtotal 1.00",
            "3 Coffee 2.00 6.00",
        ])
        self.assertEqual(rows, [
            ["Organic eggs free range, locally sourced", "2", "0.50", "1.00"],
            ["Coffee", "3", "2.00", "6.00"],
        ])
        self.assertEqual(evidence, [
            "2 Organic eggs 0.50 1.00\nfree range, locally sourced",
            "3 Coffee 2.00 6.00",
        ])

    def test_receipt_rules_emit_separate_adjustment_table(self):
        extraction = extract_rules(
            "Receipt # R-100\nSubtotal 20.00\nCoffee 2 5.00 10.00\n"
            "Discount: 1.00\nService Charge 2.00\nFood Tax 1.60\nTotal 22.60"
        )
        tables = {table["name"]: table for table in extraction["tables"]}
        self.assertIn("line_items", tables)
        self.assertIn("taxes", tables)
        self.assertIn("adjustments", tables)
        self.assertEqual(tables["adjustments"]["headers"], ["adjustment_type", "description", "quantity", "unit_price", "amount"])
        self.assertEqual(tables["adjustments"]["rows"], [
            ["discount", "Discount", "", "", "1.00"],
            ["fee", "Service Charge", "", "", "2.00"],
        ])
        self.assertEqual(tables["adjustments"]["row_evidence"], ["Discount: 1.00", "Service Charge 2.00"])

    def test_adjustments_keep_discounts_fees_and_returns_separate(self):
        headers, rows, evidence = _adjustment_rows([
            "Discount: 5.00",
            "Service Charge 2.50",
            "Refund -3.00",
            "Return (4.25)",
            "Food Tax 0.80",
            "Coffee 2 3.00 6.00",
            "Total 7.05",
        ])
        self.assertEqual(headers, ["adjustment_type", "description", "quantity", "unit_price", "amount"])
        self.assertEqual(rows, [
            ["discount", "Discount", "", "", "5.00"],
            ["fee", "Service Charge", "", "", "2.50"],
            ["return", "Refund", "", "", "-3.00"],
            ["return", "Return", "", "", "-4.25"],
        ])
        self.assertEqual(evidence, [
            "Discount: 5.00",
            "Service Charge 2.50",
            "Refund -3.00",
            "Return (4.25)",
        ])

    def test_itemized_returns_require_explicit_label_and_negative_amount(self):
        headers, rows, evidence = _adjustment_rows([
            "1 Returned Coffee 3.00 -3.00",
            "Returned Tea 2 x 2.50 (5.00)",
            "1 Coffee 3.00 -3.00",
            "1 Returned Juice 4.00 4.00",
        ])
        self.assertEqual(headers, ["adjustment_type", "description", "quantity", "unit_price", "amount"])
        self.assertEqual(rows, [
            ["return", "Coffee", "1", "3.00", "-3.00"],
            ["return", "Tea", "2", "2.50", "-5.00"],
        ])
        self.assertEqual(evidence, [
            "1 Returned Coffee 3.00 -3.00",
            "Returned Tea 2 x 2.50 (5.00)",
        ])
        _, purchased_rows, _ = _line_items([
            "1 Coffee 3.00 -3.00",
            "1 Returned Juice 4.00 4.00",
        ])
        self.assertEqual(purchased_rows, [])

    def test_line_item_exclusions_use_word_boundaries(self):
        _, rows, evidence = _line_items([
            "2 Vegetable soup 4.00 8.00",
            "Subtotal 8.00",
            "Tax 0.80",
            "Grand Total 8.80",
        ])
        self.assertEqual(rows, [["Vegetable soup", "2", "4.00", "8.00"]])
        self.assertEqual(evidence, ["2 Vegetable soup 4.00 8.00"])

    def test_duplicate_line_items_keep_one_row_and_matching_evidence(self):
        _, rows, evidence = _line_items([
            "2 Coffee 3.5 7",
            "2 Coffee 3.50 7.00",
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], ["Coffee", "2", "3.50", "7.00"])
        self.assertEqual(evidence, ["2 Coffee 3.50 7.00"])

    def test_text_and_html(self):
        plain = extract_file("note.txt", b"Invoice number: ABC-123\nTotal: $42.50", ocr_enabled=False)
        self.assertIn("ABC-123", plain["text"])
        html = extract_file("page.html", b"<html><body><h1>Invoice</h1><p>Total 42</p></body></html>", ocr_enabled=False)
        self.assertIn("Invoice", html["text"])
        self.assertNotIn("<h1>", html["text"])

    def test_json_and_csv_tables(self):
        obj = extract_file("data.json", b'{"a": 1}', ocr_enabled=False)
        self.assertIn('"a": 1', obj["text"])
        csv = extract_file("data.csv", b"name,total\nA,12.50\n", ocr_enabled=False)
        self.assertEqual(csv["segments"][0]["tables"][0]["rows"], [["A", "12.50"]])

    def test_generated_pdf(self):
        import fitz
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Invoice number: PDF-123")
        raw = doc.tobytes()
        doc.close()
        result = extract_file("sample.pdf", raw, ocr_enabled=False)
        self.assertIn("PDF-123", result["text"])
        self.assertTrue(result["segments"][0]["regions"])

    def test_generated_office_files(self):
        from docx import Document
        from openpyxl import Workbook
        from pptx import Presentation
        from pptx.util import Inches

        doc = Document()
        doc.add_paragraph("DOCX body")
        table = doc.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "Key"
        table.cell(0, 1).text = "Value"
        table.cell(1, 0).text = "A"
        table.cell(1, 1).text = "B"
        stream = io.BytesIO(); doc.save(stream)
        parsed_docx = extract_file("sample.docx", stream.getvalue(), ocr_enabled=False)
        self.assertIn("DOCX body", parsed_docx["text"])
        self.assertEqual(parsed_docx["segments"][0]["tables"][0]["rows"], [["A", "B"]])

        workbook = Workbook(); sheet = workbook.active; sheet.append(["Item", "Price"]); sheet.append(["Pen", 2.5]); sheet["C2"] = "=B2*2"
        stream = io.BytesIO(); workbook.save(stream)
        parsed_xlsx = extract_file("sample.xlsx", stream.getvalue(), ocr_enabled=False)
        self.assertIn("Pen", parsed_xlsx["text"])
        self.assertIn("=B2*2", parsed_xlsx["text"])

        deck = Presentation(); slide = deck.slides.add_slide(deck.slide_layouts[6]); box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1)); box.text = "PPTX body"
        stream = io.BytesIO(); deck.save(stream)
        parsed_pptx = extract_file("sample.pptx", stream.getvalue(), ocr_enabled=False)
        self.assertIn("PPTX body", parsed_pptx["text"])


class ValidationTests(unittest.TestCase):
    def test_quote_grounding_and_invoice_arithmetic(self):
        segments = [{"index": 1, "text": "Subtotal: 40.00 Tax: 4.00 Total: 44.00", "regions": []}]
        extraction = {"fields": {
            "subtotal": {"value": "40.00", "evidence": "Subtotal: 40.00"},
            "tax": {"value": "4.00", "evidence": "Tax: 4.00"},
            "total": {"value": "44.00", "evidence": "Total: 44.00"},
        }}
        result = validate_extraction(extraction, segments)
        self.assertFalse(result["review_required"])
        self.assertTrue(all(item["grounded"] for item in result["grounded_fields"].values()))
        self.assertTrue(result["checks"][0]["passed"])

    def test_invoice_money_uses_decimal_and_detects_line_amount_mismatch(self):
        segments = [{"index": 1, "text": "Subtotal 0.30 Tax 0.03 Total 0.33", "regions": []}]
        extraction = {"fields": {
            "subtotal": {"value": "0.30", "evidence": "Subtotal 0.30"},
            "tax": {"value": "0.03", "evidence": "Tax 0.03"},
            "total": {"value": "0.33", "evidence": "Total 0.33"},
            "line_items": {"value": [{"quantity": "3", "unit_price": "0.10", "amount": "0.27"}]},
        }}
        result = validate_extraction(extraction, segments)
        checks = {item["name"]: item for item in result["checks"]}
        self.assertTrue(checks["subtotal_plus_tax_equals_total"]["passed"])
        self.assertFalse(checks["line_item_quantity_times_unit_price_equals_amount"]["passed"])
        self.assertTrue(result["review_required"])

    def test_invoice_line_items_sum_uses_printed_amounts(self):
        segments = [{"index": 1, "text": "Subtotal 0.30 Tax 0.03 Total 0.33", "regions": []}]
        extraction = {"fields": {
            "subtotal": {"value": "0.30", "evidence": "Subtotal 0.30"},
            "tax": {"value": "0.03", "evidence": "Tax 0.03"},
            "total": {"value": "0.33", "evidence": "Total 0.33"},
            "line_items": {"value": [{"quantity": "3", "unit_price": "0.10", "amount": "0.30"}]},
        }}
        result = validate_extraction(extraction, segments)
        checks = {item["name"]: item for item in result["checks"]}
        self.assertTrue(checks["subtotal_plus_tax_equals_total"]["passed"])
        self.assertTrue(checks["line_item_quantity_times_unit_price_equals_amount"]["passed"])
        self.assertTrue(checks["line_items_sum_equals_subtotal"]["passed"])

    def test_tax_rows_recognize_cgst_sgst_and_rates(self):
        headers, rows, evidence = _tax_rows([
            "CGST 9% 9.00",
            "SGST 9% 9.00",
            "IGST @ 18% 18.00",
            "Subtotal 100.00",
        ])
        self.assertEqual(headers, ["tax_name", "rate", "amount"])
        self.assertEqual(rows, [
            ["CGST", "9%", "9.00"],
            ["SGST", "9%", "9.00"],
            ["IGST", "18%", "18.00"],
        ])
        self.assertEqual(evidence, ["CGST 9% 9.00", "SGST 9% 9.00", "IGST @ 18% 18.00"])

    def test_invoice_arithmetic_sums_multiple_printed_tax_rows(self):
        segments = [{"index": 1, "text": "Subtotal 100.00 CGST 9% 9.00 SGST 9% 9.00 Total 118.00", "regions": [
            {"text": "CGST 9% 9.00", "bbox": [0, 0, 1, 1]},
            {"text": "SGST 9% 9.00", "bbox": [0, 0.1, 1, 1.1]},
        ]}]
        extraction = {"fields": {
            "subtotal": {"value": "100.00", "evidence": "Subtotal 100.00"},
            "total": {"value": "118.00", "evidence": "Total 118.00"},
        }, "tables": [{
            "name": "taxes", "headers": ["tax_name", "rate", "amount"],
            "rows": [["CGST", "9%", "9.00"], ["SGST", "9%", "9.00"]],
            "row_evidence": ["CGST 9% 9.00", "SGST 9% 9.00"],
        }]}
        result = validate_extraction(extraction, segments)
        checks = {item["name"]: item for item in result["checks"]}
        self.assertTrue(checks["subtotal_plus_tax_equals_total"]["passed"])
        self.assertFalse(result["review_required"])

    def test_tax_inclusive_invoice_is_not_double_counted(self):
        segments = [{"index": 1, "text": "Tax inclusive prices. Subtotal 118.00 GST 18% 18.00 Total 118.00", "regions": []}]
        extraction = {"fields": {
            "subtotal": {"value": "118.00", "evidence": "Subtotal 118.00"},
            "total": {"value": "118.00", "evidence": "Total 118.00"},
        }, "tables": [{
            "name": "taxes", "headers": ["tax_name", "rate", "amount"],
            "rows": [["GST", "18%", "18.00"]], "row_evidence": ["GST 18% 18.00"],
        }]}
        result = validate_extraction(extraction, segments)
        checks = {item["name"]: item for item in result["checks"]}
        self.assertIsNone(checks["tax_inclusive_total_requires_manual_review"]["passed"])
        self.assertTrue(result["review_required"])
        self.assertNotIn("subtotal_plus_tax_equals_total", checks)

    def test_invoice_total_includes_discount_adjustment_once(self):
        segments = [{"index": 1, "text": "Subtotal 100.00 Tax 10.00 Discount 5.00 Total 105.00", "regions": [{"text": "discount Discount 5.00", "bbox": [0, 0, 1, 1]}]}]
        extraction = {"fields": {
            "subtotal": {"value": "100.00", "evidence": "Subtotal 100.00"},
            "tax": {"value": "10.00", "evidence": "Tax 10.00"},
            "discount": {"value": "5.00", "evidence": "Discount 5.00"},
            "total": {"value": "105.00", "evidence": "Total 105.00"},
        }, "tables": [{
            "name": "adjustments",
            "headers": ["adjustment_type", "description", "quantity", "unit_price", "amount"],
            "rows": [["discount", "Discount", "", "", "5.00"]],
            "row_evidence": ["Discount 5.00"],
        }]}
        result = validate_extraction(extraction, segments)
        checks = {item["name"]: item for item in result["checks"]}
        self.assertTrue(checks["subtotal_tax_and_adjustments_equal_total"]["passed"])
        self.assertFalse(result["review_required"])

    def test_invoice_total_adds_fee_adjustment(self):
        segments = [{"index": 1, "text": "Subtotal 100.00 Tax 10.00 Delivery Fee 3.00 Total 113.00", "regions": [{"text": "fee Delivery Fee 3.00", "bbox": [0, 0, 1, 1]}]}]
        extraction = {"fields": {
            "subtotal": {"value": "100.00", "evidence": "Subtotal 100.00"},
            "tax": {"value": "10.00", "evidence": "Tax 10.00"},
            "total": {"value": "113.00", "evidence": "Total 113.00"},
        }, "tables": [{
            "name": "adjustments", "headers": ["adjustment_type", "description", "amount"],
            "rows": [["fee", "Delivery Fee", "3.00"]], "row_evidence": ["Delivery Fee 3.00"],
        }]}
        result = validate_extraction(extraction, segments)
        checks = {item["name"]: item for item in result["checks"]}
        self.assertTrue(checks["subtotal_tax_and_adjustments_equal_total"]["passed"])
        self.assertFalse(result["review_required"])

    def test_invoice_total_applies_signed_itemized_return(self):
        segments = [{"index": 1, "text": "Subtotal 100.00 Tax 10.00 Returned Coffee -5.00 Total 105.00", "regions": [{"text": "return Coffee 1 5.00 -5.00", "bbox": [0, 0, 1, 1]}]}]
        extraction = {"fields": {
            "subtotal": {"value": "100.00", "evidence": "Subtotal 100.00"},
            "tax": {"value": "10.00", "evidence": "Tax 10.00"},
            "total": {"value": "105.00", "evidence": "Total 105.00"},
        }, "tables": [{
            "name": "adjustments", "headers": ["adjustment_type", "description", "quantity", "unit_price", "amount"],
            "rows": [["return", "Coffee", "1", "5.00", "-5.00"]], "row_evidence": ["Returned Coffee -5.00"],
        }]}
        result = validate_extraction(extraction, segments)
        checks = {item["name"]: item for item in result["checks"]}
        self.assertTrue(checks["subtotal_tax_and_adjustments_equal_total"]["passed"])
        self.assertFalse(result["review_required"])

    def test_unreadable_or_unknown_adjustment_routes_to_review_without_pass(self):
        for row in (["fee", "Delivery", ""], ["mystery", "Adjustment", "2.00"]):
            with self.subTest(row=row):
                segments = [{"index": 1, "text": "Subtotal 100.00 Tax 10.00 Total 112.00", "regions": []}]
                extraction = {"fields": {
                    "subtotal": {"value": "100.00", "evidence": "Subtotal 100.00"},
                    "tax": {"value": "10.00", "evidence": "Tax 10.00"},
                    "total": {"value": "112.00", "evidence": "Total 112.00"},
                }, "tables": [{
                    "name": "adjustments", "headers": ["adjustment_type", "description", "amount"],
                    "rows": [row], "row_evidence": ["Adjustment row"],
                }]}
                result = validate_extraction(extraction, segments)
                checks = {item["name"]: item for item in result["checks"]}
                self.assertIsNone(checks["subtotal_tax_and_adjustments_equal_total"]["passed"])
                self.assertTrue(result["review_required"])

    def test_unmatched_field_routes_to_review(self):
        result = validate_extraction({"fields": {"total": {"value": "99", "evidence": "Total: 99"}}}, [{"index": 1, "text": "Total: 10", "regions": []}])
        self.assertTrue(result["review_required"])
        self.assertFalse(result["grounded_fields"]["total"]["grounded"])

    def test_ocr_variants_keep_conflicts_for_review(self):
        segments = [{
            "index": 2,
            "text": "Roll Number: 123456",
            "ocr_alternates": [{"method": "psm6", "text": "Roll Number: 123458"}],
        }]
        conflicts = _ocr_conflicts(segments)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["page"], 2)
        self.assertEqual({item["value"] for item in conflicts[0]["readings"]}, {"123456", "123458"})


class ModelNormalizationTests(unittest.TestCase):
    def test_generated_response_schema_respects_custom_schema(self):
        requested = _response_json_schema({"fields": {
            "roll_number": {"type": "string"},
            "subjects": {"type": "table", "columns": ["subject", "marks"]},
        }})
        fields = requested["properties"]["fields"]
        self.assertEqual(set(fields["properties"]), {"roll_number"})
        self.assertFalse(fields["additionalProperties"])
        self.assertEqual(requested["required"], ["document_type", "summary", "fields", "tables", "checks", "uncertainties"])
        self.assertEqual(requested["properties"]["tables"]["items"]["properties"]["name"]["enum"], ["subjects"])

    def test_schema_alias_null_status_and_table_merge_preserve_rows(self):
        schema = {"fields": {
            "student_name": {"type": "string"},
            "subjects_and_marks": {"type": "table", "columns": ["subject", "marks"]},
        }}
        extraction = _normalize_extraction({
            "fields": {"student_name": {"value": None, "evidence": None}},
            "tables": [
                {"name": "marks", "headers": ["subject", "marks"], "rows": [["Math", "80"]], "row_evidence": ["Math 80"], "page": 1},
                {"name": "marks", "headers": ["subject", "marks"], "rows": [["Math", "80"], ["English", "75"]], "row_evidence": ["Math 80", "English 75"], "page": 1},
            ],
        }, schema)
        self.assertEqual(extraction["fields"]["student_name"]["value"], "")
        self.assertEqual(extraction["fields"]["student_name"]["extraction_status"], "missing")
        table = extraction["tables"][0]
        self.assertEqual(table["name"], "subjects_and_marks")
        self.assertEqual(table["rows"], [["Math", "80"], ["English", "75"]])
        self.assertEqual(table["row_evidence"], ["Math 80", "English 75"])

    def test_conflicting_table_rows_are_retained_and_marked(self):
        extraction = _normalize_extraction({"tables": [
            {"name": "items", "headers": ["description", "amount"], "rows": [["Tea", "10"]], "row_evidence": ["Tea 10"], "page": 1},
            {"name": "items", "headers": ["description", "amount"], "rows": [["Tea", "12"]], "row_evidence": ["Tea 12"], "page": 1},
        ]}, None)
        table = extraction["tables"][0]
        self.assertEqual(table["rows"], [["Tea", "10"], ["Tea", "12"]])
        self.assertEqual(table["row_conflicts"][0]["row_indexes"], [0, 1])


class ModelRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_json_gets_one_repair_attempt(self):
        from app.models import extract_with_ollama

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        class Client:
            def __init__(self, *args, **kwargs):
                self.calls = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return Response({"response": "{bad", "done": True})
                return Response({"response": json.dumps({"document_type": "note", "fields": {}, "tables": []}), "done": True})

            async def get(self, *args, **kwargs):
                return Response({"models": []})

        with patch("app.models.httpx.AsyncClient", Client):
            extraction, metrics = await asyncio.wait_for(
                extract_with_ollama("hello", "note.txt", mode="text"), timeout=5
            )
        self.assertEqual(extraction["document_type"], "note")
        self.assertEqual(metrics["response_attempts"], 2)


if __name__ == "__main__":
    unittest.main()
