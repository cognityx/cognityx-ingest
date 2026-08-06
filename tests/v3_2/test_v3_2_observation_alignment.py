"""Source-native alignment tests for the production T05 service."""

from __future__ import annotations

from cognityx_ingest import (
    ObservationSourceRegion,
    ParserFusionService,
    ParserObservation,
    ParserObservationSet,
)


def _observation(
    parser_id: str,
    region: ObservationSourceRegion,
    *,
    fact: str = "text",
    value="Exact",
    occurrence_index: int = 1,
) -> ParserObservation:
    """Construct one normal production observation for focused alignment tests."""
    return ParserObservation.create(
        parser_id=parser_id,
        parser_version=None,
        source_region=region,
        fact=fact,
        value=value,
        occurrence_index=occurrence_index,
    )


def test_explicit_region_and_exact_page_anchor_align_exactly() -> None:
    """Prefer explicit region identity, then resource/page/anchor identity."""
    explicit = tuple(
        _observation(parser_id, ObservationSourceRegion("region-shared"))
        for parser_id in ("docling", "pymupdf")
    )
    evidence, groups = ParserFusionService().align(ParserObservationSet.create(explicit))
    assert evidence[0].alignment_method == "exact-source-region-id"
    assert evidence[0].status == "exact"
    assert len(groups) == 1

    anchored = (
        _observation(
            "docling",
            ObservationSourceRegion(
                "region-docling",
                resource_id="resource-a",
                physical_page_index=1,
                source_anchor="anchor-a",
            ),
        ),
        _observation(
            "pymupdf",
            ObservationSourceRegion(
                "region-pymupdf",
                resource_id="resource-a",
                physical_page_index=1,
                source_anchor="anchor-a",
            ),
        ),
    )
    evidence, groups = ParserFusionService().align(ParserObservationSet.create(anchored))
    assert evidence[0].alignment_method == "exact-resource-page-anchor"
    assert groups[0].alignment_status == "exact"


def test_unique_mutual_best_bbox_overlap_aligns_without_averaging_geometry() -> None:
    """Accept one unique mutual-best IoU candidate and preserve both boxes."""
    observations = (
        _observation(
            "docling",
            ObservationSourceRegion(
                "region-docling",
                resource_id="resource-a",
                physical_page_index=0,
                bbox=(0.0, 0.0, 100.0, 100.0),
            ),
        ),
        _observation(
            "pymupdf",
            ObservationSourceRegion(
                "region-pymupdf",
                resource_id="resource-a",
                physical_page_index=0,
                bbox=(1.0, 1.0, 99.0, 99.0),
            ),
        ),
    )
    evidence, groups = ParserFusionService(bbox_iou_threshold=0.8).align(
        ParserObservationSet.create(observations)
    )
    assert evidence[0].alignment_method == "bbox-iou-mutual-best"
    assert evidence[0].status == "accepted-candidate"
    assert len(groups) == 1
    assert {item.source_region.bbox for item in observations} == {
        (0.0, 0.0, 100.0, 100.0),
        (1.0, 1.0, 99.0, 99.0),
    }


def test_multiple_plausible_bbox_candidates_remain_ambiguous() -> None:
    """Retain tied geometry candidates and avoid arbitrary nearest selection."""
    observations = (
        _observation(
            "docling",
            ObservationSourceRegion(
                "region-left",
                resource_id="resource-a",
                physical_page_index=0,
                bbox=(0.0, 0.0, 100.0, 100.0),
            ),
        ),
        _observation(
            "pymupdf",
            ObservationSourceRegion(
                "region-right-a",
                resource_id="resource-a",
                physical_page_index=0,
                bbox=(0.0, 0.0, 100.0, 100.0),
            ),
        ),
        _observation(
            "pymupdf",
            ObservationSourceRegion(
                "region-right-b",
                resource_id="resource-a",
                physical_page_index=0,
                bbox=(0.0, 0.0, 100.0, 100.0),
            ),
        ),
    )
    evidence, groups = ParserFusionService().align(ParserObservationSet.create(observations))
    assert len(evidence) == 2
    assert {item.status for item in evidence} == {"ambiguous"}
    assert any(item.alignment_status == "ambiguous" for item in groups)
    assert all(len(item.observation_ids) == 1 for item in groups)


def test_text_digest_fallback_is_exact_and_respects_occurrence() -> None:
    """Collapse whitespace only for alignment and never accept paraphrase meaning."""
    exact = (
        _observation("docling", ObservationSourceRegion("region-a"), value="A  policy\nrule"),
        _observation("pymupdf", ObservationSourceRegion("region-b"), value="A policy rule"),
    )
    evidence, groups = ParserFusionService().align(ParserObservationSet.create(exact))
    assert evidence[0].alignment_method == "exact-text-digest-occurrence"
    assert len(groups) == 1
    assert exact[0].value.to_value() == "A  policy\nrule"

    different_occurrence = (
        exact[0],
        _observation(
            "pymupdf",
            ObservationSourceRegion("region-c"),
            value="A policy rule",
            occurrence_index=2,
        ),
    )
    evidence, groups = ParserFusionService().align(
        ParserObservationSet.create(different_occurrence)
    )
    assert evidence == ()
    assert len(groups) == 2


def test_semantic_similarity_alone_never_aligns() -> None:
    """Keep paraphrases separate when no exact source-native evidence exists."""
    observations = (
        _observation("docling", ObservationSourceRegion("region-a"), value="Approval is required."),
        _observation("pymupdf", ObservationSourceRegion("region-b"), value="You need authorization."),
    )
    evidence, groups = ParserFusionService().align(ParserObservationSet.create(observations))
    assert evidence == ()
    assert len(groups) == 2
