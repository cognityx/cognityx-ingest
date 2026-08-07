"""Orchestrate SourceAsset-first normalization and immutable artifact persistence.

This module exists to connect registration, parser-neutral normalization, Jobs,
control decisions, provenance, and Storage without making those collaborators one
component. The main flow resolves a SourceAsset, runs the configured parser,
builds compatibility records, adds independently versioned artifacts, and writes
them through immutable keys. Its design principle is compatibility by additive
contracts: v2 outputs remain stable while T01 native evidence, T02 canonical
content, and T05 parser observations plus fusion decisions gain separate identities. Python
callers, CLI composition, DataForge handoff, operations, and audit tooling use
the service. T08 additionally publishes a deterministic, text-free Source Graph
and generated strong provenance addresses from canonical facts; logical business
addresses and evidence-set intent remain explicit downstream composition. T09 adds
run-level ``dataforge_source_refs`` so DataForge can discover those T08 artifacts
without loading parser-native payloads or copying source content.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import logging
from pathlib import Path
from tempfile import NamedTemporaryFile
import time
from typing import Any, Mapping
from uuid import uuid4

from cognityx_jobs import JobRepository
from cognityx_storage import (
    ObjectAlreadyExistsError,
    StorageClient,
    StorageConfig,
    StorageRuntime,
)

from cognityx_ingest.canonical_content import (
    CANONICAL_CONTENT_SCHEMA_VERSION,
    CanonicalArtifactDescriptor,
    CanonicalContentBuilder,
)
from cognityx_ingest.control import (
    INGEST_JOB_SUBMIT,
    ControlClient,
    IngestAuthorizationError,
    IngestLimitError,
    LocalControlClient,
)
from cognityx_ingest.enhancement import (
    BoundedInferenceResolver,
    ResolutionTask,
    SectionEnhancer,
)
from cognityx_ingest.models import (
    ArtifactRef,
    Block,
    CanonicalDocument,
    DecisionRecord,
    DocumentObject,
    Evidence,
    ExecutionContext,
    IngestJobState,
    IngestResult,
    IngestRunResult,
    PageRecord,
    Relation,
    RepeatedRegion,
    RepeatedRegionOccurrence,
    Section,
    SourceAsset,
    SourceRecord,
    UnresolvedItem,
    UsageReport,
)
from cognityx_ingest.native_artifacts import NativeArtifactStore
from cognityx_ingest.objects import (
    ObjectObservations,
    build_owned_objects,
    detect_object_observations,
)
from cognityx_ingest.parser import (
    ExtractedPage,
    ExtractionResult,
    PdfExtractor,
    PyPdfExtractor,
    UnsupportedInputError,
    normalize_repeated_region_text,
    normalize_extraction,
)
from cognityx_ingest.parser_fusion import (
    ParserFusionArtifact,
    ParserFusionCompatibilityError,
    ParserObservationSet,
)
from cognityx_ingest.references import build_reference_provenance
from cognityx_ingest.source_assets import SourceAssetRegistry
from cognityx_ingest.source_graph import (
    SourceGraphBuilder,
    build_strong_address_catalog,
)
from cognityx_ingest.structure import (
    CanonicalBlockFragment,
    build_continuation_relations,
    build_sections,
    canonical_block_fragments,
    canonical_block_type,
    terminal_sentence_split_block_ids,
)
from cognityx_ingest.tables import (
    ObservedLogicalTable,
    build_table_objects,
    detect_logical_tables,
    table_source_groups,
)

DOCUMENT_SCHEMA_VERSION = "cognityx.ingest.document/v2"
EVIDENCE_SCHEMA_VERSION = "cognityx.ingest.evidence/v2"
RUN_SCHEMA_VERSION = "cognityx.ingest.run/v2"
PROVENANCE_SCHEMA_VERSION = "cognityx.ingest.provenance/v2"
LOGGER = logging.getLogger(__name__)


class IngestService:
    """Coordinate one SourceAsset from parser execution through durable artifacts.

    Responsibility:
        Preserve the established v2 ingest flow and add independently versioned
        artifacts, including T01 native descriptors, T02 canonical content, and
        T08 Source Graph/strong-address records.
    Constructed by:
        The Python composition root, CLI adapters, tests, or applications that
        provide scoped Storage, optional Jobs, parser, control, and registry seams.
    Used by:
        ``IngestManager``, normal Python callers, compatibility CLI paths, and
        DataForge-facing ingestion workflows.
    Invariants:
        Existing document identities, v2 schemas, parser keys, and public ingest
        methods remain stable; generated objects are written immutably and usage
        accounts for every returned document-local artifact.
    Lifecycle/persistence:
        A service instance owns collaborators but no source payload state. Each
        call creates or reuses run-scoped records in the supplied repositories.
    Thread-safety assumptions:
        The service does not add mutable coordination state, but callers must obey
        the concurrency contracts of the supplied parser, Storage, Jobs, control,
        and registry implementations.
    """

    def __init__(
        self,
        storage: StorageClient,
        *,
        extractor: PdfExtractor | None = None,
        jobs: JobRepository | None = None,
        enhancer: SectionEnhancer | None = None,
        resolver: BoundedInferenceResolver | None = None,
        control: ControlClient | None = None,
        registry: SourceAssetRegistry | None = None,
    ) -> None:
        self._storage = storage
        self._extractor = extractor or PyPdfExtractor()
        self._jobs = jobs
        self._enhancer = enhancer
        self._resolver = resolver
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
        """Coordinate one durable run over already registered SourceAssets.

        Responsibility:
            Execute each requested asset independently, retain partial successes,
            and publish one immutable run manifest that points to all successful
            document artifacts, including the compact T09 DataForge source refs.
        Main algorithm and ordering:
            Process ``asset_ids`` in caller order, append successful results in
            that same order, omit failed items from success projections, and write
            the manifest only after document-local persistence has completed.
        Design principle and consumers:
            The CLI, Python composition root, Jobs observers, and DataForge use an
            additive manifest; existing v2 fields remain unchanged while T09 gets
            logical Storage URIs instead of source text or parser-native payloads.
        Lifecycle, idempotency, and side effects:
            Parsing, registry reads, Jobs events, and immutable Storage writes are
            explicit side effects. Equal retries must match existing bytes; no
            artifact is overwritten or repaired.
        Trust boundary and failures:
            Registry/parser/Storage/control errors are either raised or recorded
            as typed per-file failures according to ``raise_on_failure``. The
            service holds no run-local mutable state after return; concurrent use
            depends on the injected collaborators' thread-safety guarantees.
        """
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
            "provenance_refs": [
                self._artifact_uri(item.provenance_key)
                for item in results
                if item.provenance_key
            ],
            "dataforge_source_refs": [
                self._dataforge_source_ref(item) for item in results
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
                extraction = normalize_extraction(
                    self._extractor, Path(temporary.name)
                )
                pages = extraction.pages
        table_observations = detect_logical_tables(
            extraction.pages, extraction.backend
        )
        object_observations = detect_object_observations(
            extraction.pages, extraction.backend
        )
        page_records, blocks, parser_objects = _canonical_structure(
            document_id, extraction, table_observations, object_observations
        )
        repeated_regions = _canonical_repeated_regions(document_id, page_records, blocks)
        blocks_by_id = {item.block_id: item for item in blocks}
        page_by_index = {
            item.physical_page_index: item.page_id for item in page_records
        }
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
                physical_page_index=page.physical_page_index,
                pdf_page_label=page.page_label,
                printed_page_label=page.printed_page_label,
                block_id=(
                    next(
                        (
                            block_id
                            for block_id in page_records[index - 1].block_ids
                            if blocks_by_id[block_id].block_type
                            not in {"page_header", "page_footer"}
                        ),
                        None,
                    )
                ),
                anchor_id=page_records[index - 1].page_id,
                continues_from=(
                    f"{document_id}:page:{pages[index - 2].page_number}"
                    if index > 1
                    else None
                ),
                continues_to=(
                    f"{document_id}:page:{pages[index].page_number}"
                    if index < len(pages)
                    else None
                ),
            )
            for index, page in enumerate(pages, start=1)
        )
        self._enforce_limit(decision.limits, "max_pages", len(evidence), "pages")
        sections = _canonical_sections(
            document_id,
            Path(asset.original_filename).stem,
            extraction,
            page_records,
            blocks,
            evidence,
        )
        table_objects = build_table_objects(
            document_id, table_observations, page_records, blocks, sections
        )
        owned_objects, object_relations = build_owned_objects(
            document_id, object_observations, page_records, blocks, sections
        )
        objects = (*parser_objects, *table_objects, *owned_objects)
        continuation_relations = build_continuation_relations(
            document_id, sections, page_records, blocks
        )
        deterministic_relations, deterministic_unresolved, handled_relations = (
            build_reference_provenance(
                document_id, extraction, page_records, blocks, sections
            )
        )
        observed_relations, tasks = _relations_and_tasks(
            document_id,
            extraction,
            page_by_index,
            ignored_relation_ids=handled_relations,
        )
        valid_anchor_ids = frozenset(
            [item.page_id for item in page_records]
            + [item.block_id for item in blocks]
            + [item.object_id for item in objects]
        )
        decisions: tuple[DecisionRecord, ...] = (
            DecisionRecord(
                decision_id=f"{document_id}:parser-selection",
                task_id=f"{document_id}:parser-selection",
                status="accepted",
                method="parser_policy",
                considered_tools=extraction.considered_backends,
                invoked_tools=tuple(
                    extraction.diagnostics.get("source_backends", ())
                )
                or (extraction.backend,),
                selected_tool=extraction.backend,
                selected_reason=extraction.selected_reason,
                confidence=1.0,
                reason=extraction.selected_reason,
            ),
        )
        unresolved: tuple[UnresolvedItem, ...] = (
            *deterministic_unresolved,
            *(
                UnresolvedItem(
                    task_id=item.task_id,
                    source_anchor_id=item.source_anchor_id,
                    relation_type=item.relation_type,
                    target_text=item.target_text,
                    reason="no_resolution_policy",
                )
                for item in tasks
            )
        )
        inferred_relations: tuple[Relation, ...] = ()
        if self._resolver and tasks:
            resolution = self._resolver.resolve(
                tasks,
                valid_anchor_ids=valid_anchor_ids,
                execution_context={
                    "run_id": context.run_id,
                    "context_id": context.context_id,
                    "document_id": document_id,
                    "source_asset_id": asset.asset_id,
                },
            )
            inferred_relations = resolution.relations
            decisions = (*decisions, *resolution.decisions)
            unresolved = (*deterministic_unresolved, *resolution.unresolved)
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
            aliases=(asset.original_filename, Path(asset.original_filename).stem),
            pages=page_records,
            blocks=blocks,
            repeated_regions=repeated_regions,
            objects=objects,
            relations=(
                *continuation_relations,
                *object_relations,
                *deterministic_relations,
                *observed_relations,
                *inferred_relations,
            ),
            decisions=decisions,
            unresolved=unresolved,
        )
        result = self._persist(
            document,
            evidence,
            context,
            job_id,
            asset,
            extraction=extraction,
        )
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
                    result.provenance_key,
                    result.canonical_content_key,
                    result.observation_artifact_key,
                    result.fusion_artifact_key,
                    result.source_graph_key,
                    result.provenance_addresses_key,
                )
                if key
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
            provenance_key=result.provenance_key,
            raw_parser_key=result.raw_parser_key,
            raw_parser_keys=result.raw_parser_keys,
            canonical_content_key=result.canonical_content_key,
            observation_artifact_key=result.observation_artifact_key,
            fusion_artifact_key=result.fusion_artifact_key,
            source_graph_key=result.source_graph_key,
            provenance_addresses_key=result.provenance_addresses_key,
        )

    def _persist(
        self,
        document: CanonicalDocument,
        evidence: tuple[Evidence, ...],
        context: ExecutionContext,
        job_id: str | None,
        asset: SourceAsset,
        *,
        extraction: ExtractionResult,
    ) -> IngestResult:
        """Persist one ingest result while retaining legacy and native identities.

        The ingest pipeline calls this after normalization. Existing v2 artifacts
        retain their established keys and schema meanings. Parser-native payloads
        still pass through ``NativeArtifactStore`` at their existing keys. T02
        then builds one validated additive parser-neutral artifact at
        ``canonical-content.json``. T08 projects its existing IDs into immutable
        ``source-graph.json`` and generated strong addresses into
        ``provenance-addresses.json``. Their references are additive in provenance
        and manifest without replacing or reinterpreting ``document.json``.

        Canonical content plus optional T05 observations and fusion decisions are
        validated together, serialized once through their deterministic public
        boundaries, and written as exact ``application/json`` bytes. Both T05
        artifacts are Cognityx processing output and therefore bypass
        ``NativeArtifactStore``. Repeating
        an equivalent run-bound write is idempotent only when existing bytes and
        media type match exactly. Changed canonical data, parser bytes, native
        descriptors, or v3.2 content fails rather than overwriting retained
        evidence. Graph/address construction reads canonical records only, never
        source or parser-native payloads, so later T07 purge does not affect
        resolution. Storage writes are the only side effect. Existing
        document-prefix deletion removes these additive artifacts; T07 owns raw
        retention policy. T06 segmentation and T09 DataForge consume the records;
        no semantic graph, SDK, or CLI behavior is implemented here.
        """
        prefix = f"ingest/documents/{document.document_id}"
        document_key = f"{prefix}/document.json"
        evidence_key = f"{prefix}/evidence.jsonl"
        manifest_key = f"{prefix}/manifest.json"
        provenance_key = f"{prefix}/provenance.json"
        canonical_content_key = f"{prefix}/canonical-content.json"
        source_graph_key = f"{prefix}/source-graph.json"
        provenance_addresses_key = f"{prefix}/provenance-addresses.json"
        observation_artifact_key = (
            f"{prefix}/parser/observations.json"
            if extraction.observation_artifact is not None
            else ""
        )
        fusion_artifact_key = (
            f"{prefix}/parser/fusion-decisions.json"
            if extraction.fusion_artifact is not None
            else ""
        )
        if (extraction.observation_artifact is None) != (
            extraction.fusion_artifact is None
        ):
            raise ParserFusionCompatibilityError(
                "Parser observations and fusion decisions must be supplied together."
            )
        observation_set = (
            ParserObservationSet.from_json_bytes(extraction.observation_artifact)
            if extraction.observation_artifact is not None
            else None
        )
        fusion_artifact = (
            ParserFusionArtifact.from_json_bytes(extraction.fusion_artifact)
            if extraction.fusion_artifact is not None
            else None
        )
        if observation_set is not None and fusion_artifact is not None:
            if observation_set.to_json_bytes() != extraction.observation_artifact:
                raise ParserFusionCompatibilityError(
                    "Parser observation bytes are not canonical."
                )
            if fusion_artifact.to_json_bytes() != extraction.fusion_artifact:
                raise ParserFusionCompatibilityError(
                    "Parser fusion bytes are not canonical."
                )
            fusion_artifact.validate_against_observation_set(observation_set)
        raw_payloads = dict(extraction.raw_artifacts)
        if extraction.raw_artifact is not None:
            raw_payloads[extraction.backend] = extraction.raw_artifact
        raw_parser_items = tuple(
            (backend, f"{prefix}/parser/{backend}.json", payload)
            for backend, payload in sorted(raw_payloads.items())
        )
        raw_parser_keys = tuple(key for _backend, key, _payload in raw_parser_items)
        raw_parser_key = next(
            (
                key
                for backend, key, _payload in raw_parser_items
                if backend == extraction.backend
            ),
            raw_parser_keys[0] if raw_parser_keys else None,
        )
        self._put_immutable_json(document_key, document.to_dict())
        payload = "".join(
            json.dumps(item.to_dict(), sort_keys=True) + "\n" for item in evidence
        ).encode()
        if not self._storage.exists(evidence_key):
            self._storage.put_bytes(
                evidence_key, payload, media_type="application/x-ndjson"
            )
        if observation_set is not None:
            self._put_immutable_bytes(
                observation_artifact_key,
                observation_set.to_json_bytes(),
                media_type="application/json",
            )
        if fusion_artifact is not None:
            self._put_immutable_bytes(
                fusion_artifact_key,
                fusion_artifact.to_json_bytes(),
                media_type="application/json",
            )
        native_store = NativeArtifactStore(self._storage, context)
        raw_descriptors = {}
        for backend, key, raw_payload in raw_parser_items:
            artifact_name = _raw_parser_artifact_name(extraction.backend, backend)
            artifact_id = f"art-{document.document_id}-{artifact_name}"
            raw_descriptors[backend] = native_store.store(
                artifact_id=artifact_id,
                parser_id=backend,
                parser_version=_raw_parser_version(extraction, backend),
                payload=raw_payload,
                media_type="application/json",
                payload_key=key,
            )
        canonical_content = CanonicalContentBuilder().build(
            document,
            asset,
            context,
            native_descriptors=tuple(raw_descriptors.values()),
            artifact_descriptors=(
                CanonicalArtifactDescriptor(
                    artifact_id=f"art-{document.document_id}-document",
                    role="document",
                    uri=self._artifact_uri(document_key),
                    media_type="application/json",
                    schema_version=DOCUMENT_SCHEMA_VERSION,
                ),
                CanonicalArtifactDescriptor(
                    artifact_id=f"art-{document.document_id}-evidence",
                    role="evidence",
                    uri=self._artifact_uri(evidence_key),
                    media_type="application/x-ndjson",
                    schema_version=EVIDENCE_SCHEMA_VERSION,
                ),
                CanonicalArtifactDescriptor(
                    artifact_id=f"art-{document.document_id}-canonical_content",
                    role="canonical_content",
                    uri=self._artifact_uri(canonical_content_key),
                    media_type="application/json",
                    schema_version=CANONICAL_CONTENT_SCHEMA_VERSION,
                ),
                CanonicalArtifactDescriptor(
                    artifact_id=f"art-{document.document_id}-provenance",
                    role="provenance",
                    uri=self._artifact_uri(provenance_key),
                    media_type="application/json",
                    schema_version=PROVENANCE_SCHEMA_VERSION,
                ),
                CanonicalArtifactDescriptor(
                    artifact_id=f"art-{document.document_id}-manifest",
                    role="manifest",
                    uri=self._artifact_uri(manifest_key),
                    media_type="application/json",
                    schema_version=DOCUMENT_SCHEMA_VERSION,
                ),
                *(
                    (
                        CanonicalArtifactDescriptor(
                            artifact_id=(
                                f"art-{document.document_id}-parser_observations"
                            ),
                            role="parser_observations",
                            uri=self._artifact_uri(observation_artifact_key),
                            media_type="application/json",
                            schema_version=observation_set.schema,
                        ),
                    )
                    if observation_set is not None
                    else ()
                ),
                *(
                    (
                        CanonicalArtifactDescriptor(
                            artifact_id=(
                                f"art-{document.document_id}-parser_fusion_decisions"
                            ),
                            role="parser_fusion_decisions",
                            uri=self._artifact_uri(fusion_artifact_key),
                            media_type="application/json",
                            schema_version=fusion_artifact.schema,
                        ),
                    )
                    if fusion_artifact is not None
                    else ()
                ),
            ),
        )
        native_descriptor_map = {
            descriptor.artifact_id: descriptor
            for descriptor in raw_descriptors.values()
        }
        self._put_immutable_bytes(
            canonical_content_key,
            canonical_content.to_json_bytes(
                native_descriptors=native_descriptor_map,
            ),
            media_type="application/json",
        )
        source_graph = SourceGraphBuilder().build((canonical_content,))
        address_catalog = build_strong_address_catalog(
            source_graph, (canonical_content,)
        )
        self._put_immutable_bytes(
            source_graph_key,
            source_graph.to_json_bytes(),
            media_type="application/json",
        )
        self._put_immutable_bytes(
            provenance_addresses_key,
            address_catalog.to_json_bytes(),
            media_type="application/json",
        )
        relation_records = [
            {**item.to_dict(), "gold": _relation_is_gold(item)}
            for item in document.relations
        ]
        unresolved_records = [
            {**item.to_dict(), "gold": False} for item in document.unresolved
        ]
        ambiguous_records = [
            item for item in unresolved_records if item["status"] == "ambiguous"
        ]
        artifact_uris = {
            "document": self._artifact_uri(document_key),
            "evidence": self._artifact_uri(evidence_key),
            "canonical_content": self._artifact_uri(canonical_content_key),
            "source_graph": self._artifact_uri(source_graph_key),
            "provenance_addresses": self._artifact_uri(
                provenance_addresses_key
            ),
            "provenance": self._artifact_uri(provenance_key),
            "manifest": self._artifact_uri(manifest_key),
            "parser": {
                backend: self._artifact_uri(key)
                for backend, key, _payload in raw_parser_items
            },
        }
        if fusion_artifact is not None:
            artifact_uris["parser_fusion_decisions"] = self._artifact_uri(
                fusion_artifact_key
            )
        if observation_set is not None:
            artifact_uris["parser_observations"] = self._artifact_uri(
                observation_artifact_key
            )
        provenance = {
            "schema": "cognityx.ingest.provenance",
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "document_id": document.document_id,
            "run_id": context.run_id,
            "job_id": job_id,
            "document": {
                "document_id": document.document_id,
                "title": document.title,
                "aliases": list(document.aliases),
                "schema_version": document.schema_version,
            },
            "source_asset": {
                "asset_id": asset.asset_id,
                "bundle_id": asset.bundle_id,
                "context_id": asset.context_id,
                "blob_sha256": asset.sha256,
                "logical_uri": document.source.storage_key,
                "filename": asset.original_filename,
                "media_type": asset.media_type,
                "size_bytes": asset.size_bytes,
                "storage_role": "source_asset",
            },
            "aliases": list(document.aliases),
            "lineage": {
                "run_id": context.run_id,
                "job_id": job_id,
                "context_id": asset.context_id,
                "bundle_id": asset.bundle_id,
                "asset_id": asset.asset_id,
                "source_sha256": asset.sha256,
                "document_id": document.document_id,
            },
            "artifact_uris": artifact_uris,
            "artifact_storage_role": "artifacts",
            "pages": [item.to_dict() for item in document.pages],
            "blocks": [item.to_dict() for item in document.blocks],
            "repeated_regions": [
                item.to_dict() for item in document.repeated_regions
            ],
            "sections": [item.to_dict() for item in document.sections],
            "objects": [item.to_dict() for item in document.objects],
            "evidence": [item.to_dict() for item in evidence],
            "relations": relation_records,
            "decisions": [item.to_dict() for item in document.decisions],
            "ambiguous": ambiguous_records,
            "unresolved": unresolved_records,
            "parser": {
                "selected": extraction.backend,
                "version": extraction.backend_version,
                "considered": list(extraction.considered_backends),
                "selected_reason": extraction.selected_reason,
                "diagnostics": dict(extraction.diagnostics),
                "raw_artifacts": [
                    {
                        "artifact_id": raw_descriptors[backend].artifact_id,
                        "backend": backend,
                        "parser_id": raw_descriptors[backend].parser_id,
                        "parser_version": raw_descriptors[backend].parser_version,
                        "sha256": raw_descriptors[backend].sha256,
                        "size_bytes": raw_descriptors[backend].size_bytes,
                        "media_type": raw_descriptors[backend].media_type,
                        "uri": raw_descriptors[backend].uri,
                        "descriptor_uri": native_store.descriptor_uri(
                            raw_descriptors[backend].artifact_id
                        ),
                        "retention_class": raw_descriptors[
                            backend
                        ].retention_class,
                        "native_pointers": list(
                            raw_descriptors[backend].native_pointers
                        ),
                    }
                    for backend, _key, _payload in raw_parser_items
                ],
                **(
                    {
                        "observations": {
                            "observation_schema": observation_set.schema,
                            "observation_set_id": observation_set.observation_set_id,
                            "observation_artifact_uri": self._artifact_uri(
                                observation_artifact_key
                            ),
                            "sha256": hashlib.sha256(
                                observation_set.to_json_bytes()
                            ).hexdigest(),
                            "observation_count": len(observation_set.observations),
                            "parser_ids": list(observation_set.parser_ids),
                        }
                    }
                    if observation_set is not None
                    else {}
                ),
                **(
                    {
                        "fusion": {
                            "fusion_schema": fusion_artifact.schema,
                            "fusion_artifact_uri": self._artifact_uri(
                                fusion_artifact_key
                            ),
                            "observation_set_id": fusion_artifact.observation_set_id,
                            "observation_set_sha256": fusion_artifact.observation_set_sha256,
                            "source_backends": list(
                                fusion_artifact.source_backends
                            ),
                            "state_counts": dict(fusion_artifact.state_counts),
                            "unresolved_count": dict(
                                fusion_artifact.state_counts
                            )["unresolved"],
                            "conflict_count": dict(
                                fusion_artifact.state_counts
                            )["conflict"],
                        }
                    }
                    if fusion_artifact is not None
                    else {}
                ),
            },
        }
        self._put_immutable_json(provenance_key, provenance)
        stored = {
            "document": self._storage.stat(document_key),
            "evidence": self._storage.stat(evidence_key),
            "canonical_content": self._storage.stat(canonical_content_key),
            "source_graph": self._storage.stat(source_graph_key),
            "provenance_addresses": self._storage.stat(
                provenance_addresses_key
            ),
            "provenance": self._storage.stat(provenance_key),
        }
        if fusion_artifact is not None:
            stored["parser_fusion_decisions"] = self._storage.stat(
                fusion_artifact_key
            )
        if observation_set is not None:
            stored["parser_observations"] = self._storage.stat(
                observation_artifact_key
            )
        for backend, key, _payload in raw_parser_items:
            name = _raw_parser_artifact_name(extraction.backend, backend)
            stored[name] = self._storage.stat(key)
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
            "provenance_schema_version": PROVENANCE_SCHEMA_VERSION,
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
            provenance_key=provenance_key,
            raw_parser_key=raw_parser_key,
            raw_parser_keys=raw_parser_keys,
            canonical_content_key=canonical_content_key,
            observation_artifact_key=observation_artifact_key,
            fusion_artifact_key=fusion_artifact_key,
            source_graph_key=source_graph_key,
            provenance_addresses_key=provenance_addresses_key,
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

    def _put_immutable_bytes(
        self,
        key: str,
        payload: bytes,
        *,
        media_type: str,
    ) -> None:
        """Publish canonical serializer bytes without normalizing retry content.

        ``_persist`` calls this for ``canonical-content.json`` after the aggregate
        validates against real T01 descriptors. The method compares an existing
        object byte-for-byte and verifies its media type; equivalent retries are
        idempotent, while any changed bytes or metadata fail. A concurrent writer
        that wins create-only Storage publication is checked by the same path.
        Source text is never included in conflict diagnostics.
        """
        if self._storage.exists(key):
            self._verify_immutable_bytes(key, payload, media_type=media_type)
            return
        try:
            self._storage.put_bytes(key, payload, media_type=media_type)
        except ObjectAlreadyExistsError:
            self._verify_immutable_bytes(key, payload, media_type=media_type)

    def _verify_immutable_bytes(
        self,
        key: str,
        expected: bytes,
        *,
        media_type: str,
    ) -> None:
        """Require an existing immutable object to match exact bytes and media type.

        Idempotent retries and publication-race losers call this read-only helper.
        It performs no decoding or JSON comparison, so stored canonical formatting
        remains part of artifact identity. A mismatch raises without rewriting the
        object or exposing source content.
        """
        with self._storage.open(key) as current:
            actual = current.read()
        stored = self._storage.stat(key)
        if actual != expected or stored.media_type != media_type:
            raise RuntimeError(f"Immutable ingest artifact already exists: {key}")

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
        """Convert one owned logical key to its configured Storage URI.

        Ingest persistence and manifest projection call this pure adapter after a
        key has been selected. It delegates URI construction to the configured
        Storage client when available and otherwise emits the compatibility
        ``storage://`` form. It never resolves a physical path, reads payloads, or
        mutates state. Equal keys return equal URIs; Storage owns namespace trust
        and callers must share the service only when that client is thread-safe.
        """
        uri = getattr(self._storage, "uri", None)
        return uri(storage_key) if uri is not None else f"storage://{storage_key}"

    def _dataforge_source_ref(self, result: IngestResult) -> dict[str, str]:
        """Project one successful T08 result into a text-free DataForge bundle.

        ``ingest_assets`` calls this only after canonical content, Source Graph,
        provenance addresses, and provenance have been persisted successfully.
        The algorithm preserves result order and derives every value from the same
        document-local keys used by ``provenance.json`` so the two projections
        agree exactly. The returned closed mapping contains logical Storage URIs
        and immutable identity only: no local path, parser-native URI, source text,
        evidence, question, answer, claim, or Knowledge Unit can enter the field.
        It performs no I/O, is deterministic and idempotent, and raises normal
        attribute/type failures only for an internally incomplete successful
        result. The mapping is newly allocated per call; sharing the service has
        the same thread-safety assumptions as ``_artifact_uri``.
        """
        return {
            "document_id": result.document.document_id,
            "provenance_uri": self._artifact_uri(result.provenance_key),
            "canonical_content_uri": self._artifact_uri(
                result.canonical_content_key
            ),
            "source_graph_uri": self._artifact_uri(result.source_graph_key),
            "provenance_addresses_uri": self._artifact_uri(
                result.provenance_addresses_key
            ),
        }

    @staticmethod
    def _stored_uri(stored: object) -> str:
        uri = str(stored.uri)
        return uri if uri.startswith("storage://") else f"storage://{stored.key}"


def _relation_is_gold(relation: Relation) -> bool:
    """Mark only concrete, non-ambiguous relation observations as usable truth."""
    return relation.status in {"observed", "resolved"} and bool(
        relation.target_anchor_id
    )


def _raw_parser_artifact_name(selected_backend: str, backend: str) -> str:
    """Retain the legacy manifest name used to derive public artifact IDs."""
    if backend == selected_backend:
        return "parser_raw"
    return f"parser_raw_{backend.replace('-', '_')}"


def _raw_parser_version(
    extraction: ExtractionResult, backend: str
) -> str | None:
    """Use each contributor's version without assigning a fusion wrapper version."""
    if backend == extraction.backend:
        return extraction.backend_version
    versions = extraction.diagnostics.get("backend_versions")
    if not isinstance(versions, Mapping):
        return None
    value = versions.get(backend)
    return value if isinstance(value, str) and value else None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_structure(
    document_id: str,
    extraction: ExtractionResult,
    table_observations: tuple[ObservedLogicalTable, ...] = (),
    object_observations: ObjectObservations = ObjectObservations(),
) -> tuple[tuple[PageRecord, ...], tuple[Block, ...], tuple[DocumentObject, ...]]:
    pages: list[PageRecord] = []
    blocks: list[Block] = []
    objects: list[DocumentObject] = []
    terminal_splits = terminal_sentence_split_block_ids(extraction.pages)
    table_parts, table_captions = table_source_groups(table_observations)
    grouped_lists = _heading_adjacent_list_ids(extraction.pages)
    linked_blocks = _linked_block_ids(extraction.pages)
    used_parser_objects = {
        item.image.object_id for item in object_observations.figures
    }
    for sequence, page in enumerate(extraction.pages, start=1):
        page_id = f"{document_id}:page-index:{page.physical_page_index}"
        extracted_blocks = page.blocks or ()
        page_block_ids: list[str] = []
        if extracted_blocks:
            content_order = 0
            repeated_orders = {"page_header": 0, "page_footer": 0}
            pending_figures = sorted(
                (
                    item
                    for item in object_observations.figures
                    if item.page_index == page.physical_page_index
                ),
                key=lambda item: (item.image.bbox or (0, 0, 0, 0))[1],
            )
            for item in extracted_blocks:
                if item.block_type not in {"page_header", "page_footer"}:
                    while (
                        pending_figures
                        and item.bbox is not None
                        and pending_figures[0].image.bbox is not None
                        and pending_figures[0].image.bbox[1] <= item.bbox[1]
                    ):
                        figure = pending_figures.pop(0)
                        content_order += 1
                        figure_block_id = f"{page_id}:block:{content_order}"
                        page_block_ids.append(figure_block_id)
                        blocks.append(
                            Block(
                                block_id=figure_block_id,
                                page_id=page_id,
                                block_type="figure",
                                reading_order=content_order,
                                text=f"Figure {figure.number} image",
                                bbox=figure.image.bbox,
                                method="parser_object_geometry",
                                confidence=figure.image.confidence,
                                source_backends=(
                                    figure.image.source_backends
                                    or (extraction.backend,)
                                ),
                                fact_sources=figure.image.fact_sources,
                            )
                        )
                part_value = table_parts.get(item.block_id)
                if (
                    part_value is not None
                    and item.block_id != part_value[1].source_block_ids[0]
                ):
                    continue
                table = table_captions.get(item.block_id)
                if part_value is not None:
                    part_table, part = part_value
                    fragment_values = (
                        CanonicalBlockFragment(
                            text=(
                                f"Table {part_table.number} part {part.part_number}"
                            ),
                            block_type="table_part",
                            method="deterministic_table_part_assembly",
                        ),
                    )
                    observed_bbox = part.bbox
                elif table is not None:
                    fragment_values = (
                        CanonicalBlockFragment(
                            text=table.caption,
                            block_type="caption",
                            method="deterministic_table_caption",
                        ),
                    )
                    observed_bbox = item.bbox
                elif item.block_id in linked_blocks:
                    fragment_values = (
                        CanonicalBlockFragment(
                            text=item.text,
                            block_type="hyperlink",
                            method="native_link_geometry",
                        ),
                    )
                    observed_bbox = item.bbox
                else:
                    fragment_values = canonical_block_fragments(
                        item.text,
                        item.block_type,
                        split_terminal_sentence=item.block_id in terminal_splits,
                        split_list_items=item.block_id not in grouped_lists,
                    )
                    observed_bbox = item.bbox
                for fragment in fragment_values:
                    block_type = fragment.block_type
                    if block_type in repeated_orders:
                        repeated_orders[block_type] += 1
                        block_id = (
                            f"{page_id}:{block_type.replace('_', '-')}"
                            f":{repeated_orders[block_type]}"
                        )
                        reading_order = item.reading_order
                        method = item.method
                        confidence = item.confidence
                    else:
                        content_order += 1
                        block_id = f"{page_id}:block:{content_order}"
                        reading_order = content_order
                        method = fragment.method
                        confidence = fragment.confidence
                    page_block_ids.append(block_id)
                    blocks.append(
                        Block(
                            block_id=block_id,
                            page_id=page_id,
                            block_type=block_type,
                            reading_order=reading_order,
                            text=fragment.text,
                            bbox=observed_bbox,
                            method=method,
                            confidence=confidence,
                            source_backends=(
                                item.source_backends or (extraction.backend,)
                            ),
                            fact_sources=item.fact_sources,
                        )
                    )
            for figure in pending_figures:
                content_order += 1
                figure_block_id = f"{page_id}:block:{content_order}"
                page_block_ids.append(figure_block_id)
                blocks.append(
                    Block(
                        block_id=figure_block_id,
                        page_id=page_id,
                        block_type="figure",
                        reading_order=content_order,
                        text=f"Figure {figure.number} image",
                        bbox=figure.image.bbox,
                        method="parser_object_geometry",
                        confidence=figure.image.confidence,
                        source_backends=(
                            figure.image.source_backends or (extraction.backend,)
                        ),
                        fact_sources=figure.image.fact_sources,
                    )
                )
        else:
            block_id = f"{page_id}:block:1"
            page_block_ids.append(block_id)
            blocks.append(
                Block(
                    block_id=block_id,
                    page_id=page_id,
                    block_type="text",
                    reading_order=1,
                    text=page.text,
                    method="baseline_page_text",
                    confidence=1.0,
                    source_backends=(page.source_backends or (extraction.backend,)),
                    fact_sources=page.fact_sources,
                )
            )
        for order, item in enumerate(page.objects, start=1):
            if item.object_id in used_parser_objects:
                continue
            objects.append(
                DocumentObject(
                    object_id=f"{page_id}:{item.object_type}:{order}",
                    object_type=item.object_type,
                    page_id=page_id,
                    caption=item.caption,
                    text=item.text,
                    bbox=item.bbox,
                    source_backends=(item.source_backends or (extraction.backend,)),
                    fact_sources=item.fact_sources,
                    method=item.method,
                    confidence=item.confidence,
                )
            )
        pages.append(
            PageRecord(
                page_id=page_id,
                physical_page_index=page.physical_page_index,
                sequence_number=sequence,
                pdf_page_label=page.page_label,
                printed_page_label=page.printed_page_label,
                width=page.width,
                height=page.height,
                block_ids=tuple(page_block_ids),
                source_backends=(page.source_backends or (extraction.backend,)),
                fact_sources=page.fact_sources,
            )
        )
    return tuple(pages), tuple(blocks), tuple(objects)


def _heading_adjacent_list_ids(pages: tuple[ExtractedPage, ...]) -> frozenset[str]:
    grouped: set[str] = set()
    for page in pages:
        content = tuple(
            block
            for block in page.blocks
            if block.block_type not in {"page_header", "page_footer"}
        )
        for position, block in enumerate(content):
            if canonical_block_type(block.text, block.block_type) != "list":
                continue
            neighbors = content[max(0, position - 1) : position] + content[
                position + 1 : position + 2
            ]
            if any(canonical_block_type(item.text, item.block_type) == "heading" for item in neighbors):
                grouped.add(block.block_id)
    return frozenset(grouped)


def _linked_block_ids(pages: tuple[ExtractedPage, ...]) -> frozenset[str]:
    linked: set[str] = set()
    for page in pages:
        relation_boxes = tuple(
            relation.bbox for relation in page.relations if relation.bbox is not None
        )
        for block in page.blocks:
            if block.bbox is not None and any(
                _bbox_intersects(block.bbox, bbox) for bbox in relation_boxes
            ):
                linked.add(block.block_id)
    return frozenset(linked)


def _bbox_intersects(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    overlap_x = min(first[2], second[2]) - max(first[0], second[0])
    overlap_y = min(first[3], second[3]) - max(first[1], second[1])
    return overlap_x > 1.0 and overlap_y > 1.0


def _relations_and_tasks(
    document_id: str,
    extraction: ExtractionResult,
    page_by_index: dict[int, str],
    ignored_relation_ids: frozenset[str] = frozenset(),
) -> tuple[tuple[Relation, ...], tuple[ResolutionTask, ...]]:
    relations: list[Relation] = []
    tasks: list[ResolutionTask] = []
    for page in extraction.pages:
        default_source = page_by_index[page.physical_page_index]
        for index, item in enumerate(page.relations, start=1):
            if item.relation_id in ignored_relation_ids:
                continue
            source = _parser_anchor(item.source_anchor, page_by_index) or default_source
            target = _parser_anchor(item.target_anchor, page_by_index)
            relation_id = f"{document_id}:relation:{page.physical_page_index}:{index}"
            if target is not None and item.status in {"resolved", "observed"}:
                relations.append(
                    Relation(
                        relation_id=relation_id,
                        source_anchor_id=source,
                        target_anchor_id=target,
                        relation_type=item.relation_type,
                        status="resolved",
                        target_text=item.target_text,
                        method=item.method,
                        confidence=item.confidence,
                        source_backends=(
                            item.source_backends or (extraction.backend,)
                        ),
                        fact_sources=item.fact_sources,
                    )
                )
            else:
                tasks.append(
                    ResolutionTask(
                        task_id=relation_id,
                        source_anchor_id=source,
                        relation_type=item.relation_type,
                        target_text=item.target_text,
                        context={
                            "parser_method": item.method,
                            "parser_confidence": item.confidence,
                        },
                    )
                )
    return tuple(relations), tuple(tasks)


def _canonical_repeated_regions(
    document_id: str,
    pages: tuple[PageRecord, ...],
    blocks: tuple[Block, ...],
) -> tuple[RepeatedRegion, ...]:
    page_index = {page.page_id: page.physical_page_index for page in pages}
    grouped: dict[tuple[str, str], list[Block]] = {}
    for block in blocks:
        if block.block_type not in {"page_header", "page_footer"}:
            continue
        key = (
            block.block_type.removeprefix("page_"),
            normalize_repeated_region_text(block.text),
        )
        grouped.setdefault(key, []).append(block)

    return tuple(
        RepeatedRegion(
            region_id=f"{document_id}:repeated-region:{index}",
            region_type=region_type,
            normalized_text=normalized_text,
            occurrences=tuple(
                RepeatedRegionOccurrence(
                    page_id=block.page_id,
                    physical_page_index=page_index[block.page_id],
                    source_page_id=block.page_id,
                    source_block_id=block.block_id,
                    text=block.text,
                )
                for block in region_blocks
            ),
        )
        for index, ((region_type, normalized_text), region_blocks) in enumerate(
            grouped.items(), start=1
        )
    )


def _canonical_sections(
    document_id: str,
    title: str,
    extraction: ExtractionResult,
    pages: tuple[PageRecord, ...],
    blocks: tuple[Block, ...],
    evidence: tuple[Evidence, ...],
) -> tuple[Section, ...]:
    structured = build_sections(document_id, pages, blocks, evidence)
    if structured:
        return structured
    content_block_ids = {
        item.block_id
        for item in blocks
        if item.block_type not in {"page_header", "page_footer"}
    }
    if not extraction.sections:
        return (
            Section(
                section_id=f"{document_id}:section:1",
                title=title,
                evidence_ids=tuple(item.evidence_id for item in evidence),
                page_ids=tuple(item.page_id for item in pages),
                block_ids=tuple(
                    item.block_id for item in blocks if item.block_id in content_block_ids
                ),
                method="deterministic_page_sequence",
            ),
        )
    page_map = {item.physical_page_index: item for item in pages}
    evidence_map = {
        item.physical_page_index: item
        for item in evidence
        if item.physical_page_index is not None
    }
    result: list[Section] = []
    for index, item in enumerate(extraction.sections, start=1):
        selected_pages = tuple(
            page_map[page_index]
            for page_index in range(item.start_page_index, item.end_page_index + 1)
            if page_index in page_map
        )
        selected_evidence = tuple(
            evidence_map[page.physical_page_index].evidence_id
            for page in selected_pages
            if page.physical_page_index in evidence_map
        )
        selected_blocks = tuple(
            block_id
            for page in selected_pages
            for block_id in page.block_ids
            if block_id in content_block_ids
        )
        result.append(
            Section(
                section_id=f"{document_id}:section:{index}",
                title=item.title,
                evidence_ids=selected_evidence,
                page_ids=tuple(page.page_id for page in selected_pages),
                block_ids=selected_blocks,
                method=item.method,
                confidence=item.confidence,
            )
        )
    return tuple(result)


def _parser_anchor(
    value: str | None, page_by_index: dict[int, str]
) -> str | None:
    if value is None:
        return None
    if value.startswith("page:"):
        try:
            return page_by_index.get(int(value.split(":", 1)[1]))
        except ValueError:
            return None
    return value if value in page_by_index.values() else None
