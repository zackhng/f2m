"""Folder conversion, output naming, image handlers and the CLI."""

from __future__ import annotations

import shutil

from typer.testing import CliRunner

from f2m import Options, Pipeline, convert
from f2m.cli import app
from f2m.images import CompositeHandler, SaveAndLinkHandler


def test_convert_folder_names_and_assets(files, tmp_path):
    src = files["png"].parent
    out = tmp_path / "out"
    results = Pipeline().convert_path(src, out)
    assert all(r.ok for r in results), [r.error for r in results if not r.ok]
    names = sorted(p.name for p in out.glob("*.md"))
    # sample.* share a stem, so the extension is added
    assert "sample_docx.md" in names and "sample_pdf.md" in names and "uncached.md" in names
    assert any((out / "sample_pptx_assets").iterdir())


def test_recursive_mirrors_subfolders(files, tmp_path):
    src = tmp_path / "in"
    (src / "sub").mkdir(parents=True)
    shutil.copy(files["html"], src / "sub" / "page.html")
    (src / "ignored.txt").write_text("no parser for this")
    out = tmp_path / "out"
    assert Pipeline().convert_path(src, out) == []
    results = Pipeline().convert_path(src, out, recursive=True)
    assert [r.output for r in results] == [out / "sub" / "page.md"]


def test_image_handler_is_pluggable(files):
    seen = []

    class FakeModel:
        def handle(self, image, ctx):
            seen.append(image.location)
            return f"<!-- model output for {image.name_hint} -->"

    md = Pipeline(image_handler=CompositeHandler(SaveAndLinkHandler(), FakeModel())).convert_file(files["pptx"]).markdown
    assert "<!-- model output for slide004_img1 -->" in md
    assert {"slide": 4} in seen
    # images from the embedded workbook reach the handler as well
    assert any(loc.get("sheet") == "Sales" for loc in seen)


def test_unsupported_file_reports_error(tmp_path):
    f = tmp_path / "x.unknown"
    f.write_bytes(b"\x00\x01binary")
    res = Pipeline().convert_file(f)
    assert not res.ok and "unsupported" in res.error


def test_convert_helper(files, tmp_path):
    out = convert(files["html"], tmp_path, options=Options(), image_handler="none")
    assert "# Quarterly report" in out and "![Red box]" not in out
    assert (tmp_path / "sample.md").exists()


def test_cli(files, tmp_path):
    runner = CliRunner()
    r = runner.invoke(app, ["convert", str(files["xlsx"]), "--xlsx-formulas", "inline"])
    assert r.exit_code == 0, r.output
    assert "7.5 `=B2*C2`" in r.output
    r = runner.invoke(app, ["convert", str(files["png"].parent), "-o", str(tmp_path / "o")])
    assert r.exit_code == 0, r.output
    assert "7 converted, 0 failed" in r.output
    r = runner.invoke(app, ["formats"])
    assert "pptx" in r.output
