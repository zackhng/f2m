"""XLSX -> Document, keeping the calculation logic.

The workbook is loaded twice: once for cached values and once for formulas.
Each sheet becomes a values table plus a Formulas section in which filled-down
or filled-right formulas are grouped into one row.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass, field

import openpyxl
from openpyxl.formula.tokenizer import Token, Tokenizer
from openpyxl.formula.translate import Translator
from openpyxl.utils.cell import get_column_letter, range_boundaries
from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula

from f2m.common import charts as chartmod
from f2m.common.ooxml import NS, R_EMBED, R_ID, URI_CHART, local, parse_xml, read_rels
from f2m.common.tables import fmt_value, is_blank
from f2m.context import ParseContext
from f2m.detect import mime_for
from f2m.document import (
    Block,
    Document,
    Heading,
    ImageBlock,
    Inline,
    Paragraph,
    Table,
    Text,
)
from f2m.registry import Parser, register
from f2m.timing import timed

log = logging.getLogger(__name__)
NOT_COMPUTED = "(not computed)"


# ---------------------------------------------------------------- package
@dataclass
class DrawingItem:
    kind: str  # "chart" | "image"
    row: int
    col: int
    data: bytes
    name: str = ""
    descr: str = ""


@dataclass
class _Package:
    """Reads what openpyxl doesn't expose: sheet drawings, chart XML, oleSize."""

    z: zipfile.ZipFile
    sheet_parts: dict[str, str] = field(default_factory=dict)
    ole_size: str | None = None

    @classmethod
    def open(cls, data: bytes) -> _Package:
        z = zipfile.ZipFile(io.BytesIO(data))
        pkg = cls(z)
        root_rels = read_rels(z, "")
        wb_part = next((r.target for r in root_rels.values() if r.type == "officeDocument"), "xl/workbook.xml")
        try:
            wb = parse_xml(z.read(wb_part))
        except KeyError:
            return pkg
        rels = read_rels(z, wb_part)
        for s in wb.iter(f"{{{NS['x']}}}sheet"):
            rel = rels.get(s.get(R_ID, ""))
            if rel:
                pkg.sheet_parts[s.get("name", "")] = rel.target
        ole = wb.find(f"{{{NS['x']}}}oleSize")
        if ole is not None:
            pkg.ole_size = ole.get("ref")
        return pkg

    def drawing_items(self, sheet: str) -> list[DrawingItem]:
        part = self.sheet_parts.get(sheet)
        if not part:
            return []
        items: list[DrawingItem] = []
        for rel in read_rels(self.z, part).values():
            if rel.type != "drawing" or rel.external:
                continue
            try:
                root = parse_xml(self.z.read(rel.target))
            except KeyError:
                continue
            drels = read_rels(self.z, rel.target)
            for anchor in root:
                if local(anchor.tag) not in ("twoCellAnchor", "oneCellAnchor", "absoluteAnchor"):
                    continue
                frm = anchor.find(f"{{{NS['xdr']}}}from")
                row = col = 0
                if frm is not None:
                    col = int(frm.findtext(f"{{{NS['xdr']}}}col") or 0)
                    row = int(frm.findtext(f"{{{NS['xdr']}}}row") or 0)
                items.extend(self._anchor_items(anchor, drels, row + 1, col + 1))
        return items

    def _anchor_items(self, el, drels, row: int, col: int) -> list[DrawingItem]:
        out: list[DrawingItem] = []
        for node in el.iter():
            name = local(node.tag)
            if name == "blip":
                rel = drels.get(node.get(R_EMBED, ""))
                if rel and not rel.external:
                    pic = next((a for a in node.iterancestors() if local(a.tag) == "pic"), None)
                    cnv = pic.find(f".//{{{NS['xdr']}}}cNvPr") if pic is not None else None
                    try:
                        out.append(DrawingItem("image", row, col, self.z.read(rel.target),
                                               cnv.get("name", "") if cnv is not None else "",
                                               cnv.get("descr", "") if cnv is not None else ""))
                    except KeyError:
                        pass
            elif name == "graphicData" and node.get("uri") == URI_CHART:
                ch = node.find(f"{{{NS['c']}}}chart")
                rel = drels.get(ch.get(R_ID, "")) if ch is not None else None
                if rel and not rel.external:
                    try:
                        out.append(DrawingItem("chart", row, col, self.z.read(rel.target)))
                    except KeyError:
                        pass
        return out


# ---------------------------------------------------------------- formulas
def _formula_text(v) -> str | None:
    if isinstance(v, ArrayFormula):
        return "{" + (v.text or "") + "}"
    if isinstance(v, DataTableFormula):
        return "=TABLE(" + ",".join(str(x) for x in (v.r1, v.r2) if x) + ")"
    if isinstance(v, str) and v.startswith("=") and len(v) > 1:
        return v
    return None


@dataclass
class FormulaGroup:
    r1: int
    c1: int
    r2: int
    c2: int
    formula: str  # formula of the first cell
    direction: str = ""  # "down", "right" or ""

    @property
    def ref(self) -> str:
        a = f"{get_column_letter(self.c1)}{self.r1}"
        if (self.r1, self.c1) == (self.r2, self.c2):
            return a
        return f"{a}:{get_column_letter(self.c2)}{self.r2}"

    def cells(self) -> list[tuple[int, int]]:
        return [(r, c) for r in range(self.r1, self.r2 + 1) for c in range(self.c1, self.c2 + 1)]


def _translate(formula: str, src: tuple[int, int], dst: tuple[int, int]) -> str | None:
    try:
        return Translator(formula, origin=f"{get_column_letter(src[1])}{src[0]}").translate_formula(
            f"{get_column_letter(dst[1])}{dst[0]}")
    except Exception:
        return None


def group_formulas(formulas: dict[tuple[int, int], str]) -> list[FormulaGroup]:
    """Group cells whose formulas are the same relative formula, filled down or right."""
    plain_f = {k: v for k, v in formulas.items() if v.startswith("=")}
    used: set[tuple[int, int]] = set()
    groups: list[FormulaGroup] = []

    def runs(axis: int) -> None:
        keys = sorted((k for k in plain_f if k not in used), key=lambda k: (k[1 - axis], k[axis]))
        i = 0
        while i < len(keys):
            start = keys[i]
            f0 = plain_f[start]
            j = i + 1
            while j < len(keys):
                prev, cur = keys[j - 1], keys[j]
                if cur[1 - axis] != start[1 - axis] or cur[axis] != prev[axis] + 1:
                    break
                if _translate(f0, start, cur) != plain_f[cur]:
                    break
                j += 1
            if j - i >= 2:
                end = keys[j - 1]
                groups.append(FormulaGroup(start[0], start[1], end[0], end[1], f0,
                                           "down" if axis == 0 else "right"))
                used.update(keys[i:j])
            i = j

    runs(0)  # down columns
    runs(1)  # across rows
    for (r, c), f in formulas.items():
        if (r, c) not in used:
            groups.append(FormulaGroup(r, c, r, c, f))
    groups.sort(key=lambda g: (g.r1, g.c1))
    return groups


def formula_refs(formula: str) -> list[str]:
    """Cell ranges and names a formula reads from."""
    text = formula[1:-1] if formula.startswith("{") else formula
    try:
        tok = Tokenizer(text)
    except Exception:
        return []
    return [t.value for t in tok.items if t.type == Token.OPERAND and t.subtype == Token.RANGE]


_SHEET_REF = re.compile(r"^(?:(?P<sheet>'(?:[^']|'')+'|[^!]+)!)?(?P<rng>.+)$")


def _split_ref(ref: str) -> tuple[str | None, str]:
    m = _SHEET_REF.match(ref.strip())
    if not m:
        return None, ref
    sheet = m.group("sheet")
    if sheet and sheet.startswith("'"):
        sheet = sheet[1:-1].replace("''", "'")
    return sheet, m.group("rng").replace("$", "")


def _span_refs(a: str, b: str) -> str:
    """Bounding range of two versions of the same reference (first and last cell of a run)."""
    if a == b:
        return a
    sa, ra = _split_ref(a)
    sb, rb = _split_ref(b)
    try:
        c1, r1, c2, r2 = range_boundaries(ra)
        d1, s1, d2, s2 = range_boundaries(rb)
    except Exception:
        return f"{a} … {b}"
    rng = f"{get_column_letter(min(c1, d1))}{min(r1, s1)}:{get_column_letter(max(c2, d2))}{max(r2, s2)}"
    sheet = sa or sb
    if sheet:
        q = f"'{sheet}'" if re.search(r"[^A-Za-z0-9_]", sheet) else sheet
        return f"{q}!{rng}"
    return rng


# ---------------------------------------------------------------- parser
@register
class XlsxParser(Parser):
    formats = ("xlsx",)
    extensions = (".xlsx", ".xlsm", ".xltx")

    def parse(self, data: bytes, ctx: ParseContext) -> Document:
        opts = ctx.options
        doc = Document(format="xlsx")
        with timed("load_formulas"):
            wb_f = openpyxl.load_workbook(io.BytesIO(data), data_only=False)
        with timed("load_values"):
            wb_v = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
        with timed("package"):
            pkg = _Package.open(data)
        title = (wb_f.properties.title or "").strip() or ctx.stem
        doc.metadata.update(title=title, sheets=wb_f.sheetnames)

        computed: dict[tuple[str, str], object] = {}
        if opts.xlsx_compute_missing:
            with timed("compute"):
                computed = _compute(data, ctx.name, doc)

        embedded_view = ctx.embedded and not opts.xlsx_embedded_full
        if not ctx.embedded:
            doc.add(Heading(1, title))
        names = [wb_f.active.title] if embedded_view and wb_f.active is not None else wb_f.sheetnames
        region = pkg.ole_size if embedded_view else None
        # If any formula exists, show A1 coordinates on every sheet so references can be followed.
        coords = opts.xlsx_formulas != "none" and any(
            _formula_text(c.value) for ws in wb_f.worksheets for c in ws._cells.values())

        for name in names:
            sheet_f = wb_f[name]
            heading_level = 1 if embedded_view else 2
            hidden = getattr(sheet_f, "sheet_state", "visible") != "visible"
            if not embedded_view:
                doc.add(Heading(heading_level, f"Sheet: {name}" + (" (hidden)" if hidden else "")))
            if not hasattr(sheet_f, "_cells"):  # chartsheet
                with timed("drawings"):
                    doc.extend(self._drawings(pkg, name, wb_v, doc))
                continue
            try:
                with timed("sheets"):
                    doc.extend(_SheetWriter(sheet_f, wb_v[name], wb_v, computed, ctx, region,
                                            heading_level + 1, coords).blocks(doc))
            except Exception as e:
                log.debug("sheet failed", exc_info=True)
                doc.warn(f"sheet {name!r} could not be fully converted: {e}")
            with timed("drawings"):
                doc.extend(self._drawings(pkg, name, wb_v, doc))

        if not embedded_view:
            with timed("named_ranges"):
                doc.extend(_defined_names(wb_f, wb_v))
        return doc

    def _drawings(self, pkg: _Package, sheet: str, wb_v, doc: Document) -> list[Block]:
        out: list[Block] = []
        for i, item in enumerate(pkg.drawing_items(sheet), 1):
            where = f"{get_column_letter(item.col)}{item.row}"
            if item.kind == "chart":
                try:
                    chart = chartmod.parse_chart(item.data)
                    chartmod.fill_from_refs(chart, lambda ref: _resolve_values(wb_v, ref, sheet))
                    blocks = chartmod.chart_blocks(chart)
                    if blocks and isinstance(blocks[0], Paragraph) and isinstance(blocks[0].text, list):
                        blocks[0].text.append(Inline(f" (at {where})"))
                    out.extend(blocks)
                except Exception as e:
                    doc.warn(f"chart on {sheet!r} at {where} could not be read: {e}")
            else:
                out.append(ImageBlock(data=item.data, mime=mime_for(item.data, "image/png"),
                                      name_hint=f"{_safe(sheet)}_img{i}", alt=item.descr or item.name,
                                      location={"sheet": sheet, "cell": where}))
        return out


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_") or "sheet"


def _resolve_values(wb_v, ref: str, default_sheet: str) -> list[str]:
    sheet, rng = _split_ref(ref)
    try:
        ws = wb_v[sheet or default_sheet]
        c1, r1, c2, r2 = range_boundaries(rng)
    except Exception:
        return []
    out = []
    for row in ws.iter_rows(min_row=r1, max_row=r2, min_col=c1, max_col=c2, values_only=True):
        out.extend(fmt_value(v) for v in row)
    return out


class _SheetWriter:
    def __init__(self, ws_f, ws_v, wb_v, computed, ctx: ParseContext, region: str | None, sub_level: int,
                 coords: bool = False):
        self.ws_f, self.ws_v, self.wb_v = ws_f, ws_v, wb_v
        self.computed = computed
        self.opts = ctx.options
        self.region = region
        self.sub_level = sub_level
        self.coords = coords

    # -- helpers ----------------------------------------------------------
    def _bounds(self) -> tuple[int, int, int, int] | None:
        if self.region:
            try:
                c1, r1, c2, r2 = range_boundaries(self.region)
                return r1, c1, r2, c2
            except Exception:
                pass
        keys = [k for k, c in self.ws_f._cells.items() if c.value not in (None, "")]
        keys += [k for k, c in self.ws_v._cells.items() if c.value not in (None, "")]
        if not keys:
            return None
        rows = [k[0] for k in keys]
        cols = [k[1] for k in keys]
        return min(rows), min(cols), max(rows), max(cols)

    def _value(self, r: int, c: int) -> str:
        cell_v = self.ws_v._cells.get((r, c))
        cell_f = self.ws_f._cells.get((r, c))
        v = cell_v.value if cell_v is not None else None
        if v is None and cell_f is not None and _formula_text(cell_f.value):
            key = (self.ws_f.title.upper(), f"{get_column_letter(c)}{r}")
            if key in self.computed:
                return fmt_value(self.computed[key])
            return NOT_COMPUTED
        if v is None:
            return ""
        fmtcode = (cell_v.number_format or "") if cell_v is not None else ""
        if isinstance(v, (int, float)) and not isinstance(v, bool) and "%" in fmtcode:
            decimals = len(fmtcode.split(".")[1].split("%")[0]) if "." in fmtcode else 0
            return f"{v * 100:.{decimals}f}%"
        return fmt_value(v)

    # -- output -------------------------------------------------------------
    def blocks(self, doc: Document) -> list[Block]:
        out: list[Block] = []
        bounds = self._bounds()
        if bounds is None:
            out.append(Paragraph([Inline("(empty sheet)", italic=True)]))
            return out
        r1, c1, r2, c2 = bounds
        truncated = False
        if self.opts.xlsx_max_rows and r2 - r1 + 1 > self.opts.xlsx_max_rows:
            r2 = r1 + self.opts.xlsx_max_rows - 1
            truncated = True

        formulas: dict[tuple[int, int], str] = {}
        for (r, c), cell in self.ws_f._cells.items():
            if r1 <= r <= r2 and c1 <= c <= c2:
                f = _formula_text(cell.value)
                if f:
                    formulas[(r, c)] = f
        mode = self.opts.xlsx_formulas
        show_refs = (bool(formulas) or self.coords) and mode != "none"

        # merged ranges: (r, c) -> top-left
        merged: dict[tuple[int, int], tuple[int, int]] = {}
        for mr in self.ws_f.merged_cells.ranges:
            for r in range(mr.min_row, mr.max_row + 1):
                for c in range(mr.min_col, mr.max_col + 1):
                    if (r, c) != (mr.min_row, mr.min_col):
                        merged[(r, c)] = (mr.min_row, mr.min_col)

        grid: dict[tuple[int, int], Text] = {}
        for r in range(r1, r2 + 1):
            for c in range(c1, c2 + 1):
                if (r, c) in merged:
                    tr, tc = merged[(r, c)]
                    grid[(r, c)] = self._value(tr, tc) if self.opts.merged_cells == "repeat" else ""
                    continue
                text: Text = self._value(r, c)
                cell_f = self.ws_f._cells.get((r, c))
                link = cell_f.hyperlink.target if cell_f is not None and cell_f.hyperlink is not None else None
                if link and text:
                    text = [Inline(str(text), link=link)]
                if mode == "inline" and (r, c) in formulas:
                    runs = text if isinstance(text, list) else [Inline(str(text))]
                    text = runs + [Inline(" "), Inline(formulas[(r, c)], code=True)]
                grid[(r, c)] = text

        rows = [r for r in range(r1, r2 + 1) if any(not is_blank(grid[(r, c)]) for c in range(c1, c2 + 1))]
        cols = [c for c in range(c1, c2 + 1) if any(not is_blank(grid[(r, c)]) for r in rows)]
        if show_refs:
            # Keep A1 coordinates visible so formula references can be followed.
            table_rows: list[list[Text]] = [[""] + [get_column_letter(c) for c in cols]]
            table_rows += [[str(r)] + [grid[(r, c)] for c in cols] for r in rows]
        else:
            table_rows = [[grid[(r, c)] for c in cols] for r in rows]
        if table_rows and any(table_rows):
            out.append(Table(rows=table_rows))
        if truncated:
            out.append(Paragraph([Inline(f"(showing the first {self.opts.xlsx_max_rows} rows)", italic=True)]))

        if formulas and mode == "section":
            with timed("formulas"):
                out.extend(self._formula_section(formulas))
        out.extend(self._tables_section())
        out.extend(self._validation_section())
        out.extend(self._comments_section(r1, c1, r2, c2))
        return out

    def _formula_section(self, formulas: dict[tuple[int, int], str]) -> list[Block]:
        deps = self.opts.xlsx_dependencies
        header: list[Text] = ["Cells", "Formula", "Value"] + (["Reads from"] if deps else [])
        rows: list[list[Text]] = [header]
        for g in group_formulas(formulas):
            formula: list[Inline] = [Inline(g.formula, code=True)]
            if g.direction:
                formula.append(Inline(f" (filled {g.direction})"))
            vals = [self._value(r, c) for r, c in g.cells()]
            if len(vals) == 1:
                value = vals[0]
            else:
                shown = ", ".join(vals[:3])
                value = f"{shown}, … ({len(vals)} cells)" if len(vals) > 3 else shown
            row: list[Text] = [g.ref, formula, value]
            if deps:
                first = formula_refs(g.formula)
                if g.direction:
                    last_f = _translate(g.formula, (g.r1, g.c1), (g.r2, g.c2)) or g.formula
                    last = formula_refs(last_f)
                    refs = [_span_refs(a, b) for a, b in zip(first, last)] if len(first) == len(last) else first
                else:
                    refs = first
                row.append(", ".join(dict.fromkeys(refs)))
            rows.append(row)
        return [Heading(self.sub_level, "Formulas"), Table(rows=rows)]

    def _tables_section(self) -> list[Block]:
        tables = list(getattr(self.ws_f, "tables", {}).values())
        if not tables:
            return []
        rows: list[list[Text]] = [["Table", "Range", "Columns"]]
        for t in tables:
            cols = []
            for col in getattr(t, "tableColumns", []) or []:
                desc = col.name or ""
                calc = getattr(col, "calculatedColumnFormula", None)
                calc_text = getattr(calc, "attr_text", None) or (calc if isinstance(calc, str) else None)
                if calc_text:
                    desc += f" = {calc_text if calc_text.startswith('=') else '=' + calc_text}"
                if col.totalsRowFunction:
                    desc += f" (total: {col.totalsRowFunction})"
                cols.append(desc)
            rows.append([t.displayName or t.name, t.ref, "; ".join(cols)])
        return [Heading(self.sub_level, "Tables"), Table(rows=rows)]

    def _validation_section(self) -> list[Block]:
        dv = getattr(self.ws_f, "data_validations", None)
        items = list(getattr(dv, "dataValidation", []) or [])
        if not items:
            return []
        rows: list[list[Text]] = [["Cells", "Rule"]]
        for v in items:
            rule = v.type or "any"
            if v.operator and v.type not in ("list", None):
                rule += f" {v.operator}"
            vals = [x for x in (v.formula1, v.formula2) if x]
            if vals:
                rule += ": " + " and ".join(vals)
            rows.append([str(v.sqref), rule])
        return [Heading(self.sub_level, "Data validation"), Table(rows=rows)]

    def _comments_section(self, r1: int, c1: int, r2: int, c2: int) -> list[Block]:
        rows: list[list[Text]] = [["Cell", "Comment"]]
        for (r, c), cell in sorted(self.ws_f._cells.items()):
            if cell.comment is not None and r1 <= r <= r2 and c1 <= c <= c2:
                rows.append([cell.coordinate, cell.comment.text.strip()])
        if len(rows) == 1:
            return []
        return [Heading(self.sub_level, "Comments"), Table(rows=rows)]


def _defined_names(wb_f, wb_v) -> list[Block]:
    entries: list[tuple[str, str, str]] = []
    for name, dn in wb_f.defined_names.items():
        entries.append((name, "workbook", dn.attr_text or ""))
    for ws in wb_f.worksheets:
        for name, dn in getattr(ws, "defined_names", {}).items():
            entries.append((name, ws.title, dn.attr_text or ""))
    entries = [e for e in entries if not e[0].startswith("_xlnm.")]
    if not entries:
        return []
    rows: list[list[Text]] = [["Name", "Scope", "Refers to", "Value"]]
    for name, scope, text in entries:
        value = ""
        if text and not text.startswith("=") and "(" not in text:
            vals = _resolve_values(wb_v, text, wb_v.sheetnames[0])
            if len(vals) == 1:
                value = vals[0]
            elif vals:
                value = ", ".join(vals[:3]) + (f", … ({len(vals)} cells)" if len(vals) > 3 else "")
        rows.append([name, scope, [Inline(text, code=True)] if text else "", value])
    return [Heading(2, "Named ranges"), Table(rows=rows)]


def _compute(data: bytes, name: str, doc: Document) -> dict[tuple[str, str], object]:
    """Calculate formula values with the optional `formulas` package."""
    import os
    import tempfile

    try:
        import formulas  # type: ignore
    except ImportError:
        doc.warn("xlsx_compute_missing needs the 'calc' extra (pip install f2m[calc])")
        return {}
    out: dict[tuple[str, str], object] = {}
    fd, tmp = tempfile.mkstemp(suffix=".xlsx")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        model = formulas.ExcelModel().loads(tmp).finish()
        sol = model.calculate()
        pat = re.compile(r"^'?\[[^\]]+\](?P<sheet>.+?)'?!(?P<cell>[A-Z]+\d+)$")
        for key, val in sol.items():
            m = pat.match(str(key))
            if not m:
                continue
            try:
                v = val.value[0, 0]
                v = v.item() if hasattr(v, "item") else v
            except Exception:
                continue
            out[(m.group("sheet").upper(), m.group("cell"))] = v
    except Exception as e:
        doc.warn(f"could not compute formula values: {e}")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return out
