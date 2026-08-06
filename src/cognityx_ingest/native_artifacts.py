"""Preserve parser-native artifacts without interpreting or reducing them.

Purpose
-------
Parser libraries can return details that the stable Cognityx document model does
not yet expose. This module keeps those original bytes separate from canonical
records so later code can inspect the authoritative parser output without asking
the parser to run again.

Design principles
-----------------
Payload bytes are immutable, descriptors are small independently readable
records, and every reload verifies the stored SHA-256 and byte count. The store
depends only on the provider-neutral ``StorageClient``; it never imports Docling,
PyMuPDF, or another parser-private type. Native pointers are retained exactly as
the parser supplied them.

Processing flow
---------------
``store`` hashes the caller's bytes and checks descriptor ownership before it
touches a payload key. It then publishes or verifies one payload object and
atomically publishes an immutable descriptor. ``read`` validates that descriptor
and confines its payload key to an Ingest-owned native-artifact namespace.
``reload`` reads both objects, verifies payload integrity, and resolves local RFC
6901 pointers when the media type identifies JSON. Repeated equivalent stores are
idempotent, while any conflicting payload or descriptor fails explicitly without
leaving a payload created by an incompatible losing writer.

Primary consumers
-----------------
``IngestService`` writes native payloads and descriptors. T02's future
``NativeBinding`` model, T07 retention work, audit tooling, and future SDK read
APIs consume the descriptors. DataForge normally consumes canonical document and
provenance output rather than these raw parser payloads.

Ownership boundary
------------------
Cognityx Ingest owns parser-artifact identity, metadata, and integrity checks.
Cognityx Storage owns physical publication and retrieval. Parsers own the meaning
of their payloads and any parser-defined pointer vocabulary.

Non-goals
---------
This module does not parse documents, normalize native data, perform parser
routing or fusion, infer semantic bindings, decide retention or purge policy, or
provide a second content-addressed storage system.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from urllib.parse import unquote

from cognityx_resource import ExecutionContext
from cognityx_storage import (
    ObjectAlreadyExistsError,
    ObjectNotFoundError,
    StorageClient,
    StorageError,
)

_DESCRIPTOR_SCHEMA = "cognityx.ingest.native-artifact-descriptor/v1"
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class NativeArtifactError(Exception):
    """Base failure for the native-artifact persistence boundary.

    Responsibility:
        Give Ingest and future SDK callers a stable domain error instead of a
        backend-specific storage or JSON exception.
    Constructed by:
        ``NativeArtifactStore`` and its validation helpers.
    Used by:
        Service orchestration, audit tools, and callers that handle every native
        artifact failure uniformly.
    Invariants:
        The message identifies the failed artifact operation without including
        parser payload contents, credentials, or local paths.
    Lifecycle and persistence:
        Exception instances are transient and are never stored.
    Thread safety:
        Instances contain immutable diagnostic text and may cross worker
        boundaries like ordinary Python exceptions.
    """


class NativeArtifactNotFoundError(NativeArtifactError, FileNotFoundError):
    """Report that a requested native-artifact descriptor or payload is absent.

    Responsibility:
        Distinguish a missing artifact from malformed or corrupted content.
    Constructed by:
        ``NativeArtifactStore.read`` and ``NativeArtifactStore.reload``.
    Used by:
        API layers that translate absence into a not-found response.
    Invariants:
        The error names a logical artifact ID, never a physical backend path.
    Lifecycle and persistence:
        This transient error does not create or mutate stored objects.
    Thread safety:
        The exception carries no mutable shared state.
    """


class NativeArtifactConflictError(NativeArtifactError):
    """Reject an attempt to overwrite an immutable native-artifact identity.

    Responsibility:
        Make changed bytes or metadata under an existing artifact ID explicit.
    Constructed by:
        ``NativeArtifactStore.store`` during immutable publication checks.
    Used by:
        Ingest orchestration and retry logic that must not hide conflicting runs.
    Invariants:
        Existing bytes and descriptors remain untouched after this error.
    Lifecycle and persistence:
        The error describes a failed write; no replacement is persisted.
    Thread safety:
        Concurrent equivalent writers are accepted, while conflicting writers
        deterministically receive this error when Storage provides atomic
        no-overwrite publication.
    """


class NativeArtifactIntegrityError(NativeArtifactError):
    """Report a descriptor or payload that fails independent integrity checks.

    Responsibility:
        Separate stored-data corruption from a caller's attempted overwrite.
    Constructed by:
        Descriptor validation and ``NativeArtifactStore.reload``.
    Used by:
        Audit, repair, and API layers that must stop using untrusted bytes.
    Invariants:
        A reload never returns bytes after hash or size verification fails.
    Lifecycle and persistence:
        Verification is read-only and never repairs or replaces stored data.
    Thread safety:
        The exception contains no mutable shared state.
    """


class NativePointerError(NativeArtifactError):
    """Report an invalid or unresolvable pointer in a JSON native artifact.

    Responsibility:
        Identify pointer defects without pretending opaque non-JSON pointers can
        be interpreted by Cognityx.
    Constructed by:
        JSON pointer validation during ``NativeArtifactStore.reload``.
    Used by:
        Future binding readers and audit tooling.
    Invariants:
        Pointer validation never rewrites payload bytes or normalizes the stored
        pointer string.
    Lifecycle and persistence:
        Validation is read-only; the invalid descriptor remains available for
        diagnosis.
    Thread safety:
        The exception contains only immutable context.
    """


@dataclass(frozen=True, slots=True)
class NativeArtifactDescriptor:
    """Describe one immutable parser-native payload in provider-neutral storage.

    Responsibility:
        Carry stable identity, parser metadata, integrity facts, logical storage
        location, retention classification, and native pointers without carrying
        the potentially large payload.
    Constructed by:
        ``NativeArtifactStore.store`` after hashing bytes, or ``read`` after
        validating untrusted descriptor JSON.
    Used by:
        ``IngestService``, provenance readers, future NativeBinding and SDK APIs,
        and later retention work.
    Invariants:
        Identity fields are non-empty, SHA-256 is lowercase hexadecimal, size is
        non-negative, URI matches ``storage_key``, and pointers are immutable.
    Lifecycle and persistence:
        The descriptor is written once under a stable artifact-ID key and may be
        read without loading its payload.
    Thread safety:
        Frozen fields and tuple pointers make instances safe to share; storage
        client thread safety is governed by the selected backend.
    """

    artifact_id: str
    parser_id: str
    parser_version: str | None
    sha256: str
    size_bytes: int
    media_type: str
    storage_key: str
    uri: str
    native_pointers: tuple[str, ...]
    retention_class: str
    run_id: str
    correlation_id: str

    def to_dict(self) -> dict[str, object]:
        """Return the stable JSON representation persisted by the store.

        ``NativeArtifactStore`` is the caller. The method converts only the tuple
        of pointers to JSON's list form and adds the descriptor schema; it does
        not read or write Storage. Repeated calls are deterministic and cannot
        mutate this frozen record.
        """
        return {
            "schema": _DESCRIPTOR_SCHEMA,
            "artifact_id": self.artifact_id,
            "parser_id": self.parser_id,
            "parser_version": self.parser_version,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "storage_key": self.storage_key,
            "uri": self.uri,
            "native_pointers": list(self.native_pointers),
            "retention_class": self.retention_class,
            "run_id": self.run_id,
            "correlation_id": self.correlation_id,
        }


@dataclass(frozen=True, slots=True)
class ReloadedNativeArtifact:
    """Return a verified descriptor together with its exact original bytes.

    Responsibility:
        Package the integrity-checked payload with the descriptor that governs it.
    Constructed by:
        ``NativeArtifactStore.reload`` only after hash, size, and applicable JSON
        pointer validation succeeds.
    Used by:
        Audit tools, future NativeBinding readers, and parser-specific consumers.
    Invariants:
        ``payload`` hashes to ``descriptor.sha256`` and has exactly
        ``descriptor.size_bytes`` bytes.
    Lifecycle and persistence:
        This in-memory result does not create another stored payload.
    Thread safety:
        Both bytes and the frozen descriptor are immutable.
    """

    descriptor: NativeArtifactDescriptor
    payload: bytes

    @property
    def sha256(self) -> str:
        """Expose the verified digest for callers comparing artifact identities."""
        return self.descriptor.sha256

    @property
    def native_pointers(self) -> tuple[str, ...]:
        """Expose the exact stored pointers without requiring descriptor plumbing."""
        return self.descriptor.native_pointers


class NativeArtifactStore:
    """Persist and verify opaque parser payloads through Cognityx Storage.

    Responsibility:
        Enforce one immutable payload and descriptor per native artifact ID.
    Constructed by:
        ``IngestService`` during persistence, or application composition code
        that already has a ``StorageClient`` and ``ExecutionContext``.
    Used by:
        Ingest writes, audit reloads, and future SDK/native-binding readers.
    Invariants:
        Store never overwrites, a descriptor conflict is detected before normal
        payload publication, descriptor keys cannot redirect reads outside
        Ingest-owned native payload namespaces, and JSON pointers are checked only
        for media types Cognityx can parse safely.
    Lifecycle and persistence:
        Instances are lightweight. Payloads live at explicit or derived logical
        keys; descriptors live under ``base_prefix`` and survive process restarts.
    Thread safety:
        The instance keeps only immutable configuration. Concurrent safety relies
        on Storage's atomic no-overwrite behavior, with races re-read and checked.
    """

    def __init__(
        self,
        storage: StorageClient,
        context: ExecutionContext,
        *,
        base_prefix: str = "ingest/native-artifacts",
    ) -> None:
        """Bind artifact operations to one storage scope and execution context.

        Application composition and ``IngestService`` call this constructor. It
        validates the logical descriptor prefix once, stores no payload state,
        and performs no I/O. The supplied run and correlation IDs are copied into
        every new descriptor to retain audit context.

        Raises:
            NativeArtifactError: If the descriptor prefix is empty or unsafe.
        """
        self._storage = storage
        self._context = context
        self._base_prefix = _validate_storage_key(base_prefix, "base_prefix")

    def store(
        self,
        *,
        artifact_id: str,
        parser_id: str,
        payload: bytes,
        media_type: str,
        parser_version: str | None = None,
        native_pointers: tuple[str, ...] = (),
        retention_class: str = "temporary-audit",
        payload_key: str | None = None,
    ) -> NativeArtifactDescriptor:
        """Publish original parser bytes and an immutable descriptor.

        ``IngestService`` and direct parser integrations call this after parsing.
        The method validates metadata, hashes the original ``payload``, derives
        stable keys, and checks any existing descriptor before writing payload
        bytes. If the identity is free, it publishes or verifies the payload and
        atomically publishes the descriptor. A losing incompatible descriptor
        race removes only a payload this call definitely created and which the
        winning descriptor does not reference.

        Args:
            artifact_id: Stable Cognityx artifact identity.
            parser_id: Parser/backend identity that produced the payload.
            payload: Exact authoritative bytes; mutable byte arrays are rejected.
            media_type: Payload media type used to decide whether pointers can be
                validated generically.
            parser_version: Optional version of the contributing parser.
            native_pointers: Opaque pointer strings retained in caller order.
            retention_class: Classification for future T07 policy work.
            payload_key: Existing logical parser key to reuse. When omitted, a
                stable key below ``base_prefix`` is derived.

        Returns:
            The persisted immutable descriptor.

        Side effects:
            Writes at most one payload object and one descriptor object. It never
            logs payload contents or stores local paths or credentials. A known
            incompatible descriptor leaves no alternate payload from this call.

        Idempotency and immutability:
            An equivalent repeated call returns the existing descriptor. Changed
            bytes or metadata under the same identity are never overwritten.

        Raises:
            NativeArtifactConflictError: Existing bytes or metadata conflict.
            NativeArtifactError: Inputs are invalid or Storage cannot complete
                the operation safely.
        """
        artifact_id = _validate_identifier(artifact_id, "artifact_id")
        parser_id = _validate_identifier(parser_id, "parser_id")
        media_type = _validate_text(media_type, "media_type")
        retention_class = _validate_identifier(
            retention_class, "retention_class"
        )
        if parser_version is not None:
            parser_version = _validate_text(parser_version, "parser_version")
        if not isinstance(payload, bytes):
            raise NativeArtifactError("payload must be immutable bytes")
        pointers = _validate_pointers(native_pointers)
        selected_payload_key = (
            _validate_storage_key(payload_key, "payload_key")
            if payload_key is not None
            else self._payload_key(artifact_id)
        )
        if not _payload_key_is_approved(selected_payload_key, self._base_prefix):
            raise NativeArtifactError(
                "payload_key must remain in an approved native-artifact namespace"
            )
        payload_sha256 = _sha256(payload)
        descriptor = NativeArtifactDescriptor(
            artifact_id=artifact_id,
            parser_id=parser_id,
            parser_version=parser_version,
            sha256=payload_sha256,
            size_bytes=len(payload),
            media_type=media_type,
            storage_key=selected_payload_key,
            uri=self._storage.uri(selected_payload_key),
            native_pointers=pointers,
            retention_class=retention_class,
            run_id=_validate_identifier(self._context.run_id, "run_id"),
            correlation_id=_validate_identifier(
                self._context.correlation_id, "correlation_id"
            ),
        )
        existing = self._read_descriptor_if_exists(artifact_id)
        if existing is not None:
            if existing != descriptor:
                raise _descriptor_conflict(existing, descriptor)
            self._verify_existing_payload(existing)
            return existing

        payload_created = self._store_payload(descriptor, payload)
        return self._publish_descriptor(
            descriptor,
            payload_created=payload_created,
        )

    def read(self, artifact_id: str) -> NativeArtifactDescriptor:
        """Read and validate descriptor metadata without loading payload bytes.

        Provenance readers and list/detail APIs use this bounded operation when
        they need identity or integrity metadata but not the potentially large
        parser output. It performs one descriptor read, validates untrusted JSON
        and URI/key consistency, and rejects payload redirection outside the
        configured native prefix or established legacy parser path. It has no
        write side effects.

        Args:
            artifact_id: Stable identity whose descriptor should be loaded.

        Returns:
            A validated frozen descriptor.

        Raises:
            NativeArtifactNotFoundError: The descriptor does not exist.
            NativeArtifactIntegrityError: Stored JSON is malformed or invalid.
            NativeArtifactError: Storage fails for another domain-relevant reason.
        """
        artifact_id = _validate_identifier(artifact_id, "artifact_id")
        key = self._descriptor_key(artifact_id)
        try:
            if not self._storage.exists(key):
                raise NativeArtifactNotFoundError(
                    f"Native artifact descriptor not found: {artifact_id}"
                )
            with self._storage.open(key) as stream:
                try:
                    value = json.load(stream)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise NativeArtifactIntegrityError(
                        f"Native artifact descriptor is not valid JSON: {artifact_id}"
                    ) from error
        except NativeArtifactError:
            raise
        except ObjectNotFoundError as error:
            raise NativeArtifactNotFoundError(
                f"Native artifact descriptor not found: {artifact_id}"
            ) from error
        except StorageError as error:
            raise NativeArtifactError(
                f"Could not read native artifact descriptor: {artifact_id}"
            ) from error
        descriptor = _descriptor_from_json(value, artifact_id=artifact_id)
        if not _payload_key_is_approved(
            descriptor.storage_key, self._base_prefix
        ):
            raise NativeArtifactIntegrityError(
                "Native artifact descriptor points outside an approved "
                f"native-payload namespace: {artifact_id}"
            )
        expected_uri = self._storage.uri(descriptor.storage_key)
        if descriptor.uri != expected_uri:
            raise NativeArtifactIntegrityError(
                f"Native artifact descriptor URI does not match its storage key: {artifact_id}"
            )
        return descriptor

    def reload(self, artifact_id: str) -> ReloadedNativeArtifact:
        """Reload exact bytes, verify integrity, and validate supported pointers.

        Audit tools and future NativeBinding readers call this when they need the
        actual parser payload. The method first uses ``read``, then loads the
        descriptor's payload key, recomputes byte count and SHA-256, and validates
        local RFC 6901 pointers for JSON media types. Non-JSON pointers remain
        parser-defined opaque strings.

        Args:
            artifact_id: Stable identity whose bytes should be verified.

        Returns:
            The validated descriptor and original payload bytes.

        Side effects:
            None; reload never repairs, rewrites, or copies stored content.

        Raises:
            NativeArtifactNotFoundError: Descriptor or payload is absent.
            NativeArtifactIntegrityError: Size or SHA-256 differs.
            NativePointerError: JSON or a stored JSON pointer is invalid.
            NativeArtifactError: Storage fails for another domain-relevant reason.
        """
        descriptor = self.read(artifact_id)
        try:
            with self._storage.open(descriptor.storage_key) as stream:
                payload = stream.read()
        except ObjectNotFoundError as error:
            raise NativeArtifactNotFoundError(
                f"Native artifact payload not found: {descriptor.artifact_id}"
            ) from error
        except StorageError as error:
            raise NativeArtifactError(
                f"Could not reload native artifact payload: {descriptor.artifact_id}"
            ) from error
        _verify_payload(descriptor, payload)
        _validate_json_pointers(descriptor, payload)
        return ReloadedNativeArtifact(descriptor=descriptor, payload=payload)

    def descriptor_uri(self, artifact_id: str) -> str:
        """Return the stable provider-neutral URI of an artifact descriptor.

        ``IngestService`` uses this value in provenance so consumers can locate
        descriptor metadata without loading the payload. The method validates the
        ID and derives the URI without I/O or mutation.

        Args:
            artifact_id: Stable native artifact identity.

        Returns:
            A ``storage://`` URI in this client's configured scope.

        Raises:
            NativeArtifactError: The artifact ID is empty or unsafe.
        """
        return self._storage.uri(
            self._descriptor_key(_validate_identifier(artifact_id, "artifact_id"))
        )

    def _descriptor_key(self, artifact_id: str) -> str:
        """Derive one stable metadata key so an ID cannot acquire two descriptors."""
        return f"{self._base_prefix}/{artifact_id}.json"

    def _payload_key(self, artifact_id: str) -> str:
        """Derive the default payload key without implying a parser file format."""
        return f"{self._base_prefix}/{artifact_id}/payload"

    def _store_payload(
        self, descriptor: NativeArtifactDescriptor, payload: bytes
    ) -> bool:
        """Publish exact bytes and report whether this call created the object.

        ``store`` uses the boolean to decide whether a later incompatible
        descriptor race may be rolled back safely. Existing or concurrently won
        payloads are verified byte-for-byte and return ``False`` so this writer
        never assumes ownership of another writer's object.
        """
        try:
            if self._storage.exists(descriptor.storage_key):
                self._verify_existing_payload(descriptor)
                return False
            try:
                self._storage.put_bytes(
                    descriptor.storage_key,
                    payload,
                    media_type=descriptor.media_type,
                )
            except ObjectAlreadyExistsError:
                self._verify_existing_payload(descriptor)
                return False
        except NativeArtifactError:
            raise
        except StorageError as error:
            raise NativeArtifactError(
                f"Could not store native artifact payload: {descriptor.artifact_id}"
            ) from error
        return True

    def _verify_existing_payload(
        self, descriptor: NativeArtifactDescriptor
    ) -> None:
        """Verify retained bytes before callers accept an idempotent publication.

        Store retries and descriptor-race winners call this method. It reads one
        payload without changing it, then compares independent byte count and
        SHA-256 facts so equal metadata cannot conceal different native evidence.
        """
        try:
            with self._storage.open(descriptor.storage_key) as stream:
                existing = stream.read()
        except ObjectNotFoundError as error:
            raise NativeArtifactError(
                f"Native artifact payload disappeared during publication: {descriptor.artifact_id}"
            ) from error
        if (
            len(existing) != descriptor.size_bytes
            or _sha256(existing) != descriptor.sha256
        ):
            raise NativeArtifactConflictError(
                f"Native artifact payload conflicts with immutable key: {descriptor.artifact_id}"
            )

    def _read_descriptor_if_exists(
        self, artifact_id: str
    ) -> NativeArtifactDescriptor | None:
        """Preflight immutable identity without weakening descriptor validation.

        ``store`` calls the public ``read`` seam so malformed descriptors and
        namespace redirection still fail closed. Only a typed not-found result is
        converted to ``None``; every integrity or storage error remains visible.
        """
        try:
            return self.read(artifact_id)
        except NativeArtifactNotFoundError:
            return None

    def _publish_descriptor(
        self,
        descriptor: NativeArtifactDescriptor,
        *,
        payload_created: bool,
    ) -> NativeArtifactDescriptor:
        """Publish metadata once and adjudicate only the atomic creation race.

        Normal existing descriptors were handled by ``store`` before payload I/O.
        If another writer wins after that preflight, this method reads the winner,
        accepts and verifies an equivalent publication, or removes only this
        call's unreferenced payload before raising a typed conflict. Cleanup uses
        the exact validated winner observed here rather than performing a second
        descriptor read with another race window.
        """
        key = self._descriptor_key(descriptor.artifact_id)
        try:
            try:
                self._storage.put_json(key, descriptor.to_dict())
            except ObjectAlreadyExistsError:
                existing = self.read(descriptor.artifact_id)
                if existing != descriptor:
                    if (
                        payload_created
                        and existing.storage_key != descriptor.storage_key
                    ):
                        self._rollback_created_payload(descriptor)
                    raise _descriptor_conflict(existing, descriptor)
                self._verify_existing_payload(existing)
                return existing
        except NativeArtifactError:
            raise
        except StorageError as error:
            raise NativeArtifactError(
                f"Could not store native artifact descriptor: {descriptor.artifact_id}"
            ) from error
        return descriptor

    def _rollback_created_payload(
        self, descriptor: NativeArtifactDescriptor
    ) -> None:
        """Remove only an unreferenced payload proven to belong to this failed call.

        ``store`` invokes this compensating action only when ``_store_payload``
        reported successful creation and an incompatible descriptor won at a
        different key. The method re-verifies hash and size immediately before
        deletion. Storage objects are immutable, so a matching object is the one
        this call published; a missing object already satisfies the cleanup goal.
        """
        try:
            with self._storage.open(descriptor.storage_key) as stream:
                payload = stream.read()
            _verify_payload(descriptor, payload)
            self._storage.delete(descriptor.storage_key)
        except ObjectNotFoundError:
            return
        except NativeArtifactError:
            raise
        except StorageError as error:
            raise NativeArtifactError(
                "Could not remove payload after native artifact descriptor "
                f"conflict: {descriptor.artifact_id}"
            ) from error


def _descriptor_from_json(
    value: object, *, artifact_id: str
) -> NativeArtifactDescriptor:
    """Validate every untrusted descriptor field before constructing the record."""
    if not isinstance(value, dict):
        raise NativeArtifactIntegrityError(
            f"Native artifact descriptor must be a JSON object: {artifact_id}"
        )
    expected_fields = {
        "schema",
        "artifact_id",
        "parser_id",
        "parser_version",
        "sha256",
        "size_bytes",
        "media_type",
        "storage_key",
        "uri",
        "native_pointers",
        "retention_class",
        "run_id",
        "correlation_id",
    }
    if set(value) != expected_fields:
        raise NativeArtifactIntegrityError(
            f"Native artifact descriptor fields are invalid: {artifact_id}"
        )
    if value["schema"] != _DESCRIPTOR_SCHEMA:
        raise NativeArtifactIntegrityError(
            f"Native artifact descriptor schema is unsupported: {artifact_id}"
        )
    try:
        stored_artifact_id = _validate_identifier(value["artifact_id"], "artifact_id")
        parser_id = _validate_identifier(value["parser_id"], "parser_id")
        parser_version_value = value["parser_version"]
        parser_version = (
            None
            if parser_version_value is None
            else _validate_text(parser_version_value, "parser_version")
        )
        sha256 = _validate_sha256(value["sha256"])
        size_bytes = _validate_size(value["size_bytes"])
        media_type = _validate_text(value["media_type"], "media_type")
        storage_key = _validate_storage_key(value["storage_key"], "storage_key")
        uri = _validate_text(value["uri"], "uri")
        native_pointers = _validate_pointers(value["native_pointers"])
        retention_class = _validate_identifier(
            value["retention_class"], "retention_class"
        )
        run_id = _validate_identifier(value["run_id"], "run_id")
        correlation_id = _validate_identifier(
            value["correlation_id"], "correlation_id"
        )
    except NativeArtifactError as error:
        raise NativeArtifactIntegrityError(
            f"Native artifact descriptor values are invalid: {artifact_id}"
        ) from error
    if stored_artifact_id != artifact_id:
        raise NativeArtifactIntegrityError(
            f"Native artifact descriptor identity does not match its key: {artifact_id}"
        )
    return NativeArtifactDescriptor(
        artifact_id=stored_artifact_id,
        parser_id=parser_id,
        parser_version=parser_version,
        sha256=sha256,
        size_bytes=size_bytes,
        media_type=media_type,
        storage_key=storage_key,
        uri=uri,
        native_pointers=native_pointers,
        retention_class=retention_class,
        run_id=run_id,
        correlation_id=correlation_id,
    )


def _sha256(payload: bytes) -> str:
    """Hash exact bytes once so descriptor identity never depends on decoding."""
    return hashlib.sha256(payload).hexdigest()


def _descriptor_conflict(
    existing: NativeArtifactDescriptor,
    requested: NativeArtifactDescriptor,
) -> NativeArtifactConflictError:
    """Describe immutable identity disagreement without exposing payload content.

    ``store`` and its descriptor-race handler use this small policy helper. A
    changed digest or byte count at the same key is reported as a payload
    conflict for backward-compatible diagnostics; every other disagreement is a
    descriptor conflict. The helper only constructs an exception and performs no
    storage I/O.
    """
    conflict_kind = (
        "payload"
        if existing.storage_key == requested.storage_key
        and (
            existing.sha256 != requested.sha256
            or existing.size_bytes != requested.size_bytes
        )
        else "descriptor"
    )
    return NativeArtifactConflictError(
        f"Native artifact {conflict_kind} conflicts with immutable ID: "
        f"{requested.artifact_id}"
    )


def _verify_payload(descriptor: NativeArtifactDescriptor, payload: bytes) -> None:
    """Reject a reload unless both independent size and digest checks still hold."""
    if len(payload) != descriptor.size_bytes:
        raise NativeArtifactIntegrityError(
            f"Native artifact size does not match its descriptor: {descriptor.artifact_id}"
        )
    if _sha256(payload) != descriptor.sha256:
        raise NativeArtifactIntegrityError(
            f"Native artifact SHA-256 does not match its descriptor: {descriptor.artifact_id}"
        )


def _validate_json_pointers(
    descriptor: NativeArtifactDescriptor, payload: bytes
) -> None:
    """Resolve JSON pointers for JSON media while leaving every stored byte intact."""
    if not descriptor.native_pointers or not _is_json_media_type(
        descriptor.media_type
    ):
        return
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NativePointerError(
            "JSON native artifact cannot be parsed for pointer validation: "
            f"{descriptor.artifact_id}"
        ) from error
    for pointer in descriptor.native_pointers:
        _resolve_json_pointer(document, pointer, descriptor.artifact_id)


def _resolve_json_pointer(document: object, pointer: str, artifact_id: str) -> object:
    """Resolve one local RFC 6901 URI-fragment pointer without normalizing it."""
    if not pointer.startswith("#"):
        raise NativePointerError(
            f"Native JSON pointer must be a local fragment for {artifact_id}: {pointer}"
        )
    fragment = unquote(pointer[1:])
    if fragment == "":
        return document
    if not fragment.startswith("/"):
        raise NativePointerError(
            f"Native JSON pointer is invalid for {artifact_id}: {pointer}"
        )
    current = document
    for raw_token in fragment[1:].split("/"):
        token = _decode_pointer_token(raw_token, pointer, artifact_id)
        if isinstance(current, dict):
            if token not in current:
                raise NativePointerError(
                    f"Native JSON pointer does not resolve for {artifact_id}: {pointer}"
                )
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                raise NativePointerError(
                    f"Native JSON array pointer is invalid for {artifact_id}: {pointer}"
                )
            index = int(token)
            if index >= len(current):
                raise NativePointerError(
                    f"Native JSON pointer does not resolve for {artifact_id}: {pointer}"
                )
            current = current[index]
        else:
            raise NativePointerError(
                f"Native JSON pointer traverses a scalar for {artifact_id}: {pointer}"
            )
    return current


def _decode_pointer_token(token: str, pointer: str, artifact_id: str) -> str:
    """Decode RFC 6901 escapes while rejecting ambiguous unsupported sequences."""
    result: list[str] = []
    index = 0
    while index < len(token):
        character = token[index]
        if character != "~":
            result.append(character)
            index += 1
            continue
        if index + 1 >= len(token) or token[index + 1] not in {"0", "1"}:
            raise NativePointerError(
                f"Native JSON pointer escape is invalid for {artifact_id}: {pointer}"
            )
        result.append("~" if token[index + 1] == "0" else "/")
        index += 2
    return "".join(result)


def _is_json_media_type(media_type: str) -> bool:
    """Recognize standard and structured-suffix JSON without parser knowledge."""
    normalized = media_type.partition(";")[0].strip().lower()
    return normalized == "application/json" or normalized.endswith("+json")


def _validate_identifier(value: object, field_name: str) -> str:
    """Constrain persisted identities to one bounded portable ASCII token.

    Store calls this for artifact, parser, retention, run, and correlation IDs;
    descriptor reads apply the same rule to untrusted JSON. Requiring an ASCII
    alphanumeric first character followed by at most 127 alphanumeric, dot,
    underscore, or hyphen characters blocks path separators, whitespace,
    controls, and URI query or fragment syntax before an ID reaches a key or log.
    """
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise NativeArtifactError(
            f"{field_name} must be 1-128 ASCII letters, digits, dots, "
            "underscores, or hyphens and start with a letter or digit"
        )
    return value


def _payload_key_is_approved(storage_key: str, base_prefix: str) -> bool:
    """Confine native descriptors to current or structural legacy payload keys.

    ``store`` uses this guard before publication and ``read`` uses it before a
    descriptor can direct ``reload`` to another object. Keys below the configured
    native-artifact prefix are current storage. The only compatibility exception
    is the existing five-part
    ``ingest/documents/<document-id>/parser/<backend-file>`` shape. Storage cannot
    enforce this distinction because it intentionally treats logical keys as
    opaque; Ingest owns knowledge of parser-native versus canonical objects.
    """
    if storage_key.startswith(f"{base_prefix}/"):
        return True
    parts = storage_key.split("/")
    return (
        len(parts) == 5
        and parts[0] == "ingest"
        and parts[1] == "documents"
        and _IDENTIFIER_PATTERN.fullmatch(parts[2]) is not None
        and parts[3] == "parser"
        and _IDENTIFIER_PATTERN.fullmatch(parts[4]) is not None
    )


def _validate_storage_key(value: object, field_name: str) -> str:
    """Keep descriptor keys logical and portable without exposing backend paths."""
    text = _validate_text(value, field_name)
    if (
        text.startswith("/")
        or text.startswith("~")
        or re.match(r"^[A-Za-z]:/", text) is not None
        or "\\" in text
        or "//" in text
        or any(segment in {"", ".", ".."} for segment in text.split("/"))
    ):
        raise NativeArtifactError(f"{field_name} must be a safe logical storage key")
    return text.rstrip("/")


def _validate_text(value: object, field_name: str) -> str:
    """Require non-empty exact text so descriptor serialization is deterministic."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise NativeArtifactError(
            f"{field_name} must be non-empty text without outer whitespace"
        )
    return value


def _validate_pointers(value: object) -> tuple[str, ...]:
    """Freeze caller order and reject non-string pointers without changing values."""
    if not isinstance(value, (tuple, list)):
        raise NativeArtifactError("native_pointers must be a tuple or stored JSON list")
    pointers = tuple(value)
    if any(not isinstance(pointer, str) or not pointer for pointer in pointers):
        raise NativeArtifactError("native_pointers must contain non-empty strings")
    return pointers


def _validate_sha256(value: object) -> str:
    """Accept only canonical lowercase SHA-256 text from untrusted descriptors."""
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise NativeArtifactError("sha256 must be 64 lowercase hexadecimal characters")
    return value


def _validate_size(value: object) -> int:
    """Reject booleans and negative counts before trusting descriptor size data."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise NativeArtifactError("size_bytes must be a non-negative integer")
    return value


__all__ = [
    "NativeArtifactConflictError",
    "NativeArtifactDescriptor",
    "NativeArtifactError",
    "NativeArtifactIntegrityError",
    "NativeArtifactNotFoundError",
    "NativeArtifactStore",
    "NativePointerError",
    "ReloadedNativeArtifact",
]
