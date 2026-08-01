from __future__ import annotations

from pathlib import Path

from cognityx_ingest import CanonicalDocument, SourceAssetRegistry
from cognityx_resource import ExecutionContext
from cognityx_storage import StorageConfig, StorageRuntime


def _context(run_id: str) -> ExecutionContext:
    return ExecutionContext(
        run_id=run_id,
        correlation_id=f"correlation-{run_id}",
        principal_id="fixture-test",
    )


def test_same_fixture_bytes_reuse_blob_but_retain_logical_occurrences(
    tmp_path: Path, provenance_pdf: Path, ground_truth: dict[str, object]
) -> None:
    first_path = tmp_path / "policy-current.pdf"
    second_path = tmp_path / "policy-alias.pdf"
    first_path.write_bytes(provenance_pdf.read_bytes())
    second_path.write_bytes(provenance_pdf.read_bytes())
    runtime = StorageRuntime.from_config(StorageConfig.built_in(root=tmp_path / "runtime"))
    registry = SourceAssetRegistry.load(runtime=runtime)
    context = _context("run-aliases")

    first = registry.register_asset(context, first_path, bundle="hr/current")
    second = registry.register_asset(context, second_path, bundle="research/alias")
    first_asset = registry.show_asset(context, first.asset_id)
    second_asset = registry.show_asset(context, second.asset_id)

    assert first.sha256 == ground_truth["document"]["pdf_sha256"]
    assert second.sha256 == first.sha256
    assert second.asset_id != first.asset_id
    assert second_asset.blob_id == first_asset.blob_id
    assert second_asset.original_filename != first_asset.original_filename
    assert second_asset.bundle_id != first_asset.bundle_id


def test_v1_document_remains_readable() -> None:
    document = CanonicalDocument.from_dict(
        {
            "document_id": "legacy-document",
            "schema_version": "cognityx.ingest.document/v1",
            "source": {
                "source_id": "src-legacy",
                "filename": "legacy.pdf",
                "sha256": "a" * 64,
                "size_bytes": 1,
                "storage_key": "sourceasset://src-legacy",
                "media_type": "application/pdf",
            },
            "title": "Legacy",
            "sections": [],
        }
    )
    assert document.schema_version == "cognityx.ingest.document/v1"
    assert document.pages == ()
