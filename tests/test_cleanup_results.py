from __future__ import annotations

from contextlib import contextmanager

from cognityx_ingest import ExecutionContext
from cognityx_ingest.cleanup import SourceAssetCleanupService
from cognityx_ingest.control import ControlDecision
from cognityx_resource import ResourceContext
from cognityx_storage import BlobGcCandidate, BlobGcPlan, BlobGcResult


class _Control:
    def authorize(self, context, action, resource=None, request=None):
        return ControlDecision(allowed=True)

    def report_usage(self, context, usage):
        return None


class _Registry:
    control = _Control()

    @contextmanager
    def catalog_write_lock(self):
        yield

    def list_referenced_blob_refs(self, *, include_deleted=False):
        return ()


class _Collector:
    def execute(self, plan, *, referenced_blob_refs=()):
        candidate = plan.deletion_candidates[0]
        reason = "now_referenced" if candidate.digest == "a" * 64 else "object_changed"
        return BlobGcResult(
            plan_id=plan.plan_id,
            deleted_objects=0,
            already_absent=0,
            skipped_objects=1,
            failed_objects=0,
            reclaimed_bytes=0,
            skips=({
                "profile": candidate.profile_name,
                "storage_key": candidate.storage_key,
                "reason": reason,
            },),
        )


class _Runtime:
    def blob_gc(self, role_name):
        return _Collector()


def test_cleanup_aggregates_structured_skips_across_batches() -> None:
    candidates = tuple(
        BlobGcCandidate(
            profile_name="local",
            storage_key=f"source-assets/blob-domains/test/sha256/{digest[:2]}/{digest[2:4]}/{digest}",
            uri=f"storage://local/{digest}",
            blob_id=f"blob-{digest[:8]}",
            size_bytes=1,
            last_modified=1.0,
            digest=digest,
        )
        for digest in ("a" * 64, "b" * 64)
    )
    plan = BlobGcPlan(
        plan_id="gc-test",
        created_at="2026-01-01T00:00:00+00:00",
        role_name="source_asset",
        grace_period_seconds=1.0,
        profiles_scanned=("local",),
        objects_scanned=2,
        referenced_blob_count=0,
        unreferenced_blob_count=2,
        protected_by_grace_period=0,
        deletion_candidates=candidates,
        reclaimable_bytes=2,
    )
    service = SourceAssetCleanupService(
        registry=_Registry(), storage_runtime=_Runtime(), control=_Control()
    )
    execution = ExecutionContext.create(ResourceContext(tenant_id="tenant-a"))

    result = service.execute_blobs(execution, plan, batch_size=1)

    assert [skip["reason"] for skip in result.skips] == [
        "now_referenced",
        "object_changed",
    ]
    assert result.skipped_objects == 2
