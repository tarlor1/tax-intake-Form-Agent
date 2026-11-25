"""Generate synthetic letter-size PDFs and normalized citation rectangles."""

_PAGE_WIDTH = 612
_PAGE_HEIGHT = 792
SYNTHETIC_FOOTER = "Generated sample values. Do not use for filing."


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def source(
    document_id: str, title: str, rows: list[str], version: int = 1, *, extra_pages: tuple[list[str], ...] = (),
) -> tuple[dict, bytes, list[dict]]:
    """Make a real PDF and matching fixture regions, without libraries."""
    commands = [
        "BT /F1 17 Tf 48 744 Td (" + _pdf_escape(title) + ") Tj ET",
        "BT /F1 10 Tf 48 718 Td (SYNTHETIC DEMONSTRATION - NOT A FILED TAX DOCUMENT) Tj ET",
    ]
    citations = []
    streams = []
    for page, page_rows in enumerate((rows, *extra_pages), start=1):
        lines = list(commands)
        for index, text in enumerate(page_rows):
            baseline_from_top = 130 + index * 34
            lines.append(
                f"BT /F2 11 Tf 48 {_PAGE_HEIGHT - baseline_from_top} Td ({_pdf_escape(text)}) Tj ET"
            )
            citations.append(
                {
                    "id": f"{document_id}-citation-{len(citations) + 1}",
                    "documentId": document_id,
                    "page": page,
                    "rect": [
                        46 / _PAGE_WIDTH,
                        (baseline_from_top - 12) / _PAGE_HEIGHT,
                        min(520, len(text) * 6.6 + 4) / _PAGE_WIDTH,
                        17 / _PAGE_HEIGHT,
                    ],
                    "quote": text,
                    "label": f"{title}, line {index + 1}" if page == 1 else f"{title}, page {page}, line {index + 1}",
                }
            )
        lines.append(
            f"BT /F1 9 Tf 48 42 Td ({SYNTHETIC_FOOTER}) Tj ET"
        )
        streams.append("\n".join(lines).encode("ascii"))
    children = " ".join(f"{5 + 2 * index} 0 R" for index in range(len(streams)))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{children}] /Count {len(streams)} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
    ]
    for index, stream in enumerate(streams):
        objects.extend([
            (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
             f"/Contents {6 + index * 2} 0 R >>").encode(),
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
        ])
    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return (
        {"id": document_id, "title": title, "pageCount": len(streams), "version": version},
        bytes(pdf),
        citations,
    )
