"""Work out a file's format from its bytes, falling back to the extension."""

from __future__ import annotations

import io
import zipfile
from pathlib import PurePath

import filetype

# MIME (from magic bytes) -> format id
_MIME_FORMATS = {
    "application/pdf": "pdf",
    "image/png": "image",
    "image/jpeg": "image",
    "image/gif": "image",
    "image/bmp": "image",
    "image/tiff": "image",
    "image/webp": "image",
    "image/x-icon": "image",
}

# Extension fallback when the bytes alone don't settle it.
_EXT_FORMATS = {
    ".pdf": "pdf",
    ".docx": "docx", ".docm": "docx", ".dotx": "docx",
    ".pptx": "pptx", ".pptm": "pptx", ".potx": "pptx", ".ppsx": "pptx",
    ".xlsx": "xlsx", ".xlsm": "xlsx", ".xltx": "xlsx",
    ".html": "html", ".htm": "html", ".xhtml": "html",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".bmp": "image", ".tif": "image", ".tiff": "image", ".webp": "image",
}


def _ooxml_kind(data: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
    except zipfile.BadZipFile:
        return None
    if "word/document.xml" in names:
        return "docx"
    if "ppt/presentation.xml" in names:
        return "pptx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    # Some writers use non-default part names; check the content types.
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            ct = z.read("[Content_Types].xml").decode("utf-8", "ignore")
    except KeyError:
        return None
    if "wordprocessingml.document.main" in ct:
        return "docx"
    if "presentationml.presentation.main" in ct or "presentationml.slideshow.main" in ct:
        return "pptx"
    if "spreadsheetml.sheet.main" in ct:
        return "xlsx"
    return None


def _looks_like_html(data: bytes) -> bool:
    head = data[:2048].lstrip(b"\xef\xbb\xbf").lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html")) or b"<html" in head[:512]


def detect(data: bytes, name: str = "") -> str | None:
    """Return a format id ('pdf', 'docx', 'pptx', 'xlsx', 'html', 'image'...) or None."""
    ext = PurePath(name).suffix.lower()

    if data[:4] == b"PK\x03\x04":
        kind = _ooxml_kind(data)
        if kind:
            return kind

    kind = filetype.guess(data[:8192]) if data else None
    if kind is not None and kind.mime in _MIME_FORMATS:
        return _MIME_FORMATS[kind.mime]
    if data[:5] == b"%PDF-" or b"%PDF-" in data[:1024]:
        return "pdf"
    if _looks_like_html(data):
        return "html"

    if ext in _EXT_FORMATS:
        return _EXT_FORMATS[ext]
    from f2m.registry import extension_map  # plugins may add extensions

    return extension_map().get(ext)


def mime_for(data: bytes, default: str = "application/octet-stream") -> str:
    kind = filetype.guess(data[:8192]) if data else None
    if kind is not None:
        return kind.mime
    head = data[:64]
    if head[:4] == b"\x01\x00\x00\x00" and b" EMF" in data[:64]:
        return "image/x-emf"
    if head[:4] in (b"\xd7\xcd\xc6\x9a", b"\x01\x00\x09\x00", b"\x02\x00\x09\x00"):
        return "image/x-wmf"
    if head.lstrip().startswith(b"<svg") or b"<svg" in data[:512]:
        return "image/svg+xml"
    return default
