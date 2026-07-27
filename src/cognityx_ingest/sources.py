"""Durable source registration over Cognityx Storage Runtime."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import json
import mimetypes
import os
from pathlib import Path
import sqlite3
from typing import Any, BinaryIO, Iterator
from uuid import uuid4
import warnings

from cognityx_storage import BlobRef, StorageRuntime

from cognityx_ingest.control import (
    ControlClient,
    INGEST_BUNDLE_CREATE,
    INGEST_BUNDLE_LOCATE,
    INGEST_BUNDLE_READ,
    INGEST_SOURCE_CREATE,
    INGEST_SOURCE_LIST,
    INGEST_SOURCE_LOCATE,
    INGEST_SOURCE_READ,
    IngestAuthorizationError,
    LocalControlClient,
)
from cognityx_ingest.models import (
    ExecutionContext,
    RegisteredSource,
    SourceBundle,
    SourceContext,
    SourceLocation,
    SourceRegistrationResult,
)
from cognityx_ingest.source_migration import SourceBlobMigrator


class SourceRegistry:
    """SQLite logical Source catalog backed by Storage-owned immutable Blobs."""

    def __init__(
        self,
        storage_runtime: StorageRuntime,
        catalog_path: str | Path,
        *,
        control: ControlClient | None = None,
    ) -> None:
        self._runtime = storage_runtime
        self._blob_store = storage_runtime.blobs("source_asset")
        self._source_store = storage_runtime.for_role("source_asset")
        self._catalog_path = Path(catalog_path)
        self._catalog_path.parent.mkdir(parents=True, exist_ok=True)
        self._control = control or LocalControlClient()
        if "COGNITYX_DEDUP_SCOPE" in os.environ:
            warnings.warn(
                "COGNITYX_DEDUP_SCOPE is deprecated and ignored. Configure "
                "storage.roles.source_asset.dedup_scope instead.",
                DeprecationWarning,
                stacklevel=2,
            )
        self._initialise_and_migrate()

    def resolve_context(self, execution: ExecutionContext) -> SourceContext:
        descriptors = execution.context.descriptors()
        canonical = json.dumps(descriptors, sort_keys=True, separators=(",", ":"))
        context_id = execution.context_id
        with self._connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO contexts"
                "(context_id, context_type, descriptors_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (context_id, execution.context_type, canonical, _now()),
            )
            row = db.execute(
                "SELECT * FROM contexts WHERE context_id=?", (context_id,)
            ).fetchone()
        result = SourceContext(
            row["context_id"],
            row["context_type"],
            json.loads(row["descriptors_json"]),
            row["created_at"],
        )
        self._publish_context(result)
        return result

    def resolve_bundle(
        self,
        execution: ExecutionContext,
        path: str = "default",
        *,
        create: bool = True,
    ) -> SourceBundle:
        context, parent = self.resolve_context(execution), None
        segments = _bundle_segments(path)
        current: sqlite3.Row | None = None
        for index, name in enumerate(segments):
            with self._connection(immediate=True) as db:
                current = db.execute(
                    "SELECT * FROM bundles WHERE context_id=? "
                    "AND parent_bundle_id IS ? AND name=?",
                    (context.context_id, parent, name),
                ).fetchone()
                if current is None:
                    if not create:
                        raise KeyError(f"Bundle does not exist: {path}")
                    self._authorize(
                        execution,
                        INGEST_BUNDLE_CREATE,
                        {
                            "context_id": context.context_id,
                            "path": "/".join(segments[: index + 1]),
                        },
                    )
                    now, bundle_id = _now(), f"bun-{uuid4().hex}"
                    try:
                        db.execute(
                            "INSERT INTO bundles VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (
                                bundle_id,
                                context.context_id,
                                name,
                                parent,
                                execution.principal_id,
                                now,
                                now,
                            ),
                        )
                    except sqlite3.IntegrityError:
                        pass
                    current = db.execute(
                        "SELECT * FROM bundles WHERE context_id=? "
                        "AND parent_bundle_id IS ? AND name=?",
                        (context.context_id, parent, name),
                    ).fetchone()
            assert current is not None
            parent = current["bundle_id"]
        result = self._bundle_from_row(
            current, self._bundle_path(context.context_id, current["bundle_id"])
        )
        self._publish_bundle(result)
        return result

    def register_file(
        self,
        execution: ExecutionContext,
        file: str | Path,
        *,
        bundle: str | None = None,
    ) -> SourceRegistrationResult:
        path = Path(file)
        if not path.is_file():
            raise FileNotFoundError(
                f"Source file does not exist or is not a file: {path}"
            )
        context = self.resolve_context(execution)
        target = self.resolve_bundle(execution, bundle or "default")
        self._authorize(
            execution,
            INGEST_SOURCE_CREATE,
            {"context_id": context.context_id, "bundle_id": target.bundle_id},
        )
        media_type = (
            mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        )
        blob_ref = self._blob_store.put_file(
            path,
            context=execution.context,
            media_type=media_type,
        )
        existing = self._source_for_digest(
            context.context_id, target.bundle_id, blob_ref.digest
        )
        if existing:
            return _registration(existing, "already_registered")

        blob_ref_json = _blob_ref_json(blob_ref)
        with self._connection(immediate=True) as db:
            row = db.execute(
                "SELECT * FROM sources WHERE bundle_id=? AND sha256=?",
                (target.bundle_id, blob_ref.digest),
            ).fetchone()
            if row:
                return _registration(
                    self._source_from_row(row), "already_registered"
                )
            source_id, now = f"src-{uuid4().hex}", _now()
            db.execute(
                "INSERT INTO sources("
                "source_id, context_id, bundle_id, original_filename, media_type, "
                "size_bytes, sha256, blob_id, created_by, created_at, blob_ref_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_id,
                    context.context_id,
                    target.bundle_id,
                    path.name,
                    blob_ref.media_type,
                    blob_ref.size_bytes,
                    blob_ref.digest,
                    blob_ref.blob_id,
                    execution.principal_id,
                    now,
                    blob_ref_json,
                ),
            )
            row = db.execute(
                "SELECT * FROM sources WHERE source_id=?", (source_id,)
            ).fetchone()
        source = self._source_from_row(row)
        self._publish_source(source, blob_ref)
        return _registration(source, "created")

    def list_bundles(
        self, execution: ExecutionContext
    ) -> tuple[SourceBundle, ...]:
        context = self.resolve_context(execution)
        self._authorize(
            execution, INGEST_BUNDLE_READ, {"context_id": context.context_id}
        )
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM bundles WHERE context_id=? ORDER BY created_at, name",
                (context.context_id,),
            ).fetchall()
        return tuple(
            self._bundle_from_row(
                row, self._bundle_path(context.context_id, row["bundle_id"])
            )
            for row in rows
        )

    def list_sources(
        self, execution: ExecutionContext, *, bundle: str | None = None
    ) -> tuple[RegisteredSource, ...]:
        context = self.resolve_context(execution)
        query, values = "SELECT * FROM sources WHERE context_id=?", [
            context.context_id
        ]
        resource: dict[str, Any] = {"context_id": context.context_id}
        if bundle is not None:
            target = self.resolve_bundle(execution, bundle, create=False)
            query += " AND bundle_id=?"
            values.append(target.bundle_id)
            resource["bundle_id"] = target.bundle_id
        self._authorize(execution, INGEST_SOURCE_LIST, resource)
        with self._connection() as db:
            return tuple(
                self._source_from_row(row)
                for row in db.execute(
                    query + " ORDER BY created_at", values
                ).fetchall()
            )

    def show_source(
        self, execution: ExecutionContext, source_id: str
    ) -> RegisteredSource:
        context = self.resolve_context(execution)
        self._authorize(
            execution,
            INGEST_SOURCE_READ,
            {"context_id": context.context_id, "source_id": source_id},
        )
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM sources WHERE source_id=? AND context_id=?",
                (source_id, context.context_id),
            ).fetchone()
        if not row:
            raise KeyError(f"Source does not exist in this context: {source_id}")
        return self._source_from_row(row)

    def open(self, execution: ExecutionContext, source_id: str) -> BinaryIO:
        self.show_source(execution, source_id)
        return self._runtime.open_blob(self._blob_ref_for_source(source_id))

    def locate_source(
        self, execution: ExecutionContext, source_id: str
    ) -> SourceLocation:
        context = self.resolve_context(execution)
        self._authorize(
            execution,
            INGEST_SOURCE_LOCATE,
            {"context_id": context.context_id, "source_id": source_id},
        )
        with self._connection() as db:
            source = db.execute(
                "SELECT source_id FROM sources WHERE source_id=? AND context_id=?",
                (source_id, context.context_id),
            ).fetchone()
        if not source:
            raise KeyError(f"Source does not exist in this context: {source_id}")
        blob_ref = self._blob_ref_for_source(source_id)
        local = self._runtime.resolve_blob_local_path(blob_ref)
        profile = self._runtime.config.profiles[blob_ref.profile_name]
        return SourceLocation(
            source_id,
            blob_ref.blob_id,
            blob_ref.uri,
            profile.type,
            str(local) if local else None,
            blob_ref.profile_name,
        )

    def locate_bundle(
        self, execution: ExecutionContext, bundle_id: str
    ) -> dict[str, str | None]:
        context = self.resolve_context(execution)
        self._authorize(
            execution,
            INGEST_BUNDLE_LOCATE,
            {"context_id": context.context_id, "bundle_id": bundle_id},
        )
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM bundles WHERE bundle_id=? AND context_id=?",
                (bundle_id, context.context_id),
            ).fetchone()
        if not row:
            raise KeyError(f"Bundle does not exist in this context: {bundle_id}")
        self._publish_bundle(
            self._bundle_from_row(
                row, self._bundle_path(context.context_id, bundle_id)
            )
        )
        key = f"source-contexts/{context.context_id}/bundles/{bundle_id}"
        local = self._source_store.resolve_local_path(key)
        return {
            "bundle_id": bundle_id,
            "uri": self._source_store.uri(key),
            "backend": self._source_store.backend_name,
            "profile_name": self._source_store.profile_name,
            "local_path": str(local) if local else None,
        }

    def _blob_ref_for_source(self, source_id: str) -> BlobRef:
        with self._connection() as db:
            row = db.execute(
                "SELECT blob_ref_json FROM sources WHERE source_id=?", (source_id,)
            ).fetchone()
        if not row or not row["blob_ref_json"]:
            raise RuntimeError(f"Source {source_id} has no Storage BlobRef.")
        try:
            payload = json.loads(row["blob_ref_json"])
            return BlobRef.from_dict(payload)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Source {source_id} has an invalid Storage BlobRef."
            ) from exc

    def _source_for_digest(
        self, context_id: str, bundle_id: str, digest: str
    ) -> RegisteredSource | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM sources WHERE context_id=? "
                "AND bundle_id=? AND sha256=?",
                (context_id, bundle_id, digest),
            ).fetchone()
        return self._source_from_row(row) if row else None

    def _bundle_path(self, context_id: str, bundle_id: str) -> str:
        with self._connection() as db:
            parts: list[str] = []
            row = db.execute(
                "SELECT * FROM bundles WHERE context_id=? AND bundle_id=?",
                (context_id, bundle_id),
            ).fetchone()
            while row:
                parts.append(row["name"])
                row = (
                    db.execute(
                        "SELECT * FROM bundles WHERE bundle_id=?",
                        (row["parent_bundle_id"],),
                    ).fetchone()
                    if row["parent_bundle_id"]
                    else None
                )
        return "/".join(reversed(parts))

    def _publish_context(self, item: SourceContext) -> None:
        self._source_store.put_json_idempotent(
            f"source-contexts/{item.context_id}/context.json",
            {
                "context_id": item.context_id,
                "context_type": item.context_type,
                "descriptors": item.descriptors,
                "created_at": item.created_at,
            },
        )

    def _publish_bundle(self, item: SourceBundle) -> None:
        self._source_store.put_json_idempotent(
            f"source-contexts/{item.context_id}/bundles/"
            f"{item.bundle_id}/bundle.json",
            {
                "bundle_id": item.bundle_id,
                "context_id": item.context_id,
                "name": item.name,
                "path": item.path,
                "parent_bundle_id": item.parent_bundle_id,
                "created_by": item.created_by,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            },
        )

    def _publish_source(
        self, item: RegisteredSource, blob_ref: BlobRef
    ) -> None:
        self._source_store.put_json_idempotent(
            f"source-contexts/{item.context_id}/bundles/{item.bundle_id}/"
            f"sources/{item.source_id}/source.json",
            {
                "source_id": item.source_id,
                "context_id": item.context_id,
                "bundle_id": item.bundle_id,
                "original_filename": item.original_filename,
                "media_type": item.media_type,
                "size_bytes": blob_ref.size_bytes,
                "sha256": blob_ref.digest,
                "blob_id": blob_ref.blob_id,
                "blob_uri": blob_ref.uri,
                "created_by": item.created_by,
                "created_at": item.created_at,
            },
        )

    def _republish_migrated_source(self, source_id: str) -> None:
        with self._connection() as db:
            source_row = db.execute(
                "SELECT * FROM sources WHERE source_id=?", (source_id,)
            ).fetchone()
            context_row = db.execute(
                "SELECT * FROM contexts WHERE context_id=?",
                (source_row["context_id"],),
            ).fetchone()
            bundle_row = db.execute(
                "SELECT * FROM bundles WHERE bundle_id=?",
                (source_row["bundle_id"],),
            ).fetchone()
        context = SourceContext(
            context_row["context_id"],
            context_row["context_type"],
            json.loads(context_row["descriptors_json"]),
            context_row["created_at"],
        )
        bundle = self._bundle_from_row(
            bundle_row,
            self._bundle_path(context.context_id, bundle_row["bundle_id"]),
        )
        source = self._source_from_row(source_row)
        self._publish_context(context)
        self._publish_bundle(bundle)
        self._publish_source(source, self._blob_ref_for_source(source_id))

    def _authorize(
        self, context: ExecutionContext, action: str, resource: object
    ) -> None:
        decision = self._control.authorize(context, action, resource=resource)
        if not decision.allowed:
            raise IngestAuthorizationError(
                decision.reason or f"Control policy rejected {action}."
            )

    @staticmethod
    def _bundle_from_row(row: sqlite3.Row, path: str) -> SourceBundle:
        return SourceBundle(
            row["bundle_id"],
            row["context_id"],
            row["name"],
            row["parent_bundle_id"],
            path,
            row["created_by"],
            row["created_at"],
            row["updated_at"],
        )

    @staticmethod
    def _source_from_row(row: sqlite3.Row) -> RegisteredSource:
        return RegisteredSource(
            row["source_id"],
            row["context_id"],
            row["bundle_id"],
            row["original_filename"],
            row["media_type"],
            row["size_bytes"],
            row["sha256"],
            row["blob_id"],
            row["created_by"],
            row["created_at"],
        )

    def _initialise_and_migrate(self) -> None:
        with self._connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS contexts (
                    context_id TEXT PRIMARY KEY,
                    context_type TEXT NOT NULL,
                    descriptors_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bundles (
                    bundle_id TEXT PRIMARY KEY,
                    context_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    parent_bundle_id TEXT,
                    created_by TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(context_id,parent_bundle_id,name)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS
                    bundles_context_parent_name_unique
                    ON bundles(context_id,IFNULL(parent_bundle_id,''),name);
                CREATE TABLE IF NOT EXISTS sources (
                    source_id TEXT PRIMARY KEY,
                    context_id TEXT NOT NULL,
                    bundle_id TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    blob_id TEXT NOT NULL,
                    created_by TEXT,
                    created_at TEXT NOT NULL,
                    blob_ref_json TEXT,
                    UNIQUE(bundle_id,sha256)
                );
                """
            )
            columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(sources)").fetchall()
            }
            if "blob_ref_json" not in columns:
                db.execute("ALTER TABLE sources ADD COLUMN blob_ref_json TEXT")
        SourceBlobMigrator(self._runtime, self._catalog_path).migrate()
        with self._connection() as db:
            migrated_or_current = db.execute(
                "SELECT source_id FROM sources WHERE blob_ref_json IS NOT NULL "
                "ORDER BY created_at, source_id"
            ).fetchall()
        for row in migrated_or_current:
            source_id = row["source_id"]
            self._republish_migrated_source(source_id)

    @contextmanager
    def _connection(
        self, *, immediate: bool = False
    ) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self._catalog_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            if immediate:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if immediate:
                db.commit()
        except BaseException:
            if immediate:
                db.rollback()
            raise
        finally:
            db.close()


def _bundle_segments(path: str) -> tuple[str, ...]:
    parts = tuple(
        item.strip() for item in path.strip("/").split("/") if item.strip()
    )
    if not parts or any(item in {".", ".."} for item in parts):
        raise ValueError("Bundle path must contain normal name segments.")
    return parts


def _blob_ref_json(blob_ref: BlobRef) -> str:
    return json.dumps(blob_ref.to_dict(), sort_keys=True, separators=(",", ":"))


def _registration(
    source: RegisteredSource, status: str
) -> SourceRegistrationResult:
    return SourceRegistrationResult(
        source.context_id,
        source.bundle_id,
        source.source_id,
        source.sha256,
        source.size_bytes,
        status,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()
