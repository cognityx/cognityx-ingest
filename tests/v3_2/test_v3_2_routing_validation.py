"""Strict persisted-plan and routing-record trust-boundary tests for T04."""

from __future__ import annotations

import json

import pytest

from cognityx_ingest import (
    ParserRoutingCapabilityError,
    ParserRoutingRequest,
    ParserRoutingService,
    ParserRoutingValidationError,
    RoutingInputFacts,
    RoutingPlan,
)


def _fixture_payload(v3_2_fixture_root, name: str) -> dict[str, object]:
    """Return a fresh mutable routing fixture mapping for corruption tests."""
    return json.loads(
        (v3_2_fixture_root / "routing" / name).read_text(encoding="utf-8")
    )


def _json_bytes(value: dict[str, object]) -> bytes:
    """Encode supplied list order without silently sorting test mutations."""
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


@pytest.mark.parametrize(
    "name",
    (
        "deterministic_plan.json",
        "hybrid_plan.json",
        "llm_directed_plan.json",
    ),
)
def test_frozen_plan_shapes_round_trip_deterministically(
    v3_2_fixture_root, name: str
) -> None:
    """Preserve every authoritative mode-specific mapping without fixture edits."""
    payload = _fixture_payload(v3_2_fixture_root, name)
    plan = RoutingPlan.from_dict(payload)
    assert plan.to_dict() == payload
    encoded = plan.to_json_bytes()
    assert RoutingPlan.from_json_bytes(encoded).to_dict() == payload
    assert RoutingPlan.from_json_bytes(encoded).to_json_bytes() == encoded


@pytest.mark.parametrize(
    ("needle", "replacement", "key"),
    (
        (
            '"schema":"cognityx.ingest.routing-plan/v3.2"',
            '"schema":"cognityx.ingest.routing-plan/v3.2","schema":"wrong"',
            "schema",
        ),
        (
            '"parser_id":"docling"',
            '"parser_id":"docling","parser_id":"invented"',
            "parser_id",
        ),
        (
            '"max_parser_runs":2',
            '"max_parser_runs":2,"max_parser_runs":7',
            "max_parser_runs",
        ),
    ),
)
def test_duplicate_json_keys_fail_at_every_depth(
    v3_2_fixture_root, needle: str, replacement: str, key: str
) -> None:
    """Reject duplicate names before normal JSON last-key-wins conversion."""
    source = _json_bytes(
        _fixture_payload(v3_2_fixture_root, "hybrid_plan.json")
    ).decode("utf-8")
    if needle not in source:
        source = _json_bytes(
            _fixture_payload(v3_2_fixture_root, "llm_directed_plan.json")
        ).decode("utf-8")
    assert needle in source
    malformed = source.replace(needle, replacement, 1).encode("utf-8")
    with pytest.raises(
        ParserRoutingValidationError,
        match=rf"Duplicate routing JSON key: {key}",
    ):
        RoutingPlan.from_json_bytes(malformed)


def test_same_field_name_in_separate_invocation_objects_is_valid(
    v3_2_fixture_root,
) -> None:
    """Scope duplicate detection to one JSON object rather than the full plan."""
    path = v3_2_fixture_root / "routing" / "llm_directed_plan.json"
    assert RoutingPlan.from_json_bytes(path.read_bytes()).validation_result.accepted


@pytest.mark.parametrize("field", ("unknown", "source_text", "local_path"))
def test_unknown_or_payload_shaped_fields_fail(v3_2_fixture_root, field: str) -> None:
    """Reject unsupported fields so plans cannot smuggle source content or paths."""
    payload = _fixture_payload(v3_2_fixture_root, "deterministic_plan.json")
    payload[field] = "/tmp/source.pdf" if field == "local_path" else "source payload"
    with pytest.raises(ParserRoutingValidationError, match="fields"):
        RoutingPlan.from_dict(payload)


def test_missing_required_plan_field_fails(v3_2_fixture_root) -> None:
    """Reject incomplete persisted plans rather than inventing selected parsers."""
    payload = _fixture_payload(v3_2_fixture_root, "hybrid_plan.json")
    payload.pop("deterministic_boundary")
    with pytest.raises(ParserRoutingValidationError, match="fields"):
        RoutingPlan.from_dict(payload)


@pytest.mark.parametrize(
    "mutation",
    (
        "requirements",
        "purpose",
        "deterministic_invocations",
        "hybrid_allowlist",
        "hybrid_invocations",
    ),
)
def test_noncanonical_contractual_collection_order_fails(
    v3_2_fixture_root, mutation: str
) -> None:
    """Preserve and reject reversed persisted lists instead of normalizing them."""
    if mutation.startswith("hybrid"):
        payload = _fixture_payload(v3_2_fixture_root, "hybrid_plan.json")
        if mutation == "hybrid_allowlist":
            payload["deterministic_boundary"]["allowlist"].reverse()
        else:
            payload["frozen_llm_proposal"]["selected"].reverse()
    else:
        payload = _fixture_payload(v3_2_fixture_root, "deterministic_plan.json")
        if mutation == "requirements":
            payload["input_facts"]["requires"].reverse()
        elif mutation == "purpose":
            payload["selected_invocations"][1]["purpose"].reverse()
        else:
            payload["selected_invocations"].reverse()
    with pytest.raises(ParserRoutingValidationError, match="ordered"):
        RoutingPlan.from_dict(payload)


@pytest.mark.parametrize("value", (float("nan"), float("inf"), -0.1, 1.1))
def test_invalid_native_text_ratios_fail(
    value: float, available_routing_registry, routing_boundary
) -> None:
    """Reject non-finite and out-of-range input observations."""
    facts = RoutingInputFacts(
        media_type="application/pdf",
        native_text_ratio=value,
        required_capabilities=("hierarchy",),
    )
    request = ParserRoutingRequest(
        mode="deterministic",
        input_facts=facts,
        boundary=routing_boundary,
        registry=available_routing_registry,
    )
    with pytest.raises(ParserRoutingValidationError, match="native_text_ratio"):
        ParserRoutingService().plan(request)


def test_unknown_requirement_uses_typed_capability_error(
    available_routing_registry, routing_boundary
) -> None:
    """Reject invented routing vocabulary rather than parsing requirement prose."""
    facts = RoutingInputFacts(
        media_type="application/pdf",
        required_capabilities=("model-memory-says-yes",),
    )
    request = ParserRoutingRequest(
        mode="deterministic",
        input_facts=facts,
        boundary=routing_boundary,
        registry=available_routing_registry,
    )
    with pytest.raises(ParserRoutingCapabilityError, match="Unsupported routing"):
        ParserRoutingService().plan(request)


def test_local_path_document_class_fails_at_persisted_boundary(
    v3_2_fixture_root,
) -> None:
    """Reject local paths from the bounded input-fact audit field."""
    payload = _fixture_payload(v3_2_fixture_root, "deterministic_plan.json")
    payload["input_facts"]["document_class"] = "/tmp/private/source.pdf"
    with pytest.raises(ParserRoutingValidationError, match="forbidden content"):
        RoutingPlan.from_dict(payload)


def test_malformed_parser_id_and_scope_fail(v3_2_fixture_root) -> None:
    """Reject invented parser syntax and unsupported page-scope names."""
    for field, value in (("parser_id", "Bad Parser"), ("scope", "selected-pages")):
        payload = _fixture_payload(v3_2_fixture_root, "llm_directed_plan.json")
        payload["frozen_llm_proposal"]["invocations"][0][field] = value
        with pytest.raises(ParserRoutingValidationError):
            RoutingPlan.from_dict(payload)
