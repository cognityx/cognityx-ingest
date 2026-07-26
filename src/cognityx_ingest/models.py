"""Canonical ingestion records independent of extraction or model providers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


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

    def to_dict(self) -> dict[str, Any]:
        return {
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
