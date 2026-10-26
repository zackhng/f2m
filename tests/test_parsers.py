"""One block of checks per format, run on the generated sample files."""

from __future__ import annotations

from f2m.parsers.xlsx import FormulaGroup, formula_refs, group_formulas


# ------------------------------------------------------------------ xlsx
def test_xlsx_values_and_coordinates(md):
    out = md("xlsx")
    assert "## Sheet: Sales" in out
    assert "|  | A | B | C | D |" in out
    assert "| 2 | Apples | 3 | 2.5 | 7.5 |" in out
    assert "| 9 |  |  | Total | 30.6 |" in out
    assert "| 1 | Tax rate | 20% |" in out  # percent format kept


def test_xlsx_formula_section_groups_filled_formulas(md):
    out = md("xlsx")
    assert "### Formulas" in out
    assert "| D2:D5 | `=B2*C2` (filled down) | 7.5, 5, 5, … (4 cells) |" in out
    assert "| D8 | `=D7*TaxRate` | 5.1 |" in out


def test_xlsx_workbook_extras(md):
    out = md("xlsx")
    assert "| TaxRate | workbook | `Settings!$B$1` | 0.2 |" in out
    assert "| B2:B5 | whole greaterThan: 0 |" in out
    assert "| A1 | Product name |" in out
    assert "**Chart** (column): Totals by item (at F2)" in out
    assert "| Apples | 7.5 |" in out
    assert "![Logo](" in out


def test_xlsx_inline_and_none_modes(md):
    inline = md("xlsx", xlsx_formulas="inline")
    assert "7.5 `=B2*C2`" in inline and "### Formulas" not in inline
    none = md("xlsx", xlsx_formulas="none")
    assert "`=" not in none and "|  | A |" not in none
    assert "| Apples | 3 | 2.5 | 7.5 |" in none


def test_xlsx_dependencies(md):
    out = md("xlsx", xlsx_dependencies=True)
    assert "| Cells | Formula | Value | Reads from |" in out
    assert "B2:B5, C2:C5" in out
    assert "D7, TaxRate" in out


def test_xlsx_uncached_formulas_are_marked(md):
    out = md("xlsx_uncached")
    assert "(not computed)" in out
    assert "| C2:C3 | `=A2+B2` (filled down) |" in out


def test_group_formulas_down_right_and_single():
    f = {(2, 4): "=B2*C2", (3, 4): "=B3*C3", (4, 4): "=B4*C4",
         (10, 2): "=B9+1", (10, 3): "=C9+1",
         (12, 1): "=SUM(A1:A5)"}
    groups = {g.ref: g for g in group_formulas(f)}
    assert groups["D2:D4"].direction == "down"
    assert groups["B10:C10"].direction == "right"
    assert groups["A12"].direction == ""
    # a formula that breaks the pattern ends the run
    f = {(2, 1): "=B2", (3, 1): "=B3", (4, 1): "=B99"}
    refs = sorted(g.ref for g in group_formulas(f))
    assert refs == ["A2:A3", "A4"]


def test_formula_refs():
    assert formula_refs("=SUM(Sheet2!A1:B3)+TaxRate*C4") == ["Sheet2!A1:B3", "TaxRate", "C4"]
    assert FormulaGroup(1, 1, 1, 1, "=1").ref == "A1"


# ------------------------------------------------------------------ docx
def test_docx_text_lists_and_links(md):
    out = md("docx")
    assert "# Introduction" in out
    assert "Plain text with **bold** and *italic* and a [link](https://example.com)." in out
    assert "- Bullet one\n- Bullet two\n\n1. Step one\n2. Step two" in out


def test_docx_native_table_with_merges(md):
    out = md("docx")
    assert "| Name | Quarters | Quarters |" in out
    assert "| South | 30 | 20 |" in out  # vertical merge repeated
    blank = md("docx", merged_cells="blank")
    assert "| Name | Quarters |  |" in blank


def test_docx_chart_picture_and_embedded_workbook(md):
    out = md("docx")
    assert "**Chart** (column): Revenue" in out
    assert "| Q3 | 30 |" in out
    assert "](" in out.split("## Picture")[1].split("##")[0]  # image link
    emb = out.split("## Embedded workbook")[1]
    assert "**Embedded xlsx:" in emb
    assert "### Formulas" in emb  # nested under the H2
    assert "| D2:D5 | `=B2*C2` (filled down)" in emb
    assert emb.rstrip().endswith("The end.")


def test_docx_depth_limit_falls_back_to_preview(md):
    out = md("docx", max_depth=0)
    emb = out.split("## Embedded workbook")[1]
    assert "**Embedded xlsx" not in emb
    assert "![" in emb  # the object's preview picture


# ------------------------------------------------------------------ pptx
def test_pptx_slides_lists_notes(md):
    out = md("pptx")
    assert out.startswith("# Sample deck")
    assert "## Slide 1: Welcome\n\nA subtitle" in out
    assert "- Point one\n    - Sub point\n- Point two" in out
    assert "> **Notes:** Remember to smile." in out
    assert "Remember to smile" not in md("pptx", pptx_notes=False)


def test_pptx_table_chart_picture_group(md):
    out = md("pptx")
    assert "| Region | Quarter | Quarter |" in out
    assert "**Chart** (column): Units by region" in out
    assert "| East | 5 | 1 |" in out
    assert "![Orange box](" in out
    assert "Text inside a group" in out


def test_pptx_embedded_excel(md):
    out = md("pptx")
    slide = out.split("## Slide 5: Embedded Excel")[1]
    assert "**Embedded xlsx:" in slide
    assert "| 2 | Apples | 3 | 2.5 | 7.5 |" in slide


# ------------------------------------------------------------------ html
def test_html(md):
    out = md("html")
    assert "# Quarterly report" in out
    assert "This is **bold**, *italic* and a [link](https://example.com)." in out
    assert "- First\n- Second\n    - Nested" in out
    assert "1. One\n2. Two" in out
    assert "Regional sales\n\n| Region | Sales | Sales |" in out
    assert "| North | Q2 | 20 \\| twenty |" in out  # rowspan repeated, pipe escaped
    assert "![Red box](" in out and "*Figure 1: a red box*" in out
    assert '```python\nprint("hi")\n```' in out
    assert "> Quoted text" in out
    assert "![remote](https://example.com/remote.png)" in out
    assert "var x" not in out and "color: red" not in out


# ------------------------------------------------------------------ pdf
def test_pdf_text_structure(md):
    out = md("pdf")
    assert "# Annual Report" in out
    assert "## Overview" in out
    assert "Revenue grew strongly in every region" in out
    assert "- Revenue up 20%\n- Costs flat\n- Headcount up 5%" in out
    assert "<!-- page 2 -->" in out


def test_pdf_table_not_duplicated(md):
    out = md("pdf")
    assert "| Region | Revenue | Cost |" in out
    assert "| North | 100 | 60 |" in out
    assert out.count("North") == 1


def test_pdf_headers_footers_removed(md):
    out = md("pdf")
    assert "ACME Corp" not in out
    assert "Page 2" not in out
    assert "ACME Corp" in md("pdf", pdf_strip_headers_footers=False)


def test_pdf_images_and_vector_figures(md):
    out = md("pdf")
    assert "page002_img1" in out
    assert "page002_figure1" in out
    assert "page002_figure1" not in md("pdf", pdf_vector_graphics=False)


def test_pdf_ruling_check():
    import pymupdf

    from f2m.parsers.pdf import has_rulings

    def drawings(draw) -> list:
        doc = pymupdf.open()
        page = doc.new_page()
        draw(page)
        return page.get_drawings(), page.rect.width * page.rect.height

    def grid(p):
        for i in range(3):
            p.draw_line((50, 100 + 20 * i), (250, 100 + 20 * i))
            p.draw_line((50 + 100 * i, 100), (50 + 100 * i, 140))

    def cells_drawn_separately(p):  # each cell's border drawn as its own rectangle
        for r in range(2):
            for c in range(2):
                p.draw_rect(pymupdf.Rect(50 + 80 * c, 100 + 20 * r, 130 + 80 * c, 120 + 20 * r))

    def framed_chart(p):  # content frame + chart axes with ticks: no table possible
        p.draw_rect(pymupdf.Rect(40, 40, 360, 300))
        p.draw_line((80, 250), (300, 250))
        p.draw_line((80, 250), (80, 80))
        for x in range(100, 300, 40):
            p.draw_line((x, 250), (x, 256))

    assert has_rulings(*drawings(grid))
    assert has_rulings(*drawings(cells_drawn_separately))
    assert not has_rulings(*drawings(lambda p: p.draw_rect(pymupdf.Rect(50, 50, 200, 120))))  # 1 cell
    assert not has_rulings(*drawings(framed_chart))
    assert not has_rulings(*drawings(lambda p: p.draw_line((50, 100), (500, 100))))  # one underline
    assert not has_rulings(*drawings(lambda p: p.draw_rect(p.rect, fill=(1, 1, 1))))  # page background
    assert not has_rulings(*drawings(lambda p: p.insert_text((50, 50), "text only")))


def _pdf_with_images(path):
    """3 pages showing the same logo, plus one transparent (soft-masked) image."""
    import io

    import pymupdf
    from make_fixtures import png_bytes
    from PIL import Image

    rgba = Image.new("RGBA", (80, 60), (255, 0, 0, 128))
    buf = io.BytesIO()
    rgba.save(buf, "PNG")
    doc = pymupdf.open()
    logo = png_bytes((0, 128, 128), (100, 50))
    for i in range(3):
        page = doc.new_page()
        page.insert_text((50, 50), f"Page {i + 1} text")
        page.insert_image(pymupdf.Rect(50, 100, 250, 200), stream=logo)
    doc[2].insert_image(pymupdf.Rect(50, 300, 210, 420), stream=buf.getvalue())
    doc.save(path)
    return path


def test_pdf_images_lazy_and_extracted_once(tmp_path, monkeypatch):
    from f2m import Pipeline
    from f2m.images import DropHandler
    from f2m.parsers import pdf as pdf_mod

    path = _pdf_with_images(tmp_path / "imgs.pdf")
    calls: list[int] = []
    real = pdf_mod._PdfImages._extract
    monkeypatch.setattr(pdf_mod._PdfImages, "_extract", lambda self, x: calls.append(x) or real(self, x))

    # --images none: nothing is decoded
    res = Pipeline(image_handler=DropHandler()).convert_file(path)
    assert res.ok and len(res.document.images()) == 4
    assert calls == [] and all(not i.data for i in res.document.images())

    # default: the logo shown on 3 pages is extracted once; the masked image is kept
    res = Pipeline().convert_file(path, tmp_path / "out")
    assert res.ok, res.error
    assert sorted(calls) == sorted(set(calls)) and len(calls) == 2
    assert all(i.data for i in res.document.images())
    assert not [w for w in res.document.warnings if "could not be" in w]
    assets = list((tmp_path / "out" / "imgs_assets").iterdir())
    assert len(assets) == 2  # same logo written once


def test_pdf_text_inside_figure_kept(tmp_path):
    """A framed box with a diagram and a paragraph: the paragraph stays as text, labels stay in the image."""
    import pymupdf

    from f2m import Pipeline

    doc = pymupdf.open()
    page = doc.new_page()
    page.draw_rect(pymupdf.Rect(40, 80, 560, 400))
    for i in range(4):  # diagram boxes
        page.draw_rect(pymupdf.Rect(320 + 50 * (i % 2), 120 + 60 * i, 360 + 50 * (i % 2), 160 + 60 * i))
    page.insert_textbox(pymupdf.Rect(60, 100, 300, 300),
                        "A typical problem can be broken down in three parts that we describe here.")
    page.insert_text((330, 145), "State")
    path = tmp_path / "boxed.pdf"
    doc.save(path)
    res = Pipeline().convert_file(path, tmp_path / "out")
    assert res.ok, res.error
    assert "figure1" in res.markdown
    assert "broken down in three parts" in res.markdown
    assert "State" not in res.markdown


def test_pdf_line_breaks_and_toc(tmp_path):
    import pymupdf

    from f2m import Pipeline

    doc = pymupdf.open()
    page = doc.new_page()
    body = ("This paragraph is long enough to wrap over several lines of the text column, so the "
            "lines must be joined with spaces and not broken apart in the Markdown output at all.")
    page.insert_textbox(pymupdf.Rect(72, 60, 520, 140), body, fontsize=11)
    # address: three short lines in one block
    page.insert_text((72, 170), "Jane Tan\n12 Example Road\nSingapore 123456", fontsize=11)
    # table of contents with leader dots, a sub-entry and the page number on the same row
    for i, (x, title, pg) in enumerate([(72, "1 Introduction", 3), (90, "1.1 Background", 4), (72, "2 Methods", 7)]):
        y = 260 + 18 * i
        page.insert_text((x, y), title + " " + "." * (60 - len(title)), fontsize=11)
        page.insert_text((500, y), str(pg), fontsize=11)
    path = tmp_path / "lines.pdf"
    doc.save(path)
    out = Pipeline().convert_file(path).markdown

    assert "wrap over several lines of the text column, so the lines must be joined" in out
    assert "Jane Tan\\\n12 Example Road\\\nSingapore 123456" in out
    assert "- 1 Introduction … 3\n    - 1.1 Background … 4\n- 2 Methods … 7" in out


def test_pdf_paragraph_split_across_blocks_is_joined():
    from f2m.document import Inline, Paragraph
    from f2m.parsers.pdf import _merge_paragraphs

    out = _merge_paragraphs([Paragraph([Inline("assists with operational")]), Paragraph([Inline("issues, including")]),
                             Paragraph("A new sentence.")])
    assert len(out) == 2 and out[0].text[-1].text == "issues, including"
    out = _merge_paragraphs([Paragraph("Ends a sentence."), Paragraph("lowercase start")])
    assert len(out) == 2


# ------------------------------------------------------------------ image
def test_image(md, tmp_path):
    out = md("png")
    assert out.strip() == "![sample](sample_assets/sample.png)"
    assert (tmp_path / "sample_assets" / "sample.png").exists()
