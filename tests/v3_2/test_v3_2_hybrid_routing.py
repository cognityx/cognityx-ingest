"""Hybrid T04 hard-boundary, provider-call, and rejection tests."""

from __future__ import annotations

from dataclasses import replace

import pytest

from cognityx_ingest import (
    CapabilityConflict,
    ParserInvocation,
    ParserRoutingProposalError,
    ParserRoutingRequest,
    ParserRoutingService,
    RoutingBoundary,
    RoutingInputFacts,
    RoutingPlan,
    RoutingProposal,
)


class _FrozenProvider:
    """Return one immutable proposal while exposing the exact call count."""

    def __init__(self, proposal: RoutingProposal) -> None:
        """Store the proposal and initialize a zero call count."""
        self.proposal = proposal
        self.calls = 0

    def propose(self, request, registry, boundary) -> RoutingProposal:
        """Return the frozen proposal after asserting authoritative inputs arrive."""
        self.calls += 1
        assert registry is request.registry
        assert boundary is request.boundary
        return self.proposal


class _FailingProvider:
    """Raise a private provider exception for typed-boundary translation tests."""

    def propose(self, request, registry, boundary) -> RoutingProposal:
        """Simulate a provider implementation failure without leaking its detail."""
        raise RuntimeError("private provider detail")


def _request(registry, boundary) -> ParserRoutingRequest:
    """Create the frozen structured-PDF hybrid routing request."""
    return ParserRoutingRequest(
        mode="hybrid",
        input_facts=RoutingInputFacts(
            media_type="application/pdf",
            native_text_ratio=0.98,
            required_capabilities=("hierarchy", "tables", "native_links"),
        ),
        boundary=boundary,
        registry=registry,
    )


def _proposal(*parser_ids: str, external: bool = False) -> RoutingProposal:
    """Create a document-scoped proposal in the supplied parser order."""
    purposes = {
        "docling": ("hierarchy", "tables"),
        "pymupdf": ("native_links",),
    }
    return RoutingProposal(
        invocations=tuple(
            ParserInvocation(
                parser_id=parser_id,
                scope="document",
                purpose=purposes.get(parser_id, ()),
            )
            for parser_id in parser_ids
        ),
        reason="Use structural parser plus native PDF fact complement.",
        external_services_used=external,
    )


def test_hybrid_calls_provider_once_and_accepts_frozen_bounded_proposal(
    available_routing_registry, routing_boundary
) -> None:
    """Accept Docling plus PyMuPDF only after one deterministic provider call."""
    provider = _FrozenProvider(_proposal("docling", "pymupdf"))
    plan = ParserRoutingService().plan(
        _request(available_routing_registry, routing_boundary),
        proposal_provider=provider,
    )
    assert provider.calls == 1
    assert plan.validation_result.accepted is True
    assert tuple(item.parser_id for item in plan.selected_invocations) == (
        "docling",
        "pymupdf",
    )
    assert plan.llm_used is True
    encoded = plan.to_json_bytes()
    reloaded = RoutingPlan.from_json_bytes(encoded)
    assert reloaded.to_json_bytes() == encoded
    assert reloaded == plan


def test_hybrid_rejects_parser_outside_allowlist(
    available_routing_registry, routing_boundary
) -> None:
    """Keep an invented parser proposal visible but select nothing executable."""
    provider = _FrozenProvider(_proposal("docling", "invented-parser"))
    plan = ParserRoutingService().plan(
        _request(available_routing_registry, routing_boundary),
        proposal_provider=provider,
    )
    assert plan.validation_result.accepted is False
    assert plan.validation_result.allowlist_valid is False
    assert plan.selected_invocations == ()
    assert "parser-outside-allowlist" in plan.validation_result.rejection_reasons


def test_hybrid_rejects_parser_run_budget_overflow(
    available_routing_registry, routing_boundary
) -> None:
    """Reject two proposed runs when the deterministic budget permits only one."""
    boundary = replace(routing_boundary, max_parser_runs=1)
    plan = ParserRoutingService().plan(
        _request(available_routing_registry, boundary),
        proposal_provider=_FrozenProvider(_proposal("docling", "pymupdf")),
    )
    assert plan.validation_result.accepted is False
    assert plan.validation_result.budget_valid is False
    assert plan.selected_invocations == ()


def test_hybrid_rejects_unavailable_parser(
    available_routing_registry, routing_boundary
) -> None:
    """Preserve false runtime evidence and reject the proposed executable plan."""
    docling = available_routing_registry.get("docling")
    unavailable_record = replace(
        docling,
        parser_discovered=replace(
            docling.parser_discovered,
            runtime_probe=replace(
                docling.parser_discovered.runtime_probe,
                plugin_registered=False,
            ),
        ),
        conflicts=(
            CapabilityConflict(
                capability="document_hierarchy",
                advertised=True,
                runtime_available=False,
                resolution="declared-but-currently-unavailable",
            ),
        ),
    )
    registry = replace(
        available_routing_registry,
        parsers=tuple(
            unavailable_record if item.parser_id == "docling" else item
            for item in available_routing_registry.parsers
        ),
    )
    registry.validate()
    before_probe = registry.get("docling").parser_discovered.runtime_probe
    before_conflicts = registry.get("docling").conflicts
    plan = ParserRoutingService().plan(
        _request(registry, routing_boundary),
        proposal_provider=_FrozenProvider(_proposal("docling", "pymupdf")),
    )
    assert plan.validation_result.accepted is False
    assert plan.validation_result.runtime_valid is False
    assert registry.get("docling").parser_discovered.runtime_probe == before_probe
    assert registry.get("docling").conflicts == before_conflicts


def test_hybrid_rejects_external_service_use_when_forbidden(
    available_routing_registry, routing_boundary
) -> None:
    """Keep provider recommendations inside the deterministic security boundary."""
    plan = ParserRoutingService().plan(
        _request(available_routing_registry, routing_boundary),
        proposal_provider=_FrozenProvider(
            _proposal("docling", "pymupdf", external=True)
        ),
    )
    assert plan.validation_result.accepted is False
    assert plan.validation_result.security_valid is False
    assert "security-boundary-violated" in (
        plan.validation_result.rejection_reasons
    )


def test_hybrid_rejects_noncanonical_boundary_ordered_proposal(
    available_routing_registry, routing_boundary
) -> None:
    """Reject a provider order that attempts to replace deterministic boundary order."""
    plan = ParserRoutingService().plan(
        _request(available_routing_registry, routing_boundary),
        proposal_provider=_FrozenProvider(_proposal("pymupdf", "docling")),
    )
    assert plan.validation_result.accepted is False
    assert plan.validation_result.schema_valid is False
    assert "hybrid-invocation-order-invalid" in (
        plan.validation_result.rejection_reasons
    )


def test_hybrid_provider_failure_and_absence_raise_typed_error(
    available_routing_registry, routing_boundary
) -> None:
    """Never leak provider exceptions or silently become deterministic routing."""
    request = _request(available_routing_registry, routing_boundary)
    with pytest.raises(ParserRoutingProposalError, match="provider failed") as caught:
        ParserRoutingService().plan(
            request,
            proposal_provider=_FailingProvider(),
        )
    assert "private provider detail" not in str(caught.value)
    with pytest.raises(ParserRoutingProposalError, match="requires a proposal"):
        ParserRoutingService().plan(request)


def test_hybrid_security_tags_must_satisfy_boundary(
    available_routing_registry, routing_boundary
) -> None:
    """Reject proposals that omit a governed security classification tag."""
    boundary = RoutingBoundary(
        allowlist=routing_boundary.allowlist,
        max_parser_runs=2,
        external_services_allowed=False,
        required_security_tags=("internal",),
    )
    plan = ParserRoutingService().plan(
        _request(available_routing_registry, boundary),
        proposal_provider=_FrozenProvider(_proposal("docling", "pymupdf")),
    )
    assert plan.validation_result.accepted is False
    assert plan.validation_result.security_valid is False


@pytest.mark.parametrize(
    "invocations",
    (
        (
            ParserInvocation(
                parser_id="docling",
                scope="document",
                purpose=("hierarchy", "tables", "native_links"),
            ),
            ParserInvocation(
                parser_id="pymupdf",
                scope="document",
                purpose=(),
            ),
        ),
        (
            ParserInvocation(
                parser_id="docling",
                scope="document",
                purpose=("tables",),
            ),
            ParserInvocation(
                parser_id="pymupdf",
                scope="document",
                purpose=("hierarchy", "native_links"),
            ),
        ),
        (
            ParserInvocation(
                parser_id="docling",
                scope="document",
                purpose=("native_links",),
            ),
            ParserInvocation(
                parser_id="pymupdf",
                scope="document",
                purpose=("hierarchy", "tables"),
            ),
        ),
    ),
)
def test_hybrid_rejects_parser_incapable_or_swapped_purposes(
    invocations, available_routing_registry, routing_boundary
) -> None:
    """Refuse attribution that another selected parser could otherwise conceal."""
    proposal = RoutingProposal(invocations=invocations)
    plan = ParserRoutingService().plan(
        _request(available_routing_registry, routing_boundary),
        proposal_provider=_FrozenProvider(proposal),
    )
    assert plan.selected_invocations == ()
    assert plan.validation_result.capability_valid is False
    assert "invocation-purpose-unsupported" in (
        plan.validation_result.rejection_reasons
    )


def test_hybrid_rejects_empty_purposes_for_nonempty_request(
    available_routing_registry, routing_boundary
) -> None:
    """Require live proposals to attribute every requested capability explicitly."""
    proposal = RoutingProposal(
        invocations=(
            ParserInvocation(parser_id="docling", scope="document"),
            ParserInvocation(parser_id="pymupdf", scope="document"),
        )
    )
    plan = ParserRoutingService().plan(
        _request(available_routing_registry, routing_boundary),
        proposal_provider=_FrozenProvider(proposal),
    )
    assert plan.selected_invocations == ()
    assert "required-purpose-unresolved" in (
        plan.validation_result.rejection_reasons
    )


def test_hybrid_accepts_parser_specific_required_and_complementary_purposes(
    available_routing_registry, routing_boundary
) -> None:
    """Keep genuine Docling and PyMuPDF purpose ownership in an accepted plan."""
    proposal = RoutingProposal(
        invocations=(
            ParserInvocation(
                parser_id="docling",
                scope="document",
                purpose=("hierarchy", "tables"),
            ),
            ParserInvocation(
                parser_id="pymupdf",
                scope="document",
                purpose=("native_links", "page_labels", "geometry"),
            ),
        )
    )
    plan = ParserRoutingService().plan(
        _request(available_routing_registry, routing_boundary),
        proposal_provider=_FrozenProvider(proposal),
    )
    assert plan.validation_result.accepted is True
    assert plan.selected_invocations == proposal.invocations


def test_hybrid_rejects_unsupported_extra_complementary_purpose(
    available_routing_registry, routing_boundary
) -> None:
    """Reject an extra purpose unless that invocation's parser actually supports it."""
    proposal = RoutingProposal(
        invocations=(
            ParserInvocation(
                parser_id="docling",
                scope="document",
                purpose=("hierarchy", "tables", "native_pdf_text"),
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
        proposal_provider=_FrozenProvider(proposal),
    )
    assert plan.selected_invocations == ()
    assert "invocation-purpose-unsupported" in (
        plan.validation_result.rejection_reasons
    )
