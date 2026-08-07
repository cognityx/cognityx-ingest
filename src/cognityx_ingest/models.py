"""Define stable ingestion records independent of parser and model providers.

Purpose
-------
Keep durable Ingest values explicit, immutable, additive, and understandable to
callers even while parser implementations and retention policy evolve.

This module exists to keep the established v2 document, evidence, lifecycle, and
result contracts readable while parser implementations evolve. It also defines
T07's small extraction identity and retention records so policy code can describe
retained parser output without storing the output itself. Its core approach is
immutable typed records with explicit validation and dictionary projections.

Compatibility is the governing design principle: v3.2's generalized source model
lives separately in ``canonical_content`` and is exposed here only through an
additive result key. The T07 records are also additive and do not alter normal
ingest results. Application composition constructs them, the SourceAsset catalog
persists their metadata, and retention/audit code consumes them. Storage still
owns payload bytes and physical deletion. This module performs no parsing,
persistence, network, provider, LLM, SDK, CLI, Source Graph, or DataForge work.
Ingest services, CLI adapters, DataForge integrations, and existing Python callers
continue to use the established records below unchanged.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
import hashlib
import json
import math
import re
from typing import Any, Mapping

from cognityx_resource import ExecutionContext, ResourceRef

CANONICAL_SCHEMA = "cognityx.ingest.document"

EXTRACTION_IDENTITY_FIELDS = (
    "source_sha256",
    "parser_id",
    "parser_version",
    "parser_configuration_hash",
    "model_version",
    "scope",
)
EXTRACTION_RETENTION_STATES = (
    "validated",
    "retention-expired",
    "purged",
)
EXTRACTION_RETENTION_EVENT_TYPES = (
    "registered",
    "reference-added",
    "reference-removed",
    "legal-hold-enabled",
    "legal-hold-released",
    "retention-expired",
    "purged",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_CAMEL_CASE_BOUNDARY_RE = re.compile(
    r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])"
)
_NON_WORD_RE = re.compile(r"[^a-z0-9]+")
_FORBIDDEN_CONFIGURATION_KEY_PHRASES = {
    ("access", "token"),
    ("api", "key"),
    ("bearer", "token"),
    ("client", "secret"),
    ("correlation", "id"),
    ("execution", "id"),
    ("private", "key"),
    ("request", "id"),
    ("run", "id"),
    ("trace", "id"),
}
_FORBIDDEN_CONFIGURATION_KEY_WORDS = {
    "authorization",
    "accesstoken",
    "apikey",
    "bearertoken",
    "clientsecret",
    "correlationid",
    "credential",
    "credentials",
    "executionid",
    "invocationid",
    "jobid",
    "operationid",
    "password",
    "privatekey",
    "requestid",
    "runid",
    "secret",
    "traceid",
    "token",
}
_LOCAL_PATH_KEY_WORDS = {
    "cache",
    "dir",
    "directory",
    "file",
    "local",
    "output",
    "path",
    "temp",
    "temporary",
}
_MAX_CONFIGURATION_DEPTH = 32
_MAX_CONFIGURATION_ITEMS = 4_096
_MAX_CONFIGURATION_COLLECTION = 1_024
_MAX_CONFIGURATION_STRING = 4_096


class ExtractionRetentionError(Exception):
    """Base typed failure for T07 extraction-retention operations.

    Identity constructors, the durable registry, and the retention service raise
    this family so API and audit callers never receive raw JSON, SQLite, or
    Storage implementation errors. Messages contain bounded IDs and policy
    reasons only, never source text, payload bytes, credentials, SQL, or backend
    paths. Instances are transient, side-effect free, and safe to pass between
    threads like ordinary immutable exception diagnostics.
    """


class ExtractionIdentityError(ExtractionRetentionError, ValueError):
    """Reject incomplete, unsafe, or nondeterministic extraction identity input.

    ``ExtractionIdentity`` construction and configuration hashing use this type at
    their untrusted-data boundary. Failure occurs before catalog or Storage I/O and
    never echoes configuration values that might contain sensitive information.
    """


class ExtractionRetentionConflictError(ExtractionRetentionError):
    """Report an attempted rewrite or invalid retention state transition.

    Registry writers raise this when an immutable artifact/identity disagrees or a
    caller attempts resurrection or out-of-order transition. The failed operation
    is rolled back atomically and exposes no payload or SQL details.
    """


class ExtractionRetentionReferenceError(ExtractionRetentionError):
    """Report a missing artifact or invalid explicit active-reference operation.

    Context-scoped registry lookups and reference lifecycle calls use this bounded
    type. It distinguishes absent metadata from policy conflicts without leaking
    another context's records or any retained payload data.
    """


class ExtractionReuseError(ExtractionRetentionError):
    """Base typed failure for exact reuse acquisition.

    ``ExtractionRetentionService`` uses this family after metadata acquisition.
    Ordinary exact misses return an immutable result instead; malformed identity
    or unsafe integrity behavior fails explicitly and performs no parser call.
    """


class ExtractionReuseIntegrityError(ExtractionReuseError):
    """Report that retained bytes or descriptor identity cannot support reuse.

    The retention service raises this after ``NativeArtifactStore.reload`` or an
    exact descriptor comparison fails. It releases only a reference inserted by
    the failed acquisition and never repairs or rewrites immutable evidence.
    """


class ExtractionPurgeBlockedError(ExtractionRetentionError):
    """Report current metadata that blocks purge planning or finalization.

    Active references, legal hold, unexpired state, or an already-purged record
    produce this policy failure. Callers receive the bounded decision reason while
    canonical content, descriptors, and payloads remain untouched.
    """


class ExtractionPurgeFinalizationError(ExtractionRetentionError):
    """Report an unsafe post-deletion finalization attempt.

    The service raises this when the Storage payload still exists, Storage cannot
    prove absence, or final transaction checks fail. It never performs physical
    deletion and leaves the durable retention record unchanged.
    """


class ExtractionRetentionState(StrEnum):
    """Define the one-way lifecycle of independently retained parser payloads.

    Registry writers construct this value and policy records consume it. The only
    production transition is ``validated`` to ``retention-expired`` to ``purged``;
    no value represents resurrection. Enum values are stable persisted strings,
    immutable, side-effect free, and safe for concurrent readers.
    """

    VALIDATED = "validated"
    RETENTION_EXPIRED = "retention-expired"
    PURGED = "purged"


class ExtractionRetentionEventType(StrEnum):
    """Name each durable T07 lifecycle fact in one closed vocabulary.

    The retention registry writes these values to its append-only event table and
    audit callers consume them in sequence order. The enum prevents producers from
    inventing near-duplicate labels while keeping persisted values stable. It is
    immutable, performs no I/O, and has no parser, payload, or deletion authority.
    """

    REGISTERED = "registered"
    REFERENCE_ADDED = "reference-added"
    REFERENCE_REMOVED = "reference-removed"
    LEGAL_HOLD_ENABLED = "legal-hold-enabled"
    LEGAL_HOLD_RELEASED = "legal-hold-released"
    RETENTION_EXPIRED = "retention-expired"
    PURGED = "purged"


@dataclass(frozen=True, slots=True)
class ExtractionIdentity:
    """Identify one reusable parser execution by exactly six frozen components.

    Application composition constructs this only when it knows the exact source,
    parser/version, complete execution-affecting configuration, model version, and
    scope. Reuse lookup and retention records consume its digest. The algorithm
    validates bounded payload-free values and hashes compact sorted-key UTF-8 JSON
    over ``EXTRACTION_IDENTITY_FIELDS``. It performs no I/O, parsing, persistence,
    network, provider, or LLM call. Equal values are deterministic and thread-safe;
    incomplete or unsafe input raises ``ExtractionIdentityError`` before reuse.
    """

    source_sha256: str
    parser_id: str
    parser_version: str
    parser_configuration_hash: str
    model_version: str
    scope: str

    def __post_init__(self) -> None:
        """Enforce complete portable identity on every construction path.

        Direct callers and the configuration helper converge here. SHA values must
        be lowercase, IDs and versions are bounded, and scope cannot be an absolute
        or traversal-like local path. Validation is pure and idempotent; typed
        failure occurs before any retention lookup or side effect.
        """
        _require_sha256(self.source_sha256, "source_sha256")
        _require_identifier(self.parser_id, "parser_id")
        _require_text(self.parser_version, "parser_version")
        _require_sha256(
            self.parser_configuration_hash, "parser_configuration_hash"
        )
        _require_text(self.model_version, "model_version")
        _require_scope(self.scope)

    @classmethod
    def from_configuration(
        cls,
        *,
        source_sha256: str,
        parser_id: str,
        parser_version: str,
        parser_configuration: Mapping[str, object],
        model_version: str,
        scope: str,
    ) -> "ExtractionIdentity":
        """Hash explicit JSON-safe execution configuration into the frozen formula.

        Trusted application composition calls this after collecting all
        execution-affecting parser, adapter, and pipeline settings. The recursive
        algorithm rejects credential, run, and local-path keys; validates finite
        JSON scalars and string mapping keys; then hashes canonical compact JSON.
        Mapping insertion order cannot affect the result. The helper stores no
        configuration, performs no I/O or external call, and raises
        ``ExtractionIdentityError`` without echoing sensitive values.
        """
        normalized = _identity_configuration(parser_configuration)
        configuration_hash = hashlib.sha256(
            _canonical_identity_json(normalized)
        ).hexdigest()
        return cls(
            source_sha256=source_sha256,
            parser_id=parser_id,
            parser_version=parser_version,
            parser_configuration_hash=configuration_hash,
            model_version=model_version,
            scope=scope,
        )

    def to_dict(self) -> dict[str, str]:
        """Return the exact six fields in their frozen identity vocabulary.

        Registry persistence, tests, and audit tooling consume this fresh mapping.
        It contains no parser configuration values or mutable nested state, performs
        no I/O, and is deterministic across threads and repeated calls.
        """
        return {field: getattr(self, field) for field in EXTRACTION_IDENTITY_FIELDS}

    @property
    def digest(self) -> str:
        """Return SHA-256 of canonical JSON over exactly the six identity fields.

        Reuse indexes and retention records call this pure property. No cached or
        writable digest exists, so changing any component requires another frozen
        identity and necessarily changes the canonical hash input.
        """
        return hashlib.sha256(_canonical_identity_json(self.to_dict())).hexdigest()


@dataclass(frozen=True, slots=True)
class RetentionTombstone:
    """Preserve compact parser lineage after Storage removes a native payload.

    Purge finalization constructs this record and audit/T08 consumers read it. It
    retains parser/version, source hash, artifact hash fact, and deletion reason,
    but no bytes, text, paths, credentials, or mutable metadata. Frozen fixture
    artifact-hash markers remain representable while production registration is
    independently verified against a strict T01 descriptor. Construction is pure,
    thread-safe, and raises ``ExtractionIdentityError`` for malformed values.
    """

    parser_id: str
    parser_version: str
    source_sha256: str
    artifact_sha256: str
    deletion_reason: str

    def __post_init__(self) -> None:
        """Validate compact lineage without requiring deleted payload access."""
        _require_identifier(self.parser_id, "parser_id")
        _require_text(self.parser_version, "parser_version")
        _require_sha256(self.source_sha256, "source_sha256")
        _require_text(self.artifact_sha256, "artifact_sha256")
        _require_text(self.deletion_reason, "deletion_reason")

    def to_dict(self) -> dict[str, str]:
        """Serialize exact tombstone facts without adding operational metadata."""
        return {
            "parser_id": self.parser_id,
            "parser_version": self.parser_version,
            "source_sha256": self.source_sha256,
            "artifact_sha256": self.artifact_sha256,
            "deletion_reason": self.deletion_reason,
        }


@dataclass(frozen=True, slots=True)
class ExtractionRetentionRecord:
    """Describe one context-scoped retained extraction without owning its bytes.

    The retention service constructs this from a complete identity and verified
    T01 descriptor; ``SourceAssetRegistry`` persists and reloads it. Policy callers
    derive reuse and purge decisions from immutable state, references, hold, and
    tombstone rather than writable booleans. The record preserves exact artifact
    key/hash/media facts for Storage coordination but contains no payload or source
    text. Validation is pure and typed; tuples/frozen children support concurrent
    readers while registry transactions own mutation and race safety.
    """

    context_id: str
    artifact_id: str
    identity: ExtractionIdentity
    extraction_identity: str
    artifact_sha256: str
    artifact_storage_key: str
    artifact_media_type: str
    state: ExtractionRetentionState
    reference_ids: tuple[str, ...]
    legal_hold: bool
    created_at: str
    updated_at: str
    updated_by: str | None
    updated_run_id: str
    tombstone: RetentionTombstone | None = None

    def __post_init__(self) -> None:
        """Reject mutable, inconsistent, or resurrected retention representations."""
        _require_identifier(self.context_id, "context_id")
        _require_identifier(self.artifact_id, "artifact_id")
        if not isinstance(self.identity, ExtractionIdentity):
            raise ExtractionIdentityError("identity must be ExtractionIdentity")
        if self.extraction_identity != self.identity.digest:
            raise ExtractionIdentityError(
                "extraction_identity does not match its exact components"
            )
        _require_sha256(self.artifact_sha256, "artifact_sha256")
        _require_storage_key(self.artifact_storage_key)
        _require_text(self.artifact_media_type, "artifact_media_type")
        if not isinstance(self.state, ExtractionRetentionState):
            raise ExtractionRetentionConflictError(
                "state must be an ExtractionRetentionState"
            )
        _require_reference_ids(self.reference_ids)
        if not isinstance(self.legal_hold, bool):
            raise ExtractionRetentionConflictError("legal_hold must be boolean")
        for name, value in (
            ("created_at", self.created_at),
            ("updated_at", self.updated_at),
            ("updated_run_id", self.updated_run_id),
        ):
            _require_text(value, name)
        if self.updated_by is not None:
            _require_text(self.updated_by, "updated_by")
        if self.tombstone is not None and not isinstance(
            self.tombstone, RetentionTombstone
        ):
            raise ExtractionRetentionConflictError(
                "tombstone must be a RetentionTombstone"
            )
        if self.state is ExtractionRetentionState.PURGED:
            if self.tombstone is None or self.reference_ids or self.legal_hold:
                raise ExtractionRetentionConflictError(
                    "purged records require a tombstone and no hold or references"
                )
            if (
                self.tombstone.parser_id != self.identity.parser_id
                or self.tombstone.parser_version != self.identity.parser_version
                or self.tombstone.source_sha256 != self.identity.source_sha256
                or self.tombstone.artifact_sha256 != self.artifact_sha256
            ):
                raise ExtractionRetentionConflictError(
                    "purged tombstone must match immutable extraction metadata"
                )
        elif self.tombstone is not None:
            raise ExtractionRetentionConflictError(
                "only purged records may contain a tombstone"
            )

    @property
    def reusable(self) -> bool:
        """Derive exact-reuse eligibility without treating legal hold as a block."""
        return (
            self.state is ExtractionRetentionState.VALIDATED
            and self.tombstone is None
        )

    @property
    def purge_reason(self) -> str | None:
        """Apply the frozen purge-decision precedence and return its reason."""
        if self.state is ExtractionRetentionState.PURGED:
            return "already purged"
        if self.legal_hold:
            return "legal hold blocks purge"
        if self.reference_ids:
            return "active references remain"
        if self.state is not ExtractionRetentionState.RETENTION_EXPIRED:
            return "retention has not expired"
        return None

    @property
    def purge_eligible(self) -> bool:
        """Derive purge eligibility from current state rather than persisted input."""
        return self.purge_reason is None


@dataclass(frozen=True, slots=True)
class ExtractionPayloadAbsenceProof:
    """Bind one T01-verified missing payload to exact retention metadata.

    ``ExtractionRetentionService`` constructs this only after reading a surviving
    descriptor, observing payload absence through ``NativeArtifactStore.reload``,
    and reading the same descriptor again. The registry consumes the proof inside
    its final transaction and compares every field with the live record. This
    bounded immutable value replaces an arbitrary callback, so the catalog cannot
    accidentally query a different Storage backend or execute caller code while
    holding its write lock. It contains no bytes, secrets, physical paths, or
    deletion capability and is safe for concurrent readers.
    """

    artifact_id: str
    extraction_identity: str
    artifact_sha256: str
    artifact_storage_key: str

    def __post_init__(self) -> None:
        """Validate exact logical identity before a proof reaches persistence."""
        _require_identifier(self.artifact_id, "artifact_id")
        _require_sha256(self.extraction_identity, "extraction_identity")
        _require_sha256(self.artifact_sha256, "artifact_sha256")
        _require_storage_key(self.artifact_storage_key)


@dataclass(frozen=True, slots=True)
class ExtractionRetentionEvent:
    """Expose one append-only context-scoped extraction lifecycle fact.

    Registry mutations create these records in the same SQLite transaction as the
    state change; audit, operations, and future API composition list them by the
    database-assigned sequence. Reference events identify the exact consumer and
    purge events retain only a bounded reason. The record is immutable and
    payload-free, and validation rejects ambiguous event shapes before callers can
    treat malformed catalog data as history.
    """

    sequence: int
    context_id: str
    artifact_id: str
    event_type: ExtractionRetentionEventType
    timestamp: str
    principal_id: str | None
    run_id: str
    reference_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        """Require a positive sequence and event-specific reference/reason shape."""
        if type(self.sequence) is not int or self.sequence < 1:
            raise ExtractionRetentionError("retention event sequence must be positive")
        _require_identifier(self.context_id, "context_id")
        _require_identifier(self.artifact_id, "artifact_id")
        if not isinstance(self.event_type, ExtractionRetentionEventType):
            raise ExtractionRetentionError(
                "retention event type must use the frozen vocabulary"
            )
        _require_text(self.timestamp, "timestamp")
        if self.principal_id is not None:
            _require_text(self.principal_id, "principal_id")
        _require_text(self.run_id, "run_id")
        reference_event = self.event_type in {
            ExtractionRetentionEventType.REFERENCE_ADDED,
            ExtractionRetentionEventType.REFERENCE_REMOVED,
        }
        if reference_event:
            _require_identifier(self.reference_id, "reference_id")
        elif self.reference_id is not None:
            raise ExtractionRetentionError(
                "only reference events may contain a reference_id"
            )
        if self.reason is not None:
            _require_text(self.reason, "reason")
        if self.event_type is ExtractionRetentionEventType.PURGED and self.reason is None:
            raise ExtractionRetentionError("purged events require a reason")


@dataclass(frozen=True, slots=True)
class ExtractionReuseResult:
    """Return an exact reusable record or a bounded deterministic miss reason.

    ``ExtractionRetentionService.acquire_reusable`` constructs this after atomic
    reference acquisition and payload integrity verification. Callers use it to
    decide whether parsing must proceed. It never contains payload bytes and does
    not claim reuse for incomplete, expired, purged, or nonexact identities.
    """

    reused: bool
    reference_id: str
    record: ExtractionRetentionRecord | None
    reason: str | None

    def __post_init__(self) -> None:
        """Keep successful and missed reuse outcomes internally unambiguous."""
        _require_identifier(self.reference_id, "reference_id")
        if self.record is not None and not isinstance(
            self.record, ExtractionRetentionRecord
        ):
            raise ExtractionReuseError(
                "reuse result record must be an ExtractionRetentionRecord"
            )
        if self.reused != (self.record is not None):
            raise ExtractionReuseError("reuse result record and status disagree")
        if self.reused and self.reason is not None:
            raise ExtractionReuseError("successful reuse cannot contain a miss reason")
        if not self.reused:
            _require_text(self.reason, "reason")


@dataclass(frozen=True, slots=True)
class ExtractionPurgeCandidate:
    """Capture one current metadata-only purge decision for Storage handoff.

    Purge planning constructs candidates from validated retention records;
    operators and Storage coordination consume IDs, logical keys, and hashes. The
    candidate is advisory, immutable, and payload-free. ``eligible`` and ``reason``
    must mirror the record's derived decision, but finalization always rechecks the
    live catalog rather than trusting this snapshot.
    """

    artifact_id: str
    extraction_identity: str
    artifact_storage_key: str
    artifact_sha256: str
    eligible: bool
    reason: str | None

    def __post_init__(self) -> None:
        """Reject forged or internally inconsistent advisory purge metadata.

        Registry projection and direct callers converge here before a candidate
        enters a plan. The pure validation checks stable IDs, exact hashes, logical
        Storage key, boolean eligibility, and the required reason polarity. It
        performs no policy lookup or I/O and raises typed T07 validation errors.
        """
        _require_identifier(self.artifact_id, "artifact_id")
        _require_sha256(self.extraction_identity, "extraction_identity")
        _require_storage_key(self.artifact_storage_key)
        _require_sha256(self.artifact_sha256, "artifact_sha256")
        if not isinstance(self.eligible, bool):
            raise ExtractionPurgeBlockedError("candidate eligibility must be boolean")
        if self.eligible and self.reason is not None:
            raise ExtractionPurgeBlockedError(
                "eligible candidate cannot contain a blocking reason"
            )
        if not self.eligible:
            _require_text(self.reason, "reason")

    @classmethod
    def from_record(cls, record: ExtractionRetentionRecord) -> "ExtractionPurgeCandidate":
        """Project one immutable record into a compact advisory decision.

        Registry planning calls this pure constructor after loading current
        metadata. It copies only logical identity/hash/key facts and derives both
        eligibility and reason from the record, so callers cannot persist a second
        writable policy truth. Invalid record input raises a typed T07 conflict.
        """
        if not isinstance(record, ExtractionRetentionRecord):
            raise ExtractionRetentionConflictError(
                "purge candidate source must be an ExtractionRetentionRecord"
            )
        return cls(
            artifact_id=record.artifact_id,
            extraction_identity=record.extraction_identity,
            artifact_storage_key=record.artifact_storage_key,
            artifact_sha256=record.artifact_sha256,
            eligible=record.purge_eligible,
            reason=record.purge_reason,
        )


@dataclass(frozen=True, slots=True)
class ExtractionPurgePlan:
    """Separate eligible and protected extraction metadata without deleting it.

    ``ExtractionRetentionService.plan_purge`` constructs this deterministic
    context-scoped snapshot for operators or a Storage-owned deletion boundary.
    It contains logical keys and hashes only, performs no deletion, and has no
    authority during finalization. Frozen tuples make concurrent readers safe.
    """

    plan_id: str
    context_id: str
    created_at: str
    eligible: tuple[ExtractionPurgeCandidate, ...]
    protected: tuple[ExtractionPurgeCandidate, ...]

    def __post_init__(self) -> None:
        """Require canonical candidate partition and stable bounded plan identity."""
        _require_identifier(self.plan_id, "plan_id")
        _require_identifier(self.context_id, "context_id")
        _require_text(self.created_at, "created_at")
        if not isinstance(self.eligible, tuple) or not isinstance(
            self.protected, tuple
        ):
            raise ExtractionRetentionConflictError(
                "purge candidate partitions must be immutable tuples"
            )
        candidates = (*self.eligible, *self.protected)
        if any(not isinstance(item, ExtractionPurgeCandidate) for item in candidates):
            raise ExtractionRetentionConflictError(
                "purge plans may contain only ExtractionPurgeCandidate records"
            )
        if any(not item.eligible for item in self.eligible):
            raise ExtractionPurgeBlockedError("eligible plan entries must be eligible")
        if any(item.eligible for item in self.protected):
            raise ExtractionPurgeBlockedError("protected plan entries must be blocked")
        if tuple(item.artifact_id for item in self.eligible) != tuple(
            sorted(item.artifact_id for item in self.eligible)
        ) or tuple(item.artifact_id for item in self.protected) != tuple(
            sorted(item.artifact_id for item in self.protected)
        ):
            raise ExtractionRetentionConflictError(
                "purge candidate partitions must use stable artifact order"
            )
        ids = tuple(item.artifact_id for item in candidates)
        if len(ids) != len(set(ids)):
            raise ExtractionRetentionConflictError("purge plan repeats an artifact")


def _canonical_identity_json(value: object) -> bytes:
    """Encode deterministic compact JSON and reject non-finite or unsupported data."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ExtractionIdentityError(
            "parser configuration must contain finite JSON-safe values"
        ) from error


def _identity_configuration(value: Mapping[str, object]) -> dict[str, object]:
    """Validate a bounded configuration tree and exclude operational secrets.

    ``ExtractionIdentity.from_configuration`` calls this untrusted-data boundary.
    The recursive algorithm copies finite JSON values, counts every node, bounds
    depth/collection/string sizes, and rejects secret or run-local keys before
    canonical hashing. It retains no caller mapping, performs no I/O, and raises
    ``ExtractionIdentityError`` instead of recursion or serialization failures.
    """
    if not isinstance(value, Mapping):
        raise ExtractionIdentityError("parser_configuration must be a mapping")
    item_count = 0

    def normalize(item: object, path: tuple[str, ...]) -> object:
        """Return a fresh JSON-safe tree without logging values at the trust boundary."""
        nonlocal item_count
        item_count += 1
        if item_count > _MAX_CONFIGURATION_ITEMS:
            raise ExtractionIdentityError(
                "parser configuration contains too many values"
            )
        if len(path) > _MAX_CONFIGURATION_DEPTH:
            raise ExtractionIdentityError("parser configuration is nested too deeply")
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, str):
            if len(item) > _MAX_CONFIGURATION_STRING:
                raise ExtractionIdentityError(
                    "parser configuration text is too large"
                )
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ExtractionIdentityError(
                    "parser configuration numbers must be finite"
                )
            return item
        if isinstance(item, Mapping):
            if len(item) > _MAX_CONFIGURATION_COLLECTION:
                raise ExtractionIdentityError(
                    "parser configuration mapping is too large"
                )
            result: dict[str, object] = {}
            for key, nested in item.items():
                if not isinstance(key, str) or not key or len(key) > 128:
                    raise ExtractionIdentityError(
                        "parser configuration keys must be bounded strings"
                    )
                key_words = _configuration_key_words(key)
                if _configuration_key_is_forbidden(key_words) or (
                    isinstance(nested, str)
                    and any(word in _LOCAL_PATH_KEY_WORDS for word in key_words)
                    and _looks_like_local_path(nested)
                ):
                    raise ExtractionIdentityError(
                        "parser configuration contains forbidden operational data"
                    )
                result[key] = normalize(nested, (*path, key))
            return result
        if isinstance(item, (list, tuple)):
            if len(item) > _MAX_CONFIGURATION_COLLECTION:
                raise ExtractionIdentityError(
                    "parser configuration sequence is too large"
                )
            return [normalize(nested, path) for nested in item]
        raise ExtractionIdentityError(
            "parser configuration must contain only JSON-safe values"
        )

    normalized = normalize(value, ())
    assert isinstance(normalized, dict)
    return normalized


def _configuration_key_words(key: str) -> tuple[str, ...]:
    """Normalize camel, snake, kebab, and spaced keys into semantic words.

    The configuration trust boundary calls this before hashing. Splitting names
    such as ``apiKey``, ``api_key``, and ``api-key`` identically prevents casing
    or punctuation from bypassing secret checks without rejecting harmless
    substrings such as ``tokenizer``. The helper is pure and value-free.
    """
    split_camel = _CAMEL_CASE_BOUNDARY_RE.sub(" ", key).casefold()
    return tuple(word for word in _NON_WORD_RE.split(split_camel) if word)


def _configuration_key_is_forbidden(words: tuple[str, ...]) -> bool:
    """Reject secret and run-local identity keys after semantic normalization.

    ``_identity_configuration`` uses the closed phrases and exact secret words;
    exact-word comparison deliberately allows technical names like ``tokenizer``
    and ``model_id`` while rejecting credentials and per-run identifiers. The
    decision is deterministic and never inspects or reports a secret value.
    """
    contains_phrase = any(
        words[index : index + len(phrase)] == phrase
        for phrase in _FORBIDDEN_CONFIGURATION_KEY_PHRASES
        for index in range(len(words) - len(phrase) + 1)
    )
    return contains_phrase or any(
        word in _FORBIDDEN_CONFIGURATION_KEY_WORDS for word in words
    )


def _looks_like_local_path(value: str) -> bool:
    """Recognize filesystem-shaped values without rejecting model identifiers.

    Path-labelled configuration values are checked with this helper. Absolute,
    home-relative, dot-relative, traversal, file-URI, Windows-drive, and backslash
    forms are local operational state. A portable registry identifier such as
    ``Qwen/Qwen3-8B`` is intentionally not path-shaped. The check is pure and does
    not resolve or access the supplied value.
    """
    folded = value.casefold()
    return (
        value.startswith(("/", "\\", "~/", "./", "../"))
        or "\\" in value
        or folded.startswith("file://")
        or folded.startswith(
            ("tmp/", "temp/", "var/tmp/", "home/", "users/", ".cache/", "cache/")
        )
        or (len(value) >= 2 and value[0].isalpha() and value[1] == ":")
        or any(part in {".", ".."} for part in value.split("/"))
    )


def _require_sha256(value: object, field_name: str) -> str:
    """Require lowercase SHA-256 without echoing malformed values."""
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ExtractionIdentityError(f"{field_name} must be lowercase SHA-256")
    return value


def _require_identifier(value: object, field_name: str) -> str:
    """Require one bounded portable identifier without local path semantics."""
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ExtractionIdentityError(f"{field_name} must be a bounded identifier")
    return value


def _require_text(value: object, field_name: str) -> str:
    """Require bounded nonempty text while keeping diagnostics value-free."""
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ExtractionIdentityError(f"{field_name} must be bounded nonempty text")
    return value


def _require_scope(value: object) -> str:
    """Require a portable logical scope rather than an absolute or traversal path."""
    scope = _require_text(value, "scope")
    if (
        scope.startswith(("/", "\\"))
        or "\\" in scope
        or any(part in {".", ".."} for part in scope.split("/"))
        or (len(scope) >= 2 and scope[1] == ":")
    ):
        raise ExtractionIdentityError("scope must be logical, not a local path")
    return scope


def _require_storage_key(value: object) -> str:
    """Require a relative logical Storage key without filesystem traversal."""
    key = _require_text(value, "artifact_storage_key")
    if key.startswith(("/", "\\")) or "\\" in key or any(
        part in {"", ".", ".."} for part in key.split("/")
    ):
        raise ExtractionIdentityError(
            "artifact_storage_key must be a relative logical key"
        )
    return key


def _require_reference_ids(values: object) -> tuple[str, ...]:
    """Require immutable, sorted, unique active-reference identities."""
    if not isinstance(values, tuple):
        raise ExtractionRetentionReferenceError(
            "reference_ids must be an immutable tuple"
        )
    validated = tuple(_require_identifier(value, "reference_id") for value in values)
    if validated != tuple(sorted(set(validated))):
        raise ExtractionRetentionReferenceError(
            "reference_ids must be sorted and unique"
        )
    return validated


class IngestJobState(StrEnum):
    """Ingest-owned lifecycle states, independent of a jobs backend."""

    SUBMITTED = "submitted"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class SourceAssetContext:
    """Durable governance context for SourceAsset resources."""

    context_id: str
    context_type: str
    descriptors: dict[str, str]
    created_at: str


@dataclass(frozen=True, slots=True)
class DocBundle:
    """Logical collection of SourceAssets within one Context."""

    bundle_id: str
    context_id: str
    name: str
    parent_bundle_id: str | None
    path: str
    created_by: str | None
    created_at: str
    updated_at: str
    deleted_at: str | None = None
    deleted_by: str | None = None
    delete_run_id: str | None = None
    delete_reason: str | None = None

    @property
    def ref(self) -> ResourceRef:
        """Return the cross-service reference to this DocBundle."""
        return ResourceRef(
            resource_type="doc_bundle",
            resource_id=self.bundle_id,
            context_id=self.context_id,
        )


@dataclass(frozen=True, slots=True)
class SourceAsset:
    """One registered external digital object backed by immutable bytes."""

    source_id: str
    context_id: str
    bundle_id: str
    original_filename: str
    media_type: str
    size_bytes: int
    sha256: str
    blob_id: str
    created_by: str | None
    created_at: str
    deleted_at: str | None = None
    deleted_by: str | None = None
    delete_run_id: str | None = None
    delete_reason: str | None = None

    @property
    def asset_id(self) -> str:
        """Return the canonical API name for the stable ``src-...`` ID."""
        return self.source_id

    @property
    def ref(self) -> ResourceRef:
        """Return the cross-service reference to this SourceAsset."""
        return ResourceRef(
            resource_type="source_asset",
            resource_id=self.asset_id,
            context_id=self.context_id,
        )


@dataclass(frozen=True, slots=True)
class SourceAssetRegistrationResult:
    """Outcome of SourceAsset registration without exposing caller paths."""

    context_id: str
    bundle_id: str
    source_id: str
    sha256: str
    size_bytes: int
    status: str

    @property
    def asset_id(self) -> str:
        """Return the canonical API name for the stable ``src-...`` ID."""
        return self.source_id


@dataclass(frozen=True, slots=True)
class SourceAssetBatchItem:
    """Safe per-entry outcome from directory SourceAsset registration."""

    relative_path: str
    bundle_path: str
    asset_id: str | None
    status: str
    error_category: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class SourceAssetBatchResult:
    """Aggregate outcome of one synchronous directory registration."""

    batch_id: str
    context_id: str
    root_bundle_id: str
    root_bundle_path: str
    structure: str
    recursive: bool
    files_discovered: int
    files_processed: int
    created_count: int
    restored_count: int
    already_registered_count: int
    failed_count: int
    skipped_count: int
    items: tuple[SourceAssetBatchItem, ...]


@dataclass(frozen=True, slots=True)
class SourceAssetDeletionResult:
    context_id: str
    bundle_id: str
    asset_id: str
    blob_id: str
    deleted_at: str
    status: str
    blob_still_referenced: bool


@dataclass(frozen=True, slots=True)
class DocBundleDeletionResult:
    context_id: str
    bundle_id: str
    deleted_asset_count: int
    deleted_bundle_count: int
    deleted_at: str
    status: str


@dataclass(frozen=True, slots=True)
class SourceAssetLocation:
    """Read-only physical-location diagnostics for one SourceAsset."""

    source_id: str
    blob_id: str
    blob_uri: str
    backend: str
    local_path: str | None
    profile_name: str | None = None

    @property
    def asset_id(self) -> str:
        """Return the canonical API name for the stable ``src-...`` ID."""
        return self.source_id


# Compatibility aliases retain one implementation and stable constructor fields.
SourceContext = SourceAssetContext
SourceBundle = DocBundle
RegisteredSource = SourceAsset
SourceRegistrationResult = SourceAssetRegistrationResult
SourceLocation = SourceAssetLocation


@dataclass(frozen=True, slots=True)
class UsageReport:
    """Facts measured by the ingest execution rather than policy assertions."""

    run_id: str
    job_id: str | None = None
    documents: int = 0
    pages: int | None = None
    input_bytes: int = 0
    output_bytes: int = 0
    duration_ms: int = 0
    service: str = "cognityx-ingest"
    metrics: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """Stable artifact identity and logical storage location."""

    artifact_id: str
    uri: str
    media_type: str


@dataclass(frozen=True, slots=True)
class SourceAnchor:
    """Stable address for an observed region of the original document."""

    anchor_id: str
    document_id: str
    page_index: int
    block_id: str | None = None
    char_start: int | None = None
    char_end: int | None = None


@dataclass(frozen=True, slots=True)
class PageRecord:
    page_id: str
    physical_page_index: int
    sequence_number: int
    pdf_page_label: str | None = None
    printed_page_label: str | None = None
    width: float | None = None
    height: float | None = None
    block_ids: tuple[str, ...] = ()
    source_backends: tuple[str, ...] = ()
    fact_sources: Mapping[str, tuple[Mapping[str, Any], ...]] = field(
        default_factory=dict, hash=False
    )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["block_ids"] = list(self.block_ids)
        value["source_backends"] = list(self.source_backends)
        value["fact_sources"] = {
            key: list(sources) for key, sources in self.fact_sources.items()
        }
        return value


@dataclass(frozen=True, slots=True)
class Block:
    block_id: str
    page_id: str
    block_type: str
    reading_order: int
    text: str
    bbox: tuple[float, float, float, float] | None = None
    method: str = "parser"
    confidence: float | None = None
    source_backends: tuple[str, ...] = ()
    fact_sources: Mapping[str, tuple[Mapping[str, Any], ...]] = field(
        default_factory=dict, hash=False
    )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["bbox"] = list(self.bbox) if self.bbox is not None else None
        value["source_backends"] = list(self.source_backends)
        value["fact_sources"] = {
            key: list(sources) for key, sources in self.fact_sources.items()
        }
        return value


@dataclass(frozen=True, slots=True)
class RepeatedRegionOccurrence:
    page_id: str
    physical_page_index: int
    source_page_id: str
    source_block_id: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RepeatedRegion:
    region_id: str
    region_type: str
    normalized_text: str
    occurrences: tuple[RepeatedRegionOccurrence, ...]
    detection_method: str = "deterministic_repeated_margin"
    status: str = "deterministic"
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["occurrences"] = [item.to_dict() for item in self.occurrences]
        return value


@dataclass(frozen=True, slots=True)
class TableCell:
    column_index: int
    column_name: str
    text: str
    source_anchor_ids: tuple[str, ...] = ()
    parser_source_anchor_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["source_anchor_ids"] = list(self.source_anchor_ids)
        value["parser_source_anchor_ids"] = list(self.parser_source_anchor_ids)
        return value


@dataclass(frozen=True, slots=True)
class TableRow:
    row_number: int | None
    row_type: str
    cells: tuple[TableCell, ...] = ()
    text: str | None = None
    column_span: int = 1
    source_anchor_ids: tuple[str, ...] = ()
    parser_source_anchor_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["cells"] = [item.to_dict() for item in self.cells]
        value["source_anchor_ids"] = list(self.source_anchor_ids)
        value["parser_source_anchor_ids"] = list(self.parser_source_anchor_ids)
        return value


@dataclass(frozen=True, slots=True)
class TablePart:
    part_id: str
    page_id: str
    source_block_ids: tuple[str, ...]
    parser_source_anchor_ids: tuple[str, ...]
    row_start: int
    row_end: int
    repeated_header: bool
    merged_group_row: TableRow
    method: str = "deterministic_table_assembly"
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["source_block_ids"] = list(self.source_block_ids)
        value["parser_source_anchor_ids"] = list(self.parser_source_anchor_ids)
        value["merged_group_row"] = self.merged_group_row.to_dict()
        return value


@dataclass(frozen=True, slots=True)
class DocumentObject:
    object_id: str
    object_type: str
    page_id: str
    caption: str | None = None
    text: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    page_ids: tuple[str, ...] = ()
    owner_section_id: str | None = None
    source_anchor_ids: tuple[str, ...] = ()
    caption_anchor_id: str | None = None
    marker: str | None = None
    marker_anchor_id: str | None = None
    note_anchor_id: str | None = None
    image_anchor_id: str | None = None
    parser_source_anchor_ids: tuple[str, ...] = ()
    columns: tuple[str, ...] = ()
    rows: tuple[TableRow, ...] = ()
    parts: tuple[TablePart, ...] = ()
    source_backends: tuple[str, ...] = ()
    fact_sources: Mapping[str, tuple[Mapping[str, Any], ...]] = field(
        default_factory=dict, hash=False
    )
    method: str = "parser"
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["bbox"] = list(self.bbox) if self.bbox is not None else None
        value["page_ids"] = list(self.page_ids)
        value["source_anchor_ids"] = list(self.source_anchor_ids)
        value["parser_source_anchor_ids"] = list(self.parser_source_anchor_ids)
        value["columns"] = list(self.columns)
        value["rows"] = [item.to_dict() for item in self.rows]
        value["parts"] = [item.to_dict() for item in self.parts]
        value["source_backends"] = list(self.source_backends)
        value["fact_sources"] = {
            key: list(sources) for key, sources in self.fact_sources.items()
        }
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DocumentObject":
        selected = dict(value)
        selected["page_ids"] = tuple(value.get("page_ids", ()))
        selected["source_anchor_ids"] = tuple(value.get("source_anchor_ids", ()))
        selected["parser_source_anchor_ids"] = tuple(
            value.get("parser_source_anchor_ids", ())
        )
        selected["columns"] = tuple(value.get("columns", ()))
        selected["source_backends"] = tuple(value.get("source_backends", ()))
        selected["fact_sources"] = {
            key: tuple(sources)
            for key, sources in value.get("fact_sources", {}).items()
        }
        selected["rows"] = tuple(
            TableRow(
                **{
                    **dict(item),
                    "cells": tuple(
                        TableCell(
                            **{
                                **dict(cell),
                                "source_anchor_ids": tuple(
                                    cell.get("source_anchor_ids", ())
                                ),
                                "parser_source_anchor_ids": tuple(
                                    cell.get("parser_source_anchor_ids", ())
                                ),
                            }
                        )
                        for cell in item.get("cells", ())
                    ),
                    "source_anchor_ids": tuple(item.get("source_anchor_ids", ())),
                    "parser_source_anchor_ids": tuple(
                        item.get("parser_source_anchor_ids", ())
                    ),
                }
            )
            for item in value.get("rows", ())
        )
        selected["parts"] = tuple(
            TablePart(
                **{
                    **dict(item),
                    "source_block_ids": tuple(item.get("source_block_ids", ())),
                    "parser_source_anchor_ids": tuple(
                        item.get("parser_source_anchor_ids", ())
                    ),
                    "merged_group_row": TableRow(
                        **{
                            **dict(item["merged_group_row"]),
                            "cells": tuple(
                                TableCell(**dict(cell))
                                for cell in item["merged_group_row"].get("cells", ())
                            ),
                            "source_anchor_ids": tuple(
                                item["merged_group_row"].get("source_anchor_ids", ())
                            ),
                            "parser_source_anchor_ids": tuple(
                                item["merged_group_row"].get(
                                    "parser_source_anchor_ids", ()
                                )
                            ),
                        }
                    ),
                }
            )
            for item in value.get("parts", ())
        )
        return cls(**selected)


@dataclass(frozen=True, slots=True)
class Relation:
    relation_id: str
    source_anchor_id: str
    target_anchor_id: str | None
    relation_type: str
    status: str
    target_text: str | None = None
    method: str = "deterministic"
    confidence: float | None = None
    decision_id: str | None = None
    reason: str | None = None
    source_backends: tuple[str, ...] = ()
    fact_sources: Mapping[str, tuple[Mapping[str, Any], ...]] = field(
        default_factory=dict, hash=False
    )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["source_backends"] = list(self.source_backends)
        value["fact_sources"] = {
            key: list(sources) for key, sources in self.fact_sources.items()
        }
        return value


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    decision_id: str
    task_id: str
    status: str
    method: str
    considered_tools: tuple[str, ...] = ()
    invoked_tools: tuple[str, ...] = ()
    selected_tool: str | None = None
    selected_reason: str | None = None
    provider: str | None = None
    model: str | None = None
    backend: str | None = None
    profile: str | None = None
    server_profile: str | None = None
    request_id: str | None = None
    prompt_version: str | None = None
    configuration_hash: str | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    timings: Mapping[str, Any] = field(default_factory=dict)
    confidence: float | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["considered_tools"] = list(self.considered_tools)
        value["invoked_tools"] = list(self.invoked_tools)
        return value


@dataclass(frozen=True, slots=True)
class UnresolvedItem:
    task_id: str
    source_anchor_id: str
    relation_type: str
    target_text: str | None
    reason: str
    status: str = "unresolved"
    method: str = "deterministic"
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SourceRecord:
    source_id: str
    filename: str
    sha256: str
    size_bytes: int
    storage_key: str
    media_type: str = "application/pdf"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    document_id: str
    page_number: int
    text: str
    char_start: int
    char_end: int
    schema_version: str = "cognityx.ingest.evidence/v2"
    source_asset_id: str | None = None
    bundle_id: str | None = None
    context_id: str | None = None
    sequence_number: int | None = None
    source_sha256: str | None = None
    parser_name: str | None = None
    parser_version: str | None = None
    run_id: str | None = None
    physical_page_index: int | None = None
    pdf_page_label: str | None = None
    printed_page_label: str | None = None
    block_id: str | None = None
    anchor_id: str | None = None
    continues_from: str | None = None
    continues_to: str | None = None
    method: str = "observed"
    confidence: float | None = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Evidence":
        """Read both legacy v1 records and lineage-complete v2 records."""
        known = cls.__dataclass_fields__
        selected = {key: item for key, item in value.items() if key in known}
        selected.setdefault("schema_version", "cognityx.ingest.evidence/v1")
        return cls(**selected)


@dataclass(frozen=True, slots=True)
class Section:
    section_id: str
    title: str
    evidence_ids: tuple[str, ...]
    number: str | None = None
    level: int | None = None
    parent_section_id: str | None = None
    path: tuple[str, ...] = ()
    heading_block_id: str | None = None
    start_block_id: str | None = None
    end_block_id: str | None = None
    continuation_status: str | None = None
    continuation_method: str | None = None
    continuation_confidence: float | None = None
    page_ids: tuple[str, ...] = ()
    block_ids: tuple[str, ...] = ()
    continues_from: str | None = None
    continues_to: str | None = None
    method: str = "deterministic"
    confidence: float | None = 1.0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence_ids"] = list(self.evidence_ids)
        value["path"] = list(self.path)
        value["page_ids"] = list(self.page_ids)
        value["block_ids"] = list(self.block_ids)
        return value


@dataclass(frozen=True, slots=True)
class CanonicalDocument:
    document_id: str
    schema_version: str
    source: SourceRecord
    title: str
    sections: tuple[Section, ...]
    enhancement: dict[str, Any] | None = None
    schema: str = CANONICAL_SCHEMA
    aliases: tuple[str, ...] = ()
    pages: tuple[PageRecord, ...] = ()
    blocks: tuple[Block, ...] = ()
    repeated_regions: tuple[RepeatedRegion, ...] = ()
    objects: tuple[DocumentObject, ...] = ()
    relations: tuple[Relation, ...] = ()
    decisions: tuple[DecisionRecord, ...] = ()
    unresolved: tuple[UnresolvedItem, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "document_id": self.document_id,
            "schema_version": self.schema_version,
            "source": self.source.to_dict(),
            "title": self.title,
            "sections": [section.to_dict() for section in self.sections],
            "enhancement": self.enhancement,
            "aliases": list(self.aliases),
            "pages": [page.to_dict() for page in self.pages],
            "blocks": [block.to_dict() for block in self.blocks],
            "repeated_regions": [item.to_dict() for item in self.repeated_regions],
            "objects": [item.to_dict() for item in self.objects],
            "relations": [relation.to_dict() for relation in self.relations],
            "decisions": [decision.to_dict() for decision in self.decisions],
            "unresolved": [item.to_dict() for item in self.unresolved],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CanonicalDocument":
        """Read v1 documents while accepting the richer v2 fields."""
        source = SourceRecord(**dict(value["source"]))
        sections = tuple(
            Section(
                **{
                    **dict(item),
                    "evidence_ids": tuple(item.get("evidence_ids", ())),
                    "path": tuple(item.get("path", ())),
                    "page_ids": tuple(item.get("page_ids", ())),
                    "block_ids": tuple(item.get("block_ids", ())),
                }
            )
            for item in value.get("sections", ())
        )
        return cls(
            document_id=str(value["document_id"]),
            schema_version=str(value.get("schema_version", "cognityx.ingest.document/v1")),
            source=source,
            title=str(value.get("title", source.filename)),
            sections=sections,
            enhancement=value.get("enhancement"),
            schema=str(value.get("schema", CANONICAL_SCHEMA)),
            aliases=tuple(value.get("aliases", ())),
            pages=tuple(
                PageRecord(
                    **{
                        **dict(item),
                        "block_ids": tuple(item.get("block_ids", ())),
                        "source_backends": tuple(item.get("source_backends", ())),
                        "fact_sources": {
                            key: tuple(sources)
                            for key, sources in item.get("fact_sources", {}).items()
                        },
                    }
                )
                for item in value.get("pages", ())
            ),
            blocks=tuple(
                Block(
                    **{
                        **dict(item),
                        "bbox": (
                            tuple(item["bbox"]) if item.get("bbox") is not None else None
                        ),
                        "source_backends": tuple(item.get("source_backends", ())),
                        "fact_sources": {
                            key: tuple(sources)
                            for key, sources in item.get("fact_sources", {}).items()
                        },
                    }
                )
                for item in value.get("blocks", ())
            ),
            repeated_regions=tuple(
                RepeatedRegion(
                    **{
                        **dict(item),
                        "occurrences": tuple(
                            RepeatedRegionOccurrence(**dict(occurrence))
                            for occurrence in item.get("occurrences", ())
                        ),
                    }
                )
                for item in value.get("repeated_regions", ())
            ),
            objects=tuple(
                DocumentObject.from_dict(item) for item in value.get("objects", ())
            ),
            relations=tuple(
                Relation(
                    **{
                        **dict(item),
                        "source_backends": tuple(item.get("source_backends", ())),
                        "fact_sources": {
                            key: tuple(sources)
                            for key, sources in item.get("fact_sources", {}).items()
                        },
                    }
                )
                for item in value.get("relations", ())
            ),
            decisions=tuple(DecisionRecord(**dict(item)) for item in value.get("decisions", ())),
            unresolved=tuple(UnresolvedItem(**dict(item)) for item in value.get("unresolved", ())),
        )


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Return one completed document's compatibility and additive artifact handles.

    Responsibility:
        Keep the established document, evidence, manifest, provenance, parser, and
        usage results while exposing T02 canonical content plus T05 parser
        observations and fusion decisions additively.
    Constructed by:
        ``IngestService`` after all immutable document artifacts persist.
    Used by:
        Existing Python callers, run aggregation, usage accounting, and future
        readers that opt into the v3.2 artifact.
    Invariants:
        Existing positional constructor fields and artifact identities remain
        unchanged; every newly added field has a backward-compatible default.
    Lifecycle/persistence:
        This frozen in-memory result references Storage objects but stores no
        payload bytes of its own.
    Thread-safety assumptions:
        Frozen scalar and tuple fields are safe for concurrent reads; referenced
        clients and storage backends retain their own concurrency contracts.
    """

    document: CanonicalDocument
    evidence: tuple[Evidence, ...]
    manifest_key: str
    document_key: str
    evidence_key: str
    run_id: str = ""
    job_id: str | None = None
    artifacts: tuple[ArtifactRef, ...] = ()
    usage: UsageReport | None = None
    provenance_key: str = ""
    raw_parser_key: str | None = None
    raw_parser_keys: tuple[str, ...] = ()
    canonical_content_key: str = ""
    observation_artifact_key: str = ""
    fusion_artifact_key: str = ""


@dataclass(frozen=True, slots=True)
class IngestRunResult:
    """Aggregate outcome for one file, folder, asset, or bundle submission."""

    run_id: str
    job_id: str | None
    root_bundle_id: str | None
    results: tuple[IngestResult, ...]
    failures: tuple[dict[str, Any], ...]
    run_manifest_key: str
    run_manifest_uri: str

    @property
    def document_count(self) -> int:
        return len(self.results)

    @property
    def failed_count(self) -> int:
        return len(self.failures)

    def __len__(self) -> int:
        return len(self.results)

    def __iter__(self):
        return iter(self.results)

    def __getitem__(self, index: int) -> IngestResult:
        return self.results[index]
