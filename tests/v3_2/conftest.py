"""Shared fixtures for the frozen v3.2 contract and task implementations.

The module exists so every focused test reads the same frozen fixture tree and
the same tracked design-input directory. The design principle is explicit
fixture access: tests should state exactly which frozen input they use instead
of discovering files implicitly. These fixtures are used only by the v3.2 T00
tests, not by production ingest code. T02 composes the authoritative canonical
resource/node projection with frozen Division truth from the Source Graph file;
this test-only composition does not implement the future T08 graph repository.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cognityx_ingest import (
    CANONICAL_CONTENT_SCHEMA_VERSION,
    CanonicalContentArtifact,
    CanonicalRelation,
    CanonicalResource,
    CanonicalText,
    ContentNode,
    Division,
    PresentationUnit,
    SourceSelector,
)

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "v3_2_focused"
DESIGN_INPUT_ROOT = Path(__file__).resolve().parents[2] / "design_input" / "v3_2"


@pytest.fixture(scope="session")
def v3_2_fixture_root() -> Path:
    """Return the installed v3.2 delta fixture root used by focused tests."""
    return FIXTURE_ROOT


@pytest.fixture(scope="session")
def design_input_v3_2_root() -> Path:
    """Return the tracked frozen design-input directory for checksum tests."""
    return DESIGN_INPUT_ROOT


@pytest.fixture(scope="session")
def v3_2_manifest(v3_2_fixture_root: Path) -> dict[str, object]:
    """Load the fixture manifest once so tests share the same contract input."""
    path = v3_2_fixture_root / "fixture_manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def provenance_fixture_root() -> Path:
    """Return the existing provenance v1 fixture root reused by v3.2."""
    return Path(__file__).resolve().parents[1] / "fixtures" / "provenance_v1"


@pytest.fixture(scope="session")
def provenance_pdf(provenance_fixture_root: Path) -> Path:
    """Return the frozen base PDF that T00 must reuse without duplication."""
    return provenance_fixture_root / "main_policy_v2.pdf"


@pytest.fixture(scope="session")
def frozen_canonical_artifact(
    v3_2_fixture_root: Path,
) -> CanonicalContentArtifact:
    """Compose and validate T02 records from two unchanged frozen projections.

    Canonical fixture resources, nodes, text, hashes, and selectors remain source
    truth. The source-graph fixture contributes only presentation and Division
    facts required to complete this aggregate; no graph traversal service or
    generated expected data is introduced.
    """
    canonical = json.loads(
        (v3_2_fixture_root / "expected" / "canonical_content.json").read_text(
            encoding="utf-8"
        )
    )
    graph = json.loads(
        (v3_2_fixture_root / "expected" / "source_graph.json").read_text(
            encoding="utf-8"
        )
    )
    resources = tuple(
        sorted(
            (
                CanonicalResource(
                    resource_id=item["resource_id"],
                    source_asset_id=item["resource_id"],
                    source_sha256=item["source_sha256"],
                    media_type="text/markdown",
                    original_filename=Path(item["source_path"]).name,
                    logical_uri=f"fixture://{item['source_path']}",
                )
                for item in canonical["resources"]
            ),
            key=lambda item: item.resource_id,
        )
    )
    unit_by_resource = {
        item["resource_id"]: item["presentation_unit_id"]
        for item in graph["presentation_units"]
    }
    presentation_units = tuple(
        PresentationUnit(
            presentation_unit_id=item["presentation_unit_id"],
            resource_id=item["resource_id"],
            unit_type=item["unit_type"],
            sequence_number=index,
        )
        for index, item in enumerate(graph["presentation_units"])
    )
    node_kind = {item["node_id"]: item["node_kind"] for item in canonical["content_nodes"]}
    divisions = tuple(
        Division(
            division_id=item["division_id"],
            resource_id=item["resource_id"],
            division_role=item["division_role"],
            parent_division_id=item.get("parent_division_id"),
            child_division_ids=tuple(item["child_division_ids"]),
            title_node_id=next(
                (
                    node_id
                    for node_id in item["direct_node_ids"]
                    if node_kind[node_id] == "heading"
                ),
                None,
            ),
            number=item.get("number"),
            label=None,
            direct_node_ids=tuple(item["direct_node_ids"]),
            sequence_number=index,
        )
        for index, item in enumerate(graph["divisions"])
    )
    content_nodes = tuple(
        ContentNode(
            node_id=item["node_id"],
            resource_id=item["resource_id"],
            owner_division_id=item["owner_division_id"],
            node_kind=item["node_kind"],
            content=CanonicalText(**item["content"]),
            source_selectors=tuple(
                SourceSelector(
                    selector_id=f"{item['node_id']}:selector:{selector_index}",
                    selector_type=selector["selector_type"],
                    resource_id=item["resource_id"],
                    presentation_unit_id=unit_by_resource[item["resource_id"]],
                    source_path=selector["source_path"],
                    char_start=selector["char_start"],
                    char_end=selector["char_end"],
                )
                for selector_index, selector in enumerate(item["source_selectors"])
            ),
            sequence_number=index,
        )
        for index, item in enumerate(canonical["content_nodes"])
    )
    canonical_ids = {
        *(item.node_id for item in content_nodes),
        *(item.division_id for item in divisions),
    }
    node_ids = {item.node_id for item in content_nodes}
    relations = tuple(
        sorted(
            (
                CanonicalRelation(
                    relation_id=item["relation_id"],
                    source_id=item["source_id"],
                    target_id=item.get("target_id"),
                    relation_type=item["relation_type"],
                    status=item["status"],
                    epistemic_state=item["epistemic_state"],
                    evidence_node_ids=(
                        (item["source_id"],)
                        if item["source_id"] in node_ids
                        else ()
                    ),
                )
                for item in graph["relations"]
                if item["source_id"] in canonical_ids
                and (
                    item.get("target_id") is None
                    or item["target_id"] in canonical_ids
                )
            ),
            key=lambda item: item.relation_id,
        )
    )
    artifact = CanonicalContentArtifact(
        schema=CANONICAL_CONTENT_SCHEMA_VERSION,
        document_id="fixture-v3-2",
        resources=resources,
        presentation_units=presentation_units,
        divisions=divisions,
        content_nodes=content_nodes,
        representations=(),
        native_bindings=(),
        relations=relations,
        processing_activities=(),
        artifact_descriptors=(),
    )
    artifact.validate()
    return artifact
