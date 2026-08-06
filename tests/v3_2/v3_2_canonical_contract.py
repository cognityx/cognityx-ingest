from __future__ import annotations

import json
from pathlib import Path

from cognityx_ingest import IngestService, PyPdfExtractor, SourceAssetRegistry
from cognityx_resource import ExecutionContext
from cognityx_storage import LocalStorageBackend, StorageClient, StorageConfig, StorageRuntime


def _ingest(tmp_path: Path, source: Path):
    runtime = StorageRuntime.from_config(StorageConfig.built_in(root=tmp_path / "runtime"))
    registry = SourceAssetRegistry.load(runtime=runtime)
    storage = StorageClient(LocalStorageBackend(tmp_path / "artifacts")).for_shared_data()
    context = ExecutionContext(run_id="run-canonical", correlation_id="cor-canonical", principal_id="fixture-test")
    result = IngestService(storage, extractor=PyPdfExtractor(), registry=registry).ingest(source, context=context, registry=registry)
    return result, storage


def test_canonical_document_still_preserves_current_python_ingest_shape(tmp_path: Path, provenance_pdf: Path):
    result, storage = _ingest(tmp_path, provenance_pdf)
    payload = json.loads(storage.open(result.provenance_key).read())
    assert result.document.document_id.startswith("pdf-")
    assert payload["document_id"] == result.document.document_id
    assert payload["parser"]["selected"] == "basic"
    assert payload["document"]["pages"]
    assert all(page["physical_page_index"] == i for i, page in enumerate(payload["document"]["pages"]))


def test_expected_canonical_content_fixture_matches_contract(v3_2_fixture_root: Path) -> None:
    content = json.loads((v3_2_fixture_root / "expected" / "canonical_content.json").read_text(encoding="utf-8"))
    assert content["schema"].startswith("cognityx.ingest.v3.2")
    assert content["canonical_text_is_stored_once"] is True
    assert content["references_use_ids_and_spans"] is True
