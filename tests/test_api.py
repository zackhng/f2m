"""HTTP API (needs the api extra)."""

from __future__ import annotations

import base64
import io
import zipfile

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from f2m.api import app  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def _no_token(monkeypatch):
    monkeypatch.delenv("F2M_API_TOKEN", raising=False)


def _upload(path, name=None):
    return (name or path.name, path.read_bytes())


def test_auth_off_by_default():
    assert client.get("/formats").status_code == 200
    assert client.post("/convert/markdown", files={"file": ("a.html", b"<p>hi</p>")}).status_code == 200


def test_token_protects_everything_but_health(monkeypatch):
    monkeypatch.setenv("F2M_API_TOKEN", "s3cret")
    good = {"Authorization": "Bearer s3cret"}
    html = {"file": ("a.html", b"<p>hi</p>")}
    assert client.get("/health").status_code == 200
    r = client.get("/formats")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert client.get("/formats", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/formats", headers={"Authorization": "Basic s3cret"}).status_code == 401
    assert client.get("/formats", headers=good).status_code == 200
    assert client.post("/convert/markdown", files=html).status_code == 401
    assert client.post("/convert", files=html).status_code == 401
    assert client.post("/convert/zip", files=[("files", ("a.html", b"<p>hi</p>"))]).status_code == 401
    assert client.post("/convert/markdown", files=html, headers=good).status_code == 200


def test_unsetting_token_turns_auth_off(monkeypatch):
    monkeypatch.setenv("F2M_API_TOKEN", "s3cret")
    assert client.get("/formats").status_code == 401
    monkeypatch.delenv("F2M_API_TOKEN")
    assert client.get("/formats").status_code == 200


def test_docs_stay_open_with_token_on(monkeypatch):
    monkeypatch.setenv("F2M_API_TOKEN", "s3cret")
    assert client.get("/docs").status_code == 200
    assert "bearer" in client.get("/openapi.json").text.lower()


def test_health_and_formats():
    assert client.get("/health").json() == {"status": "ok"}
    fmts = {f["format"]: f["extensions"] for f in client.get("/formats").json()}
    assert {"pdf", "docx", "pptx", "xlsx", "html"} <= set(fmts)
    assert ".pdf" in fmts["pdf"]


def test_convert_json_with_assets(files):
    r = client.post("/convert", files={"file": _upload(files["pptx"])})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["format"] == "pptx" and body["filename"] == files["pptx"].name
    assert body["markdown"].strip()
    assert body["assets"], "the sample deck has a picture"
    for a in body["assets"]:
        assert a["path"] in body["markdown"] or a["path"].split("/", 1)[0] + "/" in body["markdown"]
        assert base64.b64decode(a["data"])
    assert "parse" in body["timings_ms"]


def test_convert_options_are_applied(files):
    inline = client.post("/convert/markdown", files={"file": _upload(files["xlsx"])},
                         data={"xlsx_formulas": "none", "images": "none"})
    section = client.post("/convert/markdown", files={"file": _upload(files["xlsx"])},
                          data={"images": "none"})
    assert inline.status_code == section.status_code == 200
    assert inline.headers["content-type"].startswith("text/markdown")
    assert "Formulas" in section.text and "Formulas" not in inline.text


def test_bad_option_and_bad_file():
    r = client.post("/convert", files={"file": ("a.pdf", b"%PDF-1.4")}, data={"images": "maybe"})
    assert r.status_code == 422
    r = client.post("/convert", files={"file": ("notes.txt", b"just some text")})
    assert r.status_code == 415
    r = client.post("/convert", files={"file": ("empty.pdf", b"")})
    assert r.status_code == 400


def test_upload_name_cannot_escape_temp_folder(files):
    r = client.post("/convert", files={"file": _upload(files["html"], "../../evil/page.html")})
    assert r.status_code == 200
    assert r.json()["filename"] == "page.html"


def test_zip_batch_with_duplicate_names_and_a_failure(files):
    upload = [
        ("files", _upload(files["docx"], "report.docx")),
        ("files", _upload(files["pdf"], "report.pdf")),
        ("files", _upload(files["html"], "report.pdf")),  # same name again
        ("files", ("readme.txt", b"no parser")),
    ]
    r = client.post("/convert/zip", files=upload, data={"workers": "1"})
    assert r.status_code == 200, r.text
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    assert {"report_docx.md", "report_pdf.md", "report_2.md"} <= names
    assert "readme.txt" in z.read("errors.txt").decode()


def test_zip_all_unsupported():
    r = client.post("/convert/zip", files=[("files", ("a.txt", b"x")), ("files", ("b.txt", b"y"))])
    assert r.status_code == 415
