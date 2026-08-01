from __future__ import annotations

import json
from pathlib import Path

import pytest

from cognityx_ingest import IngestService, PyMuPDFParser, SourceAssetRegistry
from cognityx_resource import ExecutionContext
from cognityx_storage import (
    LocalStorageBackend,
    StorageClient,
    StorageConfig,
    StorageRuntime,
)


@pytest.fixture()
def structured_ingest(tmp_path: Path, provenance_pdf: Path):
    pytest.importorskip(
        "fitz", reason="Section acceptance requires cognityx-ingest[pymupdf]"
    )
    runtime = StorageRuntime.from_config(
        StorageConfig.built_in(root=tmp_path / "runtime")
    )
    registry = SourceAssetRegistry.load(runtime=runtime)
    storage = StorageClient(LocalStorageBackend(tmp_path / "artifacts")).for_shared_data()
    result = IngestService(
        storage, extractor=PyMuPDFParser(), registry=registry
    ).ingest(
        provenance_pdf,
        context=ExecutionContext(
            run_id="run-section-structure",
            correlation_id="correlation-section-structure",
            principal_id="section-test",
        ),
        registry=registry,
    )
    return result, storage


def _ground_sections(ground_truth: dict[str, object]) -> dict[str, dict[str, object]]:
    return {item["number"]: item for item in ground_truth["sections"]}


def _anchor(page_id: str, fixture_anchor: str) -> str:
    ordinal = int(fixture_anchor.rsplit("-", 1)[1])
    return f"{page_id}:block:{ordinal}"


def test_p06_typed_blocks_preserve_reading_order_and_observations(
    structured_ingest, ground_truth: dict[str, object]
) -> None:
    result, _storage = structured_ingest
    page = next(item for item in result.document.pages if item.physical_page_index == 2)
    blocks_by_id = {item.block_id: item for item in result.document.blocks}
    blocks = [
        blocks_by_id[block_id]
        for block_id in page.block_ids
        if blocks_by_id[block_id].block_type not in {"page_header", "page_footer"}
    ]
    expected = next(
        item for item in ground_truth["pages"] if item["physical_index"] == 2
    )["blocks"]

    assert [item.block_id for item in blocks] == [
        f"{page.page_id}:block:{index}" for index in range(1, len(expected) + 1)
    ]
    assert [item.reading_order for item in blocks] == list(range(1, len(expected) + 1))
    assert [item.block_type for item in blocks] == [item[1] for item in expected]
    assert all(item.page_id == page.page_id for item in blocks)
    assert all(item.bbox is not None for item in blocks)
    assert all(item.method and item.confidence is not None for item in blocks)
    assert all(
        " ".join(block.text.split()).startswith(expected_block[2])
        for block, expected_block in zip(blocks, expected, strict=True)
    )


def test_p07_numbered_and_appendix_sections_preserve_hierarchy(
    structured_ingest, ground_truth: dict[str, object]
) -> None:
    result, _storage = structured_ingest
    actual = {item.number: item for item in result.document.sections}
    expected = _ground_sections(ground_truth)
    number_by_id = {
        item["id"]: item["number"] for item in ground_truth["sections"]
    }

    for number in ("1", "1.1", "1.2", "7", "7.2", "B", "B.1", "B.2", "B.3"):
        section = actual[number]
        oracle = expected[number]
        assert section.title == oracle["title"]
        assert section.level == len(oracle["path"])
        assert section.path == tuple(oracle["path"])
        expected_parent = oracle["parent"]
        assert section.parent_section_id == (
            None
            if expected_parent is None
            else actual[number_by_id[expected_parent]].section_id
        )
        assert section.heading_block_id == section.start_block_id
        assert section.method == "deterministic_numbered_heading"
        assert section.confidence == 1.0


def test_p08_same_page_sections_have_exact_distinct_block_spans(
    structured_ingest, ground_truth: dict[str, object]
) -> None:
    result, _storage = structured_ingest
    page = next(item for item in result.document.pages if item.physical_page_index == 2)
    actual = {item.number: item for item in result.document.sections}
    expected = _ground_sections(ground_truth)

    for number in ("1", "1.1", "1.2", "2", "2.1", "2.2"):
        section = actual[number]
        oracle = expected[number]
        assert section.start_block_id == _anchor(page.page_id, oracle["start"])
        assert section.end_block_id == _anchor(page.page_id, oracle["end"])
        start = int(oracle["start"].rsplit("-", 1)[1])
        end = int(oracle["end"].rsplit("-", 1)[1])
        assert section.block_ids == tuple(
            f"{page.page_id}:block:{index}" for index in range(start, end + 1)
        )

    assert actual["1"].end_block_id != actual["2"].heading_block_id
    assert actual["2"].start_block_id == actual["2"].heading_block_id


@pytest.mark.xfail(
    strict=True,
    reason="GAP-CONTINUATION: docs/provenance-gap-report.md#gap-rich-structure",
)
def test_p09_section_4_3_continues_across_pages(
    structured_ingest, ground_truth: dict[str, object]
) -> None:
    result, _storage = structured_ingest
    section = next(item for item in result.document.sections if item.number == "4.3")
    pages_by_id = {item.page_id: item for item in result.document.pages}
    expected = _ground_sections(ground_truth)["4.3"]

    assert [pages_by_id[page_id].physical_page_index for page_id in section.page_ids] == (
        expected["page_indexes"]
    )
    assert section.end_block_id.endswith(":page-index:5:block:5")
    assert section.continues_to is not None


@pytest.mark.xfail(
    strict=True,
    reason="GAP-CONTINUATION: docs/provenance-gap-report.md#gap-rich-structure",
)
def test_p10_section_4_4_records_explicit_non_continuation(
    structured_ingest,
) -> None:
    result, _storage = structured_ingest
    section = next(item for item in result.document.sections if item.number == "4.4")

    assert section.continuation_status == "deterministic_false"


def test_dataforge_reads_exact_section_structure_from_provenance_only(
    structured_ingest,
) -> None:
    result, storage = structured_ingest

    handoff = json.load(storage.open(result.provenance_key))
    sections = {item["number"]: item for item in handoff["sections"]}
    assert sections["1"]["end_block_id"] != sections["2"]["heading_block_id"]
    assert sections["2"]["start_block_id"] == sections["2"]["heading_block_id"]
    assert sections["1.1"]["parent_section_id"] == sections["1"]["section_id"]
    assert sections["1.2"]["parent_section_id"] == sections["1"]["section_id"]
    assert sections["B.1"]["parent_section_id"] == sections["B"]["section_id"]
    assert sections["B.2"]["path"] == ["B", "B.2"]
    assert sections["B.3"]["level"] == 2

    repeated_blocks = {
        occurrence["source_block_id"]
        for region in handoff["repeated_regions"]
        for occurrence in region["occurrences"]
    }
    assert all(
        repeated_blocks.isdisjoint(section["block_ids"])
        for section in handoff["sections"]
    )
