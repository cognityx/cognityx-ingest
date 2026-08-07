"""Prove production Source Graph revisions identify their exact persisted facts.

These T08 trust-boundary tests exercise builder output, direct immutable
construction, strict JSON reading, repository registration, and provenance
resolver composition. The frozen compact fixture remains a byte-bound
compatibility input; no test rewrites it or introduces parser, model, network,
database, embedding, semantic graph, T09, or T10 behavior.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import re

import pytest

from cognityx_ingest import (
    ProvenanceAddressResolver,
    SourceGraph,
    SourceGraphBuilder,
    SourceGraphRepository,
    SourceGraphRevisionError,
    build_strong_address_catalog,
)


_RELEASED_PRODUCTION_REVISION = (
    "sg-6be5cde005d435c39dc29329bcecae8ab9ea1a90cc7a0f9a9dcb2e3dcd7cd995"
)
_FROZEN_GRAPH_SHA256 = (
    "0a863a78e28563b45b702d75c9d07bc8a4f40be37d3eb90a647522bad5795d06"
)


def _assert_stale_revision(graph: SourceGraph) -> None:
    """Require one structurally valid changed graph to reject its old revision.

    Material-fact tests call this after changing facts while deliberately
    retaining a builder-produced revision. Public validation must reach the
    content-fingerprint comparison and raise ``SourceGraphRevisionError``. The
    helper performs no repair, rehashing, I/O, or mutation and therefore cannot
    hide invalid test construction behind another public production API.
    """
    with pytest.raises(SourceGraphRevisionError, match="persisted facts"):
        graph.validate()


def test_builder_revision_shape_value_validation_and_reload_are_exact(
    frozen_canonical_artifact,
) -> None:
    """Preserve the released hash algorithm and byte-identical strict round trip."""
    graph = SourceGraphBuilder().build((frozen_canonical_artifact,))
    payload = graph.to_json_bytes()

    assert re.fullmatch(r"sg-[0-9a-f]{64}", graph.graph_revision)
    assert graph.graph_revision == _RELEASED_PRODUCTION_REVISION
    graph.validate()
    restored = SourceGraph.from_json_bytes(payload)
    assert restored.to_json_bytes() == payload
    assert restored.graph_revision == graph.graph_revision


def test_legitimate_builder_relation_change_gets_another_revision(
    frozen_canonical_artifact,
) -> None:
    """Calculate a new fingerprint when explicit canonical relation facts change."""
    relation = frozen_canonical_artifact.relations[0]
    changed = replace(
        frozen_canonical_artifact,
        relations=(
            replace(relation, relation_type="amended-explicit-reference"),
            *frozen_canonical_artifact.relations[1:],
        ),
    )
    changed.validate()

    original_graph = SourceGraphBuilder().build((frozen_canonical_artifact,))
    changed_graph = SourceGraphBuilder().build((changed,))
    assert changed_graph.graph_revision != original_graph.graph_revision
    changed_graph.validate()


def test_stale_revision_rejects_changed_relation_type_and_target(
    frozen_canonical_artifact,
) -> None:
    """Bind both relation meaning and its concrete accepted target into identity."""
    graph = SourceGraphBuilder().build((frozen_canonical_artifact,))
    relation = next(item for item in graph.relations if item.target_id is not None)
    other_target = next(
        item.division_id
        for item in graph.divisions
        if item.division_id != relation.target_id
    )
    changed_type = replace(
        graph,
        relations=tuple(
            replace(item, relation_type="changed-reference")
            if item.relation_id == relation.relation_id
            else item
            for item in graph.relations
        ),
    )
    changed_target = replace(
        graph,
        relations=tuple(
            replace(item, target_id=other_target)
            if item.relation_id == relation.relation_id
            else item
            for item in graph.relations
        ),
    )

    _assert_stale_revision(changed_type)
    _assert_stale_revision(changed_target)


def test_stale_revision_rejects_changed_selector_fact(
    frozen_canonical_artifact,
) -> None:
    """Bind safe locator metadata even when selector and node IDs are unchanged."""
    graph = SourceGraphBuilder().build((frozen_canonical_artifact,))
    selector = graph.selectors[0]
    changed = replace(
        graph,
        selectors=(
            replace(selector, source_path="changed/logical-source.md"),
            *graph.selectors[1:],
        ),
    )
    _assert_stale_revision(changed)


def test_stale_revision_rejects_resource_hash_family_and_version_changes(
    frozen_canonical_artifact,
) -> None:
    """Bind immutable bytes and explicit business version facts into identity."""
    graph = SourceGraphBuilder().build((frozen_canonical_artifact,))
    resource = graph.resources[0]
    changed_hash = replace(
        graph,
        resources=(
            replace(resource, source_sha256="0" * 64),
            *graph.resources[1:],
        ),
    )
    changed_business_identity = replace(
        graph,
        resources=(
            replace(resource, family_id="policy-family", version="9"),
            *graph.resources[1:],
        ),
    )

    _assert_stale_revision(changed_hash)
    _assert_stale_revision(changed_business_identity)


def test_stale_revision_rejects_valid_hierarchy_change(
    frozen_canonical_artifact,
) -> None:
    """Bind reciprocal parent/child structure even when all IDs still resolve."""
    graph = SourceGraphBuilder().build((frozen_canonical_artifact,))
    root = next(item for item in graph.divisions if item.division_id == "div-policy-root")
    section = next(item for item in graph.divisions if item.division_id == "div-policy-4.2")
    nested = next(item for item in graph.divisions if item.division_id == "div-policy-7.1")
    replacements = {
        root.division_id: replace(
            root,
            child_division_ids=tuple(
                item for item in root.child_division_ids if item != nested.division_id
            ),
        ),
        section.division_id: replace(
            section, child_division_ids=(nested.division_id,)
        ),
        nested.division_id: replace(
            nested, parent_division_id=section.division_id
        ),
    }
    changed = replace(
        graph,
        divisions=tuple(
            replacements.get(item.division_id, item) for item in graph.divisions
        ),
    )
    _assert_stale_revision(changed)


def test_stale_revision_rejects_valid_direct_node_ownership_change(
    frozen_canonical_artifact,
) -> None:
    """Bind a node's deepest owner even when it stays in the same resource."""
    graph = SourceGraphBuilder().build((frozen_canonical_artifact,))
    node = next(item for item in graph.content_nodes if item.node_id == "pol-p2")
    old_owner = next(
        item for item in graph.divisions if item.division_id == node.owner_division_id
    )
    new_owner = next(
        item for item in graph.divisions if item.division_id == "div-policy-7.1"
    )
    divisions = tuple(
        replace(
            item,
            direct_node_ids=tuple(
                value for value in item.direct_node_ids if value != node.node_id
            ),
        )
        if item.division_id == old_owner.division_id
        else replace(item, direct_node_ids=(*item.direct_node_ids, node.node_id))
        if item.division_id == new_owner.division_id
        else item
        for item in graph.divisions
    )
    nodes = tuple(
        replace(item, owner_division_id=new_owner.division_id)
        if item.node_id == node.node_id
        else item
        for item in graph.content_nodes
    )
    _assert_stale_revision(replace(graph, divisions=divisions, content_nodes=nodes))


@pytest.mark.parametrize(
    "revision",
    (
        "sg-" + "0" * 64,
        "sg-" + "A" * 64,
        "sg-" + "a" * 63,
        "pending",
    ),
)
def test_direct_and_strict_reader_reject_forged_or_malformed_revision(
    frozen_canonical_artifact,
    revision: str,
) -> None:
    """Reject malformed revision values at both public production boundaries."""
    graph = SourceGraphBuilder().build((frozen_canonical_artifact,))
    with pytest.raises(SourceGraphRevisionError):
        replace(graph, graph_revision=revision).validate()
    value = graph.to_dict()
    value["graph_revision"] = revision
    with pytest.raises(SourceGraphRevisionError):
        SourceGraph.from_json_bytes(json.dumps(value).encode("utf-8"))


@pytest.mark.parametrize("replace_with_random_revision", (False, True))
def test_strict_reader_rejects_edited_production_json_revision(
    frozen_canonical_artifact,
    replace_with_random_revision: bool,
) -> None:
    """Reject persisted fact edits with either the old or another forged digest."""
    graph = SourceGraphBuilder().build((frozen_canonical_artifact,))
    value = graph.to_dict()
    value["relations"][0]["relation_type"] = "tampered-persisted-reference"
    if replace_with_random_revision:
        value["graph_revision"] = "sg-" + "1" * 64
    with pytest.raises(SourceGraphRevisionError):
        SourceGraph.from_json_bytes(json.dumps(value).encode("utf-8"))


def test_repository_rejects_invalid_production_revision_before_registration(
    frozen_canonical_artifact,
) -> None:
    """Prevent invalid content identity from entering repository lookup state."""
    graph = SourceGraphBuilder().build((frozen_canonical_artifact,))
    invalid = replace(graph, graph_revision="sg-" + "0" * 64)
    with pytest.raises(SourceGraphRevisionError):
        SourceGraphRepository.from_graphs((invalid,))


def test_forged_graph_and_matching_forged_addresses_cannot_resolve(
    frozen_canonical_artifact,
) -> None:
    """Require graph integrity before same-forgery strong-address composition."""
    graph = SourceGraphBuilder().build((frozen_canonical_artifact,))
    catalog = build_strong_address_catalog(graph, (frozen_canonical_artifact,))
    forged_revision = "sg-" + "0" * 64
    forged_graph = replace(
        graph,
        graph_revision=forged_revision,
        relations=(
            replace(graph.relations[0], relation_type="forged-reference"),
            *graph.relations[1:],
        ),
    )
    forged_catalog = replace(
        catalog,
        strong_addresses=tuple(
            replace(item, graph_revision=forged_revision)
            for item in catalog.strong_addresses
        ),
    )
    with pytest.raises(SourceGraphRevisionError):
        ProvenanceAddressResolver(forged_graph, forged_catalog)


def test_frozen_compact_revision_bytes_and_exact_resolution_remain_unchanged(
    v3_2_fixture_root,
) -> None:
    """Keep `sg-rev-001` and its frozen strong address as compatibility truth."""
    graph_path = v3_2_fixture_root / "expected" / "source_graph.json"
    payload = graph_path.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == _FROZEN_GRAPH_SHA256
    graph = SourceGraph.from_json_bytes(payload, compact_fixture=True)
    repository = SourceGraphRepository.from_fixture(v3_2_fixture_root)
    loaded = repository.load("sg-rev-001")

    assert graph.graph_revision == loaded.graph_revision == "sg-rev-001"
    result = ProvenanceAddressResolver(loaded).resolve("addr-strong-pol-p2")
    assert result.status == "exact"
    assert result.target is not None and result.target.node_id == "pol-p2"


def test_source_graph_integrity_adds_no_adjacent_architecture() -> None:
    """Keep T08 hashing free of graph databases, models, embeddings, and parsers."""
    import cognityx_ingest.source_graph as source_graph_module

    assert not hasattr(source_graph_module, "GraphDatabase")
    assert not hasattr(source_graph_module, "SemanticKnowledgeGraph")
    assert not hasattr(source_graph_module, "EmbeddingClient")
    assert not hasattr(source_graph_module, "ParserRouter")
