from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path

import pytest
from pypdf import PdfWriter

from cognityx_ingest.cli import main
from cognityx_ingest.control import (
    INGEST_RESULT_READ,
    ControlDecision,
    IngestAuthorizationError,
)
from cognityx_ingest.management import ARTIFACT_READ_NAMES, IngestManager
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


class _RecordingStorage:
    """Return fixture bytes while recording every attempted logical-key open."""

    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.opened: list[str] = []

    def open(self, key: str) -> BytesIO:
        self.opened.append(key)
        return BytesIO(self.payloads[key])


class _ArtifactAwareControl:
    """Allow metadata and ordinary artifacts but deny source-graph bytes."""

    def __init__(self, *, deny_source_graph: bool = True) -> None:
        self.deny_source_graph = deny_source_graph
        self.calls: list[tuple[str, object | None]] = []

    def authorize(
        self,
        context: ExecutionContext,
        action: str,
        resource: object | None = None,
        request: object | None = None,
    ) -> ControlDecision:
        self.calls.append((action, resource))
        denied = (
            self.deny_source_graph
            and isinstance(resource, dict)
            and resource.get("artifact") == "source-graph"
        )
        return ControlDecision(
            allowed=not denied,
            reason="source graph denied" if denied else None,
        )

    def report_usage(self, context: ExecutionContext, usage: object) -> None:
        return None


def _manager_fixture(
    *, deny_source_graph: bool = False
) -> tuple[IngestManager, _RecordingStorage, _ArtifactAwareControl, str]:
    document_id = "pdf-authorization-fixture"
    prefix = f"ingest/documents/{document_id}"
    payloads = {
        f"{prefix}/manifest.json": b'{"artifacts":{}}',
        f"{prefix}/document.json": b'{"document_id":"pdf-authorization-fixture"}',
        f"{prefix}/evidence.jsonl": b'{"evidence":true}\n',
        f"{prefix}/provenance.json": b'{"provenance":true}',
        f"{prefix}/canonical-content.json": b'{"canonical":true}',
        f"{prefix}/source-graph.json": b'{"graph":true}',
        f"{prefix}/provenance-addresses.json": b'{"addresses":true}',
        f"{prefix}/parser/observations.json": b'{"observations":true}',
        f"{prefix}/parser/fusion-decisions.json": b'{"fusion":true}',
    }
    storage = _RecordingStorage(payloads)
    control = _ArtifactAwareControl(deny_source_graph=deny_source_graph)
    manager = IngestManager(storage, JobRepository(), control=control)
    return manager, storage, control, document_id


def test_artifact_specific_denial_precedes_storage_open() -> None:
    manager, storage, control, document_id = _manager_fixture(
        deny_source_graph=True
    )
    context = ExecutionContext(
        run_id="run-authorization",
        correlation_id="correlation-authorization",
        principal_id="alice",
    )

    shown = manager.show_document(context, document_id)
    opened_for_metadata = tuple(storage.opened)

    assert shown["document"]["document_id"] == document_id
    with pytest.raises(IngestAuthorizationError, match="source graph denied"):
        manager.read_artifact(context, document_id, "source-graph")

    assert tuple(storage.opened) == opened_for_metadata
    assert control.calls[-1] == (
        INGEST_RESULT_READ,
        {"document_id": document_id, "artifact": "source-graph"},
    )


def test_manager_reads_only_the_closed_settled_artifact_vocabulary() -> None:
    manager, storage, control, document_id = _manager_fixture()
    context = ExecutionContext(
        run_id="run-allowed",
        correlation_id="correlation-allowed",
        principal_id="alice",
    )
    expected = {
        "document": "document.json",
        "evidence": "evidence.jsonl",
        "provenance": "provenance.json",
        "manifest": "manifest.json",
        "canonical-content": "canonical-content.json",
        "source-graph": "source-graph.json",
        "provenance-addresses": "provenance-addresses.json",
        "parser-observations": "parser/observations.json",
        "parser-fusion-decisions": "parser/fusion-decisions.json",
    }
    assert ARTIFACT_READ_NAMES == tuple(expected)

    for name, filename in expected.items():
        assert manager.read_artifact(context, document_id, name)
        assert storage.opened[-1] == f"ingest/documents/{document_id}/{filename}"
        assert control.calls[-1] == (
            INGEST_RESULT_READ,
            {"document_id": document_id, "artifact": name},
        )


@pytest.mark.parametrize(
    "name",
    (
        "parser/pymupdf",
        "../../source-graph",
        "storage://local-main/artifacts/source-graph.json",
        "source_graph",
    ),
)
def test_manager_rejects_raw_parser_traversal_uri_and_underscore_names(
    name: str,
) -> None:
    manager, storage, _control, document_id = _manager_fixture()
    context = ExecutionContext(
        run_id="run-rejected",
        correlation_id="correlation-rejected",
    )

    with pytest.raises(ValueError, match="Unknown artifact"):
        manager.read_artifact(context, document_id, name)
    assert storage.opened == []


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
    for name in (
        "manifest",
        "provenance",
        "canonical-content",
        "source-graph",
        "provenance-addresses",
    ):
        assert main(
            ["artifacts", "read", document_id, name, "--storage-root", str(storage_root)]
        ) == 0
        assert _json_output(capsys)["artifact"] == name

    assert main(["documents", "delete", document_id, "--yes", "--storage-root", str(storage_root)]) == 0
    assert _json_output(capsys) == {"deleted_document_id": document_id}
    assert main(["documents", "list", "--storage-root", str(storage_root)]) == 0
    assert _json_output(capsys) == []


def test_cli_cancellation_is_owner_scoped_and_retains_job_history(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    storage_root = tmp_path / "storage"
    database = storage_root / "catalog" / "ingest" / "jobs.sqlite3"
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


def test_cli_lists_shows_and_deletes_run_metadata_without_deleting_documents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "run.pdf"
    storage_root = tmp_path / "storage"
    _write_pdf(source)

    assert main(["ingest", str(source), "--storage-root", str(storage_root)]) == 0
    result = _json_output(capsys)
    run_id = result["run_id"]
    document_id = result["documents"][0]["document_id"]

    assert main(["runs", "list", "--storage-root", str(storage_root)]) == 0
    assert _json_output(capsys)[0]["run_id"] == run_id
    assert main(["runs", "show", run_id, "--storage-root", str(storage_root)]) == 0
    assert _json_output(capsys)["run_id"] == run_id
    assert main(
        ["runs", "delete", run_id, "--yes", "--storage-root", str(storage_root)]
    ) == 0
    assert _json_output(capsys) == {"deleted_run_id": run_id}
    assert main(["documents", "show", document_id, "--storage-root", str(storage_root)]) == 0
    assert _json_output(capsys)["document"]["document_id"] == document_id


@pytest.mark.parametrize(
    "name",
    (
        "source",
        "parser/pymupdf",
        "../../source-graph",
        "storage://local-main/artifacts/source-graph.json",
        "source_graph",
    ),
)
def test_unsafe_names_are_not_generated_document_artifacts(
    tmp_path: Path, name: str
) -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "artifacts",
                "read",
                "pdf-0123456789abcdef",
                name,
                "--storage-root",
                str(tmp_path / "storage"),
            ]
        )


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
