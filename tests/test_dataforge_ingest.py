from __future__ import annotations

import json
from pathlib import Path

import pytest
from pypdf import PdfWriter

from cognityx_ingest.cli import main
from cognityx_ingest.models import Evidence, ExecutionContext
from cognityx_ingest.parser import ExtractedPage, UnsupportedInputError
from cognityx_ingest.service import IngestService
from cognityx_ingest.source_assets import SourceAssetRegistry
from cognityx_jobs import JobRepository
from cognityx_storage import (
    LocalStorageBackend,
    StorageClient,
    StorageConfig,
    StorageRuntime,
)


class ContentExtractor:
    def extract(self, path: Path) -> tuple[ExtractedPage, ...]:
        content = path.read_bytes()
        if b"bad" in content:
            raise UnsupportedInputError("bad PDF")
        return (ExtractedPage(1, "page text"),)


def _context(run_id: str = "run-dataforge") -> ExecutionContext:
    return ExecutionContext(
        run_id=run_id,
        correlation_id=f"correlation-{run_id}",
        principal_id="alex",
        tenant_id="tenant-a",
    )


def _components(
    root: Path,
) -> tuple[IngestService, SourceAssetRegistry, StorageClient, JobRepository]:
    runtime = StorageRuntime.from_config(StorageConfig.built_in(root=root))
    registry = SourceAssetRegistry.load(runtime=runtime)
    storage = StorageClient(LocalStorageBackend(root)).for_shared_data()
    jobs = JobRepository()
    service = IngestService(
        storage, extractor=ContentExtractor(), jobs=jobs, registry=registry
    )
    return service, registry, storage, jobs


def test_path_ingest_registers_source_asset_and_emits_v2_lineage(
    tmp_path: Path,
) -> None:
    service, registry, storage, _ = _components(tmp_path / "storage")
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"good PDF")

    result = service.ingest(source, context=_context(), registry=registry)
    asset = registry.show_asset(_context(), result.document.source.source_id)
    evidence = result.evidence[0].to_dict()

    assert asset.sha256 == result.document.source.sha256
    assert evidence["schema_version"] == "cognityx.ingest.evidence/v2"
    assert evidence["source_asset_id"] == asset.asset_id
    assert evidence["bundle_id"] == asset.bundle_id
    assert evidence["context_id"] == asset.context_id
    assert evidence["source_sha256"] == asset.sha256
    assert evidence["run_id"] == "run-dataforge"
    assert storage.exists(result.evidence_key)


def test_ingest_asset_and_bundle_use_registered_assets(tmp_path: Path) -> None:
    service, registry, _, _ = _components(tmp_path / "storage")
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    execution = _context("run-register")
    one = registry.register_asset(execution, first, bundle="dataset")
    registry.register_asset(execution, second, bundle="dataset/nested")
    bundle = registry.resolve_doc_bundle(execution, "dataset", create=False)

    asset_result = service.ingest_asset(
        one.asset_id, registry, _context("run-asset")
    )
    bundle_result = service.ingest_bundle(
        bundle.bundle_id, registry, _context("run-bundle")
    )

    assert asset_result.document.source.source_id == one.asset_id
    assert bundle_result.document_count == 2
    assert bundle_result.root_bundle_id == bundle.bundle_id


def test_recursive_folder_preserves_bundles_and_partial_success(
    tmp_path: Path,
) -> None:
    service, registry, storage, jobs = _components(tmp_path / "storage")
    folder = tmp_path / "collection"
    nested = folder / "year" / "month"
    nested.mkdir(parents=True)
    (folder / "good.pdf").write_bytes(b"good")
    (nested / "bad.pdf").write_bytes(b"bad")
    (nested / "also-good.PDF").write_bytes(b"also good")
    (folder / "ignored.txt").write_text("ignored", encoding="utf-8")

    run = service.ingest_path(
        folder, context=_context(), registry=registry, owner_id="alex"
    )
    bundles = {item.path for item in registry.list_doc_bundles(_context())}
    manifest = json.load(storage.open(run.run_manifest_key))
    events = [item["event"] for item in jobs.events(run.job_id)]

    assert {"collection", "collection/year", "collection/year/month"} <= bundles
    assert run.document_count == 2
    assert run.failed_count == 1
    assert manifest["document_ids"] == [
        item.document.document_id for item in run.results
    ]
    assert manifest["failed_files"][0]["asset_id"]
    assert events.count("folder_discovered") == 1
    assert events.count("asset_registered") == 3
    assert "document_failed" in events
    assert events[-1] == "run_completed"


def test_job_events_are_replayable_from_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "paper.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with source.open("wb") as stream:
        writer.write(stream)
    root = tmp_path / "storage"
    assert main(["ingest", str(source), "--storage-root", str(root)]) == 0
    job_id = json.loads(capsys.readouterr().out)["job_id"]

    assert main(["jobs", "events", job_id, "--storage-root", str(root)]) == 0
    events = json.loads(capsys.readouterr().out)
    assert [item["sequence"] for item in events] == list(
        range(1, len(events) + 1)
    )


def test_cli_ingests_asset_and_bundle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "paper.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with source.open("wb") as stream:
        writer.write(stream)
    root = tmp_path / "storage"

    assert main(
        [
            "assets",
            "add",
            str(source),
            "--bundle",
            "dataforge",
            "--storage-root",
            str(root),
        ]
    ) == 0
    asset_id = json.loads(capsys.readouterr().out)["asset_id"]
    assert main(
        ["doc-bundles", "list", "--storage-root", str(root)]
    ) == 0
    bundles = json.loads(capsys.readouterr().out)
    bundle_id = next(
        item["bundle_id"] for item in bundles if item["path"] == "dataforge"
    )

    assert main(
        ["ingest", "--asset", asset_id, "--storage-root", str(root)]
    ) == 0
    asset_run = json.loads(capsys.readouterr().out)
    assert asset_run["document_count"] == 1
    assert asset_run["root_bundle_id"] == bundle_id

    assert main(
        ["ingest", "--bundle", bundle_id, "--storage-root", str(root)]
    ) == 0
    bundle_run = json.loads(capsys.readouterr().out)
    assert bundle_run["document_count"] == 1
    assert bundle_run["root_bundle_id"] == bundle_id


def test_v1_evidence_remains_readable() -> None:
    legacy = Evidence.from_dict(
        {
            "evidence_id": "pdf-old:page:1",
            "document_id": "pdf-old",
            "page_number": 1,
            "text": "legacy",
            "char_start": 0,
            "char_end": 6,
        }
    )

    assert legacy.schema_version == "cognityx.ingest.evidence/v1"
    assert legacy.source_asset_id is None
