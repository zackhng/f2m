"""Inline text -> Markdown: escaping and formatting of runs."""

from __future__ import annotations

import re

from f2m.document import Inline, Text

_ALWAYS = re.compile(r"([\\`*\[\]<>])")
# Underscores only act as emphasis at word edges, so leave snake_case alone.
_UNDERSCORE = re.compile(r"(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])")
_LINE_START = re.compile(r"^(\s*)([#>+\-=]|\d+[.)])(?=\s|$)", re.MULTILINE)


def escape(text: str, *, cell: bool = False) -> str:
    text = _ALWAYS.sub(r"\\\1", text)
    text = _UNDERSCORE.sub(r"\\_", text)
    if cell:
        text = text.replace("|", "\\|")
    else:
        text = _LINE_START.sub(lambda m: m.group(1) + "\\" + m.group(2), text)
    return text


def _code_span(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _merge(runs: list[Inline]) -> list[Inline]:
    out: list[Inline] = []
    for r in runs:
        if not r.text:
            continue
        if out and out[-1].style() == r.style():
            prev = out[-1]
            out[-1] = Inline(prev.text + r.text, prev.bold, prev.italic, prev.strike,
                             prev.code, prev.link, prev.raw)
        else:
            out.append(r)
    return out


def _break(cell: bool) -> str:
    return "<br>" if cell else "\\\n"


def render_text(text: Text | None, *, cell: bool = False) -> str:
    """Render text content. ``cell=True`` makes it safe inside a table cell."""
    if text is None:
        return ""
    if isinstance(text, str):
        return _plain(text, cell)
    parts: list[str] = []
    for run in _merge(text):
        if run.raw:
            parts.append(run.text)
            continue
        if run.code:
            body = _code_span(run.text.replace("\n", " "))
            if cell:
                body = body.replace("|", "\\|")
        else:
            body = _plain(run.text, cell)
        # Markers must hug the text, so move outer whitespace outside them.
        lead = body[: len(body) - len(body.lstrip())]
        trail = body[len(body.rstrip()):]
        core = body.strip()
        if not core:
            parts.append(body)
            continue
        if run.strike:
            core = f"~~{core}~~"
        if run.italic:
            core = f"*{core}*"
        if run.bold:
            core = f"**{core}**"
        if run.link:
            core = f"[{core}]({_url(run.link)})"
        parts.append(lead + core + trail)
    return "".join(parts)


def _plain(text: str, cell: bool) -> str:
    lines = [escape(line, cell=cell) for line in text.split("\n")]
    return _break(cell).join(lines)


def _url(url: str) -> str:
    url = url.strip()
    if any(c in url for c in " ()<>"):
        return "<" + url.replace("<", "%3C").replace(">", "%3E") + ">"
    return url
