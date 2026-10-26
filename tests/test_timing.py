"""Step timers and the bench report."""

from __future__ import annotations

from f2m import Pipeline
from f2m import bench as b
from f2m.timing import recording, timed


def test_timed_nests_and_adds_up():
    with recording() as t:
        for _ in range(3):
            with timed("parse"), timed("pages"):
                pass
        with timed("render"):
            pass
    assert t.calls["parse/pages"] == 3
    assert set(t.top_level()) == {"parse", "render"}
    assert t.total_ms() >= 0


def test_timed_is_a_no_op_outside_recording():
    with timed("anything"):
        pass  # must not fail or record


def test_result_carries_step_timings(files):
    res = Pipeline().convert_file(files["pdf"])
    steps = res.timings.ms()
    for key in ("read", "parse", "parse/detect", "parse/pdf", "parse/pdf/pages/tables", "images", "render"):
        assert key in steps, key
    assert "write" not in steps  # nothing written without an output folder


def test_embedded_parse_time_is_nested(files):
    steps = Pipeline().convert_file(files["pptx"]).timings.ms()
    assert any(k.startswith("parse/pptx/slides/ole_objects/embedded/xlsx/") for k in steps)


def test_bench_writes_reports(files, tmp_path):
    rows = b.run(Pipeline(), files["png"].parent, None, warmup=False)
    assert len(rows) == 7 and all(r.status == "ok" for r in rows)
    pdf = next(r for r in rows if r.format == "pdf")
    assert pdf.units == 3 and pdf.tables == 1 and pdf.images == 2
    folder = b.write_reports(rows, tmp_path / "report", b.settings_info(Pipeline()))
    text = (folder / "summary.md").read_text(encoding="utf-8")
    assert "## By format" in text and "### pdf" in text and "· · pages" in text
    header = (folder / "files.csv").read_text(encoding="utf-8").splitlines()[0]
    assert "parse/pdf" in header and "total_ms" in header
    assert (folder / "timings.json").exists()

    # reload and compare a run with itself: nothing changed
    rows2, _ = b.load_rows(folder / "timings.json")
    text = b.compare(rows, rows2)
    assert "No file's tables, images, warnings or Markdown changed." in text
    rows2[0].tables += 1
    assert "tables" in b.compare(rows, rows2).split("## Output changes")[1]
