from __future__ import annotations

from datetime import timedelta
import os
from pathlib import Path
import time

import pytest

from cognityx_ingest import (
    DocBundleDeletionResult,
    ExecutionContext,
    SourceAssetDeletionResult,
    SourceAssetRegistry,
)
from cognityx_ingest.cleanup import SourceAssetCleanupService
from cognityx_ingest.control import ControlDecision
from cognityx_resource import ResourceContext
from cognityx_storage import StorageConfig, StorageRuntime


def _setup(tmp_path: Path) -> tuple[SourceAssetRegistry, ExecutionContext, Path]:
    root = tmp_path / "storage"
    runtime = StorageRuntime.from_config(StorageConfig.built_in(root=root))
    catalog = tmp_path / "catalog.sqlite3"
    registry = SourceAssetRegistry(runtime, catalog)
    execution = ExecutionContext.create(
        ResourceContext(tenant_id="tenant-a", principal_id="alice")
    )
    return registry, execution, root


def test_asset_soft_delete_is_auditable_and_blob_remains(tmp_path: Path) -> None:
    registry, execution, root = _setup(tmp_path)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"paper")
    created = registry.register_asset(execution, source, bundle="research")
    asset = registry.show_asset(execution, created.asset_id)

    deleted = registry.delete_asset(execution, asset.asset_id, reason="obsolete")

    assert isinstance(deleted, SourceAssetDeletionResult)
    assert deleted.status == "deleted"
    assert deleted.blob_still_referenced is False
    assert registry.list_assets(execution) == ()
    assert registry.list_deleted_assets(execution)[0].delete_reason == "obsolete"
    with pytest.raises(KeyError):
        registry.show_asset(execution, asset.asset_id)
    with pytest.raises(KeyError):
        registry.open_asset(execution, asset.asset_id)
    repeated = registry.delete_asset(execution, asset.asset_id)
    assert repeated.status == "already_deleted"
    assert deleted.deleted_at == repeated.deleted_at


def test_shared_blob_survives_until_last_asset_deleted(tmp_path: Path) -> None:
    registry, execution, _ = _setup(tmp_path)
    first_path = tmp_path / "one.txt"
    second_path = tmp_path / "two.txt"
    first_path.write_bytes(b"shared")
    second_path.write_bytes(b"shared")
    first = registry.register_asset(execution, first_path, bundle="one")
    second = registry.register_asset(execution, second_path, bundle="two")
    assert first.asset_id != second.asset_id
    assert registry.show_asset(execution, first.asset_id).blob_id == registry.show_asset(
        execution, second.asset_id
    ).blob_id

    deleted = registry.delete_asset(execution, first.asset_id)
    assert deleted.blob_still_referenced is True
    with registry.open_asset(execution, second.asset_id) as opened:
        assert opened.read() == b"shared"


def test_deleted_asset_is_restored_with_same_id(tmp_path: Path) -> None:
    registry, execution, _ = _setup(tmp_path)
    source = tmp_path / "restore.txt"
    source.write_bytes(b"restore")
    created = registry.register_asset(execution, source, bundle="research")
    registry.delete_asset(execution, created.asset_id)

    restored = registry.register_asset(execution, source, bundle="research")

    assert restored.status == "restored"
    assert restored.asset_id == created.asset_id
    assert registry.show_asset(execution, created.asset_id).deleted_at is None


def test_recursive_bundle_delete_and_bundle_restore(tmp_path: Path) -> None:
    registry, execution, _ = _setup(tmp_path)
    source = tmp_path / "nested.txt"
    source.write_bytes(b"nested")
    bundle = registry.resolve_doc_bundle(execution, "research/child")
    registry.register_asset(execution, source, bundle="research/child")

    with pytest.raises(ValueError, match="not empty"):
        registry.delete_doc_bundle(execution, bundle.bundle_id)
    deleted = registry.delete_doc_bundle(
        execution, bundle.bundle_id, recursive=True, reason="cleanup"
    )
    assert isinstance(deleted, DocBundleDeletionResult)
    assert deleted.status == "deleted"
    assert len(registry.list_deleted_assets(execution)) == 1
    assert len(registry.list_deleted_doc_bundles(execution)) == 1

    restored = registry.resolve_doc_bundle(execution, "research/child", create=True)
    assert restored.bundle_id == bundle.bundle_id
    assert restored.deleted_at is None
    assert registry.resolve_doc_bundle(
        execution, "research/child", create=False
    ).bundle_id == bundle.bundle_id
    assert registry.list_assets(execution) == ()


def test_repeated_recursive_bundle_delete_has_zero_new_counts_and_repairs_event(
    tmp_path: Path,
) -> None:
    registry, execution, _ = _setup(tmp_path)
    source = tmp_path / "nested.txt"
    source.write_bytes(b"nested")
    bundle = registry.resolve_doc_bundle(execution, "parent/child")
    registry.register_asset(execution, source, bundle="parent/child")

    first = registry.delete_doc_bundle(execution, bundle.bundle_id, recursive=True)
    lifecycle_key = (
        f"source-contexts/{execution.context_id}/bundles/{bundle.bundle_id}/"
        f"lifecycle/{execution.run_id}-deleted.json"
    )
    registry._source_store.delete(lifecycle_key)

    second = registry.delete_doc_bundle(execution, bundle.bundle_id, recursive=True)

    assert first.status == "deleted"
    assert first.deleted_asset_count > 0
    assert first.deleted_bundle_count > 0
    assert second.status == "already_deleted"
    assert second.deleted_asset_count == 0
    assert second.deleted_bundle_count == 0
    assert registry._source_store.exists(lifecycle_key)


def test_deletion_and_cleanup_usage_metrics_are_actual(tmp_path: Path) -> None:
    class RecordingControl:
        def __init__(self) -> None:
            self.metrics: list[dict[str, int]] = []

        def authorize(self, context, action, resource=None, request=None):
            return ControlDecision(allowed=True)

        def report_usage(self, context, usage):
            self.metrics.append(dict(usage.metrics))

    root = tmp_path / "storage"
    runtime = StorageRuntime.from_config(StorageConfig.built_in(root=root))
    control = RecordingControl()
    registry = SourceAssetRegistry(
        runtime, tmp_path / "catalog.sqlite3", control=control
    )
    execution = ExecutionContext.create(
        ResourceContext(tenant_id="tenant-a", principal_id="alice")
    )
    source = tmp_path / "usage.txt"
    source.write_bytes(b"usage")
    created = registry.register_asset(execution, source, bundle="usage")

    registry.delete_asset(execution, created.asset_id)
    registry.delete_asset(execution, created.asset_id)
    bundle = registry.resolve_doc_bundle(execution, "empty")
    registry.delete_doc_bundle(execution, bundle.bundle_id)
    registry.delete_doc_bundle(execution, bundle.bundle_id)

    blob_path = root / registry._blob_ref_for_source(created.asset_id).storage_key
    old = time.time() - 10
    os.utime(blob_path, (old, old))
    cleanup = SourceAssetCleanupService(
        registry=registry, storage_runtime=runtime, control=control
    )
    plan = cleanup.plan_blobs(execution, older_than=timedelta(seconds=1))
    cleanup.execute_blobs(execution, plan, batch_size=1)

    assert {"assets_deleted": 1} in control.metrics
    assert {"assets_deleted": 0} in control.metrics
    assert {"assets_deleted": 0, "bundles_deleted": 1} in control.metrics
    assert {"assets_deleted": 0, "bundles_deleted": 0} in control.metrics
    assert {
        "objects_scanned": plan.objects_scanned,
        "candidates_planned": len(plan.deletion_candidates),
    } in control.metrics
    assert {
        "objects_deleted": 1,
        "objects_failed": 0,
        "bytes_reclaimed": len(b"usage"),
    } in control.metrics
