from __future__ import annotations

import json
from pathlib import Path

import pytest
from pypdf import PdfWriter

from cognityx_ingest.cli import main
from cognityx_ingest.management import IngestManager
from cognityx_ingest.models import ExecutionContext
from cognityx_jobs import JobRepository
from cognityx_storage import LocalStorageBackend, StorageClient


def _json_output(capsys: pytest.CaptureFixture[str]) -> object:
    return json.loads(capsys.readouterr().out)


def _write_pdf(path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as stream:
        writer.write(stream)


def test_cli_end_to_end_manages_job_and_generated_artifacts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "policy.pdf"
    storage_root = tmp_path / "storage"
    _write_pdf(source)

    assert main(["ingest", str(source), "--storage-root", str(storage_root)]) == 0
    ingest = _json_output(capsys)[0]
    document_id = ingest["document_id"]
    job_id = ingest["job_id"]

    assert main(["jobs", "list", "--storage-root", str(storage_root)]) == 0
    assert _json_output(capsys)[0]["job_id"] == job_id
    assert main(["jobs", "show", job_id, "--storage-root", str(storage_root)]) == 0
    assert _json_output(capsys)["job"]["state"] == "completed"

    assert main(["documents", "list", "--storage-root", str(storage_root)]) == 0
    assert _json_output(capsys)[0]["document_id"] == document_id
    assert main(["documents", "show", document_id, "--storage-root", str(storage_root)]) == 0
    assert _json_output(capsys)["document"]["document_id"] == document_id
    assert main(["artifacts", "read", document_id, "manifest", "--storage-root", str(storage_root)]) == 0
    assert "manifest.json" not in _json_output(capsys)["content"]

    assert main(["documents", "delete", document_id, "--yes", "--storage-root", str(storage_root)]) == 0
    assert _json_output(capsys) == {"deleted_document_id": document_id}
    assert main(["documents", "list", "--storage-root", str(storage_root)]) == 0
    assert _json_output(capsys) == []


def test_cli_cancellation_is_owner_scoped_and_retains_job_history(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    storage_root = tmp_path / "storage"
    database = storage_root / ".cognityx-ingest" / "jobs.sqlite3"
    database.parent.mkdir(parents=True)
    jobs = JobRepository(str(database))
    jobs.create("job-running", "ingest.pdf", {"document_id": "pdf-example"}, owner_id="alex")
    jobs.set_state("job-running", "running")

    assert main(["jobs", "cancel", "job-running", "--storage-root", str(storage_root), "--owner-id", "alex"]) == 0
    assert _json_output(capsys)["state"] == "cancellation_requested"

    storage = StorageClient(LocalStorageBackend(storage_root)).for_shared_data()
    manager = IngestManager(storage, jobs)
    context = ExecutionContext(run_id="run", correlation_id="correlation", principal_id="bob")
    with pytest.raises(KeyError):
        manager.show_job(context, "job-running", owner_id="bob")
    with pytest.raises(PermissionError, match="does not match"):
        manager.show_job(ExecutionContext(run_id="run", correlation_id="correlation", principal_id="alex"), "job-running", owner_id="bob")


def test_cli_document_delete_requires_explicit_confirmation(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["documents", "delete", "pdf-0123456789abcdef", "--storage-root", str(tmp_path / "storage")])


def test_cli_registers_and_lists_sources_and_bundles(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    storage_root = tmp_path / "storage"
    source = tmp_path / "notes.txt"
    source.write_text("durable source", encoding="utf-8")

    assert main(["sources", "add", str(source), "--storage-root", str(storage_root)]) == 0
    created = _json_output(capsys)
    assert created["status"] == "created"
    assert main(["sources", "add", str(source), "--storage-root", str(storage_root)]) == 0
    assert _json_output(capsys)["status"] == "already_registered"

    assert main(["bundles", "create", "phd/rag", "--storage-root", str(storage_root)]) == 0
    assert _json_output(capsys)["path"] == "phd/rag"
    assert main(["sources", "add", str(source), "--bundle", "phd/rag", "--storage-root", str(storage_root)]) == 0
    assert _json_output(capsys)["status"] == "created"
    assert main(["sources", "list", "--bundle", "phd/rag", "--storage-root", str(storage_root)]) == 0
    assert len(_json_output(capsys)) == 1
    assert main(["sources", "show", created["source_id"], "--storage-root", str(storage_root)]) == 0
    assert _json_output(capsys)["source_id"] == created["source_id"]
