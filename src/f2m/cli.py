"""Command line: ``f2m convert <input> -o <out_dir>``."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import typer

from f2m.context import Options
from f2m.images import make_handler
from f2m.parallel import default_workers
from f2m.pipeline import Pipeline

app = typer.Typer(add_completion=False, help="Convert documents and images to Markdown.")


@app.command()
def convert(
    input: Path = typer.Argument(..., exists=True, help="File or folder to convert."),
    out: Path = typer.Option(None, "-o", "--out", help="Output folder. Omit to print Markdown (single file only)."),
    recursive: bool = typer.Option(False, "-r", "--recursive", help="Include sub-folders."),
    images: str = typer.Option("link", "--images", help="Image handling: link, ocr, none."),
    xlsx_formulas: str = typer.Option("section", help="XLSX formulas: section, inline, none."),
    xlsx_deps: bool = typer.Option(False, "--xlsx-deps", help="XLSX: list what each formula reads from."),
    xlsx_compute: bool = typer.Option(False, "--xlsx-compute", help="XLSX: compute values missing from the file (needs the calc extra)."),
    merged: str = typer.Option("repeat", "--merged-cells", help="Merged table cells: repeat or blank."),
    no_notes: bool = typer.Option(False, "--no-notes", help="PPTX: leave out speaker notes."),
    no_pdf_graphics: bool = typer.Option(False, "--no-pdf-graphics", help="PDF: don't render vector drawings as images."),
    workers: int = typer.Option(0, "-w", "--workers", help="Files converted at once, one process each. 0 = all CPU cores."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING, format="%(levelname)s %(message)s")
    if xlsx_formulas not in ("section", "inline", "none"):
        raise typer.BadParameter("use section, inline or none", param_hint="--xlsx-formulas")
    if merged not in ("repeat", "blank"):
        raise typer.BadParameter("use repeat or blank", param_hint="--merged-cells")
    opts = Options(
        merged_cells=merged,  # type: ignore[arg-type]
        xlsx_formulas=xlsx_formulas,  # type: ignore[arg-type]
        xlsx_dependencies=xlsx_deps,
        xlsx_compute_missing=xlsx_compute,
        pptx_notes=not no_notes,
        pdf_vector_graphics=not no_pdf_graphics,
    )
    pipe = Pipeline(options=opts, image_handler=make_handler(images))

    if out is None:
        if input.is_dir():
            raise typer.BadParameter("give an output folder with -o when converting a folder")
        res = pipe.convert_file(input)
        if res.error:
            typer.echo(f"error: {res.error}", err=True)
            raise typer.Exit(1)
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        typer.echo(res.markdown)
        return

    results = pipe.convert_path(input, out, recursive=recursive, workers=workers or default_workers())
    failed = 0
    for r in results:
        if r.ok:
            typer.echo(f"ok    {r.source} -> {r.output}")
        else:
            failed += 1
            typer.echo(f"FAIL  {r.source}: {r.error}", err=True)
    typer.echo(f"{len(results) - failed} converted, {failed} failed")
    if failed:
        raise typer.Exit(1)


@app.command()
def bench(
    input: Path = typer.Argument(..., exists=True, help="File or folder to time."),
    out: Path = typer.Option(None, "-o", "--out", help="Also write the Markdown and images here (adds write time)."),
    report: Path = typer.Option(None, "--report", help="Report folder. Default: out/bench/<date-time>."),
    recursive: bool = typer.Option(True, "-r/--no-recursive", help="Include sub-folders (on by default)."),
    images: str = typer.Option("link", "--images", help="Image handling: link, ocr, none."),
    warmup: bool = typer.Option(True, "--warmup/--no-warmup", help="Run one file per format first, untimed."),
    xlsx_formulas: str = typer.Option("section", help="XLSX formulas: section, inline, none."),
    no_pdf_graphics: bool = typer.Option(False, "--no-pdf-graphics", help="PDF: don't render vector drawings."),
    workers: int = typer.Option(1, "-w", "--workers", help="Files timed at once, one process each. 0 = all CPU cores. "
                                "Default 1: parallel files share the CPU, so per-step times are only comparable "
                                "between runs with the same number of workers."),
) -> None:
    """Convert files and report how long each step takes."""
    import time

    from f2m import bench as b

    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(message)s")
    opts = Options(xlsx_formulas=xlsx_formulas, pdf_vector_graphics=not no_pdf_graphics)  # type: ignore[arg-type]
    pipe = Pipeline(options=opts, image_handler=make_handler(images))
    report = report or Path("out") / "bench" / time.strftime("%Y%m%d-%H%M%S")

    def progress(i: int, n: int, row: b.Row) -> None:
        mark = "ok  " if row.status == "ok" else "FAIL"
        typer.echo(f"[{i:>4}/{n}] {mark} {row.total_ms:>10.1f} ms  {row.format:5} {row.file}", err=True)

    typer.echo(f"latency log: {report / 'latency.log'}", err=True)
    started = time.perf_counter()
    rows = b.run(pipe, input, out, recursive=recursive, warmup=warmup, progress=progress,
                 log_path=report / "latency.log", workers=workers or default_workers())
    settings = b.settings_info(pipe, input=str(input), images=images, written=out is not None, warmup=warmup,
                               workers=workers or default_workers())
    folder = b.write_reports(rows, report, settings)
    failed = sum(r.status != "ok" for r in rows)
    typer.echo(f"\n{len(rows)} files, {failed} failed, {sum(r.total_ms for r in rows) / 1000:.1f} s total "
               f"(sum over files), {time.perf_counter() - started:.1f} s wall clock")
    typer.echo(f"report: {folder / 'summary.md'}")


@app.command()
def report(
    timings: Path = typer.Argument(..., exists=True, help="timings.json from an earlier bench run, or its folder."),
) -> None:
    """Rebuild summary.md (and files.csv) from a saved bench run, without re-running it."""
    from f2m import bench as b

    path = timings / "timings.json" if timings.is_dir() else timings
    rows, settings = b.load_rows(path)
    folder = b.write_reports(rows, path.parent, settings)
    typer.echo(f"report: {folder / 'summary.md'}")


@app.command()
def compare(
    before: Path = typer.Argument(..., exists=True, help="Earlier bench report folder (or its timings.json)."),
    after: Path = typer.Argument(..., exists=True, help="Later bench report folder (or its timings.json)."),
    out: Path = typer.Option(None, "-o", "--out", help="Write the comparison here. Default: <after>/compare.md"),
) -> None:
    """Compare two bench runs: speed per format and step, and which files' output changed."""
    from f2m import bench as b

    def path(p: Path) -> Path:
        return p / "timings.json" if p.is_dir() else p

    rows_a, _ = b.load_rows(path(before))
    rows_b, _ = b.load_rows(path(after))
    text = b.compare(rows_a, rows_b, path(before).parent.name, path(after).parent.name)
    target = out or path(after).parent / "compare.md"
    target.write_text(text, encoding="utf-8")
    typer.echo(f"comparison: {target}")


@app.command()
def formats() -> None:
    """List the formats that have a parser."""
    from f2m.registry import available

    for fmt, cls in sorted(available().items()):
        typer.echo(f"{fmt:8} {', '.join(cls.extensions):40} {cls.__module__}.{cls.__name__}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
