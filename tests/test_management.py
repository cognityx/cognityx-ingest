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
    ingest = _json_output(capsys)
    document_id = ingest["documents"][0]["document_id"]
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


def test_source_cli_uses_storage_config_and_rejects_root_combination(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    storage_root = tmp_path / "configured"
    source = tmp_path / "configured.txt"
    source.write_text("configured runtime", encoding="utf-8")
    config = tmp_path / "storage.toml"
    config.write_text(
        "\n".join(
            [
                "[storage]",
                'default_profile = "local-main"',
                "",
                "[storage.profiles.local-main]",
                'type = "filesystem"',
                f'root = "{storage_root}"',
                "",
                    "[storage.roles.source_asset]",
                    'profile = "local-main"',
                    'namespace = "source-assets"',
                    'dedup_scope = "tenant"',
                    "",
                    "[storage.roles.catalog]",
                    'profile = "local-main"',
                    'namespace = "catalog"',
                    'preferred_capabilities = ["native_path", "random_write", "file_locking"]',
                ]
        ),
        encoding="utf-8",
    )

    assert main(
        [
            "sources",
            "add",
            str(source),
            "--storage-config",
            str(config),
        ]
    ) == 0
    assert _json_output(capsys)["status"] == "created"
    assert (
        storage_root / "catalog/ingest/source_catalog.sqlite3"
    ).is_file()

    with pytest.raises(SystemExit):
        main(
            [
                "sources",
                "list",
                "--storage-config",
                str(config),
                "--storage-root",
                str(storage_root),
            ]
        )


def test_cli_context_file_override_scope_and_local_fallback(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
    storage_root, source = tmp_path / "storage", tmp_path / "source.txt"
    source.write_text("context", encoding="utf-8")
    context = tmp_path / "context.json"
    context.write_text('{"principal_id":"alice","workspace_id":"dev","scopes":{"repo":"ingest"}}', encoding="utf-8")

    assert main(["sources", "add", str(source), "--context", str(context), "--workspace-id", "test", "--scope", "function=trial", "--storage-root", str(storage_root)]) == 0
    result = _json_output(capsys)
    import sqlite3
    with sqlite3.connect(storage_root / "catalog/ingest/source_catalog.sqlite3") as db:
        descriptors = json.loads(db.execute("SELECT descriptors_json FROM contexts WHERE context_id=?", (result["context_id"],)).fetchone()[0])
    assert descriptors["workspace_id"] == "test"
    assert descriptors["repo"] == "ingest"
    assert descriptors["function"] == "trial"

    monkeypatch.setenv("COGNITYX_CONTEXT_FILE", str(context))
    assert main(["sources", "add", str(source), "--bundle", "env", "--storage-root", str(storage_root)]) == 0
    environment_result = _json_output(capsys)
    with sqlite3.connect(storage_root / "catalog/ingest/source_catalog.sqlite3") as db:
        environment_descriptors = json.loads(db.execute("SELECT descriptors_json FROM contexts WHERE context_id=?", (environment_result["context_id"],)).fetchone()[0])
    assert environment_descriptors["workspace_id"] == "dev"
    monkeypatch.delenv("COGNITYX_CONTEXT_FILE")
    assert main(["sources", "add", str(source), "--bundle", "local", "--storage-root", str(storage_root)]) == 0
    assert _json_output(capsys)["context_id"] != result["context_id"]
    project = tmp_path / "project"; (project / ".cognityx").mkdir(parents=True)
    (project / ".cognityx/context.json").write_text('{"project_id":"project-context"}', encoding="utf-8")
    monkeypatch.chdir(project)
    assert main(["sources", "add", str(source), "--bundle", "project", "--storage-root", str(storage_root)]) == 0
    project_result = _json_output(capsys)
    with sqlite3.connect(storage_root / "catalog/ingest/source_catalog.sqlite3") as db:
        project_descriptors = json.loads(db.execute("SELECT descriptors_json FROM contexts WHERE context_id=?", (project_result["context_id"],)).fetchone()[0])
    assert project_descriptors["project_id"] == "project-context"
