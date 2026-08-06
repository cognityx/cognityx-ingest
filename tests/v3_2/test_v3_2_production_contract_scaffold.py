"""Strict xfail tests for planned production APIs not implemented in T00.

These tests are intentionally production-facing: each one names the API that a
future task will implement, while T00 keeps production parsing unchanged. The
core design principle is narrow failure: xfails should break only when the
planned API exists and satisfies the frozen fixture contract.
"""

from __future__ import annotations

import pytest

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


@pytest.mark.xfail(strict=True, reason="T06: segmentation view production API is not implemented")
def test_segmentation_view_api_references_ids_and_spans(v3_2_fixture_root):
    """Call the planned non-copying segmentation view API."""
    from cognityx_ingest.segmentation_views import SegmentationViewService

    service = SegmentationViewService.from_fixture(v3_2_fixture_root)
    view = service.build("view-paragraph-v1")
    assert all(segment.text is None for segment in view.segments)
    assert all(segment.node_spans for segment in view.segments)


@pytest.mark.xfail(strict=True, reason="T08: source graph and provenance resolver API is not implemented")
def test_source_graph_and_provenance_resolver_api_returns_exact(v3_2_fixture_root):
    """Call the planned source graph resolver against a frozen strong address."""
    from cognityx_ingest.source_graph import ProvenanceAddressResolver, SourceGraphRepository

    graph = SourceGraphRepository.from_fixture(v3_2_fixture_root).load("sg-rev-001")
    resolver = ProvenanceAddressResolver(graph)
    result = resolver.resolve("addr-strong-pol-p2")
    assert result.status == "exact"
    assert result.target.node_id == "pol-p2"
