"""Local PDF validation and literal, region-grounded text; no OCR or value extraction."""
from __future__ import annotations

import hashlib
import math
from contextlib import contextmanager
from io import BytesIO
from typing import Iterator, TypedDict

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfplumber.utils import extract_text

MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 500
MAX_QUOTE_CHARS = 20_000


class DocumentError(ValueError):
    def __init__(self, detail: str, status_code: int = 422):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class PDFInspection(TypedDict):
    pageCount: int
    digest: str


def _validate_bytes(pdf: bytes) -> None:
    if not isinstance(pdf, bytes) or not pdf:
        raise DocumentError("A nonempty PDF document is required.")
    if len(pdf) > MAX_PDF_BYTES:
        raise DocumentError("PDF documents must be at most 10 MiB.", 413)
    if not pdf.startswith(b"%PDF-") or b"%%EOF" not in pdf[-1024:]:
        raise DocumentError("The document is not a complete PDF.")


@contextmanager
def _open_pdf(pdf: bytes) -> Iterator[pdfplumber.PDF]:
    _validate_bytes(pdf)
    try:
        with pdfplumber.open(BytesIO(pdf)) as document:
            if document.doc.encryption:
                raise DocumentError("Encrypted PDFs are not supported. Upload an unencrypted copy.")
            if not document.pages or len(document.pages) > MAX_PDF_PAGES:
                raise DocumentError(f"PDF documents must have between 1 and {MAX_PDF_PAGES} pages.")
            yield document
    except DocumentError:
        raise
    except PDFPasswordIncorrect as error:
        raise DocumentError("Encrypted PDFs are not supported. Upload an unencrypted copy.") from error
    except Exception as error:
        raise DocumentError("The PDF is malformed or cannot be read.") from error


def _page_bounds(page) -> tuple[float, float, float, float]:
    left, top, right, bottom = page.bbox
    bounds = (float(left), float(top), float(right), float(bottom))
    if (not all(math.isfinite(value) for value in bounds)
            or bounds[2] <= bounds[0] or bounds[3] <= bounds[1]):
        raise DocumentError("The PDF contains invalid page dimensions.")
    return bounds


def inspect_pdf(pdf: bytes) -> PDFInspection:
    """Validate the actual bytes; image-only pages remain valid saved documents."""
    with _open_pdf(pdf) as document:
        for page in document.pages:
            _page_bounds(page)
            # Parse page contents now, so corrupt content streams never become saved evidence.
            # No-text pages are deliberately allowed: storing an image does not imply OCR.
            page.chars
        return {"pageCount": len(document.pages), "digest": hashlib.sha256(pdf).hexdigest()}


def validate_rect(rect: object) -> list[float]:
    """Use the viewer's existing normalized [left, top, width, height] convention."""
    if (not isinstance(rect, (list, tuple)) or len(rect) != 4
            or any(type(value) not in (int, float) for value in rect)):
        raise DocumentError("rect must contain four finite numbers: left, top, width, height.")
    normalized = [0.0 if value == 0 else float(value) for value in rect]
    if not all(math.isfinite(value) and 0 <= value <= 1 for value in normalized):
        raise DocumentError("Evidence rectangle coordinates must be finite numbers between 0 and 1.")
    left, top, width, height = normalized
    if width <= 0 or height <= 0 or left + width > 1 or top + height > 1:
        raise DocumentError("The evidence rectangle must have positive area and stay inside the page.")
    return normalized


def extract_region(pdf: bytes, page: int, rect: object) -> str:
    """Quote only real characters whose boxes intersect the selected visible page region."""
    region = validate_rect(rect)
    if type(page) is not int or page < 1:
        raise DocumentError("page must be a positive integer.")
    with _open_pdf(pdf) as document:
        if page > len(document.pages):
            raise DocumentError("The selected page does not exist in this document.")
        selected = document.pages[page - 1]
        x0, top0, x1, bottom = _page_bounds(selected)
        # PDF.js displays the CropBox when present. Match its visible, rotated page.
        if selected.cropbox is not None:
            crop = selected.cropbox
            visible = (max(x0, crop[0]), max(top0, crop[1]), min(x1, crop[2]), min(bottom, crop[3]))
            if visible[2] <= visible[0] or visible[3] <= visible[1]:
                raise DocumentError("The PDF contains an invalid visible page region.")
            x0, top0, x1, bottom = visible
        left, top, width, height = region
        bounds = (x0 + left * (x1 - x0), top0 + top * (bottom - top0),
                  x0 + (left + width) * (x1 - x0), top0 + (top + height) * (bottom - top0))
        characters = [character for character in selected.chars
                      if character["x0"] < bounds[2] and character["x1"] > bounds[0]
                      and character["top"] < bounds[3] and character["bottom"] > bounds[1]]
        quote = extract_text(characters).strip() if characters else ""
        if not quote or not any(character.isprintable() and not character.isspace() for character in quote):
            raise DocumentError("No usable text exists in this region. Scanned/image-only PDFs remain saved, but OCR is not available; select a text-based source.")
        if len(quote) > MAX_QUOTE_CHARS:
            raise DocumentError("The selected region contains too much text. Select a smaller evidence region.")
        return quote


MAX_TEXT_CHARS = 500_000
MAX_TEXT_LINES = 20_000


def inspect_document(content: bytes, media_type: str) -> PDFInspection:
    if media_type == "application/pdf":
        return inspect_pdf(content)
    if media_type != "text/plain":
        raise DocumentError("Upload a selectable-text PDF or UTF-8 TXT file.", 415)
    text = decode_text(content)
    if len(text.splitlines()) > MAX_TEXT_LINES:
        raise DocumentError(f"TXT documents must have at most {MAX_TEXT_LINES} lines.")
    return {"pageCount": 1, "digest": hashlib.sha256(content).hexdigest()}


def decode_text(content: bytes) -> str:
    if not isinstance(content, bytes) or not content:
        raise DocumentError("A nonempty UTF-8 TXT document is required.")
    if len(content) > MAX_PDF_BYTES:
        raise DocumentError("Documents must be at most 10 MiB.", 413)
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise DocumentError("TXT documents must use UTF-8 encoding.") from error
    if "\x00" in text or not text.strip() or len(text) > MAX_TEXT_CHARS:
        raise DocumentError("TXT documents must contain readable text, at most 500,000 characters, without NUL bytes.")
    return text


def source_pages(content: bytes, media_type: str) -> list[dict]:
    """Build numbered immutable source lines and their actual viewer coordinates."""
    inspect_document(content, media_type)
    if media_type == "text/plain":
        lines = decode_text(content).splitlines()
        count = len(lines)
        return [{"page": 1, "lines": [
            {"text": line, "rect": [0.0, index / count, 1.0, 1 / count]}
            for index, line in enumerate(lines)
        ]}]
    pages = []
    characters = 0
    with _open_pdf(content) as document:
        for number, page in enumerate(document.pages, 1):
            x0, top, x1, bottom = _page_bounds(page)
            if page.cropbox is not None:
                crop = page.cropbox
                x0, top, x1, bottom = max(x0, crop[0]), max(top, crop[1]), min(x1, crop[2]), min(bottom, crop[3])
            if x1 <= x0 or bottom <= top:
                raise DocumentError("The PDF contains an invalid visible page region.")
            visible = page.crop((x0, top, x1, bottom))
            # TextMap lines retain the glyph-derived coordinates; the model supplies no rectangles.
            lines = []
            for line in visible.extract_text_lines():
                text = line["text"]
                if not text.strip():
                    continue
                characters += len(text)
                if characters > MAX_TEXT_CHARS:
                    raise DocumentError("The PDF contains too much text; split it into smaller documents.")
                left = max(x0, float(line["x0"]))
                right = min(x1, float(line["x1"]))
                line_top = max(top, float(line["top"]))
                line_bottom = min(bottom, float(line["bottom"]))
                if right > left and line_bottom > line_top:
                    lines.append({"text": text, "rect": [
                        (left - x0) / (x1 - x0), (line_top - top) / (bottom - top),
                        (right - left) / (x1 - x0), (line_bottom - line_top) / (bottom - top),
                    ]})
            pages.append({"page": number, "lines": lines})
    if not any(page["lines"] for page in pages):
        raise DocumentError("This PDF has no selectable text. OCR and scanned/image-only documents are not supported; upload a text-based PDF or TXT.")
    if sum(len(page["lines"]) for page in pages) > MAX_TEXT_LINES:
        raise DocumentError("The PDF contains too many text lines; split it into smaller documents.")
    return pages


def literal_citation(pages: list[dict], *, page: int, line_start: int, line_end: int,
                     quote: str, media_type: str) -> dict:
    """Check model line/quote claims against source text, then derive the rectangle locally."""
    if (type(page) is not int or type(line_start) is not int or type(line_end) is not int
            or not 1 <= page <= len(pages) or not 1 <= line_start <= line_end <= len(pages[page - 1]["lines"])):
        raise DocumentError("Extraction cited a nonexistent source page or line.")
    selected = pages[page - 1]["lines"][line_start - 1:line_end]
    source = "\n".join(line["text"] for line in selected)
    if not isinstance(quote, str) or not quote.strip() or len(quote) > MAX_QUOTE_CHARS or quote not in source:
        raise DocumentError("Extraction returned a quote that is not literal text in its cited source lines.")
    left = min(line["rect"][0] for line in selected)
    top = min(line["rect"][1] for line in selected)
    right = max(line["rect"][0] + line["rect"][2] for line in selected)
    bottom = max(line["rect"][1] + line["rect"][3] for line in selected)
    citation = {"page": page, "rect": [left, top, min(1.0, right) - left, min(1.0, bottom) - top], "quote": quote}
    if media_type == "text/plain":
        citation.update(lineStart=line_start, lineEnd=line_end)
    return citation
