"""Reads invoices. It tries up to three ways of getting the text or fields:

1. Built in: text files and PDFs that contain real text. This is best effort.
2. Local tools, if installed: pdftotext for PDFs, tesseract for images and scans.
3. Claude, only if THINGSEXPIRE_ANTHROPIC_KEY is set. The file is sent to the Anthropic API, which can also read
   scans and photos.

The result is always a draft. A person has to confirm it before anything is recorded.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
import zlib
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from .config import Settings

FIELDS = ("vendor", "invoice_number", "invoice_date", "due_date", "amount", "currency")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP"}
_CODES = ("USD", "EUR", "GBP", "CAD", "AUD", "CHF", "JPY", "INR", "SEK", "NOK", "DKK", "NZD", "MXN", "BRL", "ZAR", "SAR", "AED", "EGP")


# ---- text extraction -------------------------------------------------------------------------------------------------

def _pdf_unescape(raw: bytes) -> str:
    out = bytearray()
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == 0x5C and i + 1 < len(raw):  # backslash
            n = raw[i + 1]
            simple = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12, ord("("): 40, ord(")"): 41, 0x5C: 0x5C}
            if n in simple:
                out.append(simple[n])
                i += 2
                continue
            if 48 <= n <= 55:  # octal
                j = i + 1
                digits = b""
                while j < len(raw) and len(digits) < 3 and 48 <= raw[j] <= 55:
                    digits += bytes([raw[j]])
                    j += 1
                out.append(int(digits, 8) & 0xFF)
                i = j
                continue
            i += 1
            continue
        out.append(c)
        i += 1
    return out.decode("latin-1")


_STRING = rb"\((?:\\.|[^\\()])*\)"


def pdf_text_builtin(data: bytes) -> str:
    """Pulls text out of simple PDFs (text layer, standard encodings). Returns '' for scans or exotic fonts."""
    chunks: list[bytes] = []
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        body = match.group(1)
        try:
            chunks.append(zlib.decompress(body))
        except zlib.error:
            chunks.append(body)
    lines: list[str] = []
    for content in chunks:
        if b"BT" not in content:
            continue
        for block in re.finditer(rb"BT(.*?)ET", content, re.S):
            current = ""
            for token in re.finditer(rb"(" + _STRING + rb")\s*Tj|\[((?:" + _STRING + rb"|[^\]()])*)\]\s*TJ|(T\*|Td|TD|')", block.group(1)):
                if token.group(1):
                    current += _pdf_unescape(token.group(1)[1:-1])
                elif token.group(2) is not None:
                    for part in re.finditer(_STRING + rb"|-?\d+\.?\d*", token.group(2)):
                        piece = part.group(0)
                        if piece.startswith(b"("):
                            current += _pdf_unescape(piece[1:-1])
                        elif float(piece) < -200:  # a big negative kerning value is a visible gap
                            current += " "
                else:
                    if current.strip():
                        lines.append(current)
                    current = ""
            if current.strip():
                lines.append(current)
    text = "\n".join(lines)
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\n\t")
    return text if text and printable / max(len(text), 1) > 0.9 else ""


def _run_tool(args: list[str], data: bytes, suffix: str) -> str:
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(data)
        path = handle.name
    try:
        full = [path if a == "{file}" else a for a in args]
        result = subprocess.run(full, capture_output=True, timeout=60, check=False)
        return result.stdout.decode("utf-8", "replace") if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""
    finally:
        import os
        try:
            os.remove(path)
        except OSError:
            pass


def extract_text(data: bytes, filename: str, content_type: str) -> tuple[str, str]:
    """(text, method). Method is '' when nothing could read the file."""
    if content_type in ("text/plain", "text/csv"):
        return data.decode("utf-8-sig", "replace"), "text"
    if content_type == "application/pdf":
        text = pdf_text_builtin(data)
        if text.strip():
            return text, "pdf-builtin"
        if shutil.which("pdftotext"):
            text = _run_tool(["pdftotext", "-layout", "{file}", "-"], data, ".pdf")
            if text.strip():
                return text, "pdftotext"
        return "", ""
    if content_type.startswith("image/") and shutil.which("tesseract"):
        text = _run_tool(["tesseract", "{file}", "stdout"], data, ".png" if "png" in content_type else ".jpg")
        if text.strip():
            return text, "tesseract"
    return "", ""


# ---- field parsing ---------------------------------------------------------------------------------------------------

def _to_iso(text: str) -> Optional[str]:
    text = text.strip().rstrip(".,")
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    try:
        if m:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", text)
        if m:  # 03/04/2026 is ambiguous; days above 12 settle it, otherwise assume month first
            a, b, y = int(m[1]), int(m[2]), int(m[3])
            return (date(y, b, a) if a > 12 else date(y, a, b)).isoformat()
        m = re.fullmatch(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", text)
        if m and m[1][:3].lower() in _MONTHS:
            return date(int(m[3]), _MONTHS[m[1][:3].lower()], int(m[2])).isoformat()
        m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})", text)
        if m and m[2][:3].lower() in _MONTHS:
            return date(int(m[3]), _MONTHS[m[2][:3].lower()], int(m[1])).isoformat()
    except ValueError:
        return None
    return None


_DATE_RE = (r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4}|[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}"
            r"|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\.?,?\s+\d{4})")
_AMOUNT_RE = r"(?:[$€£]\s*|(?:" + "|".join(_CODES) + r")\s*)?(\d{1,3}(?:[,.\s]\d{3})*(?:[.,]\d{2})|\d+(?:[.,]\d{2})?)"


def parse_amount(text: str) -> Optional[Decimal]:
    text = text.strip().replace(" ", "")
    if re.search(r",\d{2}$", text) and not re.search(r"\.\d{2}$", text):  # 1.234,56 -> 1234.56
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", "")
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return value if value >= 0 else None


def _labeled_date(text: str, labels: str) -> Optional[str]:
    m = re.search(rf"(?:{labels})\s*[:#-]?\s*{_DATE_RE}", text, re.I)
    return _to_iso(m.group(1)) if m else None


def parse_invoice_text(text: str, known_vendors: Optional[list[str]] = None) -> dict[str, Any]:
    """Heuristic field finder. Returns {'fields': {...}, 'confidence': {field: 'high'|'low'}}."""
    fields: dict[str, Any] = {}
    confidence: dict[str, str] = {}

    m = re.search(r"invoice\s*(?:no\.?|number|num|#)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/_.]{1,30})", text, re.I)
    if m:
        fields["invoice_number"], confidence["invoice_number"] = m.group(1).strip(".-"), "high"
    else:
        m = re.search(r"\b(?:inv|invoice)[-_#]\s*([A-Z0-9][A-Z0-9\-/_.]{1,30})", text, re.I)
        if m:
            fields["invoice_number"], confidence["invoice_number"] = m.group(0).strip(), "low"

    due = _labeled_date(text, r"due\s*date|payment\s*due|pay\s*by|due\s*on|due")
    inv = _labeled_date(text, r"invoice\s*date|date\s*of\s*issue|issued|issue\s*date|date")
    if inv:
        fields["invoice_date"], confidence["invoice_date"] = inv, "high"
    if due:
        fields["due_date"], confidence["due_date"] = due, "high"
    if "invoice_date" not in fields:
        any_date = re.search(_DATE_RE, text)
        if any_date and _to_iso(any_date.group(1)):
            fields["invoice_date"], confidence["invoice_date"] = _to_iso(any_date.group(1)), "low"

    best: Optional[tuple[int, Decimal, str]] = None  # (priority, amount, line)
    for line in text.splitlines():
        low = line.lower()
        if "subtotal" in low or "sub-total" in low or "tax" in low and "total" not in low.replace("tax", ""):
            continue
        for priority, label in ((3, r"amount\s*due|balance\s*due|total\s*due|amount\s*payable"), (2, r"grand\s*total|total\s*amount|invoice\s*total"), (1, r"\btotal\b")):
            lm = re.search(label, line, re.I)
            if lm:
                amounts = re.findall(_AMOUNT_RE, line[lm.end():])
                values = [parse_amount(a) for a in amounts]
                values = [v for v in values if v is not None]
                if values and (best is None or priority > best[0] or (priority == best[0] and values[-1] >= best[1])):
                    best = (priority, values[-1], line)
                break
    if best:
        fields["amount"] = format(best[1].quantize(Decimal("0.01")), "f")
        confidence["amount"] = "high"
        for symbol, code in _SYMBOLS.items():
            if symbol in best[2]:
                fields["currency"], confidence["currency"] = code, "high"
        if "currency" not in fields:
            cm = re.search(r"\b(" + "|".join(_CODES) + r")\b", best[2])
            if cm:
                fields["currency"], confidence["currency"] = cm.group(1), "high"
    if "currency" not in fields:
        cm = re.search(r"\b(" + "|".join(_CODES) + r")\b", text)
        if cm:
            fields["currency"], confidence["currency"] = cm.group(1), "low"
        else:
            for symbol, code in _SYMBOLS.items():
                if symbol in text:
                    fields["currency"], confidence["currency"] = code, "low"
                    break

    lowered = text.lower()
    for vendor in sorted(known_vendors or [], key=len, reverse=True):
        if vendor and vendor.lower() in lowered:
            fields["vendor"], confidence["vendor"] = vendor, "high"
            break
    if "vendor" not in fields:
        m = re.search(r"(?:^|\n)\s*(?:from|vendor|supplier|billed\s*by|sold\s*by)\s*[:\-]\s*(.+)", text, re.I)
        if m and m.group(1).strip():
            fields["vendor"], confidence["vendor"] = m.group(1).strip()[:120], "high"
        else:
            for line in text.splitlines():
                clean = line.strip()
                if clean and "invoice" not in clean.lower() and not re.fullmatch(r"[\W\d_]+", clean) and len(clean) <= 80:
                    fields["vendor"], confidence["vendor"] = clean, "low"
                    break
    return {"fields": fields, "confidence": confidence}


# ---- Claude (opt-in) -------------------------------------------------------------------------------------------------

_PROMPT = (
    "Read this invoice and answer with ONE JSON object and nothing else, with keys: vendor (who issued it), "
    "invoice_number, invoice_date (YYYY-MM-DD), due_date (YYYY-MM-DD), amount (total payable as a plain number such as "
    "1234.50), currency (ISO code such as USD). Use an empty string for anything you cannot find. Do not guess."
)


def claude_extract(data: bytes, content_type: str, settings: Settings, text: str = "") -> dict[str, Any]:
    """Asks the Anthropic Messages API to read the file. Raises RuntimeError with a readable message on failure."""
    if content_type == "application/pdf":
        block = {"type": "document", "source": {"type": "base64", "media_type": content_type, "data": base64.b64encode(data).decode()}}
    elif content_type.startswith("image/"):
        block = {"type": "image", "source": {"type": "base64", "media_type": content_type, "data": base64.b64encode(data).decode()}}
    else:
        block = {"type": "text", "text": (text or data.decode("utf-8", "replace"))[:20000]}
    payload = {"model": settings.ai_model, "max_tokens": 600, "messages": [{"role": "user", "content": [block, {"type": "text", "text": _PROMPT}]}]}
    request = urllib.request.Request(
        settings.ai_url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"x-api-key": settings.ai_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"The AI service answered HTTP {exc.code}.") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"The AI service could not be reached: {exc}") from None
    reply = "".join(part.get("text", "") for part in body.get("content", []) if part.get("type") == "text")
    match = re.search(r"\{.*\}", reply, re.S)
    if not match:
        raise RuntimeError("The AI service did not return fields.")
    try:
        raw = json.loads(match.group(0))
    except ValueError:
        raise RuntimeError("The AI service returned something that was not JSON.") from None
    fields: dict[str, Any] = {}
    for key in FIELDS:
        value = str(raw.get(key) or "").strip()
        if value:
            fields[key] = value
    return fields


# ---- orchestration ---------------------------------------------------------------------------------------------------

def read_invoice(data: bytes, filename: str, content_type: str, settings: Settings,
                 known_vendors: Optional[list[str]] = None) -> dict[str, Any]:
    """Returns {'fields', 'confidence', 'source', 'text', 'notes'}. Never raises for unreadable files."""
    notes: list[str] = []
    text, method = extract_text(data, filename, content_type)
    parsed = parse_invoice_text(text, known_vendors) if text else {"fields": {}, "confidence": {}}
    source = method or ""
    if settings.ai_key:
        try:
            ai = claude_extract(data, content_type, settings, text)
            for key, value in ai.items():
                parsed["fields"][key] = value
                parsed["confidence"][key] = "ai"
            source = "claude" + (f"+{method}" if method else "")
        except RuntimeError as exc:
            notes.append(str(exc))
    if not parsed["fields"] and not source:
        notes.append("This file has no readable text. Install tesseract (images/scans) or pdftotext, or set "
                     "THINGSEXPIRE_ANTHROPIC_KEY to let Claude read it. You can still fill the fields in by hand.")
    return {"fields": parsed["fields"], "confidence": parsed["confidence"], "source": source, "text": text[:20000], "notes": notes}
