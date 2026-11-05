# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""A tiny, dependency-free PDF writer.

Only what the summary-report feature (vss_summary.py) needs: a title, section
headings, and word-wrapped body paragraphs/bullets, paginated onto US-Letter
pages with a built-in Helvetica font. No third-party package (e.g. reportlab)
is required, so this stays installable offline and with zero extra Docker
image weight -- the PDF spec's text/page objects are simple enough to emit
directly.

Not a general-purpose PDF library: no images, no custom fonts, no non-ASCII
beyond latin-1 (characters outside that range are replaced with '?', which is
acceptable for this tool's generated-English reports).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

PAGE_WIDTH = 612.0  # US Letter, points (72/inch)
PAGE_HEIGHT = 792.0
MARGIN = 54.0
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN

_FONT_REGULAR = "/F1"
_FONT_BOLD = "/F2"

# Helvetica's built-in per-character advance widths (in 1/1000 em), for the
# ASCII range -- used to wrap text without a real font-metrics library. Falls
# back to 556 (average) for characters outside this table.
_HELVETICA_WIDTHS = {
    " ": 278, "!": 278, '"': 355, "#": 556, "$": 556, "%": 889, "&": 667, "'": 191,
    "(": 333, ")": 333, "*": 389, "+": 584, ",": 278, "-": 333, ".": 278, "/": 278,
    "0": 556, "1": 556, "2": 556, "3": 556, "4": 556, "5": 556, "6": 556, "7": 556,
    "8": 556, "9": 556, ":": 278, ";": 278, "<": 584, "=": 584, ">": 584, "?": 556,
    "@": 1015, "[": 278, "\\": 278, "]": 278, "^": 469, "_": 556, "`": 333, "{": 334,
    "|": 260, "}": 334, "~": 584,
}
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    _HELVETICA_WIDTHS[_c] = 667
for _c in "abcdefghijklmnopqrstuvwxyz":
    _HELVETICA_WIDTHS[_c] = 556
for _c in "ilj.,'":
    _HELVETICA_WIDTHS[_c] = 222


def _text_width(text: str, size: float) -> float:
    return sum(_HELVETICA_WIDTHS.get(ch, 556) for ch in text) * size / 1000.0


def _wrap(text: str, size: float, max_width: float) -> list:
    """Greedy word-wrap, respecting embedded newlines as hard breaks."""
    lines = []
    for raw_line in text.split("\n"):
        words = raw_line.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if _text_width(candidate, size) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _escape(text: str) -> str:
    text = text.encode("latin-1", errors="replace").decode("latin-1")
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


@dataclass
class _Block:
    kind: str  # "heading" | "paragraph" | "bullet" | "spacer" | "rule"
    text: str = ""


@dataclass
class PdfReport:
    """Builds a simple, paginated PDF from a title + a sequence of blocks."""

    title: str
    subtitle: str = ""
    _blocks: list = field(default_factory=list)

    def heading(self, text: str) -> "PdfReport":
        self._blocks.append(_Block("heading", text))
        return self

    def paragraph(self, text: str) -> "PdfReport":
        self._blocks.append(_Block("paragraph", text))
        return self

    def bullet(self, text: str) -> "PdfReport":
        self._blocks.append(_Block("bullet", text))
        return self

    def rule(self) -> "PdfReport":
        self._blocks.append(_Block("rule"))
        return self

    def spacer(self) -> "PdfReport":
        self._blocks.append(_Block("spacer"))
        return self

    # -- layout ---------------------------------------------------------

    def _layout_pages(self) -> list:
        """Return a list of pages; each page is a list of (x, y, font, size, text)
        draw ops plus a list of (x0, y0, x1, y1) rule segments."""
        pages = []
        ops: list = []
        rules: list = []
        y = PAGE_HEIGHT - MARGIN

        def new_page():
            nonlocal ops, rules, y
            if ops or rules:
                pages.append((ops, rules))
            ops, rules = [], []
            y = PAGE_HEIGHT - MARGIN

        def ensure_space(height: float):
            nonlocal y
            if y - height < MARGIN:
                new_page()

        # Title block.
        ensure_space(28)
        ops.append((MARGIN, y, _FONT_BOLD, 18, self.title))
        y -= 24
        if self.subtitle:
            ensure_space(16)
            ops.append((MARGIN, y, _FONT_REGULAR, 10, self.subtitle))
            y -= 18
        y -= 6
        rules.append((MARGIN, y, PAGE_WIDTH - MARGIN, y))
        y -= 16

        for block in self._blocks:
            if block.kind == "spacer":
                y -= 10
                continue
            if block.kind == "rule":
                ensure_space(10)
                rules.append((MARGIN, y, PAGE_WIDTH - MARGIN, y))
                y -= 14
                continue
            if block.kind == "heading":
                ensure_space(20)
                y -= 6
                ops.append((MARGIN, y, _FONT_BOLD, 13, block.text))
                y -= 18
                continue
            indent = 14 if block.kind == "bullet" else 0
            size = 10
            prefix = "- " if block.kind == "bullet" else ""
            lines = _wrap(prefix + block.text, size, CONTENT_WIDTH - indent)
            for i, line in enumerate(lines):
                ensure_space(14)
                x = MARGIN + indent
                ops.append((x, y, _FONT_REGULAR, size, line))
                y -= 13.5
            y -= 4

        new_page()
        return pages

    def render(self) -> bytes:
        pages = self._layout_pages() or [([], [])]
        objects: list = []  # list[bytes], 1-indexed via objects[i-1]

        def add_object(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        font_regular_id = add_object(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
        )
        font_bold_id = add_object(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"
        )

        page_ids = []
        content_ids = []
        for ops, rules in pages:
            stream_parts = ["BT"]
            current_font: Optional[str] = None
            current_size: Optional[float] = None
            for x, y, font, size, text in ops:
                if not text:
                    continue
                if font != current_font or size != current_size:
                    stream_parts.append(f"{font} {size} Tf")
                    current_font, current_size = font, size
                stream_parts.append(f"1 0 0 1 {x:.2f} {y:.2f} Tm")
                stream_parts.append(f"({_escape(text)}) Tj")
            stream_parts.append("ET")
            for x0, y0, x1, y1 in rules:
                stream_parts.append("0.7 0.7 0.7 RG 0.75 w")
                stream_parts.append(f"{x0:.2f} {y0:.2f} m {x1:.2f} {y1:.2f} l S")
            stream = "\n".join(stream_parts).encode("latin-1", errors="replace")
            content_id = add_object(
                f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1") + stream + b"\nendstream"
            )
            content_ids.append(content_id)
            # Page object id reserved, filled in once we know the Pages id.
            page_ids.append(add_object(b""))  # placeholder

        pages_id = add_object(b"")  # placeholder, filled below
        kids = " ".join(f"{pid} 0 R" for pid in page_ids)
        objects[pages_id - 1] = (
            f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("latin-1")
        )
        for pid, cid in zip(page_ids, content_ids):
            objects[pid - 1] = (
                f"<< /Type /Page /Parent {pages_id} 0 R "
                f"/MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
                f"/Resources << /Font << {_FONT_REGULAR} {font_regular_id} 0 R "
                f"{_FONT_BOLD} {font_bold_id} 0 R >> >> "
                f"/Contents {cid} 0 R >>"
            ).encode("latin-1")

        catalog_id = add_object(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("latin-1"))

        # Serialize.
        out = bytearray()
        out += b"%PDF-1.4\n"
        offsets = [0] * (len(objects) + 1)
        for i, body in enumerate(objects, start=1):
            offsets[i] = len(out)
            out += f"{i} 0 obj\n".encode("latin-1")
            out += body
            out += b"\nendobj\n"
        xref_offset = len(out)
        out += f"xref\n0 {len(objects) + 1}\n".encode("latin-1")
        out += b"0000000000 65535 f \n"
        for i in range(1, len(objects) + 1):
            out += f"{offsets[i]:010d} 00000 n \n".encode("latin-1")
        out += b"trailer\n"
        out += f"<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n".encode("latin-1")
        out += b"startxref\n"
        out += f"{xref_offset}\n".encode("latin-1")
        out += b"%%EOF"
        return bytes(out)
