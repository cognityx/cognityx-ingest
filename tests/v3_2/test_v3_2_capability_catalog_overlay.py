"""Catalog overlay tests for preserving capability evidence and conflicts."""

from __future__ import annotations

from pathlib import Path

from cognityx_ingest import (
    ExtractionResult,
    ParserCapabilityRegistry,
    ParserRouter,
)


class _RouterOnlyParser:
    """Represent a custom registered adapter absent from the frozen catalog."""

    name = "router-only"

    def extract_document(self, path: Path) -> ExtractionResult:
        """Return a stable result if called, though overlay must never call it."""
        raise AssertionError("catalog overlay must not execute parsers")


def _catalog(v3_2_fixture_root) -> ParserCapabilityRegistry:
    """Read the authoritative fixture through the public immutable API."""
    return ParserCapabilityRegistry.from_json_bytes(
        (
            v3_2_fixture_root
            / "capability_registry"
            / "parser_capabilities.json"
        ).read_bytes()
    )


def test_catalog_overlay_preserves_sources_and_does_not_mutate_catalog(
    v3_2_fixture_root,
) -> None:
    """Replace runtime only while retaining exact catalog bytes and evidence records."""
    catalog = _catalog(v3_2_fixture_root)
    before = catalog.to_json_bytes()
    overlay = ParserCapabilityRegistry.from_router(ParserRouter(), catalog=catalog)
    assert catalog.to_json_bytes() == before
    original = catalog.get("docling")
    live = overlay.get("docling")
    assert set(live.parser_discovered.official_documentation) == set(
        original.parser_discovered.official_documentation
    )
    assert set(live.parser_discovered.capabilities) == set(
        original.parser_discovered.capabilities
    )
    assert set(live.human_guided) == set(original.human_guided)
    assert live.auto_learned.benchmark_profile == (
        original.auto_learned.benchmark_profile
    )
    assert set(live.auto_learned.measurements) == set(
        original.auto_learned.measurements
    )
    assert live.parser_discovered.runtime_probe.plugin_registered is True


def test_explicit_and_derived_availability_conflicts_remain_visible(
    v3_2_fixture_root, monkeypatch
) -> None:
    """Retain fixture conflict and add other declared/unavailable facts separately."""
    import cognityx_ingest.parser_capabilities as parser_capabilities

    real_find_spec = parser_capabilities.importlib.util.find_spec

    def without_docling(name: str):
        """Simulate absent Docling without changing other dependency probes."""
        return None if name == "docling" else real_find_spec(name)

    monkeypatch.setattr(parser_capabilities.importlib.util, "find_spec", without_docling)
    docling = ParserCapabilityRegistry.from_router(
        ParserRouter(), catalog=_catalog(v3_2_fixture_root)
    ).get("docling")
    conflicts = {item.capability: item for item in docling.conflicts}
    assert conflicts["document_hierarchy"].resolution == (
        "declared-but-currently-unavailable"
    )
    assert conflicts["tables"].advertised is True
    assert conflicts["tables"].runtime_available is False
    assert docling.parser_discovered.runtime_probe.runtime_available is False


def test_router_only_and_catalog_only_parsers_are_described_honestly(
    v3_2_fixture_root,
) -> None:
    """Keep custom registration and unavailable catalog evidence without guessing."""
    catalog = _catalog(v3_2_fixture_root)
    overlay = ParserCapabilityRegistry.from_router(
        ParserRouter((_RouterOnlyParser(),)),
        catalog=catalog,
    )
    assert tuple(record.parser_id for record in overlay.list()) == (
        "docling",
        "future-parser",
        "pymupdf",
        "router-only",
    )
    router_only = overlay.get("router-only")
    assert router_only.parser_discovered.runtime_probe.plugin_registered is True
    assert router_only.human_guided == ()
    assert router_only.auto_learned.measurements == ()
    future = overlay.get("future-parser")
    assert future.parser_discovered.runtime_probe.plugin_registered is False
    assert future.parser_discovered.runtime_probe.dependency_importable is None
    assert future.human_guided == catalog.get("future-parser").human_guided


def test_overlay_serialization_is_deterministic(v3_2_fixture_root) -> None:
    """Produce the same canonical bytes for equivalent router and catalog inputs."""
    catalog = _catalog(v3_2_fixture_root)
    first = ParserCapabilityRegistry.from_router(ParserRouter(), catalog=catalog)
    second = ParserCapabilityRegistry.from_router(ParserRouter(), catalog=catalog)
    assert first.to_json_bytes() == second.to_json_bytes()
