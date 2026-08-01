"""Canonical ingestion records independent of extraction or model providers."""

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
    block_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["block_ids"] = list(self.block_ids)
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

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["bbox"] = list(self.bbox) if self.bbox is not None else None
        return value


@dataclass(frozen=True, slots=True)
class DocumentObject:
    object_id: str
    object_type: str
    page_id: str
    caption: str | None = None
    text: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    method: str = "parser"
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["bbox"] = list(self.bbox) if self.bbox is not None else None
        return value


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
    page_ids: tuple[str, ...] = ()
    block_ids: tuple[str, ...] = ()
    continues_from: str | None = None
    continues_to: str | None = None
    method: str = "deterministic"
    confidence: float | None = 1.0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence_ids"] = list(self.evidence_ids)
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
            "objects": [item.to_dict() for item in self.objects],
            "relations": [relation.to_dict() for relation in self.relations],
            "decisions": [decision.to_dict() for decision in self.decisions],
            "unresolved": [item.to_dict() for item in self.unresolved],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CanonicalDocument":
        """Read v1 documents while accepting the richer v2 fields."""
        source = SourceRecord(**dict(value["source"]))
        sections = tuple(Section(**dict(item)) for item in value.get("sections", ()))
        return cls(
            document_id=str(value["document_id"]),
            schema_version=str(value.get("schema_version", "cognityx.ingest.document/v1")),
            source=source,
            title=str(value.get("title", source.filename)),
            sections=sections,
            enhancement=value.get("enhancement"),
            schema=str(value.get("schema", CANONICAL_SCHEMA)),
            aliases=tuple(value.get("aliases", ())),
            pages=tuple(PageRecord(**dict(item)) for item in value.get("pages", ())),
            blocks=tuple(Block(**dict(item)) for item in value.get("blocks", ())),
            objects=tuple(DocumentObject(**dict(item)) for item in value.get("objects", ())),
            relations=tuple(Relation(**dict(item)) for item in value.get("relations", ())),
            decisions=tuple(DecisionRecord(**dict(item)) for item in value.get("decisions", ())),
            unresolved=tuple(UnresolvedItem(**dict(item)) for item in value.get("unresolved", ())),
        )


@dataclass(frozen=True, slots=True)
class IngestResult:
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
