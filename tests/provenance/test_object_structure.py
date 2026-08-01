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
def object_ingest(tmp_path: Path, provenance_pdf: Path):
    pytest.importorskip(
        "fitz", reason="Object acceptance requires cognityx-ingest[pymupdf]"
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
            run_id="run-object-structure",
            correlation_id="correlation-object-structure",
            principal_id="object-test",
        ),
        registry=registry,
    )
    return result, storage


def test_p13_figure_caption_and_owner_are_canonical(
    object_ingest, ground_truth: dict[str, object]
) -> None:
    result, _storage = object_ingest
    expected = next(item for item in ground_truth["objects"] if item["id"] == "figure-10-1")
    figure = next(item for item in result.document.objects if item.object_type == "figure")
    sections = {item.number: item for item in result.document.sections}

    assert figure.object_id.endswith(":figure:10-1")
    assert figure.owner_section_id == sections["10.2"].section_id
    assert figure.caption == expected["caption"]
    assert figure.image_anchor_id.endswith(":page-index:13:block:6")
    assert figure.caption_anchor_id.endswith(":page-index:13:block:7")
    assert figure.bbox is not None
    assert figure.method == "deterministic_figure_ownership"
    assert figure.confidence == 1.0

    relations = [
        item for item in result.document.relations if item.target_anchor_id == figure.object_id
    ]
    assert any(
        item.relation_type == "caption_of"
        and item.source_anchor_id == figure.caption_anchor_id
        for item in relations
    )
    assert all(item.status == "resolved" for item in relations)


def test_p14_footnotes_preserve_markers_notes_and_owners(
    object_ingest, ground_truth: dict[str, object]
) -> None:
    result, _storage = object_ingest
    expected = {
        item["marker"]: item
        for item in ground_truth["objects"]
        if item["type"] == "footnote"
    }
    footnotes = {
        item.marker: item
        for item in result.document.objects
        if item.object_type == "footnote"
    }
    sections = {item.number: item for item in result.document.sections}

    assert set(footnotes) == {"1", "2"}
    assert footnotes["1"].owner_section_id == sections["6.2"].section_id
    assert footnotes["2"].owner_section_id == sections["10.2"].section_id
    for marker, footnote in footnotes.items():
        assert footnote.text == expected[marker]["text"]
        assert footnote.marker_anchor_id is not None
        assert footnote.note_anchor_id is not None
        assert footnote.source_anchor_ids == (
            footnote.marker_anchor_id,
            footnote.note_anchor_id,
        )
        assert any(
            relation.relation_type == "footnote_marker"
            and relation.source_anchor_id == footnote.marker_anchor_id
            and relation.target_anchor_id == footnote.object_id
            for relation in result.document.relations
        )


def test_p13_p14_preserve_exact_object_block_order(object_ingest) -> None:
    result, _storage = object_ingest
    blocks = {item.block_id: item for item in result.document.blocks}
    pages = {item.physical_page_index: item for item in result.document.pages}

    assert [blocks[item].block_type for item in pages[7].block_ids if ":block:" in item] == [
        "heading",
        "heading",
        "paragraph",
        "heading",
        "paragraph",
        "footnote",
        "paragraph",
        "heading",
        "list",
        "heading",
        "paragraph",
    ]
    assert [
        blocks[item].block_type for item in pages[13].block_ids if ":block:" in item
    ] == [
        "heading",
        "heading",
        "paragraph",
        "heading",
        "paragraph",
        "figure",
        "caption",
        "table",
        "paragraph",
        "footnote",
        "hyperlink",
        "url",
        "heading",
        "list",
    ]


def test_dataforge_reads_figures_and_footnotes_from_provenance_only(
    object_ingest,
) -> None:
    result, storage = object_ingest

    handoff = json.load(storage.open(result.provenance_key))
    objects = handoff["objects"]
    anchors = {item["block_id"] for item in handoff["blocks"]}
    relations = handoff["relations"]

    figure = next(item for item in objects if item["object_type"] == "figure")
    footnotes = [item for item in objects if item["object_type"] == "footnote"]
    assert figure["image_anchor_id"] in anchors
    assert figure["caption_anchor_id"] in anchors
    assert len(footnotes) == 2
    assert all(item["marker_anchor_id"] in anchors for item in footnotes)
    assert all(item["note_anchor_id"] in anchors for item in footnotes)
    assert all(
        any(relation["target_anchor_id"] == item["object_id"] for relation in relations)
        for item in [figure, *footnotes]
    )
