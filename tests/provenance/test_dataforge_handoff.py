from __future__ import annotations

import json
from pathlib import Path

import pytest

from cognityx_ingest import IngestService, PyPdfExtractor, SourceAssetRegistry
from cognityx_resource import ExecutionContext
from cognityx_storage import LocalStorageBackend, StorageClient, StorageConfig, StorageRuntime


def test_dataforge_can_load_provenance_without_reopening_pdf(
    tmp_path: Path, provenance_pdf: Path, ground_truth: dict[str, object]
) -> None:
    runtime = StorageRuntime.from_config(StorageConfig.built_in(root=tmp_path / "runtime"))
    registry = SourceAssetRegistry.load(runtime=runtime)
    storage = StorageClient(LocalStorageBackend(tmp_path / "artifacts")).for_shared_data()
    context = ExecutionContext(
        run_id="run-dataforge-provenance",
        correlation_id="correlation-dataforge-provenance",
        principal_id="dataforge-test",
    )
    result = IngestService(
        storage, extractor=PyPdfExtractor(), registry=registry
    ).ingest(provenance_pdf, context=context, registry=registry)

    # This read is the entire consumer boundary; the source PDF is not reopened.
    handoff = json.load(storage.open(result.provenance_key))
    serialized = json.dumps(handoff)
    assert handoff["document_id"] == result.document.document_id
    assert handoff["source_asset"]["asset_id"] == result.document.source.source_id
    assert handoff["source_asset"]["blob_sha256"] == ground_truth["document"]["pdf_sha256"]
    assert len(handoff["pages"]) == 19
    assert all(item["anchor_id"] for item in handoff["evidence"])
    assert all(item["source_asset_id"] for item in handoff["evidence"])
    assert not any(
        forbidden in serialized
        for forbidden in ground_truth["dataforge_handoff"]["forbidden_fields"]
    )


@pytest.mark.xfail(
    strict=True,
    reason="GAP-DATAFORGE-RICH: docs/provenance-gap-report.md#gap-dataforge-rich",
)
def test_dataforge_handoff_contains_exact_section_and_relation_anchors() -> None:
    raise AssertionError("Canonical rich provenance is not implemented yet")
