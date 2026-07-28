"""Cognityx SourceAsset registration and document-ingestion API."""

from cognityx_ingest.context import resolve_execution_context
from cognityx_ingest.cleanup import SourceAssetCleanupService
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
    DocBundleDeletionResult,
    Evidence,
    IngestJobState,
    IngestResult,
    RegisteredSource,
    Section,
    SourceAsset,
    SourceAssetBatchItem,
    SourceAssetBatchResult,
    SourceAssetContext,
    SourceAssetDeletionResult,
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
    SourceAssetBatchCancelled,
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
    "DocBundleDeletionResult",
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
    "SourceAssetBatchCancelled",
    "SourceAssetBatchItem",
    "SourceAssetBatchResult",
    "SourceAssetContext",
    "SourceAssetCleanupService",
    "SourceAssetDeletionResult",
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
