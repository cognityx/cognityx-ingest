"""Non-copying segmentation view fixture checks for v3.2.

Segmentation views are derived records over canonical node IDs and spans. The
tests walk the frozen view records and fail if source text is copied into a
segment. Future T06 implementers use this as the acceptance boundary.
"""

from __future__ import annotations

import json

from cognityx_ingest import (
    SEGMENTATION_STRATEGIES,
    SEGMENTATION_VIEWS_SCHEMA,
    SegmentationViewService,
)


EXPECTED_VIEW_SEGMENTS = {
    "view-paragraph-v1": ("para-1", "para-2", "para-3", "para-4", "para-5"),
    "view-direct-division-v1": ("div-42-direct", "div-71-direct"),
    "view-docling-structure-v1": ("docling-chunk-1", "docling-chunk-2"),
    "view-fixed-sentence-safe-v1": ("fixed-1", "fixed-2", "fixed-3"),
    "view-sentence-window-v1": ("window-pol-p2",),
    "view-parent-child-v1": ("child-pol-p2",),
}


def test_segmentation_views_reference_ids_and_spans_not_copied_text(v3_2_fixture_root):
    """Assert every view segment references IDs/spans rather than copied text."""
    views = json.loads(
        (v3_2_fixture_root / "segmentation_views" / "views.json").read_text(
            encoding="utf-8"
        )
    )
    for view in views["views"]:
        for segment in view["segments"]:
            assert "text" not in segment
            assert "content" not in segment
            assert any(
                key in segment
                for key in (
                    "node_spans",
                    "retrieval_node_spans",
                    "seed",
                    "context",
                    "native_chunk_pointer",
                )
            )


def test_frozen_schema_and_six_strategy_vocabulary_are_exact(v3_2_fixture_root):
    """Preserve the exact schema and six alternative strategy names."""
    service = SegmentationViewService.from_fixture(v3_2_fixture_root)

    assert service.view_set.schema == SEGMENTATION_VIEWS_SCHEMA
    assert SEGMENTATION_STRATEGIES == (
        "paragraph",
        "direct-division",
        "parser-native-structure",
        "sentence-safe-fixed-size",
        "sentence-window",
        "parent-child",
    )
    assert tuple(view.strategy for view in service.view_set.views) == (
        SEGMENTATION_STRATEGIES
    )


def test_all_frozen_view_and_segment_ids_are_preserved(v3_2_fixture_root):
    """Load all six frozen views without renaming any segment identity."""
    service = SegmentationViewService.from_fixture(v3_2_fixture_root)
    frozen = json.loads(
        (v3_2_fixture_root / "segmentation_views" / "views.json").read_text(
            encoding="utf-8"
        )
    )

    assert service.view_set.to_dict() == frozen
    assert {
        view.view_id: tuple(segment.segment_id for segment in view.segments)
        for view in service.view_set.views
    } == EXPECTED_VIEW_SEGMENTS


def test_frozen_paragraph_view_references_exact_policy_paragraphs(v3_2_fixture_root):
    """Keep the focused paragraph view on pol-p1 through pol-p5 exactly."""
    view = SegmentationViewService.from_fixture(v3_2_fixture_root).build(
        "view-paragraph-v1"
    )

    assert tuple(
        segment.node_spans[0].node_id for segment in view.segments
    ) == ("pol-p1", "pol-p2", "pol-p3", "pol-p4", "pol-p5")
    assert all(segment.text is None for segment in view.segments)
    assert all("text" not in segment.to_dict() for segment in view.segments)


def test_frozen_strategy_roles_remain_separate(v3_2_fixture_root):
    """Preserve direct, native, window, and parent-child fixture meanings."""
    service = SegmentationViewService.from_fixture(v3_2_fixture_root)

    direct = service.build("view-direct-division-v1")
    assert direct.segments[0].division_id == "div-policy-4.2"
    assert tuple(span.node_id for span in direct.segments[0].node_spans) == (
        "pol-heading-42",
        "pol-p1",
        "pol-p2",
        "pol-p3",
        "pol-p4",
    )

    native = service.build("view-docling-structure-v1")
    assert native.profile.get("chunker_id") == "docling-hierarchical"
    assert native.profile.get("native_artifact_id") == "art-docling-001"
    assert native.segments[0].native_chunk_pointer == "#/chunks/0"
    assert tuple(span.node_id for span in native.segments[0].node_spans) == (
        "pol-p2",
    )

    fixed = service.build("view-fixed-sentence-safe-v1")
    assert fixed.profile.to_dict() == {
        "max_tokens": 80,
        "tokenizer": "fixture-tokenizer",
    }

    window = service.build("view-sentence-window-v1").segments[0]
    assert window.seed.node_id == "pol-p2"
    assert tuple(span.node_id for span in window.context) == ("pol-p1", "pol-p3")

    parent_child = service.build("view-parent-child-v1").segments[0]
    assert tuple(span.node_id for span in parent_child.retrieval_node_spans) == (
        "pol-p2",
    )
    assert parent_child.return_scope.division_id == "div-policy-4.2"


def test_serialized_fixture_views_never_emit_a_text_field(v3_2_fixture_root):
    """Prove the compatibility property is absent from persisted JSON."""
    service = SegmentationViewService.from_fixture(v3_2_fixture_root)

    for view in service.view_set.views:
        decoded = json.loads(view.to_json_bytes())
        assert all("text" not in segment for segment in decoded["segments"])
