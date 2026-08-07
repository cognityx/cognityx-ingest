"""Production persistence and backward-compatibility tests for T02.

A parser-neutral fake adapter drives the real ``IngestService`` composition and
Storage client. The suite verifies additive canonical-content persistence,
manifest/provenance lineage, byte accounting, unchanged v2/T01 artifacts, and
document-local deletion without installing Docling or changing CLI behavior.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cognityx_ingest import (
    CANONICAL_CONTENT_SCHEMA_VERSION,
    CanonicalContentArtifact,
    CanonicalContentBuilder,
    CanonicalDocument,
    Evidence,
    IngestManager,
    IngestResult,
    IngestService,
    NativeArtifactStore,
    ProvenanceAddressCatalog,
    ProvenanceAddressResolver,
    SourceAsset,
    SourceAssetRegistry,
    SourceGraph,
)
from cognityx_ingest.parser import ExtractedPage, ExtractionResult
from cognityx_jobs import JobRepository
from cognityx_resource import ExecutionContext
from cognityx_storage import (
    LocalStorageBackend,
    StorageClient,
    StorageConfig,
    StorageRuntime,
)


class _ParserNeutralFixtureParser:
    """Return parser-neutral pages plus one opaque artifact for integration tests.

    Responsibility:
        Exercise the production model and T01 seam without a parser-private class.
    Constructed by:
        Persistence tests in this module.
    Used by:
        ``IngestService`` through the existing extraction protocol.
    Invariants:
        Returned pages and raw bytes are deterministic and immutable.
    Lifecycle/persistence:
        The parser holds no external resources; Ingest owns resulting artifacts.
    Thread-safety assumptions:
        Stateless reads make this fixture safe for ordinary test concurrency.
    """

    name = "fixture-parser"

    def extract_document(self, path: Path) -> ExtractionResult:
        """Return stable parser-neutral observations without reading the input path.

        ``IngestService`` calls this method. It returns two page observations and
        exact opaque JSON bytes, performs no writes, is idempotent, and raises no
        parser-private failures for the controlled test input.
        """
        return ExtractionResult(
            pages=(
                ExtractedPage(
                    page_number=1,
                    text="Policy heading\nFirst paragraph.",
                    page_label="1",
                    printed_page_label="1",
                ),
                ExtractedPage(page_number=2, text="Second paragraph."),
            ),
            backend=self.name,
            backend_version="fixture-1.0",
            raw_artifact=b'{"native":"fixture"}\n',
        )


def _ingest_with_canonical_content(
    tmp_path: Path,
) -> tuple[IngestResult, StorageClient, ExecutionContext, SourceAsset]:
    """Run persistence and return its result, storage, context, and SourceAsset."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    runtime = StorageRuntime.from_config(
        StorageConfig.built_in(root=tmp_path / "runtime")
    )
    registry = SourceAssetRegistry.load(runtime=runtime)
    storage = StorageClient(
        LocalStorageBackend(tmp_path / "artifacts")
    ).for_shared_data()
    context = ExecutionContext(
        run_id="run-canonical-persistence",
        correlation_id="cor-canonical-persistence",
        principal_id="fixture-test",
    )
    source = tmp_path / "fixture.pdf"
    source.write_bytes(b"fixture source bytes")
    result = IngestService(
        storage,
        extractor=_ParserNeutralFixtureParser(),
        registry=registry,
    ).ingest(source, context=context, registry=registry)
    asset = registry.show_asset(context, result.document.source.source_id)
    return result, storage, context, asset


def test_ingest_service_persists_valid_additive_canonical_content(
    tmp_path: Path,
) -> None:
    """Write, parse, and validate the new artifact at its exact document-local key."""
    result, storage, _context, _asset = _ingest_with_canonical_content(tmp_path)
    expected_key = (
        f"ingest/documents/{result.document.document_id}/canonical-content.json"
    )
    assert result.canonical_content_key == expected_key
    payload = json.load(storage.open(expected_key))
    artifact = CanonicalContentArtifact.from_dict(payload)
    assert artifact.schema == CANONICAL_CONTENT_SCHEMA_VERSION
    assert artifact.document_id == result.document.document_id
    assert artifact.resources[0].source_asset_id == result.document.source.source_id
    assert artifact.content_nodes
    assert all(node.source_selectors for node in artifact.content_nodes)
    assert artifact.presentation_units[0].labels[0].label_type == "pdf-page-label"
    assert artifact.presentation_units[0].labels[1].label_type == "printed-page-label"
    parser_native = next(
        item for item in artifact.artifact_descriptors if item.role == "parser_native"
    )
    assert parser_native.schema_version is None


def test_stored_canonical_content_uses_exact_deterministic_serializer_bytes(
    tmp_path: Path,
) -> None:
    """Match persisted bytes to the public serializer without JSON reformatting."""
    result, storage, _context, _asset = _ingest_with_canonical_content(tmp_path)
    with storage.open(result.canonical_content_key) as stream:
        stored = stream.read()
    artifact = CanonicalContentArtifact.from_dict(json.loads(stored))
    assert stored == artifact.to_json_bytes()
    assert storage.stat(result.canonical_content_key).media_type == "application/json"


def test_two_equivalent_builds_produce_identical_canonical_bytes(
    tmp_path: Path,
) -> None:
    """Prove separate equivalent builds serialize to the same UTF-8 byte sequence."""
    result, storage, context, asset = _ingest_with_canonical_content(tmp_path)
    artifact_id = f"art-{result.document.document_id}-parser_raw"
    descriptor = NativeArtifactStore(storage, context).read(artifact_id)
    descriptor_map = {descriptor.artifact_id: descriptor}
    first = CanonicalContentBuilder().build(
        result.document,
        asset,
        context,
        native_descriptors=(descriptor,),
    )
    second = CanonicalContentBuilder().build(
        result.document,
        asset,
        context,
        native_descriptors=(descriptor,),
    )
    assert first.to_json_bytes(native_descriptors=descriptor_map) == (
        second.to_json_bytes(native_descriptors=descriptor_map)
    )


def test_changed_bytes_under_existing_canonical_key_fail_without_rewrite(
    tmp_path: Path,
) -> None:
    """Reject changed retry bytes and retain the originally serialized artifact."""
    result, storage, _context, _asset = _ingest_with_canonical_content(tmp_path)
    with storage.open(result.canonical_content_key) as stream:
        original = stream.read()
    service = IngestService(storage)
    with pytest.raises(RuntimeError, match="Immutable ingest artifact"):
        service._put_immutable_bytes(
            result.canonical_content_key,
            original + b" ",
            media_type="application/json",
        )
    with storage.open(result.canonical_content_key) as stream:
        assert stream.read() == original


def test_manifest_provenance_artifacts_and_usage_include_canonical_content(
    tmp_path: Path,
) -> None:
    """Add the v3.2 reference and bytes without changing existing artifact identities."""
    result, storage, _context, _asset = _ingest_with_canonical_content(tmp_path)
    manifest = json.load(storage.open(result.manifest_key))
    provenance = json.load(storage.open(result.provenance_key))
    canonical_ref = manifest["artifacts"]["canonical_content"]
    assert canonical_ref == {
        "artifact_id": f"art-{result.document.document_id}-canonical_content",
        "uri": storage.uri(result.canonical_content_key),
    }
    assert provenance["artifact_uris"]["canonical_content"] == storage.uri(
        result.canonical_content_key
    )
    assert next(
        item
        for item in result.artifacts
        if item.artifact_id
        == f"art-{result.document.document_id}-canonical_content"
    ).uri == storage.uri(result.canonical_content_key)
    expected_output_bytes = sum(
        storage.stat(key).size_bytes
        for key in (
            result.document_key,
            result.evidence_key,
            result.manifest_key,
            result.provenance_key,
            result.canonical_content_key,
            result.source_graph_key,
            result.provenance_addresses_key,
        )
    )
    assert result.usage is not None
    assert result.usage.output_bytes == expected_output_bytes


def test_existing_v2_and_t01_artifacts_remain_readable_and_unchanged(
    tmp_path: Path,
) -> None:
    """Keep compatibility documents, evidence, provenance, manifest, and raw keys."""
    result, storage, context, _asset = _ingest_with_canonical_content(tmp_path)
    document = CanonicalDocument.from_dict(json.load(storage.open(result.document_key)))
    evidence = tuple(
        Evidence.from_dict(json.loads(line))
        for line in storage.open(result.evidence_key).read().decode("utf-8").splitlines()
    )
    assert document.to_dict() == result.document.to_dict()
    assert evidence == result.evidence
    assert json.load(storage.open(result.provenance_key))["schema_version"] == (
        "cognityx.ingest.provenance/v2"
    )
    assert json.load(storage.open(result.manifest_key))["schema_version"] == (
        "cognityx.ingest.document/v2"
    )
    expected_raw_key = (
        f"ingest/documents/{result.document.document_id}/parser/fixture-parser.json"
    )
    assert result.raw_parser_key == expected_raw_key
    assert result.raw_parser_keys == (expected_raw_key,)
    artifact_id = f"art-{result.document.document_id}-parser_raw"
    reloaded = NativeArtifactStore(storage, context).reload(artifact_id)
    assert reloaded.payload == b'{"native":"fixture"}\n'
    assert reloaded.descriptor.storage_key == expected_raw_key


def test_persisted_artifact_keeps_text_out_of_non_content_records(
    tmp_path: Path,
) -> None:
    """Recursively enforce the text-once rule on real production serialization."""
    result, storage, _context, _asset = _ingest_with_canonical_content(tmp_path)
    payload = json.load(storage.open(result.canonical_content_key))
    source_texts = {item["content"]["text"] for item in payload["content_nodes"]}
    matches: list[tuple[object, ...]] = []

    def visit(value: object, path: tuple[object, ...] = ()) -> None:
        """Record exact source-text values while ignoring unrelated metadata strings."""
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, (*path, key))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, (*path, index))
        elif isinstance(value, str) and value in source_texts:
            matches.append(path)

    visit(payload)
    assert len(matches) == len(payload["content_nodes"])
    assert all(
        len(path) == 4
        and path[0] == "content_nodes"
        and path[2:] == ("content", "text")
        for path in matches
    )


def test_ingest_service_persists_text_free_graph_and_strong_addresses(
    tmp_path: Path,
) -> None:
    """Publish additive deterministic T08 artifacts from canonical facts only."""
    result, storage, _context, _asset = _ingest_with_canonical_content(tmp_path)
    graph = SourceGraph.from_json_bytes(storage.open(result.source_graph_key).read())
    catalog = ProvenanceAddressCatalog.from_json_bytes(
        storage.open(result.provenance_addresses_key).read()
    )
    manifest = json.load(storage.open(result.manifest_key))
    provenance = json.load(storage.open(result.provenance_key))

    assert result.source_graph_key.endswith("/source-graph.json")
    assert result.provenance_addresses_key.endswith("/provenance-addresses.json")
    assert graph.content_nodes
    assert all("content" not in item for item in graph.to_dict()["content_nodes"])
    assert catalog.strong_addresses
    assert catalog.logical_addresses == ()
    assert catalog.evidence_set_addresses == ()
    assert manifest["artifacts"]["source_graph"]["uri"] == storage.uri(
        result.source_graph_key
    )
    assert provenance["artifact_uris"]["provenance_addresses"] == storage.uri(
        result.provenance_addresses_key
    )


def test_strong_resolution_survives_native_payload_deletion(tmp_path: Path) -> None:
    """Resolve from graph/catalog metadata after the independent parser payload is gone."""
    result, storage, _context, _asset = _ingest_with_canonical_content(tmp_path)
    graph_bytes = storage.open(result.source_graph_key).read()
    address_bytes = storage.open(result.provenance_addresses_key).read()
    graph = SourceGraph.from_json_bytes(graph_bytes)
    catalog = ProvenanceAddressCatalog.from_json_bytes(address_bytes)
    address_id = catalog.strong_addresses[0].address_id

    storage.delete(result.raw_parser_key)

    resolved = ProvenanceAddressResolver(graph, catalog).resolve(address_id)
    assert resolved.status == "exact"
    assert resolved.target is not None
    assert storage.open(result.source_graph_key).read() == graph_bytes
    assert storage.open(result.provenance_addresses_key).read() == address_bytes


def test_document_deletion_removes_document_local_canonical_content(
    tmp_path: Path,
) -> None:
    """Delete the new local artifact with its document but leave T07 policy untouched."""
    result, storage, context, _asset = _ingest_with_canonical_content(tmp_path)
    descriptor_key = (
        "ingest/native-artifacts/"
        f"art-{result.document.document_id}-parser_raw.json"
    )
    assert storage.exists(result.canonical_content_key)
    assert storage.exists(descriptor_key)
    IngestManager(storage, JobRepository()).delete_document(
        context, result.document.document_id
    )
    assert not storage.exists(result.canonical_content_key)
    assert storage.exists(descriptor_key)
