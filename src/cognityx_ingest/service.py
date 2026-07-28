"""SourceAsset-first PDF normalization, provenance, and artifact persistence."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import logging
from pathlib import Path
from tempfile import NamedTemporaryFile
import time
from typing import Any
from uuid import uuid4

from cognityx_jobs import JobRepository
from cognityx_storage import StorageClient, StorageConfig, StorageRuntime

from cognityx_ingest.control import (
    INGEST_JOB_SUBMIT,
    ControlClient,
    IngestAuthorizationError,
    IngestLimitError,
    LocalControlClient,
)
from cognityx_ingest.enhancement import SectionEnhancer
from cognityx_ingest.models import (
    ArtifactRef,
    CanonicalDocument,
    Evidence,
    ExecutionContext,
    IngestJobState,
    IngestResult,
    IngestRunResult,
    Section,
    SourceAsset,
    SourceRecord,
    UsageReport,
)
from cognityx_ingest.parser import PdfExtractor, PyPdfExtractor, UnsupportedInputError
from cognityx_ingest.source_assets import SourceAssetRegistry

DOCUMENT_SCHEMA_VERSION = "cognityx.ingest.document/v1"
EVIDENCE_SCHEMA_VERSION = "cognityx.ingest.evidence/v2"
RUN_SCHEMA_VERSION = "cognityx.ingest.run/v1"
LOGGER = logging.getLogger(__name__)


class IngestService:
    """Ingest registered SourceAssets into canonical document artifacts."""

    def __init__(
        self,
        storage: StorageClient,
        *,
        extractor: PdfExtractor | None = None,
        jobs: JobRepository | None = None,
        enhancer: SectionEnhancer | None = None,
        control: ControlClient | None = None,
        registry: SourceAssetRegistry | None = None,
    ) -> None:
        self._storage = storage
        self._extractor = extractor or PyPdfExtractor()
        self._jobs = jobs
        self._enhancer = enhancer
        self._control = control or LocalControlClient()
        self._registry = registry

    def ingest_asset(
        self,
        asset_id: str,
        registry: SourceAssetRegistry,
        execution_context: ExecutionContext,
    ) -> IngestResult:
        """Resolve and ingest one canonical SourceAsset."""
        run = self.ingest_assets(
            (asset_id,),
            registry,
            execution_context,
            submitted_input={"type": "asset", "asset_id": asset_id},
            raise_on_failure=True,
        )
        if not run.results:
            failure = run.failures[0] if run.failures else {"error": "ingest failed"}
            raise UnsupportedInputError(str(failure["error"]))
        return run.results[0]

    def ingest(
        self,
        path: str | Path,
        *,
        owner_id: str = "local",
        context: ExecutionContext | None = None,
        registry: SourceAssetRegistry | None = None,
    ) -> IngestResult:
        """Register one path as a SourceAsset, then use the asset ingest path."""
        run = self.ingest_path(
            path,
            owner_id=owner_id,
            context=context,
            registry=registry,
        )
        if not run.results:
            failure = run.failures[0] if run.failures else {"error": "ingest failed"}
            raise UnsupportedInputError(str(failure["error"]))
        return run.results[0]

    def ingest_path(
        self,
        path: str | Path,
        *,
        owner_id: str = "local",
        context: ExecutionContext | None = None,
        registry: SourceAssetRegistry | None = None,
    ) -> IngestRunResult:
        """Register and ingest one PDF or a recursive folder in one shared run."""
        selected = Path(path)
        execution = context or self._local_context(owner_id)
        selected_registry = registry or self._registry or self._local_registry()
        if selected.is_file():
            if selected.suffix.lower() != ".pdf":
                raise UnsupportedInputError(f"Only PDF input is supported: {selected}")
            registration = selected_registry.register_asset(execution, selected)
            return self.ingest_assets(
                (registration.asset_id,),
                selected_registry,
                execution,
                submitted_input={"type": "path", "path": str(selected)},
                root_bundle_id=registration.bundle_id,
                registered_assets=(registration.asset_id,),
                legacy_events=True,
                raise_on_failure=True,
            )
        if not selected.is_dir():
            raise FileNotFoundError(selected)
        return self._ingest_folder(selected, selected_registry, execution, owner_id)

    def ingest_bundle(
        self,
        bundle_id: str,
        registry: SourceAssetRegistry,
        execution_context: ExecutionContext,
    ) -> IngestRunResult:
        """Ingest all active PDF SourceAssets in a bundle subtree."""
        bundles = registry.list_doc_bundles(execution_context)
        root = next((item for item in bundles if item.bundle_id == bundle_id), None)
        if root is None:
            raise KeyError(f"DocBundle does not exist in this context: {bundle_id}")
        included = {root.bundle_id}
        changed = True
        while changed:
            previous = len(included)
            included.update(
                item.bundle_id
                for item in bundles
                if item.parent_bundle_id in included
            )
            changed = len(included) != previous
        asset_ids = tuple(
            item.asset_id
            for item in registry.list_assets(execution_context)
            if item.bundle_id in included
            and (
                item.media_type == "application/pdf"
                or item.original_filename.lower().endswith(".pdf")
            )
        )
        return self.ingest_assets(
            asset_ids,
            registry,
            execution_context,
            submitted_input={"type": "bundle", "bundle_id": bundle_id},
            root_bundle_id=bundle_id,
        )

    def ingest_assets(
        self,
        asset_ids: tuple[str, ...],
        registry: SourceAssetRegistry,
        context: ExecutionContext,
        *,
        submitted_input: dict[str, Any],
        root_bundle_id: str | None = None,
        registered_assets: tuple[str, ...] = (),
        registration_failures: tuple[dict[str, Any], ...] = (),
        legacy_events: bool = False,
        job_id_override: str | None = None,
        raise_on_failure: bool = False,
    ) -> IngestRunResult:
        """Coordinate one durable run over already registered SourceAssets."""
        owner_id = context.principal_id or "local"
        created_at = _now()
        job_id = job_id_override or self._start_job(
            owner_id, context, submitted_input, legacy_events=legacy_events
        )
        results: list[IngestResult] = []
        failures = list(registration_failures)
        assets: list[SourceAsset] = []
        for asset_id in asset_ids:
            if self._cancellation_requested(job_id):
                break
            asset: SourceAsset | None = None
            try:
                asset = registry.show_asset(context, asset_id)
                assets.append(asset)
                self._append_event(
                    job_id,
                    "document_started",
                    {"asset_id": asset.asset_id},
                    enabled=not legacy_events,
                )
                result = self._ingest_resolved_asset(
                    asset, registry, context, job_id
                )
                results.append(result)
                self._append_event(
                    job_id,
                    "document_completed",
                    {
                        "asset_id": asset.asset_id,
                        "document_id": result.document.document_id,
                    },
                    enabled=not legacy_events,
                )
            except Exception as error:
                if raise_on_failure:
                    self._finish_job(
                        job_id,
                        IngestJobState.FAILED,
                        {"error": str(error)},
                        event="ingest_failed",
                    )
                    raise
                failure = {
                    "asset_id": asset_id,
                    "filename": asset.original_filename if asset else None,
                    "bundle_id": asset.bundle_id if asset else None,
                    "error_category": type(error).__name__,
                    "error": str(error),
                }
                failures.append(failure)
                self._append_event(
                    job_id, "document_failed", failure, enabled=not legacy_events
                )

        cancelled = self._cancellation_requested(job_id)
        completed_at = _now()
        run_manifest_key = f"ingest/runs/{context.run_id}/manifest.json"
        manifest = {
            "schema": "cognityx.ingest.run",
            "schema_version": RUN_SCHEMA_VERSION,
            "document_schema_version": DOCUMENT_SCHEMA_VERSION,
            "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
            "run_id": context.run_id,
            "correlation_id": context.correlation_id,
            "context_id": context.context_id,
            "job_id": job_id,
            "submitted_input": submitted_input,
            "root_bundle_id": root_bundle_id,
            "source_assets": [
                {
                    "asset_id": item.asset_id,
                    "bundle_id": item.bundle_id,
                    "sha256": item.sha256,
                }
                for item in assets
            ],
            "registered_asset_ids": list(registered_assets),
            "document_ids": [item.document.document_id for item in results],
            "document_manifest_refs": [
                self._artifact_uri(item.manifest_key) for item in results
            ],
            "evidence_refs": [
                self._artifact_uri(item.evidence_key) for item in results
            ],
            "successful_files": [
                {
                    "asset_id": item.document.source.source_id,
                    "filename": item.document.source.filename,
                    "document_id": item.document.document_id,
                }
                for item in results
            ],
            "failed_files": failures,
            "parser_name": self._parser_name(),
            "parser_version": self._parser_version(),
            "created_at": created_at,
            "completed_at": completed_at,
            "cancelled": cancelled,
        }
        self._put_immutable_json(run_manifest_key, manifest)
        state = (
            IngestJobState.CANCELLED
            if cancelled
            else IngestJobState.COMPLETED
            if results or not failures
            else IngestJobState.FAILED
        )
        completion = {
            "run_id": context.run_id,
            "document_count": len(results),
            "failed_count": len(failures),
            "run_manifest_uri": self._artifact_uri(run_manifest_key),
        }
        if legacy_events:
            self._finish_job(job_id, state, completion, event=f"ingest_{state}")
        else:
            self._append_event(job_id, "run_completed", completion)
            self._set_job_state(job_id, state)
        return IngestRunResult(
            run_id=context.run_id,
            job_id=job_id,
            root_bundle_id=root_bundle_id,
            results=tuple(results),
            failures=tuple(failures),
            run_manifest_key=run_manifest_key,
            run_manifest_uri=self._artifact_uri(run_manifest_key),
        )

    def _ingest_folder(
        self,
        folder: Path,
        registry: SourceAssetRegistry,
        context: ExecutionContext,
        owner_id: str,
    ) -> IngestRunResult:
        root = registry.resolve_doc_bundle(context, folder.resolve().name)
        files = sorted(
            item
            for item in folder.rglob("*")
            if item.is_file() and item.suffix.lower() == ".pdf"
        )
        job_id = self._start_job(
            owner_id,
            context,
            {"type": "folder", "path": str(folder)},
            legacy_events=False,
        )
        self._append_event(
            job_id,
            "folder_discovered",
            {"root_bundle_id": root.bundle_id, "pdf_count": len(files)},
        )
        asset_ids: list[str] = []
        registration_failures: list[dict[str, Any]] = []
        for path in files:
            if self._cancellation_requested(job_id):
                break
            relative = path.relative_to(folder)
            parent = relative.parent.as_posix()
            bundle_path = root.path if parent == "." else f"{root.path}/{parent}"
            try:
                registered = registry.register_asset(
                    context, path, bundle=bundle_path
                )
                asset_ids.append(registered.asset_id)
                self._append_event(
                    job_id,
                    "asset_registered",
                    {
                        "relative_path": relative.as_posix(),
                        "asset_id": registered.asset_id,
                        "bundle_id": registered.bundle_id,
                        "status": registered.status,
                    },
                )
            except Exception as error:
                failure = {
                    "relative_path": relative.as_posix(),
                    "error_category": type(error).__name__,
                    "error": str(error),
                }
                registration_failures.append(failure)
                self._append_event(job_id, "document_failed", failure)

        return self.ingest_assets(
            tuple(asset_ids),
            registry,
            context,
            submitted_input={"type": "folder", "path": str(folder)},
            root_bundle_id=root.bundle_id,
            registered_assets=tuple(asset_ids),
            registration_failures=tuple(registration_failures),
            job_id_override=job_id,
        )

    def _ingest_resolved_asset(
        self,
        asset: SourceAsset,
        registry: SourceAssetRegistry,
        context: ExecutionContext,
        job_id: str | None,
    ) -> IngestResult:
        started_at = time.monotonic()
        if asset.media_type != "application/pdf" and not asset.original_filename.lower().endswith(
            ".pdf"
        ):
            raise UnsupportedInputError(
                f"Only PDF SourceAssets are supported: {asset.asset_id}"
            )
        decision = self._control.authorize(
            context,
            INGEST_JOB_SUBMIT,
            resource={"source_asset_id": asset.asset_id},
            request={"input_bytes": asset.size_bytes},
        )
        if not decision.allowed:
            raise IngestAuthorizationError(
                decision.reason or "Ingest submission was denied."
            )
        self._enforce_limit(
            decision.limits,
            "max_document_size",
            asset.size_bytes,
            "input bytes",
        )
        run_suffix = hashlib.sha256(context.run_id.encode()).hexdigest()[:8]
        document_id = (
            f"pdf-{asset.sha256[:12]}-{asset.asset_id[4:12]}-{run_suffix}"
        )
        with registry.open_asset(context, asset.asset_id) as source:
            with NamedTemporaryFile(suffix=".pdf") as temporary:
                temporary.write(source.read())
                temporary.flush()
                pages = self._extractor.extract(Path(temporary.name))
        evidence = tuple(
            Evidence(
                evidence_id=f"{document_id}:page:{page.page_number}",
                document_id=document_id,
                source_asset_id=asset.asset_id,
                bundle_id=asset.bundle_id,
                context_id=asset.context_id,
                page_number=page.page_number,
                sequence_number=index,
                text=page.text,
                char_start=0,
                char_end=len(page.text),
                source_sha256=asset.sha256,
                parser_name=self._parser_name(),
                parser_version=self._parser_version(),
                run_id=context.run_id,
                schema_version=EVIDENCE_SCHEMA_VERSION,
            )
            for index, page in enumerate(pages, start=1)
        )
        self._enforce_limit(decision.limits, "max_pages", len(evidence), "pages")
        sections = tuple(
            Section(
                section_id=f"{document_id}:section:{item.page_number}",
                title=f"Page {item.page_number}",
                evidence_ids=(item.evidence_id,),
            )
            for item in evidence
        )
        enhancement = (
            self._enhancer.enhance([item.text for item in evidence])
            if self._enhancer
            else None
        )
        source_record = SourceRecord(
            source_id=asset.asset_id,
            filename=asset.original_filename,
            sha256=asset.sha256,
            size_bytes=asset.size_bytes,
            storage_key=f"sourceasset://{asset.asset_id}",
            media_type=asset.media_type,
        )
        document = CanonicalDocument(
            document_id=document_id,
            schema_version=DOCUMENT_SCHEMA_VERSION,
            source=source_record,
            title=Path(asset.original_filename).stem,
            sections=sections,
            enhancement=enhancement,
        )
        result = self._persist(document, evidence, context, job_id, asset)
        usage = UsageReport(
            run_id=context.run_id,
            job_id=job_id,
            documents=1,
            pages=len(evidence),
            input_bytes=asset.size_bytes,
            output_bytes=sum(
                self._storage.stat(key).size_bytes
                for key in (
                    result.document_key,
                    result.evidence_key,
                    result.manifest_key,
                )
            ),
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )
        self._control.report_usage(context, usage)
        return IngestResult(
            result.document,
            result.evidence,
            result.manifest_key,
            result.document_key,
            result.evidence_key,
            run_id=context.run_id,
            job_id=job_id,
            artifacts=result.artifacts,
            usage=usage,
        )

    def _persist(
        self,
        document: CanonicalDocument,
        evidence: tuple[Evidence, ...],
        context: ExecutionContext,
        job_id: str | None,
        asset: SourceAsset,
    ) -> IngestResult:
        prefix = f"ingest/documents/{document.document_id}"
        document_key = f"{prefix}/document.json"
        evidence_key = f"{prefix}/evidence.jsonl"
        manifest_key = f"{prefix}/manifest.json"
        self._put_immutable_json(document_key, document.to_dict())
        payload = "".join(
            json.dumps(item.to_dict(), sort_keys=True) + "\n" for item in evidence
        ).encode()
        if not self._storage.exists(evidence_key):
            self._storage.put_bytes(
                evidence_key, payload, media_type="application/x-ndjson"
            )
        stored = {
            "document": self._storage.stat(document_key),
            "evidence": self._storage.stat(evidence_key),
        }
        artifacts = tuple(
            ArtifactRef(
                f"art-{document.document_id}-{name}",
                self._stored_uri(item),
                item.media_type,
            )
            for name, item in stored.items()
        )
        manifest = {
            "document_id": document.document_id,
            "schema": document.schema,
            "schema_version": DOCUMENT_SCHEMA_VERSION,
            "source_asset_id": asset.asset_id,
            "bundle_id": asset.bundle_id,
            "context_id": asset.context_id,
            "source_sha256": asset.sha256,
            "run_id": context.run_id,
            "job_id": job_id,
            "artifacts": {
                name: {"artifact_id": ref.artifact_id, "uri": ref.uri}
                for name, ref in zip(stored, artifacts, strict=True)
            },
        }
        self._put_immutable_json(manifest_key, manifest)
        manifest_object = self._storage.stat(manifest_key)
        manifest_ref = ArtifactRef(
            f"art-{document.document_id}-manifest",
            self._stored_uri(manifest_object),
            manifest_object.media_type,
        )
        return IngestResult(
            document,
            evidence,
            manifest_key,
            document_key,
            evidence_key,
            run_id=context.run_id,
            job_id=job_id,
            artifacts=(*artifacts, manifest_ref),
        )

    def _start_job(
        self,
        owner_id: str,
        context: ExecutionContext,
        submitted_input: dict[str, Any],
        *,
        legacy_events: bool,
    ) -> str | None:
        if self._jobs is None:
            return None
        job_id = str(uuid4())
        self._jobs.create(
            job_id,
            "ingest.run",
            {"run_id": context.run_id, "submitted_input": submitted_input},
            owner_id=owner_id,
        )
        self._jobs.append_event(
            job_id, "ingest_submitted", {"run_id": context.run_id}
        )
        self._jobs.append_event(job_id, "ingest_queued", {"run_id": context.run_id})
        self._jobs.set_state(job_id, IngestJobState.RUNNING)
        self._jobs.append_event(job_id, "ingest_started", {"run_id": context.run_id})
        return job_id

    def _finish_job(
        self,
        job_id: str | None,
        state: IngestJobState,
        data: dict[str, Any],
        *,
        event: str,
    ) -> None:
        self._append_event(job_id, event, data)
        self._set_job_state(job_id, state)

    def _append_event(
        self,
        job_id: str | None,
        event: str,
        data: dict[str, Any],
        *,
        enabled: bool = True,
    ) -> None:
        if enabled and self._jobs is not None and job_id is not None:
            self._jobs.append_event(job_id, event, data)

    def _set_job_state(
        self, job_id: str | None, state: IngestJobState
    ) -> None:
        if self._jobs is not None and job_id is not None:
            self._jobs.set_state(job_id, state)

    def _cancellation_requested(self, job_id: str | None) -> bool:
        return bool(
            self._jobs is not None
            and job_id is not None
            and self._jobs.get(job_id).state == "cancellation_requested"
        )

    def _put_immutable_json(self, key: str, value: dict[str, Any]) -> None:
        if self._storage.exists(key):
            with self._storage.open(key) as current:
                if json.load(current) != value:
                    raise RuntimeError(f"Immutable ingest artifact already exists: {key}")
            return
        self._storage.put_json(key, value)

    def _local_registry(self) -> SourceAssetRegistry:
        try:
            probe = self._storage.native_path("ingest/registry-probe")
        except Exception as error:
            raise ValueError(
                "Path ingestion requires a SourceAssetRegistry for this storage backend."
            ) from error
        root = probe.parents[2]
        runtime = StorageRuntime.from_config(StorageConfig.built_in(root=root))
        return SourceAssetRegistry.load(runtime=runtime)

    def _parser_name(self) -> str:
        return type(self._extractor).__name__

    def _parser_version(self) -> str:
        package = type(self._extractor).__module__.split(".", 1)[0]
        try:
            return version(package)
        except PackageNotFoundError:
            return "unknown"

    @staticmethod
    def _local_context(owner_id: str) -> ExecutionContext:
        return ExecutionContext(
            run_id=str(uuid4()),
            correlation_id=str(uuid4()),
            principal_id=owner_id,
        )

    @staticmethod
    def _enforce_limit(
        limits: dict[str, object],
        name: str,
        actual: int,
        unit: str,
    ) -> None:
        maximum = limits.get(name)
        if maximum is not None and actual > int(maximum):
            raise IngestLimitError(
                f"{name} exceeded: {actual} {unit} is greater than {maximum}."
            )

    def _artifact_uri(self, storage_key: str) -> str:
        uri = getattr(self._storage, "uri", None)
        return uri(storage_key) if uri is not None else f"storage://{storage_key}"

    @staticmethod
    def _stored_uri(stored: object) -> str:
        uri = str(stored.uri)
        return uri if uri.startswith("storage://") else f"storage://{stored.key}"


def _now() -> str:
    return datetime.now(UTC).isoformat()
