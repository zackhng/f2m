"""HTML -> Document. Our own walker over BeautifulSoup, so images become ImageBlocks."""

from __future__ import annotations

import base64
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from f2m.common.tables import Cell, build_grid, make_table
from f2m.context import ParseContext
from f2m.detect import mime_for
from f2m.document import (
    Block,
    CodeBlock,
    Document,
    Heading,
    HorizontalRule,
    ImageBlock,
    Inline,
    ListBlock,
    ListItem,
    Paragraph,
    Quote,
    plain,
)
from f2m.registry import Parser, register
from f2m.timing import timed

SKIP = {"script", "style", "noscript", "template", "head", "iframe", "object", "embed",
        "button", "input", "select", "textarea", "svg", "canvas", "map", "meta", "link"}
BLOCK = {"p", "div", "section", "article", "main", "header", "footer", "aside", "nav", "body", "html",
         "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "table", "pre", "blockquote", "hr",
         "figure", "figcaption", "dl", "dt", "dd", "form", "fieldset", "details", "summary", "address",
         "center", "hgroup", "menu"}
_WS = re.compile(r"[ \t\r\n\f]+")

Item = "Inline | ImageBlock"


@register
class HtmlParser(Parser):
    formats = ("html",)
    extensions = (".html", ".htm", ".xhtml")

    def parse(self, data: bytes, ctx: ParseContext) -> Document:
        with timed("load"):
            soup = BeautifulSoup(data, "lxml")
        doc = Document(format="html")
        if soup.title and soup.title.string:
            doc.metadata["title"] = soup.title.string.strip()
        base = ctx.path.parent if ctx.path else None
        walker = _Walker(doc, base)
        root = soup.body or soup
        with timed("walk"):
            walker.walk(root)
        doc.extend(walker.blocks)
        return doc


class _Walker:
    def __init__(self, doc: Document, base: Path | None):
        self.doc = doc
        self.base = base
        self.blocks: list[Block] = []
        self.n_images = 0

    # -- block level ------------------------------------------------------
    def walk(self, node: Tag) -> None:
        buf: list = []
        for child in node.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                buf.append(Inline(_WS.sub(" ", str(child))))
                continue
            if not isinstance(child, Tag) or child.name in SKIP:
                continue
            if child.name in BLOCK:
                self._flush(buf)
                buf = []
                self._block(child)
            else:
                buf.extend(self.inline(child))
        self._flush(buf)

    def _flush(self, items: list) -> None:
        """Turn collected inline items into paragraphs, splitting at images."""
        runs: list[Inline] = []
        for it in items + [None]:
            if isinstance(it, Inline):
                runs.append(it)
                continue
            text = _tidy(runs)
            if text:
                self.blocks.append(Paragraph(text))
            runs = []
            if it is not None:
                self.blocks.append(it)

    def _block(self, el: Tag) -> None:
        name = el.name
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = _tidy([i for i in self.inline(el) if isinstance(i, Inline)])
            if text:
                self.blocks.append(Heading(int(name[1]), text))
        elif name in ("ul", "ol", "menu"):
            lb = ListBlock()
            after: list[Block] = []
            self._list(el, 0, lb, after)
            if lb.items:
                self.blocks.append(lb)
            self.blocks.extend(after)
        elif name == "table":
            self._table(el)
        elif name == "pre":
            code = el.find("code")
            lang = ""
            for cls in (code.get("class") if code else el.get("class")) or []:
                if cls.startswith(("language-", "lang-")):
                    lang = cls.split("-", 1)[1]
            self.blocks.append(CodeBlock(el.get_text(), lang))
        elif name == "blockquote":
            text = self._flat_text(el)
            if text:
                self.blocks.append(Quote(text))
        elif name == "hr":
            self.blocks.append(HorizontalRule())
        elif name == "figcaption":
            text = el.get_text(" ", strip=True)
            last = self.blocks[-1] if self.blocks else None
            if isinstance(last, ImageBlock) and not last.caption:
                last.caption = text
            elif text:
                self.blocks.append(Paragraph([Inline(text, italic=True)]))
        elif name == "dt":
            text = _tidy([i for i in self.inline(el) if isinstance(i, Inline)])
            if text:
                self.blocks.append(Paragraph([Inline(plain(text), bold=True)]))
        else:  # p, div, section, li outside a list, dd, ...
            self.walk(el)

    def _list(self, el: Tag, level: int, lb: ListBlock, after: list[Block]) -> None:
        ordered = el.name == "ol"
        for li in el.find_all("li", recursive=False):
            items: list = []
            nested: list[Tag] = []
            for child in li.children:
                if isinstance(child, Tag) and child.name in ("ul", "ol"):
                    nested.append(child)
                elif isinstance(child, Tag) and child.name in BLOCK:
                    items.extend(self.inline(child))
                    items.append(Inline(" "))
                elif isinstance(child, NavigableString) and not isinstance(child, Comment):
                    items.append(Inline(_WS.sub(" ", str(child))))
                elif isinstance(child, Tag) and child.name not in SKIP:
                    items.extend(self.inline(child))
            after.extend(i for i in items if isinstance(i, ImageBlock))
            text = _tidy([i for i in items if isinstance(i, Inline)])
            lb.items.append(ListItem(text or "", level, ordered))
            for n in nested:
                self._list(n, level + 1, lb, after)

    def _table(self, table: Tag) -> None:
        rows: list[list[Cell]] = []
        images: list[Block] = []
        for tr in _own_rows(table):
            row = []
            for td in tr.find_all(["td", "th"], recursive=False):
                items = self.inline_cell(td)
                images.extend(i for i in items if isinstance(i, ImageBlock))
                text = _tidy([i for i in items if isinstance(i, Inline)])
                row.append(Cell(text or "", _int(td.get("colspan")), _int(td.get("rowspan"))))
            rows.append(row)
        cap = table.find("caption")
        caption = cap.get_text(" ", strip=True) if cap else None
        t = make_table(build_grid(rows), caption=caption)
        if t:
            self.blocks.append(t)
        self.blocks.extend(images)

    # -- inline level -----------------------------------------------------
    def inline(self, el: Tag, style: dict | None = None) -> list:
        style = dict(style or {})
        name = el.name
        if name in SKIP:
            return []
        if name == "br":
            return [Inline("\n")]
        if name == "img":
            return [self._image(el)]
        if name in ("b", "strong"):
            style["bold"] = True
        elif name in ("i", "em", "cite", "dfn"):
            style["italic"] = True
        elif name in ("s", "del", "strike"):
            style["strike"] = True
        elif name in ("code", "kbd", "samp", "tt"):
            return [Inline(el.get_text(), code=True, link=style.get("link"))]
        elif name == "a" and el.get("href") and not el["href"].startswith(("javascript:", "#")):
            style["link"] = el["href"]
        out: list = []
        for child in el.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                out.append(Inline(_WS.sub(" ", str(child)), **style))
            elif isinstance(child, Tag):
                out.extend(self.inline(child, style))
                if child.name in BLOCK:
                    out.append(Inline("\n"))
        return out

    def inline_cell(self, td: Tag) -> list:
        out: list = []
        for child in td.children:
            if isinstance(child, NavigableString) and not isinstance(child, Comment):
                out.append(Inline(_WS.sub(" ", str(child))))
            elif isinstance(child, Tag) and child.name == "table":  # nested table: flatten rows
                for tr in _own_rows(child):
                    cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"], recursive=False)]
                    out.append(Inline("; ".join(cells) + "\n"))
            elif isinstance(child, Tag) and child.name in ("ul", "ol"):
                for li in child.find_all("li"):
                    out.append(Inline("• " + li.get_text(" ", strip=True) + "\n"))
            elif isinstance(child, Tag):
                out.extend(self.inline(child))
                if child.name in BLOCK:
                    out.append(Inline("\n"))
        return out

    def _flat_text(self, el: Tag) -> list[Inline]:
        runs: list[Inline] = []
        for child in el.children:
            if isinstance(child, NavigableString) and not isinstance(child, Comment):
                runs.append(Inline(_WS.sub(" ", str(child))))
            elif isinstance(child, Tag):
                runs.extend(i for i in self.inline(child) if isinstance(i, Inline))
                if child.name in BLOCK:
                    runs.append(Inline("\n"))
        return _tidy(runs)

    def _image(self, el: Tag):
        src = (el.get("src") or el.get("data-src") or "").strip()
        alt = (el.get("alt") or el.get("title") or "").strip()
        self.n_images += 1
        hint = f"img{self.n_images:03d}"
        data = _load_src(src, self.base)
        if data is None:
            if src and not src.startswith("data:"):
                # Remote or missing file: keep the link, nothing to hand to the image handler.
                return Inline(f"![{alt}]({src})", raw=True)
            return Inline("")
        return ImageBlock(data=data, mime=mime_for(data, "image/png"), name_hint=hint, alt=alt,
                          location={"src": src[:200]})


def _own_rows(table: Tag) -> list[Tag]:
    """<tr> elements of this table, not of tables nested inside it."""
    rows = []
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is table:
            rows.append(tr)
    return rows


def _int(v) -> int:
    try:
        return max(1, int(str(v).strip()))
    except (TypeError, ValueError):
        return 1


def _load_src(src: str, base: Path | None) -> bytes | None:
    if src.startswith("data:"):
        try:
            header, payload = src.split(",", 1)
            if ";base64" in header:
                return base64.b64decode(payload)
            return unquote(payload).encode()
        except Exception:
            return None
    url = urlparse(src)
    if url.scheme in ("http", "https", "ftp") or src.startswith("//") or base is None:
        return None
    p = (base / unquote(url.path)).resolve()
    try:
        return p.read_bytes() if p.is_file() else None
    except OSError:
        return None


def _tidy(runs: list[Inline]) -> list[Inline]:
    """Collapse whitespace across runs and trim the ends. Returns [] if empty."""
    out: list[Inline] = []
    prev_space = True
    for r in runs:
        if r.raw or r.code:
            out.append(r)
            prev_space = False
            continue
        text = r.text
        if text == "\n":
            if out and not prev_space:
                out.append(Inline("\n"))
                prev_space = True
            continue
        if prev_space:
            text = text.lstrip(" ")
        if not text:
            continue
        prev_space = text.endswith(" ")
        out.append(Inline(text, r.bold, r.italic, r.strike, r.code, r.link))
    while out and not out[-1].raw and not out[-1].code and not out[-1].text.strip():
        out.pop()
    if out and not out[-1].raw and not out[-1].code:
        last = out[-1]
        out[-1] = Inline(last.text.rstrip(), last.bold, last.italic, last.strike, last.code, last.link)
    return out if any(r.text.strip() for r in out) else []
