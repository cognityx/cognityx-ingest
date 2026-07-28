from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import time

import pytest

from cognityx_ingest.models import ExecutionContext
from cognityx_ingest.cleanup import SourceAssetCleanupService
from cognityx_ingest.source_migration import SourceBlobMigrationError
from cognityx_ingest.sources import SourceRegistry
from cognityx_resource import ResourceContext
from cognityx_storage import (
    BlobRef,
    BlobStore,
    LocalStorageBackend,
    StorageBackendFactory,
    StorageCapabilities,
    StorageClient,
    StorageConfig,
    StorageRuntime,
    StoredObject,
)


def _context(
    tenant: str = "tenant-a", *, project: str | None = None
) -> ExecutionContext:
    return ExecutionContext(
        run_id="run",
        correlation_id="correlation",
        principal_id="alice",
        tenant_id=tenant,
        project_id=project,
    )


def _config(
    root: Path,
    *,
    dedup_scope: str = "tenant",
    profile_name: str = "local-main",
    profile_type: str = "filesystem",
    profiles: dict[str, dict[str, str]] | None = None,
) -> StorageConfig:
    selected_profiles = profiles or {
        profile_name: {"type": profile_type, "root": str(root)}
    }
    return StorageConfig.from_dict(
        {
            "storage": {
                "default_profile": profile_name,
                "profiles": selected_profiles,
                "roles": {
                    "source_asset": {
                        "profile": profile_name,
                        "namespace": "source-assets",
                        "dedup_scope": dedup_scope,
                    }
                },
            }
        }
    )


def _registry(
    root: Path,
    *,
    dedup_scope: str = "tenant",
    runtime: StorageRuntime | None = None,
) -> SourceRegistry:
    selected = runtime or StorageRuntime.from_config(
        _config(root, dedup_scope=dedup_scope)
    )
    return SourceRegistry(
        selected, root / ".cognityx-ingest" / "source_catalog.sqlite3"
    )


def _blob_files(root: Path) -> list[Path]:
    blob_root = root / "source-assets" / "blob-domains"
    return [path for path in blob_root.rglob("*") if path.is_file()]


def test_new_registration_persists_complete_blob_ref_without_ingest_blob_table(
    tmp_path: Path,
) -> None:
    root = tmp_path / "storage"
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF-storage-owned")
    registry = _registry(root)

    result = registry.register_file(_context(), source)

    with sqlite3.connect(root / ".cognityx-ingest/source_catalog.sqlite3") as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM sources WHERE source_id=?", (result.source_id,)
        ).fetchone()
        tables = {
            item[0]
            for item in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    blob_ref = BlobRef.from_dict(json.loads(row["blob_ref_json"]))
    assert blob_ref.blob_id == row["blob_id"]
    assert blob_ref.digest == row["sha256"]
    assert blob_ref.size_bytes == row["size_bytes"]
    assert blob_ref.uri.startswith("storage://local-main/source-assets/")
    assert "blobs" not in tables
    assert len(_blob_files(root)) == 1


@pytest.mark.parametrize(
    ("scope", "expected"),
    [("tenant", 2), ("context", 2), ("platform", 1), ("none", 2)],
)
def test_storage_dedup_configuration_is_authoritative(
    tmp_path: Path, scope: str, expected: int
) -> None:
    root = tmp_path / scope
    source = tmp_path / f"{scope}.txt"
    source.write_bytes(b"same")
    registry = _registry(root, dedup_scope=scope)

    registry.register_file(_context("tenant-a", project="one"), source)
    registry.register_file(
        _context("tenant-b", project="two"), source, bundle="other"
    )

    assert len(_blob_files(root)) == expected


def test_same_bundle_reuses_source_while_different_bundle_reuses_storage_blob(
    tmp_path: Path,
) -> None:
    root = tmp_path / "storage"
    source = tmp_path / "source.txt"
    source.write_bytes(b"same")
    registry = _registry(root)

    first = registry.register_file(_context(), source)
    duplicate = registry.register_file(_context(), source)
    other = registry.register_file(_context(), source, bundle="other")

    assert duplicate.status == "already_registered"
    assert duplicate.source_id == first.source_id
    assert other.source_id != first.source_id
    assert len(_blob_files(root)) == 1


def test_none_scope_duplicate_source_does_not_publish_orphan_blob(
    tmp_path: Path,
) -> None:
    root = tmp_path / "storage"
    source = tmp_path / "source.txt"
    source.write_bytes(b"same")
    registry = _registry(root, dedup_scope="none")

    first = registry.register_file(_context(), source)
    duplicate = registry.register_file(_context(), source)

    assert first.status == "created"
    assert duplicate.status == "already_registered"
    assert duplicate.source_id == first.source_id
    assert len(_blob_files(root)) == 1

    other = registry.register_file(_context(), source, bundle="other")

    assert other.status == "created"
    assert other.source_id != first.source_id
    assert len(_blob_files(root)) == 2


def test_concurrent_none_scope_duplicate_source_has_one_source_and_blob(
    tmp_path: Path,
) -> None:
    root = tmp_path / "storage"
    source = tmp_path / "concurrent.txt"
    source.write_bytes(b"concurrent same bytes")
    registry = _registry(root, dedup_scope="none")

    with ThreadPoolExecutor(max_workers=2) as workers:
        first, second = workers.map(
            lambda _: registry.register_file(_context(), source), range(2)
        )

    assert {first.status, second.status} == {"created", "already_registered"}
    assert first.source_id == second.source_id
    assert len(registry.list_sources(_context())) == 1
    assert len(_blob_files(root)) == 1


def test_none_scope_restores_same_asset_after_gc_removes_old_blob(
    tmp_path: Path,
) -> None:
    root = tmp_path / "storage"
    source = tmp_path / "restore.txt"
    source.write_bytes(b"restore after gc")
    registry = _registry(root, dedup_scope="none")
    execution = _context()
    created = registry.register_file(execution, source)
    old_ref = registry._blob_ref_for_source(created.source_id)

    registry.delete_asset(execution, created.source_id)
    old_path = root / old_ref.storage_key
    old = time.time() - 10
    os.utime(old_path, (old, old))
    cleanup = SourceAssetCleanupService(
        registry=registry, storage_runtime=registry._runtime
    )
    plan = cleanup.plan_blobs(execution, older_than=timedelta(seconds=1))
    result = cleanup.execute_blobs(execution, plan, batch_size=1)

    assert result.deleted_objects == 1
    assert not old_path.exists()

    restored = registry.register_file(execution, source)

    assert restored.status == "restored"
    assert restored.source_id == created.source_id
    new_ref = registry._blob_ref_for_source(restored.source_id)
    assert registry._runtime.blob_exists(new_ref)
    with registry.open(execution, restored.source_id) as stream:
        assert stream.read() == b"restore after gc"


def test_domain_scoped_catalog_migrates_once_and_preserves_ids(
    tmp_path: Path,
) -> None:
    root = tmp_path / "storage"
    expected = _write_legacy_catalog(root, domain_scoped=True)
    registry = _registry(root)

    with registry.open(_context(), "src-legacy") as stream:
        assert stream.read() == expected
    first_ref = _stored_blob_ref(root, "src-legacy")
    before = {path: path.stat().st_mtime_ns for path in _blob_files(root)}

    restarted = _registry(root)

    assert _stored_blob_ref(root, "src-legacy") == first_ref
    assert before == {path: path.stat().st_mtime_ns for path in _blob_files(root)}
    assert restarted.show_source(_context(), "src-legacy").source_id == "src-legacy"
    new_source = tmp_path / "new.txt"
    new_source.write_bytes(b"new content")
    restarted.register_file(_context(), new_source)
    with sqlite3.connect(root / ".cognityx-ingest/source_catalog.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM blobs").fetchone()[0] == 1


def test_global_v1_catalog_migrates_directly_to_blob_ref(
    tmp_path: Path,
) -> None:
    root = tmp_path / "storage"
    expected = _write_legacy_catalog(root, domain_scoped=False)

    registry = _registry(root)

    with registry.open(_context(), "src-legacy") as stream:
        assert stream.read() == expected
    assert _stored_blob_ref(root, "src-legacy").digest == sha256(
        expected
    ).hexdigest()


def test_interrupted_migration_resumes_without_changing_source_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "storage"
    _write_legacy_catalog(root, domain_scoped=True, source_count=2)
    original = BlobStore.put_stream
    calls = 0

    def fail_second(self, source, *, context, media_type="application/octet-stream"):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated interruption")
        return original(self, source, context=context, media_type=media_type)

    monkeypatch.setattr(BlobStore, "put_stream", fail_second)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        _registry(root)

    with sqlite3.connect(root / ".cognityx-ingest/source_catalog.sqlite3") as db:
        migrated = db.execute(
            "SELECT source_id FROM sources WHERE blob_ref_json IS NOT NULL"
        ).fetchall()
    assert migrated == [("src-legacy",)]

    monkeypatch.setattr(BlobStore, "put_stream", original)
    registry = _registry(root)

    assert {item.source_id for item in registry.list_sources(_context())} == {
        "src-legacy",
        "src-legacy-2",
    }
    metadata_sources = {
        path.parent.name
        for path in (
            root / "source-assets" / "source-contexts"
        ).rglob("source.json")
    }
    assert metadata_sources == {"src-legacy", "src-legacy-2"}


def test_corrupt_legacy_bytes_do_not_replace_source_mapping(
    tmp_path: Path,
) -> None:
    root = tmp_path / "storage"
    _write_legacy_catalog(root, domain_scoped=True, corrupt=True)

    with pytest.raises(SourceBlobMigrationError, match="consistency error"):
        _registry(root)

    with sqlite3.connect(root / ".cognityx-ingest/source_catalog.sqlite3") as db:
        row = db.execute(
            "SELECT blob_id, blob_ref_json FROM sources WHERE source_id='src-legacy'"
        ).fetchone()
    assert row[0] == "legacy-blob"
    assert row[1] is None
    assert not _blob_files(root)


def test_legacy_context_id_mismatch_stops_migration(
    tmp_path: Path,
) -> None:
    root = tmp_path / "storage"
    _write_legacy_catalog(root, domain_scoped=True)
    catalog = root / ".cognityx-ingest/source_catalog.sqlite3"
    with sqlite3.connect(catalog) as db:
        db.execute(
            "UPDATE contexts SET descriptors_json=?",
            (
                json.dumps(
                    ResourceContext(
                        principal_id="alice", tenant_id="tenant-b"
                    ).descriptors(),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )

    with pytest.raises(SourceBlobMigrationError, match="stored ID.*does not match"):
        _registry(root)

    with sqlite3.connect(catalog) as db:
        assert db.execute(
            "SELECT blob_ref_json FROM sources WHERE source_id='src-legacy'"
        ).fetchone()[0] is None


def test_blob_ref_profile_remains_authoritative_after_role_rerouting(
    tmp_path: Path,
) -> None:
    old_root, new_root = tmp_path / "old", tmp_path / "new"
    profiles = {
        "old-profile": {"type": "filesystem", "root": str(old_root)},
        "new-profile": {"type": "filesystem", "root": str(new_root)},
    }
    old_runtime = StorageRuntime.from_config(
        _config(
            old_root,
            profile_name="old-profile",
            profiles=profiles,
        )
    )
    catalog = tmp_path / "source_catalog.sqlite3"
    source = tmp_path / "durable.txt"
    source.write_bytes(b"durable routing")
    first = SourceRegistry(old_runtime, catalog)
    result = first.register_file(_context(), source)

    new_runtime = StorageRuntime.from_config(
        _config(
            new_root,
            profile_name="new-profile",
            profiles=profiles,
        )
    )
    rerouted = SourceRegistry(new_runtime, catalog)

    with rerouted.open(_context(), result.source_id) as stream:
        assert stream.read() == b"durable routing"
    assert rerouted.locate_source(
        _context(), result.source_id
    ).profile_name == "old-profile"


def test_non_local_locate_does_not_materialize(
    tmp_path: Path,
) -> None:
    backend = _NonLocalBackend(tmp_path / "remote")
    factory = StorageBackendFactory()
    factory.register(
        "filesystem",
        lambda profile: backend,
        capabilities=StorageCapabilities(),
    )
    config = _config(
        tmp_path / "unused",
        profile_name="remote-profile",
        profile_type="filesystem",
    )
    runtime = StorageRuntime.from_config(config, factory=factory)
    source = tmp_path / "remote.txt"
    source.write_bytes(b"remote")
    registry = SourceRegistry(runtime, tmp_path / "catalog.sqlite3")
    result = registry.register_file(_context(), source)

    location = registry.locate_source(_context(), result.source_id)

    assert location.local_path is None
    assert location.profile_name == "remote-profile"
    assert backend.materialize_calls == 0


def test_source_metadata_contains_no_raw_source_bytes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "storage"
    source = tmp_path / "private-name.pdf"
    content = b"%PDF-private-content"
    source.write_bytes(content)
    registry = _registry(root)
    result = registry.register_file(_context(), source)

    metadata_root = root / "source-assets" / "source-contexts"
    metadata_files = [path for path in metadata_root.rglob("*") if path.is_file()]
    assert metadata_files
    assert all(path.read_bytes() != content for path in metadata_files)
    payload = json.loads(
        next(
            path
            for path in metadata_files
            if path.name == "source.json"
            and result.source_id in path.parts
        ).read_text(encoding="utf-8")
    )
    assert payload["blob_uri"].startswith("storage://local-main/")
    assert _blob_files(root)[0].read_bytes() == content


def _write_legacy_catalog(
    root: Path,
    *,
    domain_scoped: bool,
    source_count: int = 1,
    corrupt: bool = False,
) -> bytes:
    expected = b"legacy-content"
    stored = b"corrupt-content" if corrupt else expected
    digest = sha256(expected).hexdigest()
    legacy = StorageClient(LocalStorageBackend(root)).for_shared_data()
    key = (
        f"blob-domains/tenant-old/sha256/{digest[:2]}/{digest[2:4]}/{digest}"
        if domain_scoped
        else f"blobs/sha256/{digest[:2]}/{digest[2:4]}/{digest}"
    )
    legacy.put_bytes(key, stored)
    context = ResourceContext(principal_id="alice", tenant_id="tenant-a")
    catalog = root / ".cognityx-ingest" / "source_catalog.sqlite3"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(catalog) as db:
        db.executescript(
            """
            CREATE TABLE contexts(
                context_id TEXT PRIMARY KEY, context_type TEXT,
                descriptors_json TEXT, created_at TEXT
            );
            CREATE TABLE bundles(
                bundle_id TEXT PRIMARY KEY, context_id TEXT, name TEXT,
                parent_bundle_id TEXT, created_by TEXT, created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE sources(
                source_id TEXT PRIMARY KEY, context_id TEXT, bundle_id TEXT,
                original_filename TEXT, media_type TEXT, size_bytes INTEGER,
                sha256 TEXT, blob_id TEXT, created_by TEXT, created_at TEXT,
                UNIQUE(bundle_id,sha256)
            );
            """
        )
        if domain_scoped:
            db.execute(
                "CREATE TABLE blobs("
                "blob_id TEXT PRIMARY KEY, dedup_domain_id TEXT, sha256 TEXT, "
                "storage_key TEXT, size_bytes INTEGER, created_at TEXT)"
            )
        else:
            db.execute(
                "CREATE TABLE blobs("
                "blob_id TEXT PRIMARY KEY, sha256 TEXT UNIQUE, storage_key TEXT, "
                "size_bytes INTEGER, created_at TEXT)"
            )
        db.execute(
            "INSERT INTO contexts VALUES (?, 'user', ?, 'now')",
            (
                context.context_id,
                json.dumps(
                    context.descriptors(),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
        for index in range(source_count):
            suffix = "" if index == 0 else f"-{index + 1}"
            bundle_id = f"bun-legacy{suffix}"
            blob_id = f"legacy-blob{suffix}"
            source_id = f"src-legacy{suffix}"
            source_key = key
            source_digest = digest
            source_bytes = expected
            if index:
                source_bytes = expected + str(index).encode()
                source_digest = sha256(source_bytes).hexdigest()
                source_key = (
                    f"blob-domains/tenant-old/sha256/{source_digest[:2]}/"
                    f"{source_digest[2:4]}/{source_digest}"
                )
                legacy.put_bytes(source_key, source_bytes)
            db.execute(
                "INSERT INTO bundles VALUES (?, ?, ?, NULL, 'alice', ?, ?)",
                (bundle_id, context.context_id, f"default{suffix}", str(index), str(index)),
            )
            if domain_scoped:
                db.execute(
                    "INSERT INTO blobs VALUES (?, 'tenant-old', ?, ?, ?, ?)",
                    (
                        blob_id,
                        source_digest,
                        source_key,
                        len(source_bytes),
                        str(index),
                    ),
                )
            else:
                db.execute(
                    "INSERT INTO blobs VALUES (?, ?, ?, ?, ?)",
                    (
                        blob_id,
                        source_digest,
                        source_key,
                        len(source_bytes),
                        str(index),
                    ),
                )
            db.execute(
                "INSERT INTO sources VALUES (?, ?, ?, ?, 'text/plain', ?, ?, ?, "
                "'alice', ?)",
                (
                    source_id,
                    context.context_id,
                    bundle_id,
                    f"legacy{suffix}.txt",
                    len(source_bytes),
                    source_digest,
                    blob_id,
                    str(index),
                ),
            )
    return expected


def _stored_blob_ref(root: Path, source_id: str) -> BlobRef:
    with sqlite3.connect(root / ".cognityx-ingest/source_catalog.sqlite3") as db:
        payload = db.execute(
            "SELECT blob_ref_json FROM sources WHERE source_id=?", (source_id,)
        ).fetchone()[0]
    return BlobRef.from_dict(json.loads(payload))


class _NonLocalBackend:
    def __init__(self, root: Path) -> None:
        self._local = LocalStorageBackend(root)
        self.materialize_calls = 0

    def put_stream(
        self,
        key: str,
        source,
        *,
        media_type: str = "application/octet-stream",
    ) -> StoredObject:
        return self._local.put_stream(key, source, media_type=media_type)

    def put_file(
        self, key: str, source: str | Path, *, media_type: str | None = None
    ) -> StoredObject:
        return self._local.put_file(key, source, media_type=media_type)

    def put_directory(self, key: str, source: str | Path) -> StoredObject:
        return self._local.put_directory(key, source)

    def open_reader(self, key: str):
        return self._local.open_reader(key)

    def materialize(self, key: str) -> Path:
        self.materialize_calls += 1
        raise AssertionError("locate must not materialize remote content")

    def resolve_local_path(self, key: str) -> None:
        return None

    def stat(self, key: str) -> StoredObject:
        return self._local.stat(key)

    def exists(self, key: str) -> bool:
        return self._local.exists(key)

    def list(self, prefix: str = "") -> tuple[StoredObject, ...]:
        return self._local.list(prefix)

    def delete(self, key: str, *, recursive: bool = False) -> None:
        self._local.delete(key, recursive=recursive)
