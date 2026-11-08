"""Transparent, dependency-free rules baseline for invoices and receipts."""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from itertools import combinations
from typing import Any


PATTERNS = {
    "invoice_number": r"(?:invoice\s*(?:no\.?|number|#)?|bill\s*(?:no\.?|number|#)|receipt\s*(?:no\.?|number|#)|check\s*#)\s*[:#-]?\s*([A-Z0-9][A-Z0-9/_.-]{1,})",
    "date": r"(?:invoice\s*date|date)\s*[:#-]?\s*([0-3]?\d[./-][01]?\d[./-](?:\d{2,4})|\d{4}-[01]?\d-[0-3]?\d)",
    "subtotal": r"(?:net\s*)?sub\s*total\s*[:₹$€£\s]*([\d,]+(?:\.\d{1,2})?)",
    "discount": r"(?:discount|dis\.)\s*(?:@\s*[\d.]+%\s*)?[:₹$€£\s]*([\d,]+(?:\.\d{1,2})?)",
    "total": r"(?:grand\s*total|total\s*to\s*pay|total\s*due|total)\s*[:₹$€£\s]*([\d,]+(?:\.\d{1,2})?)",
}
MONEY = re.compile(r"(?:[$₹€£]\s*)?\d[\d,]*(?:\.\d{1,2})?")


def _currency_facts(text: str) -> tuple[str | None, str | None, str | None]:
    """Return printed symbol, an unambiguous printed ISO code, and its evidence."""
    codes = "AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN BWP BYN BZD CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KRW KWD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SLL SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD UYU UZS VES VND VUV WST XAF XCD XOF XPF YER ZAR ZMW ZWG"
    code_map = {code: code for code in codes.split()}
    symbols = [
        ("US$", "USD"), ("C$", "CAD"), ("A$", "AUD"), ("NZ$", "NZD"), ("S$", "SGD"), ("HK$", "HKD"),
        ("R$", "BRL"), ("RM", "MYR"), ("₹", "INR"), ("€", "EUR"), ("₽", "RUB"), ("₺", "TRY"),
        ("₩", "KRW"), ("₫", "VND"), ("฿", "THB"), ("₴", "UAH"), ("₪", "ILS"), ("₦", "NGN"),
        ("₱", ""), ("₡", ""), ("₲", ""), ("₵", "GHS"), ("₭", "LAK"), ("₮", "MNT"),
        ("₸", "KZT"), ("₼", "AZN"), ("₾", "GEL"), ("֏", "AMD"), ("៛", "KHR"), ("৳", "BDT"),
        ("؋", "AFN"), ("£", ""), ("¥", ""), ("$", ""), ("₨", ""),
    ]
    # Explicit ISO codes are stronger evidence than a symbol. Require the code
    # near a numeric amount or on a clearly currency-labeled line.
    for line in text.splitlines():
        for match in re.finditer(r"\b([A-Z]{3})\b", line):
            code = match.group(1)
            nearby_amount = bool(re.search(r"\d", line[max(0, match.start() - 12):match.end() + 12]))
            labeled_currency = bool(re.search(r"\bcurrenc(?:y|ies)\b|\b(?:iso|denomination)\b", line, re.IGNORECASE))
            if code in code_map and (nearby_amount or labeled_currency):
                return code, code_map[code] or None, line.strip()
    found: dict[str, str] = {}
    for symbol, iso in symbols:
        for line in text.splitlines():
            if re.search(r"\b(?:promo(?:tional)?|take\s+home|authentic\s+recipe|pkgs?\.|for\s+only)\b", line, re.IGNORECASE):
                continue
            if symbol in line and (re.search(r"\d", line) or re.search(r"\bcurrenc(?:y|ies)\b", line, re.IGNORECASE)):
                found.setdefault(symbol, line.strip())
    found = {symbol: evidence for symbol, evidence in found.items() if not any(symbol != other and symbol in other for other in found)}
    if len(found) == 1:
        symbol, evidence = next(iter(found.items()))
        iso = next(code for candidate, code in symbols if candidate == symbol)
        return symbol, iso or None, evidence
    if found:
        symbol = next(iter(found)) if len(found) == 1 else " / ".join(found)
        evidence = next(iter(found.values()))
        return symbol, None, evidence
    return None, None, None


def _evidenced(value: str, line: str) -> dict[str, Any]:
    return {"value": value.strip(), "evidence": line.strip(), "page": 0, "bbox": []}


def _line_items(lines: list[str]) -> tuple[list[str], list[list[str]], list[str]]:
    """Extract conservative invoice/receipt rows and attach clear wrapped descriptions."""
    headers = ["description", "quantity", "unit_price", "amount"]
    rows: list[list[str]] = []
    evidence: list[str] = []
    excluded = ("subtotal", "tax", "total", "discount", "cashier", "table", "check", "date", "change", "balance", "only", "visit", "ref num")

    def is_excluded(value: str) -> bool:
        return any(
            re.search(rf"(?<!\w){re.escape(term)}(?!\w)", value)
            for term in excluded
        )

    def is_plausible_description(value: str) -> bool:
        # OCR fragments may contain quantities and prices but no recoverable
        # product name. Reject short-token noise and obvious header contamination.
        words = re.findall(r"[A-Za-z]+", value)
        if not words or max(map(len, words)) < 3:
            return False
        if re.search(r"[=~<>|]", value):
            return False
        if re.search(r"\bitem\s+name\b", value, re.IGNORECASE):
            return False
        return True

    def is_wrapped_description(value: str) -> bool:
        # Only attach a plausible lowercase continuation. Avoid OCR noise and
        # recognizable promotional/footer copy being absorbed into item names.
        folded = value.casefold()
        promotional_terms = ("authentic recipe", "visit the", "food market", "take home", "for only", "today!")
        return (
            value[:1].islower()
            and bool(re.search(r"[A-Za-z]{3}", value))
            and not re.search(r"\d|[$₹€£:#]", value)
            and not re.search(r"[=~<>|]", value)
            and not any(term in folded for term in promotional_terms)
            and not is_excluded(folded)
            and not re.search(r"\b(?:qty|quantity|unit price|amount|subtotal|grand total)\b", value, re.IGNORECASE)
        )

    money = r"(?:[$₹€£]\s*)?\d[\d,]*(?:\.\d{1,2})?"
    quantity_first = re.compile(
        rf"^(?P<qty>\d{{1,3}})[.\s:-]+(?P<description>.+?)\s+(?P<unit>{money})(?:\s+(?P<amount>{money}))?$"
    )
    description_first = re.compile(
        rf"^(?P<description>.+?)\s+(?P<qty>\d{{1,3}})\s*(?:x|@)?\s+(?P<unit>{money})(?:\s+(?P<amount>{money}))?$",
        re.IGNORECASE,
    )

    for line in lines:
        raw = line.strip()
        normalized = raw.casefold()
        if not raw:
            continue
        if is_excluded(normalized) or re.match(
            r"^(?:\d{1,3}[.\s:-]+)?(?:returned|return|refunded|refund|credited|credit)\b",
            normalized,
        ):
            # Keep explicitly labelled return rows out of purchased items,
            # even when their amount is positive/ambiguous and cannot be
            # safely classified as a return adjustment.
            continue
        match = quantity_first.match(raw)
        if match:
            description = match.group("description")
            quantity = match.group("qty")
            unit_price = match.group("unit")
            amount = match.group("amount") or unit_price
            if not match.group("amount"):
                unit_price = ""
        else:
            match = description_first.match(raw)
            if not match:
                if rows and is_wrapped_description(raw):
                    rows[-1][0] = re.sub(r"\s+", " ", f"{rows[-1][0]} {raw}").strip()
                    evidence[-1] = f"{evidence[-1]}\n{raw}"
                continue
            description = match.group("description")
            quantity = match.group("qty")
            unit_price = match.group("unit")
            amount = match.group("amount") or unit_price
            if not match.group("amount"):
                unit_price = ""

        item_text = re.sub(r"\s+", " ", description).strip(" .|:-_*")
        if not item_text or not is_plausible_description(item_text):
            continue
        clean = lambda value: re.sub(r"\s+", "", value).strip("$₹€£")
        row = [item_text, quantity, clean(unit_price), clean(amount)]
        normalized_description = re.sub(r"[^a-z0-9]+", " ", item_text.casefold()).strip()
        key = (normalized_description, quantity.casefold())
        prior = next(
            (i for i, existing in enumerate(rows)
             if (re.sub(r"[^a-z0-9]+", " ", existing[0].casefold()).strip(), existing[1].casefold()) == key),
            None,
        )
        if prior is None:
            rows.append(row)
            evidence.append(raw)
        else:
            current_quality = (bool(rows[prior][2]), len(rows[prior][3]), len(rows[prior][0]))
            new_quality = (bool(row[2]), len(row[3]), len(row[0]))
            if new_quality > current_quality:
                rows[prior] = row
                evidence[prior] = raw
    return headers, rows, evidence

def _tax_rows(lines: list[str]) -> tuple[list[str], list[list[str]], list[str]]:
    headers = ["tax_name", "rate", "amount"]
    rows: list[list[str]] = []
    evidence: list[str] = []
    for line in lines:
        folded_line = line.casefold()
        # Recognize common named tax components as well as generic tax labels.
        if not re.search(r"\b(?:tax|cgst|sgst|igst|utgst|vat|gst)\b", folded_line):
            continue
        amounts = list(MONEY.finditer(line))
        if not amounts:
            continue
        amount = amounts[-1].group(0).replace(" ", "").strip("$₹€£")
        name = line[:amounts[-1].start()].strip(" :.-")
        rate_match = re.search(r"(?:@\s*)?(\d+(?:\.\d+)?)\s*%", name)
        rate = rate_match.group(1) + "%" if rate_match else ""
        name = re.sub(r"(?:@\s*)?\d+(?:\.\d+)?\s*%", "", name).strip(" :.-")
        rows.append([name, rate, amount])
        evidence.append(line.strip())
    deduped = []
    deduped_evidence = []
    seen = set()
    for row, quote in zip(rows, evidence):
        numeric_amount = re.sub(r"[^0-9.]", "", row[2])
        amount_key = f"{float(numeric_amount):.2f}" if numeric_amount else ""
        key = (re.sub(r"[^a-z0-9]+", " ", row[0].casefold()).strip(), row[1].casefold(), amount_key)
        if key not in seen:
            seen.add(key)
            deduped.append(row)
            deduped_evidence.append(quote)
    return headers, deduped, deduped_evidence


def _adjustment_rows(lines: list[str]) -> tuple[list[str], list[list[str]], list[str]]:
    """Classify labelled summary adjustments and explicitly itemized returns."""
    headers = ["adjustment_type", "description", "quantity", "unit_price", "amount"]
    rows: list[list[str]] = []
    evidence: list[str] = []
    money = r"(?:[$₹€£]\s*)?\d[\d,]*(?:\.\d{1,2})?"
    negative_amount = r"(?:-\s*(?:" + money + r")|\(\s*" + money + r"\s*\))"
    return_label = r"(?:returned|return|refunded|refund|credited|credit)\b"

    def clean_amount(value: str) -> str:
        amount = re.sub(r"\s+", "", value).replace(",", "").strip("$₹€£")
        if amount.startswith("(") and amount.endswith(")"):
            return "-" + amount[1:-1].lstrip("-")
        return amount

    # Itemized return rows require both an explicit return/refund label and
    # a negative or parenthesized printed amount. A negative amount alone is
    # not enough to infer that a purchased item was returned.
    itemized_patterns = (
        re.compile(
            rf"^(?P<qty>\d{{1,3}})[.\s:-]+(?P<description>{return_label}.+?)\s+"
            rf"(?P<unit>{money})\s+(?P<amount>{negative_amount})$",
            re.IGNORECASE,
        ),
        re.compile(
            rf"^(?P<description>{return_label}.+?)\s+(?P<qty>\d{{1,3}})\s*(?:x|@)?\s+"
            rf"(?P<unit>{money})\s+(?P<amount>{negative_amount})$",
            re.IGNORECASE,
        ),
    )
    labels = (
        (r"discount|coupon|promotion|promo|rebate", "discount"),
        (r"refund|return|credit", "return"),
        (r"surcharge|fee|service\s+charge|delivery\s+fee|shipping(?:\s+fee)?|tip|gratuity", "fee"),
        (r"deposit|round(?:ing)?\s+off", "other"),
    )
    amount_pattern = re.compile(
        r"(?P<amount>\(?\s*-?\s*(?:[$₹€£]\s*)?\d[\d,]*(?:\.\d{1,2})?\s*\)?)\s*$"
    )
    for line in lines:
        raw = line.strip()
        if not raw:
            continue

        itemized = next((pattern.match(raw) for pattern in itemized_patterns if pattern.match(raw)), None)
        if itemized:
            description = re.sub(
                rf"^{return_label}\s*", "", itemized.group("description"), flags=re.IGNORECASE
            ).strip(" :.-")
            if not description:
                continue
            rows.append([
                "return", description, itemized.group("qty"),
                clean_amount(itemized.group("unit")), clean_amount(itemized.group("amount")),
            ])
            evidence.append(raw)
            continue

        label_match = None
        adjustment_type = ""
        for label_pattern, kind in labels:
            candidate = re.match(rf"^\s*(?P<label>{label_pattern})\b", raw, re.IGNORECASE)
            if candidate:
                label_match = candidate
                adjustment_type = kind
                break
        if not label_match:
            continue
        amount_match = amount_pattern.search(raw)
        if not amount_match or re.search(r"%\s*$", raw):
            continue
        amount = clean_amount(amount_match.group("amount"))
        description = raw[:amount_match.start()].strip(" :.-")
        if not description:
            description = label_match.group("label")
        rows.append([adjustment_type, description, "", "", amount])
        evidence.append(raw)
    return headers, rows, evidence


def _marksheet_rows(lines: list[str], columns: list[str]) -> tuple[list[list[str]], list[str]]:
    """Recover subject rows from repeated OCR layouts using the printed 3-digit subject code."""
    candidates: dict[str, list[tuple[tuple[int, int, int, int, int], list[str], str]]] = {}
    number_words = re.compile(r"\b(?:ZERO|ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE|TEN|ELEVEN|TWELVE|THIRTEEN|FOURTEEN|FIFTEEN|SIXTEEN|SEVENTEEN|EIGHTEEN|NINETEEN|TWENTY|THIRTY|FORTY|FIFTY|SIXTY|SEVENTY|EIGHTY|NINETY|HUNDRED)\b.*$", re.IGNORECASE)
    number_parts = ("THOUSAND", "HUNDRED", "NINETY", "EIGHTY", "SEVENTY", "SIXTY", "FIFTY", "FORTY", "THIRTY", "TWENTY", "NINETEEN", "EIGHTEEN", "SEVENTEEN", "SIXTEEN", "FIFTEEN", "FOURTEEN", "THIRTEEN", "TWELVE", "ELEVEN", "TEN", "NINE", "EIGHT", "SEVEN", "SIX", "FIVE", "FOUR", "THREE", "TWO", "ONE", "ZERO", "AND")

    def clean_number_words(value: str) -> str:
        tokens = re.findall(r"[A-Za-z]+", value.upper())
        output: list[str] = []
        for token in tokens:
            if token in number_parts:
                output.append(token)
                continue
            # Some OCR layouts join words like SIXTY NINE into SIXTYNINE.
            position, pieces = 0, []
            while position < len(token):
                part = next((word for word in number_parts if token.startswith(word, position)), None)
                if not part:
                    pieces = []
                    break
                pieces.append(part)
                position += len(part)
            if not pieces:
                # Keep the numeric words already recovered when OCR appends
                # a noisy grade token or column mark after the total.
                break
            output.extend(pieces)
        return " ".join(output)

    def column_key(label: str) -> str:
        value = re.sub(r"[^a-z0-9]+", " ", label.casefold()).strip()
        if "code" in value: return "subject_code"
        if "subject" in value: return "subject"
        if "theory" in value: return "theory_marks"
        if any(token in value.split() for token in ("practical", "internal", "ia", "pr")): return "practical_marks"
        if "total" in value and any(token in value.split() for token in ("word", "words")): return "total_in_words"
        if "total" in value: return "total_marks"
        if "grade" in value: return "grade"
        return value.replace(" ", "_")

    excluded_subject_terms = {
        "board", "central", "secondary", "examination", "school", "statement",
        "marks", "obtained", "result", "father", "mother", "birth", "dated",
        "date", "roll", "number", "kohli", "subject", "code", "grade",
        "total", "positional",
    }
    ocr_digit_map = {"O": "0", "S": "5", "I": "1", "L": "1", "Z": "2", "B": "8", "G": "6"}

    def mark_number(match: re.Match[str]) -> str | None:
        token = match.group(0).upper()
        if not any(char.isdigit() for char in token):
            return None
        normalized = "".join(
            str(unicodedata.digit(char)) if char.isdecimal() else ocr_digit_map.get(char, char)
            for char in token
        )
        return normalized if normalized.isdigit() else None

    for raw in lines:
        line = raw.strip()
        # OCR sometimes attaches a grade's leading "1" to the subject code,
        # as in "0871 SOCIAL SCIENCE". Accept that only before a word.
        code_match = re.match(
            r"^[^A-Za-z0-9]{0,5}(?:[A-Za-z]\s+)?(\d[A-Za-z0-9]\d)(?:(?=\b)|(?=1\s+[A-Za-z]))[\s|:.-]*(.*)$",
            line,
        )
        if not code_match:
            continue
        code, tail = code_match.groups()
        code_digit_map = {"O": "0", "I": "1", "L": "1", "S": "5", "A": "8", "B": "8"}
        code = "".join(
            str(unicodedata.digit(char)) if char.isdecimal() else code_digit_map.get(char.upper(), char)
            for char in code
        )
        grade = ""
        grade_match = re.search(r"\b([A-Ea-e])([12lI])\b", tail)
        if grade_match:
            grade = grade_match.group(1).upper() + ("1" if grade_match.group(2).casefold() in {"l", "i"} else grade_match.group(2))
            tail = tail[:grade_match.start()] + " " + tail[grade_match.end():]
        else:
            # Grade columns are small; in the final token, T/E commonly stand
            # in for a printed 1 and Z for 2. Restrict this correction to a
            # trailing grade-like token to avoid changing subject text.
            trailing_grade = re.search(r"\b([A-E])\s*([12lI|!TEZ])\s*$", tail, re.IGNORECASE)
            if trailing_grade:
                suffix = trailing_grade.group(2).upper()
                grade = trailing_grade.group(1).upper() + ("2" if suffix == "Z" else "1" if suffix in {"L", "I", "|", "!", "T", "E"} else suffix)
                tail = tail[:trailing_grade.start()] + " "
            else:
                numeric_grade = re.search(r"\b6\s*([12])\s*$", tail)
                if numeric_grade:
                    grade = "C" + numeric_grade.group(1)
                    tail = tail[:numeric_grade.start()] + " "
        mark_tokens = list(re.finditer(r"(?<!\w)(?:[\dOoSsIlZzBbGg]{2,3}|X{1,3}|[-–—])(?!\w)", tail, re.IGNORECASE))
        numeric = [item for item in mark_tokens if mark_number(item) is not None]
        triplets = list(combinations(numeric, 3))
        consistent_triplets = [
            group for group in triplets
            if all(mark_number(item) is not None for item in group)
            and int(mark_number(group[0])) + int(mark_number(group[1])) == int(mark_number(group[2]))
        ]
        placeholder = next((item for item in mark_tokens if re.fullmatch(r"X{1,3}|[-–—]", item.group(0), re.IGNORECASE)), None)
        if placeholder:
            before = [item for item in numeric if item.end() <= placeholder.start()]
            after = [item for item in numeric if item.start() >= placeholder.end()]
            placeholder_pair = (before[-1], after[0]) if before and after else None
        else:
            placeholder_pair = None
        if placeholder_pair is None and len(numeric) == 3:
            readings = [mark_number(item) for item in numeric]
            # Cross-checking can read a printed XXX as 444. Treat that token
            # as a no-practical marker only when theory and total repeat and
            # the middle reading is outside the usual subject-mark range.
            if readings[0] == readings[2] and 300 <= int(readings[1]) <= 999:
                placeholder_pair = (numeric[0], numeric[2])
        marks = consistent_triplets[-1] if consistent_triplets else numeric[-3:]
        total_words = ""
        if placeholder_pair:
            first, last = placeholder_pair
            subject_text = tail[:first.start()]
            marks_values = [mark_number(first) or "", "XXX", mark_number(last) or ""]
            total_words = clean_number_words(tail[last.end():])
        elif marks:
            subject_text = tail[:marks[0].start()]
            marks_values = [mark_number(match) or "" for match in marks]
            marks_values = [value.zfill(3) if value.isdigit() and len(value) < 3 else value for value in marks_values]
            if len(marks_values) == 2:
                # Preserve the blank practical column and the printed total.
                marks_values = [marks_values[0], "", marks_values[1]]
            total_words = clean_number_words(tail[marks[-1].end():])
        else:
            subject_text, marks_values = tail, []
        marks_values = [
            "".join(str(unicodedata.digit(char)) if char.isdecimal() else char for char in value)
            for value in marks_values
        ]
        subject_text = re.sub(r"\b(?:[oO]?\d{1,3})\b", " ", subject_text)
        subject_text = number_words.sub("", subject_text)
        subject = re.sub(r"[^\w&()/' -]", " ", subject_text, flags=re.UNICODE)
        subject = re.sub(r"\s+", " ", subject).strip(" )(|-:.;")
        # Repair high-confidence, common OCR substitutions in subject words;
        # keep the original OCR line as evidence for review.
        subject = re.sub(r"\bENGLESH\b", "ENGLISH", subject, flags=re.IGNORECASE)
        subject = re.sub(r"\bHINDI\s*['’]\s*COURSE\b", "HINDI COURSE", subject, flags=re.IGNORECASE)
        words = {word.casefold() for word in re.findall(r"[^\W\d_]+", subject, flags=re.UNICODE) if len(word) >= 2}
        if not words or words.intersection(excluded_subject_terms) or words.issubset({"x", "xx", "xxx", "ose"}):
            continue
        requested_column_keys = {column_key(column) for column in columns}
        if {"theory_marks", "total_marks"}.issubset(requested_column_keys) and len(numeric) < 2:
            # A row with fewer than two printed mark values is too ambiguous
            # to map into theory/practical/total columns.
            continue
        values = {"subject_code": code, "subject": subject, "theory_marks": "", "practical_marks": "", "total_marks": "", "total_in_words": total_words, "grade": grade}
        for key, value in zip(("theory_marks", "practical_marks", "total_marks"), marks_values):
            values[key] = value
        row = [values.get(column_key(column), "") for column in columns]
        arithmetic_match = len(marks_values) >= 3 and sum(int(value) for value in marks_values[:2] if value.isdigit()) == int(marks_values[2]) if all(value.isdigit() for value in marks_values[:3]) else False
        score = (len(marks_values), int(arithmetic_match), bool(total_words), bool(grade), len(subject))
        candidates.setdefault(code, []).append((score, row, line))

    best_candidates = []
    for code_candidates in candidates.values():
        # Prefer readings with the most complete marks/grade; stable input order
        # breaks ties while validation still checks disagreements across layouts.
        best = max(enumerate(code_candidates), key=lambda pair: (*pair[1][0], -pair[0]))[1]
        best_candidates.append(best)

    def subject_tokens(row: list[str]) -> set[str]:
        subject_column = next((i for i, column in enumerate(columns) if column_key(column) == "subject"), None)
        if subject_column is None or subject_column >= len(row):
            return set()
        return {
            token.casefold() for token in re.findall(r"[^\W\d_]+", row[subject_column], flags=re.UNICODE)
            if len(token) >= 4
        }

    def same_subject(left: list[str], right: list[str]) -> bool:
        left_tokens, right_tokens = subject_tokens(left), subject_tokens(right)
        if not left_tokens or not right_tokens:
            return False
        # Keep separately printed theory/practical rows distinct, but collapse
        # OCR variants that differ only in spacing or a misread subject code.
        if ("practical" in left_tokens) != ("practical" in right_tokens):
            return False
        left_text, right_text = " ".join(sorted(left_tokens)), " ".join(sorted(right_tokens))
        overlap = len(left_tokens & right_tokens)
        return (
            left_tokens == right_tokens
            or SequenceMatcher(None, left_text, right_text).ratio() >= 0.78
            or (overlap >= 1 and overlap / min(len(left_tokens), len(right_tokens)) >= 0.8)
        )

    selected: list[tuple[tuple[int, int, int, int, int], list[str], str]] = []
    for candidate in best_candidates:
        duplicate = next((i for i, prior in enumerate(selected) if same_subject(candidate[1], prior[1])), None)
        if duplicate is None:
            selected.append(candidate)
            continue
        prior = selected[duplicate]
        candidate_quality = (*candidate[0], sum(bool(value) for value in candidate[1]))
        prior_quality = (*prior[0], sum(bool(value) for value in prior[1]))
        if candidate_quality > prior_quality:
            selected[duplicate] = candidate

    rows = [candidate[1] for candidate in selected]
    evidence = [candidate[2] for candidate in selected]
    return rows, evidence


def extract_rules(text: str, schema: dict | None = None) -> dict:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    # The no-model path detects supported receipt/marksheet layouts first; for
    # other documents it preserves OCR lines and extracts only explicit labels.
    if not schema:
        marksheet_markers = (
            r"\bmarks?\s+statement\b", r"\bsubject\b", r"\broll\s*no\b",
            r"\bmarks\s+obtained\b", r"\bsecondary\s+school\s+examination\b",
        )
        if sum(bool(re.search(marker, text, re.IGNORECASE)) for marker in marksheet_markers) >= 3:
            marksheet_schema = {
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
                    "subjects_and_marks": {"type": "table", "required": True, "columns": ["subject_code", "subject", "theory_marks", "practical_marks", "total_marks", "total_in_words", "grade"]},
                },
            }
            return extract_rules(text, marksheet_schema)

        receipt_markers = (
            r"\breceipt\b", r"\bcheck\s*#", r"\bcashier\b", r"\bsubtotal\b",
            r"\bfood\s+tax\b", r"\blocal\s+tax\b", r"\bpax\s*\(?s?\)?(?=\W|$)",
        )
        receipt_signal_count = sum(
            1 for marker in receipt_markers if re.search(marker, text, re.IGNORECASE)
        )
        if receipt_signal_count >= 2:
            receipt_schema = {
                "document_type": "invoice_receipt",
                "fields": {
                    "merchant_name": {"type": "string", "required": False},
                    "merchant_address": {"type": "string", "required": False},
                    "phone": {"type": "string", "required": False},
                    "receipt_number": {"type": "string", "required": False},
                    "date": {"type": "string", "required": False},
                    "time": {"type": "string", "required": False},
                    "cashier": {"type": "string", "required": False},
                    "table_number": {"type": "string", "required": False},
                    "party_size": {"type": "string", "required": False},
                    "currency": {"type": "string", "required": False},
                    "currency_symbol": {"type": "string", "required": False},
                    "subtotal": {"type": "number", "required": False},
                    "discount": {"type": "number", "required": False},
                    "total": {"type": "number", "required": False},
                    "other_text": {"type": "string", "required": False},
                    "line_items": {"type": "table", "required": False, "columns": ["description", "quantity", "unit_price", "amount"]},
                    "taxes": {"type": "table", "required": False, "columns": ["tax_name", "rate", "amount"]},
                    "adjustments": {"type": "table", "required": False, "columns": ["adjustment_type", "description", "quantity", "unit_price", "amount"]},
                },
            }
            return extract_rules(text, receipt_schema)

        fields: dict[str, Any] = {}
        line_rows: list[list[str]] = []
        row_evidence: list[str] = []
        seen_lines: set[str] = set()
        for line_number, line in enumerate(lines, 1):
            if line.startswith("[Alternative OCR layout PSM") or line.startswith("[Targeted item row OCR"):
                continue
            key = re.match(r"^\s*([^:=|]{2,60}?)\s*[:=]\s*(\S.*|)$", line)
            if key:
                label = re.sub(r"[^\w]+", "_", key.group(1).casefold(), flags=re.UNICODE).strip("_")
                if label:
                    unique_label = label
                    suffix = 2
                    while unique_label in fields:
                        unique_label = f"{label}_{suffix}"
                        suffix += 1
                    fields[unique_label] = _evidenced(key.group(2).strip(), line)
            normalized = re.sub(r"\s+", " ", line).casefold()
            if normalized in seen_lines:
                continue
            seen_lines.add(normalized)
            line_rows.append([str(line_number), line])
            row_evidence.append(line)
        tables = [{
            "name": "document_lines",
            "headers": ["line_number", "text"],
            "rows": line_rows,
            "row_evidence": row_evidence,
            "page": 0,
        }] if line_rows else []
        return {
            "document_type": "document",
            "summary": "OCR text organized into labeled fields and a complete line-by-line table. Define a document-specific schema for semantic field names.",
            "fields": fields,
            "tables": tables,
            "checks": [],
            "uncertainties": [],
        }

    split_labels = {"subtotal", "net subtotal", "total", "grand total", "total to pay", "cashier", "check #", "receipt #", "date", "food tax", "local tax"}
    for index, line in enumerate(lines[:-1]):
        if line.casefold().strip(" :.-") in split_labels:
            following = lines[index + 1]
            if not following.startswith("["):
                lines.append(f"{line} {following}")
    fields: dict[str, Any] = {}
    values: dict[str, tuple[str, str]] = {}
    for name, pattern in PATTERNS.items():
        for line in lines:
            match = re.search(pattern, line, re.IGNORECASE)
            if match:
                values[name] = (match.group(1).strip(), line)
                break

    # Receipts frequently use a short merchant heading and a Check/Ref number
    # instead of a formal invoice-number label.
    vendor_line = next((line for line in lines if re.search(r"[A-Za-z]{3}", line) and not line.startswith("[") and not any(token in line.casefold() for token in ("alternative ocr", "food business center", "invoice", "receipt", "tax invoice"))), "")
    if vendor_line:
        values["vendor"] = (vendor_line, vendor_line)
        values["merchant_name"] = (vendor_line, vendor_line)
    for key, pattern in {
        "invoice_number": r"(?:invoice\s*(?:no\.?|number|#)?|bill\s*(?:no\.?|number|#)|receipt\s*(?:no\.?|number|#)|check\s*#)\s*[:#-]?\s*([A-Z0-9][A-Z0-9/_.-]{1,})",
        "time": r"((?:[01]?\d|2[0-3]):[0-5]\d)\b",
        "cashier": r"\bcashier\s*[:#-]?\s*(.+?)\s*$",
        "table_number": r"\btable\s*[-:#]?\s*(\w+)",
        "party_size": r"\b(?:pax|guests?)\s*\(?s?\)?\s*[:#-]?\s*(\d+)",
        "reference_number": r"\bref(?:erence)?\s*(?:num(?:ber)?|no\.?|#)\s*[:#-]?\s*([A-Z0-9-]+)",
    }.items():
        for line in lines:
            match = re.search(pattern, line, re.IGNORECASE)
            if match:
                values[key] = (match.group(1).strip(), line)
                break

    if str((schema or {}).get("document_type", "")).casefold() == "marksheet":
        def put_first(field: str, pattern: str, cleaner=lambda value: value.strip(" \t:|;,.")) -> None:
            for source_line in lines:
                match_line = "".join(str(unicodedata.digit(char)) if char.isdecimal() else char for char in source_line)
                match_line = match_line.replace("’", "'").replace("‘", "'")
                match = re.search(pattern, match_line, re.IGNORECASE)
                if match:
                    value = cleaner(match.group(1))
                    if field in {"mother_name", "father_name"}:
                        name_tokens = re.findall(r"[A-Za-z]+", value)
                        if len(name_tokens) < 2 or re.search(r"\d", value):
                            continue
                    if value:
                        values[field] = (value, source_line)
                        return

        put_first("roll_number", r"\broll\s*(?:no\.?|number)\s*[:#-]?\s*([A-Z0-9/-]+)")
        if "roll_number" not in values:
            for source_line in lines:
                normalized = "".join(str(unicodedata.digit(char)) if char.isdecimal() else char for char in source_line)
                if re.search(r"अनुक्रमांक|serial\s*(?:no\.?|number)", normalized, re.IGNORECASE):
                    digits = re.findall(r"(?<!\d)\d{6,}(?!\d)", normalized)
                    if digits:
                        values["roll_number"] = (digits[-1], source_line)
                        break
        put_first("registration_number", r"\breg(?:n|istration)?\s*\.?\s*no\.?\s*[:#-]?\s*([A-Z0-9/-]+)")
        put_first("student_name", r"\bthis\s+is\s+to\s+certify(?:\s+that)?\s+([A-Z][A-Z .'-]{3,})", lambda value: re.sub(r"\s+", " ", value).strip(" .'-"))
        put_first("mother_name", r"\bmother(?:\s*'?s?|e|a|o)\s*n?(?:ame|amo)\b\s*[:©|=-]*\s*(.+)", lambda value: re.sub(r"\s+", " ", value.replace(":", " ")).strip(" \t:|;,."))
        put_first("father_name", r"\bfath(?:er|or)\s*'?s?\s*(?:/\s*guardian\s*'?s?\s*)?n(?:ame|amo)\b\s*[:©|=-]*\s*(.+)", lambda value: re.sub(r"[|(<]+.*$", "", re.sub(r"\s+", " ", value.replace(":", " "))).strip(" \t:|;,."))
        values.pop("school_name", None)
        for source_line in lines:
            marker = re.search(r"\bschoo[l!](?=\W|$)|विद्यालय", source_line, re.IGNORECASE)
            if not marker:
                continue
            candidate = re.sub(r"[\u0900-\u097f]", " ", source_line[marker.end():])
            candidate = re.sub(r"\s+", " ", candidate).strip(" :#-|.,;")
            candidate = re.sub(r"^Q(?=\d{3,}\b)", "0", candidate, flags=re.IGNORECASE)
            if not candidate or re.search(r"\b(?:examination|exam|marks|statement)\b", candidate, re.IGNORECASE):
                continue
            if len(re.findall(r"[A-Za-z0-9]{2,}", candidate)) >= 2:
                values["school_name"] = (candidate, source_line)
                break
        if "student_name" not in values:
            for source_line in lines:
                match = re.search(r"^\s*([^|]{3,48})\s*\|\s*(?:अनुक्रमांक|roll\b)", source_line, re.IGNORECASE)
                if match:
                    name = re.sub(r"[\u0900-\u097f\d#]+", " ", match.group(1))
                    name = re.sub(r"[^A-Za-z .'-]", " ", name)
                    name = re.sub(r"\s+", " ", name).strip(" .'-")
                    if len(name.split()) >= 2:
                        values["student_name"] = (name, source_line)
                        break
        name_stop_words = {"name", "father", "fathers", "fathor", "fathors", "mother", "school", "date", "birth", "marks", "subject", "total"}
        for field, label_pattern in (
            ("mother_name", r"(?:mother(?:\s*'?s?|e|a|o)\s*n?(?:ame|amo)|mother'o\s*namo|माता\s*(?:का\s*)?नाम)"),
            ("father_name", r"(?:fath(?:er|or)\s*'?s?\s*n(?:ame|amo)|पिता\s*(?:का\s*)?नाम)"),
        ):
            if field in values:
                continue
            for line_index, source_line in enumerate(lines):
                match = re.search(label_pattern, source_line, re.IGNORECASE)
                if not match:
                    continue
                inline_value = source_line[match.end():].strip(" \t:|;,.=-")
                following_lines = () if re.search(r"[A-Za-z\u0900-\u097f]", inline_value) else tuple(lines[line_index + 1:line_index + 3])
                for candidate in (inline_value, *following_lines):
                    cleaned = re.sub(r"[\u0900-\u097f\d:#|]+", " ", candidate)
                    cleaned = re.sub(r"^(?:moth\w{0,3}\s*n(?:ame|amo)|father\w{0,2}\s*n(?:ame|amo))\b", " ", cleaned, flags=re.IGNORECASE)
                    cleaned = re.sub(r"[^A-Za-z .'-]", " ", cleaned)
                    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .'-")
                    words = {word.casefold() for word in cleaned.split()}
                    if 2 <= len(cleaned.split()) <= 5 and cleaned and not words.intersection(name_stop_words):
                        evidence = source_line if candidate == source_line[match.end():] else f"{source_line} {candidate.strip()}"
                        values[field] = (cleaned, evidence)
                        break
                if field in values:
                    break
        for source_line in lines:
            if re.search(r"\bcentral\s+board\s+of\s+secondary\s+education\b", source_line, re.IGNORECASE):
                values["board"] = ("Central Board of Secondary Education", source_line)
                break
        put_first("examination", r"\b(secondary\s+school\s+examination)\s*,?\s*20\d{2}\b", lambda value: re.sub(r"\s+", " ", value).title())
        put_first("exam_year", r"\bsecondary\s+school\s+examination\s*,?\s*(20\d{2})\b")
        put_first("date_of_birth", r"(?:\bdate\s+of\s+birth\b|\bd\.?o\.?b\.?\b|जन्म\s*तिथि).*?(\d{1,2}[./-]\d{1,2}[./-]\d{4}|(?:\d{1,2}(?:st|nd|rd|th|sth)?|sth)\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+[a-z ]{5,50}|(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+[a-z ]{5,50})", lambda value: re.sub(r"\bSTH\b", "5TH", re.sub(r"\s+", " ", value), flags=re.IGNORECASE).strip())
        put_first("result", r"(?:\bresult\b|परिणाम).*?\b(pass|passed|fail|failed|compartment|withheld)\b", lambda value: "PASS" if value.casefold() in {"pass", "passed"} else value.upper())
        if "result" not in values:
            for source_line in lines:
                match = re.search(r"\b(pass|passed|fail|failed|compartment|withheld)\b", source_line, re.IGNORECASE)
                if match:
                    outcome = "PASS" if match.group(1).casefold() in {"pass", "passed"} else match.group(1).upper()
                    values["result"] = (outcome, source_line)
                    break
        for source_line in lines:
            if re.search(r"\bdated\b|दिनांक|दिनाक|date\s+of\s+issue", source_line, re.IGNORECASE):
                normalized = "".join(str(unicodedata.digit(char)) if char.isdecimal() else char for char in source_line)
                match = re.search(r"(?<!\d)(0?[1-9]|[12]\d|3[01])[./-](0?[1-9]|1[0-2])[./-](20\d{2})(?!\d)", normalized)
                if match:
                    values["date_of_issue"] = (match.group(0), source_line)
                    break

    # Keep separate printed tax lines; do not silently sum them into a made-up
    # single tax value.
    line_headers, line_rows, line_quotes = _line_items(lines)
    tax_headers, tax_rows, tax_quotes = _tax_rows(lines)
    adjustment_headers, adjustment_rows, adjustment_quotes = _adjustment_rows(lines)
    currency_symbol, currency_code, currency_evidence = _currency_facts(text)
    if currency_symbol and currency_evidence:
        values["currency_as_printed"] = (currency_symbol, currency_evidence)
        if not re.fullmatch(r"[A-Z]{3}", currency_symbol):
            values["currency_symbol"] = (currency_symbol, currency_evidence)
    if currency_code and currency_evidence:
        values["currency"] = (currency_code, currency_evidence)
    if "tax" in values and tax_rows:
        values.pop("tax", None)

    schema_fields = schema.get("fields", {}) if isinstance(schema, dict) else {}
    requested_scalars = [name for name, spec in schema_fields.items() if not isinstance(spec, dict) or str(spec.get("type", "")).lower() != "table"] if isinstance(schema_fields, dict) else []
    aliases = {
        "document_number": "invoice_number", "receipt_number": "invoice_number", "check_number": "invoice_number",
        "merchant": "merchant_name", "vendor_name": "vendor", "transaction_date": "date",
        "grand_total": "total", "total_due": "total", "amount_due": "total",
        "discount_amount": "discount", "table": "table_number", "guest_count": "party_size",
    }
    fields_to_return = requested_scalars or list(dict.fromkeys([*values.keys(), "date", "vendor", "subtotal", "discount", "total"]))
    for requested in fields_to_return:
        source_key = aliases.get(requested, requested)
        if source_key in values:
            value, evidence = values[source_key]
            fields[requested] = _evidenced(value, evidence)

    tables = []
    if isinstance(schema_fields, dict):
        table_names = [name for name, spec in schema_fields.items() if isinstance(spec, dict) and str(spec.get("type", "")).lower() == "table"]
    else:
        table_names = []
    table_names = table_names or (["line_items", "taxes", "adjustments"] if line_rows or tax_rows or adjustment_rows else [])
    for name in table_names:
        spec = schema_fields.get(name, {}) if isinstance(schema_fields, dict) else {}
        requested_columns = spec.get("columns", []) if isinstance(spec, dict) else []
        column_tokens = {re.sub(r"[^a-z0-9]+", "_", str(column).casefold()).strip("_") for column in requested_columns}
        if {"subject_code", "subject"}.issubset(column_tokens) or "marksheet" in str((schema or {}).get("document_type", "")).casefold():
            headers = list(requested_columns) or ["subject_code", "subject", "theory_marks", "practical_marks", "total_marks", "grade"]
            rows, quotes = _marksheet_rows(lines, headers)
        elif "tax" in name.casefold():
            headers, rows, quotes = tax_headers, tax_rows, tax_quotes
        elif any(token in name.casefold() for token in ("adjust", "discount", "fee", "return", "refund")):
            headers, rows, quotes = adjustment_headers, adjustment_rows, adjustment_quotes
        elif any(token in name.casefold() for token in ("item", "line", "product", "transaction")) or "invoice" in str((schema or {}).get("document_type", "")).casefold() or "receipt" in str((schema or {}).get("document_type", "")).casefold():
            headers, rows, quotes = line_headers, line_rows, line_quotes
        else:
            continue
        if isinstance(schema_fields, dict) and isinstance(spec, dict):
            if isinstance(requested_columns, list) and requested_columns:
                source_map = {header: index for index, header in enumerate(headers)}
                rows = [[row[source_map[column]] if column in source_map else "" for column in requested_columns] for row in rows]
                headers = list(requested_columns)
        if rows:
            tables.append({"name": name, "headers": headers, "rows": rows, "row_evidence": quotes, "page": 0})

    return {
        "document_type": (schema or {}).get("document_type", "invoice" if values else "unknown"),
        "summary": "Local OCR and receipt/invoice rules baseline; confirm uncertain fields against the document.",
        "fields": fields,
        "tables": tables,
        "checks": [],
        "uncertainties": [],
    }


def merge_rule_supplements(extraction: dict, rule_extraction: dict, schema: dict | None = None) -> dict[str, list[str]]:
    """Fill model omissions with OCR-grounded values and report disagreements for review."""
    extraction.setdefault("fields", {})
    extraction.setdefault("tables", [])
    rule_fields = rule_extraction.get("fields", {})
    supplemented_fields: list[str] = []
    disagreements: list[str] = []

    for name, rule_field in rule_fields.items():
        rule_value = rule_field.get("value") if isinstance(rule_field, dict) else rule_field
        if rule_value in (None, ""):
            continue
        model_field = extraction["fields"].get(name)
        model_value = model_field.get("value") if isinstance(model_field, dict) and "value" in model_field else model_field
        if model_value in (None, ""):
            extraction["fields"][name] = rule_field
            supplemented_fields.append(name)
        elif name in {"merchant_name", "vendor", "receipt_number", "invoice_number", "date", "time", "subtotal", "discount", "total", "table_number", "party_size", "currency", "currency_symbol", "currency_as_printed"}:
            left = re.sub(r"\s+", " ", str(model_value)).strip().casefold()
            right = re.sub(r"\s+", " ", str(rule_value)).strip().casefold()
            if name == "table_number":
                left = re.sub(r"^(?:table\s*[-:#]?\s*)", "", left)
                right = re.sub(r"^(?:table\s*[-:#]?\s*)", "", right)
            if name in {"subtotal", "discount", "total"}:
                try:
                    left = str(Decimal(re.sub(r"[^0-9.+-]", "", left)))
                    right = str(Decimal(re.sub(r"[^0-9.+-]", "", right)))
                except InvalidOperation:
                    pass
            if left != right:
                disagreements.append(f"'{name}' differs between VLM output ({model_value}) and OCR/rules ({rule_value}); confirm against the page.")

    schema_fields = schema.get("fields", {}) if isinstance(schema, dict) else {}
    model_currency = extraction["fields"].get("currency")
    model_currency_value = model_currency.get("value") if isinstance(model_currency, dict) else model_currency
    if model_currency_value not in (None, "") and (rule_fields.get("currency_symbol") or rule_fields.get("currency_as_printed")) and not rule_fields.get("currency"):
        symbol_field = rule_fields.get("currency_symbol") or rule_fields["currency_as_printed"]
        symbol_value = symbol_field.get("value") if isinstance(symbol_field, dict) else symbol_field
        disagreements.append(f"Printed currency symbol '{symbol_value}' is ambiguous; the VLM returned currency code '{model_currency_value}'. Confirm the ISO code from the document.")
    table_names = [name for name, spec in schema_fields.items() if isinstance(spec, dict) and str(spec.get("type", "")).lower() == "table"] if isinstance(schema_fields, dict) else []
    model_tables = extraction["tables"] if isinstance(extraction["tables"], list) else []
    for table in model_tables:
        if not isinstance(table, dict):
            continue
        name = str(table.get("name", "")).casefold().replace(" ", "_")
        if name in {"table", "items", "item_table", "line_items"} and table_names:
            table["name"] = next((item for item in table_names if "item" in item.casefold() or "line" in item.casefold()), table_names[0])
            expected_columns = schema_fields.get(table["name"], {}).get("columns", [])
            if not table.get("headers") and expected_columns:
                table["headers"] = list(expected_columns)

    rule_tables = rule_extraction.get("tables", [])
    supplemented_tables: list[str] = []
    for rule_table in rule_tables:
        if not isinstance(rule_table, dict):
            continue
        name = rule_table.get("name")
        model_table = next((table for table in model_tables if isinstance(table, dict) and table.get("name") == name), None)
        if model_table is None:
            model_tables.append(rule_table)
            supplemented_tables.append(str(name))
            continue
        existing_rows = model_table.get("rows") if isinstance(model_table.get("rows"), list) else []
        rule_rows = rule_table.get("rows") if isinstance(rule_table.get("rows"), list) else []
        if not existing_rows and rule_rows:
            model_table.update(rule_table)
            supplemented_tables.append(str(name))
            continue
        headers = model_table.get("headers") or rule_table.get("headers", [])
        rule_headers = rule_table.get("headers", [])
        normalize_header = lambda value: re.sub(r"[^a-z0-9]+", "_", str(value).casefold()).strip("_")
        if headers and rule_headers and [normalize_header(item) for item in headers] != [normalize_header(item) for item in rule_headers]:
            source_name = str(rule_table.get("source_table") or f"{name}_source")
            separate = dict(rule_table)
            separate["name"] = source_name
            model_tables.append(separate)
            supplemented_tables.append(source_name)
            continue
        if headers and not model_table.get("headers"):
            model_table["headers"] = headers
        evidence = model_table.get("row_evidence") if isinstance(model_table.get("row_evidence"), list) else []
        while len(evidence) < len(existing_rows):
            evidence.append("")
        provenance = model_table.get("row_provenance") if isinstance(model_table.get("row_provenance"), list) else ["vlm"] * len(existing_rows)
        while len(provenance) < len(existing_rows):
            provenance.append("vlm")
        rule_evidence = rule_table.get("row_evidence", [])
        rule_provenance = rule_table.get("row_provenance", [])
        matched_model_indices: set[int] = set()
        for index, row in enumerate(rule_rows):
            def normalize_cell(value: Any) -> str:
                text = re.sub(r"\\s+", " ", str(value)).strip().casefold()
                try:
                    return str(Decimal(re.sub(r"[^0-9.+-]", "", text))) if re.search(r"\\d", text) else text
                except InvalidOperation:
                    return text
            primary_header = next((index for index, header in enumerate(headers) if normalize_header(header) in {"subject_code", "tax_name", "description", "item", "name"}), 0)
            quantity_header = next((index for index, header in enumerate(headers) if normalize_header(header) in {"quantity", "qty"}), None)
            rule_key = normalize_cell(row[primary_header]) if primary_header < len(row) else ""
            if quantity_header is not None and quantity_header < len(row):
                rule_key += "|" + normalize_cell(row[quantity_header])
            keyed_candidates = []
            for prior_index, prior in enumerate(existing_rows):
                if not isinstance(prior, list):
                    continue
                prior_key = normalize_cell(prior[primary_header]) if primary_header < len(prior) else ""
                if quantity_header is not None and quantity_header < len(prior):
                    prior_key += "|" + normalize_cell(prior[quantity_header])
                if rule_key and prior_key == rule_key:
                    keyed_candidates.append(prior_index)
            available_candidates = [candidate for candidate in keyed_candidates if candidate not in matched_model_indices]
            matched_index = None
            ambiguity = False
            if len(available_candidates) == 1:
                matched_index = available_candidates[0]
            elif len(available_candidates) > 1:
                # Printed unit price/amount can disambiguate repeated description
                # and quantity keys. Missing or non-unique signatures stay separate.
                amount_headers = {"unit_price", "price", "amount", "line_total"}
                comparable = [
                    column for column, header in enumerate(headers)
                    if normalize_header(header) in amount_headers
                    and column < len(row) and str(row[column] or "").strip()
                ]
                signature_matches = []
                if comparable:
                    for candidate in available_candidates:
                        prior = existing_rows[candidate]
                        if all(
                            column < len(prior)
                            and str(prior[column] or "").strip()
                            and normalize_cell(prior[column]) == normalize_cell(row[column])
                            for column in comparable
                        ):
                            signature_matches.append(candidate)
                if len(signature_matches) == 1:
                    matched_index = signature_matches[0]
                else:
                    ambiguity = True
            elif keyed_candidates:
                ambiguity = True
            quote = rule_evidence[index] if index < len(rule_evidence) else ""
            if matched_index is None:
                existing_rows.append(row)
                evidence.append(quote)
                provenance.append(rule_provenance[index] if index < len(rule_provenance) else "ocr_rules")
                supplemented_tables.append(str(name))
                if ambiguity:
                    disagreements.append(
                        f"Table '{name}' has an ambiguous repeated row key '{rule_key}'; the OCR/rules row was preserved separately for manual review."
                    )
                continue
            matched_model_indices.add(matched_index)
            prior = existing_rows[matched_index]
            was_supplemented = False
            for cell_index, rule_cell in enumerate(row):
                if cell_index >= len(prior):
                    prior.extend([""] * (cell_index + 1 - len(prior)))
                prior_value = str(prior[cell_index] or "").strip()
                rule_value = str(rule_cell or "").strip()
                if not prior_value and rule_value:
                    prior[cell_index] = rule_value
                    was_supplemented = True
                elif prior_value and rule_value and normalize_cell(prior_value) != normalize_cell(rule_value):
                    column = str(headers[cell_index]) if cell_index < len(headers) else f"column {cell_index + 1}"
                    warning = f"Table '{name}' row keyed by '{rule_key}' has conflicting {column} values ({prior_value} vs OCR/rules {rule_value}); confirm against the page."
                    if warning not in disagreements:
                        disagreements.append(warning)
            if was_supplemented:
                evidence[matched_index] = quote or evidence[matched_index]
                provenance[matched_index] = "mixed"
                supplemented_tables.append(str(name))
        model_table["rows"] = existing_rows
        model_table["row_evidence"] = evidence
        model_table["row_provenance"] = provenance
    extraction["tables"] = model_tables
    return {"fields": sorted(set(supplemented_fields)), "tables": sorted(set(supplemented_tables)), "disagreements": disagreements}


def extract_parser_tables(segments: list[dict[str, Any]], schema: dict | None = None) -> list[dict[str, Any]]:
    """Preserve tables already parsed from office, spreadsheet, PDF, and CSV sources."""
    schema_fields = schema.get("fields", {}) if isinstance(schema, dict) else {}
    requested = [
        (name, spec) for name, spec in schema_fields.items()
        if isinstance(spec, dict) and str(spec.get("type", "")).lower() == "table"
    ] if isinstance(schema_fields, dict) else []
    output: list[dict[str, Any]] = []
    for segment in segments:
        for source in segment.get("tables", []) if isinstance(segment.get("tables"), list) else []:
            if not isinstance(source, dict):
                continue
            matrix = source.get("matrix") if isinstance(source.get("matrix"), list) else []
            headers = source.get("headers") if isinstance(source.get("headers"), list) else []
            rows = source.get("rows") if isinstance(source.get("rows"), list) else []
            if matrix:
                headers = headers or matrix[0]
                rows = rows or matrix[1:]
            if not headers and rows and isinstance(rows[0], dict):
                headers = list(rows[0])
                rows = [[row.get(key, "") for key in headers] for row in rows]
            headers = [str(value or "").strip() for value in headers]
            rows = [
                ["" if value is None else str(value).strip() for value in row]
                for row in rows if isinstance(row, (list, tuple))
            ]
            if not headers or not rows:
                continue
            source_name = str(source.get("id") or source.get("name") or "table")
            target_name = source_name
            if len(requested) == 1:
                candidate_name, candidate_spec = requested[0]
                expected = candidate_spec.get("columns", [])
                def header_key(value: Any) -> str:
                    label = re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()
                    if "subject" in label and "code" not in label: return "subject"
                    if "code" in label: return "subject_code"
                    if "theory" in label: return "theory_marks"
                    if any(token in label.split() for token in ("practical", "internal", "ia", "pr")): return "practical_marks"
                    if "total" in label: return "total_marks"
                    if "grade" in label: return "grade"
                    return label.replace(" ", "_")
                source_keys = [header_key(value) for value in headers]
                expected_keys = [header_key(value) for value in expected]
                if expected_keys and all(key in source_keys for key in expected_keys):
                    positions = [source_keys.index(key) for key in expected_keys]
                    rows = [[row[index] if index < len(row) else "" for index in positions] for row in rows]
                    headers = [str(column) for column in expected]
                    row_evidence = [" | ".join(row) for row in rows]
                    target_name = candidate_name
            elif requested:
                source_tokens = set(re.findall(r"[a-z0-9]+", (source_name + " " + " ".join(headers)).casefold()))
                ranked = []
                for name, spec in requested:
                    target_tokens = set(re.findall(r"[a-z0-9]+", (name + " " + " ".join(map(str, spec.get("columns", [])))).casefold()))
                    ranked.append((len(source_tokens & target_tokens), name, spec))
                overlap, candidate, spec = max(ranked, default=(0, source_name, {}), key=lambda item: item[0])
                if overlap:
                    target_name = candidate
            row_evidence = [" | ".join(row) for row in rows]
            output.append({"name": target_name, "headers": headers, "rows": rows, "row_evidence": row_evidence, "row_provenance": ["document_parser"] * len(rows), "page": segment.get("index", 0), "source": "document_parser", "source_table": source_name})
    return output
