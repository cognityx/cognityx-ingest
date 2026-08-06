"""Production and fixture checks for the v3.2 capability registry contract.

The registry fixture is the source of truth for T03. These tests read the
frozen JSON directly, validate the three capability-source classes, and avoid
inventing parser records that are not present in the pack. T03 implementers
and reviewers use this suite to keep future registry APIs aligned with the
authoritative fixture.
"""

from __future__ import annotations

from cognityx_ingest import (
    CAPABILITY_SOURCE_CLASSES,
    ParserCapabilityRegistry,
)


def _load_registry(v3_2_fixture_root):
    """Load authoritative frozen bytes through the public production trust seam."""
    return ParserCapabilityRegistry.from_json_bytes(
        (
            v3_2_fixture_root
            / "capability_registry"
            / "parser_capabilities.json"
        ).read_bytes()
    )


def test_exactly_three_capability_source_classes(v3_2_fixture_root):
    """Assert the registry freezes exactly the three approved source classes."""
    registry = _load_registry(v3_2_fixture_root)
    assert CAPABILITY_SOURCE_CLASSES == (
        "parser-discovered",
        "human-guided",
        "auto-learned",
    )
    assert all(
        record.capability_source_classes == CAPABILITY_SOURCE_CLASSES
        for record in registry.list()
    )


def test_capability_registry_contains_real_parsers(v3_2_fixture_root):
    """Assert parser records match the actual frozen `parser_id` values."""
    registry = _load_registry(v3_2_fixture_root)
    assert {item.parser_id for item in registry.list()} == {
        "docling",
        "pymupdf",
        "future-parser",
    }


def test_frozen_registry_round_trips_deterministically(v3_2_fixture_root):
    """Normalize legacy probe syntax once and retain stable canonical bytes."""
    registry = _load_registry(v3_2_fixture_root)
    serialized = registry.to_json_bytes()
    restored = ParserCapabilityRegistry.from_json_bytes(serialized)
    assert restored == registry
    assert restored.to_json_bytes() == serialized


def test_frozen_evidence_sources_and_conflict_remain_separate(v3_2_fixture_root):
    """Preserve official, human, learned, and unavailable evidence independently."""
    docling = _load_registry(v3_2_fixture_root).get("docling")
    evidence = docling.parser_discovered.official_documentation
    assert {item.evidence_id for item in evidence} == {
        "docling-doc-model-20260806",
        "docling-chunking-20260806",
        "docling-formats-20260806",
    }
    assert evidence[0].source_url.startswith("https://docling-project.github.io/")
    assert docling.human_guided[0].recommendation == "preferred-primary"
    assert docling.auto_learned.benchmark_profile == "fixture-no-production-claim"
    assert docling.auto_learned.measurements[0].sample_count == 20
    assert docling.conflicts[0].resolution == "declared-but-currently-unavailable"
    assert docling.parser_discovered.runtime_probe.dependency_importable is False


def test_public_registry_data_is_immutable(v3_2_fixture_root):
    """Expose frozen records and tuples rather than mutable catalog indexes."""
    registry = _load_registry(v3_2_fixture_root)
    assert isinstance(registry.list(), tuple)
    assert isinstance(registry.get("docling").human_guided, tuple)
    assert isinstance(
        registry.get("docling").parser_discovered.official_documentation,
        tuple,
    )
