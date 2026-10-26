"""Document -> Markdown (GitHub-flavoured)."""

from __future__ import annotations

from f2m.document import (
    Block,
    CodeBlock,
    Document,
    EmbeddedDocument,
    Heading,
    HorizontalRule,
    ImageBlock,
    ListBlock,
    PageBreak,
    Paragraph,
    Quote,
    RawMarkdown,
    Table,
    plain,
)
from f2m.render.inline import render_text


def render(doc: Document) -> str:
    out = _render_blocks(doc.blocks, heading_offset=0)
    return out.strip() + "\n"


def _render_blocks(blocks: list[Block], heading_offset: int) -> str:
    chunks: list[str] = []
    last_level = heading_offset
    for b in blocks:
        if isinstance(b, Heading):
            last_level = min(6, b.level + heading_offset)
        if isinstance(b, EmbeddedDocument):
            text = _render_embedded(b, last_level)
        else:
            text = render_block(b, heading_offset)
        if text and text.strip():
            chunks.append(text.rstrip("\n"))
    return "\n\n".join(chunks)


def _render_embedded(b: EmbeddedDocument, parent_level: int) -> str:
    # Nested headings sit one level below the heading they appear under.
    inner = b.document.blocks
    top = min((x.level for x in inner if isinstance(x, Heading)), default=1)
    offset = max(0, parent_level + 1 - top)
    label = f"**Embedded {b.document.format or 'object'}: {b.title}**" if b.title else ""
    body = _render_blocks(inner, offset)
    return "\n\n".join(x for x in (label, body) if x)


def render_block(b: Block, heading_offset: int = 0) -> str:
    if isinstance(b, Heading):
        level = max(1, min(6, b.level + heading_offset))
        text = render_text(b.text).replace("\\\n", " ").strip()
        return f"{'#' * level} {text}" if text else ""
    if isinstance(b, Paragraph):
        return render_text(b.text).strip()
    if isinstance(b, ListBlock):
        return _render_list(b)
    if isinstance(b, Table):
        return render_table(b)
    if isinstance(b, CodeBlock):
        fence = "```"
        while fence in b.text:
            fence += "`"
        return f"{fence}{b.language}\n{b.text.rstrip()}\n{fence}"
    if isinstance(b, Quote):
        body = render_text(b.text).strip()
        if b.label:
            body = f"**{b.label}:** {body}"
        return "\n".join("> " + line if line else ">" for line in body.split("\n"))
    if isinstance(b, ImageBlock):
        if b.markdown is not None:
            return b.markdown
        return f"<!-- image: {b.alt or b.name_hint} -->"
    if isinstance(b, PageBreak):
        return f"<!-- {b.label} -->" if b.label else "---"
    if isinstance(b, HorizontalRule):
        return "---"
    if isinstance(b, RawMarkdown):
        return b.text
    if isinstance(b, EmbeddedDocument):
        return _render_embedded(b, heading_offset)
    raise TypeError(f"unknown block type: {type(b).__name__}")


def _render_list(b: ListBlock) -> str:
    lines: list[str] = []
    counters: dict[int, int] = {}
    prev_top: bool | None = None
    prev_level = -1
    base = min((i.level for i in b.items), default=0)
    for item in b.items:
        # A list starts at level 0 and nests one step at a time; deeper indents
        # would turn into a code block in Markdown.
        level = max(0, min(item.level - base, prev_level + 1))
        prev_level = level
        if level == 0:
            if prev_top is not None and prev_top != item.ordered:
                lines.append("")  # bullets <-> numbers at top level: start a new list
                counters.clear()
            prev_top = item.ordered
        for deeper in [k for k in counters if k > level]:
            del counters[deeper]
        text = render_text(item.text).replace("\\\n", " ").strip()
        if item.ordered:
            counters[level] = counters.get(level, 0) + 1
            marker = f"{counters[level]}."
        else:
            counters.pop(level, None)
            marker = "-"
        lines.append(f"{'    ' * level}{marker} {text}")
    return "\n".join(lines)


def _cell(text) -> str:
    parts = [p.strip() for p in render_text(text, cell=True).split("<br>")]
    return "<br>".join(p for p in parts if p)


def render_table(t: Table) -> str:
    rows = [r for r in t.rows if r is not None]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    if width == 0:
        return ""
    cells = [[_cell(c) for c in r] + [""] * (width - len(r)) for r in rows]
    if t.has_header:
        header, body = cells[0], cells[1:]
    else:
        header, body = [""] * width, cells
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join(["---"] * width) + "|",
    ]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    out = "\n".join(lines)
    if t.caption and plain(t.caption).strip():
        out = f"{render_text(t.caption).strip()}\n\n{out}"
    return out
