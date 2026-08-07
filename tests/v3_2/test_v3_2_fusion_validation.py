"""Strict untrusted-input and cross-reference validation tests for T05."""

from __future__ import annotations

import json
import math
from copy import deepcopy

import pytest

from cognityx_ingest import (
    AlignmentEvidence,
    FactAdjudicationPolicy,
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
from cognityx_ingest.parser_fusion import _parser_fusion_id


def _artifact_with_processing_activity(
    artifact: ParserFusionArtifact,
    processing_activity: tuple[tuple[str, str], ...],
) -> ParserFusionArtifact:
    """Rebuild a valid fusion identity around one test processing activity.

    Integrity tests use this helper to prove that recomputing the aggregate ID
    cannot hide a semantic mismatch with the observation set. It preserves every
    other public record, invokes the production fusion-ID algorithm, and returns
    a normally validated immutable artifact without bypassing constructors.
    """
    fusion_id = _parser_fusion_id(
        artifact.observation_set_id,
        artifact.observation_set_sha256,
        artifact.source_backends,
        artifact.backend_versions,
        artifact.alignment_evidence,
        artifact.aligned_groups,
        artifact.fact_decisions,
        artifact.region_decisions,
        artifact.adjudication_policies,
        processing_activity,
        artifact.state_counts,
    )
    return ParserFusionArtifact(
        schema=artifact.schema,
        fusion_id=fusion_id,
        observation_set_id=artifact.observation_set_id,
        observation_set_sha256=artifact.observation_set_sha256,
        source_backends=artifact.source_backends,
        backend_versions=artifact.backend_versions,
        alignment_evidence=artifact.alignment_evidence,
        aligned_groups=artifact.aligned_groups,
        fact_decisions=artifact.fact_decisions,
        region_decisions=artifact.region_decisions,
        adjudication_policies=artifact.adjudication_policies,
        policy_ids=artifact.policy_ids,
        processing_activity=processing_activity,
        state_counts=artifact.state_counts,
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


def test_artifact_rejects_unused_retained_policy_with_recomputed_fusion_id(
    fusion_cases, build_fusion_observation_set
) -> None:
    """Close policy ownership even when tampered aggregate identity is consistent."""
    case = next(item for item in fusion_cases if item["case_id"] == "table-versus-text")
    artifact = ParserFusionService().fuse(build_fusion_observation_set(case))
    unused = FactAdjudicationPolicy(
        "policy-unused",
        fact="unused_fact",
        strategy="exact-agreement",
        resolution_code="unused-policy-must-fail",
    )
    policies = tuple(sorted(
        (*artifact.adjudication_policies, unused),
        key=lambda item: item.policy_id,
    ))
    policy_ids = tuple(item.policy_id for item in policies)
    fusion_id = _parser_fusion_id(
        artifact.observation_set_id,
        artifact.observation_set_sha256,
        artifact.source_backends,
        artifact.backend_versions,
        artifact.alignment_evidence,
        artifact.aligned_groups,
        artifact.fact_decisions,
        artifact.region_decisions,
        policies,
        artifact.processing_activity,
        artifact.state_counts,
    )
    with pytest.raises(ParserFusionValidationError, match="retained and used"):
        ParserFusionArtifact(
            schema=artifact.schema,
            fusion_id=fusion_id,
            observation_set_id=artifact.observation_set_id,
            observation_set_sha256=artifact.observation_set_sha256,
            source_backends=artifact.source_backends,
            backend_versions=artifact.backend_versions,
            alignment_evidence=artifact.alignment_evidence,
            aligned_groups=artifact.aligned_groups,
            fact_decisions=artifact.fact_decisions,
            region_decisions=artifact.region_decisions,
            adjudication_policies=policies,
            policy_ids=policy_ids,
            processing_activity=artifact.processing_activity,
            state_counts=artifact.state_counts,
        )


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


def test_matching_processing_activity_id_passes_cross_validation(
    fusion_cases, build_fusion_observation_set
) -> None:
    """Cross-bind the fusion activity to an explicit observation-set activity."""
    original = build_fusion_observation_set(fusion_cases[0])
    observation_set = ParserObservationSet.create(
        original.observations,
        source_document_id=original.source_document_id,
        processing_activity_id="activity-parser-execution-42",
    )
    artifact = ParserFusionService().fuse(observation_set)
    assert dict(artifact.processing_activity)["activity_id"] == (
        "activity-parser-execution-42"
    )
    artifact.validate_against_observation_set(observation_set)


def test_changed_processing_activity_id_fails_even_with_recomputed_fusion_id(
    fusion_cases, build_fusion_observation_set
) -> None:
    """Reject another activity after making its aggregate fusion ID self-consistent."""
    original = build_fusion_observation_set(fusion_cases[0])
    observation_set = ParserObservationSet.create(
        original.observations,
        processing_activity_id="activity-parser-execution-42",
    )
    artifact = ParserFusionService().fuse(observation_set)
    changed = _artifact_with_processing_activity(
        artifact,
        (
            ("activity_id", "activity-another-execution"),
            ("bbox_iou_threshold", "0.5"),
            ("method", "deterministic-parser-fusion"),
        ),
    )
    assert changed.fusion_id != artifact.fusion_id
    with pytest.raises(ParserFusionValidationError, match="observation set"):
        changed.validate_against_observation_set(observation_set)


@pytest.mark.parametrize("field", ("activity_id", "method"))
def test_processing_activity_rejects_missing_required_field(
    field: str, fusion_cases, build_fusion_observation_set
) -> None:
    """Require every field in the exact bounded processing-activity shape."""
    artifact = ParserFusionService().fuse(
        build_fusion_observation_set(fusion_cases[0])
    )
    value = artifact.to_dict()
    del value["processing_activity"][field]
    with pytest.raises(ParserFusionValidationError, match="exactly"):
        ParserFusionArtifact.from_json_bytes(json.dumps(value).encode())


def test_processing_activity_rejects_additional_field(
    fusion_cases, build_fusion_observation_set
) -> None:
    """Prevent undeclared processing semantics from entering fusion identity."""
    artifact = ParserFusionService().fuse(
        build_fusion_observation_set(fusion_cases[0])
    )
    value = artifact.to_dict()
    value["processing_activity"]["provider"] = "untrusted-extra"
    with pytest.raises(ParserFusionValidationError, match="exactly"):
        ParserFusionArtifact.from_json_bytes(json.dumps(value).encode())


def test_processing_activity_rejects_changed_method(
    fusion_cases, build_fusion_observation_set
) -> None:
    """Allow only deterministic parser fusion at the T05 processing seam."""
    artifact = ParserFusionService().fuse(
        build_fusion_observation_set(fusion_cases[0])
    )
    value = artifact.to_dict()
    value["processing_activity"]["method"] = "llm-adjudication"
    with pytest.raises(ParserFusionValidationError, match="method"):
        ParserFusionArtifact.from_json_bytes(json.dumps(value).encode())


def test_processing_activity_rejects_noncanonical_threshold_text(
    fusion_cases, build_fusion_observation_set
) -> None:
    """Require one exact string representation for the replayed IoU threshold."""
    artifact = ParserFusionService().fuse(
        build_fusion_observation_set(fusion_cases[0])
    )
    value = artifact.to_dict()
    value["processing_activity"]["bbox_iou_threshold"] = "0.500000"
    with pytest.raises(ParserFusionValidationError, match="canonical"):
        ParserFusionArtifact.from_json_bytes(json.dumps(value).encode())


def test_changed_threshold_fails_exact_alignment_replay() -> None:
    """Reject a canonical threshold whose replay changes alignment and groups."""
    observations = tuple(
        ParserObservation.create(
            parser_id=parser_id,
            parser_version=None,
            source_region=ObservationSourceRegion(
                f"block:{parser_id}:0:a",
                region_kind="block",
                physical_page_index=0,
                bbox=bbox,
            ),
            fact="text",
            value="same",
        )
        for parser_id, bbox in (
            ("docling", (0.0, 0.0, 10.0, 10.0)),
            ("pymupdf", (0.0, 0.0, 7.5, 10.0)),
        )
    )
    observation_set = ParserObservationSet.create(observations)
    artifact = ParserFusionService(bbox_iou_threshold=0.5).fuse(observation_set)
    changed = _artifact_with_processing_activity(
        artifact,
        (
            ("activity_id", "activity-parser-fusion"),
            ("bbox_iou_threshold", "0.80000000000000004"),
            ("method", "deterministic-parser-fusion"),
        ),
    )
    with pytest.raises(ParserFusionValidationError, match="replay"):
        changed.validate_against_observation_set(observation_set)


def test_missing_observation_activity_uses_documented_fallback(
    fusion_cases, build_fusion_observation_set
) -> None:
    """Bind activity-parser-fusion only when no observation activity ID exists."""
    observation_set = build_fusion_observation_set(fusion_cases[0])
    assert observation_set.processing_activity_id is None
    artifact = ParserFusionService().fuse(observation_set)
    assert dict(artifact.processing_activity) == {
        "activity_id": "activity-parser-fusion",
        "bbox_iou_threshold": "0.80000000000000004",
        "method": "deterministic-parser-fusion",
    }
    artifact.validate_against_observation_set(observation_set)
