"""Document model, rendering, tables, detection and the registry."""

from __future__ import annotations

from f2m import Document, Options, ParseContext, Parser, Pipeline, register
from f2m.common.tables import Cell, build_grid, fmt_value, trim_grid
from f2m.detect import detect
from f2m.document import EmbeddedDocument, Heading, Inline, ListBlock, ListItem, Paragraph, Table
from f2m.render.inline import escape, render_text
from f2m.render.markdown import render


def test_escape_keeps_snake_case():
    assert escape("file_name") == "file_name"
    assert escape("_x_") == "\\_x\\_"
    assert escape("2 * 3") == "2 \\* 3"
    assert escape("# not a heading") == "\\# not a heading"


def test_inline_formatting_merges_runs_and_moves_spaces():
    runs = [Inline("Hello ", bold=True), Inline("world", bold=True), Inline(" and "), Inline("link", link="https://x.y")]
    assert render_text(runs) == "**Hello world** and [link](https://x.y)"


def test_table_cells_escape_pipes_and_newlines():
    t = Table(rows=[["a", "b"], ["x | y", "line1\nline2"]])
    out = render(Document([t]))
    assert "x \\| y" in out
    assert "line1<br>line2" in out


def test_list_switches_between_bullets_and_numbers():
    lb = ListBlock([ListItem("a"), ListItem("b", ordered=True), ListItem("c", level=1)])
    assert render(Document([lb])).strip() == "- a\n\n1. b\n    - c"


def test_embedded_headings_nest_under_parent():
    inner = Document([Heading(2, "Sheet"), Heading(3, "Formulas")], format="xlsx")
    doc = Document([Heading(2, "Slide 1"), EmbeddedDocument(inner, title="book.xlsx")])
    out = render(doc)
    assert "### Sheet" in out and "#### Formulas" in out


def test_build_grid_spans_and_merges():
    rows = [[Cell("A", colspan=2), Cell("B", rowspan=2)], [Cell("c"), Cell("d")]]
    assert build_grid(rows) == [["A", "A", "B"], ["c", "d", "B"]]
    assert build_grid(rows, "blank") == [["A", "", "B"], ["c", "d", ""]]
    # docx-style vertical merge
    rows = [[Cell("x"), Cell("y")], [Cell("", continues_above=True), Cell("z")]]
    assert build_grid(rows) == [["x", "y"], ["x", "z"]]


def test_trim_grid_drops_empty_rows_and_columns():
    assert trim_grid([["a", "", "b"], ["", "", ""], ["c", "", "d"]]) == [["a", "b"], ["c", "d"]]


def test_fmt_value():
    assert fmt_value(0.1 + 0.2) == "0.3"
    assert fmt_value(5.0) == "5"
    assert fmt_value(True) == "TRUE"


def test_detect(files):
    expected = {"png": "image", "html": "html", "xlsx": "xlsx", "docx": "docx", "pptx": "pptx", "pdf": "pdf"}
    for key, fmt in expected.items():
        assert detect(files[key].read_bytes(), files[key].name) == fmt
    # content wins over a wrong extension
    assert detect(files["docx"].read_bytes(), "renamed.bin") == "docx"
    assert detect(b"just text", "notes.xyz") is None


def test_custom_parser_plugs_in(tmp_path):
    @register
    class CsvLike(Parser):
        formats = ("semi",)
        extensions = (".semi",)

        def parse(self, data: bytes, ctx: ParseContext) -> Document:
            rows = [line.split(";") for line in data.decode().splitlines()]
            return Document([Paragraph(ctx.stem), Table(rows=rows)])

    f = tmp_path / "data.semi"
    f.write_text("a;b\n1;2")
    res = Pipeline(options=Options()).convert_file(f)
    assert res.ok, res.error
    assert "| a | b |" in res.markdown and "| 1 | 2 |" in res.markdown
