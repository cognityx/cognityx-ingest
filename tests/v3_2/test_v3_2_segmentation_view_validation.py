"""Strict persisted and bound-reference validation for T06 segmentation views."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json

import pytest

from cognityx_ingest import (
    NodeSpan,
    SegmentReturnScope,
    SegmentationSegment,
    SegmentationStrategyError,
    SegmentationViewReferenceError,
    SegmentationViewService,
    SegmentationViewSet,
    SegmentationViewValidationError,
    SegmentationFixtureError,
)


def _compact_digest(v3_2_fixture_root) -> str:
    """Return the exact frozen compact canonical-content byte digest."""
    payload = (v3_2_fixture_root / "expected" / "canonical_content.json").read_bytes()
    return hashlib.sha256(payload).hexdigest()


def _views_payload(v3_2_fixture_root) -> dict[str, object]:
    """Return a mutable JSON projection for isolated malformed-input tests."""
    return json.loads(
        (v3_2_fixture_root / "segmentation_views" / "views.json").read_text(
            encoding="utf-8"
        )
    )


def _json_bytes(value: object) -> bytes:
    """Encode a test mutation without applying production canonical ordering."""
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def test_duplicate_json_keys_fail_before_mapping_conversion(v3_2_fixture_root):
    """Reject duplicate names rather than accepting JSON's last value."""
    digest = _compact_digest(v3_2_fixture_root)

    with pytest.raises(
        SegmentationViewValidationError, match="Duplicate JSON object key"
    ):
        SegmentationViewSet.from_json_bytes(
            b'{"schema":"one","schema":"two","views":[]}',
            compact_canonical_sha256=digest,
        )


def test_json_and_fixture_implementation_errors_are_wrapped_typed(tmp_path):
    """Do not leak JSON decoder exceptions or raw local filesystem paths."""
    with pytest.raises(SegmentationViewValidationError) as malformed:
        SegmentationViewSet.from_json_bytes(b"{not-json")
    assert "JSONDecodeError" not in str(malformed.value)

    missing_root = tmp_path / "private" / "fixture-location"
    with pytest.raises(SegmentationFixtureError) as missing:
        SegmentationViewService.from_fixture(missing_root)
    assert str(missing_root) not in str(missing.value)


def test_unknown_strategy_error_does_not_echo_possible_source_text(
    v3_2_fixture_root,
):
    """Keep untrusted values and canonical sentences out of error messages."""
    payload = _views_payload(v3_2_fixture_root)
    canary = "Employees may claim travel expenses only for approved business travel."
    payload["views"][0]["strategy"] = canary

    with pytest.raises(SegmentationStrategyError) as error:
        SegmentationViewSet.from_json_bytes(
            _json_bytes(payload),
            compact_canonical_sha256=_compact_digest(v3_2_fixture_root),
        )
    assert canary not in str(error.value)


@pytest.mark.parametrize(
    ("mutation", "error"),
    (
        ("top-level", SegmentationViewValidationError),
        ("view", SegmentationViewValidationError),
        ("segment", SegmentationViewValidationError),
        ("strategy", SegmentationStrategyError),
    ),
)
def test_unknown_fields_and_strategies_fail_strictly(
    v3_2_fixture_root, mutation, error
):
    """Reject every unsupported persisted extension at its owning boundary."""
    payload = _views_payload(v3_2_fixture_root)
    if mutation == "top-level":
        payload["unexpected"] = True
    elif mutation == "view":
        payload["views"][0]["unexpected"] = True
    elif mutation == "segment":
        payload["views"][0]["segments"][0]["unexpected"] = True
    else:
        payload["views"][0]["strategy"] = "canonical-winner"

    with pytest.raises(error):
        SegmentationViewSet.from_json_bytes(
            _json_bytes(payload),
            compact_canonical_sha256=_compact_digest(v3_2_fixture_root),
        )


def test_duplicate_view_and_segment_ids_fail(v3_2_fixture_root):
    """Require stable uniqueness at aggregate and per-view levels."""
    payload = _views_payload(v3_2_fixture_root)
    payload["views"][1]["view_id"] = payload["views"][0]["view_id"]
    with pytest.raises(SegmentationViewValidationError, match="Duplicate"):
        SegmentationViewSet.from_json_bytes(
            _json_bytes(payload),
            compact_canonical_sha256=_compact_digest(v3_2_fixture_root),
        )

    payload = _views_payload(v3_2_fixture_root)
    payload["views"][0]["segments"][1]["segment_id"] = "para-1"
    with pytest.raises(SegmentationViewValidationError, match="Duplicate"):
        SegmentationViewSet.from_json_bytes(
            _json_bytes(payload),
            compact_canonical_sha256=_compact_digest(v3_2_fixture_root),
        )


def test_noncanonical_persisted_view_and_segment_order_fail(v3_2_fixture_root):
    """Reject byte-varying persisted array order instead of normalizing it."""
    payload = _views_payload(v3_2_fixture_root)
    payload["views"].reverse()
    with pytest.raises(SegmentationViewValidationError, match="strategy order"):
        SegmentationViewSet.from_json_bytes(
            _json_bytes(payload),
            compact_canonical_sha256=_compact_digest(v3_2_fixture_root),
        )

    payload = _views_payload(v3_2_fixture_root)
    payload["views"][0]["segments"].reverse()
    with pytest.raises(SegmentationViewValidationError, match="canonical order"):
        SegmentationViewSet.from_json_bytes(
            _json_bytes(payload),
            compact_canonical_sha256=_compact_digest(v3_2_fixture_root),
        )


@pytest.mark.parametrize(
    "span",
    (
        NodeSpan("pol-p1", 1, None),
        NodeSpan("pol-p1", -1, 2),
        NodeSpan("pol-p1", 2, 2),
        NodeSpan("pol-p1", 3, 2),
    ),
)
def test_invalid_local_character_ranges_fail_typed(span):
    """Reject missing, negative, empty, and reversed range endpoints."""
    with pytest.raises(SegmentationViewValidationError):
        span.validate()


def test_out_of_bounds_and_unknown_node_references_fail_typed(v3_2_fixture_root):
    """Validate every span against the exact bound canonical text."""
    service = SegmentationViewService.from_fixture(v3_2_fixture_root)
    original = service.build("view-paragraph-v1")

    unknown_segment = replace(
        original.segments[0], node_spans=(NodeSpan("missing-node"),)
    )
    unknown = replace(
        original, segments=(unknown_segment,) + original.segments[1:]
    )
    with pytest.raises(SegmentationViewReferenceError, match="Unknown canonical node"):
        service.build_view_set((unknown,))

    outside_segment = replace(
        original.segments[0], node_spans=(NodeSpan("pol-p1", 0, 10_000),)
    )
    outside = replace(
        original, segments=(outside_segment,) + original.segments[1:]
    )
    with pytest.raises(SegmentationViewReferenceError, match="outside canonical"):
        service.build_view_set((outside,))


def test_unknown_direct_and_return_scope_divisions_fail_typed(v3_2_fixture_root):
    """Resolve both direct and parent return divisions against canonical IDs."""
    service = SegmentationViewService.from_fixture(v3_2_fixture_root)

    direct = service.build("view-direct-division-v1")
    bad_direct_segment = replace(direct.segments[0], division_id="div-missing")
    bad_direct = replace(
        direct, segments=(bad_direct_segment,) + direct.segments[1:]
    )
    with pytest.raises(SegmentationViewReferenceError, match="Unknown canonical division"):
        service.build_view_set((bad_direct,))

    parent = service.build("view-parent-child-v1")
    bad_parent_segment = replace(
        parent.segments[0], return_scope=SegmentReturnScope("div-missing")
    )
    bad_parent = replace(parent, segments=(bad_parent_segment,))
    with pytest.raises(SegmentationViewReferenceError, match="Unknown canonical division"):
        service.build_view_set((bad_parent,))


def test_compact_and_production_shapes_cannot_be_partially_mixed(v3_2_fixture_root):
    """Keep the frozen adapter distinct from the complete production binding."""
    payload = _views_payload(v3_2_fixture_root)
    payload["canonical_content_sha256"] = _compact_digest(v3_2_fixture_root)

    with pytest.raises(SegmentationViewValidationError, match="partial fields"):
        SegmentationViewSet.from_json_bytes(
            _json_bytes(payload),
            compact_canonical_sha256=_compact_digest(v3_2_fixture_root),
        )


def test_view_and_view_set_serialization_are_deterministic(v3_2_fixture_root):
    """Produce stable bytes and stable cache identities across repeated calls."""
    service = SegmentationViewService.from_fixture(v3_2_fixture_root)
    view = service.build("view-paragraph-v1")

    assert view.to_json_bytes() == view.to_json_bytes()
    assert view.cache_identity == view.cache_identity
    assert service.view_set.to_json_bytes() == service.view_set.to_json_bytes()


def test_reversed_builder_input_produces_identical_production_bytes(
    frozen_canonical_artifact,
):
    """Canonicalize builder input while strict persisted readers reject disorder."""
    service = SegmentationViewService.from_canonical(frozen_canonical_artifact)
    paragraph = service.build_paragraph("paragraph-production")
    direct = service.build_direct_division("direct-production")

    forward = service.build_view_set((paragraph, direct)).to_json_bytes()
    reverse = service.build_view_set((direct, paragraph)).to_json_bytes()

    assert forward == reverse


def test_segment_compatibility_text_property_is_read_only_none():
    """Expose the scaffold property without creating serialized text state."""
    segment = SegmentationSegment("segment-1", node_spans=(NodeSpan("node-1"),))

    assert segment.text is None
    assert "text" not in segment.to_dict()
    with pytest.raises((AttributeError, TypeError)):
        segment.text = "copied"
