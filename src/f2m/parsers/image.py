"""Standalone image files: the whole file is one ImageBlock for the image handler."""

from __future__ import annotations

import io

from f2m.context import ParseContext
from f2m.detect import mime_for
from f2m.document import Document, ImageBlock
from f2m.registry import Parser, register


@register
class ImageParser(Parser):
    formats = ("image",)
    extensions = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp")

    def parse(self, data: bytes, ctx: ParseContext) -> Document:
        doc = Document(format="image")
        mime = mime_for(data, "image/png")
        try:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as im:
                doc.metadata.update(width=im.width, height=im.height, mode=im.mode, frames=getattr(im, "n_frames", 1))
        except Exception as e:
            doc.warn(f"could not read image header: {e}")
        doc.add(ImageBlock(data=data, mime=mime, name_hint=ctx.stem, alt=ctx.stem, location={"file": ctx.name}))
        return doc
