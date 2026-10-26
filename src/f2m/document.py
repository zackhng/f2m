"""Format-independent document model.

Parsers turn a file into a ``Document``: an ordered list of blocks. They never
write Markdown themselves; ``f2m.render.markdown`` does that in one place.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Union

from f2m.timing import timed


@dataclass
class Inline:
    """A run of text with one set of formatting."""

    text: str
    bold: bool = False
    italic: bool = False
    strike: bool = False
    code: bool = False
    link: str | None = None
    raw: bool = False  # already Markdown; inserted verbatim

    def style(self) -> tuple:
        return (self.bold, self.italic, self.strike, self.code, self.link, self.raw)


# Text content: either plain text (escaped when rendered) or formatted runs.
Text = Union[str, "list[Inline]"]


def plain(text: Text) -> str:
    """Text content without formatting."""
    if isinstance(text, str):
        return text
    return "".join(i.text for i in text)


class Block:
    """Base class for all blocks."""


@dataclass
class Heading(Block):
    level: int
    text: Text


@dataclass
class Paragraph(Block):
    text: Text


@dataclass
class ListItem:
    text: Text
    level: int = 0
    ordered: bool = False


@dataclass
class ListBlock(Block):
    items: list[ListItem] = field(default_factory=list)


@dataclass
class Table(Block):
    """A rectangular grid of cells. The first row is the header row."""

    rows: list[list[Text]]
    caption: Text | None = None
    has_header: bool = True


@dataclass
class CodeBlock(Block):
    text: str
    language: str = ""


@dataclass
class Quote(Block):
    text: Text
    label: str | None = None  # e.g. "Notes"


@dataclass
class ImageBlock(Block):
    """An image that needs an ImageHandler to become Markdown.

    ``markdown`` is filled in by the pipeline after parsing.

    A parser can pass ``loader`` instead of ``data`` so the image is only
    decoded if a handler needs it (``--images none`` then costs nothing).
    Handlers read the bytes with ``load()``.
    """

    data: bytes = b""
    mime: str = "application/octet-stream"
    name_hint: str = "image"
    alt: str = ""
    caption: str = ""
    location: dict[str, Any] = field(default_factory=dict)
    markdown: str | None = None
    # Returns (bytes, mime). Called at most once, by load().
    loader: Callable[[], tuple[bytes, str]] | None = field(default=None, repr=False, compare=False)

    def load(self) -> bytes:
        if self.loader is not None:
            loader, self.loader = self.loader, None
            with timed("decode"):
                data, mime = loader()
            self.data = data
            if mime:
                self.mime = mime
        return self.data


@dataclass
class PageBreak(Block):
    label: str = ""


@dataclass
class HorizontalRule(Block):
    pass


@dataclass
class RawMarkdown(Block):
    text: str


@dataclass
class EmbeddedDocument(Block):
    """Another document found inside this one (e.g. an xlsx inside a pptx)."""

    document: Document
    title: str = ""


@dataclass
class Document:
    blocks: list[Block] = field(default_factory=list)
    source: str = ""
    format: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def add(self, *blocks: Block | None) -> None:
        for b in blocks:
            if b is not None:
                self.blocks.append(b)

    def extend(self, blocks: list[Block]) -> None:
        self.blocks.extend(b for b in blocks if b is not None)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def iter_blocks(self) -> Iterator[Block]:
        """All blocks, including those inside embedded documents."""
        for b in self.blocks:
            yield b
            if isinstance(b, EmbeddedDocument):
                yield from b.document.iter_blocks()

    def images(self) -> list[ImageBlock]:
        return [b for b in self.iter_blocks() if isinstance(b, ImageBlock)]
