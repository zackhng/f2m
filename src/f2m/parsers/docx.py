"""DOCX -> Document.

Walks the body XML in order. Native tables are converted with fixed rules
(gridSpan / vMerge merges, nested tables). Embedded Excel/Word objects are
parsed by their own parser, charts are read from their XML, and only real
pictures go to the image handler.
"""

from __future__ import annotations

import io
import logging
import posixpath
import re

import docx as pydocx
from lxml import etree

from f2m.common.charts import chart_blocks, parse_chart
from f2m.common.ooxml import NS, R_EMBED, R_ID, R_LINK, diagram_texts, local
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
    RawMarkdown,
)
from f2m.registry import Parser, register
from f2m.timing import timed

log = logging.getLogger(__name__)

W = f"{{{NS['w']}}}"
WVAL = f"{W}val"
_HEADING_STYLE = re.compile(r"^heading\s*(\d)$", re.I)
_ROWS = etree.XPath("./w:tr | ./w:sdt/w:sdtContent/w:tr | ./w:customXml/w:tr", namespaces=NS)
_CELLS = etree.XPath("./w:tc | ./w:sdt/w:sdtContent/w:tc | ./w:customXml/w:tc", namespaces=NS)


def _on(el) -> bool:
    """Toggle property like <w:b/> or <w:b w:val="0"/>."""
    return el is not None and el.get(WVAL, "true").lower() not in ("0", "false", "off", "none")


@register
class DocxParser(Parser):
    formats = ("docx",)
    extensions = (".docx", ".docm", ".dotx")

    def parse(self, data: bytes, ctx: ParseContext) -> Document:
        with timed("load"):
            d = pydocx.Document(io.BytesIO(data))
        doc = Document(format="docx")
        title = (d.core_properties.title or "").strip()
        if title:
            doc.metadata["title"] = title
        with timed("styles_numbering"):
            walker = _Walker(d, ctx, doc)
        with timed("body"):
            doc.extend(walker.body(d.element.body))
        doc.extend(walker.footnote_blocks())
        return doc


class _Walker:
    def __init__(self, d, ctx: ParseContext, doc: Document):
        self.d = d
        self.part = d.part
        self.ctx = ctx
        self.doc = doc
        self.n_img = 0
        self.n_obj = 0
        self.styles = self._styles()
        self.numbering = self._numbering()
        self.footnotes = self._footnotes()
        self.used_footnotes: list[str] = []

    # -- setup --------------------------------------------------------------
    def _styles(self) -> dict[str, tuple[str, object]]:
        """style id -> (name, pPr element)."""
        out = {}
        try:
            root = self.d.styles.element
        except Exception:
            return out
        for s in root.findall(f"{W}style"):
            sid = s.get(f"{W}styleId", "")
            name_el = s.find(f"{W}name")
            out[sid] = (name_el.get(WVAL, sid) if name_el is not None else sid, s.find(f"{W}pPr"))
        return out

    def _numbering(self) -> dict[tuple[str, int], bool]:
        """(numId, level) -> ordered?"""
        out: dict[tuple[str, int], bool] = {}
        try:
            root = self.part.numbering_part.element
        except Exception:
            return out
        abstract: dict[str, dict[int, bool]] = {}
        for a in root.findall(f"{W}abstractNum"):
            levels = {}
            for lvl in a.findall(f"{W}lvl"):
                fmt = lvl.find(f"{W}numFmt")
                levels[int(lvl.get(f"{W}ilvl", "0"))] = (fmt is not None and fmt.get(WVAL) not in ("bullet", "none"))
            abstract[a.get(f"{W}abstractNumId", "")] = levels
        for num in root.findall(f"{W}num"):
            aid = num.find(f"{W}abstractNumId")
            levels = abstract.get(aid.get(WVAL, "") if aid is not None else "", {})
            for lvl, ordered in levels.items():
                out[(num.get(f"{W}numId", ""), lvl)] = ordered
        return out

    def _footnotes(self) -> dict[str, list[Inline]]:
        out: dict[str, list[Inline]] = {}
        for rel in self.part.rels.values():
            if rel.is_external or not rel.reltype.endswith("/footnotes"):
                continue
            try:
                root = etree.fromstring(rel.target_part.blob)
            except Exception:
                continue
            for fn in root.findall(f"{W}footnote"):
                if fn.get(f"{W}type") in ("separator", "continuationSeparator", "continuationNotice"):
                    continue
                runs: list[Inline] = []
                for p in fn.findall(f"{W}p"):
                    items = self.inline(p)
                    runs += [i for i in items if isinstance(i, Inline)] + [Inline(" ")]
                out[fn.get(f"{W}id", "")] = runs
        return out

    def footnote_blocks(self) -> list[Block]:
        out: list[Block] = []
        for n, fid in enumerate(self.used_footnotes, 1):
            text = "".join(r.text for r in self.footnotes.get(fid, [])).strip()
            out.append(RawMarkdown(f"[^{n}]: {text}"))
        return out

    # -- body -----------------------------------------------------------------
    def body(self, container) -> list[Block]:
        out: list[Block] = []
        current_list: ListBlock | None = None
        for el in container:
            name = local(el.tag)
            if name in ("sdt", "customXml"):
                content = el.find(f"{W}sdtContent") if name == "sdt" else el
                if content is not None:
                    out.extend(self.body(content))
                    current_list = None
                continue
            if name == "p":
                blocks, item = self.paragraph(el)
                if item is not None:
                    if current_list is None:
                        current_list = ListBlock()
                        out.append(current_list)
                    current_list.items.append(item)
                else:
                    if blocks:
                        current_list = None
                out.extend(blocks)
            elif name == "tbl":
                current_list = None
                try:
                    with timed("tables"):
                        out.extend(self.table(el))
                except Exception as e:
                    log.debug("table failed", exc_info=True)
                    self.doc.warn(f"a table could not be converted: {e}")
        return out

    # -- paragraphs -----------------------------------------------------------
    def _para_props(self, p) -> tuple[str, int | None, tuple[str, int] | None]:
        """(style name, heading level, (numId, ilvl) if a list paragraph)."""
        ppr = p.find(f"{W}pPr")
        sid = ""
        if ppr is not None and ppr.find(f"{W}pStyle") is not None:
            sid = ppr.find(f"{W}pStyle").get(WVAL, "")
        style_name, style_ppr = self.styles.get(sid, (sid, None))

        level = None
        m = _HEADING_STYLE.match(style_name or "")
        if m:
            level = int(m.group(1))
        elif (style_name or "").lower() == "title":
            level = 1
        else:
            for src in (ppr, style_ppr):
                ol = src.find(f"{W}outlineLvl") if src is not None else None
                if ol is not None and ol.get(WVAL, "9").isdigit() and int(ol.get(WVAL)) < 9:
                    level = int(ol.get(WVAL)) + 1
                    break

        num = None
        for src in (ppr, style_ppr):
            numpr = src.find(f"{W}numPr") if src is not None else None
            if numpr is not None:
                nid = numpr.find(f"{W}numId")
                ilvl = numpr.find(f"{W}ilvl")
                num_id = nid.get(WVAL, "0") if nid is not None else "0"
                if num_id != "0":
                    num = (num_id, int(ilvl.get(WVAL, "0")) if ilvl is not None else 0)
                break
        if num is None and (style_name or "").lower().startswith(("list bullet", "list number")):
            lvl = re.search(r"(\d)$", style_name)
            num = ("style:" + style_name.lower(), int(lvl.group(1)) - 1 if lvl else 0)
        return style_name, level, num

    def paragraph(self, p) -> tuple[list[Block], ListItem | None]:
        style_name, level, num = self._para_props(p)
        items = self.inline(p)
        runs = [i for i in items if isinstance(i, Inline)]
        extra = [i for i in items if isinstance(i, Block)]
        has_text = any(r.text.strip() for r in runs)

        if level is not None and has_text:
            text = "".join(r.text for r in runs).replace("\n", " ").strip()
            return [Heading(min(level, 6), text)] + extra, None
        if num is not None and has_text and level is None:
            num_id, ilvl = num
            ordered = self.numbering.get((num_id, ilvl), "number" in num_id)
            return extra, ListItem(_strip(runs), ilvl, ordered)
        blocks: list[Block] = []
        if has_text:
            if (style_name or "").lower() in ("quote", "intense quote"):
                from f2m.document import Quote

                blocks.append(Quote(_strip(runs)))
            else:
                blocks.append(Paragraph(_strip(runs)))
        return blocks + extra, None

    def inline(self, el, link: str | None = None) -> list:
        """Inline runs and any block objects (images, charts, embedded docs) in a paragraph."""
        out: list = []
        for child in el:
            name = local(child.tag)
            if name == "r":
                out.extend(self.run(child, link))
            elif name == "hyperlink":
                target = link
                rid = child.get(R_ID)
                if rid and rid in self.part.rels:
                    rel = self.part.rels[rid]
                    target = rel.target_ref if rel.is_external else target
                elif child.get(f"{W}anchor"):
                    target = "#" + child.get(f"{W}anchor")
                out.extend(self.inline(child, target))
            elif name in ("ins", "smartTag", "customXml", "fldSimple", "moveTo", "dir", "bdo"):
                out.extend(self.inline(child, link))
            elif name == "sdt":
                content = child.find(f"{W}sdtContent")
                if content is not None:
                    out.extend(self.inline(content, link))
            elif name in ("oMath", "oMathPara"):
                text = "".join(t.text or "" for t in child.iter() if local(t.tag) == "t")
                if text:
                    out.append(Inline(text, code=True))
            elif name == "AlternateContent":
                choice = next(iter(child), None)
                if choice is not None:
                    out.extend(self.inline(choice, link))
        return out

    def run(self, r, link: str | None) -> list:
        rpr = r.find(f"{W}rPr")
        bold = italic = strike = False
        if rpr is not None:
            bold = _on(rpr.find(f"{W}b"))
            italic = _on(rpr.find(f"{W}i"))
            strike = _on(rpr.find(f"{W}strike")) or _on(rpr.find(f"{W}dstrike"))
            if _on(rpr.find(f"{W}vanish")):
                return []

        def text(t: str) -> Inline:
            return Inline(t, bold, italic, strike, False, link)

        out: list = []
        for child in r:
            name = local(child.tag)
            if name == "t":
                out.append(text(child.text or ""))
            elif name == "tab":
                out.append(text("\t"))
            elif name in ("br", "cr"):
                if child.get(f"{W}type") != "page":
                    out.append(Inline("\n"))
            elif name == "noBreakHyphen":
                out.append(text("-"))
            elif name == "sym":
                ch = child.get(f"{W}char")
                if ch:
                    try:
                        out.append(text(chr(int(ch, 16) & 0xFFFF if int(ch, 16) < 0xF000 else int(ch, 16) - 0xF000)))
                    except ValueError:
                        pass
            elif name == "footnoteReference":
                fid = child.get(f"{W}id", "")
                if fid in self.footnotes:
                    self.used_footnotes.append(fid)
                    out.append(Inline(f"[^{len(self.used_footnotes)}]", raw=True))
            elif name in ("drawing", "pict"):
                out.extend(self.graphic(child))
            elif name == "object":
                out.extend(self.ole_object(child))
            elif name == "AlternateContent":
                out.extend(self.graphic(child))
        return out

    # -- graphics -------------------------------------------------------------
    def _part(self, rid: str):
        rel = self.part.rels.get(rid)
        if rel is None or rel.is_external:
            return None
        return rel.target_part

    def graphic(self, el) -> list[Block]:
        """Pictures, charts, SmartArt and text boxes inside a w:drawing or w:pict."""
        out: list[Block] = []
        alt = ""
        for node in el.iter():
            if local(node.tag) == "docPr":
                alt = node.get("descr") or node.get("title") or ""
                break

        def visit(node) -> None:
            name = local(node.tag)
            if name == "Fallback":  # duplicate of the mc:Choice content
                return
            if name == "txbxContent":  # text box: its own paragraphs/tables
                out.extend(self.body(node))
                return
            if name == "blip":
                rid = node.get(R_EMBED) or node.get(R_LINK)
                part = self._part(rid) if rid else None
                if part is not None:
                    out.append(self._image(part.blob, alt))
            elif name == "imagedata":
                rid = node.get(R_ID) or node.get(f"{{{NS['r']}}}pict")
                part = self._part(rid) if rid else None
                if part is not None:
                    out.append(self._image(part.blob, alt or node.get(f"{{{NS['o']}}}title", "")))
            elif name == "chart" and node.get(R_ID):
                part = self._part(node.get(R_ID))
                if part is not None:
                    try:
                        out.extend(chart_blocks(parse_chart(part.blob)))
                    except Exception as e:
                        self.doc.warn(f"chart could not be read: {e}")
            elif name == "relIds" and node.get(f"{{{NS['r']}}}dm"):
                part = self._part(node.get(f"{{{NS['r']}}}dm"))
                if part is not None:
                    texts = diagram_texts(part.blob)
                    if texts:
                        out.append(ListBlock([ListItem(t.replace("\n", " ")) for t in texts]))
            for child in node:
                if isinstance(child.tag, str):
                    visit(child)

        visit(el)
        return out

    def _image(self, data: bytes, alt: str) -> ImageBlock:
        self.n_img += 1
        return ImageBlock(data=data, mime=mime_for(data, "image/png"), name_hint=f"image{self.n_img:03d}",
                          alt=alt, location={"index": self.n_img})

    def ole_object(self, obj) -> list[Block]:
        """Embedded OLE object: parse the package if we can, else use its preview image."""
        ole = None
        for node in obj.iter():
            if local(node.tag) == "OLEObject":
                ole = node
                break
        if ole is not None and ole.get(R_ID):
            part = self._part(ole.get(R_ID))
            if part is not None:
                fname = posixpath.basename(str(part.partname))
                if detect(part.blob, fname) in ("xlsx", "docx", "pptx", "pdf"):
                    self.n_obj += 1
                    nested = self.ctx.convert_embedded(part.blob, fname)
                    if nested is not None:
                        title = ole.get("ProgID", "") or fname
                        return [EmbeddedDocument(nested, title=f"{fname} ({title})")]
        # Legacy .bin objects (or depth limit reached): fall back to the preview picture.
        return self.graphic(obj)

    # -- tables ---------------------------------------------------------------
    def table(self, tbl) -> list[Block]:
        rows: list[list[Cell]] = []
        after: list[Block] = []
        nested_n = 0
        for r_i, tr in enumerate(_ROWS(tbl)):
            row: list[Cell] = []
            trpr = tr.find(f"{W}trPr")
            if trpr is not None and trpr.find(f"{W}gridBefore") is not None:
                row += [Cell("") for _ in range(int(trpr.find(f"{W}gridBefore").get(WVAL, "0")))]
            for c_i, tc in enumerate(_CELLS(tr)):
                tcpr = tc.find(f"{W}tcPr")
                span, cont = 1, False
                if tcpr is not None:
                    gs = tcpr.find(f"{W}gridSpan")
                    if gs is not None:
                        span = int(gs.get(WVAL, "1"))
                    vm = tcpr.find(f"{W}vMerge")
                    if vm is not None and vm.get(WVAL, "continue") == "continue":
                        cont = True
                runs: list[Inline] = []
                for child in tc:
                    name = local(child.tag)
                    if name == "p":
                        _, _, num = self._para_props(child)
                        items = self.inline(child)
                        texts = [i for i in items if isinstance(i, Inline)]
                        if any(t.text.strip() for t in texts):
                            if runs:
                                runs.append(Inline("\n"))
                            if num is not None:
                                runs.append(Inline("• "))
                            runs.extend(texts)
                        after.extend(i for i in items if isinstance(i, Block))
                    elif name == "tbl":
                        nested_n += 1
                        nested = self.table(child)
                        if nested and hasattr(nested[0], "caption"):
                            nested[0].caption = f"Nested table {nested_n} (in row {r_i + 1}, column {c_i + 1})"
                        after.extend(nested)
                        if runs:
                            runs.append(Inline("\n"))
                        runs.append(Inline(f"(see nested table {nested_n})"))
                row.append(Cell(_strip(runs) if runs else "", span, 1, cont))
            rows.append(row)
        grid = build_grid(rows, self.ctx.options.merged_cells)
        t = make_table(grid)
        return ([t] if t else []) + after


def _strip(runs: list[Inline]) -> list[Inline]:
    """Trim whitespace at both ends of a run list."""
    runs = [r for r in runs if r.text]
    while runs and not runs[0].raw and not runs[0].text.strip():
        runs.pop(0)
    while runs and not runs[-1].raw and not runs[-1].text.strip():
        runs.pop()
    if not runs:
        return []
    f, last = runs[0], runs[-1]
    runs[0] = Inline(f.text.lstrip(), f.bold, f.italic, f.strike, f.code, f.link, f.raw) if not f.raw else f
    last = runs[-1]
    runs[-1] = Inline(last.text.rstrip(), last.bold, last.italic, last.strike, last.code, last.link, last.raw) if not last.raw else last
    return runs
