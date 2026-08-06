"""Bounded fact-policy and gold-eligibility tests for production T05."""

from __future__ import annotations

import pytest

from cognityx_ingest import (
    FactAdjudicationPolicy,
    FactFusionDecision,
    ObservationValue,
    ParserAdjudicationError,
    ParserFusionService,
    ParserFusionValidationError,
)


def test_object_type_policy_accepts_table_but_retains_conflict(
    fusion_cases, build_fusion_observation_set
) -> None:
    """Record explicit structural preference without renaming conflict agreement."""
    case = next(item for item in fusion_cases if item["case_id"] == "table-versus-text")
    observation_set = build_fusion_observation_set(case)
    artifact = ParserFusionService().fuse(observation_set)
    decision = artifact.fact_decisions[0]
    accepted = observation_set.get(decision.accepted_observation_ids[0])
    rejected = observation_set.get(decision.rejected_observation_ids[0])
    assert accepted.value.to_value() == "table"
    assert rejected.value.to_value() == "paragraph_text"
    assert decision.state == "conflict"
    assert decision.policy_id == "policy-object-type"
    assert decision.gold_eligible is False


def test_custom_policy_is_data_only_and_value_specific(
    build_fusion_observation_set,
) -> None:
    """Apply an explicit persisted value rather than a backend or confidence rule."""
    observation_set = build_fusion_observation_set(
        {
            "source_region_id": "region-classification",
            "observations": [
                {"parser": "docling", "fact": "classification", "value": "approved"},
                {"parser": "pymupdf", "fact": "classification", "value": "draft"},
            ],
        }
    )
    policy = FactAdjudicationPolicy(
        policy_id="policy-classification",
        fact="classification",
        strategy="prefer-explicit-value",
        preferred_values=(ObservationValue.from_value("approved"),),
        resolution_code="reviewed-business-rule",
        retain_all_observations=True,
        gold_eligible_on_accept=True,
    )
    decision = ParserFusionService().fuse(
        observation_set, policies=(policy,)
    ).fact_decisions[0]
    assert decision.state == "conflict"
    assert decision.resolution == "reviewed-business-rule"
    assert len(decision.accepted_observation_ids) == 1
    assert len(decision.rejected_observation_ids) == 1


def test_policy_rejects_unbounded_strategy_and_duplicate_fact_ownership(
    build_fusion_observation_set,
) -> None:
    """Prevent executable or ambiguous policy behavior from entering artifacts."""
    with pytest.raises(ParserAdjudicationError):
        FactAdjudicationPolicy(
            policy_id="policy-unsafe",
            fact="text",
            strategy="eval-python-expression",
        )
    observation_set = build_fusion_observation_set(
        {
            "source_region_id": "region-policy",
            "observations": [
                {"parser": "docling", "fact": "classification", "value": "a"},
                {"parser": "pymupdf", "fact": "classification", "value": "b"},
            ],
        }
    )
    policies = tuple(
        FactAdjudicationPolicy(
            policy_id=f"policy-classification-{index}",
            fact="classification",
            strategy="preserve-conflict",
        )
        for index in range(2)
    )
    with pytest.raises(ParserAdjudicationError):
        ParserFusionService().fuse(observation_set, policies=policies)


def test_invalid_decision_states_fail_direct_construction() -> None:
    """Reject accepted overlap and unresolved acceptance through typed errors."""
    with pytest.raises(ParserFusionValidationError):
        FactFusionDecision(
            decision_id="decision-overlap",
            source_region_id="region-overlap",
            fact="text",
            state="conflict",
            observation_ids=("obs-a", "obs-b"),
            accepted_observation_ids=("obs-a",),
            rejected_observation_ids=("obs-a",),
            resolution="explicit",
            policy_id="policy-explicit",
        )
    with pytest.raises(ParserFusionValidationError):
        FactFusionDecision(
            decision_id="decision-unresolved",
            source_region_id="region-unresolved",
            fact="text",
            state="unresolved",
            observation_ids=("obs-a",),
            accepted_observation_ids=("obs-a",),
            rejected_observation_ids=(),
            resolution="review",
        )
