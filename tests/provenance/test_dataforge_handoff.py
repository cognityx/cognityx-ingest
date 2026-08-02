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


def _dataforge_candidate_spans(handoff: dict[str, object]) -> list[dict[str, object]]:
    """Build downstream candidates using only the immutable handoff."""
    lineage = handoff["lineage"]
    evidence_by_page = {
        item["anchor_id"]: item["evidence_id"] for item in handoff["evidence"]
    }
    return [
        {
            **lineage,
            "section_id": section["section_id"],
            "page_ids": section["page_ids"],
            "block_ids": section["block_ids"],
            "evidence_ids": [
                evidence_by_page[page_id] for page_id in section["page_ids"]
            ],
        }
        for section in handoff["sections"]
    ]


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            key for item in value.values() for key in _all_keys(item)
        }
    if isinstance(value, list):
        return {key for item in value for key in _all_keys(item)}
    return set()


def _canonical_references(handoff: dict[str, object]) -> set[str]:
    references = {
        *(block_id for page in handoff["pages"] for block_id in page["block_ids"]),
        *(block["page_id"] for block in handoff["blocks"]),
    }
    for region in handoff["repeated_regions"]:
        for occurrence in region["occurrences"]:
            references.update(
                {
                    occurrence["page_id"],
                    occurrence["source_page_id"],
                    occurrence["source_block_id"],
                }
            )
    for section in handoff["sections"]:
        references.update(section["evidence_ids"])
        references.update(section["page_ids"])
        references.update(section["block_ids"])
        references.update(
            item
            for item in (
                section["parent_section_id"],
                section["heading_block_id"],
                section["start_block_id"],
                section["end_block_id"],
            )
            if item is not None
        )
    for item in handoff["objects"]:
        references.add(item["page_id"])
        references.update(item["page_ids"])
        references.update(item["source_anchor_ids"])
        references.update(
            anchor
            for anchor in (
                item["owner_section_id"],
                item["caption_anchor_id"],
                item["marker_anchor_id"],
                item["note_anchor_id"],
                item["image_anchor_id"],
            )
            if anchor is not None
        )
        for row in item["rows"]:
            references.update(row["source_anchor_ids"])
            for cell in row["cells"]:
                references.update(cell["source_anchor_ids"])
        for part in item["parts"]:
            references.add(part["page_id"])
            references.update(part["source_block_ids"])
            references.update(part["merged_group_row"]["source_anchor_ids"])
    for item in handoff["evidence"]:
        references.update(
            anchor
            for anchor in (
                item["anchor_id"],
                item["block_id"],
                item["continues_from"],
                item["continues_to"],
            )
            if anchor is not None
        )
    return references


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
    assert handoff["schema_version"] == "cognityx.ingest.provenance/v2"
    assert handoff["run_id"] == context.run_id
    assert handoff["lineage"]["run_id"] == context.run_id
    assert handoff["lineage"]["job_id"] == result.job_id
    assert handoff["lineage"]["context_id"] == handoff["source_asset"]["context_id"]
    assert handoff["lineage"]["bundle_id"] == handoff["source_asset"]["bundle_id"]
    assert handoff["lineage"]["asset_id"] == result.document.source.source_id
    assert handoff["lineage"]["source_sha256"] == ground_truth["document"]["pdf_sha256"]
    assert handoff["lineage"]["document_id"] == result.document.document_id
    assert handoff["source_asset"]["logical_uri"] == (
        f"sourceasset://{result.document.source.source_id}"
    )
    assert handoff["artifact_storage_role"] == "artifacts"
    assert all(
        uri.startswith("storage://")
        for name, uri in handoff["artifact_uris"].items()
        if name != "parser"
    )
    assert len(handoff["pages"]) == 19
    assert all(item["anchor_id"] for item in handoff["evidence"])
    assert all(item["source_asset_id"] for item in handoff["evidence"])
    assert not any(
        forbidden in serialized
        for forbidden in ground_truth["dataforge_handoff"]["forbidden_fields"]
    )
    assert {
        "embedding",
        "embeddings",
        "vector",
        "vectors",
        "generated_questions",
        "generated_answers",
        "training_data",
    }.isdisjoint(_all_keys(handoff))


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
    runtime = StorageRuntime.from_config(
        StorageConfig.built_in(root=tmp_path / "runtime")
    )
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


def test_dataforge_builds_valid_candidate_spans_from_provenance_only(
    tmp_path: Path, provenance_pdf: Path
) -> None:
    pytest.importorskip(
        "fitz", reason="Rich DataForge handoff requires cognityx-ingest[pymupdf]"
    )
    runtime = StorageRuntime.from_config(
        StorageConfig.built_in(root=tmp_path / "runtime")
    )
    registry = SourceAssetRegistry.load(runtime=runtime)
    storage = StorageClient(LocalStorageBackend(tmp_path / "artifacts")).for_shared_data()
    context = ExecutionContext(
        run_id="run-dataforge-final-contract",
        correlation_id="correlation-dataforge-final-contract",
        principal_id="dataforge-test",
    )
    result = IngestService(
        storage, extractor=PyMuPDFParser(), registry=registry
    ).ingest(provenance_pdf, context=context, registry=registry)

    # The consumer receives this JSON value, not the source path or parser.
    handoff = json.load(storage.open(result.provenance_key))
    anchors = {
        handoff["document_id"],
        *(page["page_id"] for page in handoff["pages"]),
        *(block["block_id"] for block in handoff["blocks"]),
        *(section["section_id"] for section in handoff["sections"]),
        *(item["object_id"] for item in handoff["objects"]),
        *(item["evidence_id"] for item in handoff["evidence"]),
    }
    external_targets = ("https://", "http://")

    assert all(page["block_ids"] for page in handoff["pages"])
    assert _canonical_references(handoff) <= anchors
    assert all(
        relation["source_anchor_id"] in anchors
        and (
            relation["target_anchor_id"] is None
            or relation["target_anchor_id"] in anchors
            or relation["target_anchor_id"].startswith(external_targets)
        )
        for relation in handoff["relations"]
    )
    assert all(item["source_anchor_id"] in anchors for item in handoff["unresolved"])
    assert all(not item["gold"] for item in handoff["ambiguous"])
    assert all(not item["gold"] for item in handoff["unresolved"])
    assert all(
        relation["gold"]
        == (
            relation["status"] in {"observed", "resolved"}
            and relation["target_anchor_id"] is not None
        )
        for relation in handoff["relations"]
    )

    candidates = _dataforge_candidate_spans(handoff)
    assert candidates
    assert all(
        candidate["document_id"] == handoff["document_id"]
        for candidate in candidates
    )
    assert all(
        candidate["asset_id"] == handoff["source_asset"]["asset_id"]
        for candidate in candidates
    )
    assert all(candidate["source_sha256"] for candidate in candidates)
    assert all(candidate["section_id"] in anchors for candidate in candidates)
    assert all(candidate["page_ids"] for candidate in candidates)
    assert all(candidate["block_ids"] for candidate in candidates)
    assert all(candidate["evidence_ids"] for candidate in candidates)
