"""Options and the context object handed to every parser."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from f2m.document import Document


@dataclass
class Options:
    # Merged table cells: repeat the value in every covered cell, or leave them blank.
    merged_cells: Literal["repeat", "blank"] = "repeat"
    # How deep embedded files (an xlsx inside a pptx inside a docx...) are followed.
    max_depth: int = 3

    # XLSX
    xlsx_formulas: Literal["section", "inline", "none"] = "section"
    xlsx_dependencies: bool = False
    xlsx_compute_missing: bool = False  # needs the [calc] extra
    xlsx_max_rows: int | None = None  # per sheet; None = all rows
    xlsx_embedded_full: bool = False  # embedded workbooks: whole workbook, not just the visible region

    # PPTX
    pptx_notes: bool = True

    # PDF
    pdf_vector_graphics: bool = True  # render clusters of vector drawings (charts) as images
    pdf_strip_headers_footers: bool = True
    pdf_min_image_size: int = 24  # px; smaller images are treated as decoration


@dataclass
class ParseContext:
    """What a parser knows about the file it is parsing."""

    name: str  # file name (no directories), used for titles and asset names
    options: Options = field(default_factory=Options)
    path: Path | None = None  # set when the input came from disk
    depth: int = 0
    embedded: bool = False  # True when this file was found inside another file
    # Callback that parses nested file bytes; set by the pipeline.
    _convert: Callable[[bytes, str, ParseContext], Document | None] | None = None

    @property
    def stem(self) -> str:
        return Path(self.name).stem

    def convert_embedded(self, data: bytes, name: str) -> Document | None:
        """Parse a file found inside the current one.

        Returns None if the type is unsupported, the depth limit is reached, or
        no pipeline is attached.
        """
        if self._convert is None or self.depth >= self.options.max_depth:
            return None
        child = ParseContext(
            name=name,
            options=self.options,
            depth=self.depth + 1,
            embedded=True,
            _convert=self._convert,
        )
        return self._convert(data, name, child)
