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
def table_ingest(tmp_path: Path, provenance_pdf: Path):
    pytest.importorskip(
        "fitz", reason="Table acceptance requires cognityx-ingest[pymupdf]"
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
            run_id="run-table-structure",
            correlation_id="correlation-table-structure",
            principal_id="table-test",
        ),
        registry=registry,
    )
    return result, storage


def _expected_table(ground_truth: dict[str, object]) -> dict[str, object]:
    return next(item for item in ground_truth["objects"] if item["id"] == "table-9-1")


def test_p11_p12_build_one_logical_table_with_ordered_parts(
    table_ingest, ground_truth: dict[str, object]
) -> None:
    result, _storage = table_ingest
    expected = _expected_table(ground_truth)
    tables = [item for item in result.document.objects if item.object_type == "table"]

    assert len(tables) == expected["logical_table_count"]
    table = tables[0]
    sections = {item.number: item for item in result.document.sections}
    pages = {item.page_id: item for item in result.document.pages}

    assert table.object_id.endswith(":table:9-1")
    assert table.owner_section_id == sections["9.2"].section_id
    assert table.caption == expected["caption"]
    assert table.caption_anchor_id.endswith(":page-index:10:block:6")
    assert table.columns == tuple(expected["header"])
    assert [row.row_number for row in table.rows] == list(range(1, 53))
    assert [row.cells[-1].text for row in table.rows] == [
        f"CC-{number}" for number in range(101, 153)
    ]
    assert len(table.rows) == expected["data_rows"]
    assert all(len(row.cells) == expected["columns"] for row in table.rows)
    assert [pages[part.page_id].physical_page_index for part in table.parts] == [
        10,
        11,
        12,
    ]
    assert [(part.row_start, part.row_end) for part in table.parts] == [
        (1, 21),
        (22, 47),
        (48, 52),
    ]
    assert [part.repeated_header for part in table.parts] == [False, True, True]
    assert all(part.merged_group_row.column_span == 5 for part in table.parts)
    assert all(
        part.merged_group_row.text
        == "PART A — ORDINARY AND CONDITIONAL REGISTRATIONS"
        for part in table.parts
    )
    assert table.method == "deterministic_table_assembly"
    assert table.confidence == 1.0


def test_p11_p12_update_section_spans_to_canonical_table_parts(table_ingest) -> None:
    result, _storage = table_ingest
    sections = {item.number: item for item in result.document.sections}
    pages = {item.page_id: item for item in result.document.pages}

    section_9 = sections["9"]
    section_9_2 = sections["9.2"]
    assert [pages[item].physical_page_index for item in section_9.page_ids] == [
        10,
        11,
        12,
    ]
    assert section_9.start_block_id.endswith(":page-index:10:block:1")
    assert section_9.end_block_id.endswith(":page-index:12:block:8")
    assert [pages[item].physical_page_index for item in section_9_2.page_ids] == [
        10,
        11,
        12,
    ]
    assert section_9_2.start_block_id.endswith(":page-index:10:block:4")
    assert section_9_2.end_block_id.endswith(":page-index:12:block:2")


def test_dataforge_reads_complete_logical_table_from_provenance_only(
    table_ingest,
) -> None:
    result, storage = table_ingest

    handoff = json.load(storage.open(result.provenance_key))
    tables = [item for item in handoff["objects"] if item["object_type"] == "table"]
    assert len(tables) == 1
    table = tables[0]

    assert table["owner_section_id"] in {
        section["section_id"] for section in handoff["sections"]
    }
    assert table["caption_anchor_id"] in {
        block["block_id"] for block in handoff["blocks"]
    }
    assert [row["row_number"] for row in table["rows"]] == list(range(1, 53))
    assert len(table["parts"]) == 3
    assert all(part["source_block_ids"] for part in table["parts"])
    assert all(part["parser_source_anchor_ids"] for part in table["parts"])
    assert all(row["row_type"] == "data" for row in table["rows"])
