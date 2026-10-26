"""Generate small sample files for every format.

Run directly to write them to tests/fixtures/:  uv run python tests/make_fixtures.py
The test suite calls ``build_all`` into a temp folder, so no binaries are committed.
"""

from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw


# ---------------------------------------------------------------- images
def png_bytes(color=(40, 110, 200), size=(120, 80), label="") -> bytes:
    im = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(im)
    d.rectangle([10, 10, size[0] - 10, size[1] - 10], fill=color)
    if label:
        d.text((16, 16), label, fill="white")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def make_png(path: Path) -> Path:
    path.write_bytes(png_bytes(label="chart"))
    return path


# ---------------------------------------------------------------- html
def make_html(path: Path) -> Path:
    img = base64.b64encode(png_bytes((200, 60, 60))).decode()
    path.write_text(f"""<!doctype html>
<html><head><title>Sample page</title><style>p {{ color: red }}</style><script>var x = 1;</script></head>
<body>
<h1>Quarterly report</h1>
<p>This is <strong>bold</strong>, <em>italic</em> and a <a href="https://example.com">link</a>.</p>
<ul><li>First</li><li>Second<ul><li>Nested</li></ul></li></ul>
<ol><li>One</li><li>Two</li></ol>
<table>
  <caption>Regional sales</caption>
  <tr><th>Region</th><th colspan="2">Sales</th></tr>
  <tr><td rowspan="2">North</td><td>Q1</td><td>10</td></tr>
  <tr><td>Q2</td><td>20 | twenty</td></tr>
</table>
<figure><img src="data:image/png;base64,{img}" alt="Red box"><figcaption>Figure 1: a red box</figcaption></figure>
<pre><code class="language-python">print("hi")</code></pre>
<blockquote>Quoted text</blockquote>
<img src="https://example.com/remote.png" alt="remote">
</body></html>""", encoding="utf-8")
    return path


# ---------------------------------------------------------------- xlsx
def xlsx_bytes() -> bytes:
    """Workbook with cached formula values (XlsxWriter lets us store results)."""
    import xlsxwriter

    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("Sales")
    st = wb.add_worksheet("Settings")
    pct = wb.add_format({"num_format": "0%"})
    st.write("A1", "Tax rate")
    st.write("B1", 0.2, pct)
    wb.define_name("TaxRate", "=Settings!$B$1")

    ws.write_row("A1", ["Item", "Qty", "Price", "Total"])
    items = [("Apples", 3, 2.5), ("Pears", 4, 1.25), ("Plums", 10, 0.5), ("Figs", 2, 4.0)]
    for i, (name, q, p) in enumerate(items, start=2):
        ws.write(f"A{i}", name)
        ws.write(f"B{i}", q)
        ws.write(f"C{i}", p)
        ws.write_formula(f"D{i}", f"=B{i}*C{i}", None, q * p)
    subtotal = sum(q * p for _, q, p in items)
    ws.write("C7", "Subtotal")
    ws.write_formula("D7", "=SUM(D2:D5)", None, subtotal)
    ws.write("C8", "Tax")
    ws.write_formula("D8", "=D7*TaxRate", None, subtotal * 0.2)
    ws.write("C9", "Total")
    ws.write_formula("D9", "=D7+D8", None, subtotal * 1.2)
    ws.merge_range("A11:B11", "Merged note", None)
    ws.write_comment("A1", "Product name")
    ws.data_validation("B2:B5", {"validate": "integer", "criteria": ">", "value": 0})

    chart = wb.add_chart({"type": "column"})
    chart.add_series({"name": "Total", "categories": "=Sales!$A$2:$A$5", "values": "=Sales!$D$2:$D$5"})
    chart.set_title({"name": "Totals by item"})
    ws.insert_chart("F2", chart)
    ws.insert_image("F20", "logo.png", {"image_data": io.BytesIO(png_bytes((20, 160, 90))), "description": "Logo"})
    wb.close()
    return buf.getvalue()


def make_xlsx(path: Path) -> Path:
    path.write_bytes(xlsx_bytes())
    return path


def make_xlsx_uncached(path: Path) -> Path:
    """openpyxl writes formulas without cached values."""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Calc"
    ws.append(["a", "b", "sum"])
    ws.append([1, 2, "=A2+B2"])
    ws.append([3, 4, "=A3+B3"])
    wb.save(path)
    return path


# ---------------------------------------------------------------- docx
def _chart_xml(title_series="Revenue") -> bytes:
    from pptx.chart.data import CategoryChartData
    from pptx.chart.xmlwriter import ChartXmlWriter
    from pptx.enum.chart import XL_CHART_TYPE

    cd = CategoryChartData()
    cd.categories = ["Q1", "Q2", "Q3"]
    cd.add_series(title_series, (10, 20, 30))
    return ChartXmlWriter(XL_CHART_TYPE.COLUMN_CLUSTERED, cd).xml.encode("utf-8")


def make_docx(path: Path) -> Path:
    import docx
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part
    from docx.oxml import OxmlElement, parse_xml
    from docx.oxml.ns import nsdecls, qn
    from docx.shared import Inches

    d = docx.Document()
    d.core_properties.title = "Sample document"
    d.add_heading("Introduction", level=1)
    p = d.add_paragraph("Plain text with ")
    p.add_run("bold").bold = True
    p.add_run(" and ")
    p.add_run("italic").italic = True
    p.add_run(" and a ")
    rid = d.part.relate_to("https://example.com", RT.HYPERLINK, is_external=True)
    hl = OxmlElement("w:hyperlink")
    hl.set(qn("r:id"), rid)
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = "link"
    r.append(t)
    hl.append(r)
    p._p.append(hl)
    p.add_run(".")

    d.add_heading("Lists", level=2)
    d.add_paragraph("Bullet one", style="List Bullet")
    d.add_paragraph("Bullet two", style="List Bullet")
    d.add_paragraph("Step one", style="List Number")
    d.add_paragraph("Step two", style="List Number")

    d.add_heading("Native table", level=2)
    tbl = d.add_table(rows=3, cols=3)
    tbl.style = "Table Grid"
    data = [["Name", "Q1", "Q2"], ["North", "10", "20"], ["South", "30", ""]]
    for i, row in enumerate(data):
        for j, v in enumerate(row):
            tbl.cell(i, j).text = v
    tbl.cell(0, 1).merge(tbl.cell(0, 2)).text = "Quarters"  # horizontal merge
    tbl.cell(1, 2).merge(tbl.cell(2, 2)).text = "20"  # vertical merge

    d.add_heading("Picture", level=2)
    d.add_picture(io.BytesIO(png_bytes((90, 40, 160))), width=Inches(1.5))

    d.add_heading("Chart", level=2)
    chart_part = Part(PackURI("/word/charts/chart1.xml"),
                      "application/vnd.openxmlformats-officedocument.drawingml.chart+xml",
                      _chart_xml(), d.part.package)
    crid = d.part.relate_to(chart_part, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart")
    drawing = parse_xml(f"""<w:drawing {nsdecls('w', 'wp', 'a', 'r')}>
      <wp:inline><wp:extent cx="3000000" cy="2000000"/><wp:docPr id="50" name="Chart 1"/>
        <a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart">
          <c:chart xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" r:id="{crid}"/>
        </a:graphicData></a:graphic></wp:inline></w:drawing>""")
    d.add_paragraph().add_run()._r.append(drawing)

    d.add_heading("Embedded workbook", level=2)
    xpart = Part(PackURI("/word/embeddings/Microsoft_Excel_Worksheet1.xlsx"),
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                 xlsx_bytes(), d.part.package)
    xrid = d.part.relate_to(xpart, RT.PACKAGE)
    img_rid, _ = d.part.get_or_add_image(io.BytesIO(png_bytes((10, 10, 10))))
    obj = parse_xml(f"""<w:object {nsdecls('w', 'r')} xmlns:v="urn:schemas-microsoft-com:vml"
        xmlns:o="urn:schemas-microsoft-com:office:office">
      <v:shape id="_x0000_i1025" style="width:200pt;height:100pt"><v:imagedata r:id="{img_rid}" o:title=""/></v:shape>
      <o:OLEObject Type="Embed" ProgID="Excel.Sheet.12" ShapeID="_x0000_i1025" DrawAspect="Content"
        ObjectID="_1" r:id="{xrid}"/></w:object>""")
    d.add_paragraph().add_run()._r.append(obj)
    d.add_paragraph("The end.")
    d.save(path)
    return path


# ---------------------------------------------------------------- pptx
def make_pptx(path: Path) -> Path:
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.enum.shapes import PROG_ID
    from pptx.util import Inches

    prs = Presentation()
    prs.core_properties.title = "Sample deck"
    s = prs.slides.add_slide(prs.slide_layouts[0])
    s.shapes.title.text = "Welcome"
    s.placeholders[1].text = "A subtitle"

    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Agenda"
    tf = s.placeholders[1].text_frame
    tf.text = "Point one"
    p = tf.add_paragraph()
    p.text = "Sub point"
    p.level = 1
    p = tf.add_paragraph()
    p.text = "Point two"
    s.notes_slide.notes_text_frame.text = "Remember to smile."

    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "Table and chart"
    gt = s.shapes.add_table(3, 3, Inches(0.5), Inches(1.5), Inches(4), Inches(1.5)).table
    vals = [["Region", "Q1", "Q2"], ["North", "1", "2"], ["South", "3", "4"]]
    for i, row in enumerate(vals):
        for j, v in enumerate(row):
            gt.cell(i, j).text = v
    gt.cell(0, 1).merge(gt.cell(0, 2))
    gt.cell(0, 1).text = "Quarter"
    cd = CategoryChartData()
    cd.categories = ["East", "West"]
    cd.add_series("Units", (5, 7))
    cd.add_series("Returns", (1, 2))
    ch = s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(5), Inches(1.5), Inches(4), Inches(3), cd).chart
    ch.has_title = True
    ch.chart_title.text_frame.text = "Units by region"

    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "Picture and group"
    pic = s.shapes.add_picture(io.BytesIO(png_bytes((200, 120, 0))), Inches(0.5), Inches(1.5))
    pic._element.nvPicPr.cNvPr.set("descr", "Orange box")
    grp = s.shapes.add_group_shape()
    tb = grp.shapes.add_textbox(Inches(5), Inches(1.5), Inches(3), Inches(1))
    tb.text_frame.text = "Text inside a group"

    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "Embedded Excel"
    s.shapes.add_ole_object(io.BytesIO(xlsx_bytes()), PROG_ID.XLSX, Inches(1), Inches(1.5), Inches(4), Inches(2))
    prs.save(path)
    return path


# ---------------------------------------------------------------- pdf
def make_pdf(path: Path) -> Path:
    import pymupdf

    doc = pymupdf.open()
    for n in range(1, 4):
        page = doc.new_page()  # A4-ish 595x842
        page.insert_text((50, 30), "ACME Corp annual report", fontsize=9)
        page.insert_text((280, 820), f"Page {n}", fontsize=9)
        if n == 1:
            page.insert_text((50, 90), "Annual Report", fontsize=24, fontname="hebo")
            page.insert_text((50, 130), "Overview", fontsize=16, fontname="hebo")
            y = 160
            for line in ["This report covers the results of the year. Revenue grew",
                         "strongly in every region and costs stayed flat."]:
                page.insert_text((50, y), line, fontsize=11)
                y += 14
            y += 14
            for b in ["• Revenue up 20%", "• Costs flat", "• Headcount up 5%"]:
                page.insert_text((60, y), b, fontsize=11)
                y += 16
            # ruled table
            x0, y0, cw, rh = 50, 300, 120, 22
            rows = [["Region", "Revenue", "Cost"], ["North", "100", "60"], ["South", "80", "50"]]
            for i in range(len(rows) + 1):
                page.draw_line((x0, y0 + i * rh), (x0 + 3 * cw, y0 + i * rh))
            for j in range(4):
                page.draw_line((x0 + j * cw, y0), (x0 + j * cw, y0 + len(rows) * rh))
            for i, row in enumerate(rows):
                for j, v in enumerate(row):
                    page.insert_text((x0 + j * cw + 5, y0 + i * rh + 15), v, fontsize=10)
        elif n == 2:
            page.insert_text((50, 90), "Charts", fontsize=16, fontname="hebo")
            page.insert_image(pymupdf.Rect(50, 110, 250, 240), stream=png_bytes((0, 128, 128), (200, 130)))
            # vector bar chart
            bx, by = 300, 400
            page.draw_line((bx, by), (bx + 220, by))
            page.draw_line((bx, by), (bx, by - 160))
            for k, h in enumerate([60, 120, 90, 140]):
                page.draw_rect(pymupdf.Rect(bx + 15 + k * 50, by - h, bx + 45 + k * 50, by), color=(0, 0, 1), fill=(0, 0, 1))
            page.insert_text((bx + 60, by + 20), "Sales by quarter", fontsize=9)
            page.insert_text((50, 500), "Text after the charts.", fontsize=11)
        else:
            page.insert_text((50, 90), "Closing remarks for the year.", fontsize=11)
    doc.set_metadata({"title": "Annual Report"})
    doc.save(path)
    return path


def build_all(folder: Path) -> dict[str, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    return {
        "png": make_png(folder / "sample.png"),
        "html": make_html(folder / "sample.html"),
        "xlsx": make_xlsx(folder / "sample.xlsx"),
        "xlsx_uncached": make_xlsx_uncached(folder / "uncached.xlsx"),
        "docx": make_docx(folder / "sample.docx"),
        "pptx": make_pptx(folder / "sample.pptx"),
        "pdf": make_pdf(folder / "sample.pdf"),
    }


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "fixtures"
    for k, v in build_all(out).items():
        print(f"{k:14} {v}")
