"""Small helpers for reading OOXML packages (docx/pptx/xlsx are zip files of XML parts)."""

from __future__ import annotations

import posixpath
import zipfile
from dataclasses import dataclass

from lxml import etree

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "v": "urn:schemas-microsoft-com:vml",
    "o": "urn:schemas-microsoft-com:office:office",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
}

R_EMBED = f"{{{NS['r']}}}embed"
R_ID = f"{{{NS['r']}}}id"
R_LINK = f"{{{NS['r']}}}link"

URI_CHART = "http://schemas.openxmlformats.org/drawingml/2006/chart"
URI_TABLE = "http://schemas.openxmlformats.org/drawingml/2006/table"
URI_DIAGRAM = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
URI_PICTURE = "http://schemas.openxmlformats.org/drawingml/2006/picture"

_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True, remove_blank_text=False)


def parse_xml(data: bytes) -> etree._Element:
    return etree.fromstring(data, parser=_PARSER)


def local(tag: object) -> str:
    """Tag name without namespace."""
    return etree.QName(tag).localname if isinstance(tag, str) else ""


@dataclass
class Rel:
    type: str
    target: str  # absolute part name (no leading slash) or external URL
    external: bool


def rels_name(part: str) -> str:
    d, f = posixpath.split(part)
    return posixpath.join(d, "_rels", f + ".rels")


def resolve(part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(part), target))


def read_rels(z: zipfile.ZipFile, part: str) -> dict[str, Rel]:
    try:
        root = parse_xml(z.read(rels_name(part)))
    except KeyError:
        return {}
    out: dict[str, Rel] = {}
    for rel in root.iter(f"{{{NS['pr']}}}Relationship"):
        external = rel.get("TargetMode") == "External"
        target = rel.get("Target", "")
        out[rel.get("Id", "")] = Rel(
            type=rel.get("Type", "").rsplit("/", 1)[-1],
            target=target if external else resolve(part, target),
            external=external,
        )
    return out


def text_of(el: etree._Element) -> str:
    """All DrawingML text (a:t) under an element, paragraphs on separate lines."""
    paras = []
    for p in el.iter(f"{{{NS['a']}}}p"):
        t = "".join(x.text or "" for x in p.iter(f"{{{NS['a']}}}t"))
        if t.strip():
            paras.append(t)
    if not paras:
        t = "".join(x.text or "" for x in el.iter(f"{{{NS['a']}}}t"))
        return t
    return "\n".join(paras)


def diagram_texts(data_xml: bytes) -> list[str]:
    """Text nodes of a SmartArt data part, in document order."""
    root = parse_xml(data_xml)
    out = []
    for pt in root.iter(f"{{{NS['dgm']}}}pt"):
        if pt.get("type") not in (None, "node"):
            continue
        t = text_of(pt).strip()
        if t:
            out.append(t)
    return out
