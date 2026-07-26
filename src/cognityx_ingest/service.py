"""Registration, normalization, provenance, and artifact persistence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from cognityx_jobs import JobRepository
from cognityx_storage import StorageClient

from cognityx_ingest.enhancement import SectionEnhancer
from cognityx_ingest.models import CanonicalDocument, Evidence, IngestResult, Section, SourceRecord
from cognityx_ingest.parser import PdfExtractor, PyPdfExtractor, UnsupportedInputError

SCHEMA_VERSION = "cognityx.ingest.document/v1"


class IngestService:
    """Ingest PDFs into canonical, source-addressable storage artifacts."""

    def __init__(
        self,
        storage: StorageClient,
        *,
        extractor: PdfExtractor | None = None,
        jobs: JobRepository | None = None,
        enhancer: SectionEnhancer | None = None,
    ) -> None:
        self._storage = storage
        self._extractor = extractor or PyPdfExtractor()
        self._jobs = jobs
        self._enhancer = enhancer

    def ingest(self, path: str | Path, *, owner_id: str = "local") -> IngestResult:
        source_path = Path(path)
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        if source_path.suffix.lower() != ".pdf":
            raise UnsupportedInputError(f"Only PDF input is supported: {source_path}")
        content = source_path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        document_id = f"pdf-{digest[:16]}"
        job_id = self._start_job(document_id, owner_id)
        try:
            source = self._register_source(source_path, digest, content, document_id)
            pages = self._extractor.extract(source_path)
            evidence = tuple(
                Evidence(
                    evidence_id=f"{document_id}:page:{page.page_number}",
                    document_id=document_id,
                    page_number=page.page_number,
                    text=page.text,
                    char_start=0,
                    char_end=len(page.text),
                )
                for page in pages
            )
            sections = tuple(
                Section(
                    section_id=f"{document_id}:section:{item.page_number}",
                    title=f"Page {item.page_number}",
                    evidence_ids=(item.evidence_id,),
                )
                for item in evidence
            )
            enhancement = self._enhancer.enhance([item.text for item in evidence]) if self._enhancer else None
            document = CanonicalDocument(
                document_id=document_id,
                schema_version=SCHEMA_VERSION,
                source=source,
                title=source_path.stem,
                sections=sections,
                enhancement=enhancement,
            )
            result = self._persist(document, evidence)
            self._finish_job(job_id, "completed", {"document_id": document_id})
            return result
        except Exception as error:
            self._finish_job(job_id, "failed", {"error": str(error)})
            raise

    def ingest_path(self, path: str | Path, *, owner_id: str = "local") -> tuple[IngestResult, ...]:
        candidate = Path(path)
        if candidate.is_file():
            return (self.ingest(candidate, owner_id=owner_id),)
        if not candidate.is_dir():
            raise FileNotFoundError(candidate)
        files = sorted(item for item in candidate.rglob("*") if item.is_file() and item.suffix.lower() == ".pdf")
        return tuple(self.ingest(item, owner_id=owner_id) for item in files)

    def _register_source(self, path: Path, digest: str, content: bytes, document_id: str) -> SourceRecord:
        key = f"ingest/documents/{document_id}/source.pdf"
        if not self._storage.exists(key):
            self._storage.put_bytes(key, content, media_type="application/pdf")
        # Artifact records use keys relative to the caller's storage scope.
        self._storage.stat(key)
        return SourceRecord(document_id, path.name, digest, len(content), key)

    def _persist(self, document: CanonicalDocument, evidence: tuple[Evidence, ...]) -> IngestResult:
        prefix = f"ingest/documents/{document.document_id}"
        document_key = f"{prefix}/document.json"
        evidence_key = f"{prefix}/evidence.jsonl"
        manifest_key = f"{prefix}/manifest.json"
        if not self._storage.exists(document_key):
            self._storage.put_json(document_key, document.to_dict())
        if not self._storage.exists(evidence_key):
            payload = "".join(json.dumps(item.to_dict(), sort_keys=True) + "\n" for item in evidence).encode()
            self._storage.put_bytes(evidence_key, payload, media_type="application/x-ndjson")
        manifest = {"document_id": document.document_id, "schema_version": SCHEMA_VERSION, "artifacts": {"source": document.source.storage_key, "document": document_key, "evidence": evidence_key}}
        if not self._storage.exists(manifest_key):
            self._storage.put_json(manifest_key, manifest)
        return IngestResult(document, evidence, manifest_key, document_key, evidence_key)

    def _start_job(self, document_id: str, owner_id: str) -> str | None:
        if self._jobs is None:
            return None
        job_id = str(uuid4())
        self._jobs.create(job_id, "ingest.pdf", {"document_id": document_id}, owner_id=owner_id)
        self._jobs.set_state(job_id, "running")
        self._jobs.append_event(job_id, "ingest_started", {"document_id": document_id})
        return job_id

    def _finish_job(self, job_id: str | None, state: str, data: dict[str, str]) -> None:
        if self._jobs is not None and job_id is not None:
            self._jobs.append_event(job_id, f"ingest_{state}", data)
            self._jobs.set_state(job_id, state)
