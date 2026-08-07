"""Source-native alignment tests for the production T05 service."""

from __future__ import annotations

import pytest

from cognityx_ingest import (
    ExtractedPage,
    ExtractedRelation,
    ExtractionResult,
    ObservationSourceRegion,
    ParserAlignmentError,
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


def _relation_result(
    parser_id: str,
    relations: tuple[ExtractedRelation, ...],
) -> ExtractionResult:
    """Build one completed production parser result for relation alignment tests."""
    return ExtractionResult(
        pages=(
            ExtractedPage(
                1,
                "Relation page",
                page_index=0,
                relations=relations,
            ),
        ),
        backend=parser_id,
    )


def _relation_observation_set(
    results: tuple[ExtractionResult, ...],
) -> ParserObservationSet:
    """Run production adaptation and retain only typed relation observations."""
    adapted = ParserFusionService().build_observation_set(results)
    return ParserObservationSet.create(tuple(
        item
        for item in adapted.observations
        if item.source_region.region_kind == "relation"
    ))


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
                region_kind="block",
                resource_id="resource-a",
                physical_page_index=1,
                source_anchor="anchor-a",
            ),
        ),
        _observation(
            "pymupdf",
            ObservationSourceRegion(
                "region-pymupdf",
                region_kind="block",
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
                region_kind="block",
                resource_id="resource-a",
                physical_page_index=0,
                bbox=(0.0, 0.0, 100.0, 100.0),
            ),
        ),
        _observation(
            "pymupdf",
            ObservationSourceRegion(
                "region-pymupdf",
                region_kind="block",
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
                region_kind="block",
                resource_id="resource-a",
                physical_page_index=0,
                bbox=(0.0, 0.0, 100.0, 100.0),
            ),
        ),
        _observation(
            "pymupdf",
            ObservationSourceRegion(
                "region-right-a",
                region_kind="block",
                resource_id="resource-a",
                physical_page_index=0,
                bbox=(0.0, 0.0, 100.0, 100.0),
            ),
        ),
        _observation(
            "pymupdf",
            ObservationSourceRegion(
                "region-right-b",
                region_kind="block",
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


def test_one_parser_block_facts_form_one_region_and_one_summary() -> None:
    """Aggregate text, type, geometry, and order before comparing any fact."""
    region = ObservationSourceRegion(
        "block-docling-a",
        region_kind="block",
        resource_id="resource-a",
        physical_page_index=0,
        bbox=(0.0, 0.0, 100.0, 40.0),
    )
    observations = tuple(
        _observation("docling", region, fact=fact, value=value)
        for fact, value in (
            ("text", "Policy text"),
            ("block_type", "paragraph"),
            ("bbox", [0.0, 0.0, 100.0, 40.0]),
            ("reading_order", 1),
        )
    )
    artifact = ParserFusionService().fuse(ParserObservationSet.create(observations))
    assert len(artifact.aligned_groups) == 1
    assert artifact.aligned_groups[0].observation_ids == tuple(
        sorted(item.observation_id for item in observations)
    )
    assert artifact.aligned_groups[0].source_region_ids == ("block-docling-a",)
    assert len(artifact.region_decisions) == 1


def test_same_region_id_rejects_incompatible_location_evidence() -> None:
    """Fail before alignment when repeated region facts contradict geometry."""
    observations = (
        _observation(
            "docling",
            ObservationSourceRegion(
                "block-docling-a",
                region_kind="block",
                resource_id="resource-a",
                physical_page_index=0,
                bbox=(0.0, 0.0, 100.0, 40.0),
            ),
            fact="text",
            value="Policy text",
        ),
        _observation(
            "docling",
            ObservationSourceRegion(
                "block-docling-a",
                region_kind="block",
                resource_id="resource-a",
                physical_page_index=0,
                bbox=(0.0, 0.0, 200.0, 40.0),
            ),
            fact="bbox",
            value=[0.0, 0.0, 200.0, 40.0],
        ),
    )
    with pytest.raises(ParserAlignmentError, match="bbox"):
        ParserFusionService().align(ParserObservationSet.create(observations))


def test_same_region_id_rejects_incompatible_non_generic_kinds() -> None:
    """Fail closed when an explicit ID claims both block and object meaning."""
    observations = (
        _observation(
            "docling",
            ObservationSourceRegion("region-shared", region_kind="block"),
        ),
        _observation(
            "pymupdf",
            ObservationSourceRegion("region-shared", region_kind="object"),
        ),
    )
    with pytest.raises(ParserAlignmentError, match="region kinds"):
        ParserFusionService().align(ParserObservationSet.create(observations))


def test_two_multifact_blocks_align_once_at_region_level() -> None:
    """Avoid sixteen tied fact comparisons for two four-fact parser blocks."""
    observations = []
    for parser_id, region_id, bbox in (
        ("docling", "block-docling", (0.0, 0.0, 100.0, 40.0)),
        ("pymupdf", "block-pymupdf", (1.0, 0.0, 101.0, 40.0)),
    ):
        region = ObservationSourceRegion(
            region_id,
            region_kind="block",
            resource_id="resource-a",
            physical_page_index=0,
            bbox=bbox,
        )
        observations.extend(
            _observation(parser_id, region, fact=fact, value=value)
            for fact, value in (
                ("text", "Policy text"),
                ("block_type", "paragraph"),
                ("bbox", list(bbox)),
                ("reading_order", 1),
            )
        )
    observation_set = ParserObservationSet.create(tuple(observations))
    artifact = ParserFusionService().fuse(observation_set)
    assert len(artifact.alignment_evidence) == 1
    assert artifact.alignment_evidence[0].status == "accepted-candidate"
    assert len(artifact.aligned_groups) == 1
    assert len(artifact.aligned_groups[0].observation_ids) == 8
    assert len(artifact.region_decisions) == 1


def test_two_multifact_objects_align_once_at_region_level() -> None:
    """Align caption, object type, and geometry as one source object."""
    observations = []
    for parser_id, region_id, bbox in (
        ("docling", "object-docling", (10.0, 10.0, 90.0, 80.0)),
        ("pymupdf", "object-pymupdf", (11.0, 10.0, 91.0, 80.0)),
    ):
        region = ObservationSourceRegion(
            region_id,
            region_kind="object",
            resource_id="resource-a",
            physical_page_index=0,
            bbox=bbox,
        )
        observations.extend(
            _observation(parser_id, region, fact=fact, value=value)
            for fact, value in (
                ("caption", "Figure 1"),
                ("object_type", "figure"),
                ("bbox", list(bbox)),
            )
        )
    artifact = ParserFusionService().fuse(
        ParserObservationSet.create(tuple(observations))
    )
    assert len(artifact.alignment_evidence) == 1
    assert len(artifact.aligned_groups) == 1
    assert len(artifact.region_decisions) == 1


def test_exact_anchor_suppresses_weaker_bbox_ambiguity() -> None:
    """Keep a reviewed exact pair accepted while preserving weaker audit evidence."""
    observations = (
        _observation(
            "docling",
            ObservationSourceRegion(
                "region-a",
                region_kind="block",
                resource_id="resource-a",
                physical_page_index=0,
                source_anchor="anchor-shared",
                bbox=(0.0, 0.0, 100.0, 100.0),
            ),
        ),
        _observation(
            "pymupdf",
            ObservationSourceRegion(
                "region-b",
                region_kind="block",
                resource_id="resource-a",
                physical_page_index=0,
                source_anchor="anchor-shared",
                bbox=(0.0, 0.0, 100.0, 100.0),
            ),
        ),
        _observation(
            "pymupdf",
            ObservationSourceRegion(
                "region-c",
                region_kind="block",
                resource_id="resource-a",
                physical_page_index=0,
                source_anchor="anchor-other",
                bbox=(0.0, 0.0, 100.0, 100.0),
            ),
        ),
    )
    evidence, groups = ParserFusionService().align(
        ParserObservationSet.create(observations)
    )
    assert {item.status for item in evidence} == {"exact", "superseded"}
    exact_group = next(group for group in groups if len(group.observation_ids) == 2)
    assert exact_group.alignment_status == "exact"
    assert all(group.alignment_status != "ambiguous" for group in groups)


def test_region_fact_and_parser_input_reversal_is_byte_stable() -> None:
    """Normalize parser, region, and observation order before identity hashing."""
    regions = (
        ObservationSourceRegion(
            "region-a", region_kind="block", resource_id="resource-a", physical_page_index=0,
            bbox=(0.0, 0.0, 50.0, 50.0),
        ),
        ObservationSourceRegion(
            "region-b", region_kind="block", resource_id="resource-a", physical_page_index=0,
            bbox=(0.0, 0.0, 50.0, 50.0),
        ),
    )
    observations = tuple(
        _observation(parser, region, fact=fact, value=value)
        for parser, region in zip(("docling", "pymupdf"), regions, strict=True)
        for fact, value in (("text", "same"), ("block_type", "paragraph"))
    )
    service = ParserFusionService()
    first = service.fuse(ParserObservationSet.create(observations))
    second = service.fuse(ParserObservationSet.create(tuple(reversed(observations))))
    assert first.to_json_bytes() == second.to_json_bytes()


def test_block_and_object_with_identical_geometry_remain_separate() -> None:
    """Forbid coincident block and object boxes from creating an alignment edge."""
    observations = (
        _observation(
            "docling",
            ObservationSourceRegion(
                "block-docling",
                region_kind="block",
                resource_id="resource-a",
                physical_page_index=0,
                bbox=(0.0, 0.0, 100.0, 100.0),
            ),
            fact="text",
            value="Block text",
        ),
        _observation(
            "pymupdf",
            ObservationSourceRegion(
                "object-pymupdf",
                region_kind="object",
                resource_id="resource-a",
                physical_page_index=0,
                bbox=(0.0, 0.0, 100.0, 100.0),
            ),
            fact="object_type",
            value="table",
        ),
    )
    artifact = ParserFusionService().fuse(
        ParserObservationSet.create(observations)
    )
    assert artifact.alignment_evidence == ()
    assert len(artifact.aligned_groups) == 2
    assert len(artifact.region_decisions) == 2


def test_block_and_relation_sharing_an_anchor_never_align() -> None:
    """Keep a relation endpoint distinct from a block record with the same anchor."""
    observations = (
        _observation(
            "docling",
            ObservationSourceRegion(
                "block-docling",
                region_kind="block",
                resource_id="resource-a",
                physical_page_index=0,
                source_anchor="shared-endpoint",
            ),
        ),
        _observation(
            "pymupdf",
            ObservationSourceRegion(
                "relation-pymupdf",
                region_kind="relation",
                resource_id="resource-a",
                physical_page_index=0,
                source_anchor="shared-endpoint",
                native_record_id="relation-1",
            ),
            fact="source_anchor",
            value="shared-endpoint",
        ),
    )
    evidence, groups = ParserFusionService().align(
        ParserObservationSet.create(observations)
    )
    assert evidence == ()
    assert len(groups) == 2


def test_exact_anchor_fanout_is_ambiguous_and_order_independent() -> None:
    """Retain equal-strength anchor fan-out without forming a three-way union."""
    observations = tuple(
        _observation(
            parser_id,
            ObservationSourceRegion(
                region_id,
                region_kind="block",
                resource_id="resource-a",
                physical_page_index=0,
                source_anchor="shared-anchor",
            ),
        )
        for parser_id, region_id in (
            ("docling", "region-a"),
            ("pymupdf", "region-b"),
            ("pymupdf", "region-c"),
        )
    )
    service = ParserFusionService()
    first = service.fuse(ParserObservationSet.create(observations))
    second = service.fuse(
        ParserObservationSet.create(tuple(reversed(observations)))
    )
    assert {item.status for item in first.alignment_evidence} == {"ambiguous"}
    assert len(first.aligned_groups) == 3
    assert all(item.alignment_status == "ambiguous" for item in first.aligned_groups)
    assert first.to_json_bytes() == second.to_json_bytes()


@pytest.mark.parametrize(
    ("locator", "method"),
    (
        ({"selector_ids": ("selector-shared",)}, "exact-selector"),
        ({"char_start": 10, "char_end": 20}, "exact-character-span"),
    ),
)
def test_exact_selector_and_span_fanout_remain_ambiguous(
    locator: dict[str, object],
    method: str,
) -> None:
    """Apply reciprocal uniqueness to selector and character-span equality."""
    observations = tuple(
        _observation(
            parser_id,
            ObservationSourceRegion(
                region_id,
                region_kind="block",
                resource_id="resource-a",
                physical_page_index=0,
                **locator,
            ),
        )
        for parser_id, region_id in (
            ("docling", "region-a"),
            ("pymupdf", "region-b"),
            ("pymupdf", "region-c"),
        )
    )
    evidence, groups = ParserFusionService().align(
        ParserObservationSet.create(observations)
    )
    assert {item.alignment_method for item in evidence} == {method}
    assert {item.status for item in evidence} == {"ambiguous"}
    assert len(groups) == 3


def test_three_parsers_with_one_explicit_region_form_one_deterministic_group() -> None:
    """Record every unique parser pair for one intentionally shared generic ID."""
    observations = tuple(
        _observation(parser_id, ObservationSourceRegion("region-reviewed"))
        for parser_id in ("docling", "marker", "pymupdf")
    )
    evidence, groups = ParserFusionService().align(
        ParserObservationSet.create(tuple(reversed(observations)))
    )
    assert len(evidence) == 3
    assert {item.status for item in evidence} == {"exact"}
    assert len(groups) == 1
    assert groups[0].parser_ids == ("docling", "marker", "pymupdf")


def test_relation_endpoint_sharing_does_not_merge_different_targets() -> None:
    """Require a complete exact relation signature rather than endpoint equality."""
    observation_set = _relation_observation_set((
        _relation_result(
            "docling",
            (ExtractedRelation(
                "docling-link-a",
                "source-shared",
                "target-a",
                "references",
            ),),
        ),
        _relation_result(
            "pymupdf",
            (ExtractedRelation(
                "pymupdf-link-b",
                "source-shared",
                "target-b",
                "references",
            ),),
        ),
    ))
    evidence, groups = ParserFusionService().align(observation_set)
    assert evidence == ()
    assert len(groups) == 2
    assert {item.value.to_value() for item in observation_set.observations_for_fact(
        "source_anchor"
    )} == {"source-shared"}


def test_relation_signatures_pair_only_corresponding_distinct_targets() -> None:
    """Align exact links by relation type, endpoint, and target without target text."""
    results = tuple(
        _relation_result(
            parser_id,
            tuple(
                ExtractedRelation(
                    f"{parser_id}-link-{suffix}",
                    "source-shared",
                    f"target-{suffix}",
                    "references",
                    target_text=f"Sensitive target {suffix}",
                )
                for suffix in ("a", "b")
            ),
        )
        for parser_id in ("docling", "pymupdf")
    )
    observation_set = _relation_observation_set(results)
    artifact = ParserFusionService().fuse(observation_set)
    signature_edges = tuple(
        item for item in artifact.alignment_evidence
        if item.alignment_method == "exact-relation-signature"
    )
    assert len(signature_edges) == 2
    assert {item.status for item in signature_edges} == {"exact"}
    assert len(artifact.aligned_groups) == 2
    assert b"Sensitive target" not in artifact.to_json_bytes()


def test_duplicate_relation_signatures_remain_ambiguous_and_byte_stable() -> None:
    """Refuse arbitrary pairing of duplicate links and normalize relation order."""
    def results(*, reverse: bool) -> tuple[ExtractionResult, ...]:
        """Return equivalent duplicate-link results in either native tuple order."""
        built = []
        for parser_id in ("docling", "pymupdf"):
            relations = tuple(
                ExtractedRelation(
                    f"{parser_id}-duplicate-{index}",
                    "source-shared",
                    "target-shared",
                    "references",
                )
                for index in (1, 2)
            )
            built.append(_relation_result(
                parser_id,
                tuple(reversed(relations)) if reverse else relations,
            ))
        return tuple(built)

    service = ParserFusionService()
    first_set = _relation_observation_set(results(reverse=False))
    second_set = _relation_observation_set(results(reverse=True))
    first = service.fuse(first_set)
    second = service.fuse(second_set)
    assert first_set.to_json_bytes() == second_set.to_json_bytes()
    assert first.to_json_bytes() == second.to_json_bytes()
    assert len(first.alignment_evidence) == 4
    assert {item.status for item in first.alignment_evidence} == {"ambiguous"}
    assert len(first.aligned_groups) == 4
    assert all(item.state == "unresolved" for item in first.region_decisions)
    assert first.gold_eligible_decisions() == ()
