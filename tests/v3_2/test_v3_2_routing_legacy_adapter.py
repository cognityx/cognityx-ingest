"""Lossless T04-to-legacy policy compatibility adapter tests."""

from __future__ import annotations

import pytest

from cognityx_ingest import (
    ExtractionPolicy,
    ParserInvocation,
    ParserRoutingCompatibilityError,
    ParserRoutingRequest,
    ParserRoutingService,
    RoutingBoundary,
    RoutingInputFacts,
    RoutingProposal,
)


class _Provider:
    """Return one immutable proposal without executing parser adapters."""

    def __init__(self, proposal: RoutingProposal) -> None:
        """Store the proposal used by the compatibility scenario."""
        self.proposal = proposal

    def propose(self, request, registry, boundary) -> RoutingProposal:
        """Return the configured proposal as untrusted T04 input."""
        return self.proposal


def _plan(registry, boundary, requirements, invocations, *, mode="hybrid", stop=None):
    """Build one accepted proposal-backed plan for compatibility assertions."""
    request = ParserRoutingRequest(
        mode=mode,
        input_facts=RoutingInputFacts(
            media_type="application/pdf",
            required_capabilities=requirements,
        ),
        boundary=boundary,
        registry=registry,
    )
    proposal = RoutingProposal(
        invocations=invocations,
        stop_condition=stop,
    )
    return ParserRoutingService().plan(
        request,
        proposal_provider=_Provider(proposal),
    )


def test_one_lossless_document_invocation_maps_to_fixed(
    available_routing_registry,
) -> None:
    """Map one purpose-free document parser without changing legacy semantics."""
    boundary = RoutingBoundary(
        allowlist=("pymupdf",),
        max_parser_runs=1,
        external_services_allowed=False,
    )
    plan = _plan(
        available_routing_registry,
        boundary,
        ("native_links",),
        (ParserInvocation(parser_id="pymupdf", scope="document"),),
    )
    assert plan.to_extraction_policy() == ExtractionPolicy(
        mode="fixed", backends=("pymupdf",)
    )


def test_multiple_lossless_document_invocations_map_to_compare(
    available_routing_registry, routing_boundary
) -> None:
    """Map two purpose-free document runs to the existing compare policy only."""
    plan = _plan(
        available_routing_registry,
        routing_boundary,
        ("hierarchy", "native_links"),
        (
            ParserInvocation(parser_id="docling", scope="document"),
            ParserInvocation(parser_id="pymupdf", scope="document"),
        ),
    )
    assert plan.to_extraction_policy() == ExtractionPolicy(
        mode="compare", backends=("docling", "pymupdf")
    )


def test_rejected_plan_refuses_legacy_conversion(
    available_routing_registry, routing_boundary
) -> None:
    """Prevent an invented parser proposal from becoming executable policy."""
    plan = _plan(
        available_routing_registry,
        routing_boundary,
        ("hierarchy",),
        (ParserInvocation(parser_id="invented-parser", scope="document"),),
    )
    assert plan.validation_result.accepted is False
    with pytest.raises(ParserRoutingCompatibilityError, match="Rejected"):
        plan.to_extraction_policy()


def test_page_scope_refuses_lossy_legacy_conversion(
    available_routing_registry, routing_boundary
) -> None:
    """Never turn a page-scoped plan into a silent whole-document parser run."""
    plan = _plan(
        available_routing_registry,
        routing_boundary,
        ("native_links",),
        (
            ParserInvocation(
                parser_id="pymupdf", scope="pages-with-native-links"
            ),
        ),
        mode="llm-directed",
    )
    assert plan.validation_result.accepted is True
    with pytest.raises(ParserRoutingCompatibilityError, match="Page-scoped"):
        plan.to_extraction_policy()


def test_stop_condition_refuses_lossy_legacy_conversion(
    available_routing_registry, routing_boundary
) -> None:
    """Keep future observation-completeness semantics out of legacy execution."""
    plan = _plan(
        available_routing_registry,
        routing_boundary,
        ("hierarchy", "native_links"),
        (
            ParserInvocation(parser_id="docling", scope="document"),
            ParserInvocation(parser_id="pymupdf", scope="document"),
        ),
        mode="llm-directed",
        stop="all-required-capabilities-observed-or-explicitly-unresolved",
    )
    assert plan.validation_result.accepted is True
    with pytest.raises(ParserRoutingCompatibilityError, match="stop condition"):
        plan.to_extraction_policy()


def test_purpose_refuses_lossy_legacy_conversion(
    available_routing_registry, routing_boundary
) -> None:
    """Keep routing purpose visible rather than dropping it from ExtractionPolicy."""
    request = ParserRoutingRequest(
        mode="deterministic",
        input_facts=RoutingInputFacts(
            media_type="application/pdf",
            native_text_ratio=0.98,
            required_capabilities=("native_links",),
        ),
        boundary=routing_boundary,
        registry=available_routing_registry,
    )
    plan = ParserRoutingService().plan(request)
    assert plan.validation_result.accepted is True
    with pytest.raises(ParserRoutingCompatibilityError, match="purpose"):
        plan.to_extraction_policy()


def test_adaptive_mode_remains_invalid_legacy_extraction_policy() -> None:
    """Keep T04 names outside the existing ParserRouter execution contract."""
    with pytest.raises(ValueError, match="Unknown extraction policy mode"):
        ExtractionPolicy(mode="deterministic")
