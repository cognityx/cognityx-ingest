from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from cognityx_ingest.context import resolve_execution_context
from cognityx_ingest.models import ExecutionContext
from cognityx_ingest.sources import SourceRegistry
from cognityx_storage import LocalStorageBackend, StorageClient
from cognityx_resource import ExecutionContext as SharedExecutionContext


def _registry(tmp_path: Path) -> SourceRegistry:
    root = tmp_path / "storage"
    return SourceRegistry(StorageClient(LocalStorageBackend(root)).for_shared_data(), root / ".cognityx-ingest" / "source_catalog.sqlite3")


def _context(tenant: str | None, principal: str = "alice", **extra: str) -> ExecutionContext:
    return ExecutionContext(run_id="r", correlation_id="c", principal_id=principal, tenant_id=tenant, scopes=extra)


def test_ingest_execution_context_is_shared_resource_implementation() -> None:
    assert ExecutionContext is SharedExecutionContext


def test_context_precedence_file_environment_project_and_overrides(tmp_path: Path, monkeypatch) -> None:
    explicit = tmp_path / "explicit.json"
    explicit.write_text(json.dumps({"principal_id": "alice", "workspace_id": "dev", "scopes": {"repo": "one"}}))
    resolved = resolve_execution_context(context_file=explicit, workspace_id="test", scopes={"function": "registration"})
    assert resolved.principal_id == "alice" and resolved.workspace_id == "test"
    assert resolved.scopes == {"repo": "one", "function": "registration"}
    env = tmp_path / "env.json"; env.write_text('{"tenant_id":"env"}')
    monkeypatch.setenv("COGNITYX_CONTEXT_FILE", str(env))
    assert resolve_execution_context().tenant_id == "env"
    monkeypatch.delenv("COGNITYX_CONTEXT_FILE")
    project = tmp_path / ".cognityx"; project.mkdir(); (project / "context.json").write_text('{"project_id":"project"}')
    assert resolve_execution_context(cwd=tmp_path).project_id == "project"
    assert resolve_execution_context().principal_id == "local"
    assert resolve_execution_context(context_type="system").principal_id is None


def test_dedup_domains_and_logical_metadata(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "file.txt"; source.write_bytes(b"same")
    registry = _registry(tmp_path)
    a = registry.register_file(_context("tenant-a"), source)
    b = registry.register_file(_context("tenant-a"), source, bundle="other")
    c = registry.register_file(_context("tenant-b"), source)
    assert a.source_id != b.source_id != c.source_id
    with sqlite3.connect(tmp_path / "storage/.cognityx-ingest/source_catalog.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM blobs").fetchone()[0] == 2
    context = registry.resolve_context(_context("tenant-a"))
    bundle = registry.resolve_bundle(_context("tenant-a"))
    metadata = tmp_path / "storage/shared/source-contexts" / context.context_id / "bundles" / bundle.bundle_id
    assert (metadata.parent.parent / "context.json").is_file()
    assert (metadata / "bundle.json").is_file()
    payload = json.loads((metadata / "sources" / a.source_id / "source.json").read_text())
    assert payload["blob_uri"].startswith("storage://shared/blob-domains/")
    assert not any(item.suffix == ".txt" for item in (tmp_path / "storage/shared/source-contexts").rglob("*"))
    monkeypatch.setenv("COGNITYX_DEDUP_SCOPE", "context")
    registry.register_file(_context("tenant-a", project="one"), source)
    registry.register_file(_context("tenant-a", project="two"), source)
    with sqlite3.connect(tmp_path / "storage/.cognityx-ingest/source_catalog.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM blobs").fetchone()[0] == 4
    monkeypatch.setenv("COGNITYX_DEDUP_SCOPE", "platform")
    registry.register_file(_context("tenant-c"), source)
    registry.register_file(_context("tenant-d"), source)
    with sqlite3.connect(tmp_path / "storage/.cognityx-ingest/source_catalog.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM blobs").fetchone()[0] == 5


def test_tenant_scope_cannot_contradict_tenant_dedup_domain(
    tmp_path: Path,
) -> None:
    source = tmp_path / "file.txt"
    source.write_bytes(b"same")
    registry = _registry(tmp_path)
    registry.register_file(_context("tenant-a"), source)

    with pytest.raises(ValueError, match=r"reserved: tenant_id"):
        registry.register_file(
            ExecutionContext(
                run_id="r",
                correlation_id="c",
                principal_id="alice",
                tenant_id="tenant-a",
                scopes={"tenant_id": "tenant-b"},
            ),
            source,
        )

    with sqlite3.connect(
        tmp_path / "storage/.cognityx-ingest/source_catalog.sqlite3"
    ) as db:
        domains = db.execute(
            "SELECT dedup_domain_id FROM blobs ORDER BY dedup_domain_id"
        ).fetchall()
    assert domains == [("tenant-80a707af7dc77ee1228f",)]


def test_locate_is_read_only_and_legacy_migration_is_domain_safe(tmp_path: Path) -> None:
    root, source = tmp_path / "storage", tmp_path / "legacy.txt"
    source.write_bytes(b"legacy")
    storage = StorageClient(LocalStorageBackend(root)).for_shared_data()
    digest = __import__("hashlib").sha256(b"legacy").hexdigest()
    old_key = f"blobs/sha256/{digest[:2]}/{digest[2:4]}/{digest}"
    storage.put_file(old_key, source)
    db_path = root / ".cognityx-ingest/source_catalog.sqlite3"; db_path.parent.mkdir(parents=True)
    descriptors = {"context_type": "user", "tenant_id": "tenant-a", "principal_id": "alice"}
    context_id = "ctx-" + __import__("hashlib").sha256(json.dumps(descriptors, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]
    with sqlite3.connect(db_path) as db:
        db.executescript("""CREATE TABLE contexts(context_id TEXT PRIMARY KEY, context_type TEXT, descriptors_json TEXT, created_at TEXT); CREATE TABLE bundles(bundle_id TEXT PRIMARY KEY, context_id TEXT, name TEXT, parent_bundle_id TEXT, created_by TEXT, created_at TEXT, updated_at TEXT); CREATE TABLE blobs(blob_id TEXT PRIMARY KEY, sha256 TEXT UNIQUE, storage_key TEXT, size_bytes INTEGER, created_at TEXT); CREATE TABLE sources(source_id TEXT PRIMARY KEY, context_id TEXT, bundle_id TEXT, original_filename TEXT, media_type TEXT, size_bytes INTEGER, sha256 TEXT, blob_id TEXT, created_by TEXT, created_at TEXT, UNIQUE(bundle_id,sha256));""")
        db.execute("INSERT INTO contexts VALUES (?,'user',?, 'now')", (context_id, json.dumps(descriptors, sort_keys=True, separators=(",", ":"))))
        db.execute("INSERT INTO bundles VALUES ('bun-old',?,'default',NULL,'alice','now','now')", (context_id,))
        db.execute("INSERT INTO blobs VALUES (?,?,?,?,?)", (f"sha256:{digest}",digest,old_key,6,"now"))
        db.execute("INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?,?)", ("src-old",context_id,"bun-old","legacy.txt","text/plain",6,digest,f"sha256:{digest}","alice","now"))
    registry = SourceRegistry(storage, db_path)
    context = ExecutionContext(run_id="x", correlation_id="y", principal_id="alice", tenant_id="tenant-a")
    with registry.open(context, "src-old") as opened: assert opened.read() == b"legacy"
    before = {path: path.stat().st_mtime_ns for path in root.rglob("*") if path.is_file()}
    located = registry.locate_source(context, "src-old")
    assert located.local_path and Path(located.local_path).is_file() and located.blob_uri.startswith("storage://")
    assert before == {path: path.stat().st_mtime_ns for path in root.rglob("*") if path.is_file()}
    bundle = registry.locate_bundle(context, "bun-old")
    assert bundle["local_path"] and "source-contexts" in bundle["local_path"]
