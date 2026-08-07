"""Legacy parser-result compatibility tests for additive production T05."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import cognityx_ingest.parser as parser_module
from cognityx_ingest import (
    ExtractedBlock,
    ExtractedObject,
    ExtractedPage,
    ExtractedRelation,
    ExtractionPolicy,
    ExtractionResult,
    FusionOutcome,
    ObservationSourceRegion,
    ParserFusionArtifact,
    ParserFusionCompatibilityError,
    ParserFusionService,
    ParserObservation,
    ParserObservationSet,
    ParserRouter,
)
from cognityx_ingest.parser_fusion import (
    ObservationValue,
    _enrich_source_details,
    _select_compatibility_observation,
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


def _repeated_block_results(
    *, reverse_results: bool = False, reverse_blocks: bool = False
) -> tuple[ExtractionResult, ExtractionResult]:
    """Build parser results with repeated text and distinct local block identities.

    Provenance tests use this production-shaped input to verify that compatibility
    projection binds identical values to parser occurrences rather than relying
    on text or input sequence order. The helper performs no parser execution and
    returns fresh immutable records for each test.
    """
    results = tuple(
        ExtractionResult(
            pages=(
                ExtractedPage(
                    1,
                    f"{backend} page",
                    page_index=0,
                    blocks=tuple(
                        reversed(
                            (
                                ExtractedBlock(
                                    f"{backend}-block-a",
                                    "Repeated policy text",
                                    reading_order=1,
                                ),
                                ExtractedBlock(
                                    f"{backend}-block-b",
                                    "Repeated policy text",
                                    reading_order=2,
                                ),
                            )
                        )
                        if reverse_blocks
                        else (
                            ExtractedBlock(
                                f"{backend}-block-a",
                                "Repeated policy text",
                                reading_order=1,
                            ),
                            ExtractedBlock(
                                f"{backend}-block-b",
                                "Repeated policy text",
                                reading_order=2,
                            ),
                        )
                    ),
                ),
            ),
            backend=backend,
            backend_version="1",
        )
        for backend in ("docling", "pymupdf")
    )
    return tuple(reversed(results)) if reverse_results else results  # type: ignore[return-value]


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
    assert result.observation_artifact is not None
    assert result.fusion_artifact is not None


def test_existing_v1_raw_artifact_and_additive_v3_2_artifact_coexist() -> None:
    """Preserve the old raw artifact schema while exposing validated v3.2 bytes."""
    result = parser_module._fuse_results(_results(), ("docling", "pymupdf"))
    assert json.loads(result.raw_artifact)["schema"] == "cognityx.ingest.parser-fusion/v1"
    artifact = ParserFusionArtifact.from_json_bytes(result.fusion_artifact)
    observations = ParserObservationSet.from_json_bytes(result.observation_artifact)
    artifact.validate_against_observation_set(observations)
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
    assert result.observation_artifact is None
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
    assert outcome.extraction_result.observation_artifact == (
        outcome.observation_set.to_json_bytes()
    )
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
    assert first.observation_artifact == second.observation_artifact
    assert first.fusion_artifact == second.fusion_artifact


def test_t05_introduces_no_segmentation_view_api() -> None:
    """Stop at segmentation observations and leave materialized views to T06."""
    import cognityx_ingest.parser_fusion as fusion

    assert not hasattr(fusion, "SegmentationView")
    assert not hasattr(fusion, "materialize_segmentation_view")


def test_fusion_outcome_requires_both_exact_public_aggregate_bytes() -> None:
    """Prevent compatibility transport from dropping or replacing observations."""
    outcome = ParserFusionService().fuse_extraction_results(
        _results(), ("docling", "pymupdf")
    )
    value = outcome.observation_set.to_dict()
    value["observations"][0]["confidence"] = 0.25
    changed = ParserObservationSet.from_json_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    )
    with pytest.raises(ParserFusionCompatibilityError):
        FusionOutcome(changed, outcome.fusion_artifact, outcome.extraction_result)


def test_identical_blocks_without_bbox_bind_to_exact_parser_occurrences() -> None:
    """Bind repeated same-page text by source region instead of value ordering."""
    outcome = ParserFusionService().fuse_extraction_results(
        _repeated_block_results(), ("docling", "pymupdf")
    )
    observations = {
        item.observation_id: item for item in outcome.observation_set.observations
    }
    blocks = outcome.extraction_result.pages[0].blocks
    assert len(blocks) == 2
    assert "Repeated policy text" not in json.dumps(
        [block.fact_sources for block in blocks]
    )
    for block in blocks:
        for source in block.fact_sources["text"]:
            observation = observations[source["observation_id"]]
            assert observation.source_region.bbox is None
            assert observation.source_region.source_region_id == source["source_region_id"]
            assert observation.source_region.source_anchor == source["source_anchor"]


def test_different_parser_block_ids_select_their_own_observations() -> None:
    """Keep parser-local anchors distinct even when source values are identical."""
    outcome = ParserFusionService().fuse_extraction_results(
        _repeated_block_results(), ("docling", "pymupdf")
    )
    observation_index = {
        item.observation_id: item for item in outcome.observation_set.observations
    }
    anchors = set()
    for block in outcome.extraction_result.pages[0].blocks:
        for source in block.fact_sources["text"]:
            observation = observation_index[source["observation_id"]]
            anchors.add(source["source_anchor"])
            assert source["backend"] in source["source_anchor"]
            assert observation.parser_id == source["backend"]
            assert observation.source_region.source_anchor == source["source_anchor"]
    assert anchors == {
        "docling-block-a",
        "docling-block-b",
        "pymupdf-block-a",
        "pymupdf-block-b",
    }


def test_occurrence_index_disambiguates_repeated_value_in_one_region() -> None:
    """Use explicit occurrence identity when region and value identity repeat."""
    region = ObservationSourceRegion(
        "block:docling:0:shared", physical_page_index=0
    )
    observations = tuple(
        ParserObservation.create(
            parser_id="docling",
            parser_version=None,
            source_region=region,
            fact="text",
            value="Repeated policy text",
            occurrence_index=index,
        )
        for index in (1, 2)
    )
    artifact = ParserFusionService().fuse(
        ParserObservationSet.create(observations)
    )
    decision_by_observation = {
        observation_id: decision
        for decision in artifact.fact_decisions
        for observation_id in decision.observation_ids
    }
    sources = _enrich_source_details(
        (
            {
                "backend": "docling",
                "method": "parser",
                "confidence": None,
                "source_region_id": region.source_region_id,
                "occurrence_index": 2,
            },
        ),
        "text",
        "Repeated policy text",
        0,
        observations,
        decision_by_observation,
    )
    assert sources[0]["observation_id"] == observations[1].observation_id


def test_exact_block_anchor_selects_the_matching_observation() -> None:
    """Prefer a parser block anchor over a nonunique value-only fallback."""
    regions = tuple(
        ObservationSourceRegion(
            f"block:docling:0:{suffix}",
            physical_page_index=0,
            source_anchor=f"block-{suffix}",
        )
        for suffix in ("a", "b")
    )
    observations = tuple(
        ParserObservation.create(
            parser_id="docling",
            parser_version=None,
            source_region=region,
            fact="text",
            value="Repeated policy text",
        )
        for region in regions
    )
    artifact = ParserFusionService().fuse(
        ParserObservationSet.create(observations)
    )
    decisions = {
        observation_id: decision
        for decision in artifact.fact_decisions
        for observation_id in decision.observation_ids
    }
    enriched = _enrich_source_details(
        (
            {
                "backend": "docling",
                "method": "parser",
                "confidence": None,
                "source_anchor": "block-b",
            },
        ),
        "text",
        "Repeated policy text",
        0,
        observations,
        decisions,
    )
    assert enriched[0]["observation_id"] == observations[1].observation_id
    assert enriched[0]["decision_id"] == decisions[
        observations[1].observation_id
    ].decision_id


def test_ambiguous_value_only_compatibility_binding_raises_typed_error() -> None:
    """Fail closed when legacy metadata cannot identify one repeated occurrence."""
    observations = tuple(
        ParserObservation.create(
            parser_id="docling",
            parser_version=None,
            source_region=ObservationSourceRegion(
                f"block:docling:0:{suffix}", physical_page_index=0
            ),
            fact="text",
            value="Repeated policy text",
        )
        for suffix in ("a", "b")
    )
    with pytest.raises(ParserFusionCompatibilityError, match="multiple"):
        _enrich_source_details(
            ({"backend": "docling", "method": "parser", "confidence": None},),
            "text",
            "Repeated policy text",
            0,
            observations,
            {},
        )


def test_compatibility_bbox_requires_exact_known_geometry() -> None:
    """Never treat missing observation geometry as equal to a supplied box."""
    missing = ParserObservation.create(
        parser_id="docling",
        parser_version=None,
        source_region=ObservationSourceRegion(
            "block:docling:0:missing",
            region_kind="block",
            physical_page_index=0,
        ),
        fact="text",
        value="Selected text",
    )
    known = ParserObservation.create(
        parser_id="docling",
        parser_version=None,
        source_region=ObservationSourceRegion(
            "block:docling:0:known",
            region_kind="block",
            physical_page_index=0,
            bbox=(0.0, 0.0, 20.0, 20.0),
        ),
        fact="text",
        value="Selected text",
    )
    selected_hash = ObservationValue.from_value("Selected text").sha256
    assert _select_compatibility_observation(
        {"backend": "docling"},
        "text",
        selected_hash,
        0,
        (0.0, 0.0, 20.0, 20.0),
        (missing,),
    ) is None
    assert _select_compatibility_observation(
        {"backend": "docling"},
        "text",
        selected_hash,
        0,
        (0.0, 0.0, 20.0, 20.0),
        (missing, known),
    ) == known


def test_exact_region_identity_precedes_missing_compatibility_geometry() -> None:
    """Allow a stronger exact region ID to identify a geometry-free observation."""
    observation = ParserObservation.create(
        parser_id="docling",
        parser_version=None,
        source_region=ObservationSourceRegion(
            "block:docling:0:exact",
            region_kind="block",
            physical_page_index=0,
        ),
        fact="text",
        value="Selected text",
    )
    assert _select_compatibility_observation(
        {
            "backend": "docling",
            "source_region_id": "block:docling:0:exact",
        },
        "text",
        observation.value_sha256,
        0,
        (0.0, 0.0, 20.0, 20.0),
        (observation,),
    ) == observation


@pytest.mark.parametrize(
    "source",
    (
        {},
        {"backend": ""},
        {"backend": "INVALID PARSER"},
        {"backend": "docling", "parser_id": "pymupdf"},
    ),
)
def test_malformed_compatibility_parser_identity_raises_typed_error(
    source: dict[str, object],
) -> None:
    """Reject missing, malformed, or conflicting parser identity metadata."""
    with pytest.raises(ParserFusionCompatibilityError, match="parser identity"):
        _select_compatibility_observation(
            source,
            "text",
            ObservationValue.from_value("Selected text").sha256,
            0,
            None,
            (),
        )


def test_reversed_result_and_block_order_produces_identical_enrichment() -> None:
    """Normalize parser and block order before assigning duplicate occurrences."""
    service = ParserFusionService()
    first = service.fuse_extraction_results(
        _repeated_block_results(), ("docling", "pymupdf")
    )
    reversed_outcome = service.fuse_extraction_results(
        _repeated_block_results(reverse_results=True, reverse_blocks=True),
        ("docling", "pymupdf"),
    )
    assert first.observation_set.to_json_bytes() == (
        reversed_outcome.observation_set.to_json_bytes()
    )
    assert first.fusion_artifact.to_json_bytes() == (
        reversed_outcome.fusion_artifact.to_json_bytes()
    )
    assert first.extraction_result.pages == reversed_outcome.extraction_result.pages


def test_object_and_relation_sources_retain_parser_local_identity_without_text() -> None:
    """Carry bounded object and relation anchors without copying source values."""
    results = tuple(
        ExtractionResult(
            pages=(
                ExtractedPage(
                    1,
                    "Page source text",
                    page_index=0,
                    objects=(
                        ExtractedObject(
                            object_id=f"{backend}-object-1",
                            object_type="table",
                            page_index=0,
                            caption="Sensitive object caption",
                        ),
                    ),
                    relations=(
                        ExtractedRelation(
                            relation_id=f"{backend}-relation-1",
                            source_anchor=f"{backend}-object-1",
                            target_anchor=f"{backend}-target-1",
                            relation_type="references",
                            target_text="Sensitive relation target",
                        ),
                    ),
                ),
            ),
            backend=backend,
        )
        for backend in ("docling", "pymupdf")
    )
    projected = ParserFusionService().fuse_extraction_results(
        results, ("docling", "pymupdf")
    ).extraction_result.pages[0]
    object_sources = projected.objects[0].fact_sources
    relation_sources = projected.relations[0].fact_sources
    for source in (*object_sources["identity"], *object_sources["selected"]):
        assert source["source_region_id"].startswith("object:")
        assert source["source_anchor"].endswith("-object-1")
        assert source["occurrence_index"] == 1
    for source in (*relation_sources["identity"], *relation_sources["selected"]):
        assert source["source_region_id"].startswith("relation:")
        assert source["source_anchor"].endswith("-object-1")
        assert source["parser_relation_id"].endswith("-relation-1")
        assert source["occurrence_index"] == 1
    metadata = json.dumps((object_sources, relation_sources))
    assert "Sensitive object caption" not in metadata
    assert "Sensitive relation target" not in metadata
