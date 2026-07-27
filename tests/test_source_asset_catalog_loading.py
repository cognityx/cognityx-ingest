from __future__ import annotations

from pathlib import Path

import pytest

from cognityx_ingest import (
    SourceAssetCatalogAmbiguityError,
    SourceAssetCatalogError,
    SourceAssetRegistry,
)
from cognityx_storage import StorageConfig, StorageRuntime
from cognityx_storage import StorageBackendFactory, StorageCapabilities


class _RemoteTestBackend:
    """Provider stub used only to prove catalog/source-role separation."""


class _NativeTestBackend:
    def __init__(self, root: Path) -> None:
        self.root = root

    def native_path(self, key: str) -> Path:
        return self.root / key


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


def _split_runtime(
    tmp_path: Path,
    *,
    catalog_capabilities: StorageCapabilities,
    catalog_backend: object,
) -> StorageRuntime:
    config = StorageConfig.from_dict(
        {
            "storage": {
                "profiles": {
                    "remote-source": {"type": "object"},
                    "local-catalog": {
                        "type": "filesystem",
                        "root": str(tmp_path / "catalog-root"),
                    },
                },
                "roles": {
                    "source_asset": {
                        "profile": "remote-source",
                        "namespace": "source-assets",
                    },
                    "catalog": {
                        "profile": "local-catalog",
                        "namespace": "catalog",
                        "preferred_capabilities": [
                            "native_path",
                            "random_write",
                            "file_locking",
                        ],
                    },
                },
            }
        }
    )
    factory = StorageBackendFactory()
    factory.register(
        "object",
        lambda profile: _RemoteTestBackend(),
        capabilities=StorageCapabilities(
            stream_read=True,
            stream_write=True,
            distributed=True,
        ),
    )
    factory.register(
        "filesystem",
        lambda profile: catalog_backend,
        capabilities=catalog_capabilities,
    )
    return StorageRuntime.from_config(config, factory=factory)


def test_remote_source_role_can_use_local_catalog_role(tmp_path: Path) -> None:
    runtime = _split_runtime(
        tmp_path,
        catalog_capabilities=StorageCapabilities(
            native_path=True,
            random_write=True,
            file_locking=True,
        ),
        catalog_backend=_NativeTestBackend(tmp_path / "catalog-root"),
    )

    registry = SourceAssetRegistry.load(runtime=runtime)

    assert registry.catalog_info()["catalog_profile"] == "local-catalog"
    assert runtime.for_role("source_asset").profile_name == "remote-source"
    assert registry.catalog_path == tmp_path / "catalog-root/catalog/ingest/source_catalog.sqlite3"


def test_unsafe_catalog_capabilities_are_rejected_before_sqlite(
    tmp_path: Path,
) -> None:
    runtime = _split_runtime(
        tmp_path,
        catalog_capabilities=StorageCapabilities(native_path=True),
        catalog_backend=_NativeTestBackend(tmp_path / "unsafe-root"),
    )

    with pytest.raises(SourceAssetCatalogError) as caught:
        SourceAssetRegistry.load(runtime=runtime)

    message = str(caught.value)
    assert "Resolved profile: local-catalog" in message
    assert "Backend: _NativeTestBackend" in message
    assert "random_write" in message
    assert "file_locking" in message
    assert "pass catalog_path explicitly" in message
    assert not (tmp_path / "unsafe-root").exists()


def test_explicit_catalog_path_overrides_unsafe_catalog_role(tmp_path: Path) -> None:
    runtime = _split_runtime(
        tmp_path,
        catalog_capabilities=StorageCapabilities(),
        catalog_backend=_RemoteTestBackend(),
    )
    explicit = tmp_path / "explicit.sqlite3"

    registry = SourceAssetRegistry.load(runtime=runtime, catalog_path=explicit)

    assert registry.catalog_path == explicit
    assert explicit.exists()


def test_catalog_without_native_path_is_rejected_but_explicit_path_works(
    tmp_path: Path,
) -> None:
    runtime = _split_runtime(
        tmp_path,
        catalog_capabilities=StorageCapabilities(),
        catalog_backend=_RemoteTestBackend(),
    )

    with pytest.raises(SourceAssetCatalogError, match="native_path"):
        SourceAssetRegistry.load(runtime=runtime)

    explicit = tmp_path / "native-explicit.sqlite3"
    registry = SourceAssetRegistry.load(runtime=runtime, catalog_path=explicit)
    assert registry.catalog_path == explicit
