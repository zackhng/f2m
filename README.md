# f2m: files to Markdown

Converts PDF, DOCX, PPTX, XLSX, HTML and image files (PNG, JPG, GIF, BMP, TIFF, WebP) into Markdown.

Text that can be read directly from the file is extracted with fixed rules. This covers headings, paragraphs, lists, links, native tables, chart data, formulas, and Office files embedded inside other files. Pictures go to a pluggable **image handler**. For now that handler saves the picture and links it (optionally adding OCR text). Later, the trained image-to-markdown model plugs in at the same spot.

## Usage

```bash
uv sync                                    # install
uv run f2m convert report.pdf              # print Markdown
uv run f2m convert docs/ -o out/ -r        # folder (recursive) -> out/<name>.md + out/<name>_assets/
uv run f2m formats                         # list registered parsers
```

Options: `--images link|ocr|none`, `--xlsx-formulas section|inline|none`, `--xlsx-deps`, `--xlsx-compute`,
`--merged-cells repeat|blank`, `--no-notes`, `--no-pdf-graphics`, `-v`.

```python
from f2m import convert, Options
md = convert("deck.pptx", "out/", options=Options(xlsx_formulas="inline"))
```

Optional extras: `uv sync --extra ocr` (also needs the Tesseract program on PATH) and `uv sync --extra calc` (computes formula values that aren't stored in the file).

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
| PDF | headings by font size, bold headings, lists, paragraphs (hyphenation re-joined), tables (PyMuPDF), header/footer removal, page markers | embedded images, **vector charts/diagrams rendered to PNG** |
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
```

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
- OLE objects inside XLSX sheets, and the new "picture in cell" images.
