"""T01-backed NativeBinding validation tests for the T02 canonical model.

The frozen binding pack is exercised through the real durable
``NativeArtifactStore``. Tests prove canonical IDs, native descriptor identities,
and retained pointers resolve without copying native payload bytes into the new
canonical-content artifact.
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from cognityx_ingest import (
    CanonicalArtifactDescriptor,
    CanonicalContentArtifact,
    NativeArtifactDescriptor,
    NativeArtifactStore,
    NativeBinding,
    NativeBindingValidationError,
)
from cognityx_resource import ExecutionContext
from cognityx_storage import LocalStorageBackend, StorageClient


def _stored_frozen_bindings(
    tmp_path: Path,
    v3_2_fixture_root: Path,
) -> tuple[
    tuple[NativeBinding, ...],
    dict[str, NativeArtifactDescriptor],
    tuple[CanonicalArtifactDescriptor, ...],
]:
    """Store frozen native bytes and return explicit bindings plus both descriptor views."""
    expected = json.loads(
        (v3_2_fixture_root / "expected" / "native_bindings.json").read_text(
            encoding="utf-8"
        )
    )
    storage = StorageClient(
        LocalStorageBackend(tmp_path / "native-artifacts")
    ).for_shared_data()
    context = ExecutionContext(
        run_id="run-native-bindings",
        correlation_id="cor-native-bindings",
        principal_id="fixture-test",
    )
    store = NativeArtifactStore(storage, context)
    pointers_by_artifact: dict[str, tuple[str, ...]] = {}
    for artifact in expected["artifacts"]:
        pointers_by_artifact[artifact["artifact_id"]] = tuple(
            item["native_pointer"]
            for item in expected["bindings"]
            if item["artifact_id"] == artifact["artifact_id"]
        )
    descriptors: dict[str, NativeArtifactDescriptor] = {}
    generic: list[CanonicalArtifactDescriptor] = []
    for item in expected["artifacts"]:
        payload = (v3_2_fixture_root / item["path"]).read_bytes()
        descriptor = store.store(
            artifact_id=item["artifact_id"],
            parser_id=item["parser_id"],
            payload=payload,
            media_type=item["media_type"],
            retention_class=item["retention_class"],
            native_pointers=pointers_by_artifact[item["artifact_id"]],
        )
        assert store.reload(descriptor.artifact_id).payload == payload
        descriptors[descriptor.artifact_id] = descriptor
        generic.append(
            CanonicalArtifactDescriptor(
                artifact_id=descriptor.artifact_id,
                role="parser_native",
                uri=descriptor.uri,
                media_type=descriptor.media_type,
                sha256=descriptor.sha256,
                schema_version=None,
            )
        )
    bindings = tuple(
        sorted(
            (NativeBinding(**item) for item in expected["bindings"]),
            key=lambda item: item.binding_id,
        )
    )
    return bindings, descriptors, tuple(
        sorted(generic, key=lambda item: item.artifact_id)
    )


def test_frozen_native_bindings_validate_against_real_t01_descriptors(
    tmp_path: Path,
    v3_2_fixture_root: Path,
    frozen_canonical_artifact: CanonicalContentArtifact,
) -> None:
    """Accept all frozen canonical/native links after actual T01 store and reload."""
    bindings, descriptors, generic = _stored_frozen_bindings(
        tmp_path, v3_2_fixture_root
    )
    artifact = replace(
        frozen_canonical_artifact,
        native_bindings=bindings,
        artifact_descriptors=generic,
    )
    artifact.validate(native_descriptors=descriptors)
    assert artifact.direct_nodes(
        "div-policy-4.2", native_descriptors=descriptors
    )
    assert artifact.subtree_nodes(
        "div-policy-root", native_descriptors=descriptors
    )
    assert {item.binding_id for item in artifact.native_bindings} == {
        "bind-pol-heading-42-docling",
        "bind-pol-p2-docling",
        "bind-pol-p3-pymupdf-link",
    }
    assert all(item.schema_version is None for item in generic)


def test_mapping_key_cannot_spoof_native_descriptor_identity(
    tmp_path: Path,
    v3_2_fixture_root: Path,
    frozen_canonical_artifact: CanonicalContentArtifact,
) -> None:
    """Reject a descriptor stored under a mapping key for another artifact ID."""
    bindings, descriptors, generic = _stored_frozen_bindings(
        tmp_path, v3_2_fixture_root
    )
    artifact_id = bindings[0].artifact_id
    descriptors[artifact_id] = replace(
        descriptors[artifact_id],
        artifact_id="artifact-spoofed",
    )
    artifact = replace(
        frozen_canonical_artifact,
        native_bindings=bindings,
        artifact_descriptors=generic,
    )
    with pytest.raises(NativeBindingValidationError, match="identity disagrees"):
        artifact.validate(native_descriptors=descriptors)


@pytest.mark.parametrize(
    ("field_name", "replacement", "message"),
    (
        ("uri", "storage://shared/another-payload", "URI disagrees"),
        ("media_type", "application/octet-stream", "media type disagrees"),
        ("sha256", "0" * 64, "SHA-256 disagrees"),
    ),
)
def test_generic_parser_payload_metadata_must_match_t01_descriptor(
    tmp_path: Path,
    v3_2_fixture_root: Path,
    frozen_canonical_artifact: CanonicalContentArtifact,
    field_name: str,
    replacement: str,
    message: str,
) -> None:
    """Cross-check generic URI, media type, and digest against T01 payload facts."""
    bindings, descriptors, generic = _stored_frozen_bindings(
        tmp_path, v3_2_fixture_root
    )
    artifact_id = bindings[0].artifact_id
    changed = tuple(
        replace(item, **{field_name: replacement})
        if item.artifact_id == artifact_id
        else item
        for item in generic
    )
    artifact = replace(
        frozen_canonical_artifact,
        native_bindings=bindings,
        artifact_descriptors=changed,
    )
    with pytest.raises(NativeBindingValidationError, match=message):
        artifact.validate(native_descriptors=descriptors)


def test_invalid_native_pointer_raises_typed_binding_error(
    tmp_path: Path,
    v3_2_fixture_root: Path,
    frozen_canonical_artifact: CanonicalContentArtifact,
) -> None:
    """Reject a pointer not retained and verified by the referenced T01 descriptor."""
    bindings, descriptors, generic = _stored_frozen_bindings(
        tmp_path, v3_2_fixture_root
    )
    invalid_binding = replace(bindings[0], native_pointer="#/missing")
    artifact = replace(
        frozen_canonical_artifact,
        native_bindings=(invalid_binding, *bindings[1:]),
        artifact_descriptors=generic,
    )
    with pytest.raises(NativeBindingValidationError, match="not retained"):
        artifact.validate(native_descriptors=descriptors)


def test_missing_native_descriptor_raises_typed_binding_error(
    tmp_path: Path,
    v3_2_fixture_root: Path,
    frozen_canonical_artifact: CanonicalContentArtifact,
) -> None:
    """Reject a generic artifact reference that has no authoritative T01 descriptor."""
    bindings, descriptors, generic = _stored_frozen_bindings(
        tmp_path, v3_2_fixture_root
    )
    missing_id = bindings[0].artifact_id
    descriptors.pop(missing_id)
    artifact = replace(
        frozen_canonical_artifact,
        native_bindings=bindings,
        artifact_descriptors=generic,
    )
    with pytest.raises(NativeBindingValidationError, match="descriptor is missing"):
        artifact.validate(native_descriptors=descriptors)


def test_missing_canonical_binding_target_raises_typed_error(
    tmp_path: Path,
    v3_2_fixture_root: Path,
    frozen_canonical_artifact: CanonicalContentArtifact,
) -> None:
    """Reject a native pointer attached to a nonexistent canonical record."""
    bindings, descriptors, generic = _stored_frozen_bindings(
        tmp_path, v3_2_fixture_root
    )
    invalid = replace(bindings[0], canonical_id="canonical-missing")
    artifact = replace(
        frozen_canonical_artifact,
        native_bindings=(invalid, *bindings[1:]),
        artifact_descriptors=generic,
    )
    with pytest.raises(NativeBindingValidationError, match="canonical ID"):
        artifact.validate(native_descriptors=descriptors)


def test_empty_native_binding_collection_is_valid(
    frozen_canonical_artifact: CanonicalContentArtifact,
) -> None:
    """Keep normal parsers valid when adapters provide no explicit native pointers."""
    frozen_canonical_artifact.validate()
    assert frozen_canonical_artifact.native_bindings == ()


def test_serialized_native_binding_contains_no_native_payload(
    tmp_path: Path,
    v3_2_fixture_root: Path,
    frozen_canonical_artifact: CanonicalContentArtifact,
) -> None:
    """Persist only IDs and pointers even after T01 validates the native bytes."""
    bindings, descriptors, generic = _stored_frozen_bindings(
        tmp_path, v3_2_fixture_root
    )
    artifact = replace(
        frozen_canonical_artifact,
        native_bindings=bindings,
        artifact_descriptors=generic,
    )
    artifact.validate(native_descriptors=descriptors)
    records = artifact.to_dict()["native_bindings"]
    assert all(set(item) == {
        "binding_id",
        "canonical_id",
        "artifact_id",
        "native_pointer",
        "binding_role",
    } for item in records)
