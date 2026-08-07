"""Bounded fact-policy and gold-eligibility tests for production T05."""

from __future__ import annotations

import pytest

from cognityx_ingest import (
    FactAdjudicationPolicy,
    FactFusionDecision,
    ObservationSourceRegion,
    ObservationValue,
    ParserAdjudicationError,
    ParserFusionArtifact,
    ParserFusionService,
    ParserFusionValidationError,
    ParserObservation,
    ParserObservationSet,
)


def _priority_observation_set(
    values: tuple[tuple[str, str], ...],
) -> ParserObservationSet:
    """Build one conflicting fact set for ordered-preference production tests.

    The helper creates normal public observations in a shared source region so
    ``ParserFusionService`` executes its real policy path. Parser/value pairs are
    explicit and no expected decision is derived inside the fixture.
    """
    region = ObservationSourceRegion("region-priority-policy")
    return ParserObservationSet.create(
        tuple(
            ParserObservation.create(
                parser_id=parser_id,
                parser_version=None,
                source_region=region,
                fact="classification",
                value=value,
            )
            for parser_id, value in values
        )
    )


def _priority_policy(*values: str) -> FactAdjudicationPolicy:
    """Create the reviewed ordered-value policy used by priority tests.

    Tests supply values in contractual order. The helper wraps each value in the
    production immutable representation without sorting or deduplicating it, so
    policy validation and replay remain responsible for enforcing semantics.
    """
    return FactAdjudicationPolicy(
        "policy-priority",
        fact="classification",
        strategy="prefer-explicit-value",
        preferred_values=tuple(ObservationValue.from_value(item) for item in values),
        resolution_code="reviewed-priority",
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


@pytest.mark.parametrize(
    ("strategy", "fact", "preferred", "expected_state", "accepted_count"),
    (
        ("exact-agreement", "classification", (), "conflict", 0),
        ("retain-complementary", "classification", (), "conflict", 0),
        ("preserve-conflict", "classification", (), "conflict", 0),
        (
            "prefer-explicit-value",
            "classification",
            (ObservationValue.from_value("approved"),),
            "conflict",
            1,
        ),
        ("require-review", "classification", (), "unresolved", 0),
        ("preserve-segmentation-variants", "segmentation", (), "conflict", 0),
    ),
)
def test_every_public_policy_strategy_has_executable_semantics(
    strategy: str,
    fact: str,
    preferred: tuple[ObservationValue, ...],
    expected_state: str,
    accepted_count: int,
) -> None:
    """Dispatch every declared strategy and retain complete classification."""
    region = ObservationSourceRegion("region-policy-strategy")
    observations = tuple(
        ParserObservation.create(
            parser_id=parser_id,
            parser_version=None,
            source_region=region,
            fact=fact,
            value=value,
        )
        for parser_id, value in (("docling", "approved"), ("pymupdf", "draft"))
    )
    policy = FactAdjudicationPolicy(
        policy_id=f"policy-{strategy}",
        fact=fact,
        strategy=strategy,
        preferred_values=preferred,
        resolution_code=f"reviewed-{strategy}",
    )
    decision = ParserFusionService().fuse(
        ParserObservationSet.create(observations), policies=(policy,)
    ).fact_decisions[0]
    assert decision.state == expected_state
    assert len(decision.accepted_observation_ids) == accepted_count
    assert set(decision.accepted_observation_ids) | set(
        decision.rejected_observation_ids
    ) == set(decision.observation_ids)
    if strategy == "require-review":
        assert decision.required_action == "selective-review-or-third-parser"
    if strategy == "preserve-segmentation-variants":
        assert decision.resolution == "reviewed-preserve-segmentation-variants"


def test_agreement_requires_two_distinct_parser_ids() -> None:
    """Do not convert repeated observations from one parser into agreement."""
    region = ObservationSourceRegion("region-one-parser")
    observations = tuple(
        ParserObservation.create(
            parser_id="docling",
            parser_version=None,
            source_region=region,
            fact="text",
            value="same",
            occurrence_index=index,
        )
        for index in (1, 2)
    )
    policy = FactAdjudicationPolicy(
        "policy-text-exact",
        fact="text",
        strategy="exact-agreement",
    )
    decision = ParserFusionService().fuse(
        ParserObservationSet.create(observations), policies=(policy,)
    ).fact_decisions[0]
    assert decision.state == "complementary"
    assert decision.accepted_observation_ids == decision.observation_ids


def test_fact_policy_overrides_bounded_family_policy() -> None:
    """Apply exact fact ownership before the reviewed textual family."""
    region = ObservationSourceRegion("region-family-policy")
    observations = tuple(
        ParserObservation.create(
            parser_id=parser_id,
            parser_version=None,
            source_region=region,
            fact="text",
            value=value,
        )
        for parser_id, value in (("docling", "approved"), ("pymupdf", "draft"))
    )
    policies = (
        FactAdjudicationPolicy(
            "policy-family-textual",
            fact_family="textual",
            strategy="require-review",
        ),
        FactAdjudicationPolicy(
            "policy-fact-text",
            fact="text",
            strategy="prefer-explicit-value",
            preferred_values=(ObservationValue.from_value("approved"),),
        ),
    )
    artifact = ParserFusionService().fuse(
        ParserObservationSet.create(observations), policies=policies
    )
    decision = artifact.fact_decisions[0]
    assert decision.policy_id == "policy-fact-text"
    assert decision.state == "conflict"
    assert len(decision.accepted_observation_ids) == 1
    assert artifact.policy_ids == ("policy-fact-text",)


def test_custom_policy_records_round_trip_and_replay_exactly() -> None:
    """Persist data-only semantics so reload can reproduce every decision."""
    region = ObservationSourceRegion("region-policy-round-trip")
    observations = tuple(
        ParserObservation.create(
            parser_id=parser_id,
            parser_version="1",
            source_region=region,
            fact="classification",
            value=value,
        )
        for parser_id, value in (("docling", "approved"), ("pymupdf", "draft"))
    )
    observation_set = ParserObservationSet.create(observations)
    policy = FactAdjudicationPolicy(
        "policy-round-trip",
        fact="classification",
        strategy="prefer-explicit-value",
        preferred_values=(ObservationValue.from_value("approved"),),
        resolution_code="reviewed-value",
    )
    artifact = ParserFusionService().fuse(observation_set, policies=(policy,))
    reloaded = ParserFusionArtifact.from_json_bytes(artifact.to_json_bytes())
    assert reloaded.adjudication_policies == (policy,)
    reloaded.validate_against_observation_set(observation_set)
    assert reloaded.to_json_bytes() == artifact.to_json_bytes()


def test_policy_cannot_disable_observation_retention() -> None:
    """Reject a policy that asks T05 to physically discard parser evidence."""
    with pytest.raises(ParserAdjudicationError):
        FactAdjudicationPolicy(
            "policy-delete-evidence",
            fact="text",
            retain_all_observations=False,
        )


@pytest.mark.parametrize(
    ("preferred", "observed", "accepted_value"),
    (
        (
            ("approved", "fallback"),
            (("docling", "approved"), ("pymupdf", "draft")),
            "approved",
        ),
        (
            ("absent", "fallback"),
            (("docling", "fallback"), ("pymupdf", "draft")),
            "fallback",
        ),
    ),
)
def test_first_present_preferred_value_wins_in_policy_order(
    preferred: tuple[str, ...],
    observed: tuple[tuple[str, str], ...],
    accepted_value: str,
) -> None:
    """Choose the first listed value that actually occurs in observations."""
    observation_set = _priority_observation_set(observed)
    decision = ParserFusionService().fuse(
        observation_set, policies=(_priority_policy(*preferred),)
    ).fact_decisions[0]
    accepted = tuple(
        observation_set.get(item).value.to_value()
        for item in decision.accepted_observation_ids
    )
    assert accepted == (accepted_value,)
    assert decision.state == "conflict"


def test_both_preferred_values_present_accepts_only_first_priority() -> None:
    """Never accept contradictory preferred values in the same conflict decision."""
    observation_set = _priority_observation_set(
        (("docling", "first"), ("pymupdf", "second"), ("basic", "first"))
    )
    decision = ParserFusionService().fuse(
        observation_set, policies=(_priority_policy("first", "second"),)
    ).fact_decisions[0]
    accepted_values = {
        observation_set.get(item).value.to_value()
        for item in decision.accepted_observation_ids
    }
    rejected_values = {
        observation_set.get(item).value.to_value()
        for item in decision.rejected_observation_ids
    }
    assert accepted_values == {"first"}
    assert rejected_values == {"second"}
    assert len(decision.accepted_observation_ids) == 2
    assert decision.state == "conflict"


def test_absent_preferred_values_accept_none_and_retain_conflict() -> None:
    """Preserve unresolved preference when no reviewed value is observed."""
    observation_set = _priority_observation_set(
        (("docling", "draft"), ("pymupdf", "rejected"))
    )
    decision = ParserFusionService().fuse(
        observation_set, policies=(_priority_policy("approved", "fallback"),)
    ).fact_decisions[0]
    assert decision.state == "conflict"
    assert decision.accepted_observation_ids == ()
    assert decision.rejected_observation_ids == decision.observation_ids


def test_preferred_value_policy_rejects_empty_and_duplicate_priorities() -> None:
    """Require a nonempty canonical-hash-unique reviewed priority list."""
    with pytest.raises(ParserAdjudicationError, match="at least one"):
        _priority_policy()
    with pytest.raises(ParserAdjudicationError, match="unique canonical SHA-256"):
        _priority_policy("approved", "approved")


def test_nonpreference_strategy_rejects_unused_preferred_values() -> None:
    """Reject policy data that would otherwise have undefined silent effect."""
    with pytest.raises(ParserAdjudicationError, match="only"):
        FactAdjudicationPolicy(
            "policy-invalid-preference",
            fact="classification",
            strategy="preserve-conflict",
            preferred_values=(ObservationValue.from_value("approved"),),
        )


def test_policy_round_trip_preserves_priority_and_replays_decision() -> None:
    """Retain supplied priority order and reproduce accepted and rejected IDs."""
    observation_set = _priority_observation_set(
        (("docling", "first"), ("pymupdf", "second"))
    )
    policy = _priority_policy("second", "first")
    artifact = ParserFusionService().fuse(observation_set, policies=(policy,))
    reloaded = ParserFusionArtifact.from_json_bytes(artifact.to_json_bytes())
    assert tuple(
        item.to_value() for item in reloaded.adjudication_policies[0].preferred_values
    ) == ("second", "first")
    reloaded.validate_against_observation_set(observation_set)
    assert reloaded.fact_decisions[0].accepted_observation_ids == (
        artifact.fact_decisions[0].accepted_observation_ids
    )
    assert reloaded.fact_decisions[0].rejected_observation_ids == (
        artifact.fact_decisions[0].rejected_observation_ids
    )


def test_changing_preferred_order_changes_decision_and_fusion_identity() -> None:
    """Make reviewed tuple priority observable in decisions and stable identity."""
    observation_set = _priority_observation_set(
        (("docling", "first"), ("pymupdf", "second"))
    )
    first = ParserFusionService().fuse(
        observation_set, policies=(_priority_policy("first", "second"),)
    )
    second = ParserFusionService().fuse(
        observation_set, policies=(_priority_policy("second", "first"),)
    )
    assert first.fact_decisions[0].accepted_observation_ids != (
        second.fact_decisions[0].accepted_observation_ids
    )
    assert first.fusion_id != second.fusion_id
