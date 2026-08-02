from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from cognityx_ingest import (
    IngestService,
    PyMuPDFParser,
    PyPdfExtractor,
    SourceAssetRegistry,
)
from cognityx_resource import ExecutionContext
from cognityx_storage import LocalStorageBackend, StorageClient, StorageConfig, StorageRuntime


def _ingest_fixture(root: Path, source: Path):
    runtime = StorageRuntime.from_config(StorageConfig.built_in(root=root / "runtime"))
    registry = SourceAssetRegistry.load(runtime=runtime)
    storage = StorageClient(LocalStorageBackend(root / "artifacts")).for_shared_data()
    context = ExecutionContext(
        run_id="run-provenance-v1",
        correlation_id="correlation-provenance-v1",
        principal_id="fixture-test",
    )
    result = IngestService(
        storage, extractor=PyPdfExtractor(), registry=registry
    ).ingest(source, context=context, registry=registry)
    return result, storage


def test_canonical_source_pages_and_immutable_artifacts(
    tmp_path: Path, provenance_pdf: Path, ground_truth: dict[str, object]
) -> None:
    result, storage = _ingest_fixture(tmp_path, provenance_pdf)
    document = result.document
    provenance = json.load(storage.open(result.provenance_key))

    assert document.source.sha256 == ground_truth["document"]["pdf_sha256"]
    assert len(document.pages) == ground_truth["document"]["page_count"]
    assert [page.physical_page_index for page in document.pages] == list(range(19))
    assert [page.sequence_number for page in document.pages] == list(range(1, 20))
    assert provenance["source_asset"]["blob_sha256"] == document.source.sha256
    assert provenance["document_id"] == document.document_id
    assert result.provenance_key.endswith("/provenance.json")
    assert all(artifact.uri.startswith("storage://") for artifact in result.artifacts)
    first = storage.open(result.provenance_key).read()
    second = storage.open(result.provenance_key).read()
    assert hashlib.sha256(first).digest() == hashlib.sha256(second).digest()


def test_canonical_numbered_sections_match_ground_truth(
    tmp_path: Path, provenance_pdf: Path, ground_truth: dict[str, object]
) -> None:
    pytest.importorskip(
        "fitz", reason="Section acceptance requires cognityx-ingest[pymupdf]"
    )
    runtime = StorageRuntime.from_config(
        StorageConfig.built_in(root=tmp_path / "structured-runtime")
    )
    registry = SourceAssetRegistry.load(runtime=runtime)
    storage = StorageClient(
        LocalStorageBackend(tmp_path / "structured-artifacts")
    ).for_shared_data()
    result = IngestService(
        storage, extractor=PyMuPDFParser(), registry=registry
    ).ingest(
        provenance_pdf,
        context=ExecutionContext(
            run_id="run-canonical-sections",
            correlation_id="correlation-canonical-sections",
            principal_id="fixture-test",
        ),
        registry=registry,
    )
    document = result.document

    assert {(section.number, section.title) for section in document.sections} == {
        (section["number"], section["title"])
        for section in ground_truth["sections"]
    }


def test_rich_profile_preserves_complementary_parser_facts(
    provenance_pdf: Path, tmp_path: Path
) -> None:
    pytest.importorskip("fitz", reason="Fusion acceptance requires PyMuPDF")
    pytest.importorskip("docling", reason="Fusion acceptance requires Docling")
    from cognityx_ingest import ExtractionPolicy, ParserRouter

    result = ParserRouter(
        policy=ExtractionPolicy("compare", ("pymupdf", "docling"))
    ).extract_document(provenance_pdf)
    assert result.diagnostics["fusion"] == "canonical_multi_source"
    assert result.backend == "fusion"
    assert result.considered_backends == ("docling", "pymupdf")
    assert result.raw_artifacts.keys() == {"docling", "pymupdf"}
    assert any(page.page_label for page in result.pages)
    assert any(page.printed_page_label for page in result.pages)
    assert any(page.relations for page in result.pages)
    assert {item.object_type for page in result.pages for item in page.objects} >= {
        "table",
        "figure",
    }
    assert any(
        len(block.source_backends) > 1
        for page in result.pages
        for block in page.blocks
    )

    class FusedFixtureParser:
        name = "fusion"

        def extract_document(self, path: Path):
            return result

    runtime = StorageRuntime.from_config(
        StorageConfig.built_in(root=tmp_path / "fusion-runtime")
    )
    registry = SourceAssetRegistry.load(runtime=runtime)
    storage = StorageClient(
        LocalStorageBackend(tmp_path / "fusion-artifacts")
    ).for_shared_data()
    ingested = IngestService(
        storage, extractor=FusedFixtureParser(), registry=registry
    ).ingest(
        provenance_pdf,
        context=ExecutionContext(
            run_id="run-fused-provenance",
            correlation_id="correlation-fused-provenance",
            principal_id="fusion-test",
        ),
        registry=registry,
    )
    handoff = json.load(storage.open(ingested.provenance_key))

    assert handoff["parser"]["selected"] == "fusion"
    assert handoff["parser"]["diagnostics"]["fusion"] == "canonical_multi_source"
    assert handoff["parser"]["diagnostics"]["conflicts"]
    fused_block = next(
        block for block in handoff["blocks"] if len(block["source_backends"]) > 1
    )
    assert all(
        {"backend", "method", "confidence"} <= observation.keys()
        for observations in fused_block["fact_sources"].values()
        for observation in observations
    )
    selection = next(
        item for item in handoff["decisions"] if item["method"] == "parser_policy"
    )
    assert selection["considered_tools"] == ["docling", "pymupdf"]
    assert selection["invoked_tools"] == ["docling", "pymupdf"]
    assert selection["selected_tool"] == "fusion"
    assert selection["selected_reason"] == "canonical_fact_level_fusion"
    assert selection["confidence"] == 1.0
    assert {item["backend"] for item in handoff["parser"]["raw_artifacts"]} == {
        "docling",
        "fusion",
        "pymupdf",
    }
    assert all(
        item["uri"].startswith("storage://")
        for item in handoff["parser"]["raw_artifacts"]
    )
    assert all(storage.exists(key) for key in ingested.raw_parser_keys)
