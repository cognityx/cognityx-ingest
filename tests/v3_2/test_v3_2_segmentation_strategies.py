"""Production strategy behavior for all six non-copying T06 alternatives."""

from __future__ import annotations

import ast
from pathlib import Path
import urllib.request

import cognityx_ingest
import cognityx_ingest.segmentation_views as segmentation_views
from cognityx_ingest import SegmentationViewService
from cognityx_ingest.parser import ParserRouter


class CharacterTokenCounter:
    """Provide a deterministic local counter for sentence-safe strategy tests."""

    def __init__(self) -> None:
        """Create an empty call log so tests can prove transient counting."""
        self.calls: list[str] = []

    def count_tokens(self, text: str) -> int:
        """Count non-whitespace characters without retaining them in views."""
        self.calls.append(text)
        return max(1, len(text.replace(" ", "")))


def test_production_paragraph_builder_uses_paragraph_nodes_only(
    frozen_canonical_artifact,
):
    """Build one whole-node segment for each paragraph in canonical order."""
    service = SegmentationViewService.from_canonical(frozen_canonical_artifact)

    view = service.build_paragraph("paragraph-production")
    node_kinds = {node.node_id: node.node_kind for node in frozen_canonical_artifact.content_nodes}
    referenced = tuple(segment.node_spans[0].node_id for segment in view.segments)

    assert referenced == tuple(
        node.node_id
        for node in frozen_canonical_artifact.content_nodes
        if node.node_kind == "paragraph"
    )
    assert all(node_kinds[node_id] == "paragraph" for node_id in referenced)
    assert all(len(segment.node_spans) == 1 for segment in view.segments)


def test_production_direct_division_uses_direct_nodes_not_subtree(
    frozen_canonical_artifact,
):
    """Keep deepest direct ownership distinct from reconstructed descendants."""
    service = SegmentationViewService.from_canonical(frozen_canonical_artifact)

    view = service.build_direct_division(
        "direct-production", division_ids=("div-policy-4.2",)
    )
    segment = view.segments[0]

    assert segment.division_id == "div-policy-4.2"
    assert tuple(span.node_id for span in segment.node_spans) == tuple(
        node.node_id
        for node in frozen_canonical_artifact.direct_nodes("div-policy-4.2")
    )


def test_sentence_safe_fixed_size_uses_only_injected_counter_and_spans(
    frozen_canonical_artifact,
):
    """Apply a local token budget without introducing tokenizer dependencies."""
    counter = CharacterTokenCounter()
    service = SegmentationViewService.from_canonical(
        frozen_canonical_artifact, token_counter=counter
    )

    view = service.build_sentence_safe_fixed_size(
        "fixed-production", max_tokens=90, tokenizer="local-test-counter"
    )

    assert view.profile.to_dict() == {
        "max_tokens": 90,
        "tokenizer": "local-test-counter",
    }
    assert counter.calls
    assert all(segment.node_spans for segment in view.segments)
    assert all(segment.text is None for segment in view.segments)
    assert all("text" not in segment.to_dict() for segment in view.segments)


def test_sentence_window_keeps_seed_and_context_roles_explicit(
    frozen_canonical_artifact,
):
    """Reference neighbours without flattening them into a combined node."""
    service = SegmentationViewService.from_canonical(frozen_canonical_artifact)

    view = service.build_sentence_window(
        "window-production",
        seed_node_ids=("pol-p2",),
        context_before=1,
        context_after=1,
    )
    segment = view.segments[0]

    assert segment.seed.node_id == "pol-p2"
    assert tuple(span.node_id for span in segment.context) == ("pol-p1", "pol-p3")
    assert not segment.node_spans
    assert "seed" in segment.to_dict()
    assert "context" in segment.to_dict()


def test_parent_child_keeps_retrieval_span_and_return_division_separate(
    frozen_canonical_artifact,
):
    """Point a child hit at its canonical parent scope without copying parent text."""
    service = SegmentationViewService.from_canonical(frozen_canonical_artifact)

    view = service.build_parent_child(
        "parent-production", retrieval_node_ids=("pol-p2",)
    )
    segment = view.segments[0]

    assert tuple(span.node_id for span in segment.retrieval_node_spans) == (
        "pol-p2",
    )
    assert segment.return_scope.division_id == "div-policy-4.2"
    assert "retrieval_node_spans" in segment.to_dict()
    assert "return_scope" in segment.to_dict()
    assert "node_spans" not in segment.to_dict()


def test_multiple_overlapping_views_coexist_without_a_canonical_winner(
    frozen_canonical_artifact,
):
    """Allow conflicting boundaries while leaving canonical content unchanged."""
    service = SegmentationViewService.from_canonical(frozen_canonical_artifact)
    before = frozen_canonical_artifact.to_json_bytes()

    paragraph = service.build_paragraph("paragraph-production")
    direct = service.build_direct_division(
        "direct-production", division_ids=("div-policy-4.2",)
    )
    window = service.build_sentence_window(
        "window-production", seed_node_ids=("pol-p2",)
    )
    parent = service.build_parent_child(
        "parent-production", retrieval_node_ids=("pol-p2",)
    )
    view_set = service.build_view_set((paragraph, direct, window, parent))

    assert len(view_set.views) == 4
    assert sum(
        span.node_id == "pol-p2"
        for view in view_set.views
        for segment in view.segments
        for span in (
            segment.node_spans
            + segment.retrieval_node_spans
            + ((segment.seed,) if segment.seed else ())
        )
    ) >= 4
    assert frozen_canonical_artifact.to_json_bytes() == before
    assert not hasattr(view_set, "winning_view")


def test_no_fused_or_canonical_chunk_api_is_exposed():
    """Keep segmentation alternatives from becoming canonical boundary truth."""
    for name in ("canonical_chunks", "fused_chunks", "winning_view", "accepted_boundary"):
        assert not hasattr(cognityx_ingest, name)
        assert not hasattr(SegmentationViewService, name)


def test_strategy_generation_does_not_execute_parser_or_network(
    frozen_canonical_artifact, monkeypatch
):
    """Prove normal T06 builders stay within the in-memory canonical boundary."""
    def fail_parser(*_args, **_kwargs):
        """Fail immediately if T06 accidentally invokes parser execution."""
        raise AssertionError("parser execution is prohibited in T06")

    def fail_network(*_args, **_kwargs):
        """Fail immediately if T06 accidentally invokes the network."""
        raise AssertionError("network access is prohibited in T06")

    monkeypatch.setattr(ParserRouter, "extract_document", fail_parser)
    monkeypatch.setattr(urllib.request, "urlopen", fail_network)
    service = SegmentationViewService.from_canonical(frozen_canonical_artifact)

    assert service.build_paragraph("paragraph-no-io").segments
    assert service.build_direct_division(
        "direct-no-io", division_ids=("div-policy-4.2",)
    ).segments


def test_t06_module_introduces_no_model_vector_or_provider_dependency():
    """Keep normal T06 imports dependency-light and offline-safe."""
    source = Path(segmentation_views.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_roots.update(
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )

    assert imported_roots.isdisjoint(
        {"openai", "transformers", "torch", "sentence_transformers", "faiss"}
    )
