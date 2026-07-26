from __future__ import annotations

import json
from pathlib import Path

import pytest
from pypdf import PdfWriter

from cognityx_ingest.control import ControlDecision, IngestAuthorizationError, IngestLimitError
from cognityx_ingest.models import ExecutionContext, UsageReport
from cognityx_ingest.parser import ExtractedPage, UnsupportedInputError
from cognityx_ingest.service import IngestService
from cognityx_jobs import JobRepository
from cognityx_storage import LocalStorageBackend, StorageClient


class StubExtractor:
    def extract(self, path: Path) -> tuple[ExtractedPage, ...]:
        return (ExtractedPage(1, "First page."), ExtractedPage(2, "Second page."))


class RecordingControl:
    def __init__(self, decision: ControlDecision) -> None:
        self.decision = decision
        self.actions: list[str] = []
        self.usage: list[UsageReport] = []

    def authorize(self, context: ExecutionContext, action: str, resource: object | None = None, request: object | None = None) -> ControlDecision:
        self.actions.append(action)
        return self.decision

    def report_usage(self, context: ExecutionContext, usage: UsageReport) -> None:
        self.usage.append(usage)


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
    assert manifest["schema"] == "cognityx.ingest.document"
    assert manifest["artifacts"]["evidence"]["uri"].startswith("storage://")
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
    assert [event["event"] for event in jobs.events(record.job_id)] == ["ingest_submitted", "ingest_queued", "ingest_started", "ingest_completed"]


def test_control_authorizes_limits_and_receives_measured_usage(tmp_path: Path) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-1.4 test")
    control = RecordingControl(ControlDecision(allowed=True, limits={"max_document_size": 100, "max_pages": 2}))
    storage = StorageClient(LocalStorageBackend(tmp_path / "storage")).for_shared_data()
    context = ExecutionContext(run_id="run-1", correlation_id="correlation-1", principal_id="alex")

    result = IngestService(storage, extractor=StubExtractor(), control=control).ingest(source, context=context)

    assert control.actions == ["ingest.job.submit"]
    assert result.run_id == "run-1"
    assert result.usage is control.usage[0]
    assert control.usage[0].pages == 2
    assert control.usage[0].input_bytes == len(source.read_bytes())
    assert all(artifact.artifact_id.startswith("art-pdf-") for artifact in result.artifacts)


def test_control_rejection_and_limits_stop_ingestion(tmp_path: Path) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-1.4 test")
    storage = StorageClient(LocalStorageBackend(tmp_path / "storage")).for_shared_data()

    with pytest.raises(IngestAuthorizationError, match="denied"):
        IngestService(storage, extractor=StubExtractor(), control=RecordingControl(ControlDecision(False, reason="denied"))).ingest(source)
    with pytest.raises(IngestLimitError, match="max_pages"):
        IngestService(storage, extractor=StubExtractor(), control=RecordingControl(ControlDecision(True, limits={"max_pages": 1}))).ingest(source)


def test_cli_end_to_end_uses_real_pdf_parser_and_local_storage(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from cognityx_ingest.cli import main

    source = tmp_path / "real.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with source.open("wb") as stream:
        writer.write(stream)

    assert main(["ingest", str(source), "--storage-root", str(tmp_path / "storage")]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result[0]["run_id"]
    assert result[0]["document_id"].startswith("pdf-")
    assert result[0]["artifacts"][0]["uri"].startswith("storage://")
