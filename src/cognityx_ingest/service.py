"""Registration, normalization, provenance, and artifact persistence."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from uuid import uuid4

from cognityx_jobs import JobRepository
from cognityx_storage import StorageClient

from cognityx_ingest.enhancement import SectionEnhancer
from cognityx_ingest.control import (
    INGEST_JOB_SUBMIT,
    ControlClient,
    IngestAuthorizationError,
    IngestLimitError,
    LocalControlClient,
)
from cognityx_ingest.models import ArtifactRef, CanonicalDocument, Evidence, ExecutionContext, IngestJobState, IngestResult, Section, SourceRecord, UsageReport
from cognityx_ingest.parser import PdfExtractor, PyPdfExtractor, UnsupportedInputError

SCHEMA_VERSION = "cognityx.ingest.document/v1"
LOGGER = logging.getLogger(__name__)


class IngestService:
    """Ingest PDFs into canonical, source-addressable storage artifacts."""

    def __init__(
        self,
        storage: StorageClient,
        *,
        extractor: PdfExtractor | None = None,
        jobs: JobRepository | None = None,
        enhancer: SectionEnhancer | None = None,
        control: ControlClient | None = None,
    ) -> None:
        self._storage = storage
        self._extractor = extractor or PyPdfExtractor()
        self._jobs = jobs
        self._enhancer = enhancer
        self._control = control or LocalControlClient()

    def ingest(
        self,
        path: str | Path,
        *,
        owner_id: str = "local",
        context: ExecutionContext | None = None,
    ) -> IngestResult:
        started_at = time.monotonic()
        context = context or self._local_context(owner_id)
        source_path = Path(path)
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        if source_path.suffix.lower() != ".pdf":
            raise UnsupportedInputError(f"Only PDF input is supported: {source_path}")
        content = source_path.read_bytes()
        decision = self._control.authorize(
            context,
            INGEST_JOB_SUBMIT,
            resource={"source_name": source_path.name},
            request={"input_bytes": len(content)},
        )
        if not decision.allowed:
            raise IngestAuthorizationError(decision.reason or "Ingest submission was denied.")
        self._enforce_limit(decision.limits, "max_document_size", len(content), "input bytes")
        digest = hashlib.sha256(content).hexdigest()
        document_id = f"pdf-{digest[:16]}"
        job_id = self._start_job(document_id, owner_id)
        self._log("ingest_started", context, job_id, document_id)
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
            self._enforce_limit(decision.limits, "max_pages", len(evidence), "pages")
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
            result = self._persist(document, evidence, context.run_id, job_id)
            usage = UsageReport(
                run_id=context.run_id,
                job_id=job_id,
                documents=1,
                pages=len(evidence),
                input_bytes=len(content),
                output_bytes=sum(self._storage.stat(key).size_bytes for key in (result.document_key, result.evidence_key, result.manifest_key)),
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            self._control.report_usage(context, usage)
            self._finish_job(job_id, "completed", {"document_id": document_id})
            self._log("ingest_completed", context, job_id, document_id)
            return IngestResult(
                result.document, result.evidence, result.manifest_key, result.document_key, result.evidence_key,
                run_id=context.run_id, job_id=job_id, artifacts=result.artifacts, usage=usage,
            )
        except Exception as error:
            self._finish_job(job_id, "failed", {"error": str(error)})
            self._log("ingest_failed", context, job_id, document_id)
            raise

    def ingest_path(
        self,
        path: str | Path,
        *,
        owner_id: str = "local",
        context: ExecutionContext | None = None,
    ) -> tuple[IngestResult, ...]:
        candidate = Path(path)
        if candidate.is_file():
            return (self.ingest(candidate, owner_id=owner_id, context=context),)
        if not candidate.is_dir():
            raise FileNotFoundError(candidate)
        files = sorted(item for item in candidate.rglob("*") if item.is_file() and item.suffix.lower() == ".pdf")
        return tuple(self.ingest(item, owner_id=owner_id, context=context or self._local_context(owner_id)) for item in files)

    def _register_source(self, path: Path, digest: str, content: bytes, document_id: str) -> SourceRecord:
        key = f"ingest/documents/{document_id}/source.pdf"
        if not self._storage.exists(key):
            self._storage.put_bytes(key, content, media_type="application/pdf")
        # Artifact records use keys relative to the caller's storage scope.
        self._storage.stat(key)
        return SourceRecord(document_id, path.name, digest, len(content), key)

    def _persist(self, document: CanonicalDocument, evidence: tuple[Evidence, ...], run_id: str, job_id: str | None) -> IngestResult:
        prefix = f"ingest/documents/{document.document_id}"
        document_key = f"{prefix}/document.json"
        evidence_key = f"{prefix}/evidence.jsonl"
        manifest_key = f"{prefix}/manifest.json"
        if not self._storage.exists(document_key):
            self._storage.put_json(document_key, document.to_dict())
        if not self._storage.exists(evidence_key):
            payload = "".join(json.dumps(item.to_dict(), sort_keys=True) + "\n" for item in evidence).encode()
            self._storage.put_bytes(evidence_key, payload, media_type="application/x-ndjson")
        stored = {
            "source": self._storage.stat(document.source.storage_key),
            "document": self._storage.stat(document_key),
            "evidence": self._storage.stat(evidence_key),
        }
        artifacts = tuple(
            ArtifactRef(f"art-{document.document_id}-{name}", self._artifact_uri(item.key), item.media_type)
            for name, item in stored.items()
        )
        manifest = {
            "document_id": document.document_id,
            "schema": document.schema,
            "schema_version": SCHEMA_VERSION,
            "artifacts": {name: {"artifact_id": ref.artifact_id, "uri": ref.uri} for name, ref in zip(stored, artifacts, strict=True)},
        }
        if not self._storage.exists(manifest_key):
            self._storage.put_json(manifest_key, manifest)
        manifest_object = self._storage.stat(manifest_key)
        manifest_ref = ArtifactRef(
            f"art-{document.document_id}-manifest",
            self._artifact_uri(manifest_object.key),
            manifest_object.media_type,
        )
        return IngestResult(
            document,
            evidence,
            manifest_key,
            document_key,
            evidence_key,
            run_id=run_id,
            job_id=job_id,
            artifacts=(*artifacts, manifest_ref),
        )

    def _start_job(self, document_id: str, owner_id: str) -> str | None:
        if self._jobs is None:
            return None
        job_id = str(uuid4())
        self._jobs.create(job_id, "ingest.pdf", {"document_id": document_id}, owner_id=owner_id)
        self._jobs.append_event(job_id, "ingest_submitted", {"document_id": document_id})
        self._jobs.append_event(job_id, "ingest_queued", {"document_id": document_id})
        self._jobs.set_state(job_id, IngestJobState.RUNNING)
        self._jobs.append_event(job_id, "ingest_started", {"document_id": document_id})
        return job_id

    def _finish_job(self, job_id: str | None, state: str, data: dict[str, str]) -> None:
        if self._jobs is not None and job_id is not None:
            self._jobs.append_event(job_id, f"ingest_{state}", data)
            self._jobs.set_state(job_id, state)

    @staticmethod
    def _local_context(owner_id: str) -> ExecutionContext:
        return ExecutionContext(run_id=str(uuid4()), correlation_id=str(uuid4()), principal_id=owner_id)

    @staticmethod
    def _enforce_limit(limits: dict[str, object], name: str, actual: int, unit: str) -> None:
        maximum = limits.get(name)
        if maximum is not None and actual > int(maximum):
            raise IngestLimitError(f"{name} exceeded: {actual} {unit} is greater than {maximum}.")

    @staticmethod
    def _artifact_uri(storage_key: str) -> str:
        """Expose backend-neutral references instead of local backend locations."""
        return f"storage://{storage_key}"

    @staticmethod
    def _log(event: str, context: ExecutionContext, job_id: str | None, document_id: str) -> None:
        LOGGER.info(
            event,
            extra={"service": "cognityx-ingest", "run_id": context.run_id, "correlation_id": context.correlation_id, "job_id": job_id, "document_id": document_id},
        )
