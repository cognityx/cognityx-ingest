"""Strict Source Graph JSON, hierarchy, reference, and revision failure tests."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json

import pytest

from cognityx_ingest import (
    ProvenanceTarget,
    SourceGraph,
    SourceGraphBuilder,
    SourceGraphReferenceError,
    SourceGraphRepository,
    SourceGraphRevisionError,
    SourceGraphValidationError,
)
from cognityx_ingest.source_graph import (
    SourceGraphRepresentation,
    _graph_subject_resource_id,
    _validate_graph_records,
)


def _payload(v3_2_fixture_root) -> dict[str, object]:
    """Return an independent mutable copy of the frozen compact graph."""
    return json.loads(
        (v3_2_fixture_root / "expected" / "source_graph.json").read_text()
    )


def _load(value: dict[str, object]) -> SourceGraph:
    """Pass a mutated mapping through the real strict compact reader."""
    return SourceGraph.from_json_bytes(
        json.dumps(value).encode(), compact_fixture=True
    )


def test_parent_child_reciprocity_is_required(v3_2_fixture_root) -> None:
    """Reject one-sided hierarchy instead of repairing or guessing a parent."""
    value = _payload(v3_2_fixture_root)
    value["divisions"][0]["child_division_ids"] = ["div-policy-7.1"]
    with pytest.raises(SourceGraphValidationError, match="reciprocal"):
        _load(value)


def test_division_hierarchy_cycles_are_rejected(v3_2_fixture_root) -> None:
    """Reject reciprocal back-edges before any subtree traversal is available."""
    value = _payload(v3_2_fixture_root)
    root = value["divisions"][0]
    child = value["divisions"][1]
    root["parent_division_id"] = child["division_id"]
    child["child_division_ids"] = [root["division_id"]]
    with pytest.raises(SourceGraphValidationError, match="cycle"):
        _load(value)


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    (
        ("source_id", "missing-source", "source is missing"),
        ("target_id", "missing-target", "target is missing"),
    ),
)
def test_dangling_concrete_relation_endpoints_are_rejected(
    v3_2_fixture_root, field, replacement, message
) -> None:
    """Require both endpoint IDs to resolve, including cross-resource targets."""
    value = _payload(v3_2_fixture_root)
    value["relations"][0][field] = replacement
    with pytest.raises(SourceGraphReferenceError, match=message):
        _load(value)


def test_dangling_candidate_target_is_rejected(v3_2_fixture_root) -> None:
    """Require every ambiguous candidate to remain auditable in the same graph."""
    value = _payload(v3_2_fixture_root)
    value["relations"][-1]["candidate_target_ids"][0] = "missing-candidate"
    with pytest.raises(SourceGraphReferenceError, match="candidate target"):
        _load(value)


def test_duplicate_ids_are_rejected(v3_2_fixture_root) -> None:
    """Reject duplicate persisted record identities before index overwrite."""
    value = _payload(v3_2_fixture_root)
    value["resources"].append(deepcopy(value["resources"][0]))
    with pytest.raises(SourceGraphValidationError, match="resource IDs"):
        _load(value)


def test_duplicate_json_keys_are_rejected() -> None:
    """Reject duplicate object keys at decode time rather than last-key wins."""
    payload = b'{"schema":"a","schema":"b"}'
    with pytest.raises(SourceGraphValidationError, match="duplicate JSON key"):
        SourceGraph.from_json_bytes(payload, compact_fixture=True)


def test_unknown_or_partially_mixed_fields_are_rejected(v3_2_fixture_root) -> None:
    """Keep compact and complete production persistence as two closed shapes."""
    value = _payload(v3_2_fixture_root)
    value["content_nodes"] = []
    with pytest.raises(SourceGraphValidationError, match="fields"):
        _load(value)


def test_unsafe_relation_can_never_be_gold(v3_2_fixture_root) -> None:
    """Reject persisted ambiguity that attempts to claim gold eligibility."""
    value = _payload(v3_2_fixture_root)
    value["relations"][-1]["gold_eligible"] = True
    with pytest.raises(SourceGraphValidationError, match="gold eligible"):
        _load(value)


def test_repository_rejects_same_revision_with_different_content(
    v3_2_fixture_root,
) -> None:
    """Protect immutable revision identity against a conflicting second graph."""
    graph = SourceGraphRepository.from_fixture(v3_2_fixture_root).load("sg-rev-001")
    changed_relation = replace(graph.relations[0], relation_type="changed")
    changed = replace(
        graph,
        relations=(changed_relation, *graph.relations[1:]),
        address_catalog=None,
    )
    with pytest.raises(SourceGraphRevisionError, match="Conflicting"):
        SourceGraphRepository.from_graphs((graph, changed))


def test_repository_missing_revision_fails_typed(v3_2_fixture_root) -> None:
    """Never substitute a latest or lexical revision for an absent exact request."""
    repository = SourceGraphRepository.from_fixture(v3_2_fixture_root)
    with pytest.raises(SourceGraphRevisionError, match="Unknown"):
        repository.load("sg-missing")


def _representation(representation_id: str, subject_id: str) -> SourceGraphRepresentation:
    """Create one payload-free representation record for subject-chain tests."""
    return SourceGraphRepresentation(
        representation_id=representation_id,
        subject_id=subject_id,
        representation_type="test-view",
        artifact_id=None,
        selector_ids=(),
        caption_node_id=None,
    )


def test_representation_self_cycle_fails_typed_without_recursion(
    frozen_canonical_artifact,
) -> None:
    """Reject A-to-A both at graph validation and defensive subject traversal."""
    base = SourceGraphBuilder().build((frozen_canonical_artifact,))
    graph = replace(
        base,
        representations=(_representation("rep-a", "rep-a"),),
        address_catalog=None,
    )

    with pytest.raises(SourceGraphValidationError, match="cycle"):
        graph.validate()
    indexes = _validate_graph_records(graph)
    with pytest.raises(SourceGraphValidationError, match="cycle"):
        _graph_subject_resource_id("rep-a", indexes)
    with pytest.raises(SourceGraphValidationError, match="cycle"):
        graph.target_resource_id(ProvenanceTarget(representation_id="rep-a"))


def test_persisted_representation_multi_record_cycle_fails_before_use(
    frozen_canonical_artifact,
) -> None:
    """Reject persisted A-to-B-to-A lineage at the strict production JSON reader."""
    base = SourceGraphBuilder().build((frozen_canonical_artifact,))
    terminal = base.content_nodes[0].node_id
    valid = replace(
        base,
        representations=(
            _representation("rep-a", "rep-b"),
            _representation("rep-b", terminal),
        ),
        address_catalog=None,
    )
    value = valid.to_dict()
    value["representations"][1]["subject_id"] = "rep-a"

    with pytest.raises(SourceGraphValidationError, match="cycle"):
        SourceGraph.from_json_bytes(json.dumps(value).encode())


@pytest.mark.parametrize("terminal_kind", ("node", "division"))
def test_nested_representation_chain_resolves_terminal_resource_deterministically(
    frozen_canonical_artifact,
    terminal_kind,
) -> None:
    """Allow acyclic A-to-B chains ending at canonical nodes or divisions."""
    base = SourceGraphBuilder().build((frozen_canonical_artifact,))
    if terminal_kind == "node":
        terminal_id = base.content_nodes[0].node_id
        expected_resource_id = base.content_nodes[0].resource_id
    else:
        terminal_id = base.divisions[0].division_id
        expected_resource_id = base.divisions[0].resource_id
    graph = replace(
        base,
        representations=(
            _representation("rep-a", "rep-b"),
            _representation("rep-b", terminal_id),
        ),
        address_catalog=None,
    )
    target = ProvenanceTarget(representation_id="rep-a")

    graph.validate()
    assert graph.target_resource_id(target) == expected_resource_id
    assert graph.target_resource_id(target) == expected_resource_id
