"""Focused deterministic-value and observation-set tests for production T05."""

from __future__ import annotations

import math

import pytest

from cognityx_ingest import (
    ExtractedBlock,
    ExtractedObject,
    ExtractedPage,
    ExtractedRelation,
    ExtractedSection,
    ExtractionResult,
    ObservationSourceRegion,
    ObservationValue,
    ParserObservation,
    ParserObservationSet,
    ParserObservationValidationError,
    ParserFusionService,
    SOURCE_REGION_KINDS,
)


def test_observation_values_round_trip_exactly_and_hash_canonical_bytes() -> None:
    """Preserve strings and nested JSON values through deterministic bytes."""
    source = {
        "z": [" exact  whitespace ", 3, True, None],
        "a": {"finite": 1.25},
    }
    value = ObservationValue.from_value(source)
    assert value.to_value() == source
    assert value.to_json_bytes() == (
        b'{"a":{"finite":1.25},"z":[" exact  whitespace ",3,true,null]}'
    )
    assert ObservationValue.from_json_bytes(value.to_json_bytes()) == value
    assert len(value.sha256) == 64
    detached = value.to_value()
    detached["z"].append("changed")
    assert value.to_value() == source


@pytest.mark.parametrize("number", [math.nan, math.inf, -math.inf])
def test_observation_values_reject_non_finite_numbers_without_value_leak(
    number: float,
) -> None:
    """Raise the typed value error without rendering the rejected source value."""
    with pytest.raises(ParserObservationValidationError) as error:
        ObservationValue.from_value({"unsafe": number})
    assert "unsafe" not in str(error.value)


def test_observation_values_reject_unsupported_mutable_custom_types() -> None:
    """Reject sets and custom containers instead of retaining mutable objects."""
    with pytest.raises(ParserObservationValidationError):
        ObservationValue.from_value({"values": {"a", "b"}})


def test_observation_json_rejects_duplicate_object_keys() -> None:
    """Reject ambiguous JSON objects at the public untrusted reader seam."""
    with pytest.raises(ParserObservationValidationError):
        ObservationValue.from_json_bytes(b'{"fact":1,"fact":2}')


def test_observation_set_is_order_independent_and_strictly_round_trips() -> None:
    """Create stable observation IDs and bytes regardless of caller sequence."""
    region = ObservationSourceRegion(
        "region-order",
        resource_id="resource-order",
        physical_page_index=2,
        char_start=10,
        char_end=20,
    )
    observations = tuple(
        ParserObservation.create(
            parser_id=parser_id,
            parser_version="1.0",
            source_region=region,
            fact="text",
            value="Exact text",
            occurrence_index=1,
        )
        for parser_id in ("pymupdf", "docling")
    )
    first = ParserObservationSet.create(observations, source_document_id="doc-order")
    second = ParserObservationSet.create(tuple(reversed(observations)), source_document_id="doc-order")
    assert first == second
    assert first.to_json_bytes() == second.to_json_bytes()
    assert ParserObservationSet.from_json_bytes(first.to_json_bytes()) == first
    assert first.get(observations[0].observation_id) == observations[0]
    assert len(first.observations_for_region("region-order")) == 2
    assert len(first.observations_for_fact("text")) == 2


def test_source_region_requires_valid_real_locator_and_never_copies_text() -> None:
    """Keep only bounded locator fields with valid span and ordered geometry."""
    region = ObservationSourceRegion(
        "region-located",
        selector_ids=("selector-a",),
        physical_page_index=0,
        bbox=(1.0, 2.0, 10.0, 20.0),
    )
    assert "text" not in region.to_dict()
    assert ObservationSourceRegion.from_dict(
        {"source_region_id": "region-minimal"}
    ) == ObservationSourceRegion("region-minimal")
    with pytest.raises(ParserObservationValidationError):
        ObservationSourceRegion("region-invalid", char_start=20, char_end=10)
    with pytest.raises(ParserObservationValidationError):
        ObservationSourceRegion("region-invalid", bbox=(10.0, 2.0, 1.0, 20.0))


def test_source_region_kind_and_native_record_identity_round_trip_strictly() -> None:
    """Bind exact bounded kind and parser-local record identity into observation IDs."""
    assert SOURCE_REGION_KINDS == (
        "page",
        "block",
        "object",
        "relation",
        "section",
        "generic",
    )
    region = ObservationSourceRegion(
        "relation-docling-1",
        region_kind="relation",
        source_anchor="source-endpoint",
        native_record_id="native-relation-1",
    )
    assert ObservationSourceRegion.from_dict(region.to_dict()) == region
    block_observation = ParserObservation.create(
        parser_id="docling",
        parser_version=None,
        source_region=ObservationSourceRegion(
            "record-shared",
            region_kind="block",
        ),
        fact="text",
        value="same",
    )
    object_observation = ParserObservation.create(
        parser_id="docling",
        parser_version=None,
        source_region=ObservationSourceRegion(
            "record-shared",
            region_kind="object",
        ),
        fact="text",
        value="same",
    )
    assert block_observation.observation_id != object_observation.observation_id
    with pytest.raises(ParserObservationValidationError, match="region_kind"):
        ObservationSourceRegion("region-invalid", region_kind="paragraph")


def test_production_adaptation_assigns_every_source_region_kind() -> None:
    """Classify normalized page, block, object, relation, and section records."""
    observation_set = ParserFusionService().build_observation_set((
        ExtractionResult(
            pages=(
                ExtractedPage(
                    1,
                    "Page text",
                    page_index=0,
                    blocks=(ExtractedBlock(
                        block_id="block-1",
                        text="Block text",
                        reading_order=1,
                        block_type="paragraph",
                    ),),
                    objects=(ExtractedObject(
                        "object-1",
                        "table",
                        0,
                    ),),
                    relations=(ExtractedRelation(
                        "relation-1",
                        "block-1",
                        "object-1",
                        "references",
                    ),),
                ),
            ),
            sections=(ExtractedSection(
                "section-1",
                "Introduction",
                0,
                0,
            ),),
            backend="docling",
        ),
    ))
    kinds_by_prefix = {
        prefix: {
            item.source_region.region_kind
            for item in observation_set.observations
            if item.source_region.source_region_id.startswith(prefix)
        }
        for prefix in ("page:", "block:", "object:", "relation:", "section:")
    }
    assert kinds_by_prefix == {
        "page:": {"page"},
        "block:": {"block"},
        "object:": {"object"},
        "relation:": {"relation"},
        "section:": {"section"},
    }
    relation_regions = {
        item.source_region
        for item in observation_set.observations
        if item.source_region.region_kind == "relation"
    }
    assert {item.native_record_id for item in relation_regions} == {"relation-1"}


def test_page_facts_do_not_fabricate_missing_confidence() -> None:
    """Preserve absent page confidence as unknown rather than zero or one."""
    observation_set = ParserFusionService().build_observation_set(
        (
            ExtractionResult(
                pages=(
                    ExtractedPage(
                        1,
                        "Page text",
                        page_index=0,
                        page_label="1",
                        width=612.0,
                        height=792.0,
                    ),
                ),
                backend="docling",
            ),
        )
    )
    page_observations = tuple(
        item
        for item in observation_set.observations
        if item.source_region.source_region_id == "page:0"
    )
    assert page_observations
    assert all(item.confidence is None for item in page_observations)
    assert all(item.confidence not in {0, 1} for item in page_observations)
