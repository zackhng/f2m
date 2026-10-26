"""PDF -> Document with PyMuPDF.

- Text: headings guessed from font size (relative to the body text size), lists
  from bullet/number prefixes, bold/italic from span flags.
- Tables: PyMuPDF's table finder; text inside a table is not repeated.
- Images: embedded raster images go to the image handler.
- Vector graphics: clusters of drawings (usually charts/diagrams) are rendered
  to PNG and sent to the image handler too, since that's what the future model
  will read.
- Lines repeated at the top/bottom of most pages (headers/footers) are dropped.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

import pymupdf

from f2m.common.tables import fmt_value, make_table
from f2m.context import ParseContext
from f2m.document import (
    Block,
    Document,
    Heading,
    ImageBlock,
    Inline,
    ListBlock,
    ListItem,
    PageBreak,
    Paragraph,
    Text,
    plain,
)
from f2m.registry import Parser, register
from f2m.timing import timed

log = logging.getLogger(__name__)
if hasattr(pymupdf, "no_recommend_layout"):
    pymupdf.no_recommend_layout()  # silence the stdout hint about the optional layout package

_BULLET = re.compile(r"^\s*([•◦▪▫●○■□‣⁃∙·\-–—*-]|\(?\d{1,3}[.)]|\(?[a-z][)])\s+")
# Table-of-contents entry: leader dots (or …/·/_) then a page number at the end of the line.
_TOC = re.compile(r"(?:\s*[.…·_]\s*){4,}\s*(\d{1,4}|[ivxlc]{1,6})\s*$", re.IGNORECASE)
_LEADER = re.compile(r"(?:[.…·_]\s*){4,}")
_FLAG_ITALIC = 2
_FLAG_MONO = 8
_FLAG_BOLD = 16
_MARGIN = 0.07  # top/bottom share of the page where headers/footers live


@dataclass
class _Line:
    runs: list[Inline]
    size: float
    bold: bool
    x0: float
    bbox: tuple[float, float, float, float]

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)


def _norm(text: str) -> str:
    return re.sub(r"\d+", "#", text.strip().lower())


def _overlap(a, b) -> float:
    """Share of rect a that lies inside rect b."""
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    area = (a[2] - a[0]) * (a[3] - a[1])
    return ((x1 - x0) * (y1 - y0)) / area if area > 0 else 0.0


@register
class PdfParser(Parser):
    formats = ("pdf",)
    extensions = (".pdf",)

    def parse(self, data: bytes, ctx: ParseContext) -> Document:
        with timed("open"):
            pdf = pymupdf.open(stream=data, filetype="pdf")
        if pdf.needs_pass and not pdf.authenticate(""):
            raise ValueError("PDF is password protected")
        doc = Document(format="pdf")
        meta = pdf.metadata or {}
        if meta.get("title"):
            doc.metadata["title"] = meta["title"].strip()
        doc.metadata["pages"] = pdf.page_count

        with timed("extract_text"):
            pages = [p.get_text("dict", sort=True, flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
                     for p in pdf]
        with timed("font_stats"):
            body, heading_levels = _font_stats(pages)
        with timed("headers_footers"):
            repeated = _repeated_lines(pdf, pages) if ctx.options.pdf_strip_headers_footers else set()
        cache = _PdfImages(pdf)

        for i, page in enumerate(pdf):
            doc.add(PageBreak(f"page {i + 1}"))
            try:
                with timed("pages"):
                    doc.extend(_PageWriter(pdf, page, pages[i], i + 1, ctx, body, heading_levels, repeated, doc, cache).blocks())
            except Exception as e:
                log.debug("page failed", exc_info=True)
                doc.warn(f"page {i + 1} could not be fully converted: {e}")
        return doc


_PROSE_WORDS = 8


def _is_prose(block: dict) -> bool:
    """A text block long enough to be body text rather than a figure label."""
    words = 0
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            words += len(span.get("text", "").split())
            if words >= _PROSE_WORDS:
                return True
    return False


class _PdfImages:
    """Per-document image lookup and extraction.

    - Listing uses PyMuPDF's fast image info (no hashing). Placements are matched
      to their image by pixel size among the page's own images; only pages with
      two same-sized images fall back to hashing, and each image is hashed once
      per document.
    - Each image is extracted once per document, however many pages show it.
    - Extraction happens in the loader, i.e. only if an image handler asks.
    """

    def __init__(self, pdf):
        self.pdf = pdf
        self._digest: dict[int, bytes] = {}  # xref -> pixel MD5
        self._bytes: dict[int, tuple[bytes, str]] = {}  # xref -> extracted image

    def placements(self, page) -> list[tuple[tuple, int, int, int]]:
        """(bbox, xref, width, height) for each image drawn on the page (xref 0 = inline image)."""
        infos = page.get_image_info()
        if not infos:
            return []
        by_size: dict[tuple[int, int], set[int]] = defaultdict(set)
        for item in page.get_images(full=True):
            by_size[(item[2], item[3])].add(item[0])
        if all(len(by_size.get((im["width"], im["height"]), ())) <= 1 for im in infos):
            return [(tuple(im["bbox"]), next(iter(by_size.get((im["width"], im["height"]), {0}))),
                     im["width"], im["height"]) for im in infos]
        # Same-sized images on this page: identify by pixel hash.
        by_digest = {self.digest(x): x for xrefs in by_size.values() for x in xrefs}
        return [(tuple(im["bbox"]), by_digest.get(im["digest"], 0), im["width"], im["height"])
                for im in page.get_image_info(hashes=True)]

    def digest(self, xref: int) -> bytes:
        if xref not in self._digest:
            self._digest[xref] = pymupdf.Pixmap(self.pdf, xref).digest
        return self._digest[xref]

    def loader(self, xref: int, page_index: int, bbox: tuple, doc: Document):
        def load() -> tuple[bytes, str]:
            if xref and xref in self._bytes:
                return self._bytes[xref]
            result = None
            if xref:
                try:
                    result = self._extract(xref)
                except Exception as e:
                    log.debug("image %s extraction failed: %s", xref, e)
            if result is None:
                # Inline image, or an image PyMuPDF can't convert: render its area of the page.
                try:
                    pix = self.pdf[page_index].get_pixmap(clip=pymupdf.Rect(bbox), dpi=150)
                    result = (pix.tobytes("png"), "image/png")
                except Exception as e:
                    doc.warn(f"page {page_index + 1}: an image could not be extracted: {e}")
                    result = (b"", "image/png")
            if xref:
                self._bytes[xref] = result
            return result

        return load

    def _extract(self, xref: int) -> tuple[bytes, str]:
        info = self.pdf.extract_image(xref)
        ext, smask = info.get("ext"), info.get("smask")
        if not smask and ext in ("png", "jpeg", "jpg"):
            return info["image"], "image/png" if ext == "png" else "image/jpeg"
        pix = pymupdf.Pixmap(self.pdf, xref)
        if smask:
            try:
                mask = pymupdf.Pixmap(self.pdf, smask)
                if (mask.width, mask.height) == (pix.width, pix.height):
                    if pix.alpha:
                        pix = pymupdf.Pixmap(pix, 0)  # drop existing alpha before applying the mask
                    pix = pymupdf.Pixmap(pix, mask)
                # else: mask of another size; keep the image without transparency
            except Exception as e:
                log.debug("mask for image %s ignored: %s", xref, e)
        if pix.colorspace is None or pix.colorspace.n not in (1, 3):
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)  # CMYK, Lab, DeviceN... -> RGB so PNG can hold it
        return pix.tobytes("png"), "image/png"


def _font_stats(pages: list[dict]) -> tuple[float, dict[float, int]]:
    sizes: Counter[float] = Counter()
    line_sizes: Counter[float] = Counter()  # sizes seen on short lines (heading candidates)
    for d in pages:
        for b in d.get("blocks", []):
            for line in b.get("lines", []):
                text = "".join(s["text"] for s in line["spans"]).strip()
                for s in line["spans"]:
                    sizes[round(s["size"] * 2) / 2] += len(s["text"].strip())
                if text and len(text) < 150 and line["spans"]:
                    line_sizes[round(max(s["size"] for s in line["spans"]) * 2) / 2] += 1
    if not sizes:
        return 11.0, {}
    body = sizes.most_common(1)[0][0]
    bigger = sorted((s for s in line_sizes if s >= body * 1.15), reverse=True)[:4]
    return body, {s: n + 1 for n, s in enumerate(bigger)}


def _repeated_lines(pdf, pages: list[dict]) -> set[str]:
    if len(pages) < 3:
        return set()
    counts: Counter[str] = Counter()
    for page, d in zip(pdf, pages):
        h = page.rect.height
        seen = set()
        for b in d.get("blocks", []):
            for line in b.get("lines", []):
                y0, y1 = line["bbox"][1], line["bbox"][3]
                if y1 < h * _MARGIN or y0 > h * (1 - _MARGIN):
                    t = _norm("".join(s["text"] for s in line["spans"]))
                    if t:
                        seen.add(t)
        counts.update(seen)
    return {t for t, n in counts.items() if n >= max(2, len(pages) // 2)}


class _PageWriter:
    def __init__(self, pdf, page, d: dict, number: int, ctx: ParseContext, body: float,
                 levels: dict[float, int], repeated: set[str], doc: Document, cache: _PdfImages):
        self.pdf, self.page, self.d, self.n = pdf, page, d, number
        self.opts = ctx.options
        self.ctx = ctx
        self.body = body
        self.levels = levels
        self.repeated = repeated
        self.doc = doc
        self.cache = cache
        # (left, right) of every text line on the page, to find each column's right edge
        self._line_edges = [(ln["bbox"][0], ln["bbox"][2]) for b in d.get("blocks", []) if b.get("type") == 0
                            for ln in b.get("lines", [])]
        self._right: dict[int, float] = {}

    def column_right(self, x0: float) -> float:
        """Right edge of the text column whose lines start at x0 (widest line with the same left edge)."""
        key = round(x0)
        if key not in self._right:
            self._right[key] = max((r for left, r in self._line_edges if abs(left - x0) < 3), default=0.0)
        return self._right[key]

    def blocks(self) -> list[Block]:
        items: list[tuple[float, float, list[Block]]] = []
        with timed("drawings"):  # read once; used by the table check and the figure step
            try:
                self.drawings = self.page.get_drawings()
            except Exception:
                self.drawings = []
        with timed("tables"):
            table_rects = self.tables(items)
        with timed("vector_figures"):
            figure_rects = self.figures(items, table_rects) if self.opts.pdf_vector_graphics else []
        with timed("images"):
            self.images(items, table_rects, figure_rects)
        has_text = False
        with timed("text"):
            for b in self.d.get("blocks", []):
                if b.get("type") != 0:
                    continue
                bbox = b["bbox"]
                if any(_overlap(bbox, r) > 0.5 for r in table_rects):
                    continue
                if not _is_prose(b) and any(_overlap(bbox, r) > 0.9 for r in figure_rects):
                    continue  # a label that belongs to a rendered figure
                lines = self.lines(b)
                if not lines:
                    continue
                has_text = True
                items.append((bbox[1], bbox[0], self.text_blocks(lines)))
        if not has_text and not table_rects:
            self.doc.warn(f"page {self.n} has no text layer (scanned?); use --images ocr to read it")
        items.sort(key=lambda t: (round(t[0] / 3), t[1]))
        out: list[Block] = []
        for _, _, blocks in items:
            out.extend(blocks)
        return _merge_lists(_merge_paragraphs(out))

    # -- tables -------------------------------------------------------------
    def tables(self, items) -> list[tuple]:
        rects = []
        # PyMuPDF's table finder costs ~30-90 ms/page even with no table, and its
        # default strategy only finds tables made of ruled lines. Skip pages that
        # have no horizontal + vertical rulings (or boxes): it can't find anything there.
        with timed("line_check"):
            ruled = has_rulings(self.drawings, self.page.rect.width * self.page.rect.height)
        if not ruled:
            return rects
        try:
            with timed("find"):
                found = self.page.find_tables()
        except Exception as e:
            self.doc.warn(f"page {self.n}: table detection failed: {e}")
            return rects
        for tab in found.tables:
            rows = tab.extract()
            if not rows or len(rows) < 2 and len(rows[0]) < 2:
                continue
            header = getattr(tab, "header", None)
            if header is not None and getattr(header, "external", False) and header.names:
                rows = [list(header.names)] + rows
            grid = _fill_merged(rows, self.opts.merged_cells)
            t = make_table(grid)
            if t is None:
                continue
            bbox = tuple(tab.bbox)
            rects.append(bbox)
            items.append((bbox[1], bbox[0], [t]))
        return rects

    # -- images -------------------------------------------------------------
    def images(self, items, table_rects, figure_rects) -> None:
        try:
            with timed("list"):
                placements = self.cache.placements(self.page)
        except Exception as e:
            self.doc.warn(f"page {self.n}: images could not be listed: {e}")
            return
        k = 0
        page_rect = self.page.rect
        for bbox, xref, width, height in placements:
            bbox = tuple(pymupdf.Rect(bbox) & page_rect)  # off-page parts are invisible
            if (bbox[2] - bbox[0]) < 4 or (bbox[3] - bbox[1]) < 4:
                continue
            if min(width, height) < self.opts.pdf_min_image_size:
                continue
            if any(_overlap(bbox, r) > 0.9 for r in figure_rects):
                continue  # already part of a rendered figure
            k += 1
            items.append((bbox[1], bbox[0], [ImageBlock(
                name_hint=f"page{self.n:03d}_img{k}", location={"page": self.n, "bbox": bbox, "xref": xref},
                loader=self.cache.loader(xref, self.n - 1, bbox, self.doc))]))

    def figures(self, items, table_rects) -> list[tuple]:
        """Render clusters of vector drawings (charts, diagrams) as PNG images."""
        try:
            drawings = self.drawings
            if len(drawings) < 3:
                return []
            clusters = self.page.cluster_drawings(drawings=drawings)
        except Exception:
            return []
        page_area = self.page.rect.width * self.page.rect.height
        rects = []
        for rect in clusters:
            r = tuple(rect)
            w, h = r[2] - r[0], r[3] - r[1]
            if w < 50 or h < 50 or w * h < page_area * 0.02 or w * h > page_area * 0.85:
                continue
            if any(_overlap(r, t) > 0.5 or _overlap(t, r) > 0.5 for t in table_rects):
                continue  # table borders
            inside = sum(1 for dr in drawings if _overlap(tuple(dr["rect"]), r) > 0.9)
            if inside < 3:
                continue
            clip = pymupdf.Rect(r) + (-6, -6, 6, 6)
            # include text labels that belong to the figure (axis labels, legend).
            # Longer text stays as Markdown text (see blocks()).
            for b in self.d.get("blocks", []):
                if b.get("type") == 0 and not _is_prose(b) and _overlap(b["bbox"], r) > 0.5:
                    clip |= pymupdf.Rect(b["bbox"])
            png = self.page.get_pixmap(clip=clip & self.page.rect, dpi=150).tobytes("png")
            rects.append(tuple(clip))
            items.append((r[1], r[0], [ImageBlock(
                data=png, mime="image/png", name_hint=f"page{self.n:03d}_figure{len(rects)}",
                location={"page": self.n, "bbox": tuple(clip), "kind": "vector"})]))
        return rects

    # -- text ---------------------------------------------------------------
    def lines(self, block: dict) -> list[_Line]:
        out = []
        h = self.page.rect.height
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if s["text"]]
            if not spans:
                continue
            text = "".join(s["text"] for s in spans)
            if not text.strip():
                continue
            y0, y1 = line["bbox"][1], line["bbox"][3]
            if (y1 < h * _MARGIN or y0 > h * (1 - _MARGIN)) and _norm(text) in self.repeated:
                continue
            runs = [Inline(s["text"], bold=bool(s["flags"] & _FLAG_BOLD) or "bold" in s["font"].lower(),
                           italic=bool(s["flags"] & _FLAG_ITALIC), code=False) for s in spans]
            size = max(s["size"] for s in spans)
            bold = all(r.bold for r in runs if r.text.strip())
            out.append(_Line(runs, round(size * 2) / 2, bold, line["bbox"][0], tuple(line["bbox"])))
        return out

    def toc_indents(self) -> list[float]:
        """Distinct left edges of the page's table-of-contents lines, left to right."""
        if not hasattr(self, "_toc_indents"):
            xs = sorted(ln["bbox"][0] for b in self.d.get("blocks", []) if b.get("type") == 0
                        for ln in b.get("lines", []) if _LEADER.search("".join(s["text"] for s in ln["spans"])))
            indents: list[float] = []
            for x in xs:
                if not indents or x - indents[-1] > 4:
                    indents.append(x)
            self._toc_indents = indents
            self._toc_prev = -1
        return self._toc_indents

    def toc_blocks(self, lines: list[_Line]) -> list[Block]:
        """Table-of-contents lines -> one list item per entry, nested by indent.

        An entry ends at the line with the leader dots and page number; a title
        that wraps over several lines is joined first.
        """
        out: list[Block] = []
        lst = ListBlock()
        pending: list[_Line] = []
        indents = self.toc_indents()
        for ln in lines:
            if not _TOC.search(ln.text):
                if ln.size > self.body + 0.5:  # a "Contents" title, not part of an entry
                    out.append(Heading(min(6, len(self.levels) + 1), ln.text.strip()))
                else:
                    pending.append(ln)
                continue
            entry = pending + [ln]
            pending = []
            title = " ".join(x.text.strip() for x in entry)
            m = _TOC.search(title)
            title = f"{title[:m.start()].strip()} … {m.group(1)}"
            x0 = entry[0].x0
            rank = min(range(len(indents)), key=lambda k: abs(indents[k] - x0)) if indents else 0
            # at most one level deeper than the entry above (Markdown can't skip levels)
            level = min(rank, self._toc_prev + 1, 3)
            self._toc_prev = level
            lst.items.append(ListItem(title, level))
        if lst.items:
            out.append(lst)
        if pending:
            out.append(Paragraph(_join(pending, self.column_right(pending[0].x0))))
        return out

    def headings(self, level: int, lines: list[_Line]) -> list[Block]:
        """Heading-sized lines: a wrapped title stays one heading, separate lines become separate headings."""
        out: list[Block] = []
        right = self.column_right(min(ln.x0 for ln in lines))
        group: list[_Line] = []
        for i, ln in enumerate(lines):
            group.append(ln)
            if i == len(lines) - 1 or _hard_break(ln, lines[i + 1], max(right, ln.bbox[2])):
                out.append(Heading(level, " ".join(x.text.strip() for x in group)))
                group = []
        return out

    def text_blocks(self, lines: list[_Line]) -> list[Block]:
        lines = _same_row(lines)
        if any(_TOC.search(ln.text) for ln in lines):
            return self.toc_blocks(lines)
        text = " ".join(ln.text.strip() for ln in lines)
        level = self.levels.get(max(ln.size for ln in lines))
        if level and len(text) < 200:
            return self.headings(level, lines)
        if len(lines) == 1 and lines[0].bold and len(text) < 80 and abs(lines[0].size - self.body) < 1 \
                and not text.rstrip().endswith((".", ":", ",")) and not _BULLET.match(text):
            return [Heading(min(6, len(self.levels) + 1), text.strip())]

        out: list[Block] = []
        para: list[_Line] = []
        item: list[_Line] = []
        item_x = 0.0
        lst: ListBlock | None = None

        def flush_para():
            nonlocal para
            if para:
                out.append(Paragraph(_join(para, self.column_right(min(ln.x0 for ln in para)))))
                para = []

        def flush_item():
            nonlocal item, lst
            if item:
                first = item[0]
                m = _BULLET.match(first.text)
                marker = m.group(1) if m else ""
                runs = _join(item, self.column_right(item[0].x0))
                runs = _strip_prefix(runs, len(m.group(0)) if m else 0)
                if lst is None:
                    lst = ListBlock()
                    out.append(lst)
                lst.items.append(ListItem(runs, 0, bool(re.match(r"\(?[\da-z]", marker))))
                item = []

        for ln in lines:
            if _BULLET.match(ln.text):
                flush_para()
                flush_item()
                item = [ln]
                item_x = ln.x0
            elif item and ln.x0 > item_x + 2:
                item.append(ln)
            else:
                flush_item()
                lst = None
                para.append(ln)
        flush_item()
        flush_para()
        return out


def _same_row(lines: list[_Line]) -> list[_Line]:
    """Join pieces PyMuPDF reports as separate lines but that sit on one row
    (e.g. "1." | "Introduction", or "Purpose ……" | "2")."""
    out: list[_Line] = []
    for ln in lines:
        prev = out[-1] if out else None
        if prev is not None and ln.bbox[0] >= prev.bbox[2] - 1 \
                and prev.bbox[1] <= (ln.bbox[1] + ln.bbox[3]) / 2 <= prev.bbox[3]:
            runs = prev.runs + [Inline(" ")] + ln.runs
            bbox = (prev.bbox[0], min(prev.bbox[1], ln.bbox[1]), ln.bbox[2], max(prev.bbox[3], ln.bbox[3]))
            out[-1] = _Line(runs, max(prev.size, ln.size), prev.bold and ln.bold, prev.x0, bbox)
        else:
            out.append(ln)
    return out


def _hard_break(line: _Line, nxt: _Line, right: float) -> bool:
    """True if the line was ended on purpose: the next line's first word would have fit after it.

    Text that wraps only moves a word down when it doesn't fit, so a short line
    followed by a short word is a real line break (addresses, signatures, verse).
    """
    text = line.text.rstrip()
    words = nxt.text.split()
    if not text or not words:
        return False
    if nxt.bbox[1] < (line.bbox[1] + line.bbox[3]) / 2:
        return False  # same row (e.g. "1." and the heading text after a tab)
    width = line.bbox[2] - line.bbox[0]
    char_w = width / max(len(text), 1)
    return right - line.bbox[2] > (len(words[0]) + 1.5) * char_w


def _join(lines: list[_Line], right: float = 0.0) -> list[Inline]:
    """Join a block's lines: wrapped lines with a space, deliberate breaks with a line break."""
    right = max([right] + [ln.bbox[2] for ln in lines])
    runs: list[Inline] = []
    for i, ln in enumerate(lines):
        line_runs = list(ln.runs)
        if i < len(lines) - 1:
            last = line_runs[-1]
            t = last.text.rstrip()
            nxt = lines[i + 1].text.lstrip()
            if t.endswith("-") and not t.endswith(" -") and nxt[:1].islower():
                line_runs[-1] = Inline(t[:-1], last.bold, last.italic)  # re-join hyphenated word
            else:
                line_runs[-1] = Inline(t, last.bold, last.italic)
                line_runs.append(Inline("\n" if _hard_break(ln, lines[i + 1], right) else " "))
        runs.extend(line_runs)
    if runs:
        runs[0] = Inline(runs[0].text.lstrip(), runs[0].bold, runs[0].italic)
        runs[-1] = Inline(runs[-1].text.rstrip(), runs[-1].bold, runs[-1].italic)
    return runs


def _strip_prefix(runs: list[Inline], n: int) -> list[Inline]:
    out = []
    for r in runs:
        if n <= 0:
            out.append(r)
            continue
        if len(r.text) <= n:
            n -= len(r.text)
            continue
        out.append(Inline(r.text[n:], r.bold, r.italic))
        n = 0
    return out


def _fill_merged(rows: list[list], merged: str) -> list[list[Text]]:
    """PyMuPDF marks cells covered by a merge as None: fill them from the left, else from above."""
    grid: list[list[Text]] = []
    for r, row in enumerate(rows):
        out_row: list[Text] = []
        for c, v in enumerate(row):
            if v is None:
                if merged == "repeat":
                    if c > 0:
                        v = out_row[c - 1]
                    elif r > 0 and c < len(grid[r - 1]):
                        v = grid[r - 1][c]
                v = v or ""
            out_row.append(fmt_value(v).strip() if not isinstance(v, list) else v)
        grid.append(out_row)
    return grid


_RULE_MIN = 3.0  # pt: shorter strokes are glyph-like marks, not table rulings
_THIN = 2.0  # pt: a rectangle thinner than this is a drawn line
_TOL = 3.0  # pt: snap/join tolerance, same as PyMuPDF's table finder default
_MAX_PAIRS = 40_000  # beyond this many edge pairs, skip the check and run the finder


def _edges(drawings: list[dict], page_area: float) -> tuple[list[tuple], list[tuple]]:
    """Horizontal (y, x0, x1) and vertical (x, y0, y1) edges, as the table finder sees them."""
    hs: list[tuple] = []
    vs: list[tuple] = []
    big = page_area * 0.8 if page_area else float("inf")
    for d in drawings:
        for item in d.get("items", ()):
            kind = item[0]
            if kind == "l":
                p, q = item[1], item[2]
                if abs(q.y - p.y) < 1 and abs(q.x - p.x) >= _RULE_MIN:
                    hs.append((p.y, min(p.x, q.x), max(p.x, q.x)))
                elif abs(q.x - p.x) < 1 and abs(q.y - p.y) >= _RULE_MIN:
                    vs.append((p.x, min(p.y, q.y), max(p.y, q.y)))
            elif kind in ("re", "qu"):
                r = item[1].rect if kind == "qu" else item[1]
                x0, y0, x1, y1 = min(r.x0, r.x1), min(r.y0, r.y1), max(r.x0, r.x1), max(r.y0, r.y1)
                w, h = x1 - x0, y1 - y0
                if w * h >= big:
                    continue  # page background
                if h <= _THIN and w >= _RULE_MIN:
                    hs.append(((y0 + y1) / 2, x0, x1))
                elif w <= _THIN and h >= _RULE_MIN:
                    vs.append(((x0 + x1) / 2, y0, y1))
                elif w >= _RULE_MIN and h >= _RULE_MIN:
                    hs += [(y0, x0, x1), (y1, x0, x1)]
                    vs += [(x0, y0, y1), (x1, y0, y1)]
    return _join_edges(hs), _join_edges(vs)


def _join_edges(edges: list[tuple]) -> list[tuple]:
    """Merge collinear segments that touch (cell borders are often drawn piece by piece)."""
    out: list[list[float]] = []
    for pos, a, b in sorted(edges):
        last = out[-1] if out else None
        if last is not None and abs(pos - last[0]) <= _TOL and a <= last[2] + _TOL:
            last[2] = max(last[2], b)
        else:
            out.append([pos, a, b])
    return [tuple(e) for e in out]


def _crossing(across: list[tuple], lo: tuple, hi: tuple) -> int:
    """How many edges run from edge ``lo`` to edge ``hi`` (both perpendicular to them)."""
    start, end = lo[0] + _TOL, hi[0] - _TOL
    left, right = max(lo[1], hi[1]) - _TOL, min(lo[2], hi[2]) + _TOL
    return sum(1 for pos, a, b in across if a <= start and b >= end and left <= pos <= right)


def has_rulings(drawings: list[dict], page_area: float = 0.0) -> bool:
    """True if the page's lines could form a table of at least two cells.

    PyMuPDF's table finder (default "lines" strategy) builds cells from drawn
    lines and rectangles, and we drop tables smaller than 2 cells anyway. Two
    cells need two parallel edges crossed by three edges, in either direction.
    A frame around the page or a pair of chart axes doesn't qualify, so those
    pages skip the (slow) finder.
    """
    hs, vs = _edges(drawings, page_area)
    if len(hs) < 2 or len(vs) < 2 or (len(hs) < 3 and len(vs) < 3):
        return False
    if (len(hs) ** 2) * len(vs) + (len(vs) ** 2) * len(hs) > _MAX_PAIRS * 50:
        return True  # very busy page: let the finder decide
    for group, across in ((hs, vs), (vs, hs)):
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                if group[j][0] - group[i][0] <= _TOL:
                    continue
                if _crossing(across, group[i], group[j]) >= 3:
                    return True
    return False


_SENTENCE_END = (".", "!", "?", ":", ";", "”", '"', ")")


def _merge_paragraphs(blocks: list[Block]) -> list[Block]:
    """Join a paragraph that PyMuPDF split into two text blocks.

    The second part must start in lowercase and the first must not end a sentence.
    """
    out: list[Block] = []
    for b in blocks:
        if isinstance(b, Paragraph) and out and isinstance(out[-1], Paragraph):
            prev, cur = plain(out[-1].text).rstrip(), plain(b.text).lstrip()
            if prev and cur[:1].islower() and not prev.endswith(_SENTENCE_END):
                a = out[-1].text if isinstance(out[-1].text, list) else [Inline(out[-1].text)]
                c = b.text if isinstance(b.text, list) else [Inline(b.text)]
                out[-1] = Paragraph(a + [Inline(" ")] + c)
                continue
        out.append(b)
    return out


def _merge_lists(blocks: list[Block]) -> list[Block]:
    """Join list blocks that were split across text blocks."""
    out: list[Block] = []
    for b in blocks:
        if isinstance(b, ListBlock) and out and isinstance(out[-1], ListBlock):
            out[-1].items.extend(b.items)
        else:
            out.append(b)
    return out
