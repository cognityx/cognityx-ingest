"""Legacy parser-result compatibility tests for additive production T05."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import cognityx_ingest.parser as parser_module
from cognityx_ingest import (
    ExtractedPage,
    ExtractionPolicy,
    ExtractionResult,
    ParserFusionArtifact,
    ParserFusionService,
    ParserRouter,
)


class _ResultPlugin:
    """Return one prebuilt result so tests isolate routing from parser behavior."""

    def __init__(self, result: ExtractionResult) -> None:
        """Retain one immutable result and expose its established backend name."""
        self.name = result.backend
        self._result = result

    def extract_document(self, _path: Path) -> ExtractionResult:
        """Return the configured result without reading a source or external service."""
        return self._result


def _results() -> tuple[ExtractionResult, ExtractionResult]:
    """Return two small conflicting parser results for compatibility checks."""
    return (
        ExtractionResult(
            pages=(ExtractedPage(1, "Docling text", page_index=0),),
            backend="docling",
            backend_version="1",
            raw_artifact=b'{"native":"docling"}',
        ),
        ExtractionResult(
            pages=(ExtractedPage(1, "PyMuPDF text", page_index=0),),
            backend="pymupdf",
            backend_version="2",
            raw_artifact=b'{"native":"pymupdf"}',
        ),
    )


def test_fuse_results_delegates_to_production_t05_service(monkeypatch) -> None:
    """Keep parser.py as a thin local-import wrapper around the new service."""
    calls = []
    original = ParserFusionService.fuse_extraction_results

    def tracked(self, results, candidates, **kwargs):
        """Record one delegation while preserving the real production algorithm."""
        calls.append((tuple(item.backend for item in results), candidates))
        return original(self, results, candidates, **kwargs)

    monkeypatch.setattr(ParserFusionService, "fuse_extraction_results", tracked)
    result = parser_module._fuse_results(_results(), ("docling", "pymupdf"))
    assert calls == [(('docling', 'pymupdf'), ('docling', 'pymupdf'))]
    assert result.fusion_artifact is not None


def test_existing_v1_raw_artifact_and_additive_v3_2_artifact_coexist() -> None:
    """Preserve the old raw artifact schema while exposing validated v3.2 bytes."""
    result = parser_module._fuse_results(_results(), ("docling", "pymupdf"))
    assert json.loads(result.raw_artifact)["schema"] == "cognityx.ingest.parser-fusion/v1"
    artifact = ParserFusionArtifact.from_json_bytes(result.fusion_artifact)
    assert artifact.schema == "cognityx.ingest.parser-fusion/v3.2"
    assert result.raw_artifacts == {
        "docling": b'{"native":"docling"}',
        "pymupdf": b'{"native":"pymupdf"}',
    }


def test_fixed_single_parser_behavior_remains_object_identical(tmp_path: Path) -> None:
    """Avoid invoking fusion or changing results outside compare mode."""
    path = tmp_path / "input.pdf"
    path.write_bytes(b"pdf")
    original = _results()[0]
    result = ParserRouter(
        (_ResultPlugin(original),),
        policy=ExtractionPolicy("fixed", ("docling",)),
    ).extract_document(path)
    assert result.pages is original.pages
    assert result.raw_artifact == original.raw_artifact
    assert result.fusion_artifact is None
    assert result.backend == "docling"


def test_fusion_performs_no_parser_network_provider_or_llm_call(monkeypatch) -> None:
    """Fuse completed results when every external execution seam is forbidden."""
    def forbidden(*_args, **_kwargs):
        """Fail if T05 crosses an execution or network boundary."""
        raise AssertionError("external execution occurred")

    monkeypatch.setattr(_ResultPlugin, "extract_document", forbidden)
    import socket

    monkeypatch.setattr(socket, "create_connection", forbidden)
    outcome = ParserFusionService().fuse_extraction_results(
        _results(), ("docling", "pymupdf")
    )
    assert outcome.extraction_result.backend == "fusion"
    assert outcome.fusion_artifact.fact_decisions


def test_compare_with_one_available_backend_is_deterministic(tmp_path: Path) -> None:
    """Retain compare compatibility when only one configured parser returns."""
    path = tmp_path / "input.pdf"
    path.write_bytes(b"pdf")
    original = _results()[0]
    router = ParserRouter(
        (_ResultPlugin(original),),
        policy=ExtractionPolicy("compare", ("docling",)),
    )
    first = router.extract_document(path)
    second = router.extract_document(path)
    assert first == second
    assert first.considered_backends == ("docling",)
    assert first.fusion_artifact == second.fusion_artifact


def test_t05_introduces_no_segmentation_view_api() -> None:
    """Stop at segmentation observations and leave materialized views to T06."""
    import cognityx_ingest.parser_fusion as fusion

    assert not hasattr(fusion, "SegmentationView")
    assert not hasattr(fusion, "materialize_segmentation_view")
