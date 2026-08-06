"""LLM-directed T04 proposal freedom and deterministic authority tests."""

from __future__ import annotations

import hashlib

import pytest

from cognityx_ingest import (
    ParserInvocation,
    ParserRoutingProposalError,
    ParserRoutingRequest,
    ParserRoutingService,
    RoutingInputFacts,
    RoutingPlan,
    RoutingProposal,
)


_STOP = "all-required-capabilities-observed-or-explicitly-unresolved"


class _CountingProvider:
    """Return one proposal and count the single allowed LLM-directed call."""

    def __init__(self, proposal: RoutingProposal) -> None:
        """Store immutable output and initialize a zero call count."""
        self.proposal = proposal
        self.calls = 0

    def propose(self, request, registry, boundary) -> RoutingProposal:
        """Return the configured untrusted proposal exactly once per service call."""
        self.calls += 1
        return self.proposal


def _request(registry, boundary) -> ParserRoutingRequest:
    """Create a registry-bound LLM-directed routing request."""
    return ParserRoutingRequest(
        mode="llm-directed",
        input_facts=RoutingInputFacts(
            media_type="application/pdf",
            required_capabilities=("hierarchy", "native_links"),
        ),
        boundary=boundary,
        registry=registry,
    )


def _proposal(
    invocations: tuple[ParserInvocation, ...], *, stop_condition: str = _STOP
) -> RoutingProposal:
    """Create a frozen provider proposal with bounded audit identifiers."""
    return RoutingProposal(
        invocations=invocations,
        reason="Use complementary registry-backed observations.",
        stop_condition=stop_condition,
        provider="fixture-provider",
        model="fixture-model",
        request_id="request-001",
    )


def test_llm_directed_calls_provider_once_and_accepts_frozen_plan(
    available_routing_registry, routing_boundary, routing_provider_profile
) -> None:
    """Accept the frozen document plus native-link-page proposal after validation."""
    proposal = _proposal(
        (
            ParserInvocation(
                parser_id="docling",
                scope="document",
                purpose=("hierarchy",),
            ),
            ParserInvocation(
                parser_id="pymupdf",
                scope="pages-with-native-links",
                purpose=("native_links",),
            ),
        )
    )
    provider = _CountingProvider(proposal)
    plan = ParserRoutingService().plan(
        _request(available_routing_registry, routing_boundary),
        proposal_provider=provider,
        provider_profile=routing_provider_profile,
    )
    assert provider.calls == 1
    assert plan.validation_result.accepted is True
    assert plan.registry_version == available_routing_registry.registry_version
    assert plan.proposal == proposal
    assert plan.provider_profile == routing_provider_profile
    assert plan.llm_used is True
    encoded = plan.to_json_bytes()
    reloaded = RoutingPlan.from_json_bytes(encoded)
    assert reloaded.to_json_bytes() == encoded
    assert reloaded == plan
    assert plan.registry_sha256 == hashlib.sha256(
        available_routing_registry.to_json_bytes()
    ).hexdigest()


def test_llm_directed_preserves_provider_invocation_order(
    available_routing_registry, routing_boundary, routing_provider_profile
) -> None:
    """Retain an LLM-directed order while still validating every selected parser."""
    proposal = _proposal(
        (
            ParserInvocation(
                parser_id="pymupdf",
                scope="document",
                purpose=("native_links",),
            ),
            ParserInvocation(
                parser_id="docling",
                scope="document",
                purpose=("hierarchy",),
            ),
        )
    )
    plan = ParserRoutingService().plan(
        _request(available_routing_registry, routing_boundary),
        proposal_provider=_CountingProvider(proposal),
        provider_profile=routing_provider_profile,
    )
    assert plan.validation_result.accepted is True
    assert plan.selected_invocations == proposal.invocations


def test_llm_directed_rejects_invented_parser_id(
    available_routing_registry, routing_boundary, routing_provider_profile
) -> None:
    """Keep model-invented parser identities outside the registry authority."""
    proposal = _proposal(
        (
            ParserInvocation(
                parser_id="docling",
                scope="document",
                purpose=("hierarchy",),
            ),
            ParserInvocation(parser_id="invented-parser", scope="document"),
        )
    )
    plan = ParserRoutingService().plan(
        _request(available_routing_registry, routing_boundary),
        proposal_provider=_CountingProvider(proposal),
        provider_profile=routing_provider_profile,
    )
    assert plan.validation_result.accepted is False
    assert plan.validation_result.registry_valid is False
    assert plan.selected_invocations == ()
    assert "parser-not-in-registry" in plan.validation_result.rejection_reasons


def test_llm_directed_rejects_unsupported_scope(
    available_routing_registry, routing_boundary, routing_provider_profile
) -> None:
    """Reject and safely quarantine a provider-invented execution scope."""
    proposal = _proposal(
        (ParserInvocation(parser_id="docling", scope="selected-pages"),)
    )
    plan = ParserRoutingService().plan(
        _request(available_routing_registry, routing_boundary),
        proposal_provider=_CountingProvider(proposal),
        provider_profile=routing_provider_profile,
    )
    assert plan.validation_result.accepted is False
    assert plan.validation_result.schema_valid is False
    assert plan.selected_invocations == ()
    assert plan.proposal.invocations == ()


def test_llm_directed_rejects_invalid_stop_condition(
    available_routing_registry, routing_boundary, routing_provider_profile
) -> None:
    """Record rejection without executing or accepting an invented stop rule."""
    proposal = _proposal(
        (
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
        ),
        stop_condition="model-says-enough",
    )
    plan = ParserRoutingService().plan(
        _request(available_routing_registry, routing_boundary),
        proposal_provider=_CountingProvider(proposal),
        provider_profile=routing_provider_profile,
    )
    assert plan.validation_result.accepted is False
    assert plan.validation_result.schema_valid is False
    assert plan.proposal.stop_condition is None
    encoded = plan.to_json_bytes()
    assert RoutingPlan.from_json_bytes(encoded).to_json_bytes() == encoded


def test_llm_directed_without_provider_fails_explicitly(
    available_routing_registry, routing_boundary
) -> None:
    """Never hide missing model configuration behind deterministic fallback."""
    with pytest.raises(ParserRoutingProposalError, match="requires a proposal"):
        ParserRoutingService().plan(
            _request(available_routing_registry, routing_boundary)
        )


def test_llm_proposal_receives_no_source_content_or_paths(
    available_routing_registry, routing_boundary, routing_provider_profile
) -> None:
    """Limit provider input to immutable facts, registry evidence, and policy."""
    class InspectingProvider:
        """Inspect typed inputs and return a valid registry-backed proposal."""

        def propose(self, request, registry, boundary) -> RoutingProposal:
            """Assert the request model has no source bytes, text, or path fields."""
            assert not hasattr(request, "source_text")
            assert not hasattr(request, "source_bytes")
            assert not hasattr(request, "source_path")
            assert request.registry is registry
            assert request.boundary is boundary
            return _proposal(
                (
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

    plan = ParserRoutingService().plan(
        _request(available_routing_registry, routing_boundary),
        proposal_provider=InspectingProvider(),
        provider_profile=routing_provider_profile,
    )
    assert plan.validation_result.accepted is True
