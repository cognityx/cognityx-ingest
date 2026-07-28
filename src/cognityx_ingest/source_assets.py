"""Canonical SourceAsset and DocBundle registry over Cognityx Storage Runtime."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, BinaryIO, Iterator
from uuid import uuid4
import warnings
from dataclasses import dataclass

from cognityx_storage import BlobRef, PreparedBlob, StorageRuntime
from cognityx_storage.exceptions import (
    ObjectNotFoundError,
    StorageError,
    StorageRoleNotFoundError,
    StorageRoleUnavailableError,
)

from cognityx_ingest.control import (
    ControlClient,
    INGEST_BUNDLE_CREATE,
    INGEST_BUNDLE_DELETE,
    INGEST_BUNDLE_DELETED_LIST,
    INGEST_BUNDLE_LOCATE,
    INGEST_BUNDLE_READ,
    INGEST_SOURCE_CREATE,
    INGEST_SOURCE_DELETE,
    INGEST_SOURCE_DELETED_LIST,
    INGEST_SOURCE_LIST,
    INGEST_SOURCE_LOCATE,
    INGEST_SOURCE_READ,
    IngestAuthorizationError,
    LocalControlClient,
)
from cognityx_ingest.models import (
    DocBundle,
    DocBundleDeletionResult,
    ExecutionContext,
    SourceAsset,
    SourceAssetDeletionResult,
    SourceAssetContext,
    SourceAssetLocation,
    SourceAssetRegistrationResult,
    UsageReport,
)
from cognityx_ingest.source_migration import SourceBlobMigrator

CATALOG_REQUIRED_CAPABILITIES = (
    "native_path",
    "random_write",
    "file_locking",
)


class SourceAssetCatalogError(ValueError):
    """The SourceAsset catalog cannot be selected or opened safely."""


class SourceAssetCatalogAmbiguityError(SourceAssetCatalogError):
    """Legacy and catalog-role databases exist at different paths."""


@dataclass(frozen=True, slots=True)
class _CatalogSelection:
    path: Path
    selection: str
    profile_name: str | None = None
    backend_name: str | None = None


class SourceAssetRegistry:
    """SQLite SourceAsset catalog backed by Storage-owned immutable Blobs."""

    def __init__(
        self,
        storage_runtime: StorageRuntime,
        catalog_path: str | Path,
        *,
        control: ControlClient | None = None,
        _catalog_selection: _CatalogSelection | None = None,
    ) -> None:
        self._runtime = storage_runtime
        self._blob_store = storage_runtime.blobs("source_asset")
        self._source_store = storage_runtime.for_role("source_asset")
        self._catalog_path = Path(catalog_path)
        self._catalog_selection = _catalog_selection
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

    @classmethod
    def load(
        cls,
        *,
        runtime: StorageRuntime | None = None,
        storage_config: str | Path | None = None,
        catalog_path: str | Path | None = None,
        control: ControlClient | None = None,
    ) -> "SourceAssetRegistry":
        """Load a registry using the Storage ``catalog`` role by default."""
        if runtime is not None and storage_config is not None:
            raise ValueError(
                "Pass either runtime or storage_config to SourceAssetRegistry.load(), "
                "not both."
            )
        selected_runtime = runtime or StorageRuntime.load(
            config_file=storage_config
        )
        selection = _resolve_catalog_selection(
            selected_runtime, catalog_path=catalog_path
        )
        return cls(
            selected_runtime,
            selection.path,
            control=control,
            _catalog_selection=selection,
        )

    @property
    def catalog_path(self) -> Path:
        """Return the native SQLite path selected for this registry."""
        return self._catalog_path

    def catalog_info(self) -> dict[str, str | None]:
        """Return non-secret catalog routing diagnostics."""
        selection = getattr(self, "_catalog_selection", None)
        return {
            "catalog_path": str(self._catalog_path),
            "selection": selection.selection if selection else "explicit",
            "catalog_profile": selection.profile_name if selection else None,
            "catalog_backend": selection.backend_name if selection else None,
        }

    def resolve_context(
        self, execution: ExecutionContext
    ) -> SourceAssetContext:
        descriptors = execution.context.descriptors()
        canonical = json.dumps(descriptors, sort_keys=True, separators=(",", ":"))
        context_id = execution.context_id
        deleted_assets: list[sqlite3.Row] = []
        deleted_bundles: list[sqlite3.Row] = []
        with self._connection(immediate=True) as db:
            db.execute(
                "INSERT OR IGNORE INTO contexts"
                "(context_id, context_type, descriptors_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (context_id, execution.context_type, canonical, _now()),
            )
            row = db.execute(
                "SELECT * FROM contexts WHERE context_id=?", (context_id,)
            ).fetchone()
        result = SourceAssetContext(
            row["context_id"],
            row["context_type"],
            json.loads(row["descriptors_json"]),
            row["created_at"],
        )
        self._publish_context(result)
        return result

    def resolve_doc_bundle(
        self,
        execution: ExecutionContext,
        path: str = "default",
        *,
        create: bool = True,
    ) -> DocBundle:
        context, parent = self.resolve_context(execution), None
        restored = False
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
                            "INSERT INTO bundles("
                            "bundle_id, context_id, name, parent_bundle_id, "
                            "created_by, created_at, updated_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
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
                elif current["deleted_at"] is not None:
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
                    db.execute(
                        "UPDATE bundles SET deleted_at=NULL, deleted_by=NULL, "
                        "delete_run_id=NULL, delete_reason=NULL "
                        "WHERE bundle_id=? AND context_id=?",
                        (current["bundle_id"], context.context_id),
                    )
                    current = db.execute(
                        "SELECT * FROM bundles WHERE bundle_id=?",
                        (current["bundle_id"],),
                    ).fetchone()
                    restored = True
            assert current is not None
            parent = current["bundle_id"]
        result = self._bundle_from_row(
            current, self._bundle_path(context.context_id, current["bundle_id"])
        )
        if not restored:
            self._publish_bundle(result)
        if restored:
            self._publish_lifecycle(
                execution,
                result.context_id,
                result.bundle_id,
                None,
                event="restored",
                reason=None,
                blob_id=None,
            )
        return result

    def register_asset(
        self,
        execution: ExecutionContext,
        file: str | Path,
        *,
        bundle: str | None = None,
    ) -> SourceAssetRegistrationResult:
        path = Path(file)
        if not path.is_file():
            raise FileNotFoundError(
                f"Source file does not exist or is not a file: {path}"
            )
        context = self.resolve_context(execution)
        target = self.resolve_doc_bundle(execution, bundle or "default")
        self._authorize(
            execution,
            INGEST_SOURCE_CREATE,
            {"context_id": context.context_id, "bundle_id": target.bundle_id},
        )
        with self._blob_store.prepare_file(path) as prepared:
            existing = self._source_for_digest(
                context.context_id, target.bundle_id, prepared.digest
            )
            if existing:
                if existing.deleted_at is None:
                    return _registration(existing, "already_registered")
                source, blob_ref = self._restore_prepared_source(
                    execution, existing.source_id, prepared
                )
                self._publish_lifecycle(
                    execution,
                    source.context_id,
                    source.bundle_id,
                    source.source_id,
                    event="restored",
                    reason=None,
                    blob_id=source.blob_id,
                    blob_ref=blob_ref,
                )
                return _registration(source, "restored")
            source, blob_ref = self._register_prepared_source(
                execution,
                context,
                target,
                path.name,
                prepared,
            )
        if blob_ref is None:
            return _registration(source, "already_registered")
        self._publish_source(source, blob_ref)
        return _registration(source, "created")

    def _register_prepared_source(
        self,
        execution: ExecutionContext,
        context: SourceAssetContext,
        target: DocBundle,
        original_filename: str,
        prepared: PreparedBlob,
    ) -> tuple[SourceAsset, BlobRef | None]:
        with self._connection(immediate=True) as db:
            row = self._source_row_for_digest(
                db, target.bundle_id, prepared.digest
            )
            if row:
                return self._source_from_row(row), None
            blob_ref = prepared.publish(context=execution.context)
            row = self._insert_source(
                db,
                execution,
                context,
                target,
                original_filename,
                blob_ref,
            )
        return self._source_from_row(row), blob_ref

    def _restore_prepared_source(
        self,
        execution: ExecutionContext,
        source_id: str,
        prepared: PreparedBlob,
    ) -> tuple[SourceAsset, BlobRef]:
        with self._connection(immediate=True) as db:
            blob_ref = prepared.publish(context=execution.context)
            db.execute(
                "UPDATE sources SET media_type=?, size_bytes=?, sha256=?, "
                "blob_id=?, blob_ref_json=?, deleted_at=NULL, deleted_by=NULL, "
                "delete_run_id=NULL, delete_reason=NULL WHERE source_id=? "
                "AND context_id=?",
                (
                    blob_ref.media_type,
                    blob_ref.size_bytes,
                    blob_ref.digest,
                    blob_ref.blob_id,
                    _blob_ref_json(blob_ref),
                    source_id,
                    execution.context_id,
                ),
            )
            row = db.execute(
                "SELECT * FROM sources WHERE source_id=? AND context_id=?",
                (source_id, execution.context_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"SourceAsset does not exist in this context: {source_id}")
        return self._source_from_row(row), blob_ref

    @staticmethod
    def _source_row_for_digest(
        db: sqlite3.Connection, bundle_id: str, digest: str
    ) -> sqlite3.Row | None:
        return db.execute(
            "SELECT * FROM sources WHERE bundle_id=? AND sha256=?",
            (bundle_id, digest),
        ).fetchone()

    @staticmethod
    def _insert_source(
        db: sqlite3.Connection,
        execution: ExecutionContext,
        context: SourceAssetContext,
        target: DocBundle,
        original_filename: str,
        blob_ref: BlobRef,
    ) -> sqlite3.Row:
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
                original_filename,
                blob_ref.media_type,
                blob_ref.size_bytes,
                blob_ref.digest,
                blob_ref.blob_id,
                execution.principal_id,
                now,
                _blob_ref_json(blob_ref),
            ),
        )
        return db.execute(
            "SELECT * FROM sources WHERE source_id=?", (source_id,)
        ).fetchone()

    def list_doc_bundles(
        self, execution: ExecutionContext
    ) -> tuple[DocBundle, ...]:
        context = self.resolve_context(execution)
        self._authorize(
            execution, INGEST_BUNDLE_READ, {"context_id": context.context_id}
        )
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM bundles WHERE context_id=? AND deleted_at IS NULL "
                "ORDER BY created_at, name",
                (context.context_id,),
            ).fetchall()
        return tuple(
            self._bundle_from_row(
                row, self._bundle_path(context.context_id, row["bundle_id"])
            )
            for row in rows
        )

    def list_assets(
        self, execution: ExecutionContext, *, bundle: str | None = None
    ) -> tuple[SourceAsset, ...]:
        context = self.resolve_context(execution)
        query, values = (
            "SELECT * FROM sources WHERE context_id=? AND deleted_at IS NULL",
            [
            context.context_id
            ],
        )
        resource: dict[str, Any] = {"context_id": context.context_id}
        if bundle is not None:
            target = self.resolve_doc_bundle(execution, bundle, create=False)
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

    def show_asset(
        self, execution: ExecutionContext, asset_id: str
    ) -> SourceAsset:
        context = self.resolve_context(execution)
        self._authorize(
            execution,
            INGEST_SOURCE_READ,
            {"context_id": context.context_id, "source_id": asset_id},
        )
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM sources WHERE source_id=? AND context_id=? "
                "AND deleted_at IS NULL",
                (asset_id, context.context_id),
            ).fetchone()
        if not row:
            raise KeyError(
                f"SourceAsset does not exist in this context: {asset_id}"
            )
        return self._source_from_row(row)

    def open_asset(
        self, execution: ExecutionContext, asset_id: str
    ) -> BinaryIO:
        self.show_asset(execution, asset_id)
        return self._runtime.open_blob(self._blob_ref_for_source(asset_id))

    def locate_asset(
        self, execution: ExecutionContext, asset_id: str
    ) -> SourceAssetLocation:
        context = self.resolve_context(execution)
        self._authorize(
            execution,
            INGEST_SOURCE_LOCATE,
            {"context_id": context.context_id, "source_id": asset_id},
        )
        with self._connection() as db:
            source = db.execute(
                "SELECT source_id FROM sources WHERE source_id=? AND context_id=? "
                "AND deleted_at IS NULL",
                (asset_id, context.context_id),
            ).fetchone()
        if not source:
            raise KeyError(
                f"SourceAsset does not exist in this context: {asset_id}"
            )
        blob_ref = self._blob_ref_for_source(asset_id)
        local = self._runtime.resolve_blob_local_path(blob_ref)
        profile = self._runtime.config.profiles[blob_ref.profile_name]
        return SourceAssetLocation(
            asset_id,
            blob_ref.blob_id,
            blob_ref.uri,
            profile.type,
            str(local) if local else None,
            blob_ref.profile_name,
        )

    def locate_doc_bundle(
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
                "SELECT * FROM bundles WHERE bundle_id=? AND context_id=? "
                "AND deleted_at IS NULL",
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

    def delete_asset(
        self,
        execution: ExecutionContext,
        asset_id: str,
        *,
        reason: str | None = None,
    ) -> SourceAssetDeletionResult:
        context = self.resolve_context(execution)
        self._authorize(
            execution,
            INGEST_SOURCE_DELETE,
            {"context_id": context.context_id, "asset_id": asset_id},
        )
        deleted_at = _now()
        with self._connection(immediate=True) as db:
            row = db.execute(
                "SELECT * FROM sources WHERE source_id=? AND context_id=?",
                (asset_id, context.context_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"SourceAsset does not exist in this context: {asset_id}")
            if row["deleted_at"] is None:
                db.execute(
                    "UPDATE sources SET deleted_at=?, deleted_by=?, "
                    "delete_run_id=?, delete_reason=? WHERE source_id=? "
                    "AND context_id=? AND deleted_at IS NULL",
                    (
                        deleted_at,
                        execution.principal_id,
                        execution.run_id,
                        reason,
                        asset_id,
                        context.context_id,
                    ),
                    )
                row = db.execute("SELECT * FROM sources WHERE source_id=? AND context_id=?", (asset_id, context.context_id)).fetchone()
                status = "deleted"
            else:
                deleted_at = row["deleted_at"]
                status = "already_deleted"
        blob_ref = self._blob_ref_for_source(asset_id)
        still_referenced = self._blob_is_live_referenced(
            blob_ref, excluding_asset_id=None
        )
        event_run_id = row["delete_run_id"] or execution.run_id
        event_execution = execution if event_run_id == execution.run_id else ExecutionContext(
            run_id=event_run_id, correlation_id=execution.correlation_id, context=execution.context
        )
        if not self._lifecycle_exists(context.context_id, row["bundle_id"], asset_id, event_run_id, "deleted"):
            self._publish_lifecycle(
                event_execution,
                context.context_id,
                row["bundle_id"],
                asset_id,
                event="deleted",
                reason=row["delete_reason"],
                blob_id=blob_ref.blob_id,
                blob_ref=blob_ref,
            )
        result = SourceAssetDeletionResult(
            context.context_id,
            row["bundle_id"],
            asset_id,
            blob_ref.blob_id,
            deleted_at,
            status,
            still_referenced,
        )
        self._report_usage(
            execution,
            {"assets_deleted": 1 if status == "deleted" else 0},
        )
        return result

    @property
    def control(self) -> ControlClient:
        """Read-only access for coordinating adjacent services."""
        return self._control

    def delete_doc_bundle(
        self,
        execution: ExecutionContext,
        bundle_id: str,
        *,
        recursive: bool = False,
        reason: str | None = None,
    ) -> DocBundleDeletionResult:
        context = self.resolve_context(execution)
        self._authorize(
            execution,
            INGEST_BUNDLE_DELETE,
            {"context_id": context.context_id, "bundle_id": bundle_id},
        )
        repair_assets: list[sqlite3.Row] = []
        repair_bundles: list[sqlite3.Row] = []
        newly_deleted_asset_count = 0
        newly_deleted_bundle_count = 0
        with self._connection(immediate=True) as db:
            target = db.execute(
                "SELECT * FROM bundles WHERE bundle_id=? AND context_id=?",
                (bundle_id, context.context_id),
            ).fetchone()
            if target is None:
                raise KeyError(f"DocBundle does not exist in this context: {bundle_id}")
            descendants = self._bundle_descendants(db, context.context_id, bundle_id)
            bundle_ids = [bundle_id, *(row["bundle_id"] for row in descendants)]
            live_assets = db.execute(
                "SELECT * FROM sources WHERE context_id=? AND deleted_at IS NULL "
                "AND bundle_id IN ({})".format(",".join("?" * len(bundle_ids))),
                [context.context_id, *bundle_ids],
            ).fetchall()
            live_children = [row for row in descendants if row["deleted_at"] is None]
            if not recursive and (live_assets or live_children):
                raise ValueError(
                    "DocBundle is not empty. Use recursive=True to delete contained "
                    "SourceAssets and child DocBundles."
                )
            if target["deleted_at"] is not None and not live_assets and not live_children:
                deleted_at = target["deleted_at"]
                status = "already_deleted"
            else:
                deleted_at = _now()
                db.execute(
                    "UPDATE sources SET deleted_at=?, deleted_by=?, delete_run_id=?, "
                    "delete_reason=? WHERE context_id=? AND deleted_at IS NULL "
                    "AND bundle_id IN ({})".format(",".join("?" * len(bundle_ids))),
                    [deleted_at, execution.principal_id, execution.run_id, reason,
                     context.context_id, *bundle_ids],
                )
                db.execute(
                    "UPDATE bundles SET deleted_at=?, deleted_by=?, delete_run_id=?, "
                    "delete_reason=? WHERE context_id=? "
                    "AND bundle_id IN ({}) AND deleted_at IS NULL".format(",".join("?" * len(bundle_ids))),
                    [deleted_at, execution.principal_id, execution.run_id, reason,
                     context.context_id, *bundle_ids],
                )
                repair_assets = db.execute(
                    "SELECT * FROM sources WHERE context_id=? AND deleted_at=? AND delete_run_id=? AND bundle_id IN ({})".format(",".join("?" * len(bundle_ids))),
                    [context.context_id, deleted_at, execution.run_id, *bundle_ids],
                ).fetchall()
                repair_bundles = db.execute(
                    "SELECT * FROM bundles WHERE context_id=? AND deleted_at=? AND delete_run_id=? AND bundle_id IN ({})".format(",".join("?" * len(bundle_ids))),
                    [context.context_id, deleted_at, execution.run_id, *bundle_ids],
                ).fetchall()
                newly_deleted_asset_count = len(repair_assets)
                newly_deleted_bundle_count = len(repair_bundles)
                status = "deleted"
            if status == "already_deleted":
                repair_assets = db.execute(
                    "SELECT * FROM sources WHERE context_id=? AND delete_run_id=? AND bundle_id IN ({})".format(",".join("?" * len(bundle_ids))),
                    [context.context_id, target["delete_run_id"], *bundle_ids],
                ).fetchall() if target["delete_run_id"] else []
                repair_bundles = db.execute(
                    "SELECT * FROM bundles WHERE context_id=? AND delete_run_id=? AND bundle_id IN ({})".format(",".join("?" * len(bundle_ids))),
                    [context.context_id, target["delete_run_id"], *bundle_ids],
                ).fetchall() if target["delete_run_id"] else []
        for row in repair_assets:
            blob_ref = BlobRef.from_dict(json.loads(row["blob_ref_json"])) if row["blob_ref_json"] else None
            if not self._lifecycle_exists(context.context_id, row["bundle_id"], row["source_id"], row["delete_run_id"], "deleted"):
                self._publish_lifecycle_event(
                    context_id=context.context_id, bundle_id=row["bundle_id"], asset_id=row["source_id"],
                    event="deleted", timestamp=row["deleted_at"], principal_id=row["deleted_by"],
                    run_id=row["delete_run_id"], reason=row["delete_reason"], blob_ref=blob_ref,
                )
        for row in repair_bundles:
            if not self._lifecycle_exists(context.context_id, row["bundle_id"], None, row["delete_run_id"], "deleted"):
                self._publish_lifecycle_event(
                    context_id=context.context_id, bundle_id=row["bundle_id"], asset_id=None,
                    event="deleted", timestamp=row["deleted_at"], principal_id=row["deleted_by"],
                    run_id=row["delete_run_id"], reason=row["delete_reason"], blob_ref=None,
                )
        result = DocBundleDeletionResult(
            context.context_id,
            bundle_id,
            newly_deleted_asset_count,
            newly_deleted_bundle_count,
            deleted_at,
            status,
        )
        self._report_usage(
            execution,
            {
                "assets_deleted": result.deleted_asset_count,
                "bundles_deleted": result.deleted_bundle_count,
            },
        )
        return result

    def list_deleted_assets(
        self, execution: ExecutionContext
    ) -> tuple[SourceAsset, ...]:
        context = self.resolve_context(execution)
        self._authorize(
            execution, INGEST_SOURCE_DELETED_LIST, {"context_id": context.context_id}
        )
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM sources WHERE context_id=? AND deleted_at IS NOT NULL "
                "ORDER BY deleted_at, source_id",
                (context.context_id,),
            ).fetchall()
        return tuple(self._source_from_row(row) for row in rows)

    def list_deleted_doc_bundles(
        self, execution: ExecutionContext
    ) -> tuple[DocBundle, ...]:
        context = self.resolve_context(execution)
        self._authorize(
            execution, INGEST_BUNDLE_DELETED_LIST, {"context_id": context.context_id}
        )
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM bundles WHERE context_id=? AND deleted_at IS NOT NULL "
                "ORDER BY deleted_at, bundle_id",
                (context.context_id,),
            ).fetchall()
        return tuple(
            self._bundle_from_row(row, self._bundle_path(context.context_id, row["bundle_id"]))
            for row in rows
        )

    def list_referenced_blob_refs(
        self, *, include_deleted: bool = False
    ) -> tuple[BlobRef, ...]:
        query = "SELECT blob_ref_json FROM sources"
        if not include_deleted:
            query += " WHERE deleted_at IS NULL"
        with self._connection() as db:
            rows = db.execute(query).fetchall()
        refs: dict[tuple[str, str], BlobRef] = {}
        for row in rows:
            if row["blob_ref_json"]:
                ref = BlobRef.from_dict(json.loads(row["blob_ref_json"]))
                refs.setdefault((ref.profile_name, ref.storage_key), ref)
        return tuple(refs.values())

    @contextmanager
    def catalog_write_lock(self) -> Iterator[None]:
        """Coordinate bounded cleanup batches with SourceAsset registration."""
        with self._connection(immediate=True):
            yield

    # Historical method names delegate to the canonical domain vocabulary.
    def resolve_bundle(
        self,
        execution: ExecutionContext,
        path: str = "default",
        *,
        create: bool = True,
    ) -> DocBundle:
        return self.resolve_doc_bundle(execution, path, create=create)

    def list_bundles(
        self, execution: ExecutionContext
    ) -> tuple[DocBundle, ...]:
        return self.list_doc_bundles(execution)

    def locate_bundle(
        self, execution: ExecutionContext, bundle_id: str
    ) -> dict[str, str | None]:
        return self.locate_doc_bundle(execution, bundle_id)

    def register_file(
        self,
        execution: ExecutionContext,
        file: str | Path,
        *,
        bundle: str | None = None,
    ) -> SourceAssetRegistrationResult:
        return self.register_asset(execution, file, bundle=bundle)

    def list_sources(
        self, execution: ExecutionContext, *, bundle: str | None = None
    ) -> tuple[SourceAsset, ...]:
        return self.list_assets(execution, bundle=bundle)

    def show_source(
        self, execution: ExecutionContext, source_id: str
    ) -> SourceAsset:
        return self.show_asset(execution, source_id)

    def open(self, execution: ExecutionContext, source_id: str) -> BinaryIO:
        return self.open_asset(execution, source_id)

    def locate_source(
        self, execution: ExecutionContext, source_id: str
    ) -> SourceAssetLocation:
        return self.locate_asset(execution, source_id)

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

    @staticmethod
    def _bundle_descendants(
        db: sqlite3.Connection, context_id: str, bundle_id: str
    ) -> list[sqlite3.Row]:
        rows: list[sqlite3.Row] = []
        frontier = [bundle_id]
        while frontier:
            placeholders = ",".join("?" * len(frontier))
            children = db.execute(
                f"SELECT * FROM bundles WHERE context_id=? AND parent_bundle_id "
                f"IN ({placeholders})",
                [context_id, *frontier],
            ).fetchall()
            rows.extend(children)
            frontier = [row["bundle_id"] for row in children]
        return rows

    def _blob_is_live_referenced(
        self, blob_ref: BlobRef, *, excluding_asset_id: str | None
    ) -> bool:
        with self._connection() as db:
            rows = db.execute(
                "SELECT source_id, blob_ref_json FROM sources "
                "WHERE deleted_at IS NULL"
            ).fetchall()
        identity = (blob_ref.profile_name, blob_ref.storage_key)
        for row in rows:
            if excluding_asset_id and row["source_id"] == excluding_asset_id:
                continue
            if row["blob_ref_json"]:
                ref = BlobRef.from_dict(json.loads(row["blob_ref_json"]))
                if (ref.profile_name, ref.storage_key) == identity:
                    return True
        return False

    def _publish_lifecycle(
        self,
        execution: ExecutionContext,
        context_id: str,
        bundle_id: str,
        asset_id: str | None,
        *,
        event: str,
        reason: str | None,
        blob_id: str | None,
        blob_ref: BlobRef | None = None,
    ) -> None:
        self._publish_lifecycle_event(
            context_id=context_id, bundle_id=bundle_id, asset_id=asset_id,
            event=event, timestamp=_now(), principal_id=execution.principal_id,
            run_id=execution.run_id, reason=reason,
            blob_ref=blob_ref,
        )

    def _publish_lifecycle_event(
        self, *, context_id: str, bundle_id: str, asset_id: str | None,
        event: str, timestamp: str, principal_id: str | None, run_id: str,
        reason: str | None, blob_ref: BlobRef | None,
    ) -> None:
        path = (
            f"source-contexts/{context_id}/bundles/{bundle_id}/"
            + (f"sources/{asset_id}/" if asset_id else "")
            + f"lifecycle/{run_id}-{event}.json"
        )
        self._source_store.put_json_idempotent(
            path,
            {
                "event": event,
                "asset_id": asset_id,
                "bundle_id": bundle_id,
                "context_id": context_id,
                "timestamp": timestamp,
                "principal_id": principal_id,
                "run_id": run_id,
                "reason": reason,
                "blob_id": blob_ref.blob_id if blob_ref else None,
                "blob_ref": blob_ref.to_dict() if blob_ref else None,
                "profile_name": blob_ref.profile_name if blob_ref else None,
                "storage_key": blob_ref.storage_key if blob_ref else None,
                "digest": blob_ref.digest if blob_ref else None,
                "size_bytes": blob_ref.size_bytes if blob_ref else None,
                "media_type": blob_ref.media_type if blob_ref else None,
            },
        )

    def _lifecycle_exists(
        self, context_id: str, bundle_id: str, asset_id: str | None,
        run_id: str, event: str,
    ) -> bool:
        path = (
            f"source-contexts/{context_id}/bundles/{bundle_id}/"
            + (f"sources/{asset_id}/" if asset_id else "")
            + f"lifecycle/{run_id}-{event}.json"
        )
        try:
            self._source_store.stat(path)
            return True
        except ObjectNotFoundError:
            return False

    def _report_usage(
        self, execution: ExecutionContext, metrics: dict[str, int]
    ) -> None:
        try:
            self._control.report_usage(
                execution,
                UsageReport(run_id=execution.run_id, metrics=metrics),
            )
        except Exception as exc:
            warnings.warn(
                f"Usage reporting failed after a completed operation: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )

    def _source_for_digest(
        self, context_id: str, bundle_id: str, digest: str
    ) -> SourceAsset | None:
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

    def _publish_context(self, item: SourceAssetContext) -> None:
        self._source_store.put_json_idempotent(
            f"source-contexts/{item.context_id}/context.json",
            {
                "context_id": item.context_id,
                "context_type": item.context_type,
                "descriptors": item.descriptors,
                "created_at": item.created_at,
            },
        )

    def _publish_bundle(self, item: DocBundle) -> None:
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
        self, item: SourceAsset, blob_ref: BlobRef
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
        context = SourceAssetContext(
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
    def _bundle_from_row(row: sqlite3.Row, path: str) -> DocBundle:
        return DocBundle(
            row["bundle_id"],
            row["context_id"],
            row["name"],
            row["parent_bundle_id"],
            path,
            row["created_by"],
            row["created_at"],
            row["updated_at"],
            row["deleted_at"],
            row["deleted_by"],
            row["delete_run_id"],
            row["delete_reason"],
        )

    @staticmethod
    def _source_from_row(row: sqlite3.Row) -> SourceAsset:
        return SourceAsset(
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
            row["deleted_at"],
            row["deleted_by"],
            row["delete_run_id"],
            row["delete_reason"],
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
                    deleted_at TEXT,
                    deleted_by TEXT,
                    delete_run_id TEXT,
                    delete_reason TEXT,
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
                    deleted_at TEXT,
                    deleted_by TEXT,
                    delete_run_id TEXT,
                    delete_reason TEXT,
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
            for column in ("deleted_at", "deleted_by", "delete_run_id", "delete_reason"):
                if column not in columns:
                    db.execute(f"ALTER TABLE sources ADD COLUMN {column} TEXT")
            bundle_columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(bundles)").fetchall()
            }
            for column in ("deleted_at", "deleted_by", "delete_run_id", "delete_reason"):
                if column not in bundle_columns:
                    db.execute(f"ALTER TABLE bundles ADD COLUMN {column} TEXT")
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
    source: SourceAsset, status: str
) -> SourceAssetRegistrationResult:
    return SourceAssetRegistrationResult(
        source.context_id,
        source.bundle_id,
        source.source_id,
        source.sha256,
        source.size_bytes,
        status,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _resolve_catalog_selection(
    runtime: StorageRuntime,
    *,
    catalog_path: str | Path | None,
) -> _CatalogSelection:
    """Resolve the SQLite catalog without coupling it to Blob storage."""
    if catalog_path is not None:
        return _CatalogSelection(Path(catalog_path), "explicit")

    environment_path = os.environ.get("COGNITYX_INGEST_CATALOG")
    if environment_path:
        return _CatalogSelection(Path(environment_path), "environment")
    if "COGNITYX_INGEST_CATALOG" in os.environ:
        raise SourceAssetCatalogError(
            "COGNITYX_INGEST_CATALOG must contain a non-empty catalog path."
        )

    legacy = _legacy_catalog_selection(runtime)
    role_selection = _catalog_role_selection(runtime)
    if legacy is not None:
        if role_selection is not None and _same_path(
            legacy.path, role_selection.path
        ):
            return legacy
        if role_selection is not None and role_selection.path.exists():
            raise SourceAssetCatalogAmbiguityError(
                "Both a legacy SourceAsset catalog and a catalog-role database "
                "exist. Pass catalog_path explicitly to select the authoritative "
                "catalog."
            )
        return legacy
    if role_selection is None:
        raise SourceAssetCatalogError(
            "The SourceAsset catalog requires native filesystem semantics. "
            "Configure the 'catalog' role with a filesystem profile providing "
            "native_path, random_write, and file_locking, or pass catalog_path "
            "explicitly."
        )
    return role_selection


def _legacy_catalog_selection(
    runtime: StorageRuntime,
) -> _CatalogSelection | None:
    """Find the pre-3C catalog only when source assets use a filesystem root."""
    try:
        source_store = runtime.for_role("source_asset")
        profile = runtime.config.profiles[source_store.profile_name]
    except (KeyError, StorageError):
        return None
    if profile.type != "filesystem":
        return None
    root = profile.options.get("root")
    if not isinstance(root, (str, Path)) or not str(root):
        return None
    path = Path(root) / ".cognityx-ingest" / "source_catalog.sqlite3"
    if not path.is_file():
        return None
    return _CatalogSelection(
        path,
        "legacy",
        profile_name=source_store.profile_name,
        backend_name=source_store.backend_name,
    )


def _catalog_role_selection(
    runtime: StorageRuntime,
) -> _CatalogSelection | None:
    try:
        store = runtime.for_role("catalog")
    except (StorageRoleNotFoundError, StorageRoleUnavailableError):
        return None
    except StorageError as exc:
        raise SourceAssetCatalogError(
            "The resolved 'catalog' role could not be opened for the SourceAsset "
            "SQLite catalog. Configure the 'catalog' role with a native "
            "filesystem profile, or pass catalog_path explicitly."
        ) from exc
    missing = store.capabilities.missing(CATALOG_REQUIRED_CAPABILITIES)
    if missing:
        missing_lines = "\n".join(f"- {name}" for name in missing)
        raise SourceAssetCatalogError(
            "The resolved 'catalog' role cannot host the SourceAsset SQLite "
            f"catalog.\n\nResolved profile: {store.profile_name}\n"
            f"Backend: {store.backend_name}\n\n"
            f"Missing required capabilities:\n{missing_lines}\n\n"
            "Configure the 'catalog' role with a native filesystem profile, "
            "or pass catalog_path explicitly."
        )
    try:
        path = store.native_path("ingest/source_catalog.sqlite3")
    except StorageError as exc:
        raise SourceAssetCatalogError(
            "The resolved 'catalog' role reports the required capabilities, "
            "but native_path() failed for the SourceAsset SQLite catalog. "
            "Configure the 'catalog' role with a working native filesystem "
            "profile, or pass catalog_path explicitly."
        ) from exc
    return _CatalogSelection(
        path,
        "catalog_role",
        profile_name=store.profile_name,
        backend_name=store.backend_name,
    )


def _same_path(left: Path, right: Path) -> bool:
    return left.resolve(strict=False) == right.resolve(strict=False)
