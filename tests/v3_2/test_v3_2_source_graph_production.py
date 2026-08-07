"""Production canonical projection, revision, strong-address, and boundary tests."""

from __future__ import annotations

from dataclasses import replace
import json
import re

import pytest

from cognityx_ingest import (
    GraphProjectionDescriptor,
    ProvenanceAddressResolver,
    ProvenanceTarget,
    ResourceVersionMetadata,
    SourceGraph,
    SourceGraphBuilder,
    SourceGraphValidationError,
    build_strong_address_catalog,
)


def _catalog_with_address(catalog, address):
    """Replace one generated strong address while preserving catalog identity."""
    return replace(
        catalog,
        strong_addresses=tuple(
            address if item.address_id == address.address_id else item
            for item in catalog.strong_addresses
        ),
    )


def _resolve_replaced_address(graph, catalog, address):
    """Resolve one deliberately changed address through the production resolver."""
    return ProvenanceAddressResolver(
        graph,
        _catalog_with_address(catalog, address),
    ).resolve(address.address_id)


def test_production_graph_reuses_complete_canonical_ids_without_text(
    frozen_canonical_artifact,
) -> None:
    """Project nodes/selectors/representations/bindings/activities as references only."""
    graph = SourceGraphBuilder().build((frozen_canonical_artifact,))
    payload = graph.to_json_bytes()

    assert re.fullmatch(r"sg-[0-9a-f]{64}", graph.graph_revision)
    assert {item.resource_id for item in graph.resources} == {
        item.resource_id for item in frozen_canonical_artifact.resources
    }
    assert {item.node_id for item in graph.content_nodes} == {
        item.node_id for item in frozen_canonical_artifact.content_nodes
    }
    assert {item.selector_id for item in graph.selectors} == {
        selector.selector_id
        for node in frozen_canonical_artifact.content_nodes
        for selector in node.source_selectors
    }
    assert all(
        node.content.text.encode() not in payload
        for node in frozen_canonical_artifact.content_nodes
    )
    assert all("content" not in item for item in graph.to_dict()["content_nodes"])
    assert SourceGraph.from_json_bytes(payload).to_json_bytes() == payload


def test_production_revision_is_input_order_independent_and_materially_bound(
    frozen_canonical_artifact,
) -> None:
    """Ignore metadata input order but change revision when explicit version changes."""
    resources = frozen_canonical_artifact.resources
    metadata = (
        ResourceVersionMetadata(resources[0].resource_id, "family-a", "2"),
        ResourceVersionMetadata(resources[1].resource_id, "family-b", "7"),
    )
    builder = SourceGraphBuilder()
    first = builder.build(
        (frozen_canonical_artifact,), resource_versions=metadata
    )
    reordered = builder.build(
        (frozen_canonical_artifact,), resource_versions=tuple(reversed(metadata))
    )
    changed = builder.build(
        (frozen_canonical_artifact,),
        resource_versions=(replace(metadata[0], version="3"), metadata[1]),
    )

    assert first.graph_revision == reordered.graph_revision
    assert first.to_json_bytes() == reordered.to_json_bytes()
    assert changed.graph_revision != first.graph_revision


def test_selector_fact_change_changes_production_revision(
    frozen_canonical_artifact,
) -> None:
    """Bind revision identity to selector provenance, not only resource hierarchy."""
    node = frozen_canonical_artifact.content_nodes[0]
    selector = node.source_selectors[0]
    changed_selector = replace(
        selector,
        parser_source_anchor_ids=(*selector.parser_source_anchor_ids, "new-anchor"),
    )
    changed_node = replace(
        node,
        source_selectors=(changed_selector, *node.source_selectors[1:]),
    )
    changed_artifact = replace(
        frozen_canonical_artifact,
        content_nodes=(changed_node, *frozen_canonical_artifact.content_nodes[1:]),
    )
    changed_artifact.validate()

    original = SourceGraphBuilder().build((frozen_canonical_artifact,))
    changed = SourceGraphBuilder().build((changed_artifact,))
    assert changed.graph_revision != original.graph_revision


def test_generated_catalog_contains_only_resolvable_strong_addresses(
    frozen_canonical_artifact,
) -> None:
    """Generate strong evidence where facts exist and invent no business/claim intent."""
    graph = SourceGraphBuilder().build((frozen_canonical_artifact,))
    catalog = build_strong_address_catalog(graph, (frozen_canonical_artifact,))
    resolver = ProvenanceAddressResolver(graph, catalog)

    assert len(catalog.strong_addresses) == len(
        frozen_canonical_artifact.content_nodes
    )
    assert catalog.logical_addresses == ()
    assert catalog.evidence_set_addresses == ()
    assert all(
        resolver.resolve(item.address_id).status == "exact"
        for item in catalog.strong_addresses
    )
    assert catalog.to_json_bytes() == catalog.to_json_bytes()


def test_production_selector_without_id_must_match_graph_facts_exactly(
    frozen_canonical_artifact,
) -> None:
    """Accept an omitted ID only when every remaining locator fact is graph-provable."""
    graph = SourceGraphBuilder().build((frozen_canonical_artifact,))
    catalog = build_strong_address_catalog(graph, (frozen_canonical_artifact,))
    address = next(item for item in catalog.strong_addresses if item.selectors)
    without_ids = replace(
        address,
        selectors=tuple(replace(item, selector_id=None) for item in address.selectors),
    )

    assert ProvenanceAddressResolver(graph, catalog).resolve(address.address_id).status == "exact"
    assert _resolve_replaced_address(graph, catalog, without_ids).status == "exact"


@pytest.mark.parametrize("changed_fact", ("char_start", "source_path"))
def test_production_selector_without_id_rejects_changed_locator_fact(
    frozen_canonical_artifact,
    changed_fact,
) -> None:
    """Return unresolved when an ID-less range or logical path lacks exact graph proof."""
    graph = SourceGraphBuilder().build((frozen_canonical_artifact,))
    catalog = build_strong_address_catalog(graph, (frozen_canonical_artifact,))
    address = next(item for item in catalog.strong_addresses if item.selectors)
    selector = address.selectors[0]
    if changed_fact == "char_start":
        assert selector.char_start is not None and selector.char_end is not None
        assert selector.char_start < selector.char_end
        changed = replace(selector, selector_id=None, char_start=selector.char_start + 1)
    else:
        changed = replace(selector, selector_id=None, source_path="changed/source.md")
    altered = replace(address, selectors=(changed, *address.selectors[1:]))

    assert _resolve_replaced_address(graph, catalog, altered).status == "unresolved"


def test_production_selector_id_with_changed_facts_is_unresolved(
    frozen_canonical_artifact,
) -> None:
    """Keep selector identity insufficient when its represented facts are altered."""
    graph = SourceGraphBuilder().build((frozen_canonical_artifact,))
    catalog = build_strong_address_catalog(graph, (frozen_canonical_artifact,))
    address = next(item for item in catalog.strong_addresses if item.selectors)
    selector = address.selectors[0]
    assert selector.char_start is not None and selector.char_end is not None
    assert selector.char_start < selector.char_end
    altered = replace(
        address,
        selectors=(
            replace(selector, char_start=selector.char_start + 1),
            *address.selectors[1:],
        ),
    )

    assert _resolve_replaced_address(graph, catalog, altered).status == "unresolved"


def test_selector_owned_by_another_resource_is_unresolved(
    frozen_canonical_artifact,
) -> None:
    """Reject a real graph selector when it belongs to a different source resource."""
    graph = SourceGraphBuilder().build((frozen_canonical_artifact,))
    catalog = build_strong_address_catalog(graph, (frozen_canonical_artifact,))
    address = next(item for item in catalog.strong_addresses if item.selectors)
    foreign = next(
        item
        for item in catalog.strong_addresses
        if item.resource_id != address.resource_id and item.selectors
    )
    altered = replace(address, selectors=foreign.selectors)

    assert _resolve_replaced_address(graph, catalog, altered).status == "unresolved"


def test_graph_projection_descriptor_is_lineage_only() -> None:
    """Validate a replaceable projection descriptor without constructing a graph."""
    descriptor = GraphProjectionDescriptor(
        projection_id="projection-1",
        source_graph_revision="sg-" + "a" * 64,
        adapter_id="future-adapter",
        adapter_version="1",
        configuration_sha256="b" * 64,
        support_ids=("node-1", "node-2"),
        retention_policy="derived",
    )
    descriptor.validate()


def test_production_reader_rejects_noncanonical_record_order(
    frozen_canonical_artifact,
) -> None:
    """Require persisted production collections to use deterministic ID ordering."""
    graph = SourceGraphBuilder().build((frozen_canonical_artifact,))
    value = graph.to_dict()
    value["resources"] = list(reversed(value["resources"]))
    with pytest.raises(SourceGraphValidationError, match="deterministically ordered"):
        SourceGraph.from_json_bytes(json.dumps(value).encode())
