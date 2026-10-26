"""Shared table building: cells with spans/merges -> rectangular grid -> Table."""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass
from decimal import Decimal

from f2m.document import Table, Text, plain


@dataclass
class Cell:
    text: Text = ""
    colspan: int = 1
    rowspan: int = 1
    # True for a cell that continues the merge of the cell above (docx vMerge).
    continues_above: bool = False


def build_grid(rows: list[list[Cell]], merged: str = "repeat") -> list[list[Text]]:
    """Expand spans into a full grid. ``merged`` is 'repeat' or 'blank'."""
    shown: dict[tuple[int, int], Text] = {}  # what each grid position displays
    origin: dict[tuple[int, int], Text] = {}  # the merged cell's own text, for copying down
    for r, row in enumerate(rows):
        c = 0
        for cell in row:
            while (r, c) in shown:
                c += 1
            span = max(1, cell.colspan)
            if cell.continues_above:
                for dc in range(span):
                    val = origin.get((r - 1, c + dc), "")
                    shown[(r, c + dc)] = val if merged == "repeat" else ""
                    origin[(r, c + dc)] = val
                c += span
                continue
            for dr in range(max(1, cell.rowspan)):
                for dc in range(span):
                    first = dr == 0 and dc == 0
                    shown[(r + dr, c + dc)] = cell.text if (first or merged == "repeat") else ""
                    origin[(r + dr, c + dc)] = cell.text
            c += span
    if not shown:
        return []
    nrows = max(r for r, _ in shown) + 1
    ncols = max(c for _, c in shown) + 1
    return [[shown.get((r, c), "") for c in range(ncols)] for r in range(nrows)]


def is_blank(t: Text) -> bool:
    return not plain(t).strip()


def trim_grid(grid: list[list[Text]]) -> list[list[Text]]:
    """Drop rows and columns that are completely empty."""
    rows = [r for r in grid if not all(is_blank(c) for c in r)]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    keep = [c for c in range(width) if not all(is_blank(r[c]) for r in rows)]
    return [[r[c] for c in keep] for r in rows]


def dedupe_spanned_columns(grid: list[list[Text]]) -> list[list[Text]]:
    """Merge neighbouring columns that are identical in every row (common in PDFs)."""
    if not grid:
        return grid
    width = len(grid[0])
    keep = [0] + [c for c in range(1, width) if any(plain(r[c]) != plain(r[c - 1]) for r in grid)]
    return [[r[c] for c in keep] for r in grid]


def make_table(grid: list[list[Text]], caption: Text | None = None, trim: bool = True) -> Table | None:
    if trim:
        grid = trim_grid(grid)
    if not grid:
        return None
    return Table(rows=grid, caption=caption)


def fmt_value(v: object) -> str:
    """Readable text for a cell/chart value."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return str(v)
        if v.is_integer() and abs(v) < 1e15:
            return str(int(v))
        s = repr(v)
        if "e" not in s and len(s.split(".")[-1]) > 10:
            # Float noise like 0.30000000000000004 -> 0.3
            s = format(Decimal(repr(round(v, 10))).normalize(), "f")
        return s
    if isinstance(v, _dt.datetime):
        if v.time() == _dt.time(0, 0):
            return v.date().isoformat()
        return v.isoformat(sep=" ")
    if isinstance(v, (_dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, _dt.timedelta):
        return str(v)
    return str(v)


def fmt_number_string(s: str) -> str:
    """Tidy a number stored as text (chart caches): '12.300000000000001' -> '12.3'."""
    try:
        f = float(s)
    except (TypeError, ValueError):
        return s
    return fmt_value(f)
