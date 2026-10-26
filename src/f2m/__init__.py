"""f2m: convert PDF, DOCX, PPTX, XLSX, HTML and images to Markdown."""

from __future__ import annotations

from pathlib import Path

from f2m.context import Options, ParseContext
from f2m.document import Document
from f2m.images import ImageHandler, make_handler
from f2m.pipeline import Pipeline, Result, UnsupportedFormat
from f2m.registry import Parser, register

__all__ = [
    "Document",
    "ImageHandler",
    "Options",
    "ParseContext",
    "Parser",
    "Pipeline",
    "Result",
    "UnsupportedFormat",
    "convert",
    "register",
]


def convert(
    path: str | Path,
    out_dir: str | Path | None = None,
    *,
    options: Options | None = None,
    image_handler: ImageHandler | str = "link",
) -> str:
    """Convert one file and return its Markdown.

    With ``out_dir``, also writes ``<stem>.md`` and the extracted images.
    Raises on failure.
    """
    handler = make_handler(image_handler) if isinstance(image_handler, str) else image_handler
    pipe = Pipeline(options=options or Options(), image_handler=handler)
    res = pipe.convert_file(path, out_dir)
    if res.error:
        raise RuntimeError(res.error)
    return res.markdown
