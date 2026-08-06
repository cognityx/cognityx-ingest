"""Native parser preservation tests for the completed T01 production seam.

The fixture includes an opaque Docling-like artifact that must be preserved
byte-for-byte. Deterministic tests exercise immutable publication, lazy metadata
reads, integrity failures, pointer validation, and existing IngestService output
without requiring optional Docling in normal CI. The optional real-parser check
remains separate for environments that already install Docling.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from cognityx_ingest import (
    DoclingParser,
    IngestManager,
    IngestService,
    NativeArtifactConflictError,
    NativeArtifactError,
    NativeArtifactIntegrityError,
    NativeArtifactNotFoundError,
    NativeArtifactStore,
    NativePointerError,
    PyPdfExtractor,
    SourceAssetRegistry,
)
from cognityx_ingest.parser import ExtractedPage, ExtractionResult
from cognityx_jobs import JobRepository
from cognityx_resource import ExecutionContext
from cognityx_storage import (
    LocalStorageBackend,
    ObjectAlreadyExistsError,
    StorageClient,
    StorageConfig,
    StorageRuntime,
)


def _native_artifact_components(
    tmp_path: Path,
) -> tuple[NativeArtifactStore, StorageClient, ExecutionContext]:
    """Build the real store, scoped storage client, and audit context for a test."""
    storage = StorageClient(LocalStorageBackend(tmp_path / "artifacts")).for_shared_data()
    context = ExecutionContext(
        run_id="run-native",
        correlation_id="cor-native",
        principal_id="fixture-test",
    )
    return NativeArtifactStore(storage=storage, context=context), storage, context


def test_docling_native_artifact_round_trip_preserves_bytes_and_native_pointers(
    tmp_path: Path, v3_2_fixture_root: Path
) -> None:
    """Round-trip the frozen opaque fixture through the real durable store API."""
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
    store, _storage, _context = _native_artifact_components(tmp_path)
    stored = store.store(
        parser_id="docling",
        artifact_id="art-docling-001",
        payload=original_bytes,
        media_type="application/vnd.docling.document+json",
        native_pointers=(native_pointer,),
    )
    reloaded = store.reload(stored.artifact_id)
    assert reloaded.payload == original_bytes
    assert reloaded.sha256 == hashlib.sha256(original_bytes).hexdigest()
    assert native_pointer in reloaded.native_pointers


def test_store_read_reload_exact_bytes_and_stable_descriptor(tmp_path: Path) -> None:
    """Verify exact bytes, digest, size, pointers, and metadata-only reads."""
    store, _storage, _context = _native_artifact_components(tmp_path)
    payload = b'{"texts":[{"text":"exact"}]}\n'
    descriptor = store.store(
        artifact_id="art-exact",
        parser_id="fixture",
        parser_version="1.2.3",
        payload=payload,
        media_type="application/json",
        native_pointers=("#/texts/0",),
    )

    assert store.read("art-exact") == descriptor
    reloaded = store.reload("art-exact")
    assert reloaded.payload == payload
    assert reloaded.sha256 == hashlib.sha256(payload).hexdigest()
    assert descriptor.size_bytes == len(payload)
    assert descriptor.native_pointers == ("#/texts/0",)


def test_repeated_equivalent_store_is_idempotent(tmp_path: Path) -> None:
    """Accept an equivalent retry while retaining one payload and descriptor."""
    store, storage, _context = _native_artifact_components(tmp_path)
    arguments = {
        "artifact_id": "art-idempotent",
        "parser_id": "fixture",
        "parser_version": "v1",
        "payload": b'{"stable":true}',
        "media_type": "application/json",
        "native_pointers": ("#/stable",),
    }

    first = store.store(**arguments)
    second = store.store(**arguments)

    assert second == first
    assert storage.open(first.storage_key).read() == arguments["payload"]


def test_same_artifact_id_with_changed_payload_fails(tmp_path: Path) -> None:
    """Reject changed bytes under an identity whose payload is immutable."""
    store, _storage, _context = _native_artifact_components(tmp_path)
    store.store(
        artifact_id="art-conflict",
        parser_id="fixture",
        payload=b'{"version":1}',
        media_type="application/json",
    )

    with pytest.raises(NativeArtifactConflictError, match="payload conflicts"):
        store.store(
            artifact_id="art-conflict",
            parser_id="fixture",
            payload=b'{"version":2}',
            media_type="application/json",
        )


def test_same_artifact_id_with_changed_metadata_fails(tmp_path: Path) -> None:
    """Reject metadata drift even when the retained payload bytes are identical."""
    store, _storage, _context = _native_artifact_components(tmp_path)
    payload = b'{"version":1}'
    store.store(
        artifact_id="art-metadata-conflict",
        parser_id="fixture",
        parser_version="v1",
        payload=payload,
        media_type="application/json",
    )

    with pytest.raises(NativeArtifactConflictError, match="descriptor conflicts"):
        store.store(
            artifact_id="art-metadata-conflict",
            parser_id="fixture",
            parser_version="v2",
            payload=payload,
            media_type="application/json",
        )


@pytest.mark.parametrize(
    "retry_payload",
    [b'{"version":2}', b'{"version":1}'],
    ids=["changed-bytes", "identical-bytes"],
)
def test_existing_descriptor_conflict_does_not_create_alternate_payload(
    tmp_path: Path, retry_payload: bytes
) -> None:
    """Preflight descriptor ownership before either kind of alternate-key retry.

    This public-seam regression covers both changed and identical bytes because
    the descriptor's payload key is itself immutable metadata. Neither rejected
    retry may leave an unreferenced object at its requested alternate key.
    """
    store, storage, _context = _native_artifact_components(tmp_path)
    original_key = "ingest/documents/doc-conflict/parser/original.json"
    alternate_key = "ingest/documents/doc-conflict/parser/alternate.json"
    original = store.store(
        artifact_id="art-no-orphan",
        parser_id="fixture",
        payload=b'{"version":1}',
        media_type="application/json",
        payload_key=original_key,
    )

    with pytest.raises(NativeArtifactConflictError, match="descriptor conflicts"):
        store.store(
            artifact_id="art-no-orphan",
            parser_id="fixture",
            payload=retry_payload,
            media_type="application/json",
            payload_key=alternate_key,
        )

    assert store.read(original.artifact_id) == original
    assert storage.open(original_key).read() == b'{"version":1}'
    assert not storage.exists(alternate_key)


@pytest.mark.parametrize(
    ("field_name", "valid_identifier"),
    [
        ("artifact_id", "A"),
        ("parser_id", "Parser_2.1-alpha"),
        ("retention_class", "temporary-audit.v2"),
        ("run_id", "Run_2026.08-01"),
        ("correlation_id", "Correlation_2026.08-01"),
        ("artifact_id", "A" * 128),
    ],
)
def test_bounded_ascii_identifiers_are_accepted(
    tmp_path: Path, field_name: str, valid_identifier: str
) -> None:
    """Accept portable identifier characters and the documented upper bound.

    The test drives every persisted identifier through ``store`` so callers can
    continue using UUID-like and human-readable IDs without relying on private
    validators.
    """
    run_id = valid_identifier if field_name == "run_id" else "run-valid"
    correlation_id = (
        valid_identifier
        if field_name == "correlation_id"
        else "correlation-valid"
    )
    storage = StorageClient(
        LocalStorageBackend(tmp_path / field_name)
    ).for_shared_data()
    context = ExecutionContext(
        run_id=run_id,
        correlation_id=correlation_id,
        principal_id="fixture-test",
    )
    arguments: dict[str, object] = {
        "artifact_id": "art-valid",
        "parser_id": "parser-valid",
        "payload": b"valid",
        "media_type": "application/octet-stream",
        "retention_class": "temporary-audit",
    }
    if field_name in arguments:
        arguments[field_name] = valid_identifier

    descriptor = NativeArtifactStore(storage, context).store(**arguments)

    descriptor_field = {
        "artifact_id": "artifact_id",
        "parser_id": "parser_id",
        "retention_class": "retention_class",
        "run_id": "run_id",
        "correlation_id": "correlation_id",
    }[field_name]
    assert getattr(descriptor, descriptor_field) == valid_identifier


@pytest.mark.parametrize(
    "invalid_identifier",
    [
        "",
        " leading",
        "trailing ",
        "embedded space",
        "path/segment",
        r"path\segment",
        "line\nbreak",
        "control\x00byte",
        "query?value",
        "fragment#value",
        "scheme:value",
        "A" * 129,
    ],
    ids=[
        "empty",
        "leading-space",
        "trailing-space",
        "embedded-space",
        "forward-slash",
        "backslash",
        "newline",
        "control",
        "query",
        "fragment",
        "colon",
        "too-long",
    ],
)
@pytest.mark.parametrize(
    "field_name",
    ["artifact_id", "parser_id", "retention_class", "run_id", "correlation_id"],
)
def test_unsafe_identifiers_are_rejected_at_the_public_store_boundary(
    tmp_path: Path,
    field_name: str,
    invalid_identifier: str,
) -> None:
    """Reject path, control, URI-syntax, whitespace, and oversized identifiers.

    Every persisted identity field is exercised through the production store.
    Rejection happens before payload publication, protecting logical keys, logs,
    and future API transport from ambiguous identifier syntax.
    """
    run_id = invalid_identifier if field_name == "run_id" else "run-valid"
    correlation_id = (
        invalid_identifier
        if field_name == "correlation_id"
        else "correlation-valid"
    )
    storage = StorageClient(
        LocalStorageBackend(tmp_path / field_name)
    ).for_shared_data()
    context = ExecutionContext(
        run_id=run_id,
        correlation_id=correlation_id,
        principal_id="fixture-test",
    )
    arguments: dict[str, object] = {
        "artifact_id": "art-invalid",
        "parser_id": "parser-valid",
        "payload": b"must-not-persist",
        "media_type": "application/octet-stream",
        "retention_class": "temporary-audit",
    }
    if field_name in arguments:
        arguments[field_name] = invalid_identifier

    with pytest.raises(NativeArtifactError, match="must be 1-128 ASCII"):
        NativeArtifactStore(storage, context).store(**arguments)

    assert not storage.exists("ingest/native-artifacts")


def test_tampered_stored_payload_fails_reload(tmp_path: Path) -> None:
    """Detect a payload changed outside the immutable StorageClient API."""
    store, storage, _context = _native_artifact_components(tmp_path)
    descriptor = store.store(
        artifact_id="art-tampered",
        parser_id="fixture",
        payload=b'{"trusted":true}',
        media_type="application/json",
    )
    local_payload = storage.resolve_local_path(descriptor.storage_key)
    assert local_payload is not None
    local_payload.write_bytes(b'{"trusted":false}')

    with pytest.raises(NativeArtifactIntegrityError, match="descriptor"):
        store.reload("art-tampered")


def test_missing_artifact_produces_typed_error(tmp_path: Path) -> None:
    """Expose descriptor absence through the stable domain exception hierarchy."""
    store, _storage, _context = _native_artifact_components(tmp_path)

    with pytest.raises(NativeArtifactNotFoundError, match="not found"):
        store.read("art-missing")
    with pytest.raises(NativeArtifactNotFoundError, match="not found"):
        store.reload("art-missing")


def test_missing_payload_produces_typed_error(tmp_path: Path) -> None:
    """Distinguish a lost payload from descriptor corruption or parser failure."""
    store, storage, _context = _native_artifact_components(tmp_path)
    descriptor = store.store(
        artifact_id="art-missing-payload",
        parser_id="fixture",
        payload=b'{"stored":true}',
        media_type="application/json",
    )
    storage.delete(descriptor.storage_key)

    with pytest.raises(NativeArtifactNotFoundError, match="payload not found"):
        store.reload(descriptor.artifact_id)


def test_untrusted_descriptor_json_is_validated(tmp_path: Path) -> None:
    """Reject malformed stored metadata before constructing a typed descriptor."""
    store, storage, _context = _native_artifact_components(tmp_path)
    descriptor = store.store(
        artifact_id="art-bad-descriptor",
        parser_id="fixture",
        payload=b'{"stored":true}',
        media_type="application/json",
    )
    descriptor_key = "ingest/native-artifacts/art-bad-descriptor.json"
    local_descriptor = storage.resolve_local_path(descriptor_key)
    assert local_descriptor is not None
    local_descriptor.write_text('{"artifact_id":"wrong"}\n', encoding="utf-8")

    with pytest.raises(NativeArtifactIntegrityError, match="fields are invalid"):
        store.read(descriptor.artifact_id)


def test_descriptor_cannot_redirect_reload_to_unrelated_ingest_object(
    tmp_path: Path,
) -> None:
    """Reject a valid-looking descriptor that targets canonical Ingest content.

    The tampered descriptor carries a matching URI, size, and digest for the
    unrelated object, proving namespace authorization happens independently of
    byte integrity and before ``reload`` can open that target.
    """
    store, storage, _context = _native_artifact_components(tmp_path)
    descriptor = store.store(
        artifact_id="art-redirection",
        parser_id="fixture",
        payload=b'{"native":true}',
        media_type="application/json",
    )
    unrelated_key = "ingest/documents/doc-unrelated/provenance.json"
    unrelated_payload = b'{"provenance":"not-native"}'
    storage.put_bytes(
        unrelated_key,
        unrelated_payload,
        media_type="application/json",
    )
    descriptor_path = storage.resolve_local_path(
        "ingest/native-artifacts/art-redirection.json"
    )
    assert descriptor_path is not None
    tampered = json.loads(descriptor_path.read_text(encoding="utf-8"))
    tampered.update(
        {
            "sha256": hashlib.sha256(unrelated_payload).hexdigest(),
            "size_bytes": len(unrelated_payload),
            "storage_key": unrelated_key,
            "uri": storage.uri(unrelated_key),
        }
    )
    descriptor_path.write_text(
        json.dumps(tampered, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        NativeArtifactIntegrityError,
        match="outside an approved native-payload namespace",
    ):
        store.reload(descriptor.artifact_id)


def test_equivalent_descriptor_race_retains_shared_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep payload bytes when a concurrent writer publishes the same descriptor.

    The storage hook simulates another writer winning between descriptor
    preflight and atomic publication. Equivalent metadata is idempotent, the
    payload remains available, and reload still verifies the exact bytes.
    """
    store, storage, _context = _native_artifact_components(tmp_path)
    original_put_json = storage.put_json

    def publish_equivalent_then_report_race(
        key: str, value: object
    ) -> None:
        """Publish the competing equivalent descriptor before signalling loss."""
        original_put_json(key, value)
        raise ObjectAlreadyExistsError("simulated equivalent descriptor race")

    monkeypatch.setattr(storage, "put_json", publish_equivalent_then_report_race)
    payload = b'{"race":"equivalent"}'
    descriptor = store.store(
        artifact_id="art-equivalent-race",
        parser_id="fixture",
        payload=payload,
        media_type="application/json",
    )

    assert storage.exists(descriptor.storage_key)
    assert store.reload(descriptor.artifact_id).payload == payload


def test_incompatible_descriptor_race_removes_only_losing_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compensate an incompatible publication race without harming its winner.

    The simulated winner uses a different approved parser key. The losing call
    created its default payload before discovering the winner, so that exact
    unreferenced object must be removed while the winning payload and descriptor
    remain readable.
    """
    store, storage, _context = _native_artifact_components(tmp_path)
    original_put_json = storage.put_json
    winner_key = "ingest/documents/doc-race/parser/winner.json"
    winner_payload = b'{"race":"winner"}'

    def publish_incompatible_then_report_race(
        key: str, value: object
    ) -> None:
        """Install a valid incompatible winner before reporting atomic loss."""
        assert isinstance(value, dict)
        storage.put_bytes(
            winner_key,
            winner_payload,
            media_type="application/json",
        )
        winner = dict(value)
        winner.update(
            {
                "parser_id": "winner-parser",
                "sha256": hashlib.sha256(winner_payload).hexdigest(),
                "size_bytes": len(winner_payload),
                "storage_key": winner_key,
                "uri": storage.uri(winner_key),
            }
        )
        original_put_json(key, winner)
        raise ObjectAlreadyExistsError("simulated incompatible descriptor race")

    monkeypatch.setattr(storage, "put_json", publish_incompatible_then_report_race)
    losing_key = "ingest/native-artifacts/art-incompatible-race/payload"

    with pytest.raises(NativeArtifactConflictError, match="descriptor conflicts"):
        store.store(
            artifact_id="art-incompatible-race",
            parser_id="losing-parser",
            payload=b'{"race":"loser"}',
            media_type="application/json",
        )

    assert not storage.exists(losing_key)
    assert storage.open(winner_key).read() == winner_payload
    assert store.reload("art-incompatible-race").payload == winner_payload


def test_invalid_json_pointer_produces_typed_error(tmp_path: Path) -> None:
    """Reject a stored JSON pointer that cannot resolve in the exact payload."""
    store, _storage, _context = _native_artifact_components(tmp_path)
    store.store(
        artifact_id="art-pointer",
        parser_id="fixture",
        payload=b'{"texts":[]}',
        media_type="application/json",
        native_pointers=("#/texts/0",),
    )

    with pytest.raises(NativePointerError, match="does not resolve"):
        store.reload("art-pointer")


def test_non_json_pointer_remains_opaque(tmp_path: Path) -> None:
    """Preserve parser-defined pointers when Cognityx cannot resolve the media."""
    store, _storage, _context = _native_artifact_components(tmp_path)
    pointer = "parser-private:item:17"
    descriptor = store.store(
        artifact_id="art-opaque-pointer",
        parser_id="fixture-binary",
        payload=b"opaque parser bytes",
        media_type="application/vnd.example.parser",
        native_pointers=(pointer,),
    )

    assert store.reload(descriptor.artifact_id).native_pointers == (pointer,)


def test_read_does_not_load_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prove metadata reads do not open the potentially large parser payload."""
    store, storage, _context = _native_artifact_components(tmp_path)
    descriptor = store.store(
        artifact_id="art-lazy-read",
        parser_id="fixture",
        payload=b'{"large":"payload"}',
        media_type="application/json",
    )
    original_open = storage.open

    def guarded_open(key: str):
        """Fail the test if a descriptor-only read touches payload storage."""
        if key == descriptor.storage_key:
            raise AssertionError("read() loaded the native payload")
        return original_open(key)

    monkeypatch.setattr(storage, "open", guarded_open)

    assert store.read(descriptor.artifact_id) == descriptor


def test_explicit_existing_parser_payload_key_is_reused(tmp_path: Path) -> None:
    """Reuse a current parser key and prove the default payload path stays absent."""
    store, storage, _context = _native_artifact_components(tmp_path)
    explicit_key = "ingest/documents/pdf-example/parser/fixture.json"
    payload = b'{"native":true}'
    storage.put_bytes(explicit_key, payload, media_type="application/json")
    descriptor = store.store(
        artifact_id="art-explicit-key",
        parser_id="fixture",
        payload=payload,
        media_type="application/json",
        payload_key=explicit_key,
    )

    assert descriptor.storage_key == explicit_key
    assert storage.open(explicit_key).read() == payload
    assert not storage.exists(
        "ingest/native-artifacts/art-explicit-key/payload"
    )


class _RawFixtureParser:
    """Return one deterministic raw artifact for IngestService integration tests.

    The test constructs this parser, ``IngestService`` uses it, and its only
    responsibility is to expose normalized page data plus opaque bytes. It keeps
    no lifecycle state and is safe for the single-threaded test composition.
    """

    name = "fixture-parser"

    def __init__(self, payload: bytes) -> None:
        """Retain immutable fixture bytes that ``extract_document`` will return."""
        self._payload = payload

    def extract_document(self, path: Path) -> ExtractionResult:
        """Return normalized content without reading or transforming the raw bytes."""
        return ExtractionResult(
            pages=(ExtractedPage(page_number=1, text="Fixture page"),),
            backend=self.name,
            backend_version="fixture-1.0",
            raw_artifact=self._payload,
        )


def test_ingest_service_reuses_raw_path_and_enriches_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integrate one fake parser while preserving legacy manifests and readers."""
    runtime = StorageRuntime.from_config(
        StorageConfig.built_in(root=tmp_path / "runtime")
    )
    registry = SourceAssetRegistry.load(runtime=runtime)
    storage = StorageClient(
        LocalStorageBackend(tmp_path / "artifacts")
    ).for_shared_data()
    context = ExecutionContext(
        run_id="run-native-integration",
        correlation_id="cor-native-integration",
        principal_id="fixture-test",
    )
    source = tmp_path / "fixture.pdf"
    source.write_bytes(b"fixture source bytes")
    raw_payload = b'{"native":"exact","items":[1,2]}\n'
    payload_writes: list[str] = []
    original_put_bytes = storage.put_bytes

    def recording_put_bytes(
        key: str, content: bytes, *, media_type: str = "application/octet-stream"
    ):
        """Record logical writes while delegating unchanged bytes to Storage."""
        payload_writes.append(key)
        return original_put_bytes(key, content, media_type=media_type)

    monkeypatch.setattr(storage, "put_bytes", recording_put_bytes)
    result = IngestService(
        storage,
        extractor=_RawFixtureParser(raw_payload),
        registry=registry,
    ).ingest(source, context=context, registry=registry)

    expected_key = (
        f"ingest/documents/{result.document.document_id}/parser/fixture-parser.json"
    )
    expected_artifact_id = f"art-{result.document.document_id}-parser_raw"
    assert result.raw_parser_key == expected_key
    assert result.raw_parser_keys == (expected_key,)
    assert payload_writes.count(expected_key) == 1
    assert storage.open(expected_key).read() == raw_payload

    provenance_bytes = storage.open(result.provenance_key).read()
    provenance = json.loads(provenance_bytes)
    raw_record = provenance["parser"]["raw_artifacts"][0]
    assert raw_record == {
        "artifact_id": expected_artifact_id,
        "backend": "fixture-parser",
        "parser_id": "fixture-parser",
        "parser_version": "fixture-1.0",
        "sha256": hashlib.sha256(raw_payload).hexdigest(),
        "size_bytes": len(raw_payload),
        "media_type": "application/json",
        "uri": storage.uri(expected_key),
        "descriptor_uri": storage.uri(
            f"ingest/native-artifacts/{expected_artifact_id}.json"
        ),
        "retention_class": "temporary-audit",
        "native_pointers": [],
    }

    reloaded = NativeArtifactStore(storage, context).reload(expected_artifact_id)
    assert reloaded.payload == raw_payload
    manifest = json.load(storage.open(result.manifest_key))
    assert manifest["artifacts"]["parser_raw"] == {
        "artifact_id": expected_artifact_id,
        "uri": storage.uri(expected_key),
    }
    parser_ref = next(
        item for item in result.artifacts if item.artifact_id == expected_artifact_id
    )
    assert parser_ref.uri == storage.uri(expected_key)
    manager = IngestManager(storage, JobRepository())
    assert manager.read_artifact(
        context, result.document.document_id, "provenance"
    ) == provenance_bytes


def test_fusion_contributors_keep_individual_versions(tmp_path: Path) -> None:
    """Read contributor versions from fusion diagnostics, not the wrapper version."""
    runtime = StorageRuntime.from_config(
        StorageConfig.built_in(root=tmp_path / "fusion-runtime")
    )
    registry = SourceAssetRegistry.load(runtime=runtime)
    storage = StorageClient(
        LocalStorageBackend(tmp_path / "fusion-artifacts")
    ).for_shared_data()
    context = ExecutionContext(
        run_id="run-native-fusion",
        correlation_id="cor-native-fusion",
        principal_id="fixture-test",
    )
    source = tmp_path / "fusion.pdf"
    source.write_bytes(b"fusion source bytes")
    extraction = ExtractionResult(
        pages=(ExtractedPage(page_number=1, text="Fused page"),),
        backend="fusion",
        backend_version="wrapper-version-must-not-leak",
        raw_artifact=b'{"fusion":true}',
        raw_artifacts={
            "docling": b'{"docling":true}',
            "pymupdf": b'{"pymupdf":true}',
        },
        diagnostics={
            "backend_versions": {"docling": "2.50", "pymupdf": "1.26"}
        },
    )

    class _FusionFixtureParser:
        """Expose a prepared fusion result without invoking real parser packages."""

        name = "fusion"

        def extract_document(self, path: Path) -> ExtractionResult:
            """Return the prepared result so persistence alone is under test."""
            return extraction

    result = IngestService(
        storage, extractor=_FusionFixtureParser(), registry=registry
    ).ingest(source, context=context, registry=registry)
    records = {
        item["backend"]: item
        for item in json.load(storage.open(result.provenance_key))["parser"][
            "raw_artifacts"
        ]
    }

    assert records["docling"]["parser_version"] == "2.50"
    assert records["pymupdf"]["parser_version"] == "1.26"
    assert records["fusion"]["parser_version"] == "wrapper-version-must-not-leak"


def test_parser_without_raw_artifact_does_not_fabricate_one(
    tmp_path: Path, provenance_pdf: Path
) -> None:
    """Keep baseline parsers working without inventing payloads or descriptors."""
    runtime = StorageRuntime.from_config(
        StorageConfig.built_in(root=tmp_path / "plain-runtime")
    )
    registry = SourceAssetRegistry.load(runtime=runtime)
    storage = StorageClient(
        LocalStorageBackend(tmp_path / "plain-artifacts")
    ).for_shared_data()
    context = ExecutionContext(
        run_id="run-without-native-artifact",
        correlation_id="cor-without-native-artifact",
        principal_id="fixture-test",
    )

    result = IngestService(
        storage, extractor=PyPdfExtractor(), registry=registry
    ).ingest(provenance_pdf, context=context, registry=registry)

    assert result.raw_parser_key is None
    assert result.raw_parser_keys == ()
    assert not storage.exists("ingest/native-artifacts")


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
