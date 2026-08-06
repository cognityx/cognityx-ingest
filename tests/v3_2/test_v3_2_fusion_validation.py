"""Strict untrusted-input and cross-reference validation tests for T05."""

from __future__ import annotations

import json
import math
from copy import deepcopy

import pytest

from cognityx_ingest import (
    AlignmentEvidence,
    FactFusionDecision,
    ObservationSourceRegion,
    ObservationValue,
    ParserAlignmentError,
    ParserFusionArtifact,
    ParserFusionService,
    ParserFusionValidationError,
    ParserObservation,
    ParserObservationSet,
    ParserObservationValidationError,
)


def test_malformed_observation_identity_hash_and_numbers_raise_typed_errors() -> None:
    """Reject invalid parser IDs, value hashes, confidence, spans, and geometry."""
    region = ObservationSourceRegion("region-valid")
    value = ObservationValue.from_value("secret source value")
    with pytest.raises(ParserObservationValidationError):
        ParserObservation(
            observation_id="obs-valid",
            parser_id="INVALID PARSER",
            parser_version=None,
            source_region=region,
            fact="text",
            value=value,
            value_sha256=value.sha256,
        )
    with pytest.raises(ParserObservationValidationError) as error:
        ParserObservation(
            observation_id="obs-valid",
            parser_id="docling",
            parser_version=None,
            source_region=region,
            fact="text",
            value=value,
            value_sha256="0" * 64,
        )
    assert "secret source value" not in str(error.value)
    with pytest.raises(ParserObservationValidationError):
        ParserObservation.create(
            parser_id="docling",
            parser_version=None,
            source_region=region,
            fact="text",
            value="value",
            confidence=math.inf,
        )


def test_duplicate_observation_identity_and_nondeterministic_order_fail() -> None:
    """Reject duplicate parser/fact/occurrence identity and direct unsorted sets."""
    region = ObservationSourceRegion("region-duplicate")
    first = ParserObservation.create(
        parser_id="docling",
        parser_version=None,
        source_region=region,
        fact="text",
        value="A",
    )
    second = ParserObservation.create(
        parser_id="pymupdf",
        parser_version=None,
        source_region=region,
        fact="text",
        value="A",
    )
    with pytest.raises(ParserObservationValidationError):
        ParserObservationSet(
            schema="cognityx.ingest.parser-observation-set/v3.2",
            observation_set_id="obset-direct",
            source_document_id=None,
            parser_ids=("pymupdf", "docling"),
            observations=(second, first),
        )
    with pytest.raises(ParserObservationValidationError):
        ParserObservationSet.create((first, first))


def test_alignment_score_and_endpoint_order_raise_typed_errors() -> None:
    """Reject non-finite scores and unordered observation endpoints directly."""
    with pytest.raises(ParserAlignmentError):
        AlignmentEvidence(
            alignment_id="align-invalid",
            left_observation_id="obs-b",
            right_observation_id="obs-a",
            alignment_method="bbox-iou",
            alignment_score=0.9,
        )
    with pytest.raises(ParserAlignmentError):
        AlignmentEvidence(
            alignment_id="align-invalid",
            left_observation_id="obs-a",
            right_observation_id="obs-b",
            alignment_method="bbox-iou",
            alignment_score=math.nan,
        )


def test_fusion_artifact_strict_reader_rejects_duplicate_and_unknown_fields(
    fusion_cases, build_fusion_observation_set
) -> None:
    """Reject duplicate keys and schema extensions before reference validation."""
    case = fusion_cases[0]
    artifact = ParserFusionService().fuse(build_fusion_observation_set(case))
    with pytest.raises(ParserFusionValidationError):
        ParserFusionArtifact.from_json_bytes(
            artifact.to_json_bytes()[:-1] + b',"schema":"duplicate"}'
        )
    value = artifact.to_dict()
    value["unsupported"] = True
    with pytest.raises(ParserFusionValidationError):
        ParserFusionArtifact.from_json_bytes(json.dumps(value).encode())


def test_artifact_rejects_missing_policy_and_wrong_state_counts(
    fusion_cases, build_fusion_observation_set
) -> None:
    """Validate decision-policy references and exact aggregate state accounting."""
    case = next(item for item in fusion_cases if item["case_id"] == "table-versus-text")
    artifact = ParserFusionService().fuse(build_fusion_observation_set(case))
    missing_policy = artifact.to_dict()
    missing_policy["policy_ids"] = []
    with pytest.raises(ParserFusionValidationError):
        ParserFusionArtifact.from_json_bytes(json.dumps(missing_policy).encode())
    wrong_counts = artifact.to_dict()
    wrong_counts["state_counts"]["conflict"] = 0
    with pytest.raises(ParserFusionValidationError):
        ParserFusionArtifact.from_json_bytes(json.dumps(wrong_counts).encode())


def test_agreement_requires_two_equivalent_observations() -> None:
    """Prevent direct records from using agreement as a single-observation alias."""
    with pytest.raises(ParserFusionValidationError):
        FactFusionDecision(
            decision_id="decision-invalid-agreement",
            source_region_id="region-agreement",
            fact="text",
            state="agreement",
            observation_ids=("obs-a",),
            accepted_observation_ids=("obs-a",),
            rejected_observation_ids=(),
            resolution="exact",
        )


def test_fusion_artifact_references_values_in_observation_set_not_decisions(
    fusion_cases, build_fusion_observation_set
) -> None:
    """Keep exact source text in observations and out of the decision artifact."""
    case = next(item for item in fusion_cases if item["case_id"] == "agreement-text")
    observation_set = build_fusion_observation_set(case)
    artifact = ParserFusionService().fuse(observation_set)
    exact_text = case["expected"]["accepted_value"].encode("utf-8")
    assert exact_text in observation_set.to_json_bytes()
    assert exact_text not in artifact.to_json_bytes()


def test_every_stable_record_id_rejects_arbitrary_replacement(
    fusion_cases, build_fusion_observation_set
) -> None:
    """Recompute identities for observations, sets, edges, groups, and decisions."""
    observation_set = build_fusion_observation_set(fusion_cases[0])
    artifact = ParserFusionService().fuse(observation_set)

    observation = observation_set.observations[0].to_dict()
    observation["observation_id"] = "obs-arbitrary-replacement"
    with pytest.raises(ParserObservationValidationError):
        ParserObservation.from_dict(observation)

    set_value = observation_set.to_dict()
    set_value["observation_set_id"] = "obset-arbitrary-replacement"
    with pytest.raises(ParserObservationValidationError):
        ParserObservationSet.from_dict(set_value)

    for collection, id_field, replacement in (
        ("alignment_evidence", "alignment_id", "align-arbitrary-replacement"),
        ("aligned_groups", "alignment_group_id", "group-arbitrary-replacement"),
        ("fact_decisions", "decision_id", "decision-arbitrary-replacement"),
        (
            "region_decisions",
            "region_decision_id",
            "region-decision-arbitrary-replacement",
        ),
    ):
        value = deepcopy(artifact.to_dict())
        assert value[collection]
        value[collection][0][id_field] = replacement
        with pytest.raises((ParserFusionValidationError, ParserAlignmentError)):
            ParserFusionArtifact.from_json_bytes(json.dumps(value).encode())

    value = artifact.to_dict()
    value["fusion_id"] = "fusion-arbitrary-replacement"
    with pytest.raises(ParserFusionValidationError):
        ParserFusionArtifact.from_json_bytes(json.dumps(value).encode())


def test_same_observation_set_id_with_changed_bytes_fails_sha_binding(
    fusion_cases, build_fusion_observation_set
) -> None:
    """Bind fusion to exact bytes even when non-identity evidence changes."""
    observation_set = build_fusion_observation_set(fusion_cases[0])
    artifact = ParserFusionService().fuse(observation_set)
    value = observation_set.to_dict()
    value["observations"][0]["confidence"] = 0.125
    changed = ParserObservationSet.from_json_bytes(json.dumps(value).encode())
    assert changed.observation_set_id == observation_set.observation_set_id
    assert changed.to_json_bytes() != observation_set.to_json_bytes()
    with pytest.raises(ParserFusionValidationError, match="SHA-256"):
        artifact.validate_against_observation_set(changed)


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("strategy", "require-review"),
        ("preferred_values", ["unreviewed-value"]),
        ("resolution_code", "altered-resolution"),
    ),
)
def test_altered_persisted_policy_semantics_fail_integrity_validation(
    field: str,
    replacement: object,
    fusion_cases,
    build_fusion_observation_set,
) -> None:
    """Reject changed policy behavior rather than trusting a retained policy ID."""
    case = next(item for item in fusion_cases if item["case_id"] == "table-versus-text")
    artifact = ParserFusionService().fuse(build_fusion_observation_set(case))
    value = deepcopy(artifact.to_dict())
    value["adjudication_policies"][0][field] = replacement
    with pytest.raises(ParserFusionValidationError):
        ParserFusionArtifact.from_json_bytes(json.dumps(value).encode())
