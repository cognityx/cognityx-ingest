from __future__ import annotations

import json
from pathlib import Path

import pytest

from cognityx_ingest import IngestService, PyPdfExtractor, SourceAssetRegistry
from cognityx_ingest.parser import ExtractionPolicy, ParserRouter
from cognityx_resource import ExecutionContext
from cognityx_storage import LocalStorageBackend, StorageClient, StorageConfig, StorageRuntime


def _ingest_fixture(root: Path, source: Path):
    runtime = StorageRuntime.from_config(StorageConfig.built_in(root=root / "runtime"))
    registry = SourceAssetRegistry.load(runtime=runtime)
    storage = StorageClient(LocalStorageBackend(root / "artifacts")).for_shared_data()
    context = ExecutionContext(
        run_id="run-v3-2-fixture",
        correlation_id="correlation-v3-2-fixture",
        principal_id="fixture-test",
    )
    result = IngestService(storage, extractor=PyPdfExtractor(), registry=registry).ingest(
        source, context=context, registry=registry
    )
    return result, storage


def test_existing_python_ingest_path_and_cli_shape_remain_supported(
    tmp_path: Path, provenance_pdf: Path, v3_2_fixture_root: Path
) -> None:
    result, storage = _ingest_fixture(tmp_path, provenance_pdf)
    provenance = json.load(storage.open(result.provenance_key))
    assert result.document.document_id.startswith("pdf-")
    assert result.document.source.sha256 == provenance["source_asset"]["blob_sha256"]
    manifest = json.loads((v3_2_fixture_root / "fixture_manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "cognityx.ingest.fixture-manifest/v3.2"


@pytest.mark.xfail(strict=True, reason="T01: parser-native artifact preservation API is not implemented")
def test_native_parser_artifact_is_preserved_without_flattening(provenance_pdf: Path):
    result = ParserRouter(policy=ExtractionPolicy("compare", ("pymupdf", "docling"))).extract_document(
        provenance_pdf
    )
    assert result.backend == "fusion"
    assert result.diagnostics["fusion"] == "canonical_multi_source"
    assert result.raw_artifacts.keys() == {"docling", "pymupdf"}


@pytest.mark.xfail(strict=True, reason="T03: parser capability registry API is not implemented")
def test_capability_registry_exposes_exactly_three_source_classes(provenance_pdf: Path):
    record = ParserRouter(policy=ExtractionPolicy("fixed", ("docling",))).extract_document(
        provenance_pdf
    )
    assert record.diagnostics["capability_sources"] == [
        "parser-discovered",
        "human-guided",
        "auto-learned",
    ]


@pytest.mark.xfail(strict=True, reason="T06: non-copying segmentation view API is not implemented")
def test_segmentation_views_reference_ids_and_spans_not_copied_text(
    tmp_path: Path, provenance_pdf: Path
):
    result, storage = _ingest_fixture(tmp_path, provenance_pdf)
    runtime = StorageRuntime.from_config(StorageConfig.built_in(root=tmp_path / "view-runtime"))
    registry = SourceAssetRegistry.load(runtime=runtime)
    view = IngestService(
        storage, extractor=PyPdfExtractor(), registry=registry
    ).create_segmentation_view(result.document.document_id, strategy="paragraph")
    assert all(segment.text is None for segment in view.segments)
    assert all(segment.node_spans for segment in view.segments)


@pytest.mark.xfail(strict=True, reason="T08: source graph and provenance address API is not implemented")
def test_source_graph_and_provenance_addresses_are_exact_and_resolvable(
    tmp_path: Path, provenance_pdf: Path
):
    result, storage = _ingest_fixture(tmp_path, provenance_pdf)
    runtime = StorageRuntime.from_config(StorageConfig.built_in(root=tmp_path / "graph-runtime"))
    registry = SourceAssetRegistry.load(runtime=runtime)
    service = IngestService(storage, extractor=PyPdfExtractor(), registry=registry)
    graph = service.build_source_graph(result.document.document_id)
    address = service.resolve_provenance_address(graph["addresses"][0])
    assert address["status"] == "exact"
    assert address["source_graph_revision"]
