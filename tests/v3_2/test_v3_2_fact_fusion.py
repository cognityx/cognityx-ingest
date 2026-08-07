"""Fact grouping and deterministic fusion tests for production T05."""

from __future__ import annotations

from cognityx_ingest import ParserFusionService


def test_reversing_observations_produces_identical_fusion_bytes(
    fusion_cases, build_fusion_observation_set
) -> None:
    """Normalize caller order before IDs, groups, decisions, and serialization."""
    case = next(item for item in fusion_cases if item["case_id"] == "agreement-text")
    observation_set = build_fusion_observation_set(case)
    reversed_set = type(observation_set).create(tuple(reversed(observation_set.observations)))
    service = ParserFusionService()
    first = service.fuse(observation_set)
    second = service.fuse(reversed_set)
    assert first == second
    assert first.to_json_bytes() == second.to_json_bytes()
    assert first.state_counts == second.state_counts


def test_agreement_retains_every_supporting_observation(
    fusion_cases, build_fusion_observation_set
) -> None:
    """Accept all equivalent observations rather than selecting one parser winner."""
    case = next(item for item in fusion_cases if item["case_id"] == "agreement-text")
    artifact = ParserFusionService().fuse(build_fusion_observation_set(case))
    decision = artifact.fact_decisions[0]
    assert decision.accepted_observation_ids == decision.observation_ids
    assert decision.rejected_observation_ids == ()


def test_complementary_facts_remain_separate_decisions(
    fusion_cases, build_fusion_observation_set
) -> None:
    """Retain coexisting facts without merging them into one invented value."""
    case = next(
        item
        for item in fusion_cases
        if item["case_id"] == "complementary-link-and-structure"
    )
    artifact = ParserFusionService().fuse(build_fusion_observation_set(case))
    assert len(artifact.fact_decisions) == 2
    assert {item.fact for item in artifact.fact_decisions} == {
        "owner_division",
        "native_link_target",
    }
    assert all(len(item.accepted_observation_ids) == 1 for item in artifact.fact_decisions)


def test_conflicts_retain_values_and_never_average_bboxes(
    fusion_cases, build_fusion_observation_set
) -> None:
    """Reference both exact bbox observations without creating averaged geometry."""
    case = next(item for item in fusion_cases if item["case_id"] == "bbox-conflict")
    observation_set = build_fusion_observation_set(case)
    artifact = ParserFusionService().fuse(observation_set)
    decision = artifact.fact_decisions[0]
    assert decision.observation_ids == tuple(
        sorted(item.observation_id for item in observation_set.observations)
    )
    values = {item.value.to_json_bytes() for item in observation_set.observations}
    assert values == {b"[10,20,200,50]", b"[8,18,205,52]"}
    assert b"[9" not in artifact.to_json_bytes()


def test_confidence_alone_never_selects_a_conflicting_winner(
    fusion_cases, build_fusion_observation_set
) -> None:
    """Leave differing text unresolved by policy regardless of confidence order."""
    case = {
        "source_region_id": "region-confidence",
        "observations": [
            {"parser": "docling", "fact": "text", "value": "A", "confidence": 0.99},
            {"parser": "pymupdf", "fact": "text", "value": "B", "confidence": 0.20},
        ],
    }
    artifact = ParserFusionService().fuse(build_fusion_observation_set(case))
    decision = artifact.fact_decisions[0]
    assert decision.state == "conflict"
    assert decision.accepted_observation_ids == ()


def test_split_and_merged_segmentation_observations_are_both_retained(
    fusion_cases, build_fusion_observation_set
) -> None:
    """Preserve both segmentation variants without materializing a T06 view."""
    case = next(item for item in fusion_cases if item["case_id"] == "split-versus-merged")
    observation_set = build_fusion_observation_set(case)
    artifact = ParserFusionService().fuse(observation_set)
    decision = artifact.fact_decisions[0]
    assert set(decision.observation_ids) == {
        item.observation_id for item in observation_set.observations
    }
    assert decision.accepted_observation_ids == ()
