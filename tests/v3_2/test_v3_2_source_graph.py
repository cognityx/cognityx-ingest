"""Frozen Source Graph fidelity, traversal, and non-gold safety tests."""

from __future__ import annotations

import json

from cognityx_ingest import SOURCE_GRAPH_SCHEMA, SourceGraphRepository


def test_frozen_source_graph_loads_without_fact_reinterpretation(
    v3_2_fixture_root,
) -> None:
    """Preserve every authoritative compact record and exact frozen revision."""
    expected = json.loads(
        (v3_2_fixture_root / "expected" / "source_graph.json").read_text()
    )
    graph = SourceGraphRepository.from_fixture(v3_2_fixture_root).load("sg-rev-001")

    assert graph.schema == SOURCE_GRAPH_SCHEMA
    assert graph.graph_revision == "sg-rev-001"
    assert graph.to_dict() == expected
    assert [item.resource_id for item in graph.resources] == [
        "res-policy-v2",
        "res-authority-v2",
    ]
    assert [item.presentation_unit_id for item in graph.presentation_units] == [
        "pu-policy-document",
        "pu-authority-document",
    ]
    assert len(graph.divisions) == 5


def test_direct_node_ownership_and_cross_resource_relations_are_preserved(
    v3_2_fixture_root,
) -> None:
    """Keep deepest ownership and both explicit cross-resource edges unchanged."""
    graph = SourceGraphRepository.from_fixture(v3_2_fixture_root).load("sg-rev-001")
    owners = {
        node_id: division.division_id
        for division in graph.divisions
        for node_id in division.direct_node_ids
    }

    assert owners["pol-p2"] == "div-policy-4.2"
    assert owners["auth-p2"] == "div-authority-2.1"
    assert graph.get_relation("rel-policy-to-authority").target_id == (
        "div-authority-2.1"
    )
    assert graph.get_relation("rel-authority-defines").target_id == (
        "div-policy-4.2"
    )
    assert [item.relation_id for item in graph.outgoing("pol-p4")] == [
        "rel-policy-to-authority"
    ]
    assert [item.relation_id for item in graph.incoming("div-policy-4.2")] == [
        "rel-7.1-exception",
        "rel-authority-defines",
    ]


def test_ambiguous_relation_remains_targetless_candidate_bearing_and_non_gold(
    v3_2_fixture_root,
) -> None:
    """Retain ambiguity for audit while excluding it from default gold closure."""
    graph = SourceGraphRepository.from_fixture(v3_2_fixture_root).load("sg-rev-001")
    relation = graph.get_relation("rel-ambiguous-example")

    assert relation.target_id is None
    assert relation.status == "ambiguous"
    assert relation.epistemic_state == "ambiguous"
    assert relation.gold_eligible is False
    assert relation.candidate_target_ids == (
        "div-policy-7.1",
        "div-authority-2.1",
    )
    assert graph.outgoing("pol-p1") == ()
    assert graph.outgoing("pol-p1", gold_only=False) == (relation,)


def test_graph_serialization_is_deterministic_and_text_free(v3_2_fixture_root) -> None:
    """Serialize identical bytes repeatedly without any fixture source passage."""
    graph = SourceGraphRepository.from_fixture(v3_2_fixture_root).load("sg-rev-001")
    payload = graph.to_json_bytes()
    policy_text = (
        v3_2_fixture_root / "sources" / "segmentation_policy.md"
    ).read_text()

    assert payload == graph.to_json_bytes()
    assert policy_text.encode() not in payload
    assert b'"text"' not in payload
