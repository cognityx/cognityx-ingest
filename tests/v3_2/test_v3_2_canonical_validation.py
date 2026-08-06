"""Typed validation tests for the complete v3.2 canonical-content aggregate.

The suite mutates immutable fixture-derived records with ``dataclasses.replace``
so each case isolates one violated invariant. Public validation must reject bad
hierarchy, ownership, references, hashes, selectors, order, and copied-text fields
without leaking raw ``KeyError`` or ``AssertionError`` exceptions.
"""

from __future__ import annotations

from dataclasses import replace
import json

import pytest

from cognityx_ingest import (
    CanonicalContentArtifact,
    CanonicalContentValidationError,
    CanonicalOwnershipError,
    CanonicalReferenceError,
    CanonicalRepresentation,
    CanonicalText,
    ProcessingActivity,
)


def test_complete_artifact_round_trips_through_strict_untrusted_reader(
    frozen_canonical_artifact: CanonicalContentArtifact,
) -> None:
    """Parse deterministic JSON back into the exact immutable public aggregate."""
    serialized = frozen_canonical_artifact.to_json_bytes()
    restored = CanonicalContentArtifact.from_dict(json.loads(serialized))
    assert restored == frozen_canonical_artifact
    assert restored.to_json_bytes() == serialized


def test_division_hierarchy_cycle_raises_typed_ownership_error(
    frozen_canonical_artifact: CanonicalContentArtifact,
) -> None:
    """Detect a reciprocal two-node cycle before subtree reconstruction can loop."""
    divisions = {item.division_id: item for item in frozen_canonical_artifact.divisions}
    root = divisions["div-policy-root"]
    child = divisions["div-policy-4.2"]
    replacements = {
        root.division_id: replace(root, parent_division_id=child.division_id),
        child.division_id: replace(
            child,
            child_division_ids=(root.division_id,),
        ),
    }
    invalid = replace(
        frozen_canonical_artifact,
        divisions=tuple(replacements.get(item.division_id, item) for item in frozen_canonical_artifact.divisions),
    )
    with pytest.raises(CanonicalOwnershipError, match="cycle"):
        invalid.validate()


def test_missing_resource_reference_raises_typed_reference_error(
    frozen_canonical_artifact: CanonicalContentArtifact,
) -> None:
    """Reject a presentation unit whose resource cannot resolve."""
    first = frozen_canonical_artifact.presentation_units[0]
    invalid = replace(
        frozen_canonical_artifact,
        presentation_units=(
            replace(first, resource_id="res-missing"),
            *frozen_canonical_artifact.presentation_units[1:],
        ),
    )
    with pytest.raises(CanonicalReferenceError, match="missing resource"):
        invalid.validate()


def test_missing_owner_raises_typed_ownership_error(
    frozen_canonical_artifact: CanonicalContentArtifact,
) -> None:
    """Reject a node that claims an absent deepest direct owner."""
    node = frozen_canonical_artifact.content_nodes[0]
    invalid = replace(
        frozen_canonical_artifact,
        content_nodes=(
            replace(node, owner_division_id="div-missing"),
            *frozen_canonical_artifact.content_nodes[1:],
        ),
    )
    with pytest.raises(CanonicalOwnershipError):
        invalid.validate()


def test_content_hash_mismatch_raises_typed_validation_error(
    frozen_canonical_artifact: CanonicalContentArtifact,
) -> None:
    """Reject text whose stored SHA-256 is not over its exact UTF-8 bytes."""
    node = frozen_canonical_artifact.content_nodes[0]
    invalid_node = replace(
        node,
        content=CanonicalText(text=node.content.text, sha256="0" * 64),
    )
    invalid = replace(
        frozen_canonical_artifact,
        content_nodes=(invalid_node, *frozen_canonical_artifact.content_nodes[1:]),
    )
    with pytest.raises(CanonicalContentValidationError, match="SHA-256"):
        invalid.validate()


def test_invalid_character_range_raises_typed_validation_error(
    frozen_canonical_artifact: CanonicalContentArtifact,
) -> None:
    """Reject reversed real offsets without trying to repair or infer them."""
    node = frozen_canonical_artifact.content_nodes[0]
    selector = node.source_selectors[0]
    invalid_node = replace(
        node,
        source_selectors=(replace(selector, char_start=40, char_end=10),),
    )
    invalid = replace(
        frozen_canonical_artifact,
        content_nodes=(invalid_node, *frozen_canonical_artifact.content_nodes[1:]),
    )
    with pytest.raises(CanonicalContentValidationError, match="range"):
        invalid.validate()


def test_invalid_bbox_length_fails_during_strict_deserialization(
    frozen_canonical_artifact: CanonicalContentArtifact,
) -> None:
    """Reject malformed geometry before constructing a typed SourceSelector."""
    payload = frozen_canonical_artifact.to_dict()
    payload["content_nodes"][0]["source_selectors"][0]["bbox"] = [0, 1, 2]
    with pytest.raises(CanonicalContentValidationError, match="four numbers"):
        CanonicalContentArtifact.from_dict(payload)


def test_invalid_bbox_length_in_typed_record_raises_typed_error(
    frozen_canonical_artifact: CanonicalContentArtifact,
) -> None:
    """Apply the JSON trust rule to malformed directly constructed records too."""
    node = frozen_canonical_artifact.content_nodes[0]
    selector = node.source_selectors[0]
    invalid_node = replace(
        node,
        source_selectors=(replace(selector, bbox=(0.0, 1.0, 2.0)),),
    )
    invalid = replace(
        frozen_canonical_artifact,
        content_nodes=(invalid_node, *frozen_canonical_artifact.content_nodes[1:]),
    )
    with pytest.raises(CanonicalContentValidationError, match="four numbers"):
        invalid.validate()


def test_selector_missing_every_source_locator_raises_typed_error(
    frozen_canonical_artifact: CanonicalContentArtifact,
) -> None:
    """Reject a selector that has no page, logical path, or retained source anchor."""
    node = frozen_canonical_artifact.content_nodes[0]
    selector = replace(
        node.source_selectors[0],
        presentation_unit_id=None,
        source_path=None,
        char_start=None,
        char_end=None,
        bbox=None,
        source_anchor_ids=(),
    )
    invalid_node = replace(node, source_selectors=(selector,))
    invalid = replace(
        frozen_canonical_artifact,
        content_nodes=(invalid_node, *frozen_canonical_artifact.content_nodes[1:]),
    )
    with pytest.raises(CanonicalContentValidationError, match="no source locator"):
        invalid.validate()


def test_representation_missing_subject_raises_typed_reference_error(
    frozen_canonical_artifact: CanonicalContentArtifact,
) -> None:
    """Reject a non-text representation detached from every canonical subject."""
    representation = CanonicalRepresentation(
        representation_id="rep-missing-subject",
        subject_id="node-missing",
        representation_type="figure",
        media_type="image/png",
    )
    invalid = replace(
        frozen_canonical_artifact,
        representations=(representation,),
    )
    with pytest.raises(CanonicalReferenceError, match="subject"):
        invalid.validate()


def test_invalid_title_node_reference_raises_typed_reference_error(
    frozen_canonical_artifact: CanonicalContentArtifact,
) -> None:
    """Reject a Division title that does not resolve to canonical text."""
    first = frozen_canonical_artifact.divisions[0]
    invalid = replace(
        frozen_canonical_artifact,
        divisions=(
            replace(first, title_node_id="node-missing"),
            *frozen_canonical_artifact.divisions[1:],
        ),
    )
    with pytest.raises(CanonicalReferenceError, match="title"):
        invalid.validate()


def test_unknown_text_field_is_rejected_by_strict_schema_shape(
    frozen_canonical_artifact: CanonicalContentArtifact,
) -> None:
    """Prevent copied subtree text from hiding in a Division extension field."""
    payload = frozen_canonical_artifact.to_dict()
    payload["divisions"][0]["text"] = frozen_canonical_artifact.content_nodes[0].content.text
    with pytest.raises(CanonicalContentValidationError, match="unknown=text"):
        CanonicalContentArtifact.from_dict(payload)


def test_nondeterministic_top_level_order_is_rejected(
    frozen_canonical_artifact: CanonicalContentArtifact,
) -> None:
    """Reject equivalent resources serialized in a different byte-producing order."""
    invalid = replace(
        frozen_canonical_artifact,
        resources=tuple(reversed(frozen_canonical_artifact.resources)),
    )
    with pytest.raises(CanonicalContentValidationError, match="deterministic"):
        invalid.validate()


def test_processing_activity_missing_artifact_raises_typed_reference_error(
    frozen_canonical_artifact: CanonicalContentArtifact,
) -> None:
    """Reject lineage that names an input or output artifact absent from the model."""
    activity = ProcessingActivity(
        activity_id="activity-missing-artifact",
        activity_type="canonical-content-build",
        run_id="run-fixture",
        correlation_id="cor-fixture",
        method="fixture",
        parser_ids=(),
        input_artifact_ids=("artifact-missing",),
        output_artifact_ids=(),
    )
    invalid = replace(
        frozen_canonical_artifact,
        processing_activities=(activity,),
    )
    with pytest.raises(CanonicalReferenceError, match="artifact is missing"):
        invalid.validate()
