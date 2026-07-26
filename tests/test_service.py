from __future__ import annotations

import json
from pathlib import Path

import pytest

from cognityx_ingest.parser import ExtractedPage, UnsupportedInputError
from cognityx_ingest.service import IngestService
from cognityx_jobs import JobRepository
from cognityx_storage import LocalStorageBackend, StorageClient


class StubExtractor:
    def extract(self, path: Path) -> tuple[ExtractedPage, ...]:
        return (ExtractedPage(1, "First page."), ExtractedPage(2, "Second page."))


@pytest.fixture
def service(tmp_path: Path) -> IngestService:
    storage = StorageClient(LocalStorageBackend(tmp_path / "storage")).for_shared_data()
    return IngestService(storage, extractor=StubExtractor())


def test_ingest_persists_canonical_provenance_artifacts(service: IngestService, tmp_path: Path) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-1.4 test")

    result = service.ingest(source)

    assert result.document.document_id.startswith("pdf-")
    assert [item.page_number for item in result.evidence] == [1, 2]
    assert result.evidence[0].evidence_id.endswith("page:1")
    storage = service._storage
    assert storage.exists(result.document.source.storage_key)
    manifest = json.load(storage.open(result.manifest_key))
    assert manifest["artifacts"]["evidence"] == result.evidence_key
    assert storage.open(result.evidence_key).read().decode().count("\n") == 2


def test_same_source_is_idempotent(service: IngestService, tmp_path: Path) -> None:
    source = tmp_path / "same.pdf"
    source.write_bytes(b"%PDF-1.4 same")

    first = service.ingest(source)
    second = service.ingest(source)

    assert first.document.document_id == second.document.document_id
    assert first.manifest_key == second.manifest_key


def test_folder_discovers_only_pdfs(service: IngestService, tmp_path: Path) -> None:
    (tmp_path / "one.pdf").write_bytes(b"one")
    (tmp_path / "two.PDF").write_bytes(b"two")
    (tmp_path / "ignored.txt").write_text("ignore")

    assert len(service.ingest_path(tmp_path)) == 2


def test_rejects_unsupported_input(service: IngestService, tmp_path: Path) -> None:
    source = tmp_path / "not-a-pdf.txt"
    source.write_text("no")

    with pytest.raises(UnsupportedInputError):
        service.ingest(source)


def test_rejects_malformed_pdf(tmp_path: Path) -> None:
    source = tmp_path / "broken.pdf"
    source.write_bytes(b"this is not a PDF")
    storage = StorageClient(LocalStorageBackend(tmp_path / "storage")).for_shared_data()

    with pytest.raises(UnsupportedInputError):
        IngestService(storage).ingest(source)


def test_records_job_events_for_successful_ingest(tmp_path: Path) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-1.4 test")
    jobs = JobRepository()
    storage = StorageClient(LocalStorageBackend(tmp_path / "storage")).for_shared_data()

    IngestService(storage, extractor=StubExtractor(), jobs=jobs).ingest(source, owner_id="alex")

    record = jobs.list_for_owner("alex")[0]
    assert record.state == "completed"
    assert [event["event"] for event in jobs.events(record.job_id)] == ["ingest_started", "ingest_completed"]
