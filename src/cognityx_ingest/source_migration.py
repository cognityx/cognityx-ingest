"""Legacy Ingest Blob-catalog conversion to durable Storage BlobRefs."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import BinaryIO, Iterator

from cognityx_resource import ResourceContext
from cognityx_storage import (
    BlobRef,
    LocalStorageBackend,
    StorageClient,
    StorageRuntime,
    hash_stream,
)


class SourceBlobMigrationError(RuntimeError):
    """A legacy Source cannot be converted without losing integrity."""


class SourceBlobMigrator:
    """Restartable one-Source-at-a-time legacy Blob migration."""

    def __init__(
        self, storage_runtime: StorageRuntime, catalog_path: str | Path
    ) -> None:
        self._runtime = storage_runtime
        self._blob_store = storage_runtime.blobs("source_asset")
        self._catalog_path = Path(catalog_path)

    def migrate(self) -> tuple[str, ...]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM sources WHERE blob_ref_json IS NULL "
                "ORDER BY created_at, source_id"
            ).fetchall()
        if not rows:
            return ()
        legacy_storage = self._legacy_storage()
        migrated: list[str] = []
        for source in rows:
            context = self._resource_context(source["context_id"])
            legacy = self._legacy_blob(source["blob_id"])
            with legacy_storage.open(legacy["storage_key"]) as stream:
                self._validate_legacy_stream(source, legacy, stream)
            with legacy_storage.open(legacy["storage_key"]) as stream:
                blob_ref = self._blob_store.put_stream(
                    stream,
                    context=context,
                    media_type=source["media_type"],
                )
            self._validate_blob_ref(source, legacy, blob_ref)
            with self._connection(immediate=True) as db:
                cursor = db.execute(
                    "UPDATE sources SET blob_ref_json=?, blob_id=?, sha256=?, "
                    "size_bytes=? WHERE source_id=? AND blob_ref_json IS NULL",
                    (
                        json.dumps(
                            blob_ref.to_dict(),
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        blob_ref.blob_id,
                        blob_ref.digest,
                        blob_ref.size_bytes,
                        source["source_id"],
                    ),
                )
            if cursor.rowcount:
                migrated.append(source["source_id"])
        return tuple(migrated)

    def _resource_context(self, context_id: str) -> ResourceContext:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM contexts WHERE context_id=?", (context_id,)
            ).fetchone()
        if row is None:
            raise SourceBlobMigrationError(
                f"Legacy Source references missing Context: {context_id}"
            )
        try:
            descriptors = json.loads(row["descriptors_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise SourceBlobMigrationError(
                f"Context {context_id} has invalid descriptors JSON."
            ) from exc
        if not isinstance(descriptors, dict):
            raise SourceBlobMigrationError(
                f"Context {context_id} descriptors must be an object."
            )
        stored_type = row["context_type"]
        descriptor_type = descriptors.pop("context_type", stored_type)
        if descriptor_type != stored_type:
            raise SourceBlobMigrationError(
                f"Context {context_id} has conflicting context_type descriptors."
            )
        standard = {
            key: descriptors.pop(key, None)
            for key in (
                "principal_id",
                "tenant_id",
                "project_id",
                "workspace_id",
            )
        }
        try:
            context = ResourceContext(
                context_type=stored_type,
                scopes=descriptors,
                **standard,
            )
        except (TypeError, ValueError) as exc:
            raise SourceBlobMigrationError(
                f"Context {context_id} cannot be reconstructed: {exc}"
            ) from exc
        if context.context_id != context_id:
            raise SourceBlobMigrationError(
                f"Context consistency error: stored ID {context_id} does not "
                f"match reconstructed ID {context.context_id}."
            )
        return context

    def _legacy_blob(self, blob_id: str) -> sqlite3.Row:
        with self._connection() as db:
            for table in ("blobs", "blobs_legacy"):
                if not self._table_exists(db, table):
                    continue
                row = db.execute(
                    f"SELECT * FROM {table} WHERE blob_id=?", (blob_id,)
                ).fetchone()
                if row is not None:
                    return row
        raise SourceBlobMigrationError(
            f"Legacy Blob metadata does not exist for Blob ID: {blob_id}"
        )

    def _legacy_storage(self) -> StorageClient:
        store = self._runtime.for_role("source_asset")
        profile = self._runtime.config.profiles[store.profile_name]
        if profile.type != "filesystem":
            raise SourceBlobMigrationError(
                "Automatic legacy Ingest Blob migration currently requires the "
                "recorded source_asset profile to be filesystem storage."
            )
        root = profile.options.get("root")
        if not isinstance(root, str) or not root:
            raise SourceBlobMigrationError(
                f"Filesystem profile '{profile.name}' has no usable root."
            )
        return StorageClient(LocalStorageBackend(root)).for_shared_data()

    @staticmethod
    def _validate_legacy_stream(
        source: sqlite3.Row, legacy: sqlite3.Row, stream: BinaryIO
    ) -> None:
        digest, size = hash_stream(stream)
        expected_digest = legacy["sha256"]
        expected_size = legacy["size_bytes"]
        if digest != expected_digest or size != expected_size:
            raise SourceBlobMigrationError(
                f"Legacy Blob consistency error for Source {source['source_id']}: "
                f"catalog has sha256={expected_digest}, size={expected_size}; "
                f"bytes have sha256={digest}, size={size}."
            )
        if source["sha256"] != digest or source["size_bytes"] != size:
            raise SourceBlobMigrationError(
                f"Legacy Source consistency error for Source {source['source_id']}."
            )

    @staticmethod
    def _validate_blob_ref(
        source: sqlite3.Row, legacy: sqlite3.Row, blob_ref: BlobRef
    ) -> None:
        if (
            blob_ref.digest != source["sha256"]
            or blob_ref.digest != legacy["sha256"]
            or blob_ref.size_bytes != source["size_bytes"]
            or blob_ref.size_bytes != legacy["size_bytes"]
        ):
            raise SourceBlobMigrationError(
                f"Storage BlobRef consistency error for Source "
                f"{source['source_id']}."
            )

    @staticmethod
    def _table_exists(db: sqlite3.Connection, table: str) -> bool:
        return (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            is not None
        )

    @contextmanager
    def _connection(
        self, *, immediate: bool = False
    ) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self._catalog_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
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
