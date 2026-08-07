"""Production seam checks retained from the original T00 scaffold.

T03, T06, and T08 converted their strict expected failures into real
production-facing acceptance tests. This compact module now guards the live
capability registry, non-copying view service, and exact Source Graph resolver.
It contains no placeholder assertion and no v3.2 strict expected failure.
"""

from __future__ import annotations

from cognityx_ingest import ParserRouter


def test_parser_capability_registry_api_exposes_three_source_classes(v3_2_fixture_root):
    """Call the planned ParserCapabilityRegistry API instead of parser diagnostics."""
    from cognityx_ingest.parser_capabilities import ParserCapabilityRegistry

    router = ParserRouter()
    registry = ParserCapabilityRegistry.from_router(router)
    record = registry.get("docling")
    assert record.capability_source_classes == (
        "parser-discovered",
        "human-guided",
        "auto-learned",
    )


def test_segmentation_view_api_references_ids_and_spans(v3_2_fixture_root):
    """Call the planned non-copying segmentation view API."""
    from cognityx_ingest.segmentation_views import SegmentationViewService

    service = SegmentationViewService.from_fixture(v3_2_fixture_root)
    view = service.build("view-paragraph-v1")
    assert all(segment.text is None for segment in view.segments)
    assert all(segment.node_spans for segment in view.segments)


def test_source_graph_and_provenance_resolver_api_returns_exact(v3_2_fixture_root):
    """Call the planned source graph resolver against a frozen strong address."""
    from cognityx_ingest.source_graph import ProvenanceAddressResolver, SourceGraphRepository

    graph = SourceGraphRepository.from_fixture(v3_2_fixture_root).load("sg-rev-001")
    resolver = ProvenanceAddressResolver(graph)
    result = resolver.resolve("addr-strong-pol-p2")
    assert result.status == "exact"
    assert result.target.node_id == "pol-p2"
