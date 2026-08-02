from __future__ import annotations

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


def test_dataforge_can_load_provenance_without_reopening_pdf(
    tmp_path: Path, provenance_pdf: Path, ground_truth: dict[str, object]
) -> None:
    runtime = StorageRuntime.from_config(
        StorageConfig.built_in(root=tmp_path / "runtime")
    )
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


def test_dataforge_handoff_contains_page_labels_and_repeated_regions(
    tmp_path: Path, provenance_pdf: Path, ground_truth: dict[str, object]
) -> None:
    pytest.importorskip(
        "fitz", reason="Rich handoff requires cognityx-ingest[pymupdf]"
    )
    runtime = StorageRuntime.from_config(
        StorageConfig.built_in(root=tmp_path / "runtime")
    )
    registry = SourceAssetRegistry.load(runtime=runtime)
    storage = StorageClient(LocalStorageBackend(tmp_path / "artifacts")).for_shared_data()
    context = ExecutionContext(
        run_id="run-dataforge-repeated-regions",
        correlation_id="correlation-dataforge-repeated-regions",
        principal_id="dataforge-test",
    )
    result = IngestService(
        storage, extractor=PyMuPDFParser(), registry=registry
    ).ingest(provenance_pdf, context=context, registry=registry)

    # DataForge's consumer boundary starts here and reads only the stored handoff.
    handoff = json.load(storage.open(result.provenance_key))
    assert [page["printed_page_label"] for page in handoff["pages"]] == [
        page["printed_label"] for page in ground_truth["pages"]
    ]
    assert [page["pdf_page_label"] for page in handoff["pages"]] == [
        page["native_pdf_label"] for page in ground_truth["pages"]
    ]
    assert all(page["block_ids"] for page in handoff["pages"])

    regions = {item["region_type"]: item for item in handoff["repeated_regions"]}
    assert set(regions) == {"header", "footer"}
    for region in regions.values():
        assert region["status"] == "deterministic"
        assert region["detection_method"] == "deterministic_repeated_margin"
        assert region["confidence"] == 1.0
        assert len(region["occurrences"]) == len(handoff["pages"])
        assert all(item["source_page_id"] for item in region["occurrences"])
        assert all(item["source_block_id"] for item in region["occurrences"])

    section_blocks = {
        block_id
        for section in handoff["sections"]
        for block_id in section["block_ids"]
    }
    repeated_blocks = {
        occurrence["source_block_id"]
        for region in regions.values()
        for occurrence in region["occurrences"]
    }
    assert section_blocks.isdisjoint(repeated_blocks)


def test_dataforge_handoff_contains_exact_relation_anchors(
    tmp_path: Path, provenance_pdf: Path, ground_truth: dict[str, object]
) -> None:
    pytest.importorskip(
        "fitz", reason="Rich relation handoff requires cognityx-ingest[pymupdf]"
    )
    runtime = StorageRuntime.from_config(StorageConfig.built_in(root=tmp_path / "runtime"))
    registry = SourceAssetRegistry.load(runtime=runtime)
    storage = StorageClient(LocalStorageBackend(tmp_path / "artifacts")).for_shared_data()
    context = ExecutionContext(
        run_id="run-dataforge-rich-gap",
        correlation_id="correlation-dataforge-rich-gap",
        principal_id="dataforge-test",
    )
    result = IngestService(
        storage, extractor=PyMuPDFParser(), registry=registry
    ).ingest(provenance_pdf, context=context, registry=registry)
    handoff = json.load(storage.open(result.provenance_key))

    required_literals = {
        relation["literal"]
        for relation in ground_truth["relations"]
        if relation["id"]
        in {
            "rel-exact-7.2",
            "rel-exact-8.2",
            "rel-plural-7.2-11.3",
            "rel-appendix-b",
            "rel-page-a-1",
            "rel-plain-url",
            "rel-native-external",
            "rel-native-appendix-b",
        }
    }
    relations = handoff["relations"]
    unresolved = handoff["unresolved"]
    anchor_ids = {
        *(page["page_id"] for page in handoff["pages"]),
        *(block["block_id"] for block in handoff["blocks"]),
        *(section["section_id"] for section in handoff["sections"]),
        *(item["object_id"] for item in handoff["objects"]),
    }

    assert {item["target_text"] for item in relations} >= required_literals
    assert all(item["source_anchor_id"] in anchor_ids for item in relations)
    assert all(
        item["target_anchor_id"] in anchor_ids
        or str(item["target_anchor_id"]).startswith("https://")
        for item in relations
        if item["target_anchor_id"] is not None
    )
    missing = next(
        item
        for item in unresolved
        if item["target_text"] == "Moonlit Conduct Handbook, Version 9.4"
    )
    assert missing["status"] == "unresolved"
    assert missing["reason"] == "document_not_in_corpus"
    assert not missing.get("gold", False)
