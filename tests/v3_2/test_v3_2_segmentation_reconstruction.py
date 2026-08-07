"""Read-time reconstruction and immutability proofs for T06 segmentation views."""

from __future__ import annotations

from cognityx_ingest import NodeSpan, SegmentationViewService


def test_resolve_whole_node_and_character_range_from_canonical_content(
    frozen_canonical_artifact,
):
    """Return canonical text or a half-open slice without changing the span."""
    service = SegmentationViewService.from_canonical(frozen_canonical_artifact)
    expected = next(
        node.content.text
        for node in frozen_canonical_artifact.content_nodes
        if node.node_id == "pol-p2"
    )
    span = NodeSpan("pol-p2", 4, 20)

    assert service.resolve_span(NodeSpan("pol-p2")) == expected
    assert service.resolve_span(span) == expected[4:20]
    assert span.to_dict() == {"node_id": "pol-p2", "char_start": 4, "char_end": 20}


def test_multi_node_reconstruction_returns_ordered_slices_not_concatenation(
    v3_2_fixture_root,
):
    """Leave semantic joining decisions to the downstream caller."""
    service = SegmentationViewService.from_fixture(v3_2_fixture_root)

    slices = service.resolve_segment_spans(
        "fixed-1", view_id="view-fixed-sentence-safe-v1"
    )

    assert slices == (
        "Employees may claim travel expenses only for approved business travel.",
        "The ordinary approval limit is ₹25,000 with manager approval.",
    )
    assert isinstance(slices, tuple)


def test_canonical_bytes_are_unchanged_by_build_and_reconstruction(
    frozen_canonical_artifact,
):
    """Prove view construction and reads cannot rewrite the canonical artifact."""
    before = frozen_canonical_artifact.to_json_bytes()
    service = SegmentationViewService.from_canonical(frozen_canonical_artifact)

    view = service.build_paragraph("paragraph-immutable")
    after_build = frozen_canonical_artifact.to_json_bytes()
    resolved = service.resolve_segment_spans(
        view.segments[0].segment_id, view=view
    )
    after_read = frozen_canonical_artifact.to_json_bytes()

    assert resolved
    assert after_build == before
    assert after_read == before


def test_distinctive_canonical_sentences_never_appear_in_view_bytes(
    frozen_canonical_artifact,
):
    """Search both individual and aggregate serialization for exact canaries."""
    service = SegmentationViewService.from_canonical(frozen_canonical_artifact)
    paragraph = service.build_paragraph("paragraph-canary")
    direct = service.build_direct_division(
        "direct-canary", division_ids=("div-policy-4.2",)
    )
    aggregate = service.build_view_set((paragraph, direct))
    canonical_sentences = tuple(
        node.content.text.encode("utf-8")
        for node in frozen_canonical_artifact.content_nodes
    )

    for payload in (
        paragraph.to_json_bytes(),
        direct.to_json_bytes(),
        aggregate.to_json_bytes(),
    ):
        assert all(sentence not in payload for sentence in canonical_sentences)


def test_parent_return_scope_reconstructs_canonical_subtree_at_read_time(
    frozen_canonical_artifact,
):
    """Resolve the parent division without materializing a duplicate parent chunk."""
    service = SegmentationViewService.from_canonical(frozen_canonical_artifact)
    view = service.build_parent_child(
        "parent-reconstruct", retrieval_node_ids=("pol-p2",)
    )
    segment = view.segments[0]

    resolved = service.resolve_return_scope(segment.segment_id, view=view)
    expected = tuple(
        node.content.text
        for node in frozen_canonical_artifact.subtree_nodes("div-policy-4.2")
    )

    assert resolved == expected
    assert all(text.encode("utf-8") not in view.to_json_bytes() for text in resolved)


def test_t01_and_t05_fixture_bytes_are_untouched_by_t06_loading(v3_2_fixture_root):
    """Keep native artifacts and fusion decisions frozen across view operations."""
    protected = (
        v3_2_fixture_root / "native_artifacts" / "docling_document_opaque.json",
        v3_2_fixture_root / "native_artifacts" / "pymupdf_observations.json",
        v3_2_fixture_root / "parser_observations" / "fusion_cases.json",
    )
    before = {path: path.read_bytes() for path in protected}

    service = SegmentationViewService.from_fixture(v3_2_fixture_root)
    service.build("view-docling-structure-v1").to_json_bytes()
    service.resolve_segment_spans(
        "docling-chunk-1", view_id="view-docling-structure-v1"
    )

    assert {path: path.read_bytes() for path in protected} == before
