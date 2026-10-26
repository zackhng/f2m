"""Read native Office charts (chart*.xml). The data lives in the file, so no model is needed."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from lxml import etree

from f2m.common.ooxml import NS, local, parse_xml
from f2m.common.tables import fmt_number_string
from f2m.document import Block, Inline, Paragraph, Table

C = f"{{{NS['c']}}}"
A = f"{{{NS['a']}}}"

_KIND_NAMES = {
    "barChart": "bar", "bar3DChart": "3D bar", "lineChart": "line", "line3DChart": "3D line",
    "pieChart": "pie", "pie3DChart": "3D pie", "doughnutChart": "doughnut", "ofPieChart": "pie",
    "areaChart": "area", "area3DChart": "3D area", "scatterChart": "scatter", "bubbleChart": "bubble",
    "radarChart": "radar", "stockChart": "stock", "surfaceChart": "surface", "surface3DChart": "3D surface",
}


@dataclass
class Series:
    name: str = ""
    categories: list[str] = field(default_factory=list)
    values: list[str] = field(default_factory=list)
    name_ref: str = ""
    cat_ref: str = ""
    val_ref: str = ""
    kind: str = ""


@dataclass
class Chart:
    title: str = ""
    kinds: list[str] = field(default_factory=list)
    series: list[Series] = field(default_factory=list)
    axis_titles: list[str] = field(default_factory=list)


def _cache_points(el: etree._Element | None) -> list[str]:
    """Values from a strCache/numCache/strLit/numLit, placed by their idx."""
    if el is None:
        return []
    count_el = el.find(f"{C}ptCount")
    pts = el.findall(f"{C}pt")
    n = int(count_el.get("val", "0")) if count_el is not None else 0
    n = max(n, max((int(p.get("idx", "0")) + 1 for p in pts), default=0))
    out = [""] * n
    for p in pts:
        v = p.find(f"{C}v")
        out[int(p.get("idx", "0"))] = (v.text or "") if v is not None else ""
    return out


def _data_source(el: etree._Element | None) -> tuple[list[str], str]:
    """(cached values, formula ref) for c:cat / c:val / c:tx / c:xVal / c:yVal."""
    if el is None:
        return [], ""
    for ref_tag in ("numRef", "strRef", "multiLvlStrRef"):
        ref = el.find(f"{C}{ref_tag}")
        if ref is not None:
            f = ref.find(f"{C}f")
            cache = ref.find(f"{C}numCache")
            if cache is None:  # lxml elements are falsy when empty, so no `or` here
                cache = ref.find(f"{C}strCache")
            if ref_tag == "multiLvlStrRef":
                cache = ref.find(f"{C}multiLvlStrCache/{C}lvl")
            return _cache_points(cache), (f.text or "") if f is not None else ""
    for lit_tag in ("numLit", "strLit"):
        lit = el.find(f"{C}{lit_tag}")
        if lit is not None:
            return _cache_points(lit), ""
    v = el.find(f"{C}v")
    if v is not None:
        return [v.text or ""], ""
    return [], ""


def _rich_text(el: etree._Element | None) -> str:
    if el is None:
        return ""
    texts, _ = _data_source(el.find(f"{C}tx"))
    if texts:
        return " ".join(t for t in texts if t)
    return " ".join("".join(t.text or "" for t in p.iter(f"{A}t")) for p in el.iter(f"{A}p")).strip()


def parse_chart(xml: bytes | etree._Element) -> Chart:
    root = parse_xml(xml) if isinstance(xml, (bytes, bytearray)) else xml
    chart_el = root.find(f"{C}chart") if local(root.tag) == "chartSpace" else root
    chart = Chart()
    if chart_el is None:
        return chart
    chart.title = _rich_text(chart_el.find(f"{C}title"))
    plot = chart_el.find(f"{C}plotArea")
    if plot is None:
        return chart
    for group in plot:
        name = local(group.tag)
        if not name.endswith("Chart"):
            continue
        kind = _KIND_NAMES.get(name, name.removesuffix("Chart"))
        bar_dir = group.find(f"{C}barDir")
        if bar_dir is not None and bar_dir.get("val") == "col":
            kind = kind.replace("bar", "column")
        if kind not in chart.kinds:
            chart.kinds.append(kind)
        for ser in group.findall(f"{C}ser"):
            s = Series(kind=kind)
            tx = ser.find(f"{C}tx")
            names, s.name_ref = _data_source(tx)
            s.name = " ".join(n for n in names if n)
            cat = ser.find(f"{C}cat")
            if cat is None:
                cat = ser.find(f"{C}xVal")
            val = ser.find(f"{C}val")
            if val is None:
                val = ser.find(f"{C}yVal")
            s.categories, s.cat_ref = _data_source(cat)
            s.values, s.val_ref = _data_source(val)
            chart.series.append(s)
    for ax_tag in ("catAx", "dateAx", "valAx", "serAx"):
        for ax in plot.findall(f"{C}{ax_tag}"):
            t = _rich_text(ax.find(f"{C}title"))
            if t:
                chart.axis_titles.append(t)
    return chart


RefResolver = Callable[[str], "list[str]"]


def fill_from_refs(chart: Chart, resolve: RefResolver) -> None:
    """Fill series that have formula refs but no cached data (e.g. charts written by openpyxl)."""
    for s in chart.series:
        if not s.values and s.val_ref:
            s.values = resolve(s.val_ref)
        if not s.categories and s.cat_ref:
            s.categories = resolve(s.cat_ref)
        if not s.name and s.name_ref:
            s.name = " ".join(resolve(s.name_ref))


def chart_blocks(chart: Chart) -> list[Block]:
    """A short description line plus a data table (categories x series)."""
    kind = " / ".join(chart.kinds) or "chart"
    title = chart.title or (chart.series[0].name if len(chart.series) == 1 else "")
    head: list[Inline] = [Inline("Chart", bold=True), Inline(f" ({kind})")]
    if title:
        head.append(Inline(f": {title}"))
    if chart.axis_titles:
        head.append(Inline(f". Axes: {', '.join(chart.axis_titles)}"))
    blocks: list[Block] = [Paragraph(head)]
    if not chart.series:
        return blocks

    names = [s.name or f"Series {i + 1}" for i, s in enumerate(chart.series)]
    cats = next((s.categories for s in chart.series if s.categories), [])
    shared = all(not s.categories or s.categories == cats for s in chart.series)
    if shared:
        n = max([len(cats)] + [len(s.values) for s in chart.series])
        label = "X" if "scatter" in chart.kinds or "bubble" in chart.kinds else "Category"
        rows: list[list] = [[label] + names]
        for i in range(n):
            cat = cats[i] if i < len(cats) else str(i + 1)
            row = [fmt_number_string(cat)]
            for s in chart.series:
                row.append(fmt_number_string(s.values[i]) if i < len(s.values) else "")
            rows.append(row)
    else:  # series with their own x values: long format
        rows = [["Series", "X", "Y"]]
        for name, s in zip(names, chart.series):
            for i, v in enumerate(s.values):
                x = s.categories[i] if i < len(s.categories) else str(i + 1)
                rows.append([name, fmt_number_string(x), fmt_number_string(v)])
    blocks.append(Table(rows=rows))
    return blocks
