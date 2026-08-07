"""Deterministic six-outcome provenance resolution and no-leak tests."""

from __future__ import annotations

from dataclasses import replace

import pytest

from cognityx_ingest import (
    AddressResolution,
    PROVENANCE_RESOLUTION_STATUSES,
    ProvenanceAddressCatalog,
    ProvenanceAddressResolver,
    ProvenanceAddressValidationError,
    ProvenanceTarget,
    SourceGraphRepository,
)
from cognityx_ingest.source_graph import SourceGraphDivision, SourceGraphResource


class _DenyResource:
    """Deny one fixture resource through the narrow access-policy protocol."""

    def __init__(self, resource_id: str) -> None:
        self.resource_id = resource_id

    def allows(self, address_id: str, resource_id: str) -> bool:
        """Return false only for the configured resource without inspecting targets."""
        return resource_id != self.resource_id


def _fixture(v3_2_fixture_root):
    """Return the frozen graph and its explicitly attached catalog."""
    graph = SourceGraphRepository.from_fixture(v3_2_fixture_root).load("sg-rev-001")
    assert graph.address_catalog is not None
    return graph, graph.address_catalog


def _catalog_with_strong(catalog, address):
    """Replace one strong address by ID while retaining other frozen families."""
    return replace(
        catalog,
        strong_addresses=tuple(
            address if item.address_id == address.address_id else item
            for item in catalog.strong_addresses
        ),
        compact_fixture=False,
    )


def test_all_frozen_strong_addresses_resolve_exact(v3_2_fixture_root) -> None:
    """Resolve node and division targets against exact hash/revision membership."""
    graph, catalog = _fixture(v3_2_fixture_root)
    resolver = ProvenanceAddressResolver(graph, catalog)

    p2 = resolver.resolve("addr-strong-pol-p2")
    p5 = resolver.resolve("addr-strong-pol-p5")
    authority = resolver.resolve("addr-strong-auth-21")
    assert (p2.status, p2.target.node_id) == ("exact", "pol-p2")
    assert (p5.status, p5.target.node_id) == ("exact", "pol-p5")
    assert (authority.status, authority.target.division_id) == (
        "exact",
        "div-authority-2.1",
    )


def test_compact_frozen_selector_compatibility_remains_exact(v3_2_fixture_root) -> None:
    """Keep the frozen selector without ID and division address without selectors exact."""
    graph, catalog = _fixture(v3_2_fixture_root)
    p2 = next(
        item for item in catalog.strong_addresses if item.address_id == "addr-strong-pol-p2"
    )
    authority = next(
        item
        for item in catalog.strong_addresses
        if item.address_id == "addr-strong-auth-21"
    )

    assert p2.selectors and all(item.selector_id is None for item in p2.selectors)
    assert authority.selectors == ()
    resolver = ProvenanceAddressResolver(graph, catalog)
    assert resolver.resolve(p2.address_id).status == "exact"
    assert resolver.resolve(authority.address_id).status == "exact"


def test_logical_unique_candidate_redirects_and_multiple_candidates_are_ambiguous(
    v3_2_fixture_root,
) -> None:
    """Select only a unique explicit family/version candidate and never lexical latest."""
    graph, catalog = _fixture(v3_2_fixture_root)
    address_id = "addr-logical-policy-4.2-effective"
    unique = ProvenanceAddressResolver(graph, catalog).resolve(address_id)
    assert (unique.status, unique.target.division_id) == (
        "redirected",
        "div-policy-4.2",
    )

    second_resource = SourceGraphResource(
        resource_id="res-policy-v3",
        family_id="aster-vale-travel-policy",
        version="3",
        source_sha256="f" * 64,
    )
    second_division = SourceGraphDivision(
        division_id="div-policy-v3-4.2",
        resource_id=second_resource.resource_id,
        division_role="section",
        number="4.2",
        parent_division_id=None,
        child_division_ids=(),
        direct_node_ids=(),
    )
    ambiguous_graph = replace(
        graph,
        resources=(*graph.resources, second_resource),
        divisions=(*graph.divisions, second_division),
        address_catalog=None,
    )
    ambiguous = ProvenanceAddressResolver(ambiguous_graph, catalog).resolve(address_id)
    assert ambiguous.status == "ambiguous"
    assert ambiguous.target is None
    assert tuple(item.division_id for item in ambiguous.candidate_targets) == (
        "div-policy-4.2",
        "div-policy-v3-4.2",
    )


def test_explicit_obsolescence_and_missing_address_are_safe(v3_2_fixture_root) -> None:
    """Return obsolete/unresolved without redirecting or inventing target details."""
    graph, catalog = _fixture(v3_2_fixture_root)
    obsolete = ProvenanceAddressResolver(
        graph,
        catalog,
        obsolete_address_ids=frozenset({"addr-strong-pol-p2"}),
    ).resolve("addr-strong-pol-p2")
    missing = ProvenanceAddressResolver(graph, catalog).resolve("addr-missing")

    assert obsolete.status == "obsolete"
    assert obsolete.target is None
    assert missing.status == "unresolved"
    assert missing.target is None
    assert missing.candidate_targets == ()


def test_denied_resolution_returns_forbidden_without_target_leak(v3_2_fixture_root) -> None:
    """Apply access policy before exposing target IDs, selectors, or candidates."""
    graph, catalog = _fixture(v3_2_fixture_root)
    result = ProvenanceAddressResolver(
        graph,
        catalog,
        access_policy=_DenyResource("res-policy-v2"),
    ).resolve("addr-strong-pol-p2")

    assert result.status == "forbidden"
    assert result.target is None
    assert result.targets == ()
    assert result.candidate_targets == ()


def test_hash_revision_and_target_resource_mismatch_cannot_return_exact(
    v3_2_fixture_root,
) -> None:
    """Fail immutable binding checks instead of substituting current evidence."""
    graph, catalog = _fixture(v3_2_fixture_root)
    address = catalog.strong_addresses[0]

    wrong_hash = replace(address, source_sha256="a" * 64)
    wrong_revision = replace(address, graph_revision="sg-old")
    wrong_target = replace(
        address,
        canonical_target=catalog.strong_addresses[2].canonical_target,
        selectors=(),
    )
    hash_result = ProvenanceAddressResolver(
        graph, _catalog_with_strong(catalog, wrong_hash)
    ).resolve(address.address_id)
    revision_result = ProvenanceAddressResolver(
        graph, _catalog_with_strong(catalog, wrong_revision)
    ).resolve(address.address_id)
    target_result = ProvenanceAddressResolver(
        graph, _catalog_with_strong(catalog, wrong_target)
    ).resolve(address.address_id)

    assert hash_result.status == "obsolete"
    assert revision_result.status == "obsolete"
    assert target_result.status == "unresolved"
    assert target_result.target is None


def test_evidence_set_requires_every_ordered_member_and_forbidden_member_leaks_nothing(
    v3_2_fixture_root,
) -> None:
    """Keep exact ordered closure only when every strong member is permitted/exact."""
    graph, catalog = _fixture(v3_2_fixture_root)
    address_id = "addr-evidence-ku-travel-approval"
    exact = ProvenanceAddressResolver(graph, catalog).resolve(address_id)
    denied = ProvenanceAddressResolver(
        graph,
        catalog,
        access_policy=_DenyResource("res-authority-v2"),
    ).resolve(address_id)

    assert exact.status == "exact"
    assert tuple(item.target_id for item in exact.targets) == (
        "pol-p2",
        "pol-p5",
        "div-authority-2.1",
    )
    assert tuple(item.address_id for item in exact.member_resolutions) == (
        "addr-strong-pol-p2",
        "addr-strong-pol-p5",
        "addr-strong-auth-21",
    )
    assert denied.status == "forbidden"
    assert denied.target is None
    assert denied.targets == ()
    assert denied.member_resolutions == ()


def test_every_exact_resolver_status_is_exercised(v3_2_fixture_root) -> None:
    """Prove the six-value vocabulary represents behavior rather than constants only."""
    graph, catalog = _fixture(v3_2_fixture_root)
    results = {
        ProvenanceAddressResolver(graph, catalog)
        .resolve("addr-strong-pol-p2")
        .status,
        ProvenanceAddressResolver(graph, catalog)
        .resolve("addr-logical-policy-4.2-effective")
        .status,
        ProvenanceAddressResolver(
            graph,
            catalog,
            obsolete_address_ids=frozenset({"addr-strong-pol-p2"}),
        )
        .resolve("addr-strong-pol-p2")
        .status,
        ProvenanceAddressResolver(
            graph,
            catalog,
            access_policy=_DenyResource("res-policy-v2"),
        )
        .resolve("addr-strong-pol-p2")
        .status,
        ProvenanceAddressResolver(graph, catalog).resolve("missing").status,
    }
    second = replace(
        graph,
        resources=(
            *graph.resources,
            SourceGraphResource(
                "res-policy-v3", "b" * 64, "aster-vale-travel-policy", "3"
            ),
        ),
        divisions=(
            *graph.divisions,
            SourceGraphDivision(
                "div-policy-v3-4.2",
                "res-policy-v3",
                "section",
                None,
                (),
                (),
                "4.2",
            ),
        ),
        address_catalog=None,
    )
    results.add(
        ProvenanceAddressResolver(second, catalog)
        .resolve("addr-logical-policy-4.2-effective")
        .status
    )
    assert results == set(PROVENANCE_RESOLUTION_STATUSES)


def test_valid_direct_resolution_shapes_include_ordered_evidence_exact() -> None:
    """Accept each target-bearing form and preserve evidence member target order."""
    first_target = ProvenanceTarget(node_id="node-a")
    second_target = ProvenanceTarget(division_id="division-b")
    first = AddressResolution("member-a", "exact", target=first_target)
    second = AddressResolution("member-b", "exact", target=second_target)

    AddressResolution("single", "exact", target=first_target).validate()
    AddressResolution("logical", "redirected", target=second_target).validate()
    AddressResolution(
        "evidence",
        "exact",
        targets=(first_target, second_target),
        member_resolutions=(first, second),
    ).validate()


@pytest.mark.parametrize(
    "resolution",
    (
        AddressResolution(
            "ambiguous-target",
            "ambiguous",
            target=ProvenanceTarget(node_id="node-a"),
        ),
        AddressResolution("redirect-missing", "redirected"),
        AddressResolution(
            "redirect-candidate",
            "redirected",
            target=ProvenanceTarget(node_id="node-a"),
            candidate_targets=(ProvenanceTarget(node_id="node-b"),),
        ),
        AddressResolution(
            "forbidden-member",
            "forbidden",
            member_resolutions=(
                AddressResolution(
                    "member-a",
                    "exact",
                    target=ProvenanceTarget(node_id="node-a"),
                ),
            ),
        ),
        AddressResolution("exact-empty", "exact"),
        AddressResolution(
            "exact-both",
            "exact",
            target=ProvenanceTarget(node_id="node-a"),
            targets=(ProvenanceTarget(node_id="node-b"),),
        ),
        AddressResolution(
            "unresolved-candidate",
            "unresolved",
            candidate_targets=(ProvenanceTarget(node_id="node-a"),),
        ),
        AddressResolution(
            "long-revision",
            "unresolved",
            graph_revision="g" * 4_097,
        ),
    ),
)
def test_direct_resolution_rejects_contradictory_status_shapes(resolution) -> None:
    """Reject accepted, candidate, member, empty, and unbounded invalid combinations."""
    with pytest.raises(ProvenanceAddressValidationError):
        resolution.validate()


def test_evidence_exact_requires_member_order_and_unique_targets() -> None:
    """Reject reordered or duplicate collected targets in an exact evidence set."""
    first_target = ProvenanceTarget(node_id="node-a")
    second_target = ProvenanceTarget(node_id="node-b")
    members = (
        AddressResolution("member-a", "exact", target=first_target),
        AddressResolution("member-b", "exact", target=second_target),
    )

    with pytest.raises(ProvenanceAddressValidationError, match="ordered exact"):
        AddressResolution(
            "reordered",
            "exact",
            targets=(second_target, first_target),
            member_resolutions=members,
        ).validate()
    with pytest.raises(ProvenanceAddressValidationError, match="unique"):
        AddressResolution(
            "duplicates",
            "exact",
            targets=(first_target, first_target),
            member_resolutions=(members[0], members[0]),
        ).validate()


def test_parent_resolution_recursively_rejects_malformed_member() -> None:
    """Apply status-shape validation recursively to retained evidence explanations."""
    malformed = AddressResolution(
        "malformed-member",
        "ambiguous",
        target=ProvenanceTarget(node_id="protected-node"),
    )
    parent = AddressResolution(
        "incomplete-evidence",
        "unresolved",
        member_resolutions=(malformed,),
    )

    with pytest.raises(ProvenanceAddressValidationError, match="Ambiguous"):
        parent.validate()
