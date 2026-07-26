"""The narrow control-plane seam used at ingest submission boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from cognityx_ingest.models import ExecutionContext, UsageReport


INGEST_JOB_SUBMIT = "ingest.job.submit"
INGEST_JOB_CANCEL = "ingest.job.cancel"
INGEST_RESULT_READ = "ingest.result.read"
INGEST_DOCUMENT_DELETE = "ingest.document.delete"


@dataclass(frozen=True, slots=True)
class ControlDecision:
    """An extensible authorization result supplied by a control client."""

    allowed: bool
    decision_id: str | None = None
    reason: str | None = None
    limits: dict[str, Any] = field(default_factory=dict)
    obligations: dict[str, Any] = field(default_factory=dict)
    policy_version: str | None = None


class ControlClient(Protocol):
    """Authorize an operation and receive facts measured by ingest."""

    def authorize(
        self,
        context: ExecutionContext,
        action: str,
        resource: object | None = None,
        request: object | None = None,
    ) -> ControlDecision: ...

    def report_usage(self, context: ExecutionContext, usage: UsageReport) -> None: ...


class LocalControlClient:
    """Standalone default: allow local work without platform policy services."""

    def authorize(
        self,
        context: ExecutionContext,
        action: str,
        resource: object | None = None,
        request: object | None = None,
    ) -> ControlDecision:
        return ControlDecision(allowed=True, decision_id="local-allow", policy_version="local")

    def report_usage(self, context: ExecutionContext, usage: UsageReport) -> None:
        return None


class IngestAuthorizationError(PermissionError):
    """Raised when the control client rejects an ingest submission."""


class IngestLimitError(ValueError):
    """Raised when an allowed decision contains an exceeded ingest limit."""
