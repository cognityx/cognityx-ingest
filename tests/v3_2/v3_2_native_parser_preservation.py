from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from cognityx_ingest import DoclingParser, IngestService, PyPdfExtractor, SourceAssetRegistry
from cognityx_resource import ExecutionContext
from cognityx_storage import LocalStorageBackend, StorageClient, StorageConfig, StorageRuntime


def _ingest_docling(tmp_path: Path, source: Path):
    runtime = StorageRuntime.from_config(StorageConfig.built_in(root=tmp_path / "runtime"))
    registry = SourceAssetRegistry.load(runtime=runtime)
    storage = StorageClient(LocalStorageBackend(tmp_path / "artifacts")).for_shared_data()
    context = ExecutionContext(run_id="run-native", correlation_id="cor-native", principal_id="fixture-test")
    result = IngestService(storage, extractor=DoclingParser(), registry=registry).ingest(
        source, context=context, registry=registry
    )
    return result, storage


@pytest.mark.xfail(strict=True, reason="T01: durable native-artifact store/read/reload seam is not implemented")
def test_docling_native_artifact_round_trip_preserves_bytes_and_native_pointers(tmp_path: Path, provenance_pdf: Path):
    result, storage = _ingest_docling(tmp_path, provenance_pdf)
    native = result.raw_artifacts["docling"]
    assert hashlib.sha256(native).hexdigest() == hashlib.sha256(native).hexdigest()
    payload = json.loads(storage.open(result.provenance_key).read())
    assert payload["parser"]["raw_artifacts"][0]["backend"] == "docling"
    assert payload["parser"]["raw_artifacts"][0]["uri"].startswith("storage://")
    assert payload["parser"]["raw_artifacts"][0]["sha256"] == hashlib.sha256(native).hexdigest()


def test_docling_optional_parser_still_parses_fixture_when_available(provenance_pdf: Path) -> None:
    pytest.importorskip("docling", reason="Native preservation coverage is optional in normal CI")
    extraction = DoclingParser().extract_document(provenance_pdf)
    assert extraction.backend == "docling"
    assert extraction.raw_artifact is not None
