"""Canonical ingestion records independent of extraction or model providers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

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


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """Stable artifact identity and logical storage location."""

    artifact_id: str
    uri: str
    media_type: str


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Section:
    section_id: str
    title: str
    evidence_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence_ids"] = list(self.evidence_ids)
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "document_id": self.document_id,
            "schema_version": self.schema_version,
            "source": self.source.to_dict(),
            "title": self.title,
            "sections": [section.to_dict() for section in self.sections],
            "enhancement": self.enhancement,
        }


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
