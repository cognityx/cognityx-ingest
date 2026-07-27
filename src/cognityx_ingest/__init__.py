"""Cognityx SourceAsset registration and document-ingestion API."""

from cognityx_ingest.context import resolve_execution_context
from cognityx_ingest.control import (
    ControlClient,
    ControlDecision,
    IngestAuthorizationError,
    IngestLimitError,
    LocalControlClient,
)
from cognityx_ingest.management import IngestManager
from cognityx_ingest.models import (
    ArtifactRef,
    CanonicalDocument,
    DocBundle,
    Evidence,
    IngestJobState,
    IngestResult,
    RegisteredSource,
    Section,
    SourceAsset,
    SourceAssetContext,
    SourceAssetLocation,
    SourceAssetRegistrationResult,
    SourceBundle,
    SourceContext,
    SourceLocation,
    SourceRecord,
    SourceRegistrationResult,
    UsageReport,
)
from cognityx_ingest.parser import (
    ExtractedPage,
    PyPdfExtractor,
    UnsupportedInputError,
)
from cognityx_ingest.service import IngestService
from cognityx_ingest.source_assets import (
    SourceAssetCatalogAmbiguityError,
    SourceAssetCatalogError,
    SourceAssetRegistry,
)
from cognityx_ingest.sources import SourceRegistry
from cognityx_resource import ExecutionContext, ResourceContext, ResourceRef

__all__ = [
    "ArtifactRef",
    "CanonicalDocument",
    "ControlClient",
    "ControlDecision",
    "DocBundle",
    "Evidence",
    "ExecutionContext",
    "ExtractedPage",
    "IngestAuthorizationError",
    "IngestJobState",
    "IngestLimitError",
    "IngestManager",
    "IngestResult",
    "IngestService",
    "LocalControlClient",
    "PyPdfExtractor",
    "RegisteredSource",
    "ResourceContext",
    "ResourceRef",
    "Section",
    "SourceAsset",
    "SourceAssetContext",
    "SourceAssetCatalogAmbiguityError",
    "SourceAssetCatalogError",
    "SourceAssetLocation",
    "SourceAssetRegistrationResult",
    "SourceAssetRegistry",
    "SourceBundle",
    "SourceContext",
    "SourceLocation",
    "SourceRecord",
    "SourceRegistrationResult",
    "SourceRegistry",
    "UnsupportedInputError",
    "UsageReport",
    "resolve_execution_context",
]
