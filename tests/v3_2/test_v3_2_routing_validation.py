"""Strict persisted-plan and routing-record trust-boundary tests for T04."""

from __future__ import annotations

from dataclasses import replace
import json

import pytest

from cognityx_ingest import (
    ParserInvocation,
    ParserRoutingCapabilityError,
    ParserRoutingRequest,
    ParserRoutingService,
    ParserRoutingValidationError,
    RoutingInputFacts,
    RoutingPlan,
    RoutingProposal,
    RoutingProviderProfile,
)


def _fixture_payload(v3_2_fixture_root, name: str) -> dict[str, object]:
    """Return a fresh mutable routing fixture mapping for corruption tests."""
    return json.loads(
        (v3_2_fixture_root / "routing" / name).read_text(encoding="utf-8")
    )


def _json_bytes(value: dict[str, object]) -> bytes:
    """Encode supplied list order without silently sorting test mutations."""
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


class _VerificationProvider:
    """Return one valid proposal while exposing calls made during plan creation."""

    def __init__(self, proposal: RoutingProposal) -> None:
        """Store immutable proposal evidence and initialize a zero call count."""
        self.proposal = proposal
        self.calls = 0

    def propose(self, request, registry, boundary) -> RoutingProposal:
        """Return the configured proposal and increment the observable call count."""
        self.calls += 1
        return self.proposal


def _canonical_plan(
    mode: str,
    registry,
    boundary,
    profile: RoutingProviderProfile,
) -> tuple[RoutingPlan, _VerificationProvider | None]:
    """Build one accepted canonical plan for registry-binding corruption tests."""
    request = ParserRoutingRequest(
        mode=mode,
        input_facts=RoutingInputFacts(
            media_type="application/pdf",
            native_text_ratio=0.98,
            required_capabilities=("hierarchy", "native_links"),
        ),
        boundary=boundary,
        registry=registry,
    )
    if mode == "deterministic":
        return ParserRoutingService().plan(request), None
    proposal = RoutingProposal(
        invocations=(
            ParserInvocation(
                parser_id="docling",
                scope="document",
                purpose=("hierarchy",),
            ),
            ParserInvocation(
                parser_id="pymupdf",
                scope="document",
                purpose=("native_links",),
            ),
        )
    )
    provider = _VerificationProvider(proposal)
    return (
        ParserRoutingService().plan(
            request,
            proposal_provider=provider,
            provider_profile=profile,
        ),
        provider,
    )


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


def test_rejected_typed_plan_cannot_retain_selected_invocations(
    available_routing_registry, routing_boundary
) -> None:
    """Enforce the empty-selection invariant even for direct record construction."""
    request = ParserRoutingRequest(
        mode="deterministic",
        input_facts=RoutingInputFacts(
            media_type="application/pdf",
            required_capabilities=("native_links",),
        ),
        boundary=routing_boundary,
        registry=available_routing_registry,
    )
    accepted = ParserRoutingService().plan(request)
    rejected_result = replace(
        accepted.validation_result,
        accepted=False,
        budget_valid=False,
        rejection_reasons=("parser-run-budget-exceeded",),
    )
    malformed = replace(accepted, validation_result=rejected_result)
    with pytest.raises(
        ParserRoutingValidationError,
        match="Rejected routing plan cannot contain selected invocations",
    ):
        malformed.validate()


@pytest.mark.parametrize(
    "field",
    (
        "candidate_invocations",
        "deterministic_boundary",
        "registry_sha256",
        "registry_version",
        "validation_result",
    ),
)
def test_partially_extended_canonical_plan_fails(
    field: str, available_routing_registry, routing_boundary
) -> None:
    """Accept exact compact or complete canonical records, never partial context."""
    request = ParserRoutingRequest(
        mode="deterministic",
        input_facts=RoutingInputFacts(
            media_type="application/pdf",
            required_capabilities=("native_links",),
        ),
        boundary=routing_boundary,
        registry=available_routing_registry,
    )
    payload = ParserRoutingService().plan(request).to_dict()
    payload.pop(field)
    with pytest.raises(ParserRoutingValidationError, match="exact compact or canonical"):
        RoutingPlan.from_dict(payload)


@pytest.mark.parametrize("digest", ("A" * 64, "0" * 63, "not-a-digest"))
def test_canonical_registry_digest_requires_lowercase_sha256(
    digest: str, available_routing_registry, routing_boundary
) -> None:
    """Reject malformed evidence bindings without normalizing persisted values."""
    request = ParserRoutingRequest(
        mode="deterministic",
        input_facts=RoutingInputFacts(
            media_type="application/pdf",
            required_capabilities=("native_links",),
        ),
        boundary=routing_boundary,
        registry=available_routing_registry,
    )
    payload = ParserRoutingService().plan(request).to_dict()
    payload["registry_sha256"] = digest
    with pytest.raises(ParserRoutingValidationError, match="lowercase SHA-256"):
        RoutingPlan.from_dict(payload)


@pytest.mark.parametrize("mode", ("deterministic", "hybrid", "llm-directed"))
def test_canonical_plan_verifies_against_original_registry(
    mode: str,
    available_routing_registry,
    routing_boundary,
    routing_provider_profile,
) -> None:
    """Recompute exact validation and selections for every canonical T04 mode."""
    plan, _ = _canonical_plan(
        mode,
        available_routing_registry,
        routing_boundary,
        routing_provider_profile,
    )
    assert plan.validate_against_registry(available_routing_registry) is plan
    assert plan.require_executable(available_routing_registry) is plan


def test_same_registry_version_with_modified_bytes_fails_digest_binding(
    available_routing_registry, routing_boundary, routing_provider_profile
) -> None:
    """Use exact registry bytes rather than trusting a repeated readable version."""
    plan, _ = _canonical_plan(
        "deterministic",
        available_routing_registry,
        routing_boundary,
        routing_provider_profile,
    )
    first = available_routing_registry.parsers[0]
    modified = replace(
        available_routing_registry,
        parsers=(replace(first, version_scope="modified-same-version"),)
        + available_routing_registry.parsers[1:],
    )
    modified.validate()
    assert modified.registry_version == available_routing_registry.registry_version
    with pytest.raises(ParserRoutingValidationError, match="SHA-256"):
        plan.validate_against_registry(modified)


def test_registry_version_mismatch_fails_before_digest_acceptance(
    available_routing_registry, routing_boundary, routing_provider_profile
) -> None:
    """Require the readable registry release identity as well as exact bytes."""
    plan, _ = _canonical_plan(
        "deterministic",
        available_routing_registry,
        routing_boundary,
        routing_provider_profile,
    )
    modified = replace(
        available_routing_registry,
        registry_version="routing-test-v2",
    )
    modified.validate()
    with pytest.raises(ParserRoutingValidationError, match="version"):
        plan.validate_against_registry(modified)


def test_altered_registry_digest_fails_binding_verification(
    available_routing_registry, routing_boundary, routing_provider_profile
) -> None:
    """Reject a syntactically valid digest that does not identify supplied evidence."""
    plan, _ = _canonical_plan(
        "deterministic",
        available_routing_registry,
        routing_boundary,
        routing_provider_profile,
    )
    altered = replace(plan, registry_sha256="0" * 64)
    with pytest.raises(ParserRoutingValidationError, match="SHA-256"):
        altered.validate_against_registry(available_routing_registry)


def test_altered_persisted_validation_flags_fail_recomputation(
    available_routing_registry, routing_boundary, routing_provider_profile
) -> None:
    """Reject internally consistent flags that disagree with live registry evidence."""
    plan, _ = _canonical_plan(
        "deterministic",
        available_routing_registry,
        routing_boundary,
        routing_provider_profile,
    )
    altered_result = replace(
        plan.validation_result,
        accepted=False,
        budget_valid=False,
        rejection_reasons=("parser-run-budget-exceeded",),
    )
    altered = replace(
        plan,
        selected_invocations=(),
        validation_result=altered_result,
    )
    with pytest.raises(ParserRoutingValidationError, match="validation result"):
        altered.validate_against_registry(available_routing_registry)


def test_altered_selection_and_candidate_records_fail_verification(
    available_routing_registry, routing_boundary, routing_provider_profile
) -> None:
    """Reject selected or candidate work that differs from registry-backed output."""
    plan, _ = _canonical_plan(
        "deterministic",
        available_routing_registry,
        routing_boundary,
        routing_provider_profile,
    )
    with pytest.raises(ParserRoutingValidationError):
        replace(plan, selected_invocations=()).validate_against_registry(
            available_routing_registry
        )
    with pytest.raises(ParserRoutingValidationError):
        replace(
            plan,
            candidate_invocations=plan.candidate_invocations[:-1],
            selected_invocations=plan.selected_invocations[:-1],
        ).validate_against_registry(available_routing_registry)


def test_altered_proposal_purpose_fails_registry_recomputation(
    available_routing_registry, routing_boundary, routing_provider_profile
) -> None:
    """Reject a persisted proposal whose parser-specific purpose was rewritten."""
    plan, _ = _canonical_plan(
        "hybrid",
        available_routing_registry,
        routing_boundary,
        routing_provider_profile,
    )
    assert plan.proposal is not None
    altered_invocations = (
        replace(plan.proposal.invocations[0], purpose=("native_links",)),
        plan.proposal.invocations[1],
    )
    altered_proposal = replace(plan.proposal, invocations=altered_invocations)
    altered = replace(
        plan,
        proposal=altered_proposal,
        selected_invocations=altered_invocations,
    )
    with pytest.raises(ParserRoutingValidationError, match="validation result"):
        altered.validate_against_registry(available_routing_registry)


@pytest.mark.parametrize("mutation", ("input_facts", "boundary"))
def test_altered_persisted_context_fails_registry_recomputation(
    mutation: str,
    available_routing_registry,
    routing_boundary,
    routing_provider_profile,
) -> None:
    """Reject changed requirements or budget even when record shape remains valid."""
    plan, _ = _canonical_plan(
        "hybrid",
        available_routing_registry,
        routing_boundary,
        routing_provider_profile,
    )
    if mutation == "input_facts":
        altered = replace(
            plan,
            input_facts=replace(
                plan.input_facts,
                required_capabilities=("hierarchy", "tables", "native_links"),
            ),
        )
    else:
        altered = replace(plan, boundary=replace(plan.boundary, max_parser_runs=1))
    with pytest.raises(ParserRoutingValidationError, match="validation result"):
        altered.validate_against_registry(available_routing_registry)


def test_registry_verification_calls_no_provider_parser_or_network(
    monkeypatch,
    available_routing_registry,
    routing_boundary,
    routing_provider_profile,
) -> None:
    """Keep execution authorization deterministic, local, and side-effect free."""
    import socket

    import cognityx_ingest.parser as parser_module

    plan, provider = _canonical_plan(
        "hybrid",
        available_routing_registry,
        routing_boundary,
        routing_provider_profile,
    )
    assert provider is not None and provider.calls == 1

    def fail(*args, **kwargs):
        """Raise if registry verification crosses an execution or network seam."""
        raise AssertionError("registry verification must be read-only")

    monkeypatch.setattr(socket, "socket", fail)
    monkeypatch.setattr(parser_module.ParserRouter, "extract_document", fail)
    monkeypatch.setattr(parser_module, "_fuse_results", fail)
    assert plan.validate_against_registry(available_routing_registry) is plan
    assert provider.calls == 1


@pytest.mark.parametrize(
    "name",
    (
        "deterministic_plan.json",
        "hybrid_plan.json",
        "llm_directed_plan.json",
    ),
)
def test_compact_fixture_is_readable_but_not_executable(
    name: str, v3_2_fixture_root, available_routing_registry
) -> None:
    """Keep frozen compatibility artifacts readable without granting authority."""
    plan = RoutingPlan.from_json_bytes(
        (v3_2_fixture_root / "routing" / name).read_bytes()
    )
    with pytest.raises(ParserRoutingValidationError, match="audit-readable"):
        plan.require_executable(available_routing_registry)


def test_direct_compact_records_reject_canonical_only_fields(
    v3_2_fixture_root, routing_provider_profile
) -> None:
    """Apply exact compact shape rules to immutable records built with replace."""
    deterministic = RoutingPlan.from_json_bytes(
        (v3_2_fixture_root / "routing" / "deterministic_plan.json").read_bytes()
    )
    with pytest.raises(ParserRoutingValidationError, match="canonical-only"):
        replace(deterministic, registry_sha256="0" * 64).validate()
    with pytest.raises(ParserRoutingValidationError, match="canonical-only"):
        replace(
            deterministic,
            candidate_invocations=deterministic.selected_invocations,
        ).validate()

    hybrid = RoutingPlan.from_json_bytes(
        (v3_2_fixture_root / "routing" / "hybrid_plan.json").read_bytes()
    )
    with pytest.raises(ParserRoutingValidationError):
        replace(
            hybrid,
            input_facts=deterministic.input_facts,
            provider_profile=routing_provider_profile,
        ).validate()


def test_direct_canonical_proposal_plan_requires_provider_profile(
    available_routing_registry, routing_boundary, routing_provider_profile
) -> None:
    """Prevent trusted security context from disappearing after direct replacement."""
    plan, _ = _canonical_plan(
        "hybrid",
        available_routing_registry,
        routing_boundary,
        routing_provider_profile,
    )
    with pytest.raises(ParserRoutingValidationError, match="exact validation context"):
        replace(plan, provider_profile=None).validate()


def test_direct_canonical_deterministic_plan_rejects_provider_profile(
    available_routing_registry,
    routing_boundary,
    routing_provider_profile,
) -> None:
    """Keep provider trust context exclusive to proposal-backed canonical modes."""
    plan, _ = _canonical_plan(
        "deterministic",
        available_routing_registry,
        routing_boundary,
        routing_provider_profile,
    )
    with pytest.raises(ParserRoutingValidationError, match="exact validation context"):
        replace(plan, provider_profile=routing_provider_profile).validate()


def test_direct_canonical_security_flag_must_match_trusted_profile(
    available_routing_registry, routing_boundary, routing_provider_profile
) -> None:
    """Reject internal security claims contradicted by persisted composition facts."""
    plan, _ = _canonical_plan(
        "hybrid",
        available_routing_registry,
        routing_boundary,
        routing_provider_profile,
    )
    unsafe_profile = replace(
        routing_provider_profile,
        uses_external_services=True,
    )
    with pytest.raises(ParserRoutingValidationError, match="security result"):
        replace(plan, provider_profile=unsafe_profile).validate()


def test_canonical_serialization_emits_every_trusted_profile_field(
    available_routing_registry, routing_boundary
) -> None:
    """Retain all validated provider facts rather than silently dropping metadata."""
    profile = RoutingProviderProfile(
        provider_id="complete-profile",
        uses_external_services=False,
        security_tags=("internal",),
        provider_kind="local-model",
        deployment_id="deployment-7",
    )
    boundary = replace(routing_boundary, required_security_tags=("internal",))
    plan, _ = _canonical_plan(
        "hybrid",
        available_routing_registry,
        boundary,
        profile,
    )
    payload = plan.to_dict()
    assert payload["provider_profile"] == {
        "provider_id": "complete-profile",
        "uses_external_services": False,
        "security_tags": ["internal"],
        "provider_kind": "local-model",
        "deployment_id": "deployment-7",
    }
    assert RoutingPlan.from_dict(payload) == plan
