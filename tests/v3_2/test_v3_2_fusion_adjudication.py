"""End-to-end frozen-case checks for production T05 adjudication."""

from __future__ import annotations

import json

from cognityx_ingest import (
    FUSION_STATES,
    ObservationSourceRegion,
    ParserFusionService,
    ParserObservation,
    ParserObservationSet,
)


def test_fusion_cases_preserve_agreement_conflict_and_unresolved_states(
    fusion_cases, build_fusion_observation_set
) -> None:
    """Run every frozen case through production and preserve all four states."""
    service = ParserFusionService()
    outcomes = {
        case["case_id"]: service.fuse(build_fusion_observation_set(case))
        for case in fusion_cases
    }
    assert tuple(FUSION_STATES) == (
        "agreement",
        "complementary",
        "conflict",
        "unresolved",
    )
    assert {
        decision.state
        for artifact in outcomes.values()
        for decision in artifact.region_decisions
    } == set(FUSION_STATES)
    for case in fusion_cases:
        artifact = outcomes[case["case_id"]]
        assert len(artifact.region_decisions) == 1
        assert artifact.region_decisions[0].state == case["expected"]["state"]


def test_all_seven_frozen_cases_have_exact_contractual_outcomes(
    fusion_cases, build_fusion_observation_set
) -> None:
    """Assert exact accepted facts, resolution reasons, and retained uncertainty."""
    service = ParserFusionService()
    by_case = {
        case["case_id"]: (
            case,
            service.fuse(build_fusion_observation_set(case)),
        )
        for case in fusion_cases
    }

    case, artifact = by_case["agreement-text"]
    decision = artifact.fact_decisions[0]
    assert decision.state == "agreement"
    assert len(decision.accepted_observation_ids) == 2
    accepted_values = {
        build_fusion_observation_set(case).get(item).value.to_value()
        for item in decision.accepted_observation_ids
    }
    assert accepted_values == {case["expected"]["accepted_value"]}
    assert artifact.region_decisions[0].source_parsers == ("docling", "pymupdf")

    _case, artifact = by_case["complementary-link-and-structure"]
    assert {item.fact for item in artifact.fact_decisions} == {
        "owner_division",
        "native_link_target",
    }
    assert all(item.state == "complementary" for item in artifact.fact_decisions)

    _case, artifact = by_case["split-versus-merged"]
    decision = artifact.fact_decisions[0]
    assert decision.state == "conflict"
    assert decision.resolution == (
        "align-by-source-span-then-preserve-both-segmentation-observations"
    )
    assert len(decision.observation_ids) == 2

    _case, artifact = by_case["reading-order-conflict"]
    decision = artifact.fact_decisions[0]
    assert decision.state == "unresolved"
    assert decision.accepted_observation_ids == ()
    assert decision.gold_eligible is False
    assert decision.required_action == "selective-review-or-third-parser"

    _case, artifact = by_case["bbox-conflict"]
    decision = artifact.fact_decisions[0]
    assert decision.state == "conflict"
    assert decision.resolution == "fact-specific-policy"
    assert len(decision.observation_ids) == 2
    assert decision.accepted_observation_ids == ()

    case, artifact = by_case["table-versus-text"]
    observation_set = build_fusion_observation_set(case)
    decision = artifact.fact_decisions[0]
    assert decision.state == "conflict"
    assert decision.resolution == "richer-validated-structure"
    assert len(decision.rejected_observation_ids) == 1
    assert observation_set.get(decision.accepted_observation_ids[0]).value.to_value() == "table"

    _case, artifact = by_case["caption-and-image-geometry"]
    assert artifact.region_decisions[0].state == "complementary"
    assert {item.fact for item in artifact.fact_decisions} == {"caption", "image_bbox"}


def test_ambiguous_and_unresolved_relations_are_never_gold_support(
    v3_2_fixture_root
) -> None:
    """Call production adjudication and retain the frozen ambiguous graph boundary."""
    graph = json.loads(
        (v3_2_fixture_root / "expected" / "source_graph.json").read_text(
            encoding="utf-8"
        )
    )
    relation = next(
        item for item in graph["relations"] if item["relation_id"] == "rel-ambiguous-example"
    )
    assert relation["status"] == "ambiguous"
    assert relation["epistemic_state"] == "ambiguous"
    assert relation["gold_eligible"] is False
    assert len(relation["candidate_target_ids"]) == 2

    region = ObservationSourceRegion("region-ambiguous-relation")
    observations = tuple(
        ParserObservation.create(
            parser_id=parser_id,
            parser_version=None,
            source_region=region,
            fact="target_anchor",
            value=target,
            epistemic_state="ambiguous",
        )
        for parser_id, target in zip(
            ("docling", "pymupdf"), relation["candidate_target_ids"], strict=True
        )
    )
    artifact = ParserFusionService().fuse(ParserObservationSet.create(observations))
    assert artifact.fact_decisions[0].state == "conflict"
    assert artifact.fact_decisions[0].gold_eligible is False
    assert artifact.gold_eligible_decisions() == ()
