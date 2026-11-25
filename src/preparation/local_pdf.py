"""Read ruled PDF cells and derive literal, visible-page evidence from their glyphs."""

from collections.abc import Iterator
from dataclasses import dataclass

from pdfplumber.utils import chars_to_textmap, extract_text

from ..review_documents import _open_pdf, _page_bounds


@dataclass(frozen=True, slots=True)
class PDFCell:
    text: str
    rect: tuple[float, float, float, float] | None
    quote: str | None


def _intersects(char: dict, bounds: tuple[float, float, float, float]) -> bool:
    left, top, right, bottom = bounds
    return char["x0"] < right and char["x1"] > left and char["top"] < bottom and char["bottom"] > top


def _evidence_rect(chars: list[dict], page_chars: list[dict], quote: str,
                   visible: tuple[float, float, float, float]) -> tuple[float, float, float, float] | None:
    x, y, right, bottom = visible
    width, height = right - x, bottom - y
    # A printed rule can touch the preceding row's glyph box. Tighten to the
    # glyphs owned by this cell, then check the same overlap rule as extract_region.
    pad = 0.05
    chars = [char for char in chars if char["text"].strip()]
    if not chars:
        return None
    left = max(0.0, (min(c["x0"] for c in chars) - pad - x) / width)
    top = max(0.0, (min(c["top"] for c in chars) - pad - y) / height)
    end_x = min(1.0, (max(c["x1"] for c in chars) + pad - x) / width)
    end_y = min(1.0, (max(c["bottom"] for c in chars) + pad - y) / height)
    if left >= end_x or top >= end_y:
        return None
    rect = left, top, end_x - left, end_y - top
    bounds = (x + left * width, y + top * height,
              x + (left + rect[2]) * width, y + (top + rect[3]) * height)
    literal = extract_text([char for char in page_chars if _intersects(char, bounds)]).strip()
    return rect if literal == quote else None


def ruled_tables(content: bytes, page_numbers: set[int]) -> Iterator[tuple[int, tuple[PDFCell, ...]]]:
    """Yield recognized vector grids, with each glyph assigned by its center."""
    with _open_pdf(content) as document:
        for number in sorted(page_numbers):
            page = document.pages[number - 1]
            x, y, right, bottom = _page_bounds(page)
            crop = page.cropbox
            if crop is not None:
                x, y = max(x, crop[0]), max(y, crop[1])
                right, bottom = min(right, crop[2]), min(bottom, crop[3])
            visible = x, y, right, bottom
            if right <= x or bottom <= y:
                continue
            surface = page if visible == page.bbox else page.crop(visible)
            page_chars = page.chars
            positions = [((char["x0"] + char["x1"]) / 2, (char["top"] + char["bottom"]) / 2, char)
                         for char in page_chars]
            for table in sorted(surface.find_tables(), key=lambda table: (table.bbox[1], table.bbox[0])):
                left, top, end_x, end_y = table.bbox
                table_chars = [(cx, cy, char) for cx, cy, char in positions
                               if left <= cx < end_x and top <= cy < end_y]
                cells = []
                for left, top, end_x, end_y in table.cells:
                    chars = [char for cx, cy, char in table_chars if left <= cx < end_x and top <= cy < end_y]
                    text = extract_text(chars).strip() if chars else ""
                    if text:
                        quote = text
                        rect = _evidence_rect(chars, page_chars, quote, visible)
                        if rect is None and "\n" in text:
                            # If adjacent ink crosses into the full-cell crop,
                            # try the last row alone. The caller checks that the
                            # narrower quote still contains the complete amount.
                            lines = chars_to_textmap(chars).extract_text_lines()
                            if lines:
                                quote = text.rsplit("\n", 1)[1]
                                rect = _evidence_rect(lines[-1]["chars"], page_chars, quote, visible)
                        cells.append(PDFCell(text, rect, quote if rect is not None else None))
                yield number, tuple(cells)
