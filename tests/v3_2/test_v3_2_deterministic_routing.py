"""Deterministic T04 rule and live-capability eligibility tests."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import socket

import pytest

from cognityx_ingest import (
    ParserRoutingRequest,
    ParserRoutingRejectedError,
    ParserRoutingService,
    RoutingBoundary,
    RoutingInputFacts,
    RoutingPlan,
)


class _FailIfCalledProvider:
    """Raise on proposal use so deterministic tests prove the provider is ignored."""

    def __init__(self) -> None:
        """Initialize the observable provider-call counter."""
        self.calls = 0

    def propose(self, request, registry, boundary):
        """Fail immediately if deterministic mode crosses the proposal boundary."""
        self.calls += 1
        raise AssertionError("deterministic routing must not call a provider")


def _request(registry, boundary, requirements) -> ParserRoutingRequest:
    """Construct one structured native-PDF deterministic routing request."""
    return ParserRoutingRequest(
        mode="deterministic",
        input_facts=RoutingInputFacts(
            media_type="application/pdf",
            native_text_ratio=0.98,
            required_capabilities=requirements,
        ),
        boundary=boundary,
        registry=registry,
    )


def _replace_parser(registry, parser_id: str, transform):
    """Return a validated registry with one immutable parser record transformed."""
    updated = replace(
        registry,
        parsers=tuple(
            transform(item) if item.parser_id == parser_id else item
            for item in registry.parsers
        ),
    )
    updated.validate()
    return updated


def test_deterministic_mode_never_calls_proposal_provider(
    available_routing_registry, routing_boundary
) -> None:
    """Evaluate rules and validation without consulting an LLM or fake provider."""
    provider = _FailIfCalledProvider()
    plan = ParserRoutingService().plan(
        _request(
            available_routing_registry,
            routing_boundary,
            ("hierarchy", "tables", "native_links"),
        ),
        proposal_provider=provider,
    )
    assert plan.validation_result.accepted is True
    encoded = plan.to_json_bytes()
    assert RoutingPlan.from_json_bytes(encoded).to_json_bytes() == encoded
    assert plan.llm_used is False
    assert provider.calls == 0


def test_deterministic_rules_produce_frozen_two_parser_plan(
    available_routing_registry, routing_boundary, v3_2_fixture_root
) -> None:
    """Match the frozen Docling-plus-PyMuPDF rule and invocation sequence."""
    plan = ParserRoutingService().plan(
        _request(
            available_routing_registry,
            routing_boundary,
            ("hierarchy", "tables", "native_links"),
        )
    )
    frozen = json.loads(
        (
            v3_2_fixture_root / "routing" / "deterministic_plan.json"
        ).read_text(encoding="utf-8")
    )
    assert plan.rules_evaluated == tuple(frozen["rules_evaluated"])
    assert [item.parser_id for item in plan.selected_invocations] == [
        item["parser_id"] for item in frozen["selected_invocations"]
    ]
    assert [list(item.purpose) for item in plan.selected_invocations] == [
        item["purpose"] for item in frozen["selected_invocations"]
    ]
    assert plan.validation_result.accepted is True
    assert plan.candidate_invocations == plan.selected_invocations


def test_rejected_budget_plan_retains_candidates_but_selects_nothing(
    available_routing_registry, routing_boundary
) -> None:
    """Keep two valid candidates auditable when a one-run budget rejects them."""
    boundary = replace(routing_boundary, max_parser_runs=1)
    plan = ParserRoutingService().plan(
        _request(
            available_routing_registry,
            boundary,
            ("hierarchy", "tables", "native_links"),
        )
    )
    assert tuple(item.parser_id for item in plan.candidate_invocations) == (
        "docling",
        "pymupdf",
    )
    assert plan.selected_invocations == ()
    assert plan.validation_result.accepted is False
    assert plan.validation_result.budget_valid is False
    assert RoutingPlan.from_json_bytes(plan.to_json_bytes()) == plan
    with pytest.raises(ParserRoutingRejectedError):
        plan.require_accepted()


def test_partially_satisfiable_request_has_no_selected_invocations(
    available_routing_registry, routing_boundary
) -> None:
    """Reject the whole plan while retaining only the eligible audit candidate."""
    unavailable = _replace_parser(
        available_routing_registry,
        "docling",
        lambda record: replace(
            record,
            parser_discovered=replace(
                record.parser_discovered,
                runtime_probe=replace(
                    record.parser_discovered.runtime_probe,
                    dependency_importable=False,
                ),
            ),
        ),
    )
    plan = ParserRoutingService().plan(
        _request(unavailable, routing_boundary, ("hierarchy", "native_links"))
    )
    assert tuple(item.parser_id for item in plan.candidate_invocations) == (
        "pymupdf",
    )
    assert plan.selected_invocations == ()
    assert plan.validation_result.accepted is False
    assert "required-purpose-unresolved" in plan.validation_result.rejection_reasons


def test_deterministic_security_rejection_has_no_selected_invocations(
    available_routing_registry, routing_boundary
) -> None:
    """Keep eligible candidates separate when required security tags are absent."""
    boundary = RoutingBoundary(
        allowlist=routing_boundary.allowlist,
        max_parser_runs=2,
        external_services_allowed=False,
        required_security_tags=("internal",),
    )
    plan = ParserRoutingService().plan(
        _request(available_routing_registry, boundary, ("native_links",))
    )
    assert tuple(item.parser_id for item in plan.candidate_invocations) == (
        "pymupdf",
    )
    assert plan.selected_invocations == ()
    assert plan.validation_result.accepted is False
    assert plan.validation_result.security_valid is False


def test_canonical_deterministic_plan_round_trip_preserves_complete_context(
    available_routing_registry, routing_boundary
) -> None:
    """Reload facts, boundary, candidates, validation, version, and exact digest."""
    plan = ParserRoutingService().plan(
        _request(available_routing_registry, routing_boundary, ("native_links",))
    )
    reloaded = RoutingPlan.from_json_bytes(plan.to_json_bytes())
    assert reloaded == plan
    assert plan.registry_sha256 == hashlib.sha256(
        available_routing_registry.to_json_bytes()
    ).hexdigest()


def test_unavailable_docling_is_not_selected_and_requirement_is_unresolved(
    available_routing_registry, routing_boundary
) -> None:
    """Keep a false runtime fact from becoming an executable deterministic choice."""
    unavailable = _replace_parser(
        available_routing_registry,
        "docling",
        lambda record: replace(
            record,
            parser_discovered=replace(
                record.parser_discovered,
                runtime_probe=replace(
                    record.parser_discovered.runtime_probe,
                    dependency_importable=False,
                ),
            ),
        ),
    )
    plan = ParserRoutingService().plan(
        _request(unavailable, routing_boundary, ("hierarchy", "tables"))
    )
    assert plan.selected_invocations == ()
    assert plan.validation_result.accepted is False
    assert plan.validation_result.capability_valid is False
    assert "required-purpose-unresolved" in (
        plan.validation_result.rejection_reasons
    )


def test_native_links_select_pymupdf_independently(
    available_routing_registry, routing_boundary
) -> None:
    """Route a native-link-only request without unnecessarily selecting Docling."""
    plan = ParserRoutingService().plan(
        _request(available_routing_registry, routing_boundary, ("native_links",))
    )
    assert tuple(item.parser_id for item in plan.selected_invocations) == (
        "pymupdf",
    )
    assert plan.validation_result.accepted is True


@pytest.mark.parametrize(
    "status", ("unsupported", "not-declared", "unavailable", "unknown")
)
def test_ineligible_required_capability_does_not_silently_pass(
    status: str, available_routing_registry, routing_boundary
) -> None:
    """Treat every ineligible assertion as unresolved despite available runtime."""
    unsupported = _replace_parser(
        available_routing_registry,
        "docling",
        lambda record: replace(
            record,
            parser_discovered=replace(
                record.parser_discovered,
                capabilities=tuple(
                    replace(item, status=status)
                    if item.capability == "tables"
                    else item
                    for item in record.parser_discovered.capabilities
                ),
            ),
        ),
    )
    plan = ParserRoutingService().plan(
        _request(unsupported, routing_boundary, ("tables",))
    )
    assert plan.selected_invocations == ()
    assert plan.validation_result.accepted is False
    assert plan.validation_result.capability_valid is False


@pytest.mark.parametrize(
    "status", ("available", "declared", "declared-when-available")
)
def test_explicit_eligible_capability_statuses_satisfy_requirements(
    status: str, available_routing_registry, routing_boundary
) -> None:
    """Accept only the three reviewed registry assertion statuses for routing."""
    registry = _replace_parser(
        available_routing_registry,
        "docling",
        lambda record: replace(
            record,
            parser_discovered=replace(
                record.parser_discovered,
                capabilities=tuple(
                    replace(item, status=status)
                    if item.capability == "tables"
                    else item
                    for item in record.parser_discovered.capabilities
                ),
            ),
        ),
    )
    plan = ParserRoutingService().plan(
        _request(registry, routing_boundary, ("tables",))
    )
    assert plan.validation_result.accepted is True


def test_planning_never_executes_router_or_fusion(
    monkeypatch, available_routing_registry, routing_boundary
) -> None:
    """Keep T04 outside existing parser execution and T05 fusion mechanisms."""
    import cognityx_ingest.parser as parser_module

    def fail(*args, **kwargs):
        """Raise if routing reaches either parser execution or fusion."""
        raise AssertionError("routing must not execute or fuse parsers")

    monkeypatch.setattr(parser_module.ParserRouter, "extract_document", fail)
    monkeypatch.setattr(parser_module, "_fuse_results", fail)
    plan = ParserRoutingService().plan(
        _request(available_routing_registry, routing_boundary, ("native_links",))
    )
    assert plan.validation_result.accepted is True


def test_deterministic_planning_uses_no_network_and_does_not_mutate_registry(
    monkeypatch, available_routing_registry, routing_boundary
) -> None:
    """Keep normal planning local, read-only, and independent of parser services."""
    before = available_routing_registry.to_json_bytes()

    def fail_socket(*args, **kwargs):
        """Raise if T04 attempts to construct any network socket."""
        raise AssertionError("routing must not open a network socket")

    monkeypatch.setattr(socket, "socket", fail_socket)
    plan = ParserRoutingService().plan(
        _request(available_routing_registry, routing_boundary, ("native_links",))
    )
    assert plan.validation_result.accepted
    assert available_routing_registry.to_json_bytes() == before
