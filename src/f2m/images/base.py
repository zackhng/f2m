"""Image handler interface and asset writing.

An ImageHandler turns one ImageBlock into Markdown. The default handler saves
the image and links it. Later, a trained image-to-markdown model plugs in here
as another handler, and no parser has to change.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from f2m.document import ImageBlock

log = logging.getLogger(__name__)

_EXT = {
    "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/bmp": "bmp",
    "image/tiff": "tiff", "image/webp": "webp", "image/svg+xml": "svg",
    "image/x-emf": "emf", "image/emf": "emf", "image/x-wmf": "wmf", "image/wmf": "wmf",
    "image/x-icon": "ico",
}
# Formats most Markdown viewers can't show; converted to PNG when Pillow can read them.
_CONVERT = {"emf", "wmf", "tiff", "bmp", "ico"}


def ext_for(mime: str) -> str:
    return _EXT.get(mime.lower(), "bin")


def to_png(data: bytes) -> bytes | None:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            if im.mode not in ("RGB", "RGBA", "L", "LA"):
                im = im.convert("RGBA")
            buf = io.BytesIO()
            im.save(buf, "PNG")
            return buf.getvalue()
    except Exception:
        return None


@dataclass
class AssetWriter:
    """Writes image files next to the Markdown file and returns relative links."""

    md_dir: Path
    assets_dir: Path
    convert_formats: bool = True
    _seen: dict[str, str] = field(default_factory=dict)
    _names: set[str] = field(default_factory=set)

    def write(self, image: ImageBlock) -> str:
        data = image.load()
        ext = ext_for(image.mime)
        if self.convert_formats and ext in _CONVERT:
            png = to_png(data)
            if png is not None:
                data, ext = png, "png"
        digest = hashlib.sha1(data).hexdigest()
        if digest in self._seen:
            return self._seen[digest]
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", image.name_hint).strip("._") or "image"
        name, n = f"{stem}.{ext}", 2
        while name in self._names:
            name, n = f"{stem}_{n}.{ext}", n + 1
        self._names.add(name)
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        (self.assets_dir / name).write_bytes(data)
        rel = (self.assets_dir / name).relative_to(self.md_dir).as_posix()
        link = quote(rel)
        self._seen[digest] = link
        return link


@dataclass
class ImageContext:
    """Where images go. ``assets`` is None when only a string is wanted."""

    assets: AssetWriter | None = None
    source: str = ""


class ImageHandler(Protocol):
    def handle(self, image: ImageBlock, ctx: ImageContext) -> str:
        """Return the Markdown that replaces this image."""
        ...


def alt_text(image: ImageBlock) -> str:
    alt = (image.alt or image.caption or image.name_hint).replace("\n", " ").strip()
    return alt.replace("[", "(").replace("]", ")")
