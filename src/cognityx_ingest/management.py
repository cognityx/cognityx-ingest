"""Lifecycle and artifact management at the ingest application boundary."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from cognityx_jobs import JobRecord, JobRepository
from cognityx_storage import ObjectNotFoundError, StorageClient

from cognityx_ingest.control import (
    INGEST_DOCUMENT_DELETE,
    INGEST_JOB_CANCEL,
    INGEST_RESULT_READ,
    ControlClient,
    IngestAuthorizationError,
    LocalControlClient,
)
from cognityx_ingest.models import ExecutionContext

_ARTIFACT_KEYS = {
    "source": "source.pdf",
    "document": "document.json",
    "evidence": "evidence.jsonl",
    "manifest": "manifest.json",
}
_TERMINAL_JOB_STATES = {"completed", "failed", "cancelled", "interrupted"}


class IngestManager:
    """Manage ingest-owned jobs and artifacts without exposing storage paths."""

    def __init__(
        self,
        storage: StorageClient,
        jobs: JobRepository,
        *,
        control: ControlClient | None = None,
    ) -> None:
        self._storage = storage
        self._jobs = jobs
        self._control = control or LocalControlClient()

    def list_jobs(self, context: ExecutionContext, *, owner_id: str) -> tuple[dict[str, Any], ...]:
        self._validate_owner(context, owner_id)
        self._authorize(context, INGEST_RESULT_READ, {"owner_id": owner_id})
        return tuple(asdict(record) for record in self._jobs.list_for_owner(owner_id))

    def show_job(self, context: ExecutionContext, job_id: str, *, owner_id: str) -> dict[str, Any]:
        self._validate_owner(context, owner_id)
        record = self._jobs.get_for_owner(job_id, owner_id)
        self._authorize(context, INGEST_RESULT_READ, {"job_id": job_id})
        return {"job": asdict(record), "events": self._jobs.events(record.job_id)}

    def request_cancel(self, context: ExecutionContext, job_id: str, *, owner_id: str) -> dict[str, Any]:
        self._validate_owner(context, owner_id)
        record = self._jobs.get_for_owner(job_id, owner_id)
        if record.state in _TERMINAL_JOB_STATES:
            raise ValueError(f"Job {job_id} is already terminal: {record.state}.")
        self._authorize(context, INGEST_JOB_CANCEL, {"job_id": job_id})
        if record.state != "cancellation_requested":
            self._jobs.request_cancel(job_id)
        return asdict(self._jobs.get_for_owner(job_id, owner_id))

    def list_documents(self, context: ExecutionContext) -> tuple[dict[str, Any], ...]:
        self._authorize(context, INGEST_RESULT_READ, {"collection": "documents"})
        try:
            documents = self._storage.list("ingest/documents")
        except ObjectNotFoundError:
            return ()
        return tuple(
            {"document_id": item.key.rsplit("/", 1)[-1], "uri": self._storage_uri(item.key), "size_bytes": item.size_bytes}
            for item in documents
            if item.is_directory
        )

    def show_document(self, context: ExecutionContext, document_id: str) -> dict[str, Any]:
        self._authorize(context, INGEST_RESULT_READ, {"document_id": document_id})
        prefix = self._document_prefix(document_id)
        return {
            "manifest": self._read_json(f"{prefix}/manifest.json"),
            "document": self._read_json(f"{prefix}/document.json"),
        }

    def read_artifact(self, context: ExecutionContext, document_id: str, name: str) -> bytes:
        self._authorize(context, INGEST_RESULT_READ, {"document_id": document_id, "artifact": name})
        try:
            filename = _ARTIFACT_KEYS[name]
        except KeyError as error:
            available = ", ".join(sorted(_ARTIFACT_KEYS))
            raise ValueError(f"Unknown artifact {name!r}; choose one of: {available}.") from error
        with self._storage.open(f"{self._document_prefix(document_id)}/{filename}") as source:
            return source.read()

    def delete_document(self, context: ExecutionContext, document_id: str) -> None:
        self._authorize(context, INGEST_DOCUMENT_DELETE, {"document_id": document_id})
        self._storage.delete(self._document_prefix(document_id), recursive=True)

    def _authorize(self, context: ExecutionContext, action: str, resource: object) -> None:
        decision = self._control.authorize(context, action, resource=resource)
        if not decision.allowed:
            raise IngestAuthorizationError(decision.reason or f"{action} was denied.")

    @staticmethod
    def _validate_owner(context: ExecutionContext, owner_id: str) -> None:
        if context.principal_id is not None and context.principal_id != owner_id:
            raise IngestAuthorizationError("The execution context principal does not match the requested owner.")

    @staticmethod
    def _document_prefix(document_id: str) -> str:
        if not document_id.startswith("pdf-") or "/" in document_id:
            raise ValueError("Document IDs must be canonical ingest document IDs.")
        return f"ingest/documents/{document_id}"

    def _read_json(self, key: str) -> dict[str, Any]:
        with self._storage.open(key) as source:
            return json.load(source)

    @staticmethod
    def _storage_uri(key: str) -> str:
        return f"storage://{key}"
