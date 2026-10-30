# f2m: files to Markdown

Converts PDF, DOCX, PPTX, XLSX, HTML and image files (PNG, JPG, GIF, BMP, TIFF, WebP) into Markdown.

Text that can be read directly from the file is extracted with fixed rules. This covers headings, paragraphs, lists, links, native tables, chart data, formulas, and Office files embedded inside other files. Pictures go to a pluggable **image handler**. For now that handler saves the picture and links it (optionally adding OCR text). Later, the trained image-to-markdown model plugs in at the same spot.

## Usage

```bash
uv sync                                    # install
uv run f2m convert report.pdf              # print Markdown
uv run f2m convert docs/ -o out/ -r        # folder (recursive) -> out/<name>.md + out/<name>_assets/
uv run f2m convert docs/ -o out/ -r -w 4   # use 4 processes (default: all CPU cores; -w 1 = one at a time)
uv run f2m formats                         # list registered parsers
```

Folders are converted in parallel, one file per process (Python runs one thread of Python code at a
time per process, so more cores means more processes). Each file is independent, so the output is the
same whatever the number of workers.

Options: `--images link|ocr|none`, `--xlsx-formulas section|inline|none`, `--xlsx-deps`, `--xlsx-compute`,
`--merged-cells repeat|blank`, `--no-notes`, `--no-pdf-graphics`, `-v`.

```python
from f2m import convert, Options
md = convert("deck.pptx", "out/", options=Options(xlsx_formulas="inline"))
```

Optional extras: `uv sync --extra ocr` (also needs the Tesseract program on PATH), `uv sync --extra calc` (computes formula values that aren't stored in the file) and `uv sync --extra api` (the HTTP API below).

## HTTP API

```bash
uv sync --extra api
uv run f2m serve                    # http://127.0.0.1:8000, interactive docs at /docs
uv run f2m serve --host 0.0.0.0     # accept requests from other machines (only on a trusted network, or with a token)
```

Auth is optional and off by default. To require a bearer token on every endpoint except `/health`, set `F2M_API_TOKEN` before starting the server (or pass `--token`, which shows in process lists, so prefer the variable). Unset it to turn auth off again.

```bash
F2M_API_TOKEN=s3cret uv run f2m serve --host 0.0.0.0
curl -H "Authorization: Bearer s3cret" -F file=@report.pdf http://127.0.0.1:8000/convert
```

The Authorize button in `/docs` takes the same token.

### Docker

```bash
docker build -t f2m .
docker run -p 8000:8000 -e F2M_API_TOKEN=s3cret f2m
```

The image runs the API as a non-root user with a health check on `/health`. Leave out `-e F2M_API_TOKEN` to run without auth. Image-to-Markdown models are not included yet, so `images=model` isn't available (`ModelHandler` in `src/f2m/images/handlers.py` is the placeholder).

| Endpoint | Sends | Returns |
|---|---|---|
| `GET /health` | | `{"status": "ok"}` |
| `GET /formats` | | each format and its file extensions |
| `POST /convert` | `file` | JSON: `filename`, `format`, `markdown`, `warnings`, `assets` (images as base64, under the path the Markdown links to), `timings_ms` |
| `POST /convert/markdown` | `file` | the Markdown as `text/markdown` (add `images=none`, as image links would point nowhere) |
| `POST /convert/zip` | `files` (one or more) | zip with `<name>.md` + `<name>_assets/` per file, and `errors.txt` if some failed |

The CLI options are form fields: `images` (link/ocr/none), `xlsx_formulas` (section/inline/none), `xlsx_deps`, `xlsx_compute`, `merged_cells` (repeat/blank), `notes`, `pdf_graphics`, and `workers` for `/convert/zip` (0 = one process per file up to the core count; batches under 3 files run in one process).

```bash
curl -F file=@report.pdf http://127.0.0.1:8000/convert
curl -F file=@book.xlsx -F xlsx_formulas=inline -F images=none http://127.0.0.1:8000/convert/markdown
curl -F files=@a.pdf -F files=@b.docx -o out.zip http://127.0.0.1:8000/convert/zip
```

Errors: 400 empty file, 413 too large (`F2M_MAX_UPLOAD_MB`, default 200; `F2M_MAX_FILES` per zip request, default 100), 415 unsupported type, 422 bad option or a file that failed to convert. Uploaded files are kept in a temporary folder only while the request runs.

## How it works

```
file ─► detect (magic bytes, then extension) ─► registry ─► Parser.parse(bytes, ctx) ─► Document (blocks)
                                                                                         │
                     ImageBlocks ─► ImageHandler (link / OCR / future model) ◄───────────┤
                                                                                         ▼
                                                                         render/markdown.py ─► .md
```

- `document.py`: format-independent blocks (Heading, Paragraph, ListBlock, Table, ImageBlock, EmbeddedDocument...). Parsers never write Markdown themselves.
- `registry.py`: `Parser` base class and the `@register` decorator.
- `pipeline.py`: detect → parse → image handling → render. One bad file doesn't stop a folder run.
- `common/`: shared helpers for merged-cell tables, the Office chart XML reader, and OOXML zip/relationship handling.
- `images/`: `SaveAndLinkHandler`, `OcrHandler`, `CompositeHandler`, `DropHandler`, and the `ModelHandler` placeholder.

### What each parser handles

| Format | Handled with fixed rules | Sent to the image handler |
|---|---|---|
| DOCX | headings, lists (bullet/numbered from numbering.xml), bold/italic/links, native tables (gridSpan, vMerge, nested), charts (data from the chart XML), SmartArt text, text boxes, footnotes, **embedded xlsx/docx/pptx parsed by their own parser** | pictures, previews of old binary OLE objects |
| PPTX | one section per slide, shapes in reading order, bullets with levels, group shapes, native tables (merged cells), charts, SmartArt, speaker notes, **embedded Excel/Word objects** | pictures |
| XLSX | values table with A1 coordinates, **Formulas section** (filled-down/right formulas grouped), named ranges, Excel tables, data validation, comments, charts (from cache or by resolving their ranges), merged cells, % formats | pictures on sheets |
| PDF | headings by font size, bold headings, lists, paragraphs (hyphenation re-joined), deliberate line breaks kept (addresses, signatures), paragraphs split by PyMuPDF re-joined, table-of-contents pages as a nested list, tables (PyMuPDF), boxes drawn around text read as text (not as a table), header/footer removal, page markers | embedded images, **vector charts/diagrams rendered to PNG** |
| HTML | headings, paragraphs, nested lists, tables with colspan/rowspan, code blocks, quotes, figures + captions | `data:` URIs and local images (remote images stay as links) |
| Images | — | the whole file |

### XLSX: keeping the calculation logic

Every sheet becomes a values table with row numbers and column letters, so formula references can be followed. A **Formulas** table follows it:

| Cells | Formula | Value |
|---|---|---|
| D2:D5 | `=B2*C2` (filled down) | 7.5, 5, 5, … (4 cells) |
| D8 | `=D7*TaxRate` | 5.1 |

Add `--xlsx-deps` to get a "Reads from" column (e.g. `B2:B5, C2:C5`). `--xlsx-formulas inline` shows the formula inside the value cell instead. Formulas without a stored result show `(not computed)`; `--xlsx-compute` calculates them (requires the `calc` extra).

An embedded workbook (for example, Excel inside a slide) shows only its visible sheet and region by default (`Options.xlsx_embedded_full=True` renders the whole workbook).

### PDF text structure

A PDF stores positioned lines, not paragraphs, so the PDF parser rebuilds the structure with these rules:

- **Line breaks.** Lines of a text block are joined with a space when the text wrapped, and kept as a line break when the line was ended on purpose: if the next line's first word would have fit after it. Wrapped prose is measured against its own text box (so narrow slide boxes aren't broken up); blocks of short lines such as addresses are measured against the page column.
- **Split paragraphs.** A block that starts in lowercase, sits directly under the previous one in the same column, and follows text that didn't end a sentence is joined to it.
- **Pieces on one row** (`1.` + `Introduction`, or a contents entry + its page number) are joined first.
- **Table of contents.** Lines ending in leader dots and a page number become one list item each, nested by indent: `- 1.1 Background … 4`.
- **Boxes around text.** When the table finder reads a frame around a text box as a one-cell table, the text is read as normal text instead, and a real table inside the box is kept.
- **Text in figures.** Short labels inside a chart or diagram stay in its image; longer text (8+ words) stays as Markdown text.

### Languages

Text is Unicode throughout, so any language the file stores as text comes through (tested: accented Latin, Vietnamese, Greek, Cyrillic, Chinese, Japanese, Korean, Arabic, Hindi, Thai). For PDF:

- Special spaces (no-break, thin, full-width) become normal spaces.
- Chinese and Japanese lines are joined without a space where the text wrapped.
- Arabic stored as shaped letters in display order is turned back into standard letters in reading order.

Limits: right-to-left lines that mix in numbers or Latin words may place those on the wrong side (untested on real Arabic/Hebrew PDFs); the hyphenation and "lowercase start" rules only apply to scripts with capital letters; OCR defaults to English; PDFs whose fonts have no Unicode mapping give garbled text in any language.

## Adding a format

```python
from f2m import Parser, register, Document, ParseContext
from f2m.document import Paragraph

@register
class RtfParser(Parser):
    formats = ("rtf",)
    extensions = (".rtf",)

    def parse(self, data: bytes, ctx: ParseContext) -> Document:
        return Document([Paragraph(...)])
```

Put the module in `src/f2m/parsers/` and import it in `parsers/__init__.py`. Another package can instead expose the parser through the `f2m.parsers` entry-point group. If the format can be recognised from its bytes, add it to `detect.py`. Use `ctx.convert_embedded(bytes, name)` to parse files found inside the current file.

## Plugging in the image-to-markdown model

Implement a handler with `handle(image: ImageBlock, ctx) -> str`. `image.data` holds the bytes, and `image.location` holds the page, slide, sheet or cell where the image was found. Then pass the handler to the pipeline:

```python
from f2m import Pipeline
from f2m.images import CompositeHandler, SaveAndLinkHandler, ModelHandler

class MyModel(ModelHandler):
    def predict(self, image):
        return run_model(image.data)   # Markdown for a chart/table/diagram

Pipeline(image_handler=CompositeHandler(SaveAndLinkHandler(), MyModel())).convert_path("in/", "out/")
```

## Timing

Every step is timed: `read`, `parse` (with the parser's own steps under it), `images`, `render` and `write`. Steps nest as paths such as `parse/pdf/pages/tables`, and an embedded file's time appears under the step that found it (e.g. `parse/pptx/slides/ole_objects/embedded/xlsx/...`).

```bash
uv run f2m bench tests/real_docs                    # time every file (sub-folders included)
uv run f2m bench tests/real_docs -o out/real_docs   # also write the Markdown, so disk writes are counted
uv run f2m bench tests/real_docs --images none      # leave image handling out
uv run f2m bench tests/real_docs -w 0               # all CPU cores: much faster run
```

`bench` uses one process by default (`-w 1`). With more workers the whole run finishes sooner, but files
share the CPU, memory and disk, so each file's step times come out higher (about 2x on 12 cores).
Compare per-step times only between runs made with the same `-w`; the setting is saved in the report.

The report goes to `out/bench/<date-time>/` (or `--report DIR`):
- `summary.md`: per format: files, total, mean, p50/p95/max, ms per page/slide/sheet, MB/s. Then a step tree with each step's share of the time (`(other)` = time in a step that no smaller step covers, i.e. a place to add a timer). Then the slowest files with their slowest step, failures, and files with warnings.
- `files.csv`: one row per file, with the step columns. Open it in Excel to sort and filter.
- `timings.json`: everything, including the full step tree per file.

One file per format is run untimed first (`--no-warmup` to skip this), so one-off library start-up costs aren't counted. In your own code, `Result.timings` holds the same data, and `with f2m.timing.timed("name"):` adds a new step.

## Development

```bash
uv run pytest                              # tests build their own sample files
uv run python tests/make_fixtures.py       # write the samples to tests/fixtures/ to look at
uv run ruff check
```

## Not covered yet

- Legacy `.doc` / `.ppt` / `.xls` (could be added as a parser that converts through LibreOffice).
- Full-page OCR for scanned PDFs. Such pages are flagged with a warning, and their page image goes to the image handler.
- PDF reading order on complex multi-column layouts is a simple top-to-bottom, left-to-right sort.
- PDF paragraphs that continue onto the next page are split at the page marker, and numbered lists restart at 1 after a page break.
- `bench` has no per-file time limit: a file that hangs stops the run from finishing.
- OLE objects inside XLSX sheets, and the new "picture in cell" images.
