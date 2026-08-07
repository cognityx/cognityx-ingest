"""Parser-native artifact identity boundaries for T06 segmentation views."""

from __future__ import annotations

import pytest

from cognityx_ingest import (
    NativeArtifactDescriptor,
    NodeSpan,
    SegmentationSegment,
    SegmentationStrategyError,
    SegmentationViewReferenceError,
    SegmentationViewService,
)


def _descriptor(*pointers: str) -> NativeArtifactDescriptor:
    """Create one verified-style immutable T01 descriptor for focused T06 tests."""
    return NativeArtifactDescriptor(
        artifact_id="native-docling-1",
        parser_id="docling",
        parser_version="fixture",
        sha256="1" * 64,
        size_bytes=123,
        media_type="application/vnd.docling.document+json",
        storage_key="documents/doc-1/native/native-docling-1/payload.json",
        uri="storage://documents/doc-1/native/native-docling-1/payload.json",
        native_pointers=tuple(pointers),
        retention_class="temporary-audit",
        run_id="run-1",
        correlation_id="correlation-1",
    )


def test_production_parser_native_view_requires_real_artifact_identity(
    frozen_canonical_artifact,
):
    """Bind chunker observations to a supplied T01 descriptor and canonical span."""
    descriptor = _descriptor("#/texts/0")
    service = SegmentationViewService.from_canonical(
        frozen_canonical_artifact,
        native_descriptors={descriptor.artifact_id: descriptor},
    )
    segment = SegmentationSegment(
        segment_id="native-segment-1",
        native_chunk_pointer="#/chunks/0",
        node_spans=(NodeSpan("pol-p2"),),
    )

    view = service.build_parser_native(
        "native-production",
        chunker_id="docling-hierarchical",
        native_artifact_id=descriptor.artifact_id,
        segments=(segment,),
    )

    assert view.profile.to_dict() == {
        "chunker_id": "docling-hierarchical",
        "native_artifact_id": "native-docling-1",
    }
    assert view.segments[0].native_chunk_pointer == "#/chunks/0"
    assert view.segments[0].node_spans == (NodeSpan("pol-p2"),)


def test_unknown_native_artifact_identity_fails_typed(frozen_canonical_artifact):
    """Never silently invent a parser-native artifact binding."""
    service = SegmentationViewService.from_canonical(frozen_canonical_artifact)
    segment = SegmentationSegment(
        segment_id="native-segment-1",
        native_chunk_pointer="#/chunks/0",
        node_spans=(NodeSpan("pol-p2"),),
    )

    with pytest.raises(SegmentationViewReferenceError, match="Unknown native artifact"):
        service.build_parser_native(
            "native-production",
            chunker_id="docling-hierarchical",
            native_artifact_id="missing-artifact",
            segments=(segment,),
        )


def test_native_descriptor_mapping_rejects_aliased_artifact_identity(
    frozen_canonical_artifact,
):
    """Reject a descriptor stored under a key other than its immutable artifact ID."""
    descriptor = _descriptor()

    with pytest.raises(SegmentationViewReferenceError, match="mapping identity"):
        SegmentationViewService.from_canonical(
            frozen_canonical_artifact,
            native_descriptors={"artifact-alias": descriptor},
        )


@pytest.mark.parametrize("pointer", ("chunks/0", "#/chunks/~2bad", "#/" + "x" * 1025))
def test_native_chunk_pointer_syntax_is_bounded(
    frozen_canonical_artifact, pointer
):
    """Reject malformed chunker pointers before any native payload navigation."""
    descriptor = _descriptor()
    service = SegmentationViewService.from_canonical(
        frozen_canonical_artifact,
        native_descriptors={descriptor.artifact_id: descriptor},
    )
    segment = SegmentationSegment(
        segment_id="native-segment-1",
        native_chunk_pointer=pointer,
        node_spans=(NodeSpan("pol-p2"),),
    )

    with pytest.raises(SegmentationStrategyError, match="pointer"):
        service.build_parser_native(
            "native-production",
            chunker_id="docling-hierarchical",
            native_artifact_id=descriptor.artifact_id,
            segments=(segment,),
        )


def test_native_chunk_payload_is_not_copied_into_serialized_view(v3_2_fixture_root):
    """Preserve native identity and pointer while excluding opaque payload text."""
    service = SegmentationViewService.from_fixture(v3_2_fixture_root)
    view = service.build("view-docling-structure-v1")
    native_payload = (
        v3_2_fixture_root / "native_artifacts" / "docling_document_opaque.json"
    ).read_bytes()
    distinctive_native_text = b"The ordinary approval limit is"

    assert distinctive_native_text in native_payload
    assert distinctive_native_text not in view.to_json_bytes()
    assert view.profile.get("native_artifact_id") == "art-docling-001"


def test_native_pointer_changes_cache_identity_without_changing_canonical_binding(
    frozen_canonical_artifact,
):
    """Include exact native reference structure in deterministic cache identity."""
    descriptor = _descriptor()
    service = SegmentationViewService.from_canonical(
        frozen_canonical_artifact,
        native_descriptors={descriptor.artifact_id: descriptor},
    )

    first = service.build_parser_native(
        "native-production",
        chunker_id="docling-hierarchical",
        native_artifact_id=descriptor.artifact_id,
        segments=(
            SegmentationSegment(
                segment_id="native-segment-1",
                native_chunk_pointer="#/chunks/0",
                node_spans=(NodeSpan("pol-p2"),),
            ),
        ),
    )
    second = service.build_parser_native(
        "native-production",
        chunker_id="docling-hierarchical",
        native_artifact_id=descriptor.artifact_id,
        segments=(
            SegmentationSegment(
                segment_id="native-segment-1",
                native_chunk_pointer="#/chunks/1",
                node_spans=(NodeSpan("pol-p2"),),
            ),
        ),
    )

    assert first.canonical_content_sha256 == second.canonical_content_sha256
    assert first.cache_identity != second.cache_identity
