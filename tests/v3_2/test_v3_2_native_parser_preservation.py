"""Native parser preservation tests and planned T01 strict xfail.

The fixture includes an opaque Docling-like artifact that must be preserved
byte-for-byte by future production code. The strict xfail names the intended
durable store/read/reload API without requiring optional Docling in normal CI.
The optional integration test remains separate for environments that install
Docling.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from cognityx_ingest import DoclingParser
from cognityx_resource import ExecutionContext
from cognityx_storage import LocalStorageBackend, StorageClient


def _native_artifact_store(tmp_path: Path):
    """Return the planned durable native-artifact store production API."""
    from cognityx_ingest.native_artifacts import NativeArtifactStore

    storage = StorageClient(LocalStorageBackend(tmp_path / "artifacts")).for_shared_data()
    context = ExecutionContext(
        run_id="run-native",
        correlation_id="cor-native",
        principal_id="fixture-test",
    )
    return NativeArtifactStore(storage=storage, context=context)


@pytest.mark.xfail(strict=True, reason="T01: durable native-artifact store/read/reload API is not implemented")
def test_docling_native_artifact_round_trip_preserves_bytes_and_native_pointers(
    tmp_path: Path, v3_2_fixture_root: Path
):
    """Call the planned durable native-artifact API with the opaque fixture."""
    artifact = v3_2_fixture_root / "native_artifacts" / "docling_document_opaque.json"
    bindings = json.loads(
        (v3_2_fixture_root / "expected" / "native_bindings.json").read_text(
            encoding="utf-8"
        )
    )
    native_pointer = next(
        item["native_pointer"]
        for item in bindings["bindings"]
        if item["artifact_id"] == "art-docling-001"
    )
    original_bytes = artifact.read_bytes()
    store = _native_artifact_store(tmp_path)
    stored = store.store(
        parser_id="docling",
        artifact_id="art-docling-001",
        payload=original_bytes,
        native_pointers=(native_pointer,),
    )
    reloaded = store.reload(stored.artifact_id)
    assert reloaded.payload == original_bytes
    assert reloaded.sha256 == hashlib.sha256(original_bytes).hexdigest()
    assert native_pointer in reloaded.native_pointers


def test_opaque_docling_artifact_fixture_contains_native_pointers(v3_2_fixture_root: Path) -> None:
    """Validate the frozen opaque native artifact has resolvable pointers."""
    payload = json.loads(
        (v3_2_fixture_root / "native_artifacts" / "docling_document_opaque.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["texts"][0]["self_ref"] == "#/texts/0"
    assert payload["texts"][1]["self_ref"] == "#/texts/1"


def test_docling_optional_parser_still_parses_fixture_when_available(provenance_pdf: Path) -> None:
    """Exercise real Docling only when the optional dependency is installed."""
    pytest.importorskip("docling", reason="Native preservation coverage is optional in normal CI")
    extraction = DoclingParser().extract_document(provenance_pdf)
    assert extraction.backend == "docling"
    assert extraction.raw_artifact is not None
