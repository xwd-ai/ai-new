from types import SimpleNamespace

import pytest

from backend.services import document_text
from backend.tests.test_api_contracts import _headers, api_client


def test_markdown_upload_uses_existing_ingest_and_deduplication(api_client):
    path = "/api/knowledge/ingest/file?filename=export.md&kind=faq&keywords=CSV"
    content = "# Export\n\n请使用 CSV 导出。".encode()
    forbidden = api_client.post(path, content=content, headers=_headers(api_client, "customer"))
    assert forbidden.status_code == 403
    result = api_client.post(path, content=content, headers=_headers(api_client))
    assert result.status_code == 200
    assert result.json()["created"] is True
    repeated = api_client.post(path, content=content, headers=_headers(api_client))
    assert repeated.status_code == 200
    assert repeated.json()["created"] is False
    assert repeated.json()["knowledge_id"] == result.json()["knowledge_id"]


@pytest.mark.parametrize(("name", "data", "status"), [
    ("file.docx", b"unsupported", 415),
    ("file.md", b"\xff", 422),
    ("file.md", b"   ", 422),
    ("file.pdf", b"not a PDF", 422),
    ("file.md", b"x" * (8 * 1024 * 1024 + 1), 413),
])
def test_invalid_document_upload_is_rejected(api_client, name, data, status):
    response = api_client.post(
        "/api/knowledge/ingest/file", params={"filename": name},
        content=data, headers=_headers(api_client),
    )
    assert response.status_code == status


def test_pdf_extracts_text_and_runs_ocr_only_for_scanned_page(api_client, monkeypatch):
    commands = []
    replies = iter([
        b"Pages:          2\n",
        b"Digitally generated document page\f\f",
        b"",
        "扫描资料：导出步骤".encode(),
    ])

    def fake_run(args, **options):
        commands.append(args)
        assert options["env"]["OMP_THREAD_LIMIT"] == "1"
        assert options["timeout"] <= 24
        return SimpleNamespace(stdout=next(replies))

    monkeypatch.setattr(document_text.subprocess, "run", fake_run)
    response = api_client.post(
        "/api/knowledge/ingest/file?filename=mixed.pdf",
        content=b"%PDF-1.4\n", headers=_headers(api_client),
    )
    assert response.status_code == 200
    assert [args[0] for args in commands] == ["pdfinfo", "pdftotext", "pdftoppm", "tesseract"]
    assert commands[2][1:5] == ("-f", "2", "-l", "2")
    assert commands[3][-2:] == ("-l", "chi_sim+eng")
