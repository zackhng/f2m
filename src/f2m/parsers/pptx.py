"""PPTX -> Document.

One section per slide. Shapes are read top-to-bottom, left-to-right. Native
tables, charts, SmartArt and embedded Office files are converted with fixed
rules; only pictures go to the image handler.
"""

from __future__ import annotations

import io
import logging

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER

from f2m.common.charts import chart_blocks, parse_chart
from f2m.common.ooxml import NS, R_EMBED, R_ID, URI_DIAGRAM, diagram_texts, local
from f2m.common.tables import Cell, build_grid, make_table
from f2m.context import ParseContext
from f2m.detect import detect, mime_for
from f2m.document import (
    Block,
    Document,
    EmbeddedDocument,
    Heading,
    ImageBlock,
    Inline,
    ListBlock,
    ListItem,
    Paragraph,
    Quote,
)
from f2m.registry import Parser, register
from f2m.timing import timed

log = logging.getLogger(__name__)
A = f"{{{NS['a']}}}"
_ROW_TOLERANCE = 228600  # 0.25 inch in EMU: shapes this close vertically count as one row
_BODY_PLACEHOLDERS = {PP_PLACEHOLDER.BODY, PP_PLACEHOLDER.OBJECT, PP_PLACEHOLDER.VERTICAL_BODY,
                      PP_PLACEHOLDER.VERTICAL_OBJECT}
_TITLE_PLACEHOLDERS = {PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE, PP_PLACEHOLDER.VERTICAL_TITLE}
_PROG_EXT = {"excel": ".xlsx", "word": ".docx", "powerpoint": ".pptx", "acrobat": ".pdf"}


@register
class PptxParser(Parser):
    formats = ("pptx",)
    extensions = (".pptx", ".pptm", ".potx", ".ppsx")

    def parse(self, data: bytes, ctx: ParseContext) -> Document:
        with timed("load"):
            prs = Presentation(io.BytesIO(data))
        doc = Document(format="pptx")
        title = (prs.core_properties.title or "").strip() or ctx.stem
        doc.metadata.update(title=title, slides=len(prs.slides))
        if not ctx.embedded:
            doc.add(Heading(1, title))
        for n, slide in enumerate(prs.slides, 1):
            try:
                with timed("slides"):
                    doc.extend(_SlideWriter(slide, n, ctx, doc).blocks())
            except Exception as e:
                log.debug("slide failed", exc_info=True)
                doc.warn(f"slide {n} could not be fully converted: {e}")
        return doc


class _SlideWriter:
    def __init__(self, slide, number: int, ctx: ParseContext, doc: Document):
        self.slide = slide
        self.n = number
        self.ctx = ctx
        self.doc = doc
        self.n_img = 0

    def blocks(self) -> list[Block]:
        with timed("title"):
            title_shape = None
            for sh in self.slide.shapes:
                if sh.is_placeholder and sh.placeholder_format.type in _TITLE_PLACEHOLDERS:
                    title_shape = sh
                    break
            title = ""
            if title_shape is not None and title_shape.has_text_frame:
                title = title_shape.text_frame.text.strip().replace("\n", " ").replace("\x0b", " ")
        hidden = self.slide._element.get("show") == "0"
        head = f"Slide {self.n}" + (f": {title}" if title else "") + (" (hidden)" if hidden else "")
        out: list[Block] = [Heading(2, head)]
        with timed("order_shapes"):
            shapes = _ordered(self.slide.shapes)
        for sh in shapes:
            if title_shape is not None and sh.shape_id == title_shape.shape_id:
                continue  # python-pptx makes new proxy objects, so compare ids
            try:
                with timed(_kind(sh)):
                    out.extend(self.shape(sh))
            except Exception as e:
                log.debug("shape failed", exc_info=True)
                self.doc.warn(f"slide {self.n}: shape {sh.name!r} could not be converted: {e}")
        with timed("notes"):
            if self.ctx.options.pptx_notes and self.slide.has_notes_slide:
                tf = self.slide.notes_slide.notes_text_frame
                notes = tf.text.strip() if tf is not None else ""
                if notes:
                    out.append(Quote(notes, label="Notes"))
        return out

    # -- shapes ---------------------------------------------------------------
    def shape(self, sh) -> list[Block]:
        st = sh.shape_type
        if st == MSO_SHAPE_TYPE.GROUP:
            out: list[Block] = []
            for child in _ordered(sh.shapes):
                out.extend(self.shape(child))
            return out
        if getattr(sh, "has_table", False) and sh.has_table:
            return self.table(sh.table)
        if getattr(sh, "has_chart", False) and sh.has_chart:
            return chart_blocks(parse_chart(sh.chart._chartSpace))
        if st in (MSO_SHAPE_TYPE.EMBEDDED_OLE_OBJECT, MSO_SHAPE_TYPE.LINKED_OLE_OBJECT):
            return self.ole(sh)
        if st == MSO_SHAPE_TYPE.MEDIA:
            return []
        el = sh._element
        gd = el.find(f".//{A}graphicData")
        if gd is not None and gd.get("uri") == URI_DIAGRAM:
            return self.smartart(gd)
        out = []
        image = _shape_image(sh)
        if image is not None:
            out.append(self._image(image[0], image[1], _alt(sh)))
        if sh.has_text_frame and sh.text_frame.text.strip():
            body = sh.is_placeholder and sh.placeholder_format.type in _BODY_PLACEHOLDERS
            out.extend(self.text_frame(sh.text_frame, body))
        return out

    def _image(self, data: bytes, mime: str, alt: str) -> ImageBlock:
        self.n_img += 1
        return ImageBlock(data=data, mime=mime or mime_for(data, "image/png"),
                          name_hint=f"slide{self.n:03d}_img{self.n_img}", alt=alt,
                          location={"slide": self.n})

    def ole(self, sh) -> list[Block]:
        fmt = sh.ole_format
        prog = (fmt.prog_id or "").lower()
        blob = None
        try:
            blob = fmt.blob
        except Exception:
            pass
        if blob:
            ext = next((e for k, e in _PROG_EXT.items() if k in prog), "")
            name = f"slide{self.n:03d}_{sh.name}{ext}".replace(" ", "_")
            if detect(blob, name) in ("xlsx", "docx", "pptx", "pdf"):
                nested = self.ctx.convert_embedded(blob, name)
                if nested is not None:
                    return [EmbeddedDocument(nested, title=f"{sh.name} ({fmt.prog_id})")]
        # Old binary OLE object, a linked object, or depth limit: use the preview picture.
        for blip in sh._element.iter(f"{A}blip"):
            rid = blip.get(R_EMBED)
            try:
                part = self.slide.part.related_part(rid)
            except (KeyError, AttributeError):
                continue
            return [self._image(part.blob, getattr(part, "content_type", ""), f"{sh.name} ({fmt.prog_id})")]
        return [Paragraph([Inline(f"[embedded object: {fmt.prog_id or sh.name}]", italic=True)])]

    def smartart(self, gd) -> list[Block]:
        rel_ids = gd.find(f"{{{NS['dgm']}}}relIds")
        rid = rel_ids.get(f"{{{NS['r']}}}dm") if rel_ids is not None else None
        if not rid:
            return []
        try:
            texts = diagram_texts(self.slide.part.related_part(rid).blob)
        except Exception:
            return []
        return [ListBlock([ListItem(t.replace("\n", " ")) for t in texts])] if texts else []

    # -- text -----------------------------------------------------------------
    def text_frame(self, tf, body_placeholder: bool) -> list[Block]:
        out: list[Block] = []
        current: ListBlock | None = None
        for p in tf.paragraphs:
            runs = self.runs(p._p)
            if not any(r.text.strip() for r in runs):
                continue
            ppr = p._p.find(f"{A}pPr")
            bullet = None  # None = not stated
            ordered = False
            if ppr is not None:
                if ppr.find(f"{A}buNone") is not None:
                    bullet = False
                elif ppr.find(f"{A}buAutoNum") is not None:
                    bullet, ordered = True, True
                elif ppr.find(f"{A}buChar") is not None or ppr.find(f"{A}buBlip") is not None:
                    bullet = True
            is_list = bullet if bullet is not None else (body_placeholder or p.level > 0)
            if is_list:
                if current is None:
                    current = ListBlock()
                    out.append(current)
                current.items.append(ListItem(_trim(runs), p.level, ordered))
            else:
                current = None
                out.append(Paragraph(_trim(runs)))
        return out

    def runs(self, p_el) -> list[Inline]:
        out: list[Inline] = []
        for child in p_el:
            name = local(child.tag)
            if name in ("r", "fld"):
                rpr = child.find(f"{A}rPr")
                bold = italic = strike = False
                link = None
                if rpr is not None:
                    bold = rpr.get("b") in ("1", "true")
                    italic = rpr.get("i") in ("1", "true")
                    strike = rpr.get("strike") not in (None, "noStrike")
                    h = rpr.find(f"{A}hlinkClick")
                    if h is not None and h.get(R_ID):
                        try:
                            link = self.slide.part.rels[h.get(R_ID)].target_ref
                        except KeyError:
                            pass
                t = child.find(f"{A}t")
                text = (t.text or "") if t is not None else ""
                out.append(Inline(text.replace("\x0b", "\n"), bold, italic, strike, False, link))
            elif name == "br":
                out.append(Inline("\n"))
        return out

    # -- tables ---------------------------------------------------------------
    def table(self, tbl) -> list[Block]:
        rows: list[list[Cell]] = []
        for r in tbl.rows:
            row = []
            for c in r.cells:
                if c.is_spanned:
                    continue  # covered by a merge origin; build_grid fills it
                runs: list[Inline] = []
                for p in c.text_frame.paragraphs:
                    pr = self.runs(p._p)
                    if any(x.text.strip() for x in pr):
                        if runs:
                            runs.append(Inline("\n"))
                        runs.extend(pr)
                span_w = c.span_width if c.is_merge_origin else 1
                span_h = c.span_height if c.is_merge_origin else 1
                row.append(Cell(_trim(runs) if runs else "", span_w, span_h))
            rows.append(row)
        t = make_table(build_grid(rows, self.ctx.options.merged_cells))
        return [t] if t else []


def _kind(sh) -> str:
    """Short shape category, used to name timing steps."""
    try:
        if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
            return "groups"
        if getattr(sh, "has_table", False) and sh.has_table:
            return "tables"
        if getattr(sh, "has_chart", False) and sh.has_chart:
            return "charts"
        if sh.shape_type in (MSO_SHAPE_TYPE.EMBEDDED_OLE_OBJECT, MSO_SHAPE_TYPE.LINKED_OLE_OBJECT):
            return "ole_objects"
        if sh.shape_type == MSO_SHAPE_TYPE.PICTURE or local(sh._element.tag) == "pic":
            return "pictures"
    except Exception:
        pass
    return "text_shapes"


def _ordered(shapes) -> list:
    def key(sh):
        top = sh.top if sh.top is not None else 0
        left = sh.left if sh.left is not None else 0
        return (top // _ROW_TOLERANCE, left)

    return sorted(shapes, key=key)


def _shape_image(sh) -> tuple[bytes, str] | None:
    if sh.shape_type == MSO_SHAPE_TYPE.PICTURE or local(sh._element.tag) == "pic":
        try:
            return sh.image.blob, sh.image.content_type
        except Exception:
            pass
    return None


def _alt(sh) -> str:
    for node in sh._element.iter():
        if local(node.tag) == "cNvPr":
            return node.get("descr") or node.get("title") or ""
    return ""


def _trim(runs: list[Inline]) -> list[Inline]:
    runs = [r for r in runs if r.text]
    if runs:
        f = runs[0]
        runs[0] = Inline(f.text.lstrip(), f.bold, f.italic, f.strike, f.code, f.link)
        last = runs[-1]
        runs[-1] = Inline(last.text.rstrip(), last.bold, last.italic, last.strike, last.code, last.link)
    return [r for r in runs if r.text]
