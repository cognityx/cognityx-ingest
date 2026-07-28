"""Ingest/Storage coordination for explicit SourceAsset Blob cleanup."""

from __future__ import annotations

from datetime import timedelta

from cognityx_ingest.control import ControlClient
from cognityx_ingest.models import ExecutionContext
from cognityx_ingest.source_assets import SourceAssetRegistry
from cognityx_storage import BlobGcPlan, BlobGcResult, StorageRuntime


class SourceAssetCleanupService:
    """Keep catalog reference enumeration separate from Storage deletion."""

    def __init__(
        self,
        *,
        registry: SourceAssetRegistry,
        storage_runtime: StorageRuntime,
        control: ControlClient | None = None,
    ) -> None:
        self.registry = registry
        self.storage_runtime = storage_runtime
        self.control = control or registry.control

    def plan_blobs(
        self,
        execution: ExecutionContext,
        *,
        older_than: timedelta = timedelta(days=7),
    ) -> BlobGcPlan:
        self._authorize(execution, "storage.blob.gc.plan")
        refs = self.registry.list_referenced_blob_refs(include_deleted=False)
        historical = self.registry.list_referenced_blob_refs(include_deleted=True)
        return self.storage_runtime.blob_gc("source_asset").plan(
            referenced_blob_refs=refs, profile_hint_blob_refs=historical,
            older_than=older_than
        )

    def execute_blobs(
        self,
        execution: ExecutionContext,
        plan: BlobGcPlan,
        *,
        batch_size: int = 100,
    ) -> BlobGcResult:
        if not 50 <= batch_size <= 500:
            raise ValueError("batch_size must be between 50 and 500")
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
        return BlobGcResult(plan.plan_id, sum(r.deleted_objects for r in results),
            sum(r.already_absent for r in results), sum(r.skipped_objects for r in results),
            sum(r.failed_objects for r in results), sum(r.reclaimed_bytes for r in results),
            tuple(f for r in results for f in r.failures))

    def _authorize(self, execution: ExecutionContext, action: str) -> None:
        decision = self.control.authorize(execution, action, resource={"role": "source_asset"})
        if not decision.allowed:
            from cognityx_ingest.control import IngestAuthorizationError

            raise IngestAuthorizationError(
                decision.reason or f"Control policy rejected {action}."
            )
