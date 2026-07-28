from __future__ import annotations

from pathlib import Path

import pytest

from cognityx_ingest import (
    DocBundleDeletionResult,
    ExecutionContext,
    SourceAssetDeletionResult,
    SourceAssetRegistry,
)
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
