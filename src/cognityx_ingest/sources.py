"""Durable, trust-domain-aware source registration over Cognityx storage."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
import json
import mimetypes
import os
from pathlib import Path
import sqlite3
from typing import Any, BinaryIO, Iterator
from uuid import uuid4

from cognityx_storage import ObjectAlreadyExistsError, StorageClient
from cognityx_ingest.control import (
    ControlClient, INGEST_BUNDLE_CREATE, INGEST_BUNDLE_LOCATE, INGEST_BUNDLE_READ,
    INGEST_SOURCE_CREATE, INGEST_SOURCE_LIST, INGEST_SOURCE_LOCATE,
    INGEST_SOURCE_READ, IngestAuthorizationError, LocalControlClient,
)
from cognityx_ingest.models import (
    ExecutionContext, RegisteredSource, SourceBundle, SourceContext, SourceLocation,
    SourceRegistrationResult,
)


class SourceRegistry:
    """SQLite logical source catalog; bytes and projections remain in Storage."""

    def __init__(self, storage: StorageClient, catalog_path: str | Path, *, control: ControlClient | None = None) -> None:
        self._storage, self._catalog_path = storage, Path(catalog_path)
        self._catalog_path.parent.mkdir(parents=True, exist_ok=True)
        self._control = control or LocalControlClient()
        self._initialise_and_migrate()

    @property
    def dedup_scope(self) -> str:
        scope = os.environ.get("COGNITYX_DEDUP_SCOPE", "tenant").strip().lower()
        if scope not in {"tenant", "context", "platform"}:
            raise ValueError("COGNITYX_DEDUP_SCOPE must be tenant, context, or platform.")
        return scope

    def resolve_context(self, execution: ExecutionContext) -> SourceContext:
        descriptors = execution.context.descriptors()
        canonical = json.dumps(descriptors, sort_keys=True, separators=(",", ":"))
        context_id = execution.context_id
        with self._connection() as db:
            db.execute("INSERT OR IGNORE INTO contexts(context_id, context_type, descriptors_json, created_at) VALUES (?, ?, ?, ?)", (context_id, execution.context_type, canonical, _now()))
            row = db.execute("SELECT * FROM contexts WHERE context_id=?", (context_id,)).fetchone()
        result = SourceContext(row["context_id"], row["context_type"], json.loads(row["descriptors_json"]), row["created_at"])
        self._publish_context(result)
        return result

    def resolve_bundle(self, execution: ExecutionContext, path: str = "default", *, create: bool = True) -> SourceBundle:
        context, parent = self.resolve_context(execution), None
        segments = _bundle_segments(path)
        current: sqlite3.Row | None = None
        for index, name in enumerate(segments):
            with self._connection(immediate=True) as db:
                current = db.execute("SELECT * FROM bundles WHERE context_id=? AND parent_bundle_id IS ? AND name=?", (context.context_id, parent, name)).fetchone()
                if current is None:
                    if not create:
                        raise KeyError(f"Bundle does not exist: {path}")
                    self._authorize(execution, INGEST_BUNDLE_CREATE, {"context_id": context.context_id, "path": "/".join(segments[:index + 1])})
                    now, bundle_id = _now(), f"bun-{uuid4().hex}"
                    try:
                        db.execute("INSERT INTO bundles VALUES (?, ?, ?, ?, ?, ?, ?)", (bundle_id, context.context_id, name, parent, execution.principal_id, now, now))
                    except sqlite3.IntegrityError:
                        pass
                    current = db.execute("SELECT * FROM bundles WHERE context_id=? AND parent_bundle_id IS ? AND name=?", (context.context_id, parent, name)).fetchone()
            assert current is not None
            parent = current["bundle_id"]
        result = self._bundle_from_row(current, self._bundle_path(context.context_id, current["bundle_id"]))
        self._publish_bundle(result)
        return result

    def register_file(self, execution: ExecutionContext, file: str | Path, *, bundle: str | None = None) -> SourceRegistrationResult:
        path = Path(file)
        if not path.is_file():
            raise FileNotFoundError(f"Source file does not exist or is not a file: {path}")
        context, target = self.resolve_context(execution), self.resolve_bundle(execution, bundle or "default")
        self._authorize(execution, INGEST_SOURCE_CREATE, {"context_id": context.context_id, "bundle_id": target.bundle_id})
        digest, size = _hash_file(path)
        existing = self._source_for_digest(context.context_id, target.bundle_id, digest)
        if existing:
            return _registration(existing, "already_registered")
        domain = self._dedup_domain(context)
        blob = self._ensure_blob(domain, digest, size, path, mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        with self._connection(immediate=True) as db:
            row = db.execute("SELECT * FROM sources WHERE bundle_id=? AND sha256=?", (target.bundle_id, digest)).fetchone()
            if row:
                return _registration(self._source_from_row(row), "already_registered")
            source_id, now = f"src-{uuid4().hex}", _now()
            db.execute("INSERT INTO sources(source_id, context_id, bundle_id, original_filename, media_type, size_bytes, sha256, blob_id, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (source_id, context.context_id, target.bundle_id, path.name, mimetypes.guess_type(path.name)[0] or "application/octet-stream", size, digest, blob["blob_id"], execution.principal_id, now))
            row = db.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
        source = self._source_from_row(row)
        self._publish_source(source, blob)
        return _registration(source, "created")

    def list_bundles(self, execution: ExecutionContext) -> tuple[SourceBundle, ...]:
        context = self.resolve_context(execution)
        self._authorize(execution, INGEST_BUNDLE_READ, {"context_id": context.context_id})
        with self._connection() as db:
            rows = db.execute("SELECT * FROM bundles WHERE context_id=? ORDER BY created_at, name", (context.context_id,)).fetchall()
        return tuple(self._bundle_from_row(row, self._bundle_path(context.context_id, row["bundle_id"])) for row in rows)

    def list_sources(self, execution: ExecutionContext, *, bundle: str | None = None) -> tuple[RegisteredSource, ...]:
        context, query, values = self.resolve_context(execution), "SELECT * FROM sources WHERE context_id=?", [self.resolve_context(execution).context_id]
        resource: dict[str, Any] = {"context_id": context.context_id}
        if bundle is not None:
            target = self.resolve_bundle(execution, bundle, create=False)
            query += " AND bundle_id=?"; values.append(target.bundle_id); resource["bundle_id"] = target.bundle_id
        self._authorize(execution, INGEST_SOURCE_LIST, resource)
        with self._connection() as db:
            return tuple(self._source_from_row(row) for row in db.execute(query + " ORDER BY created_at", values).fetchall())

    def show_source(self, execution: ExecutionContext, source_id: str) -> RegisteredSource:
        context = self.resolve_context(execution)
        self._authorize(execution, INGEST_SOURCE_READ, {"context_id": context.context_id, "source_id": source_id})
        with self._connection() as db:
            row = db.execute("SELECT * FROM sources WHERE source_id=? AND context_id=?", (source_id, context.context_id)).fetchone()
        if not row: raise KeyError(f"Source does not exist in this context: {source_id}")
        return self._source_from_row(row)

    def open(self, execution: ExecutionContext, source_id: str) -> BinaryIO:
        source, blob = self.show_source(execution, source_id), self._blob_for_source(source_id)
        return self._storage.open(blob["storage_key"])

    def locate_source(self, execution: ExecutionContext, source_id: str) -> SourceLocation:
        context = self.resolve_context(execution)
        self._authorize(execution, INGEST_SOURCE_LOCATE, {"context_id": context.context_id, "source_id": source_id})
        with self._connection() as db:
            source = db.execute("SELECT source_id FROM sources WHERE source_id=? AND context_id=?", (source_id, context.context_id)).fetchone()
        if not source: raise KeyError(f"Source does not exist in this context: {source_id}")
        blob = self._blob_for_source(source_id)
        local = self._storage.resolve_local_path(blob["storage_key"])
        return SourceLocation(source_id, blob["blob_id"], self._storage.uri(blob["storage_key"]), self._storage.backend_name, str(local) if local else None)

    def locate_bundle(self, execution: ExecutionContext, bundle_id: str) -> dict[str, str | None]:
        context = self.resolve_context(execution)
        self._authorize(execution, INGEST_BUNDLE_LOCATE, {"context_id": context.context_id, "bundle_id": bundle_id})
        with self._connection() as db:
            row = db.execute("SELECT * FROM bundles WHERE bundle_id=? AND context_id=?", (bundle_id, context.context_id)).fetchone()
        if not row: raise KeyError(f"Bundle does not exist in this context: {bundle_id}")
        self._publish_bundle(self._bundle_from_row(row, self._bundle_path(context.context_id, bundle_id)))
        key = f"source-contexts/{context.context_id}/bundles/{bundle_id}"
        local = self._storage.resolve_local_path(key)
        return {"bundle_id": bundle_id, "uri": self._storage.uri(key), "backend": self._storage.backend_name, "local_path": str(local) if local else None}

    def _ensure_blob(self, domain: str, digest: str, size: int, file: Path, media_type: str) -> sqlite3.Row:
        with self._connection() as db:
            row = db.execute("SELECT * FROM blobs WHERE dedup_domain_id=? AND sha256=?", (domain, digest)).fetchone()
        if row: return row
        key, blob_id = _blob_key(domain, digest), f"blob-{sha256(f'{domain}:{digest}'.encode()).hexdigest()[:24]}"
        try: self._storage.put_file(key, file, media_type=media_type)
        except ObjectAlreadyExistsError: pass
        with self._connection(immediate=True) as db:
            db.execute("INSERT OR IGNORE INTO blobs(blob_id, dedup_domain_id, sha256, storage_key, size_bytes, created_at) VALUES (?, ?, ?, ?, ?, ?)", (blob_id, domain, digest, key, size, _now()))
            return db.execute("SELECT * FROM blobs WHERE dedup_domain_id=? AND sha256=?", (domain, digest)).fetchone()

    def _dedup_domain(self, context: SourceContext) -> str:
        if self.dedup_scope == "platform": return "platform"
        if self.dedup_scope == "context": return f"ctx-{context.context_id}"
        descriptor = context.descriptors
        if descriptor.get("tenant_id"): return f"tenant-{_domain_token(descriptor['tenant_id'])}"
        if context.context_type == "system": return f"system-{_domain_token(context.context_id)}"
        principal = descriptor.get("principal_id") or context.context_id
        return f"principal-{_domain_token(principal)}"

    def _blob_for_source(self, source_id: str) -> sqlite3.Row:
        with self._connection() as db:
            row = db.execute("SELECT b.* FROM blobs b JOIN sources s ON s.blob_id=b.blob_id WHERE s.source_id=?", (source_id,)).fetchone()
        if not row: raise RuntimeError(f"Source {source_id} has no registered blob.")
        return row

    def _source_for_digest(self, context_id: str, bundle_id: str, digest: str) -> RegisteredSource | None:
        with self._connection() as db: row = db.execute("SELECT * FROM sources WHERE context_id=? AND bundle_id=? AND sha256=?", (context_id, bundle_id, digest)).fetchone()
        return self._source_from_row(row) if row else None

    def _bundle_path(self, context_id: str, bundle_id: str) -> str:
        with self._connection() as db:
            parts, row = [], db.execute("SELECT * FROM bundles WHERE context_id=? AND bundle_id=?", (context_id, bundle_id)).fetchone()
            while row:
                parts.append(row["name"]); row = db.execute("SELECT * FROM bundles WHERE bundle_id=?", (row["parent_bundle_id"],)).fetchone() if row["parent_bundle_id"] else None
        return "/".join(reversed(parts))

    def _publish_context(self, item: SourceContext) -> None: self._storage.put_json_idempotent(f"source-contexts/{item.context_id}/context.json", {"context_id": item.context_id, "context_type": item.context_type, "descriptors": item.descriptors, "created_at": item.created_at})
    def _publish_bundle(self, item: SourceBundle) -> None: self._storage.put_json_idempotent(f"source-contexts/{item.context_id}/bundles/{item.bundle_id}/bundle.json", {"bundle_id": item.bundle_id, "context_id": item.context_id, "name": item.name, "path": item.path, "parent_bundle_id": item.parent_bundle_id, "created_by": item.created_by, "created_at": item.created_at, "updated_at": item.updated_at})
    def _publish_source(self, item: RegisteredSource, blob: sqlite3.Row) -> None: self._storage.put_json_idempotent(f"source-contexts/{item.context_id}/bundles/{item.bundle_id}/sources/{item.source_id}/source.json", {"source_id": item.source_id, "context_id": item.context_id, "bundle_id": item.bundle_id, "original_filename": item.original_filename, "media_type": item.media_type, "size_bytes": item.size_bytes, "sha256": item.sha256, "blob_id": item.blob_id, "blob_uri": self._storage.uri(blob["storage_key"]), "created_by": item.created_by, "created_at": item.created_at})

    def _authorize(self, context: ExecutionContext, action: str, resource: object) -> None:
        decision = self._control.authorize(context, action, resource=resource)
        if not decision.allowed: raise IngestAuthorizationError(decision.reason or f"Control policy rejected {action}.")

    @staticmethod
    def _bundle_from_row(row: sqlite3.Row, path: str) -> SourceBundle: return SourceBundle(row["bundle_id"], row["context_id"], row["name"], row["parent_bundle_id"], path, row["created_by"], row["created_at"], row["updated_at"])
    @staticmethod
    def _source_from_row(row: sqlite3.Row) -> RegisteredSource: return RegisteredSource(row["source_id"], row["context_id"], row["bundle_id"], row["original_filename"], row["media_type"], row["size_bytes"], row["sha256"], row["blob_id"], row["created_by"], row["created_at"])

    def _initialise_and_migrate(self) -> None:
        with self._connection() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS contexts (context_id TEXT PRIMARY KEY, context_type TEXT NOT NULL, descriptors_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bundles (bundle_id TEXT PRIMARY KEY, context_id TEXT NOT NULL, name TEXT NOT NULL, parent_bundle_id TEXT, created_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(context_id,parent_bundle_id,name));
            CREATE UNIQUE INDEX IF NOT EXISTS bundles_context_parent_name_unique ON bundles(context_id,IFNULL(parent_bundle_id,''),name);
            CREATE TABLE IF NOT EXISTS sources (source_id TEXT PRIMARY KEY, context_id TEXT NOT NULL, bundle_id TEXT NOT NULL, original_filename TEXT NOT NULL, media_type TEXT NOT NULL, size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, blob_id TEXT NOT NULL, created_by TEXT, created_at TEXT NOT NULL, UNIQUE(bundle_id,sha256));""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(blobs)").fetchall()}
            if columns and "dedup_domain_id" not in columns:
                db.execute("ALTER TABLE blobs RENAME TO blobs_legacy")
            db.execute("CREATE TABLE IF NOT EXISTS blobs (blob_id TEXT PRIMARY KEY, dedup_domain_id TEXT NOT NULL, sha256 TEXT NOT NULL, storage_key TEXT NOT NULL, size_bytes INTEGER NOT NULL, created_at TEXT NOT NULL, UNIQUE(dedup_domain_id,sha256))")
        self._migrate_legacy_blobs()

    def _migrate_legacy_blobs(self) -> None:
        with self._connection() as db:
            exists = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='blobs_legacy'").fetchone()
            if not exists: return
            rows = db.execute("SELECT s.source_id,s.context_id,l.* FROM sources s JOIN blobs_legacy l ON l.blob_id=s.blob_id").fetchall()
        for row in rows:
            with self._connection() as db:
                ctx = db.execute("SELECT * FROM contexts WHERE context_id=?", (row["context_id"],)).fetchone()
            context = SourceContext(ctx["context_id"], ctx["context_type"], json.loads(ctx["descriptors_json"]), ctx["created_at"])
            domain, key = self._dedup_domain(context), _blob_key(self._dedup_domain(context), row["sha256"])
            try:
                with self._storage.open(row["storage_key"]) as source: self._storage.put_stream(key, source)
            except ObjectAlreadyExistsError: pass
            blob_id = f"blob-{sha256(f'{domain}:{row['sha256']}'.encode()).hexdigest()[:24]}"
            with self._connection(immediate=True) as db:
                db.execute("INSERT OR IGNORE INTO blobs VALUES (?, ?, ?, ?, ?, ?)", (blob_id, domain, row["sha256"], key, row["size_bytes"], row["created_at"]))
                actual = db.execute("SELECT blob_id FROM blobs WHERE dedup_domain_id=? AND sha256=?", (domain, row["sha256"])).fetchone()["blob_id"]
                db.execute("UPDATE sources SET blob_id=? WHERE source_id=?", (actual, row["source_id"]))
        with self._connection() as db: db.execute("DROP TABLE blobs_legacy")

    @contextmanager
    def _connection(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self._catalog_path, timeout=30, isolation_level=None); db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON"); db.execute("PRAGMA busy_timeout=30000"); db.execute("PRAGMA journal_mode=WAL")
        try:
            if immediate: db.execute("BEGIN IMMEDIATE")
            yield db
            if immediate: db.commit()
        except BaseException:
            if immediate: db.rollback()
            raise
        finally: db.close()


def _bundle_segments(path: str) -> tuple[str, ...]:
    parts = tuple(item.strip() for item in path.strip("/").split("/") if item.strip())
    if not parts or any(item in {".", ".."} for item in parts): raise ValueError("Bundle path must contain normal name segments.")
    return parts
def _hash_file(path: Path) -> tuple[str, int]:
    digest, size = sha256(), 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024): digest.update(chunk); size += len(chunk)
    return digest.hexdigest(), size
def _blob_key(domain: str, digest: str) -> str: return f"blob-domains/{domain}/sha256/{digest[:2]}/{digest[2:4]}/{digest}"
def _domain_token(value: str) -> str: return sha256(value.encode()).hexdigest()[:20]
def _registration(source: RegisteredSource, status: str) -> SourceRegistrationResult: return SourceRegistrationResult(source.context_id, source.bundle_id, source.source_id, source.sha256, source.size_bytes, status)
def _now() -> str: return datetime.now(UTC).isoformat()
