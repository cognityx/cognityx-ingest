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
        self.control = control or registry._control

    def plan_blobs(
        self,
        execution: ExecutionContext,
        *,
        older_than: timedelta = timedelta(days=7),
    ) -> BlobGcPlan:
        self._authorize(execution, "storage.blob.gc.plan")
        refs = self.registry.list_referenced_blob_refs()
        return self.storage_runtime.blob_gc("source_asset").plan(
            referenced_blob_refs=refs, older_than=older_than
        )

    def execute_blobs(
        self,
        execution: ExecutionContext,
        plan: BlobGcPlan,
    ) -> BlobGcResult:
        self._authorize(execution, "storage.blob.gc.execute")
        with self.registry.catalog_write_lock():
            refs = self.registry.list_referenced_blob_refs()
            return self.storage_runtime.blob_gc("source_asset").execute(
                plan, referenced_blob_refs=refs
            )

    def _authorize(self, execution: ExecutionContext, action: str) -> None:
        decision = self.control.authorize(execution, action, resource={"role": "source_asset"})
        if not decision.allowed:
            from cognityx_ingest.control import IngestAuthorizationError

            raise IngestAuthorizationError(
                decision.reason or f"Control policy rejected {action}."
            )
