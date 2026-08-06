"""Focused parser-neutral canonical contract checks for v3.2 T00.

These tests freeze the canonical fixture shape and confirm that the current
Python composition root still produces the existing document output. The core
algorithm is direct fixture validation: compare schema names, canonical text
storage, and selector references without deriving new expected output. Future
T02 implementers and reviewers use this suite to ensure T00 does not weaken
existing ingest behavior.
"""

from __future__ import annotations

import json
from pathlib import Path

from cognityx_ingest import IngestService, PyPdfExtractor, SourceAssetRegistry
from cognityx_resource import ExecutionContext
from cognityx_storage import LocalStorageBackend, StorageClient, StorageConfig, StorageRuntime


def _ingest(tmp_path: Path, source: Path):
    """Ingest the reused provenance PDF through the current production API."""
    runtime = StorageRuntime.from_config(StorageConfig.built_in(root=tmp_path / "runtime"))
    registry = SourceAssetRegistry.load(runtime=runtime)
    storage = StorageClient(LocalStorageBackend(tmp_path / "artifacts")).for_shared_data()
    context = ExecutionContext(
        run_id="run-canonical",
        correlation_id="cor-canonical",
        principal_id="fixture-test",
    )
    result = IngestService(storage, extractor=PyPdfExtractor(), registry=registry).ingest(
        source, context=context, registry=registry
    )
    return result, storage


def test_canonical_document_still_preserves_current_python_ingest_shape(
    tmp_path: Path, provenance_pdf: Path
) -> None:
    """Verify the existing Python ingest path still returns the v2 document shape."""
    result, storage = _ingest(tmp_path, provenance_pdf)
    payload = json.loads(storage.open(result.provenance_key).read())
    assert result.document.document_id.startswith("pdf-")
    assert payload["document_id"] == result.document.document_id
    assert payload["parser"]["selected"] == "basic"
    assert payload["pages"]
    assert all(
        page["physical_page_index"] == i
        for i, page in enumerate(payload["pages"])
    )


def test_expected_canonical_content_fixture_matches_contract(v3_2_fixture_root: Path) -> None:
    """Assert the frozen v3.2 canonical fixture stores text once per node."""
    content = json.loads(
        (v3_2_fixture_root / "expected" / "canonical_content.json").read_text(
            encoding="utf-8"
        )
    )
    assert content["schema"] == "cognityx.ingest.canonical-content/v3.2"
    texts = [node["content"]["text"] for node in content["content_nodes"]]
    assert texts
    assert len(texts) == len(set(texts))
    assert all(node["source_selectors"] for node in content["content_nodes"])
