"""Built-in image handlers."""

from __future__ import annotations

import io
import logging
import shutil

from f2m.document import ImageBlock
from f2m.images.base import ImageContext, ImageHandler, alt_text, to_png
from f2m.timing import timed

log = logging.getLogger(__name__)


class SaveAndLinkHandler:
    """Save the image to the assets folder and link it."""

    def handle(self, image: ImageBlock, ctx: ImageContext) -> str:
        alt = alt_text(image)
        if ctx.assets is None:
            return f"![{alt}](<{image.name_hint}>)"
        with timed("save"):
            link = ctx.assets.write(image)
        out = f"![{alt}]({link})"
        if image.caption and image.caption != image.alt:
            out += f"\n\n*{image.caption.strip()}*"
        return out


class DropHandler:
    """Leave images out of the output."""

    def handle(self, image: ImageBlock, ctx: ImageContext) -> str:
        return ""


class OcrHandler:
    """Read the text in an image with Tesseract. Returns "" when nothing is found.

    Needs the ``ocr`` extra and the Tesseract program on PATH. If Tesseract is
    missing it logs one warning and returns nothing.
    """

    def __init__(self, lang: str = "eng", min_chars: int = 3):
        self.lang = lang
        self.min_chars = min_chars
        self._ok: bool | None = None

    def _available(self) -> bool:
        if self._ok is None:
            try:
                import pytesseract  # noqa: F401

                self._ok = shutil.which("tesseract") is not None
                if not self._ok:
                    import pytesseract as pt

                    pt.get_tesseract_version()
                    self._ok = True
            except Exception:
                self._ok = False
            if not self._ok:
                log.warning("OCR requested but Tesseract is not available; images will only be linked")
        return self._ok

    def text(self, image: ImageBlock) -> str:
        if not self._available():
            return ""
        import pytesseract
        from PIL import Image

        data = image.load()
        try:
            im = Image.open(io.BytesIO(data))
            im.load()
        except Exception:
            png = to_png(data)
            if png is None:
                return ""
            im = Image.open(io.BytesIO(png))
        try:
            text = pytesseract.image_to_string(im, lang=self.lang)
        except Exception as e:
            log.warning("OCR failed for %s: %s", image.name_hint, e)
            return ""
        text = "\n".join(line.rstrip() for line in text.strip().splitlines())
        return text if len(text.strip()) >= self.min_chars else ""

    def handle(self, image: ImageBlock, ctx: ImageContext) -> str:
        with timed("ocr"):
            text = self.text(image)
        if not text:
            return ""
        return "\n".join("> " + line if line else ">" for line in text.splitlines())


class CompositeHandler:
    """Run several handlers and join their output (e.g. link + OCR text)."""

    def __init__(self, *handlers: ImageHandler):
        self.handlers = handlers

    def handle(self, image: ImageBlock, ctx: ImageContext) -> str:
        parts = [h.handle(image, ctx) for h in self.handlers]
        return "\n\n".join(p for p in parts if p)


class ModelHandler:
    """Placeholder for the future image-to-markdown model.

    Implement ``predict`` to return Markdown for an image (a table, a chart's
    data, a diagram description...). Combine it with SaveAndLinkHandler through
    CompositeHandler to keep the link next to the model output.
    """

    def predict(self, image: ImageBlock) -> str:
        raise NotImplementedError("the image-to-markdown model has not been trained yet")

    def handle(self, image: ImageBlock, ctx: ImageContext) -> str:
        return self.predict(image)


def make_handler(name: str) -> ImageHandler:
    """Handler by CLI name: 'link' (default), 'ocr' (link + OCR text), 'none'."""
    if name == "link":
        return SaveAndLinkHandler()
    if name == "ocr":
        return CompositeHandler(SaveAndLinkHandler(), OcrHandler())
    if name == "none":
        return DropHandler()
    raise ValueError(f"unknown image handler: {name!r} (use link, ocr or none)")
