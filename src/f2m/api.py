"""HTTP API: ``uv run f2m serve``, then open http://127.0.0.1:8000/docs.

Endpoints
- ``GET  /health``           liveness check
- ``GET  /formats``          formats that have a parser
- ``POST /convert``          one file -> JSON (Markdown, warnings, timings, images as base64)
- ``POST /convert/markdown`` one file -> the Markdown as text (image links point at nothing)
- ``POST /convert/zip``      one or more files -> zip of ``<name>.md`` + ``<name>_assets/``

Auth is optional. When the ``F2M_API_TOKEN`` environment variable is set (or ``f2m serve --token``),
every endpoint except ``/health`` needs ``Authorization: Bearer <token>``. Unset it to turn auth off.

Conversion options are form fields next to the file. Requests are handled in a
thread pool; a batch sent to ``/convert/zip`` is converted in several processes.
"""

from __future__ import annotations

import base64
import io
import mimetypes
import os
import secrets
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from f2m.context import Options
from f2m.images import make_handler
from f2m.parallel import default_workers
from f2m.pipeline import Pipeline, Result
from f2m.registry import available

MAX_UPLOAD_MB = float(os.environ.get("F2M_MAX_UPLOAD_MB", "200"))
MAX_FILES = int(os.environ.get("F2M_MAX_FILES", "100"))
TOKEN_ENV = "F2M_API_TOKEN"
_CHUNK = 1 << 20

app = FastAPI(
    title="f2m",
    version="0.1.0",
    description="Convert PDF, DOCX, PPTX, XLSX, HTML and image files to Markdown.",
)

# -- auth ----------------------------------------------------------------
_bearer = HTTPBearer(auto_error=False, description=f"Used only when {TOKEN_ENV} is set. Leave empty otherwise.")


def require_token(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    """Check the bearer token if one is configured. Read on every request, so unsetting it turns auth off."""
    expected = os.environ.get(TOKEN_ENV, "")
    if not expected:
        return
    given = creds.credentials if creds else ""
    if not secrets.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(401, "missing or wrong bearer token", headers={"WWW-Authenticate": "Bearer"})



# -- request options -----------------------------------------------------
@dataclass
class ConvertOptions:
    images: str
    options: Options


def convert_options(
    images: Literal["link", "ocr", "none"] = Form("link", description="Images: save and link, add OCR text, or drop."),
    xlsx_formulas: Literal["section", "inline", "none"] = Form("section", description="XLSX formulas layout."),
    xlsx_deps: bool = Form(False, description="XLSX: list what each formula reads from."),
    xlsx_compute: bool = Form(False, description="XLSX: compute values missing from the file (calc extra)."),
    merged_cells: Literal["repeat", "blank"] = Form("repeat", description="Merged table cells."),
    notes: bool = Form(True, description="PPTX: include speaker notes."),
    pdf_graphics: bool = Form(True, description="PDF: render vector drawings (charts) as images."),
) -> ConvertOptions:
    return ConvertOptions(images, Options(
        merged_cells=merged_cells,
        xlsx_formulas=xlsx_formulas,
        xlsx_dependencies=xlsx_deps,
        xlsx_compute_missing=xlsx_compute,
        pptx_notes=notes,
        pdf_vector_graphics=pdf_graphics,
    ))


# -- responses -----------------------------------------------------------
class FormatInfo(BaseModel):
    format: str
    extensions: list[str]


class Asset(BaseModel):
    path: str  # as linked from the Markdown, e.g. "report_assets/page1_img1.png"
    media_type: str
    data: str  # base64


class ConvertResponse(BaseModel):
    filename: str
    format: str
    markdown: str
    warnings: list[str]
    assets: list[Asset]
    timings_ms: dict[str, float]


# -- helpers -------------------------------------------------------------
def _safe_name(name: str | None, taken: set[str]) -> str:
    """File name without folders, unique within one request."""
    base = Path((name or "").replace("\\", "/")).name.strip() or "upload"
    stem, suffix = Path(base).stem, Path(base).suffix
    out, n = base, 2
    while out.lower() in taken:
        out, n = f"{stem}_{n}{suffix}", n + 1
    taken.add(out.lower())
    return out


def _save_upload(upload: UploadFile, target: Path) -> None:
    limit = MAX_UPLOAD_MB * 1024 * 1024
    size = 0
    with open(target, "wb") as fh:
        while chunk := upload.file.read(_CHUNK):
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, f"{upload.filename}: larger than {MAX_UPLOAD_MB:g} MB")
            fh.write(chunk)
    if size == 0:
        raise HTTPException(400, f"{upload.filename}: empty file")


def _fail(res: Result, name: str) -> HTTPException:
    error = res.error or "unknown error"
    if error.startswith("UnsupportedFormat"):
        return HTTPException(415, f"{name}: unsupported or unrecognised file type")
    return HTTPException(422, f"{name}: {error}")


def _pipeline(opts: ConvertOptions) -> Pipeline:
    return Pipeline(options=opts.options, image_handler=make_handler(opts.images))


def _convert_one(upload: UploadFile, opts: ConvertOptions, out: Path | None) -> tuple[str, Result]:
    """Convert an upload inside a temp folder. With ``out``, images are written there."""
    tmp = Path(tempfile.mkdtemp(prefix="f2m-in-"))
    try:
        name = _safe_name(upload.filename, set())
        src = tmp / name
        _save_upload(upload, src)
        res = _pipeline(opts).convert_file(src, out)
    finally:
        _rmtree(tmp)
    if not res.ok:
        raise _fail(res, name)
    return name, res


def _rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


# -- endpoints -----------------------------------------------------------
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/formats", response_model=list[FormatInfo], dependencies=[Depends(require_token)])
def formats() -> list[FormatInfo]:
    return [FormatInfo(format=f, extensions=list(cls.extensions)) for f, cls in sorted(available().items())]


@app.post("/convert", response_model=ConvertResponse, dependencies=[Depends(require_token)])
def convert(
    file: UploadFile = File(..., description="The document or image to convert."),
    opts: ConvertOptions = Depends(convert_options),
) -> ConvertResponse:
    """Convert one file. Images come back base64-encoded under the paths the Markdown links to."""
    out = Path(tempfile.mkdtemp(prefix="f2m-out-"))
    try:
        name, res = _convert_one(file, opts, out)
        assets = []
        for p in sorted(out.rglob("*")):
            if p.is_file() and p.suffix != ".md":
                assets.append(Asset(
                    path=p.relative_to(out).as_posix(),
                    media_type=mimetypes.guess_type(p.name)[0] or "application/octet-stream",
                    data=base64.b64encode(p.read_bytes()).decode("ascii"),
                ))
    finally:
        _rmtree(out)
    doc = res.document
    return ConvertResponse(
        filename=name,
        format=doc.format if doc else "",
        markdown=res.markdown,
        warnings=list(doc.warnings) if doc else [],
        assets=assets,
        timings_ms=res.timings.top_level() if res.timings else {},
    )


@app.post("/convert/markdown", response_class=PlainTextResponse, dependencies=[Depends(require_token)])
def convert_markdown(
    file: UploadFile = File(..., description="The document or image to convert."),
    opts: ConvertOptions = Depends(convert_options),
) -> PlainTextResponse:
    """Convert one file and return only the Markdown. Use ``images=none`` (or ``/convert/zip``) to avoid dead links."""
    _, res = _convert_one(file, opts, None)
    return PlainTextResponse(res.markdown, media_type="text/markdown; charset=utf-8")


@app.post("/convert/zip", response_class=Response, dependencies=[Depends(require_token)],
          responses={200: {"content": {"application/zip": {}}, "description": "Markdown files and images."}})
def convert_zip(
    files: list[UploadFile] = File(..., description="One or more files to convert."),
    opts: ConvertOptions = Depends(convert_options),
    workers: int = Form(0, ge=0, description="Processes for a batch. 0 = one per file, up to the CPU count."),
) -> Response:
    """Convert files into a zip: ``<name>.md`` + ``<name>_assets/`` each, and ``errors.txt`` if any failed."""
    if len(files) > MAX_FILES:
        raise HTTPException(413, f"at most {MAX_FILES} files per request")
    tmp = Path(tempfile.mkdtemp(prefix="f2m-batch-"))
    try:
        src, out = tmp / "in", tmp / "out"
        src.mkdir()
        taken: set[str] = set()
        for f in files:
            _save_upload(f, src / _safe_name(f.filename, taken))
        n = workers or min(len(files), default_workers())
        # Starting worker processes costs about a second, so small batches run here.
        n = 1 if len(files) < 3 else n
        results = _pipeline(opts).convert_path(src, out, workers=n)
        converted = {r.source.name for r in results if r.source}
        errors = [f"{r.source.name if r.source else '?'}: {r.error}" for r in results if not r.ok]
        errors += [f"{p.name}: unsupported or unrecognised file type"
                   for p in sorted(src.iterdir()) if p.name not in converted]
        if not any(r.ok for r in results):
            raise HTTPException(422 if results else 415, "; ".join(errors))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(out.rglob("*")):
                if p.is_file():
                    z.write(p, p.relative_to(out).as_posix())
            if errors:
                z.writestr("errors.txt", "\n".join(errors) + "\n")
    finally:
        _rmtree(tmp)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="f2m.zip"'})
