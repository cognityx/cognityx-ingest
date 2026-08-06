"""Define stable ingestion records independent of parser and model providers.

This module exists to keep the established v2 document, evidence, lifecycle, and
result contracts readable while parser implementations evolve. Its core approach
is immutable typed records with explicit dictionary projections. Compatibility is
the governing design principle: v3.2's generalized source model lives separately
in ``canonical_content`` and is exposed here only through an additive result key.
Ingest services, CLI adapters, DataForge integrations, and existing Python callers
use these records.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from cognityx_resource import ExecutionContext, ResourceRef

CANONICAL_SCHEMA = "cognityx.ingest.document"


class IngestJobState(StrEnum):
    """Ingest-owned lifecycle states, independent of a jobs backend."""

    SUBMITTED = "submitted"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class SourceAssetContext:
    """Durable governance context for SourceAsset resources."""

    context_id: str
    context_type: str
    descriptors: dict[str, str]
    created_at: str


@dataclass(frozen=True, slots=True)
class DocBundle:
    """Logical collection of SourceAssets within one Context."""

    bundle_id: str
    context_id: str
    name: str
    parent_bundle_id: str | None
    path: str
    created_by: str | None
    created_at: str
    updated_at: str
    deleted_at: str | None = None
    deleted_by: str | None = None
    delete_run_id: str | None = None
    delete_reason: str | None = None

    @property
    def ref(self) -> ResourceRef:
        """Return the cross-service reference to this DocBundle."""
        return ResourceRef(
            resource_type="doc_bundle",
            resource_id=self.bundle_id,
            context_id=self.context_id,
        )


@dataclass(frozen=True, slots=True)
class SourceAsset:
    """One registered external digital object backed by immutable bytes."""

    source_id: str
    context_id: str
    bundle_id: str
    original_filename: str
    media_type: str
    size_bytes: int
    sha256: str
    blob_id: str
    created_by: str | None
    created_at: str
    deleted_at: str | None = None
    deleted_by: str | None = None
    delete_run_id: str | None = None
    delete_reason: str | None = None

    @property
    def asset_id(self) -> str:
        """Return the canonical API name for the stable ``src-...`` ID."""
        return self.source_id

    @property
    def ref(self) -> ResourceRef:
        """Return the cross-service reference to this SourceAsset."""
        return ResourceRef(
            resource_type="source_asset",
            resource_id=self.asset_id,
            context_id=self.context_id,
        )


@dataclass(frozen=True, slots=True)
class SourceAssetRegistrationResult:
    """Outcome of SourceAsset registration without exposing caller paths."""

    context_id: str
    bundle_id: str
    source_id: str
    sha256: str
    size_bytes: int
    status: str

    @property
    def asset_id(self) -> str:
        """Return the canonical API name for the stable ``src-...`` ID."""
        return self.source_id


@dataclass(frozen=True, slots=True)
class SourceAssetBatchItem:
    """Safe per-entry outcome from directory SourceAsset registration."""

    relative_path: str
    bundle_path: str
    asset_id: str | None
    status: str
    error_category: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class SourceAssetBatchResult:
    """Aggregate outcome of one synchronous directory registration."""

    batch_id: str
    context_id: str
    root_bundle_id: str
    root_bundle_path: str
    structure: str
    recursive: bool
    files_discovered: int
    files_processed: int
    created_count: int
    restored_count: int
    already_registered_count: int
    failed_count: int
    skipped_count: int
    items: tuple[SourceAssetBatchItem, ...]


@dataclass(frozen=True, slots=True)
class SourceAssetDeletionResult:
    context_id: str
    bundle_id: str
    asset_id: str
    blob_id: str
    deleted_at: str
    status: str
    blob_still_referenced: bool


@dataclass(frozen=True, slots=True)
class DocBundleDeletionResult:
    context_id: str
    bundle_id: str
    deleted_asset_count: int
    deleted_bundle_count: int
    deleted_at: str
    status: str


@dataclass(frozen=True, slots=True)
class SourceAssetLocation:
    """Read-only physical-location diagnostics for one SourceAsset."""

    source_id: str
    blob_id: str
    blob_uri: str
    backend: str
    local_path: str | None
    profile_name: str | None = None

    @property
    def asset_id(self) -> str:
        """Return the canonical API name for the stable ``src-...`` ID."""
        return self.source_id


# Compatibility aliases retain one implementation and stable constructor fields.
SourceContext = SourceAssetContext
SourceBundle = DocBundle
RegisteredSource = SourceAsset
SourceRegistrationResult = SourceAssetRegistrationResult
SourceLocation = SourceAssetLocation


@dataclass(frozen=True, slots=True)
class UsageReport:
    """Facts measured by the ingest execution rather than policy assertions."""

    run_id: str
    job_id: str | None = None
    documents: int = 0
    pages: int | None = None
    input_bytes: int = 0
    output_bytes: int = 0
    duration_ms: int = 0
    service: str = "cognityx-ingest"
    metrics: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """Stable artifact identity and logical storage location."""

    artifact_id: str
    uri: str
    media_type: str


@dataclass(frozen=True, slots=True)
class SourceAnchor:
    """Stable address for an observed region of the original document."""

    anchor_id: str
    document_id: str
    page_index: int
    block_id: str | None = None
    char_start: int | None = None
    char_end: int | None = None


@dataclass(frozen=True, slots=True)
class PageRecord:
    page_id: str
    physical_page_index: int
    sequence_number: int
    pdf_page_label: str | None = None
    printed_page_label: str | None = None
    width: float | None = None
    height: float | None = None
    block_ids: tuple[str, ...] = ()
    source_backends: tuple[str, ...] = ()
    fact_sources: Mapping[str, tuple[Mapping[str, Any], ...]] = field(
        default_factory=dict, hash=False
    )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["block_ids"] = list(self.block_ids)
        value["source_backends"] = list(self.source_backends)
        value["fact_sources"] = {
            key: list(sources) for key, sources in self.fact_sources.items()
        }
        return value


@dataclass(frozen=True, slots=True)
class Block:
    block_id: str
    page_id: str
    block_type: str
    reading_order: int
    text: str
    bbox: tuple[float, float, float, float] | None = None
    method: str = "parser"
    confidence: float | None = None
    source_backends: tuple[str, ...] = ()
    fact_sources: Mapping[str, tuple[Mapping[str, Any], ...]] = field(
        default_factory=dict, hash=False
    )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["bbox"] = list(self.bbox) if self.bbox is not None else None
        value["source_backends"] = list(self.source_backends)
        value["fact_sources"] = {
            key: list(sources) for key, sources in self.fact_sources.items()
        }
        return value


@dataclass(frozen=True, slots=True)
class RepeatedRegionOccurrence:
    page_id: str
    physical_page_index: int
    source_page_id: str
    source_block_id: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RepeatedRegion:
    region_id: str
    region_type: str
    normalized_text: str
    occurrences: tuple[RepeatedRegionOccurrence, ...]
    detection_method: str = "deterministic_repeated_margin"
    status: str = "deterministic"
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["occurrences"] = [item.to_dict() for item in self.occurrences]
        return value


@dataclass(frozen=True, slots=True)
class TableCell:
    column_index: int
    column_name: str
    text: str
    source_anchor_ids: tuple[str, ...] = ()
    parser_source_anchor_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["source_anchor_ids"] = list(self.source_anchor_ids)
        value["parser_source_anchor_ids"] = list(self.parser_source_anchor_ids)
        return value


@dataclass(frozen=True, slots=True)
class TableRow:
    row_number: int | None
    row_type: str
    cells: tuple[TableCell, ...] = ()
    text: str | None = None
    column_span: int = 1
    source_anchor_ids: tuple[str, ...] = ()
    parser_source_anchor_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["cells"] = [item.to_dict() for item in self.cells]
        value["source_anchor_ids"] = list(self.source_anchor_ids)
        value["parser_source_anchor_ids"] = list(self.parser_source_anchor_ids)
        return value


@dataclass(frozen=True, slots=True)
class TablePart:
    part_id: str
    page_id: str
    source_block_ids: tuple[str, ...]
    parser_source_anchor_ids: tuple[str, ...]
    row_start: int
    row_end: int
    repeated_header: bool
    merged_group_row: TableRow
    method: str = "deterministic_table_assembly"
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["source_block_ids"] = list(self.source_block_ids)
        value["parser_source_anchor_ids"] = list(self.parser_source_anchor_ids)
        value["merged_group_row"] = self.merged_group_row.to_dict()
        return value


@dataclass(frozen=True, slots=True)
class DocumentObject:
    object_id: str
    object_type: str
    page_id: str
    caption: str | None = None
    text: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    page_ids: tuple[str, ...] = ()
    owner_section_id: str | None = None
    source_anchor_ids: tuple[str, ...] = ()
    caption_anchor_id: str | None = None
    marker: str | None = None
    marker_anchor_id: str | None = None
    note_anchor_id: str | None = None
    image_anchor_id: str | None = None
    parser_source_anchor_ids: tuple[str, ...] = ()
    columns: tuple[str, ...] = ()
    rows: tuple[TableRow, ...] = ()
    parts: tuple[TablePart, ...] = ()
    source_backends: tuple[str, ...] = ()
    fact_sources: Mapping[str, tuple[Mapping[str, Any], ...]] = field(
        default_factory=dict, hash=False
    )
    method: str = "parser"
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["bbox"] = list(self.bbox) if self.bbox is not None else None
        value["page_ids"] = list(self.page_ids)
        value["source_anchor_ids"] = list(self.source_anchor_ids)
        value["parser_source_anchor_ids"] = list(self.parser_source_anchor_ids)
        value["columns"] = list(self.columns)
        value["rows"] = [item.to_dict() for item in self.rows]
        value["parts"] = [item.to_dict() for item in self.parts]
        value["source_backends"] = list(self.source_backends)
        value["fact_sources"] = {
            key: list(sources) for key, sources in self.fact_sources.items()
        }
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DocumentObject":
        selected = dict(value)
        selected["page_ids"] = tuple(value.get("page_ids", ()))
        selected["source_anchor_ids"] = tuple(value.get("source_anchor_ids", ()))
        selected["parser_source_anchor_ids"] = tuple(
            value.get("parser_source_anchor_ids", ())
        )
        selected["columns"] = tuple(value.get("columns", ()))
        selected["source_backends"] = tuple(value.get("source_backends", ()))
        selected["fact_sources"] = {
            key: tuple(sources)
            for key, sources in value.get("fact_sources", {}).items()
        }
        selected["rows"] = tuple(
            TableRow(
                **{
                    **dict(item),
                    "cells": tuple(
                        TableCell(
                            **{
                                **dict(cell),
                                "source_anchor_ids": tuple(
                                    cell.get("source_anchor_ids", ())
                                ),
                                "parser_source_anchor_ids": tuple(
                                    cell.get("parser_source_anchor_ids", ())
                                ),
                            }
                        )
                        for cell in item.get("cells", ())
                    ),
                    "source_anchor_ids": tuple(item.get("source_anchor_ids", ())),
                    "parser_source_anchor_ids": tuple(
                        item.get("parser_source_anchor_ids", ())
                    ),
                }
            )
            for item in value.get("rows", ())
        )
        selected["parts"] = tuple(
            TablePart(
                **{
                    **dict(item),
                    "source_block_ids": tuple(item.get("source_block_ids", ())),
                    "parser_source_anchor_ids": tuple(
                        item.get("parser_source_anchor_ids", ())
                    ),
                    "merged_group_row": TableRow(
                        **{
                            **dict(item["merged_group_row"]),
                            "cells": tuple(
                                TableCell(**dict(cell))
                                for cell in item["merged_group_row"].get("cells", ())
                            ),
                            "source_anchor_ids": tuple(
                                item["merged_group_row"].get("source_anchor_ids", ())
                            ),
                            "parser_source_anchor_ids": tuple(
                                item["merged_group_row"].get(
                                    "parser_source_anchor_ids", ()
                                )
                            ),
                        }
                    ),
                }
            )
            for item in value.get("parts", ())
        )
        return cls(**selected)


@dataclass(frozen=True, slots=True)
class Relation:
    relation_id: str
    source_anchor_id: str
    target_anchor_id: str | None
    relation_type: str
    status: str
    target_text: str | None = None
    method: str = "deterministic"
    confidence: float | None = None
    decision_id: str | None = None
    reason: str | None = None
    source_backends: tuple[str, ...] = ()
    fact_sources: Mapping[str, tuple[Mapping[str, Any], ...]] = field(
        default_factory=dict, hash=False
    )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["source_backends"] = list(self.source_backends)
        value["fact_sources"] = {
            key: list(sources) for key, sources in self.fact_sources.items()
        }
        return value


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    decision_id: str
    task_id: str
    status: str
    method: str
    considered_tools: tuple[str, ...] = ()
    invoked_tools: tuple[str, ...] = ()
    selected_tool: str | None = None
    selected_reason: str | None = None
    provider: str | None = None
    model: str | None = None
    backend: str | None = None
    profile: str | None = None
    server_profile: str | None = None
    request_id: str | None = None
    prompt_version: str | None = None
    configuration_hash: str | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    timings: Mapping[str, Any] = field(default_factory=dict)
    confidence: float | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["considered_tools"] = list(self.considered_tools)
        value["invoked_tools"] = list(self.invoked_tools)
        return value


@dataclass(frozen=True, slots=True)
class UnresolvedItem:
    task_id: str
    source_anchor_id: str
    relation_type: str
    target_text: str | None
    reason: str
    status: str = "unresolved"
    method: str = "deterministic"
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SourceRecord:
    source_id: str
    filename: str
    sha256: str
    size_bytes: int
    storage_key: str
    media_type: str = "application/pdf"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    document_id: str
    page_number: int
    text: str
    char_start: int
    char_end: int
    schema_version: str = "cognityx.ingest.evidence/v2"
    source_asset_id: str | None = None
    bundle_id: str | None = None
    context_id: str | None = None
    sequence_number: int | None = None
    source_sha256: str | None = None
    parser_name: str | None = None
    parser_version: str | None = None
    run_id: str | None = None
    physical_page_index: int | None = None
    pdf_page_label: str | None = None
    printed_page_label: str | None = None
    block_id: str | None = None
    anchor_id: str | None = None
    continues_from: str | None = None
    continues_to: str | None = None
    method: str = "observed"
    confidence: float | None = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Evidence":
        """Read both legacy v1 records and lineage-complete v2 records."""
        known = cls.__dataclass_fields__
        selected = {key: item for key, item in value.items() if key in known}
        selected.setdefault("schema_version", "cognityx.ingest.evidence/v1")
        return cls(**selected)


@dataclass(frozen=True, slots=True)
class Section:
    section_id: str
    title: str
    evidence_ids: tuple[str, ...]
    number: str | None = None
    level: int | None = None
    parent_section_id: str | None = None
    path: tuple[str, ...] = ()
    heading_block_id: str | None = None
    start_block_id: str | None = None
    end_block_id: str | None = None
    continuation_status: str | None = None
    continuation_method: str | None = None
    continuation_confidence: float | None = None
    page_ids: tuple[str, ...] = ()
    block_ids: tuple[str, ...] = ()
    continues_from: str | None = None
    continues_to: str | None = None
    method: str = "deterministic"
    confidence: float | None = 1.0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence_ids"] = list(self.evidence_ids)
        value["path"] = list(self.path)
        value["page_ids"] = list(self.page_ids)
        value["block_ids"] = list(self.block_ids)
        return value


@dataclass(frozen=True, slots=True)
class CanonicalDocument:
    document_id: str
    schema_version: str
    source: SourceRecord
    title: str
    sections: tuple[Section, ...]
    enhancement: dict[str, Any] | None = None
    schema: str = CANONICAL_SCHEMA
    aliases: tuple[str, ...] = ()
    pages: tuple[PageRecord, ...] = ()
    blocks: tuple[Block, ...] = ()
    repeated_regions: tuple[RepeatedRegion, ...] = ()
    objects: tuple[DocumentObject, ...] = ()
    relations: tuple[Relation, ...] = ()
    decisions: tuple[DecisionRecord, ...] = ()
    unresolved: tuple[UnresolvedItem, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "document_id": self.document_id,
            "schema_version": self.schema_version,
            "source": self.source.to_dict(),
            "title": self.title,
            "sections": [section.to_dict() for section in self.sections],
            "enhancement": self.enhancement,
            "aliases": list(self.aliases),
            "pages": [page.to_dict() for page in self.pages],
            "blocks": [block.to_dict() for block in self.blocks],
            "repeated_regions": [item.to_dict() for item in self.repeated_regions],
            "objects": [item.to_dict() for item in self.objects],
            "relations": [relation.to_dict() for relation in self.relations],
            "decisions": [decision.to_dict() for decision in self.decisions],
            "unresolved": [item.to_dict() for item in self.unresolved],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CanonicalDocument":
        """Read v1 documents while accepting the richer v2 fields."""
        source = SourceRecord(**dict(value["source"]))
        sections = tuple(
            Section(
                **{
                    **dict(item),
                    "evidence_ids": tuple(item.get("evidence_ids", ())),
                    "path": tuple(item.get("path", ())),
                    "page_ids": tuple(item.get("page_ids", ())),
                    "block_ids": tuple(item.get("block_ids", ())),
                }
            )
            for item in value.get("sections", ())
        )
        return cls(
            document_id=str(value["document_id"]),
            schema_version=str(value.get("schema_version", "cognityx.ingest.document/v1")),
            source=source,
            title=str(value.get("title", source.filename)),
            sections=sections,
            enhancement=value.get("enhancement"),
            schema=str(value.get("schema", CANONICAL_SCHEMA)),
            aliases=tuple(value.get("aliases", ())),
            pages=tuple(
                PageRecord(
                    **{
                        **dict(item),
                        "block_ids": tuple(item.get("block_ids", ())),
                        "source_backends": tuple(item.get("source_backends", ())),
                        "fact_sources": {
                            key: tuple(sources)
                            for key, sources in item.get("fact_sources", {}).items()
                        },
                    }
                )
                for item in value.get("pages", ())
            ),
            blocks=tuple(
                Block(
                    **{
                        **dict(item),
                        "bbox": (
                            tuple(item["bbox"]) if item.get("bbox") is not None else None
                        ),
                        "source_backends": tuple(item.get("source_backends", ())),
                        "fact_sources": {
                            key: tuple(sources)
                            for key, sources in item.get("fact_sources", {}).items()
                        },
                    }
                )
                for item in value.get("blocks", ())
            ),
            repeated_regions=tuple(
                RepeatedRegion(
                    **{
                        **dict(item),
                        "occurrences": tuple(
                            RepeatedRegionOccurrence(**dict(occurrence))
                            for occurrence in item.get("occurrences", ())
                        ),
                    }
                )
                for item in value.get("repeated_regions", ())
            ),
            objects=tuple(
                DocumentObject.from_dict(item) for item in value.get("objects", ())
            ),
            relations=tuple(
                Relation(
                    **{
                        **dict(item),
                        "source_backends": tuple(item.get("source_backends", ())),
                        "fact_sources": {
                            key: tuple(sources)
                            for key, sources in item.get("fact_sources", {}).items()
                        },
                    }
                )
                for item in value.get("relations", ())
            ),
            decisions=tuple(DecisionRecord(**dict(item)) for item in value.get("decisions", ())),
            unresolved=tuple(UnresolvedItem(**dict(item)) for item in value.get("unresolved", ())),
        )


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Return one completed document's compatibility and additive artifact handles.

    Responsibility:
        Keep the established document, evidence, manifest, provenance, parser, and
        usage results while exposing T02 canonical content and T05 parser-fusion
        decisions additively.
    Constructed by:
        ``IngestService`` after all immutable document artifacts persist.
    Used by:
        Existing Python callers, run aggregation, usage accounting, and future
        readers that opt into the v3.2 artifact.
    Invariants:
        Existing positional constructor fields and artifact identities remain
        unchanged; every newly added field has a backward-compatible default.
    Lifecycle/persistence:
        This frozen in-memory result references Storage objects but stores no
        payload bytes of its own.
    Thread-safety assumptions:
        Frozen scalar and tuple fields are safe for concurrent reads; referenced
        clients and storage backends retain their own concurrency contracts.
    """

    document: CanonicalDocument
    evidence: tuple[Evidence, ...]
    manifest_key: str
    document_key: str
    evidence_key: str
    run_id: str = ""
    job_id: str | None = None
    artifacts: tuple[ArtifactRef, ...] = ()
    usage: UsageReport | None = None
    provenance_key: str = ""
    raw_parser_key: str | None = None
    raw_parser_keys: tuple[str, ...] = ()
    canonical_content_key: str = ""
    fusion_artifact_key: str = ""


@dataclass(frozen=True, slots=True)
class IngestRunResult:
    """Aggregate outcome for one file, folder, asset, or bundle submission."""

    run_id: str
    job_id: str | None
    root_bundle_id: str | None
    results: tuple[IngestResult, ...]
    failures: tuple[dict[str, Any], ...]
    run_manifest_key: str
    run_manifest_uri: str

    @property
    def document_count(self) -> int:
        return len(self.results)

    @property
    def failed_count(self) -> int:
        return len(self.failures)

    def __len__(self) -> int:
        return len(self.results)

    def __iter__(self):
        return iter(self.results)

    def __getitem__(self, index: int) -> IngestResult:
        return self.results[index]
