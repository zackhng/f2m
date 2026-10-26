"""Time a folder of files and report where the time goes.

Writes three files to the report folder:
- files.csv     one row per file: size, pages, status, total and per-step ms
- timings.json  the same plus the full nested step timings for each file
- summary.md    totals per format, step breakdown (with shares), slowest files, failures
"""

from __future__ import annotations

import csv
import json
import platform
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from f2m.document import ImageBlock, Table
from f2m.pipeline import Pipeline, Result

TOP_STEPS = ("read", "parse", "images", "render", "write")
TAB = "\t"


@dataclass
class Row:
    file: str
    format: str
    size_kb: float
    units: int  # pages / slides / sheets (0 if not applicable)
    status: str
    error: str = ""
    total_ms: float = 0.0
    steps: dict[str, float] = field(default_factory=dict)  # step path -> ms
    calls: dict[str, int] = field(default_factory=dict)
    images: int = 0
    tables: int = 0
    warnings: int = 0
    md_chars: int = 0
    output: str = ""  # the .md written, if any


def _units(res: Result) -> int:
    meta = res.document.metadata if res.document else {}
    for key in ("pages", "slides"):
        if isinstance(meta.get(key), int):
            return meta[key]
    if isinstance(meta.get("sheets"), list):
        return len(meta["sheets"])
    return 0


def _row(res: Result, root: Path) -> Row:
    src = res.source
    rel = str(src.relative_to(root)) if src and root in src.parents else str(src)
    doc = res.document
    t = res.timings
    steps = t.ms() if t else {}
    fmt = doc.format if doc else next((k.split("/")[1] for k in steps if k.startswith("parse/") and k.count("/") == 1 and k != "parse/detect"), "")
    return Row(
        file=rel,
        format=fmt or (src.suffix.lstrip(".").lower() if src else ""),
        size_kb=round(src.stat().st_size / 1024, 1) if src else 0,
        units=_units(res),
        status="ok" if res.ok else "failed",
        error=res.error or "",
        total_ms=t.total_ms() if t else 0.0,
        steps=steps,
        calls=dict(t.calls) if t else {},
        images=sum(1 for b in doc.iter_blocks() if isinstance(b, ImageBlock)) if doc else 0,
        tables=sum(1 for b in doc.iter_blocks() if isinstance(b, Table)) if doc else 0,
        warnings=len(doc.warnings) if doc else 0,
        md_chars=len(res.markdown),
        output=str(res.output) if res.output else "",
    )


def run(
    pipe: Pipeline,
    folder: Path,
    out_dir: Path | None,
    recursive: bool = True,
    warmup: bool = True,
    progress: Callable[[int, int, Row], None] | None = None,
    log_path: Path | None = None,
) -> list[Row]:
    """Convert every file and time it. With ``log_path``, one line per file is
    appended as soon as that file is done, so the log survives a stopped run."""
    folder = Path(folder)
    files = [folder] if folder.is_file() else pipe.find_files(folder, recursive)
    root = folder if folder.is_dir() else folder.parent

    if warmup:  # first call per format pays one-off library costs; don't count it
        done: set[str] = set()
        for f in files:
            ext = f.suffix.lower()
            if ext not in done:
                done.add(ext)
                pipe.convert_file(f, None)

    stems: dict[tuple[Path, str], int] = defaultdict(int)
    for f in files:
        stems[(f.parent, f.stem.lower())] += 1
    rows = []
    log = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(log_path, "w", encoding="utf-8", newline="")
        log.write(TAB.join(LOG_COLUMNS) + "\n")
    try:
        for i, f in enumerate(files, 1):
            stem = f.stem if stems[(f.parent, f.stem.lower())] == 1 else f"{f.stem}_{f.suffix.lstrip('.').lower()}"
            target = (out_dir / f.parent.relative_to(root)) if out_dir is not None else None
            res = pipe.convert_file(f, target, stem=stem)
            row = _row(res, root)
            rows.append(row)
            if log is not None:
                log.write(log_line(row) + "\n")
                log.flush()
            if progress:
                progress(i, len(files), row)
    finally:
        if log is not None:
            log.close()
    return rows


LOG_COLUMNS = ("time", "status", "total_ms", *(f"{s}_ms" for s in TOP_STEPS), "format", "size_kb", "units",
               "images", "tables", "file", "output", "error")


def log_line(r: Row) -> str:
    """One tab-separated latency line for a file."""
    vals = [time.strftime("%Y-%m-%d %H:%M:%S"), r.status, f"{r.total_ms:.1f}",
            *(f"{r.steps.get(s, 0):.1f}" for s in TOP_STEPS),
            r.format, str(r.size_kb), str(r.units), str(r.images), str(r.tables), r.file, r.output,
            r.error.replace(TAB, " ").replace("\n", " ")]
    return TAB.join(vals)


# ---------------------------------------------------------------- reports
def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _fmt_ms(ms: float) -> str:
    return f"{ms / 1000:.2f} s" if ms >= 10000 else f"{ms:.1f} ms"


def write_reports(rows: list[Row], report_dir: Path, settings: dict) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)

    # files.csv: top-level steps plus the parser's own steps (two levels below parse/)
    step_cols = sorted({k for r in rows for k in r.steps if k.count("/") <= 2})
    with open(report_dir / "files.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["file", "format", "size_kb", "units", "status", "total_ms", "images", "tables",
                    "warnings", "md_chars", *step_cols, "error"])
        for r in rows:
            w.writerow([r.file, r.format, r.size_kb, r.units, r.status, r.total_ms, r.images, r.tables,
                        r.warnings, r.md_chars, *[r.steps.get(c, "") for c in step_cols], r.error])

    with open(report_dir / "timings.json", "w", encoding="utf-8") as f:
        json.dump({"settings": settings, "files": [r.__dict__ for r in rows]}, f, indent=1)

    (report_dir / "summary.md").write_text(summary(rows, settings), encoding="utf-8")
    return report_dir


@dataclass
class _StepStats:
    """Per-format step data: per-file values (0 when a file didn't run the step)."""

    rows: list[Row]
    total: float = 0.0
    calls: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    per_file: dict[str, list[float]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        paths = list(dict.fromkeys(k for r in self.rows for k in r.steps))
        for p in paths:
            self.per_file[p] = [r.steps.get(p, 0.0) for r in self.rows]
            self.calls[p] = sum(r.calls.get(p, 0) for r in self.rows)
        self.total = sum(sum(v) for k, v in self.per_file.items() if "/" not in k) or 1.0
        self.has_units = any(r.units for r in self.rows)

    def sum(self, path: str) -> float:
        return sum(self.per_file.get(path, []))

    def cols(self, values: list[float]) -> str:
        """Total | Share | Median/file | p95/file | Max/file [| Median/page]"""
        s = sum(values)
        cells = [_fmt_ms(s), f"{100 * s / self.total:.1f}%", _fmt_ms(_pct(values, .5)),
                 _fmt_ms(_pct(values, .95)), _fmt_ms(max(values) if values else 0)]
        if self.has_units:
            per_unit = [v / r.units for v, r in zip(values, self.rows) if r.units]
            cells.append(f"{_pct(per_unit, .5):.2f} ms" if per_unit else "–")
        return " | ".join(cells)

    def header(self) -> list[str]:
        cols = ["Step", "Total", "Share", "Median / file", "p95 / file", "Max / file"]
        if self.has_units:
            cols.append("Median / page")
        cols.append("Calls")
        return ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]


def _step_tree(path: str, depth: int, st: _StepStats) -> list[str]:
    """Table rows for a step and its sub-steps, largest first."""
    v = st.sum(path)
    if v / st.total < 0.002 and depth > 0:
        return []
    name = path.rsplit("/", 1)[-1]
    lines = [f"| {'· ' * depth}{name} | {st.cols(st.per_file[path])} | {st.calls[path]} |"]
    kids = [k for k in st.per_file if k.startswith(path + "/") and k.count("/") == path.count("/") + 1]
    for k in sorted(kids, key=lambda k: -st.sum(k)):
        lines += _step_tree(k, depth + 1, st)
    if kids:
        other = [max(0.0, a - sum(st.per_file[k][i] for k in kids)) for i, a in enumerate(st.per_file[path])]
        if sum(other) / st.total >= 0.01:
            lines.append(f"| {'· ' * (depth + 1)}(other) | {st.cols(other)} | |")
    return lines


def summary(rows: list[Row], settings: dict) -> str:
    ok = [r for r in rows if r.status == "ok"]
    out: list[str] = ["# f2m timing report", ""]
    out.append(" · ".join(f"{k}: {v}" for k, v in settings.items()))
    total = sum(r.total_ms for r in rows)
    out += ["", f"**{len(rows)} files, {len(ok)} ok, {len(rows) - len(ok)} failed, "
                f"{total / 1000:.1f} s total.**", ""]

    # per format
    out += ["## By format", "", "| Format | Files | Total | Mean | p50 | p95 | Max | ms per page/slide/sheet | MB/s |",
            "|---|---|---|---|---|---|---|---|---|"]
    by_fmt: dict[str, list[Row]] = defaultdict(list)
    for r in ok:
        by_fmt[r.format].append(r)
    for fmt, rs in sorted(by_fmt.items(), key=lambda kv: -sum(r.total_ms for r in kv[1])):
        t = [r.total_ms for r in rs]
        units = sum(r.units for r in rs)
        mb = sum(r.size_kb for r in rs) / 1024
        per_unit = f"{sum(t) / units:.1f}" if units else "–"
        out.append(f"| {fmt} | {len(rs)} | {_fmt_ms(sum(t))} | {_fmt_ms(statistics.mean(t))} | "
                   f"{_fmt_ms(_pct(t, .5))} | {_fmt_ms(_pct(t, .95))} | {_fmt_ms(max(t))} | {per_unit} | "
                   f"{mb / (sum(t) / 1000):.2f} |" if sum(t) else f"| {fmt} | {len(rs)} | 0 | | | | | | |")

    # step breakdown per format, as a tree with "(other)" for time not covered by a child step
    out += ["", "## Where the time goes", "",
            "Indented steps are part of the step above; `(other)` is time inside a step that no smaller step "
            "covers. Total and Share are summed over all files of the format. Median / p95 / Max are taken "
            "over files (a file that never ran the step counts as 0). Median / page divides each file's "
            "time by its page (slide, sheet) count first.", ""]
    for fmt, rs in sorted(by_fmt.items(), key=lambda kv: -sum(r.total_ms for r in kv[1])):
        st = _StepStats(rs)
        out += [f"### {fmt} ({len(rs)} files, {_fmt_ms(st.total)})", "", *st.header()]
        for top in TOP_STEPS:
            if top in st.per_file:
                out += _step_tree(top, 0, st)
        out.append("")

    # slowest files
    out += ["## Slowest files", "", "| File | Format | Size | Units | Total | Slowest step |", "|---|---|---|---|---|---|"]
    for r in sorted(ok, key=lambda r: -r.total_ms)[:15]:
        leaf = {k: v for k, v in r.steps.items() if not any(o.startswith(k + "/") for o in r.steps)}
        step, ms = max(leaf.items(), key=lambda kv: kv[1]) if leaf else ("", 0)
        out.append(f"| {r.file} | {r.format} | {r.size_kb / 1024:.1f} MB | {r.units} | {_fmt_ms(r.total_ms)} | "
                   f"`{step}` ({_fmt_ms(ms)}) |")

    failed = [r for r in rows if r.status != "ok"]
    if failed:
        out += ["", "## Failures", "", "| File | Error |", "|---|---|"]
        out += [f"| {r.file} | {r.error.replace('|', '/')[:200]} |" for r in failed]

    warned = sorted((r for r in ok if r.warnings), key=lambda r: -r.warnings)
    if warned:
        out += ["", "## Files with warnings", "", "| File | Warnings |", "|---|---|"]
        out += [f"| {r.file} | {r.warnings} |" for r in warned[:20]]
    return "\n".join(out) + "\n"


def _md_text(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8") if path else None
    except OSError:
        return None


def _strip_asset_dirs(md: str) -> str:
    """Output folders differ between runs; compare Markdown without the asset folder prefix."""
    import re

    return re.sub(r"\]\([^)]*?_assets/", "](_assets/", md)


def compare(base: list[Row], new: list[Row], base_name: str = "before", new_name: str = "after") -> str:
    """Markdown report: speed change per format and step, and files whose output changed."""
    b_by, n_by = {r.file: r for r in base}, {r.file: r for r in new}
    common = [f for f in b_by if f in n_by and b_by[f].status == "ok" and n_by[f].status == "ok"]
    out = [f"# Comparison: {base_name} → {new_name}", "",
           f"{len(common)} files in both runs (ok in both)."]
    only = sorted(set(b_by) ^ set(n_by))
    if only:
        out.append(f"{len(only)} files only in one run: {', '.join(only[:10])}{' …' if len(only) > 10 else ''}")

    def delta(a: float, b: float) -> str:
        return f"{100 * (b - a) / a:+.0f}%" if a else "–"

    out += ["", "## Total time by format", "",
            "| Format | Files | Total before | Total after | Change | Median before | Median after | Change |",
            "|---|---|---|---|---|---|---|---|"]
    fmts = sorted({b_by[f].format for f in common})
    for fmt in fmts:
        fs = [f for f in common if b_by[f].format == fmt]
        ta, tb = [b_by[f].total_ms for f in fs], [n_by[f].total_ms for f in fs]
        out.append(f"| {fmt} | {len(fs)} | {_fmt_ms(sum(ta))} | {_fmt_ms(sum(tb))} | {delta(sum(ta), sum(tb))} | "
                   f"{_fmt_ms(_pct(ta, .5))} | {_fmt_ms(_pct(tb, .5))} | {delta(_pct(ta, .5), _pct(tb, .5))} |")

    out += ["", "## Steps (two levels below the parser)", "",
            "| Format | Step | Total before | Total after | Change | Median before | Median after |",
            "|---|---|---|---|---|---|---|"]
    for fmt in fmts:
        fs = [f for f in common if b_by[f].format == fmt]
        paths = list(dict.fromkeys(k for f in fs for r in (b_by[f], n_by[f]) for k in r.steps
                                   if k.count("/") <= 4))
        for p in paths:
            a = [b_by[f].steps.get(p, 0.0) for f in fs]
            b = [n_by[f].steps.get(p, 0.0) for f in fs]
            if max(sum(a), sum(b)) < 0.002 * sum(b_by[f].total_ms for f in fs):
                continue
            out.append(f"| {fmt} | {p} | {_fmt_ms(sum(a))} | {_fmt_ms(sum(b))} | {delta(sum(a), sum(b))} | "
                       f"{_fmt_ms(_pct(a, .5))} | {_fmt_ms(_pct(b, .5))} |")

    # output changes: a speed-up must not change what gets extracted
    changed = []
    same_md = compared_md = 0
    for f in common:
        a, b = b_by[f], n_by[f]
        diffs = [f"{k} {getattr(a, k)} → {getattr(b, k)}" for k in ("tables", "images", "warnings")
                 if getattr(a, k) != getattr(b, k)]
        ma, mb = _md_text(a.output), _md_text(b.output)
        if ma is not None and mb is not None:
            compared_md += 1
            if _strip_asset_dirs(ma) == _strip_asset_dirs(mb):
                same_md += 1
            else:
                diffs.append(f"markdown differs ({len(ma)} → {len(mb)} chars)")
        elif a.md_chars != b.md_chars:
            diffs.append(f"md_chars {a.md_chars} → {b.md_chars}")
        if diffs:
            changed.append((f, "; ".join(diffs)))
    out += ["", "## Output changes", ""]
    if compared_md:
        out.append(f"Markdown compared for {compared_md} files: {same_md} identical, {compared_md - same_md} differ.")
    if changed:
        out += ["", "| File | What changed |", "|---|---|"] + [f"| {f} | {d} |" for f, d in changed]
    else:
        out.append("No file's tables, images, warnings or Markdown changed.")
    return "\n".join(out) + "\n"


def load_rows(timings_json: Path) -> tuple[list[Row], dict]:
    """Rows and settings from a saved timings.json (to rebuild a report without re-running)."""
    data = json.loads(Path(timings_json).read_text(encoding="utf-8"))
    names = set(Row.__dataclass_fields__)
    rows = [Row(**{k: v for k, v in f.items() if k in names}) for f in data["files"]]
    return rows, data.get("settings", {})


def settings_info(pipe: Pipeline, **extra) -> dict:
    return {
        "date": time.strftime("%Y-%m-%d %H:%M"),
        "python": platform.python_version(),
        "machine": f"{platform.system()} {platform.machine()}",
        "image_handler": type(pipe.image_handler).__name__,
        **extra,
        "argv": " ".join(sys.argv[1:]),
    }
