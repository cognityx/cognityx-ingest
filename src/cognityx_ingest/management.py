"""Enforce lifecycle and artifact authorization at the Ingest boundary.

``IngestManager`` exists so SDKs and CLIs do not open protected Ingest results
without component-owned policy context.  It combines a caller execution context,
the narrow ``ControlClient`` authorization seam, durable Jobs state, and the
configured artifact-role store.  Artifact reads accept only a closed public name,
authorize the exact document-plus-artifact resource before opening Storage, and
map that name to a fixed logical filename.  No caller URI, path, or parser-native
filename becomes executable input.

The manager is constructed by application composition roots.  It keeps shared
dependency references but no per-request mutable state; concurrency and durable
ordering remain owned by the injected Jobs and Storage implementations.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from cognityx_jobs import JobRecord, JobRepository
from cognityx_storage import ObjectNotFoundError

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
    "document": "document.json",
    "evidence": "evidence.jsonl",
    "provenance": "provenance.json",
    "manifest": "manifest.json",
    "canonical-content": "canonical-content.json",
    "source-graph": "source-graph.json",
    "provenance-addresses": "provenance-addresses.json",
    "parser-observations": "parser/observations.json",
    "parser-fusion-decisions": "parser/fusion-decisions.json",
}
# Compatibility CLIs consume the mapping order without gaining filename access.
ARTIFACT_READ_NAMES = tuple(_ARTIFACT_KEYS)
_TERMINAL_JOB_STATES = {"completed", "failed", "cancelled", "interrupted"}


class IngestManager:
    """Manage owner-scoped work and protected results through component policy.

    Application composition roots construct this class with an artifact store,
    Jobs repository, and optional centralized ``ControlClient``.  Public methods
    authorize their exact resource before reading, cancelling, or deleting and
    return JSON-ready records or exact artifact bytes without physical paths.
    The manager persists no duplicate policy state, performs deterministic fixed
    key construction, propagates typed component failures, and may be shared when
    its injected dependencies support concurrent access.
    """

    def __init__(
        self,
        storage: Any,
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

    def job_events(
        self,
        context: ExecutionContext,
        job_id: str,
        *,
        owner_id: str,
        after: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        """Replay ordered durable events for one owner-scoped job."""
        self._validate_owner(context, owner_id)
        record = self._jobs.get_for_owner(job_id, owner_id)
        self._authorize(context, INGEST_RESULT_READ, {"job_id": job_id})
        return tuple(self._jobs.events(record.job_id, after=after))

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
            {"document_id": item.key.rsplit("/", 1)[-1], "uri": self._stored_uri(item), "size_bytes": item.size_bytes}
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
        """Authorize and read one exact settled artifact from a closed vocabulary.

        SDK and compatibility CLI callers provide a canonical document ID and one
        hyphenated name from ``ARTIFACT_READ_NAMES``.  The method first asks the
        Ingest control boundary to authorize ``INGEST_RESULT_READ`` with the exact
        ``{"document_id": document_id, "artifact": name}`` resource.  A denial
        raises ``IngestAuthorizationError`` before any Storage open.  After an
        allow decision, the closed mapping selects a fixed relative filename and
        ``_document_prefix`` validates/builds the logical key; unknown names raise
        ``ValueError`` and Storage failures propagate unchanged.

        No URI, traversal, underscore alias, parser backend name, or caller-chosen
        filename is interpreted.  The operation is read-only and deterministic
        for immutable artifacts, retains no request state, and follows the
        thread-safety guarantees of the injected ControlClient and Storage store.
        """
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

    def list_runs(self, context: ExecutionContext) -> tuple[dict[str, Any], ...]:
        """List generated ingest runs without exposing physical storage paths."""
        self._authorize(context, INGEST_RESULT_READ, {"collection": "runs"})
        try:
            runs = self._storage.list("ingest/runs")
        except ObjectNotFoundError:
            return ()
        return tuple(
            {
                "run_id": item.key.rsplit("/", 1)[-1],
                "uri": self._stored_uri(item),
                "size_bytes": item.size_bytes,
            }
            for item in runs
            if item.is_directory
        )

    def show_run(self, context: ExecutionContext, run_id: str) -> dict[str, Any]:
        self._authorize(context, INGEST_RESULT_READ, {"run_id": run_id})
        return self._read_json(f"{self._run_prefix(run_id)}/manifest.json")

    def delete_run(self, context: ExecutionContext, run_id: str) -> None:
        """Delete generated run metadata without touching documents or SourceAssets."""
        self._authorize(context, INGEST_DOCUMENT_DELETE, {"run_id": run_id})
        self._storage.delete(self._run_prefix(run_id), recursive=True)

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

    @staticmethod
    def _run_prefix(run_id: str) -> str:
        if not run_id or "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
            raise ValueError("Run IDs must be one non-empty storage-key segment.")
        return f"ingest/runs/{run_id}"

    def _read_json(self, key: str) -> dict[str, Any]:
        with self._storage.open(key) as source:
            return json.load(source)

    @staticmethod
    def _stored_uri(item: Any) -> str:
        uri = str(item.uri)
        return uri if uri.startswith("storage://") else f"storage://{item.key}"
