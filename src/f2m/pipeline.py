"""detect -> parse -> handle images -> render."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from f2m.context import Options, ParseContext
from f2m.detect import detect
from f2m.document import Document
from f2m.images import AssetWriter, ImageContext, ImageHandler, SaveAndLinkHandler
from f2m.registry import get_parser
from f2m.render.markdown import render
from f2m.timing import Timings, recording, timed

log = logging.getLogger(__name__)


class UnsupportedFormat(Exception):
    pass


@dataclass
class Result:
    source: Path | None
    markdown: str = ""
    document: Document | None = None
    output: Path | None = None
    error: str | None = None
    timings: Timings | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class Pipeline:
    options: Options = field(default_factory=Options)
    image_handler: ImageHandler = field(default_factory=SaveAndLinkHandler)

    # -- parsing ---------------------------------------------------------
    def parse(self, data: bytes, name: str, path: Path | None = None) -> Document:
        """Parse bytes into a Document (images not yet handled)."""
        ctx = ParseContext(name=name, options=self.options, path=path, _convert=self._parse_nested)
        return self._parse(data, name, ctx, strict=True)  # type: ignore[return-value]

    def _parse(self, data: bytes, name: str, ctx: ParseContext, strict: bool) -> Document | None:
        with timed("detect"):
            fmt = detect(data, name)
            parser = get_parser(fmt) if fmt else None
        if parser is None:
            if strict:
                raise UnsupportedFormat(f"{name}: unsupported or unrecognised file type")
            return None
        with timed(fmt or "unknown"):
            doc = parser.parse(data, ctx)
        doc.source = doc.source or name
        doc.format = doc.format or (fmt or "")
        return doc

    def _parse_nested(self, data: bytes, name: str, ctx: ParseContext) -> Document | None:
        try:
            with timed("embedded"):
                return self._parse(data, name, ctx, strict=False)
        except Exception as e:
            log.warning("could not parse embedded %s: %s", name, e)
            return None

    # -- images + rendering ---------------------------------------------
    def finish(self, doc: Document, assets: AssetWriter | None) -> str:
        ictx = ImageContext(assets=assets, source=doc.source)
        with timed("images"):
            for img in doc.images():
                try:
                    img.markdown = self.image_handler.handle(img, ictx)
                except Exception as e:
                    log.warning("image handler failed for %s: %s", img.name_hint, e)
                    img.markdown = f"<!-- image {img.name_hint}: handler failed -->"
        with timed("render"):
            return render(doc)

    # -- public entry points --------------------------------------------
    def convert_bytes(self, data: bytes, name: str, out_dir: Path | None = None) -> Result:
        doc = self.parse(data, name)
        assets = None
        if out_dir is not None:
            assets = AssetWriter(md_dir=out_dir, assets_dir=out_dir / f"{Path(name).stem}_assets")
        md = self.finish(doc, assets)
        return Result(source=None, markdown=md, document=doc)

    def convert_file(self, path: str | Path, out_dir: str | Path | None = None, stem: str | None = None) -> Result:
        """Convert one file. With ``out_dir``, writes ``<stem>.md`` and ``<stem>_assets/``."""
        path = Path(path)
        out = Path(out_dir) if out_dir is not None else None
        stem = stem or path.stem
        with recording() as rec:
            try:
                with timed("read"):
                    data = path.read_bytes()
                with timed("parse"):
                    doc = self.parse(data, path.name, path=path)
                assets = None
                if out is not None:
                    out.mkdir(parents=True, exist_ok=True)
                    assets = AssetWriter(md_dir=out, assets_dir=out / f"{stem}_assets")
                md = self.finish(doc, assets)
                target = None
                if out is not None:
                    target = out / f"{stem}.md"
                    with timed("write"):
                        target.write_text(md, encoding="utf-8")
                for w in doc.warnings:
                    log.warning("%s: %s", path.name, w)
                return Result(source=path, markdown=md, document=doc, output=target, timings=rec)
            except Exception as e:
                log.debug("conversion failed", exc_info=True)
                return Result(source=path, error=f"{type(e).__name__}: {e}", timings=rec)

    def convert_path(self, path: str | Path, out_dir: str | Path, recursive: bool = False,
                     workers: int = 1) -> list[Result]:
        """Convert a file, or every supported file in a folder, mirroring sub-folders.

        With ``workers > 1`` files are converted in that many processes; the
        results then carry no ``document``/``markdown`` (the files are written).
        """
        from f2m.parallel import convert_many

        path, out = Path(path), Path(out_dir)
        if path.is_file():
            return [self.convert_file(path, out)]
        files = self.find_files(path, recursive)
        jobs = [(f, out / f.parent.relative_to(path), stem) for f, stem in zip(files, output_stems(files))]
        fn = _keep if workers <= 1 else slim
        results: list[Result | None] = [None] * len(jobs)
        for i, res in convert_many(self, jobs, workers, fn):
            results[i] = res
        return results  # type: ignore[return-value]

    @staticmethod
    def find_files(folder: Path, recursive: bool = False) -> list[Path]:
        """Supported files in a folder (Office lock files and empty files skipped)."""
        pattern = "**/*" if recursive else "*"
        files = []
        for f in sorted(p for p in Path(folder).glob(pattern) if p.is_file()):
            # ~$x: Office lock files; ._x: macOS metadata files, not real documents
            if f.name.startswith(("~$", "._")) or f.stat().st_size == 0:
                continue
            with open(f, "rb") as fh:
                head = fh.read(8192)
            # Office files are zips: the part list is needed, so read those fully.
            data = f.read_bytes() if head.startswith(b"PK") else head
            if detect(data, f.name) is not None:
                files.append(f)
        return files


def output_stems(files: list[Path]) -> list[str]:
    """Output name per file. Same stem in the same folder (report.pdf + report.docx): add the extension."""
    seen: dict[tuple[Path, str], int] = {}
    for f in files:
        key = (f.parent, f.stem.lower())
        seen[key] = seen.get(key, 0) + 1
    return [f.stem if seen[(f.parent, f.stem.lower())] == 1 else f"{f.stem}_{f.suffix.lstrip('.').lower()}"
            for f in files]


def _keep(res: Result) -> Result:
    return res


def slim(res: Result) -> Result:
    """Result without the parsed document and Markdown, to send back from a worker process."""
    return Result(source=res.source, output=res.output, error=res.error, timings=res.timings)
