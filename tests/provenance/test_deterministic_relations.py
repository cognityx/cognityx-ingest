from __future__ import annotations

from pathlib import Path

from cognityx_ingest import IngestService, PyMuPDFParser, SourceAssetRegistry
from cognityx_resource import ExecutionContext
from cognityx_storage import (
    LocalStorageBackend,
    StorageClient,
    StorageConfig,
    StorageRuntime,
)


DETERMINISTIC_RELATION_IDS = (
    "rel-exact-7.2",
    "rel-exact-8.2",
    "rel-plural-7.2-11.3",
    "rel-appendix-b",
    "rel-page-a-1",
    "rel-plain-url",
    "rel-native-external",
    "rel-native-appendix-b",
)


def test_deterministic_references_preserve_exact_canonical_contract(
    ground_truth: dict[str, object],
    provenance_pdf: Path,
    tmp_path: Path,
) -> None:
    result = _ingest(provenance_pdf, tmp_path)
    expected = {
        item["id"]: item
        for item in ground_truth["relations"]
        if item["id"] in DETERMINISTIC_RELATION_IDS
    }
    sections = {section.number: section.section_id for section in result.document.sections}
    pages = {
        page.printed_page_label: page.page_id for page in result.document.pages
    }

    for relation_id, oracle in expected.items():
        source_anchor = _canonical_block_id(result, oracle["source"])
        matches = [
            relation
            for relation in result.document.relations
            if relation.target_text == oracle["literal"]
            and relation.relation_type == oracle["type"]
            and relation.method == oracle["method"]
            and relation.source_anchor_id == source_anchor
        ]
        assert matches, relation_id
        assert {item.source_anchor_id for item in matches} == {source_anchor}
        assert {item.status for item in matches} == {oracle["status"]}
        assert {item.confidence for item in matches} == {1.0}
        assert {item.target_anchor_id for item in matches} == {
            _canonical_target(target, sections, pages) for target in oracle["targets"]
        }


def test_absent_document_reference_is_explicitly_unresolved(
    ground_truth: dict[str, object],
    provenance_pdf: Path,
    tmp_path: Path,
) -> None:
    result = _ingest(provenance_pdf, tmp_path)
    oracle = next(
        item
        for item in ground_truth["relations"]
        if item["id"] == "rel-missing-handbook"
    )
    item = next(
        item
        for item in result.document.unresolved
        if item.target_text == oracle["literal"]
    )

    assert item.source_anchor_id == _canonical_block_id(result, oracle["source"])
    assert item.relation_type == oracle["type"]
    assert item.status == oracle["status"]
    assert item.method == oracle["method"]
    assert item.confidence == 1.0
    assert item.reason == oracle["reason"]


def test_ground_truth_separates_resolved_ambiguous_and_unresolved(
    ground_truth: dict[str, object]
) -> None:
    statuses = {item["status"] for item in ground_truth["relations"]}
    assert {"observed", "resolved", "rejected", "ambiguous", "unresolved"} <= statuses
    missing = next(
        item
        for item in ground_truth["relations"]
        if item["id"] == "rel-missing-handbook"
    )
    ambiguous = next(
        item
        for item in ground_truth["relations"]
        if item["id"] == "rel-ambiguous-travel"
    )
    assert missing["targets"] == []
    assert len(ambiguous["targets"]) == 2


def _ingest(provenance_pdf: Path, tmp_path: Path):
    runtime = StorageRuntime.from_config(
        StorageConfig.built_in(root=tmp_path / "runtime")
    )
    registry = SourceAssetRegistry.load(runtime=runtime)
    storage = StorageClient(LocalStorageBackend(tmp_path / "artifacts")).for_shared_data()
    context = ExecutionContext(
        run_id="run-deterministic-relations",
        correlation_id="correlation-deterministic-relations",
        principal_id="relation-test",
    )
    return IngestService(
        storage, extractor=PyMuPDFParser(), registry=registry
    ).ingest(provenance_pdf, context=context, registry=registry)


def _canonical_block_id(result, ground_truth_anchor: str) -> str:
    page_value, block_value = ground_truth_anchor.split(":")
    page_index = int(page_value.removeprefix("page-"))
    block_index = int(block_value.removeprefix("block-"))
    page = next(
        item for item in result.document.pages if item.physical_page_index == page_index
    )
    return f"{page.page_id}:block:{block_index}"


def _canonical_target(
    target: str, sections: dict[str | None, str], pages: dict[str | None, str]
) -> str:
    if target.startswith("sec-"):
        return sections[target.removeprefix("sec-")]
    if target.startswith("appendix-"):
        return sections[target.removeprefix("appendix-").upper()]
    if target == "page-016":
        return pages["A-1"]
    return target
