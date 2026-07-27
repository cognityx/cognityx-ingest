from __future__ import annotations

from pathlib import Path

import pytest

from cognityx_ingest import (
    SourceAssetCatalogAmbiguityError,
    SourceAssetRegistry,
)
from cognityx_storage import StorageConfig, StorageRuntime


def _runtime(root: Path) -> StorageRuntime:
    return StorageRuntime.from_config(StorageConfig.built_in(root=root))


def test_load_uses_catalog_role_and_reports_selection(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    registry = SourceAssetRegistry.load(runtime=runtime)

    assert registry.catalog_path == tmp_path / "catalog/ingest/source_catalog.sqlite3"
    assert registry.catalog_info() == {
        "catalog_path": str(registry.catalog_path),
        "selection": "catalog_role",
        "catalog_profile": "local-main",
        "catalog_backend": "LocalStorageBackend",
    }
    assert registry.catalog_path.exists()


def test_explicit_and_environment_paths_take_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path / "storage")
    environment_path = tmp_path / "environment.sqlite3"
    explicit_path = tmp_path / "explicit.sqlite3"
    monkeypatch.setenv("COGNITYX_INGEST_CATALOG", str(environment_path))

    from_environment = SourceAssetRegistry.load(runtime=runtime)
    from_explicit = SourceAssetRegistry.load(
        runtime=runtime, catalog_path=explicit_path
    )

    assert from_environment.catalog_path == environment_path
    assert from_environment.catalog_info()["selection"] == "environment"
    assert from_explicit.catalog_path == explicit_path
    assert from_explicit.catalog_info()["selection"] == "explicit"


def test_legacy_catalog_is_reused_without_relocation(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    legacy = tmp_path / ".cognityx-ingest/source_catalog.sqlite3"
    legacy.parent.mkdir(parents=True)
    legacy.touch()

    registry = SourceAssetRegistry.load(runtime=runtime)

    assert registry.catalog_path == legacy
    assert registry.catalog_info()["selection"] == "legacy"


def test_legacy_and_catalog_role_files_require_explicit_selection(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    legacy = tmp_path / ".cognityx-ingest/source_catalog.sqlite3"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy")
    role_path = runtime.for_role("catalog").native_path(
        "ingest/source_catalog.sqlite3"
    )
    role_path.parent.mkdir(parents=True, exist_ok=True)
    role_path.write_bytes(b"role")

    with pytest.raises(SourceAssetCatalogAmbiguityError, match="Both a legacy"):
        SourceAssetRegistry.load(runtime=runtime)


def test_runtime_and_storage_config_are_not_combined(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="either runtime or storage_config"):
        SourceAssetRegistry.load(
            runtime=_runtime(tmp_path), storage_config=tmp_path / "storage.toml"
        )


def test_compatibility_registry_alias_uses_loader() -> None:
    from cognityx_ingest import SourceRegistry

    assert SourceRegistry is SourceAssetRegistry
    assert hasattr(SourceRegistry, "load")
