"""Durable source registration over an opaque storage client."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
import json
import mimetypes
from pathlib import Path
import sqlite3
from typing import Any, BinaryIO, Iterator
from uuid import uuid4

from cognityx_storage import ObjectAlreadyExistsError, StorageClient

from cognityx_ingest.control import (
    ControlClient,
    INGEST_BUNDLE_CREATE,
    INGEST_BUNDLE_READ,
    INGEST_SOURCE_CREATE,
    INGEST_SOURCE_LIST,
    INGEST_SOURCE_READ,
    IngestAuthorizationError,
    LocalControlClient,
)
from cognityx_ingest.models import (
    ExecutionContext,
    RegisteredSource,
    SourceBundle,
    SourceContext,
    SourceRegistrationResult,
)


class SourceRegistry:
    """Catalog governed sources while keeping bytes in ``cognityx-storage``."""

    def __init__(
        self,
        storage: StorageClient,
        catalog_path: str | Path,
        *,
        control: ControlClient | None = None,
    ) -> None:
        self._storage = storage
        self._catalog_path = Path(catalog_path)
        self._catalog_path.parent.mkdir(parents=True, exist_ok=True)
        self._control = control or LocalControlClient()
        self._initialise()

    def resolve_context(self, execution: ExecutionContext) -> SourceContext:
        descriptors = _context_descriptors(execution)
        canonical = json.dumps(descriptors, sort_keys=True, separators=(",", ":"))
        context_id = f"ctx-{sha256(canonical.encode()).hexdigest()[:20]}"
        now = _now()
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO contexts(context_id, context_type, descriptors_json, created_at) VALUES (?, ?, ?, ?)",
                (context_id, execution.context_type, canonical, now),
            )
            row = connection.execute("SELECT * FROM contexts WHERE context_id = ?", (context_id,)).fetchone()
        return SourceContext(row["context_id"], row["context_type"], json.loads(row["descriptors_json"]), row["created_at"])

    def resolve_bundle(
        self,
        execution: ExecutionContext,
        path: str = "default",
        *,
        create: bool = True,
    ) -> SourceBundle:
        context = self.resolve_context(execution)
        segments = _bundle_segments(path)
        parent_id: str | None = None
        current: sqlite3.Row | None = None
        for index, name in enumerate(segments):
            with self._connection() as connection:
                current = connection.execute(
                    "SELECT * FROM bundles WHERE context_id = ? AND parent_bundle_id IS ? AND name = ?",
                    (context.context_id, parent_id, name),
                ).fetchone()
                if current is None:
                    if not create:
                        raise KeyError(f"Bundle does not exist: {path}")
                    self._authorize(execution, INGEST_BUNDLE_CREATE, {"context_id": context.context_id, "path": "/".join(segments[: index + 1])})
                    now = _now()
                    bundle_id = f"bun-{uuid4().hex}"
                    try:
                        connection.execute(
                            "INSERT INTO bundles(bundle_id, context_id, name, parent_bundle_id, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (bundle_id, context.context_id, name, parent_id, execution.principal_id, now, now),
                        )
                    except sqlite3.IntegrityError:
                        pass
                    current = connection.execute(
                        "SELECT * FROM bundles WHERE context_id = ? AND parent_bundle_id IS ? AND name = ?",
                        (context.context_id, parent_id, name),
                    ).fetchone()
            assert current is not None
            parent_id = current["bundle_id"]
        return self._bundle_from_row(current, self._bundle_path(context.context_id, current["bundle_id"]))

    def register_file(
        self,
        execution: ExecutionContext,
        file: str | Path,
        *,
        bundle: str | None = None,
    ) -> SourceRegistrationResult:
        source_path = Path(file)
        if not source_path.is_file():
            raise FileNotFoundError(f"Source file does not exist or is not a file: {source_path}")
        context = self.resolve_context(execution)
        target = self.resolve_bundle(execution, bundle or "default", create=True)
        self._authorize(execution, INGEST_SOURCE_CREATE, {"context_id": context.context_id, "bundle_id": target.bundle_id})
        digest, size = _hash_file(source_path)

        existing = self._source_for_digest(context.context_id, target.bundle_id, digest)
        if existing is not None:
            return _registration(existing, "already_registered")

        blob_id = f"sha256:{digest}"
        blob_key = _blob_key(digest)
        media_type = mimetypes.guess_type(source_path.name)[0] or "application/octet-stream"
        try:
            self._storage.put_file(blob_key, source_path, media_type=media_type)
        except ObjectAlreadyExistsError:
            pass
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO blobs(blob_id, sha256, storage_key, size_bytes, created_at) VALUES (?, ?, ?, ?, ?)",
                (blob_id, digest, blob_key, size, _now()),
            )

        with self._connection(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM sources WHERE bundle_id = ? AND sha256 = ?",
                (target.bundle_id, digest),
            ).fetchone()
            if existing is not None:
                return _registration(self._source_from_row(existing), "already_registered")
            now = _now()
            source_id = f"src-{uuid4().hex}"
            connection.execute(
                "INSERT INTO sources(source_id, context_id, bundle_id, original_filename, media_type, size_bytes, sha256, blob_id, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source_id, context.context_id, target.bundle_id, source_path.name, media_type, size, digest, blob_id, execution.principal_id, now),
            )
            created = connection.execute("SELECT * FROM sources WHERE source_id = ?", (source_id,)).fetchone()
        return _registration(self._source_from_row(created), "created")

    def list_bundles(self, execution: ExecutionContext) -> tuple[SourceBundle, ...]:
        context = self.resolve_context(execution)
        self._authorize(execution, INGEST_BUNDLE_READ, {"context_id": context.context_id})
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM bundles WHERE context_id = ? ORDER BY created_at, name", (context.context_id,)).fetchall()
            return tuple(self._bundle_from_row(row, self._bundle_path(context.context_id, row["bundle_id"])) for row in rows)

    def list_sources(self, execution: ExecutionContext, *, bundle: str | None = None) -> tuple[RegisteredSource, ...]:
        context = self.resolve_context(execution)
        resource: dict[str, Any] = {"context_id": context.context_id}
        query = "SELECT * FROM sources WHERE context_id = ?"
        parameters: list[str] = [context.context_id]
        if bundle is not None:
            target = self.resolve_bundle(execution, bundle, create=False)
            query += " AND bundle_id = ?"
            parameters.append(target.bundle_id)
            resource["bundle_id"] = target.bundle_id
        self._authorize(execution, INGEST_SOURCE_LIST, resource)
        with self._connection() as connection:
            return tuple(self._source_from_row(row) for row in connection.execute(query + " ORDER BY created_at", parameters).fetchall())

    def show_source(self, execution: ExecutionContext, source_id: str) -> RegisteredSource:
        context = self.resolve_context(execution)
        self._authorize(execution, INGEST_SOURCE_READ, {"context_id": context.context_id, "source_id": source_id})
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM sources WHERE source_id = ? AND context_id = ?", (source_id, context.context_id)).fetchone()
        if row is None:
            raise KeyError(f"Source does not exist in this context: {source_id}")
        return self._source_from_row(row)

    def open(self, execution: ExecutionContext, source_id: str) -> BinaryIO:
        source = self.show_source(execution, source_id)
        with self._connection() as connection:
            row = connection.execute("SELECT storage_key FROM blobs WHERE blob_id = ?", (source.blob_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"Source {source_id} has no registered blob.")
        return self._storage.open(row["storage_key"])

    def _source_for_digest(self, context_id: str, bundle_id: str, digest: str) -> RegisteredSource | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM sources WHERE context_id = ? AND bundle_id = ? AND sha256 = ?", (context_id, bundle_id, digest)).fetchone()
        return self._source_from_row(row) if row else None

    def _bundle_path(self, context_id: str, bundle_id: str) -> str:
        with self._connection() as connection:
            parts: list[str] = []
            current = connection.execute("SELECT * FROM bundles WHERE context_id = ? AND bundle_id = ?", (context_id, bundle_id)).fetchone()
            while current is not None:
                parts.append(current["name"])
                parent = current["parent_bundle_id"]
                current = connection.execute("SELECT * FROM bundles WHERE bundle_id = ?", (parent,)).fetchone() if parent else None
        return "/".join(reversed(parts))

    @staticmethod
    def _bundle_from_row(row: sqlite3.Row, path: str) -> SourceBundle:
        return SourceBundle(row["bundle_id"], row["context_id"], row["name"], row["parent_bundle_id"], path, row["created_by"], row["created_at"], row["updated_at"])

    @staticmethod
    def _source_from_row(row: sqlite3.Row) -> RegisteredSource:
        return RegisteredSource(row["source_id"], row["context_id"], row["bundle_id"], row["original_filename"], row["media_type"], row["size_bytes"], row["sha256"], row["blob_id"], row["created_by"], row["created_at"])

    def _authorize(self, context: ExecutionContext, action: str, resource: object) -> None:
        decision = self._control.authorize(context, action, resource=resource)
        if not decision.allowed:
            raise IngestAuthorizationError(decision.reason or f"Control policy rejected {action}.")

    def _initialise(self) -> None:
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS contexts (
                    context_id TEXT PRIMARY KEY, context_type TEXT NOT NULL,
                    descriptors_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bundles (
                    bundle_id TEXT PRIMARY KEY, context_id TEXT NOT NULL,
                    name TEXT NOT NULL, parent_bundle_id TEXT,
                    created_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(context_id, parent_bundle_id, name)
                );
                CREATE TABLE IF NOT EXISTS blobs (
                    blob_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE,
                    storage_key TEXT NOT NULL, size_bytes INTEGER NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sources (
                    source_id TEXT PRIMARY KEY, context_id TEXT NOT NULL, bundle_id TEXT NOT NULL,
                    original_filename TEXT NOT NULL, media_type TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL, blob_id TEXT NOT NULL, created_by TEXT, created_at TEXT NOT NULL,
                    UNIQUE(bundle_id, sha256)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS bundles_context_parent_name_unique
                    ON bundles(context_id, IFNULL(parent_bundle_id, ''), name);
            """)

    @contextmanager
    def _connection(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._catalog_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if immediate:
                connection.commit()
        except BaseException:
            if immediate:
                connection.rollback()
            raise
        finally:
            connection.close()


def _context_descriptors(execution: ExecutionContext) -> dict[str, str]:
    if execution.context_type not in {"user", "system"}:
        raise ValueError("Execution context type must be 'user' or 'system'.")
    values = {"context_type": execution.context_type}
    values.update({
        key: value for key, value in {
            "tenant_id": execution.tenant_id,
            "project_id": execution.project_id,
            "workspace_id": execution.workspace_id,
            "principal_id": execution.principal_id,
            **execution.scopes,
        }.items() if value is not None and value != ""
    })
    return dict(sorted(values.items()))


def _bundle_segments(path: str) -> tuple[str, ...]:
    segments = tuple(segment.strip() for segment in path.strip("/").split("/") if segment.strip())
    if not segments or any(segment in {".", ".."} or "/" in segment for segment in segments):
        raise ValueError("Bundle path must contain one or more normal name segments.")
    return segments


def _hash_file(path: Path) -> tuple[str, int]:
    digest = sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _blob_key(digest: str) -> str:
    return f"blobs/sha256/{digest[:2]}/{digest[2:4]}/{digest}"


def _registration(source: RegisteredSource, status: str) -> SourceRegistrationResult:
    return SourceRegistrationResult(source.context_id, source.bundle_id, source.source_id, source.sha256, source.size_bytes, status)


def _now() -> str:
    return datetime.now(UTC).isoformat()
