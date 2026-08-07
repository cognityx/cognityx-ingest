"""Persist SourceAsset lifecycle and additive extraction-policy metadata.

Purpose
-------
The registry gives Ingest one durable SQLite coordination point for logical
contexts, bundles, source files, and T07 parser-extraction retention records.

Design principles and processing flow
-------------------------------------
Existing SourceAsset tables and behavior remain authoritative. T07 adds normalized
metadata tables only: one immutable extraction row, deduplicated active reference
rows, and append-only changed-state events. Context-scoped operations authorize first. Registrations,
reference changes, legal hold, retention expiry, reuse acquisition, and purge
finalization use explicit transactions; race-sensitive writes use
``BEGIN IMMEDIATE``. Returned records contain hashes and logical Storage keys,
never parser payload bytes or copied canonical text.

Primary consumers and ownership boundary
----------------------------------------
Source lifecycle services use the original APIs. ``ExtractionRetentionService``
uses the additive T07 APIs. Ingest owns logical metadata and policy decisions;
Cognityx Storage owns physical objects and deletion. T01 owns native descriptor
integrity, T06 owns segmentation views, and T08 owns the future Source Graph.

Non-goals
---------
This module does not parse documents, execute routing or fusion, inspect parser
payload meaning, physically delete Storage objects, modify canonical evidence,
expose SDK/CLI controls, or implement T08-T10. Connections are operation-local,
immutable return values are safe to share, and SQLite serializes conflicting
writers.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, BinaryIO, Iterator
from uuid import uuid4
import warnings

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
    INGEST_SOURCE_BATCH_CREATE,
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
    ExtractionIdentity,
    _ExtractionPayloadAbsenceProof,
    ExtractionPurgeBlockedError,
    ExtractionPurgeCandidate,
    ExtractionPurgeFinalizationError,
    ExtractionRetentionConflictError,
    ExtractionRetentionError,
    ExtractionRetentionEvent,
    ExtractionRetentionEventType,
    ExtractionRetentionRecord,
    ExtractionRetentionReferenceError,
    ExtractionRetentionState,
    RetentionTombstone,
    SourceAsset,
    SourceAssetBatchItem,
    SourceAssetBatchResult,
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

_EXTRACTION_REGISTER = "ingest.extraction.retention.register"
_EXTRACTION_READ = "ingest.extraction.retention.read"
_EXTRACTION_REFERENCE_WRITE = "ingest.extraction.reference.write"
_EXTRACTION_HOLD_WRITE = "ingest.extraction.legal_hold.write"
_EXTRACTION_EXPIRE = "ingest.extraction.retention.expire"
_EXTRACTION_PURGE_PLAN = "ingest.extraction.purge.plan"
_EXTRACTION_PURGE_FINALIZE = "ingest.extraction.purge.finalize"
_RETENTION_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


class SourceAssetCatalogError(ValueError):
    """The SourceAsset catalog cannot be selected or opened safely."""


class SourceAssetCatalogAmbiguityError(SourceAssetCatalogError):
    """Legacy and catalog-role databases exist at different paths."""


class SourceAssetBatchCancelled(RuntimeError):
    """Directory registration stopped safely after preserving completed work."""

    def __init__(
        self,
        *,
        result: SourceAssetBatchResult | None,
        files_discovered: int,
        files_processed: int,
    ) -> None:
        super().__init__(
            "SourceAsset batch registration was cancelled after "
            f"{files_processed} of {files_discovered} discovered files."
        )
        self.result = result
        self.files_discovered = files_discovered
        self.files_processed = files_processed


@dataclass(frozen=True, slots=True)
class _CatalogSelection:
    path: Path
    selection: str
    profile_name: str | None = None
    backend_name: str | None = None


@dataclass(frozen=True, slots=True)
class _DiscoveredFile:
    path: Path
    relative_path: str


class SourceAssetRegistry:
    """Coordinate SourceAsset and extraction-policy metadata in one SQLite catalog.

    Application composition constructs this repository from ``StorageRuntime``;
    source lifecycle APIs, cleanup coordination, and T07 retention services use it.
    Existing tables continue to coordinate immutable Storage-owned SourceAsset
    blobs. Additive T07 tables record exact extraction identity, state, legal hold,
    references, and tombstones without storing parser payload bytes or canonical
    text. Mutations authorize against the supplied execution context and use
    ``BEGIN IMMEDIATE`` where reuse or purge can race. SQLite connections are local
    per operation, so immutable returned records are thread-safe and database locks
    serialize conflicting writers. Public retention methods translate database
    conflicts into bounded typed domain failures and never physically delete a
    Storage object.
    """

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

    def register_path(
        self,
        execution: ExecutionContext,
        path: str | Path,
        *,
        bundle: str | None = None,
        structure: str = "preserve",
        recursive: bool = True,
        progress: Callable[[dict[str, Any]], None] | None = None,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> SourceAssetRegistrationResult | SourceAssetBatchResult:
        """Register one file or a deterministic directory tree synchronously."""
        if structure not in {"preserve", "flat"}:
            raise ValueError("structure must be either 'preserve' or 'flat'.")
        selected = Path(path)
        if selected.is_file():
            return self.register_asset(execution, selected, bundle=bundle)
        if not selected.exists() or not selected.is_dir() or selected.is_symlink():
            raise FileNotFoundError(
                "Source path does not exist or is not a regular file or directory."
            )

        root_bundle_path = _normalise_generated_bundle_path(
            bundle if bundle is not None else selected.resolve().name
        )
        context = self.resolve_context(execution)
        self._authorize(
            execution,
            INGEST_SOURCE_BATCH_CREATE,
            {
                "context_id": context.context_id,
                "root_bundle": root_bundle_path,
                "structure": structure,
                "recursive": recursive,
            },
        )
        if cancellation_requested and cancellation_requested():
            raise SourceAssetBatchCancelled(
                result=None, files_discovered=0, files_processed=0
            )

        emit = progress or (lambda payload: None)
        emit({"event": "scan_started", "files_discovered": 0})
        discovered, skipped = self._scan_source_tree(
            selected,
            recursive=recursive,
            root_bundle_path=root_bundle_path,
            structure=structure,
            emit=emit,
        )
        emit(
            {
                "event": "scan_completed",
                "files_discovered": len(discovered),
                "entries_skipped": len(skipped),
            }
        )

        if cancellation_requested and cancellation_requested():
            raise SourceAssetBatchCancelled(
                result=None,
                files_discovered=len(discovered),
                files_processed=0,
            )
        root_bundle = self.resolve_doc_bundle(
            execution, root_bundle_path, create=True
        )
        batch_id = f"batch-{uuid4().hex}"
        items: list[SourceAssetBatchItem] = list(skipped)
        created = restored = already_registered = failed = input_bytes = 0

        for index, source in enumerate(discovered, start=1):
            if cancellation_requested and cancellation_requested():
                result = _batch_result(
                    batch_id=batch_id,
                    context_id=context.context_id,
                    root_bundle_id=root_bundle.bundle_id,
                    root_bundle_path=root_bundle.path,
                    structure=structure,
                    recursive=recursive,
                    files_discovered=len(discovered),
                    created=created,
                    restored=restored,
                    already_registered=already_registered,
                    failed=failed,
                    skipped=len(skipped),
                    items=items,
                )
                raise SourceAssetBatchCancelled(
                    result=result,
                    files_discovered=len(discovered),
                    files_processed=result.files_processed,
                )
            bundle_path = _bundle_for_relative_path(
                root_bundle.path, source.relative_path, structure
            )
            emit(
                {
                    "event": "file_started",
                    "relative_path": source.relative_path,
                    "bundle_path": bundle_path,
                    "file_index": index,
                    "files_discovered": len(discovered),
                }
            )
            try:
                result = self.register_asset(
                    execution, source.path, bundle=bundle_path
                )
            except Exception as exc:
                failed += 1
                item = SourceAssetBatchItem(
                    relative_path=source.relative_path,
                    bundle_path=bundle_path,
                    asset_id=None,
                    status="failed",
                    error_category=type(exc).__name__,
                    error_message=(
                        f"{type(exc).__name__} while registering "
                        f"{source.relative_path}."
                    ),
                )
                items.append(item)
                emit(
                    {
                        "event": "file_failed",
                        "relative_path": item.relative_path,
                        "bundle_path": item.bundle_path,
                        "error_category": item.error_category,
                        "files_processed": created
                        + restored
                        + already_registered
                        + failed,
                        "files_discovered": len(discovered),
                    }
                )
                continue

            if result.status == "created":
                created += 1
            elif result.status == "restored":
                restored += 1
            elif result.status == "already_registered":
                already_registered += 1
            else:
                raise RuntimeError(
                    f"Unexpected SourceAsset registration status: {result.status}"
                )
            input_bytes += result.size_bytes
            item = SourceAssetBatchItem(
                relative_path=source.relative_path,
                bundle_path=bundle_path,
                asset_id=result.asset_id,
                status=result.status,
            )
            items.append(item)
            emit(
                {
                    "event": "file_completed",
                    "relative_path": item.relative_path,
                    "bundle_path": item.bundle_path,
                    "asset_id": item.asset_id,
                    "status": item.status,
                    "files_processed": created
                    + restored
                    + already_registered
                    + failed,
                    "files_discovered": len(discovered),
                }
            )

        result = _batch_result(
            batch_id=batch_id,
            context_id=context.context_id,
            root_bundle_id=root_bundle.bundle_id,
            root_bundle_path=root_bundle.path,
            structure=structure,
            recursive=recursive,
            files_discovered=len(discovered),
            created=created,
            restored=restored,
            already_registered=already_registered,
            failed=failed,
            skipped=len(skipped),
            items=items,
        )
        self._report_usage(
            execution,
            {
                "files_discovered": result.files_discovered,
                "files_processed": result.files_processed,
                "assets_created": result.created_count,
                "assets_restored": result.restored_count,
                "assets_already_registered": result.already_registered_count,
                "assets_failed": result.failed_count,
                "entries_skipped": result.skipped_count,
                "input_bytes": input_bytes,
            },
        )
        emit(
            {
                "event": "batch_completed",
                "batch_id": result.batch_id,
                "files_discovered": result.files_discovered,
                "files_processed": result.files_processed,
                "failed_count": result.failed_count,
                "skipped_count": result.skipped_count,
            }
        )
        return result

    def _scan_source_tree(
        self,
        root: Path,
        *,
        recursive: bool,
        root_bundle_path: str,
        structure: str,
        emit: Callable[[dict[str, Any]], None],
    ) -> tuple[list[_DiscoveredFile], list[SourceAssetBatchItem]]:
        discovered: list[_DiscoveredFile] = []
        skipped: list[SourceAssetBatchItem] = []
        excluded_roots = self._local_storage_roots()
        catalog = self._catalog_path.resolve(strict=False)

        def skip(path: Path, relative_path: str, reason: str) -> None:
            bundle_path = _bundle_for_relative_path(
                root_bundle_path, relative_path, structure
            )
            skipped.append(
                SourceAssetBatchItem(
                    relative_path=relative_path,
                    bundle_path=bundle_path,
                    asset_id=None,
                    status="skipped",
                    error_category=reason,
                    error_message=None,
                )
            )
            emit(
                {
                    "event": "entry_skipped",
                    "relative_path": relative_path,
                    "bundle_path": bundle_path,
                    "reason": reason,
                }
            )

        def visit(directory: Path, prefix: Path) -> None:
            try:
                entries = sorted(os.scandir(directory), key=lambda item: item.name)
            except OSError as exc:
                relative = prefix.as_posix() if prefix.parts else directory.name
                skip(directory, relative, type(exc).__name__)
                return
            for entry in entries:
                entry_path = Path(entry.path)
                relative = (prefix / entry.name).as_posix()
                try:
                    if entry.is_symlink():
                        skip(entry_path, relative, "symlink")
                    elif entry.is_file(follow_symlinks=False):
                        resolved = entry_path.resolve(strict=False)
                        if _is_catalog_file(resolved, catalog):
                            skip(entry_path, relative, "cognityx_catalog")
                        else:
                            discovered.append(
                                _DiscoveredFile(entry_path, relative)
                            )
                    elif entry.is_dir(follow_symlinks=False):
                        resolved = entry_path.resolve(strict=False)
                        if resolved in excluded_roots:
                            skip(entry_path, relative, "cognityx_storage_root")
                        elif recursive:
                            visit(entry_path, prefix / entry.name)
                    else:
                        skip(entry_path, relative, "special_entry")
                except OSError as exc:
                    skip(entry_path, relative, type(exc).__name__)

        visit(root, Path())
        discovered.sort(key=lambda item: item.relative_path)
        skipped.sort(key=lambda item: item.relative_path)
        return discovered, skipped

    def _local_storage_roots(self) -> set[Path]:
        roots: set[Path] = set()
        for profile in self._runtime.config.profiles.values():
            if profile.type != "filesystem":
                continue
            root = profile.options.get("root")
            if isinstance(root, str) and root.strip():
                roots.add(Path(root).expanduser().resolve(strict=False))
        return roots

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

    def register_extraction_record(
        self,
        execution: ExecutionContext,
        record: ExtractionRetentionRecord,
    ) -> ExtractionRetentionRecord:
        """Register one immutable validated extraction and initial references.

        ``ExtractionRetentionService`` calls this after verifying a complete
        identity against a T01 descriptor. The algorithm resolves and authorizes
        the execution context, then uses one immediate transaction to insert the
        immutable record, initial references, and one registered event. An
        equivalent retry returns the live record without replaying references or
        changing lifecycle, audit fields, or event history. Changed immutable
        facts, a duplicate live identity, or a purged
        artifact ID raises ``ExtractionRetentionConflictError``. No payload bytes
        enter SQLite, no Storage operation occurs, and concurrent registrations are
        serialized by SQLite's write lock.
        """
        if not isinstance(record, ExtractionRetentionRecord):
            raise ExtractionRetentionConflictError(
                "record must be an ExtractionRetentionRecord"
            )
        if record.state is not ExtractionRetentionState.VALIDATED:
            raise ExtractionRetentionConflictError(
                "new extraction records must start validated"
            )
        context = self.resolve_context(execution)
        if record.context_id != context.context_id:
            raise ExtractionRetentionConflictError(
                "extraction record context does not match execution context"
            )
        self._authorize(
            execution,
            _EXTRACTION_REGISTER,
            {"context_id": context.context_id, "artifact_id": record.artifact_id},
        )
        try:
            with self._connection(immediate=True) as db:
                row = self._retention_row(
                    db, context.context_id, record.artifact_id
                )
                if row is None:
                    db.execute(
                        "INSERT INTO extraction_retention_records("
                        "context_id,artifact_id,source_sha256,parser_id,"
                        "parser_version,parser_configuration_hash,model_version,"
                        "scope,extraction_identity,artifact_sha256,"
                        "artifact_storage_key,artifact_media_type,state,legal_hold,"
                        "created_at,updated_at,updated_by,updated_run_id,tombstone_json"
                        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                        (
                            record.context_id,
                            record.artifact_id,
                            record.identity.source_sha256,
                            record.identity.parser_id,
                            record.identity.parser_version,
                            record.identity.parser_configuration_hash,
                            record.identity.model_version,
                            record.identity.scope,
                            record.extraction_identity,
                            record.artifact_sha256,
                            record.artifact_storage_key,
                            record.artifact_media_type,
                            record.state.value,
                            int(record.legal_hold),
                            record.created_at,
                            record.updated_at,
                            record.updated_by,
                            record.updated_run_id,
                        ),
                    )
                    for reference_id in record.reference_ids:
                        db.execute(
                            "INSERT INTO extraction_references("
                            "context_id,artifact_id,reference_id,created_at,created_by,"
                            "created_run_id) VALUES (?,?,?,?,?,?)",
                            (
                                context.context_id,
                                record.artifact_id,
                                reference_id,
                                record.updated_at,
                                execution.principal_id,
                                execution.run_id,
                            ),
                        )
                    self._append_retention_event(
                        db,
                        execution,
                        record.artifact_id,
                        ExtractionRetentionEventType.REGISTERED,
                        timestamp=record.created_at,
                    )
                else:
                    current = self._retention_record_from_row(db, row)
                    if current.state is ExtractionRetentionState.PURGED:
                        raise ExtractionRetentionConflictError(
                            "a purged artifact record cannot be resurrected"
                        )
                    if _immutable_retention_facts(current) != _immutable_retention_facts(
                        record
                    ):
                        raise ExtractionRetentionConflictError(
                            "artifact ID conflicts with immutable extraction metadata"
                        )
                    return current
                stored = self._retention_record_from_row(
                    db,
                    self._required_retention_row(
                        db, context.context_id, record.artifact_id
                    ),
                )
        except ExtractionRetentionError:
            raise
        except sqlite3.IntegrityError as error:
            raise ExtractionRetentionConflictError(
                "a live extraction already owns this exact identity"
            ) from error
        except sqlite3.DatabaseError as error:
            raise ExtractionRetentionError(
                "could not register extraction retention metadata"
            ) from error
        return stored

    def get_extraction_record(
        self, execution: ExecutionContext, artifact_id: str
    ) -> ExtractionRetentionRecord:
        """Read one context-scoped retention record and its active references.

        Retention services and audit callers use this metadata-only operation. It
        authorizes the current context, loads the record plus sorted reference rows,
        and reconstructs immutable validated values. It performs no Storage read or
        write. Missing or cross-context IDs raise
        ``ExtractionRetentionReferenceError`` without disclosing another context.
        """
        context = self.resolve_context(execution)
        self._authorize(
            execution,
            _EXTRACTION_READ,
            {"context_id": context.context_id, "artifact_id": artifact_id},
        )
        try:
            with self._connection() as db:
                row = self._required_retention_row(
                    db, context.context_id, artifact_id
                )
                return self._retention_record_from_row(db, row)
        except ExtractionRetentionError:
            raise
        except sqlite3.DatabaseError as error:
            raise ExtractionRetentionError(
                "could not read extraction retention metadata"
            ) from error

    def find_reusable_extraction(
        self, execution: ExecutionContext, identity: ExtractionIdentity
    ) -> ExtractionRetentionRecord | None:
        """Return only an exact validated extraction identity from this context.

        Exact-reuse composition calls this read-only lookup. It compares the
        canonical six-field digest and requires ``validated`` state with no
        tombstone; source-only, parser-only, expired, and purged matches are never
        returned. Payload integrity remains the retention service's T01 boundary.
        SQLite failures become typed errors and no parser or Storage call occurs.
        """
        if not isinstance(identity, ExtractionIdentity):
            raise ExtractionRetentionReferenceError(
                "complete ExtractionIdentity is required for reuse"
            )
        context = self.resolve_context(execution)
        self._authorize(
            execution,
            _EXTRACTION_READ,
            {"context_id": context.context_id, "extraction_identity": identity.digest},
        )
        try:
            with self._connection() as db:
                row = db.execute(
                    "SELECT * FROM extraction_retention_records "
                    "WHERE context_id=? AND extraction_identity=? AND state=? "
                    "AND tombstone_json IS NULL",
                    (
                        context.context_id,
                        identity.digest,
                        ExtractionRetentionState.VALIDATED.value,
                    ),
                ).fetchone()
                return self._retention_record_from_row(db, row) if row else None
        except sqlite3.DatabaseError as error:
            raise ExtractionRetentionError(
                "could not look up reusable extraction metadata"
            ) from error

    def acquire_reusable_extraction(
        self,
        execution: ExecutionContext,
        identity: ExtractionIdentity,
        reference_id: str,
    ) -> tuple[ExtractionRetentionRecord | None, str | None]:
        """Atomically protect an exact reusable record before integrity checking.

        ``ExtractionRetentionService.acquire_reusable`` calls this race boundary.
        One immediate transaction locates the exact validated identity and inserts
        the stable active reference before a purge planner can see it unreferenced.
        The second return value is a private acquisition timestamp only when this
        call inserted the row; it lets failed integrity verification remove exactly
        that acquisition. Exact misses return ``(None, None)``. No parser, payload,
        network, or deletion operation occurs.
        """
        if not isinstance(identity, ExtractionIdentity):
            raise ExtractionRetentionReferenceError(
                "complete ExtractionIdentity is required for reuse"
            )
        reference_id = _retention_identifier(reference_id, "reference_id")
        context = self.resolve_context(execution)
        self._authorize(
            execution,
            _EXTRACTION_REFERENCE_WRITE,
            {"context_id": context.context_id, "reference_id": reference_id},
        )
        try:
            with self._connection(immediate=True) as db:
                row = db.execute(
                    "SELECT * FROM extraction_retention_records "
                    "WHERE context_id=? AND extraction_identity=? AND state=? "
                    "AND tombstone_json IS NULL",
                    (
                        context.context_id,
                        identity.digest,
                        ExtractionRetentionState.VALIDATED.value,
                    ),
                ).fetchone()
                if row is None:
                    return None, None
                acquired_at = _now()
                cursor = db.execute(
                    "INSERT OR IGNORE INTO extraction_references("
                    "context_id,artifact_id,reference_id,created_at,created_by,"
                    "created_run_id) VALUES (?,?,?,?,?,?)",
                    (
                        context.context_id,
                        row["artifact_id"],
                        reference_id,
                        acquired_at,
                        execution.principal_id,
                        execution.run_id,
                    ),
                )
                token = acquired_at if cursor.rowcount == 1 else None
                if token is not None:
                    self._touch_retention_row(db, execution, row["artifact_id"])
                    self._append_retention_event(
                        db,
                        execution,
                        row["artifact_id"],
                        ExtractionRetentionEventType.REFERENCE_ADDED,
                        reference_id=reference_id,
                        timestamp=acquired_at,
                    )
                    row = self._required_retention_row(
                        db, context.context_id, row["artifact_id"]
                    )
                return self._retention_record_from_row(db, row), token
        except ExtractionRetentionError:
            raise
        except sqlite3.DatabaseError as error:
            raise ExtractionRetentionError(
                "could not acquire reusable extraction metadata"
            ) from error

    def add_extraction_reference(
        self,
        execution: ExecutionContext,
        artifact_id: str,
        reference_id: str,
    ) -> ExtractionRetentionRecord:
        """Add one explicit payload-requiring reference idempotently.

        Canonical binding, validated T06 view, and downstream lifecycle code call
        this operation after deciding that a consumer requires the native payload.
        An immediate transaction rejects purged artifacts, inserts one deduplicated
        reference, and returns canonical sorted state. It never infers or removes
        references and performs no Storage or parser operation.
        """
        return self._change_extraction_reference(
            execution, artifact_id, reference_id, add=True
        )

    def remove_extraction_reference(
        self,
        execution: ExecutionContext,
        artifact_id: str,
        reference_id: str,
    ) -> ExtractionRetentionRecord:
        """Remove one explicit active reference idempotently and context-safely.

        The owning consumer lifecycle calls this only when it no longer requires
        raw parser bytes. An immediate transaction removes the named row and leaves
        every other reference untouched. It never derives removal from compact
        lineage, performs no physical deletion, and rejects purged artifacts.
        """
        return self._change_extraction_reference(
            execution, artifact_id, reference_id, add=False
        )

    def set_extraction_legal_hold(
        self,
        execution: ExecutionContext,
        artifact_id: str,
        *,
        enabled: bool,
    ) -> ExtractionRetentionRecord:
        """Enable or release durable legal hold as an idempotent policy operation.

        Authorized governance composition calls this independently of retention
        expiry. One immediate transaction rejects purged records, writes the exact
        requested boolean only when changed, and updates audit identity. Hold does
        not block read/reuse, but current derived purge eligibility changes
        immediately. No payload or canonical record is read or altered.
        """
        if not isinstance(enabled, bool):
            raise ExtractionRetentionConflictError("legal hold value must be boolean")

        def set_hold(
            db: sqlite3.Connection, row: sqlite3.Row
        ) -> ExtractionRetentionEventType | None:
            """Write legal hold and name its event only when the value differs."""
            if _stored_legal_hold(row["legal_hold"]) == enabled:
                return None
            db.execute(
                "UPDATE extraction_retention_records SET legal_hold=? "
                "WHERE context_id=? AND artifact_id=?",
                (int(enabled), row["context_id"], row["artifact_id"]),
            )
            return (
                ExtractionRetentionEventType.LEGAL_HOLD_ENABLED
                if enabled
                else ExtractionRetentionEventType.LEGAL_HOLD_RELEASED
            )

        return self._update_retention_policy(
            execution,
            artifact_id,
            action=_EXTRACTION_HOLD_WRITE,
            operation="legal hold",
            update=set_hold,
        )

    def mark_extraction_retention_expired(
        self,
        execution: ExecutionContext,
        artifact_id: str,
    ) -> ExtractionRetentionRecord:
        """Advance validated metadata to retention-expired without deleting bytes.

        Retention policy orchestration calls this explicit one-way transition.
        ``validated`` advances, ``retention-expired`` is an idempotent retry, and
        ``purged`` cannot be resurrected. References and legal hold remain intact
        and continue to block purge. The transaction performs metadata writes only.
        """

        def expire(
            db: sqlite3.Connection, row: sqlite3.Row
        ) -> ExtractionRetentionEventType | None:
            """Apply only the allowed first state transition inside the caller lock."""
            if row["state"] == ExtractionRetentionState.VALIDATED.value:
                db.execute(
                    "UPDATE extraction_retention_records SET state=? "
                    "WHERE context_id=? AND artifact_id=?",
                    (
                        ExtractionRetentionState.RETENTION_EXPIRED.value,
                        row["context_id"],
                        row["artifact_id"],
                    ),
                )
                return ExtractionRetentionEventType.RETENTION_EXPIRED
            return None

        return self._update_retention_policy(
            execution,
            artifact_id,
            action=_EXTRACTION_EXPIRE,
            operation="retention expiry",
            update=expire,
        )

    def list_extraction_records(
        self, execution: ExecutionContext
    ) -> tuple[ExtractionRetentionRecord, ...]:
        """List current context retention metadata in deterministic artifact order.

        Audit and purge planning call this authorized read. It loads references and
        tombstones into immutable records without reading payloads or revealing
        other contexts. Repeated calls over unchanged metadata are identical.
        """
        context = self.resolve_context(execution)
        self._authorize(
            execution,
            _EXTRACTION_READ,
            {"context_id": context.context_id},
        )
        try:
            with self._connection() as db:
                rows = db.execute(
                    "SELECT * FROM extraction_retention_records WHERE context_id=? "
                    "ORDER BY artifact_id",
                    (context.context_id,),
                ).fetchall()
                return tuple(self._retention_record_from_row(db, row) for row in rows)
        except sqlite3.DatabaseError as error:
            raise ExtractionRetentionError(
                "could not list extraction retention metadata"
            ) from error

    def list_extraction_retention_events(
        self,
        execution: ExecutionContext,
        *,
        artifact_id: str | None = None,
    ) -> tuple[ExtractionRetentionEvent, ...]:
        """List append-only lifecycle facts in database sequence order.

        Audit and operations callers use this context-scoped read to explain how
        one extraction reached its current state. An optional artifact ID narrows
        the history without revealing records in another context. The algorithm
        authorizes once, selects immutable payload-free rows by increasing event
        sequence, and validates each row through the public event model. It does
        not infer events from mutable current state, perform Storage I/O, or alter
        history; malformed catalog rows raise a bounded T07 error.
        """
        context = self.resolve_context(execution)
        if artifact_id is not None:
            artifact_id = _retention_identifier(artifact_id, "artifact_id")
        self._authorize(
            execution,
            _EXTRACTION_READ,
            {
                "context_id": context.context_id,
                **({"artifact_id": artifact_id} if artifact_id is not None else {}),
            },
        )
        try:
            with self._connection() as db:
                if artifact_id is None:
                    rows = db.execute(
                        "SELECT * FROM extraction_retention_events "
                        "WHERE context_id=? ORDER BY event_sequence",
                        (context.context_id,),
                    ).fetchall()
                else:
                    rows = db.execute(
                        "SELECT * FROM extraction_retention_events "
                        "WHERE context_id=? AND artifact_id=? "
                        "ORDER BY event_sequence",
                        (context.context_id, artifact_id),
                    ).fetchall()
                return tuple(self._retention_event_from_row(row) for row in rows)
        except ExtractionRetentionError:
            raise
        except sqlite3.DatabaseError as error:
            raise ExtractionRetentionError(
                "could not list extraction retention events"
            ) from error

    def list_extraction_purge_candidates(
        self, execution: ExecutionContext
    ) -> tuple[ExtractionPurgeCandidate, ...]:
        """Project current retention records into advisory metadata decisions.

        ``ExtractionRetentionService.plan_purge`` calls this authorized operation.
        The algorithm applies each record's derived precedence and returns logical
        keys/hashes only. It performs no Storage call or deletion, and finalization
        never trusts the returned snapshot without another immediate transaction.
        """
        context = self.resolve_context(execution)
        self._authorize(
            execution,
            _EXTRACTION_PURGE_PLAN,
            {"context_id": context.context_id},
        )
        try:
            with self._connection() as db:
                rows = db.execute(
                    "SELECT * FROM extraction_retention_records WHERE context_id=? "
                    "ORDER BY artifact_id",
                    (context.context_id,),
                ).fetchall()
                return tuple(
                    ExtractionPurgeCandidate.from_record(
                        self._retention_record_from_row(db, row)
                    )
                    for row in rows
                )
        except sqlite3.DatabaseError as error:
            raise ExtractionRetentionError(
                "could not plan extraction purge metadata"
            ) from error

    def _finalize_extraction_purge_after_verified_absence(
        self,
        execution: ExecutionContext,
        artifact_id: str,
        deletion_reason: str,
        *,
        absence_proof: _ExtractionPayloadAbsenceProof,
    ) -> ExtractionRetentionRecord:
        """Commit purge metadata after the service's verified T01 observation.

        This explicitly private registry seam is called only by
        ``ExtractionRetentionService.finalize_purge`` after that public service has
        traversed the same T01 ``NativeArtifactStore`` around payload-specific
        absence. The handoff fields alone are not evidence an application may
        supply. Inside one immediate transaction the algorithm reloads live state,
        rejects hold/references/unexpired/purged records, and requires artifact,
        extraction identity, hash, and logical key to match before writing
        ``purged``, tombstone, and event atomically. It executes no callback,
        Storage call, delete, parser, or network action. Stale or malformed
        internal handoffs fail without changing state or history; SQLite's write
        lock serializes competing lifecycle writers.
        """
        if not isinstance(absence_proof, _ExtractionPayloadAbsenceProof):
            raise ExtractionPurgeFinalizationError(
                "payload absence handoff must use the internal service contract"
            )
        context = self.resolve_context(execution)
        self._authorize(
            execution,
            _EXTRACTION_PURGE_FINALIZE,
            {"context_id": context.context_id, "artifact_id": artifact_id},
        )
        try:
            with self._connection(immediate=True) as db:
                row = self._required_retention_row(
                    db, context.context_id, artifact_id
                )
                current = self._retention_record_from_row(db, row)
                if not current.purge_eligible:
                    raise ExtractionPurgeBlockedError(
                        current.purge_reason or "purge is blocked"
                    )
                if (
                    absence_proof.artifact_id != current.artifact_id
                    or absence_proof.extraction_identity
                    != current.extraction_identity
                    or absence_proof.artifact_sha256 != current.artifact_sha256
                    or absence_proof.artifact_storage_key
                    != current.artifact_storage_key
                ):
                    raise ExtractionPurgeFinalizationError(
                        "payload absence proof does not match live extraction metadata"
                    )
                tombstone = RetentionTombstone(
                    parser_id=current.identity.parser_id,
                    parser_version=current.identity.parser_version,
                    source_sha256=current.identity.source_sha256,
                    artifact_sha256=current.artifact_sha256,
                    deletion_reason=deletion_reason,
                )
                now = _now()
                db.execute(
                    "UPDATE extraction_retention_records SET state=?,legal_hold=0,"
                    "updated_at=?,updated_by=?,updated_run_id=?,tombstone_json=? "
                    "WHERE context_id=? AND artifact_id=?",
                    (
                        ExtractionRetentionState.PURGED.value,
                        now,
                        execution.principal_id,
                        execution.run_id,
                        json.dumps(
                            tombstone.to_dict(),
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        context.context_id,
                        artifact_id,
                    ),
                )
                self._append_retention_event(
                    db,
                    execution,
                    artifact_id,
                    ExtractionRetentionEventType.PURGED,
                    reason=deletion_reason,
                    timestamp=now,
                )
                return self._retention_record_from_row(
                    db,
                    self._required_retention_row(
                        db, context.context_id, artifact_id
                    ),
                )
        except ExtractionRetentionError:
            raise
        except sqlite3.DatabaseError as error:
            raise ExtractionPurgeFinalizationError(
                "could not finalize extraction purge metadata"
            ) from error

    def _release_failed_reuse_acquisition(
        self,
        execution: ExecutionContext,
        artifact_id: str,
        reference_id: str,
        acquisition_token: str | None,
    ) -> None:
        """Remove only the reference row inserted by one failed reuse acquisition.

        The retention service calls this compensating metadata action after T01
        reload fails. A missing token means the reference predated this acquisition
        and must survive. The guarded delete matches context, artifact, reference,
        timestamp, and run ID under an immediate transaction, so another caller's
        lifecycle state is not removed. No Storage or parser operation occurs.
        """
        if acquisition_token is None:
            return
        context = self.resolve_context(execution)
        try:
            with self._connection(immediate=True) as db:
                cursor = db.execute(
                    "DELETE FROM extraction_references WHERE context_id=? "
                    "AND artifact_id=? AND reference_id=? AND created_at=? "
                    "AND created_run_id=?",
                    (
                        context.context_id,
                        artifact_id,
                        reference_id,
                        acquisition_token,
                        execution.run_id,
                    ),
                )
                if cursor.rowcount == 1:
                    self._touch_retention_row(db, execution, artifact_id)
                    self._append_retention_event(
                        db,
                        execution,
                        artifact_id,
                        ExtractionRetentionEventType.REFERENCE_REMOVED,
                        reference_id=reference_id,
                    )
        except sqlite3.DatabaseError as error:
            raise ExtractionRetentionError(
                "could not release failed reuse acquisition reference"
            ) from error

    def _change_extraction_reference(
        self,
        execution: ExecutionContext,
        artifact_id: str,
        reference_id: str,
        *,
        add: bool,
    ) -> ExtractionRetentionRecord:
        """Implement explicit idempotent reference add/remove under one write lock."""
        artifact_id = _retention_identifier(artifact_id, "artifact_id")
        reference_id = _retention_identifier(reference_id, "reference_id")
        context = self.resolve_context(execution)
        self._authorize(
            execution,
            _EXTRACTION_REFERENCE_WRITE,
            {
                "context_id": context.context_id,
                "artifact_id": artifact_id,
                "reference_id": reference_id,
            },
        )
        try:
            with self._connection(immediate=True) as db:
                row = self._required_retention_row(db, context.context_id, artifact_id)
                if row["state"] == ExtractionRetentionState.PURGED.value:
                    raise ExtractionRetentionConflictError(
                        "purged extraction references cannot change"
                    )
                if add:
                    cursor = db.execute(
                        "INSERT OR IGNORE INTO extraction_references("
                        "context_id,artifact_id,reference_id,created_at,created_by,"
                        "created_run_id) VALUES (?,?,?,?,?,?)",
                        (
                            context.context_id,
                            artifact_id,
                            reference_id,
                            _now(),
                            execution.principal_id,
                            execution.run_id,
                        ),
                    )
                else:
                    cursor = db.execute(
                        "DELETE FROM extraction_references WHERE context_id=? "
                        "AND artifact_id=? AND reference_id=?",
                        (context.context_id, artifact_id, reference_id),
                    )
                if cursor.rowcount == 1:
                    self._touch_retention_row(db, execution, artifact_id)
                    self._append_retention_event(
                        db,
                        execution,
                        artifact_id,
                        (
                            ExtractionRetentionEventType.REFERENCE_ADDED
                            if add
                            else ExtractionRetentionEventType.REFERENCE_REMOVED
                        ),
                        reference_id=reference_id,
                    )
                return self._retention_record_from_row(
                    db,
                    self._required_retention_row(db, context.context_id, artifact_id),
                )
        except ExtractionRetentionError:
            raise
        except sqlite3.DatabaseError as error:
            raise ExtractionRetentionError(
                "could not update extraction active references"
            ) from error

    def _update_retention_policy(
        self,
        execution: ExecutionContext,
        artifact_id: str,
        *,
        action: str,
        operation: str,
        update: Callable[
            [sqlite3.Connection, sqlite3.Row],
            ExtractionRetentionEventType | None,
        ],
    ) -> ExtractionRetentionRecord:
        """Apply one policy mutation and append its event in the same transaction.

        Legal-hold and expiry methods share this authorized race boundary. Their
        bounded callback may only mutate the caller-owned row and return a frozen
        event type when state changed; ``None`` makes retries side-effect free.
        This helper then updates audit metadata and appends history atomically.
        Purge, payload I/O, parser calls, and physical deletion are outside it.
        """
        artifact_id = _retention_identifier(artifact_id, "artifact_id")
        context = self.resolve_context(execution)
        self._authorize(
            execution,
            action,
            {"context_id": context.context_id, "artifact_id": artifact_id},
        )
        try:
            with self._connection(immediate=True) as db:
                row = self._required_retention_row(db, context.context_id, artifact_id)
                if row["state"] == ExtractionRetentionState.PURGED.value:
                    raise ExtractionRetentionConflictError(
                        f"purged extraction cannot change {operation}"
                    )
                event_type = update(db, row)
                if event_type is not None:
                    self._touch_retention_row(db, execution, artifact_id)
                    self._append_retention_event(
                        db, execution, artifact_id, event_type
                    )
                return self._retention_record_from_row(
                    db,
                    self._required_retention_row(db, context.context_id, artifact_id),
                )
        except ExtractionRetentionError:
            raise
        except sqlite3.DatabaseError as error:
            raise ExtractionRetentionError(
                f"could not update extraction {operation} metadata"
            ) from error

    @staticmethod
    def _retention_row(
        db: sqlite3.Connection, context_id: str, artifact_id: str
    ) -> sqlite3.Row | None:
        """Read one scoped retention row inside the caller's transaction."""
        return db.execute(
            "SELECT * FROM extraction_retention_records "
            "WHERE context_id=? AND artifact_id=?",
            (context_id, artifact_id),
        ).fetchone()

    @classmethod
    def _required_retention_row(
        cls, db: sqlite3.Connection, context_id: str, artifact_id: str
    ) -> sqlite3.Row:
        """Require one scoped row without revealing cross-context existence."""
        row = cls._retention_row(db, context_id, artifact_id)
        if row is None:
            raise ExtractionRetentionReferenceError(
                "extraction artifact does not exist in this context"
            )
        return row

    @staticmethod
    def _touch_retention_row(
        db: sqlite3.Connection,
        execution: ExecutionContext,
        artifact_id: str,
    ) -> None:
        """Update bounded audit metadata inside an existing immediate transaction."""
        db.execute(
            "UPDATE extraction_retention_records SET updated_at=?,updated_by=?,"
            "updated_run_id=? WHERE context_id=? AND artifact_id=?",
            (
                _now(),
                execution.principal_id,
                execution.run_id,
                execution.context_id,
                artifact_id,
            ),
        )

    @staticmethod
    def _append_retention_event(
        db: sqlite3.Connection,
        execution: ExecutionContext,
        artifact_id: str,
        event_type: ExtractionRetentionEventType,
        *,
        reference_id: str | None = None,
        reason: str | None = None,
        timestamp: str | None = None,
    ) -> None:
        """Append one changed-state fact inside the caller's write transaction.

        All retention writers use this helper only after a real mutation. SQLite
        assigns the monotonic sequence while the supplied execution context fixes
        tenant, principal, and run ownership. The insert shares commit/rollback
        fate with current-state metadata, stores no payload or source text, and
        cannot update or delete an earlier event.
        """
        db.execute(
            "INSERT INTO extraction_retention_events("
            "context_id,artifact_id,event_type,timestamp,principal_id,run_id,"
            "reference_id,reason) VALUES (?,?,?,?,?,?,?,?)",
            (
                execution.context_id,
                artifact_id,
                event_type.value,
                timestamp or _now(),
                execution.principal_id,
                execution.run_id,
                reference_id,
                reason,
            ),
        )

    @staticmethod
    def _retention_event_from_row(row: sqlite3.Row) -> ExtractionRetentionEvent:
        """Project one untrusted event row into the strict immutable contract.

        Event-list readers call this trust boundary after context filtering. It
        validates sequence, vocabulary, identifiers, and event-specific fields;
        malformed SQL values become bounded retention errors rather than silently
        entering audit history. The projection is pure and has no side effects.
        """
        try:
            return ExtractionRetentionEvent(
                sequence=row["event_sequence"],
                context_id=row["context_id"],
                artifact_id=row["artifact_id"],
                event_type=ExtractionRetentionEventType(row["event_type"]),
                timestamp=row["timestamp"],
                principal_id=row["principal_id"],
                run_id=row["run_id"],
                reference_id=row["reference_id"],
                reason=row["reason"],
            )
        except ExtractionRetentionError:
            raise
        except (TypeError, ValueError) as error:
            raise ExtractionRetentionError(
                "retention catalog contains an invalid lifecycle event"
            ) from error

    @staticmethod
    def _retention_record_from_row(
        db: sqlite3.Connection, row: sqlite3.Row
    ) -> ExtractionRetentionRecord:
        """Rebuild strict immutable metadata and sorted references from SQLite.

        Every retention reader shares this trust-boundary projection. The algorithm
        parses exact stored state/tombstone fields and selects active references in
        lexical order; malformed catalog data raises typed T07 validation rather
        than escaping as mutable dictionaries or raw SQL values.
        """
        references = tuple(
            item["reference_id"]
            for item in db.execute(
                "SELECT reference_id FROM extraction_references "
                "WHERE context_id=? AND artifact_id=? ORDER BY reference_id",
                (row["context_id"], row["artifact_id"]),
            ).fetchall()
        )
        try:
            tombstone_value = (
                json.loads(row["tombstone_json"])
                if row["tombstone_json"]
                else None
            )
            if tombstone_value is not None and not isinstance(
                tombstone_value, dict
            ):
                raise TypeError("tombstone must be an object")
            tombstone = (
                RetentionTombstone(**tombstone_value)
                if tombstone_value is not None
                else None
            )
            return ExtractionRetentionRecord(
                context_id=row["context_id"],
                artifact_id=row["artifact_id"],
                identity=ExtractionIdentity(
                    source_sha256=row["source_sha256"],
                    parser_id=row["parser_id"],
                    parser_version=row["parser_version"],
                    parser_configuration_hash=row["parser_configuration_hash"],
                    model_version=row["model_version"],
                    scope=row["scope"],
                ),
                extraction_identity=row["extraction_identity"],
                artifact_sha256=row["artifact_sha256"],
                artifact_storage_key=row["artifact_storage_key"],
                artifact_media_type=row["artifact_media_type"],
                state=ExtractionRetentionState(row["state"]),
                reference_ids=references,
                legal_hold=_stored_legal_hold(row["legal_hold"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                updated_by=row["updated_by"],
                updated_run_id=row["updated_run_id"],
                tombstone=tombstone,
            )
        except ExtractionRetentionError:
            raise
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise ExtractionRetentionError(
                "retention catalog contains invalid lifecycle metadata"
            ) from error

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
                CREATE TABLE IF NOT EXISTS extraction_retention_records (
                    context_id TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    parser_id TEXT NOT NULL,
                    parser_version TEXT NOT NULL,
                    parser_configuration_hash TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    extraction_identity TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    artifact_storage_key TEXT NOT NULL,
                    artifact_media_type TEXT NOT NULL,
                    state TEXT NOT NULL,
                    legal_hold INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_by TEXT,
                    updated_run_id TEXT NOT NULL,
                    tombstone_json TEXT,
                    PRIMARY KEY(context_id,artifact_id),
                    FOREIGN KEY(context_id) REFERENCES contexts(context_id)
                );
                CREATE INDEX IF NOT EXISTS
                    extraction_retention_identity_lookup
                    ON extraction_retention_records(context_id,extraction_identity,state);
                CREATE UNIQUE INDEX IF NOT EXISTS
                    extraction_retention_live_identity_unique
                    ON extraction_retention_records(context_id,extraction_identity)
                    WHERE state != 'purged';
                CREATE TABLE IF NOT EXISTS extraction_references (
                    context_id TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    reference_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    created_by TEXT,
                    created_run_id TEXT NOT NULL,
                    PRIMARY KEY(context_id,artifact_id,reference_id),
                    FOREIGN KEY(context_id,artifact_id)
                        REFERENCES extraction_retention_records(context_id,artifact_id)
                        ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS extraction_retention_events (
                    event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    context_id TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    event_type TEXT NOT NULL CHECK(event_type IN (
                        'registered',
                        'reference-added',
                        'reference-removed',
                        'legal-hold-enabled',
                        'legal-hold-released',
                        'retention-expired',
                        'purged'
                    )),
                    timestamp TEXT NOT NULL,
                    principal_id TEXT,
                    run_id TEXT NOT NULL,
                    reference_id TEXT,
                    reason TEXT,
                    FOREIGN KEY(context_id,artifact_id)
                        REFERENCES extraction_retention_records(context_id,artifact_id)
                        ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS
                    extraction_retention_events_context_artifact_sequence
                    ON extraction_retention_events(
                        context_id,artifact_id,event_sequence
                    );
                CREATE TRIGGER IF NOT EXISTS
                    extraction_retention_events_no_update
                    BEFORE UPDATE ON extraction_retention_events
                    BEGIN
                        SELECT RAISE(ABORT, 'retention events are append-only');
                    END;
                CREATE TRIGGER IF NOT EXISTS
                    extraction_retention_events_no_delete
                    BEFORE DELETE ON extraction_retention_events
                    BEGIN
                        SELECT RAISE(ABORT, 'retention events are append-only');
                    END;
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


def _immutable_retention_facts(record: ExtractionRetentionRecord) -> tuple[object, ...]:
    """Return fields registration may compare but lifecycle operations never rewrite.

    Equivalent registration retries use this projection rather than comparing
    state, references, hold, timestamps, or tombstone values that legitimately
    evolve later. The helper is pure, payload-free, deterministic, and receives an
    already validated frozen record.
    """
    return (
        record.context_id,
        record.artifact_id,
        record.identity,
        record.extraction_identity,
        record.artifact_sha256,
        record.artifact_storage_key,
        record.artifact_media_type,
    )


def _stored_legal_hold(value: object) -> bool:
    """Decode only SQLite's exact zero-or-one legal-hold representation.

    Registry readers and hold writers share this untrusted-catalog boundary.
    Accepting Python truthiness would convert values such as ``2`` or ``"false"``
    into policy state, so this helper requires an actual SQLite integer equal to
    zero or one. It performs no mutation and raises a bounded conflict before an
    invalid row can influence purge eligibility.
    """
    if type(value) is not int or value not in {0, 1}:
        raise ExtractionRetentionConflictError(
            "retention catalog legal_hold must be exact integer 0 or 1"
        )
    return value == 1


def _retention_identifier(value: object, field_name: str) -> str:
    """Validate bounded registry IDs without exposing malformed input values."""
    if not isinstance(value, str) or _RETENTION_IDENTIFIER_RE.fullmatch(value) is None:
        raise ExtractionRetentionReferenceError(
            f"{field_name} must be a bounded identifier"
        )
    return value


def _bundle_segments(path: str) -> tuple[str, ...]:
    parts = tuple(
        item.strip() for item in path.strip("/").split("/") if item.strip()
    )
    if not parts or any(item in {".", ".."} for item in parts):
        raise ValueError("Bundle path must contain normal name segments.")
    return parts


def _normalise_generated_bundle_path(path: str) -> str:
    selected = path.strip().replace("\\", "/")
    if not selected or selected.startswith("/"):
        raise ValueError("Bundle path must be a non-empty relative path.")
    if len(selected) >= 2 and selected[1] == ":":
        raise ValueError("Bundle path must not contain an absolute drive path.")
    parts = selected.split("/")
    if any(not part.strip() or part.strip() in {".", ".."} for part in parts):
        raise ValueError("Bundle path must contain only normal name segments.")
    return "/".join(part.strip() for part in parts)


def _bundle_for_relative_path(
    root_bundle_path: str, relative_path: str, structure: str
) -> str:
    if structure == "flat":
        return root_bundle_path
    parent_parts = Path(relative_path).parent.parts
    if not parent_parts:
        return root_bundle_path
    return _normalise_generated_bundle_path(
        "/".join((root_bundle_path, *parent_parts))
    )


def _is_catalog_file(candidate: Path, catalog: Path) -> bool:
    if candidate == catalog or candidate.parent != catalog.parent:
        return candidate == catalog
    return candidate.name in {
        f"{catalog.name}-journal",
        f"{catalog.name}-shm",
        f"{catalog.name}-wal",
    }


def _batch_result(
    *,
    batch_id: str,
    context_id: str,
    root_bundle_id: str,
    root_bundle_path: str,
    structure: str,
    recursive: bool,
    files_discovered: int,
    created: int,
    restored: int,
    already_registered: int,
    failed: int,
    skipped: int,
    items: list[SourceAssetBatchItem],
) -> SourceAssetBatchResult:
    return SourceAssetBatchResult(
        batch_id=batch_id,
        context_id=context_id,
        root_bundle_id=root_bundle_id,
        root_bundle_path=root_bundle_path,
        structure=structure,
        recursive=recursive,
        files_discovered=files_discovered,
        files_processed=created + restored + already_registered + failed,
        created_count=created,
        restored_count=restored,
        already_registered_count=already_registered,
        failed_count=failed,
        skipped_count=skipped,
        items=tuple(sorted(items, key=lambda item: item.relative_path)),
    )


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
