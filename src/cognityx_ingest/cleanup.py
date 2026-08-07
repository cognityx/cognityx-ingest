"""Coordinate deletion policy while leaving physical removal to Storage.

Purpose
-------
This module separates two kinds of cleanup. ``SourceAssetCleanupService`` keeps
the existing source-blob garbage-collection flow. ``ExtractionRetentionService``
adds T07 policy for parser-native artifacts: exact reuse, active references,
retention expiry, legal hold, advisory purge plans, and post-deletion tombstones.

Design and flow
---------------
Ingest owns metadata decisions and Storage owns bytes. Exact extraction identity
selects a validated record, the registry atomically acquires a reference, and
``NativeArtifactStore.reload`` verifies retained bytes before reuse succeeds.
Purge planning reads metadata only. After an external Storage-owned process has
removed a payload, finalization rechecks live policy and ``StorageClient.exists``
before recording ``purged``. Canonical content, selectors, native bindings,
segmentation views, observations, and immutable native descriptors are untouched.

Consumers and boundaries
------------------------
Application composition, retention operators, and future T10 controls use these
services. Parsers, routing, T08 source graphs, DataForge generation, SDK, CLI, and
physical artifact deletion remain outside this module. Instances hold immutable
collaborator references; SQLite transactions provide metadata race safety while
the configured Storage backend defines its own thread-safety guarantees.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
import hashlib
import json
import warnings

from cognityx_ingest.canonical_content import CanonicalContentArtifact
from cognityx_ingest.control import ControlClient
from cognityx_ingest.models import (
    ExecutionContext,
    ExtractionIdentity,
    ExtractionPurgeCandidate,
    ExtractionPurgeFinalizationError,
    ExtractionPurgePlan,
    ExtractionRetentionConflictError,
    ExtractionRetentionRecord,
    ExtractionRetentionState,
    ExtractionReuseIntegrityError,
    ExtractionReuseResult,
    UsageReport,
)
from cognityx_ingest.native_artifacts import (
    NativeArtifactDescriptor,
    NativeArtifactError,
    NativeArtifactStore,
)
from cognityx_ingest.segmentation_views import (
    SegmentationViewService,
    SegmentationViewSet,
)
from cognityx_ingest.source_assets import SourceAssetRegistry
from cognityx_storage import (
    BlobGcPlan,
    BlobGcResult,
    StorageClient,
    StorageRuntime,
)


class SourceAssetCleanupService:
    """Keep existing source-blob reference policy separate from Storage deletion.

    Source lifecycle composition constructs this service and operators use it to
    plan and execute Storage's content-addressed blob GC. The algorithm enumerates
    live catalog references, delegates grace periods and deletion to
    ``StorageRuntime.blob_gc``, and rechecks references under a catalog lock for
    each bounded batch. It does not govern T07 parser-native retention. Repeated
    planning is read-only; execution is intentionally side-effecting and Storage
    reports idempotent already-absent results. Authorization failures remain
    typed by the control client. Each call owns local state, while registry and
    Storage collaborators provide concurrency safety.
    """

    def __init__(
        self,
        *,
        registry: SourceAssetRegistry,
        storage_runtime: StorageRuntime,
        control: ControlClient | None = None,
    ) -> None:
        """Bind source cleanup to its registry, Storage runtime, and control seam.

        Application composition calls this pure constructor. It performs no I/O,
        adds no mutable cleanup state, and preserves the historical default of
        using the registry's control client. Invalid collaborators fail naturally
        when an operation uses their documented protocol.
        """
        self.registry = registry
        self.storage_runtime = storage_runtime
        self.control = control or registry.control

    def plan_blobs(
        self,
        execution: ExecutionContext,
        *,
        older_than: timedelta = timedelta(days=7),
    ) -> BlobGcPlan:
        """Build a grace-period-aware source-blob plan without deleting objects.

        Operators call this authorized, metadata-first operation. It gathers live
        and historical BlobRefs, delegates candidate calculation to Storage, and
        reports bounded usage metrics. The catalog and payloads are not mutated;
        repeated calls may differ only as time or external state changes.
        """
        self._authorize(execution, "storage.blob.gc.plan")
        refs = self.registry.list_referenced_blob_refs(include_deleted=False)
        historical = self.registry.list_referenced_blob_refs(include_deleted=True)
        plan = self.storage_runtime.blob_gc("source_asset").plan(
            referenced_blob_refs=refs, profile_hint_blob_refs=historical,
            older_than=older_than
        )
        self._report(
            execution,
            {
                "objects_scanned": plan.objects_scanned,
                "candidates_planned": len(plan.deletion_candidates),
            },
        )
        return plan

    def execute_blobs(
        self,
        execution: ExecutionContext,
        plan: BlobGcPlan,
        *,
        batch_size: int = 100,
    ) -> BlobGcResult:
        """Execute a Storage-owned source-blob plan in reference-safe batches.

        Operators call this explicit destructive boundary for SourceAsset CAS
        blobs, never T07 native artifacts. Each batch reacquires the catalog write
        lock, reloads live references, delegates deletion to Storage, and combines
        immutable results. Invalid batch bounds raise ``ValueError`` before any
        deletion; backend failures are represented by Storage's result contract.
        """
        if not 1 <= batch_size <= 500:
            raise ValueError("batch_size must be between 1 and 500")
        self._authorize(execution, "storage.blob.gc.execute")
        results = []
        candidates = plan.deletion_candidates
        for start in range(0, len(candidates), batch_size):
            batch = BlobGcPlan(plan.plan_id, plan.created_at, plan.role_name,
                plan.grace_period_seconds, plan.profiles_scanned,
                plan.objects_scanned, plan.referenced_blob_count,
                plan.unreferenced_blob_count, plan.protected_by_grace_period,
                tuple(candidates[start:start + batch_size]),
                sum(c.size_bytes for c in candidates[start:start + batch_size]),
                plan.skipped_objects, plan.warnings)
            with self.registry.catalog_write_lock():
                refs = self.registry.list_referenced_blob_refs(include_deleted=False)
                results.append(self.storage_runtime.blob_gc("source_asset").execute(batch, referenced_blob_refs=refs))
        result = BlobGcResult(plan_id=plan.plan_id,
            deleted_objects=sum(r.deleted_objects for r in results),
            already_absent=sum(r.already_absent for r in results),
            skipped_objects=sum(r.skipped_objects for r in results),
            failed_objects=sum(r.failed_objects for r in results),
            reclaimed_bytes=sum(r.reclaimed_bytes for r in results),
            failures=tuple(f for r in results for f in r.failures),
            skips=tuple(s for r in results for s in r.skips))
        self._report(execution, {"objects_deleted": result.deleted_objects,
            "objects_failed": result.failed_objects, "bytes_reclaimed": result.reclaimed_bytes})
        return result

    def _report(self, execution: ExecutionContext, metrics: dict[str, int]) -> None:
        """Report completed cleanup usage without undoing successful Storage work."""
        try:
            self.control.report_usage(execution, UsageReport(run_id=execution.run_id, metrics=metrics))
        except Exception as exc:
            warnings.warn(
                f"Usage reporting failed after a completed operation: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )

    def _authorize(self, execution: ExecutionContext, action: str) -> None:
        """Require the existing owner-scoped source-blob cleanup permission."""
        decision = self.control.authorize(execution, action, resource={"role": "source_asset"})
        if not decision.allowed:
            from cognityx_ingest.control import IngestAuthorizationError

            raise IngestAuthorizationError(
                decision.reason or f"Control policy rejected {action}."
            )


class ExtractionRetentionService:
    """Coordinate exact extraction reuse and metadata-only retention policy.

    Application composition constructs this service from the existing catalog,
    one context-bound T01 native store, and its matching Storage client. Retention
    operators and complete-identity callers use it. Registration and reuse verify
    descriptors and payloads through ``NativeArtifactStore``; policy mutations
    delegate to transactional registry APIs; purge planning is advisory; and
    finalization records a tombstone only after Storage reports payload absence.
    The service never selects or executes a parser, calls a provider/LLM/network,
    copies payload bytes into metadata, deletes Storage objects, or modifies
    canonical/T05/T06 records. Calls are idempotent where the registry contract is
    idempotent; typed T07 errors bound integrity and policy failures. Immutable
    collaborator references are safe to share when their implementations are.
    """

    def __init__(
        self,
        *,
        registry: SourceAssetRegistry,
        native_artifacts: NativeArtifactStore,
        artifact_storage: StorageClient,
    ) -> None:
        """Bind T07 policy to one catalog and matching native artifact scope.

        The composition root constructs this without I/O. The caller is trusted to
        provide the same scoped ``StorageClient`` used by ``native_artifacts``;
        registration and finalization prove descriptor URI/key agreement before
        accepting that boundary. No optional parser or deletion collaborator is
        accepted, keeping prohibited side effects structurally unavailable.
        """
        self._registry = registry
        self._native_artifacts = native_artifacts
        self._artifact_storage = artifact_storage

    def register_extraction(
        self,
        execution: ExecutionContext,
        identity: ExtractionIdentity,
        descriptor: NativeArtifactDescriptor,
        *,
        reference_ids: Iterable[str] = (),
    ) -> ExtractionRetentionRecord:
        """Register one already stored and integrity-verified native extraction.

        Trusted post-parser composition calls this only when all six identity
        fields are explicit. The algorithm reloads exact T01 bytes, checks artifact,
        parser, version, hash, key, URI, and media facts, builds a validated record,
        and delegates atomic idempotent persistence to the registry. It does not
        execute a parser or write/copy payloads. Conflicting metadata raises typed
        retention errors, and failed integrity checks leave no catalog record.
        """
        if not isinstance(identity, ExtractionIdentity):
            raise ExtractionRetentionConflictError(
                "complete ExtractionIdentity is required for registration"
            )
        verified = self._verified_descriptor(descriptor)
        if (
            verified.parser_id != identity.parser_id
            or verified.parser_version != identity.parser_version
        ):
            raise ExtractionRetentionConflictError(
                "native descriptor parser identity does not match extraction identity"
            )
        now = _now()
        record = ExtractionRetentionRecord(
            context_id=execution.context_id,
            artifact_id=verified.artifact_id,
            identity=identity,
            extraction_identity=identity.digest,
            artifact_sha256=verified.sha256,
            artifact_storage_key=verified.storage_key,
            artifact_media_type=verified.media_type,
            state=ExtractionRetentionState.VALIDATED,
            reference_ids=_ordered_reference_ids(reference_ids),
            legal_hold=False,
            created_at=now,
            updated_at=now,
            updated_by=execution.principal_id,
            updated_run_id=execution.run_id,
        )
        return self._registry.register_extraction_record(execution, record)

    def acquire_reusable(
        self,
        execution: ExecutionContext,
        identity: ExtractionIdentity,
        reference_id: str,
    ) -> ExtractionReuseResult:
        """Acquire and verify only one exact validated retained extraction.

        Complete-identity composition calls this before deciding whether parsing
        is needed. The registry atomically adds the consumer reference before this
        service reloads payload bytes through T01. Exact misses, expired records,
        and tombstones return a deterministic non-reuse result. Integrity mismatch
        removes only the reference inserted by this call and raises
        ``ExtractionReuseIntegrityError``. No parser, network, provider, LLM, or
        Storage write/delete occurs; a successful reference remains until its
        owner explicitly removes it.
        """
        record, acquisition_token = self._registry.acquire_reusable_extraction(
            execution, identity, reference_id
        )
        if record is None:
            return ExtractionReuseResult(
                reused=False,
                reference_id=reference_id,
                record=None,
                reason="no exact validated extraction",
            )
        try:
            descriptor = self._native_artifacts.reload(
                record.artifact_id
            ).descriptor
            self._assert_record_descriptor(record, descriptor)
        except (NativeArtifactError, ExtractionReuseIntegrityError) as error:
            self._registry._release_failed_reuse_acquisition(
                execution,
                record.artifact_id,
                reference_id,
                acquisition_token,
            )
            if isinstance(error, ExtractionReuseIntegrityError):
                raise
            raise ExtractionReuseIntegrityError(
                "retained native artifact failed integrity verification"
            ) from error
        return ExtractionReuseResult(
            reused=True,
            reference_id=reference_id,
            record=self._registry.get_extraction_record(
                execution, record.artifact_id
            ),
            reason=None,
        )

    def add_reference(
        self, execution: ExecutionContext, artifact_id: str, reference_id: str
    ) -> ExtractionRetentionRecord:
        """Add one explicit payload-requiring consumer reference idempotently.

        NativeBinding, verified-view, and downstream lifecycle composition call
        this after establishing a real payload dependency. The registry validates
        context and identity, serializes the insert, deduplicates retries, and
        returns current immutable state. No payload, parser, network, provider,
        LLM, or deletion operation occurs; typed registry failures propagate and
        thread safety comes from the immediate SQLite transaction.
        """
        return self._registry.add_extraction_reference(
            execution, artifact_id, reference_id
        )

    def remove_reference(
        self, execution: ExecutionContext, artifact_id: str, reference_id: str
    ) -> ExtractionRetentionRecord:
        """Remove only a caller-named reference as an explicit lifecycle step.

        The consumer that owns a stable reference calls this when it no longer
        requires native bytes. The registry removes that exact deduplicated row
        under a context-scoped transaction and returns current immutable state;
        absent retries are safe. It never infers removal from expiry or compact
        lineage and performs no payload I/O or deletion. Typed registry failures
        propagate, and competing policy writers serialize through SQLite.
        """
        return self._registry.remove_extraction_reference(
            execution, artifact_id, reference_id
        )

    def set_legal_hold(
        self, execution: ExecutionContext, artifact_id: str, *, enabled: bool
    ) -> ExtractionRetentionRecord:
        """Enable or release independent durable legal hold idempotently.

        Authorized governance composition calls this with an explicit boolean.
        The registry rejects purged artifacts, changes audit metadata only when
        the value differs, and returns a frozen record whose purge eligibility is
        derived immediately. Hold does not change reuse state or payload bytes.
        Typed policy failures propagate; one immediate transaction provides race
        safety and repeated equal requests have no additional side effect.
        """
        return self._registry.set_extraction_legal_hold(
            execution, artifact_id, enabled=enabled
        )

    def mark_retention_expired(
        self, execution: ExecutionContext, artifact_id: str
    ) -> ExtractionRetentionRecord:
        """Advance validated metadata to retention-expired without deleting bytes.

        Retention policy orchestration calls this one-way state operation. The
        registry changes `validated` once, accepts an already-expired retry, and
        rejects resurrection from `purged` under an immediate transaction. Active
        references and legal hold remain untouched and continue to protect purge.
        No Storage, parser, network, provider, or LLM call occurs; typed policy
        failures propagate and the returned record is immutable.
        """
        return self._registry.mark_extraction_retention_expired(
            execution, artifact_id
        )

    def plan_purge(self, execution: ExecutionContext) -> ExtractionPurgePlan:
        """Build a deterministic advisory partition without physical deletion.

        Operators call this after policy updates. Current records are projected by
        the registry, sorted into eligible and protected tuples, and hashed into a
        stable plan ID with no payload reads or writes. The snapshot has no future
        authority: ``finalize_purge`` always rechecks live metadata and Storage.
        """
        candidates = self._registry.list_extraction_purge_candidates(execution)
        eligible = tuple(item for item in candidates if item.eligible)
        protected = tuple(item for item in candidates if not item.eligible)
        plan_material = {
            "context_id": execution.context_id,
            "eligible": [_candidate_dict(item) for item in eligible],
            "protected": [_candidate_dict(item) for item in protected],
        }
        plan_id = "purge-" + hashlib.sha256(
            json.dumps(
                plan_material,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return ExtractionPurgePlan(
            plan_id=plan_id,
            context_id=execution.context_id,
            created_at=_now(),
            eligible=eligible,
            protected=protected,
        )

    def finalize_purge(
        self,
        execution: ExecutionContext,
        artifact_id: str,
        deletion_reason: str,
    ) -> ExtractionRetentionRecord:
        """Record purged only after descriptor survival and payload absence.

        Storage coordination calls this after an external owner removed the payload.
        The method reads the immutable descriptor, proves it still agrees with
        catalog facts, then asks the registry to reacquire its write lock, recheck
        expiry/references/hold, call read-only ``StorageClient.exists``, and write
        the compact tombstone. Stale plans fail safely. This method never invokes
        delete, unlink, rmtree, parser, provider, LLM, or network operations.
        """
        record = self._registry.get_extraction_record(execution, artifact_id)
        try:
            descriptor = self._native_artifacts.read(artifact_id)
            self._assert_record_descriptor(record, descriptor)
        except (NativeArtifactError, ExtractionReuseIntegrityError) as error:
            raise ExtractionPurgeFinalizationError(
                "native artifact descriptor failed finalization verification"
            ) from error
        return self._registry.finalize_extraction_purge(
            execution,
            artifact_id,
            deletion_reason,
            payload_exists=self._artifact_storage.exists,
        )

    def _verified_descriptor(
        self, descriptor: NativeArtifactDescriptor
    ) -> NativeArtifactDescriptor:
        """Reload T01 bytes and require caller metadata to equal stored metadata."""
        if not isinstance(descriptor, NativeArtifactDescriptor):
            raise ExtractionRetentionConflictError(
                "descriptor must be a NativeArtifactDescriptor"
            )
        try:
            verified = self._native_artifacts.reload(descriptor.artifact_id).descriptor
        except NativeArtifactError as error:
            raise ExtractionReuseIntegrityError(
                "native artifact failed registration integrity verification"
            ) from error
        if verified != descriptor or self._artifact_storage.uri(
            descriptor.storage_key
        ) != descriptor.uri:
            raise ExtractionRetentionConflictError(
                "native artifact descriptor does not match retained Storage metadata"
            )
        return verified

    @staticmethod
    def _assert_record_descriptor(
        record: ExtractionRetentionRecord,
        descriptor: NativeArtifactDescriptor,
    ) -> None:
        """Compare every immutable T07/T01 fact without reading payload content."""
        if (
            descriptor.artifact_id != record.artifact_id
            or descriptor.parser_id != record.identity.parser_id
            or descriptor.parser_version != record.identity.parser_version
            or descriptor.sha256 != record.artifact_sha256
            or descriptor.storage_key != record.artifact_storage_key
            or descriptor.media_type != record.artifact_media_type
        ):
            raise ExtractionReuseIntegrityError(
                "retention metadata disagrees with native artifact descriptor"
            )


def collect_reference_ids(
    artifact_id: str,
    *,
    canonical_content: CanonicalContentArtifact | None = None,
    native_descriptors: Mapping[str, NativeArtifactDescriptor] | None = None,
    segmentation_view_sets: Iterable[
        tuple[SegmentationViewService, SegmentationViewSet]
    ] = (),
    consumer_reference_ids: Iterable[str] = (),
) -> tuple[str, ...]:
    """Collect explicit payload users from trusted canonical and T06 records.

    Post-parser composition calls this before registration or explicit reference
    reconciliation. Canonical content is validated with authoritative T01
    descriptors before matching ``NativeBinding.artifact_id``. Every supplied T06
    pair is verified through its bound service's ``validate_view_set`` method, not
    merely value-level validation, before parser-native views with a matching
    ``native_artifact_id`` contribute their stable ``view_id``. Explicit external
    consumer IDs are included, then results are deduplicated and sorted.

    The helper never interprets T05 observations as payload references, removes a
    reference, reconstructs source text, mutates records, performs I/O, or calls a
    parser/network/provider/LLM. Typed canonical/T06/T07 validation errors cross
    the trust boundary unchanged. Pure local state makes repeated and concurrent
    calls deterministic.
    """
    if not isinstance(artifact_id, str) or not artifact_id:
        raise ExtractionRetentionConflictError("artifact_id must be nonempty")
    references = set(_ordered_reference_ids(consumer_reference_ids))
    if canonical_content is not None:
        canonical_content.validate(native_descriptors=native_descriptors)
        references.update(
            binding.binding_id
            for binding in canonical_content.native_bindings
            if binding.artifact_id == artifact_id
        )
    for service, view_set in segmentation_view_sets:
        verified = service.validate_view_set(view_set)
        references.update(
            view.view_id
            for view in verified.views
            if view.strategy == "parser-native-structure"
            and view.profile is not None
            and view.profile.get("native_artifact_id") == artifact_id
        )
    result = tuple(sorted(set(references)))
    if any(not isinstance(item, str) or not item for item in result):
        raise ExtractionRetentionConflictError(
            "reference IDs must be nonempty strings"
        )
    return result


def _candidate_dict(candidate: ExtractionPurgeCandidate) -> dict[str, object]:
    """Project one validated purge candidate into deterministic plan hash data."""
    return {
        "artifact_id": candidate.artifact_id,
        "extraction_identity": candidate.extraction_identity,
        "artifact_storage_key": candidate.artifact_storage_key,
        "artifact_sha256": candidate.artifact_sha256,
        "eligible": candidate.eligible,
        "reason": candidate.reason,
    }


def _ordered_reference_ids(values: Iterable[str]) -> tuple[str, ...]:
    """Validate caller reference iterables before deterministic deduplication.

    Registration and reference collection share this pure trust-boundary helper.
    It materializes an iterable once, rejects non-string or empty identities with
    a typed T07 conflict, and returns a sorted unique immutable tuple. It performs
    no I/O or mutation and is deterministic for equal input values.
    """
    materialized = tuple(values)
    if any(not isinstance(item, str) or not item for item in materialized):
        raise ExtractionRetentionConflictError(
            "reference IDs must be nonempty strings"
        )
    return tuple(sorted(set(materialized)))


def _now() -> str:
    """Return one sortable UTC audit timestamp for new retention metadata."""
    return datetime.now(UTC).isoformat()
