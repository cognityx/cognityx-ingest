# Output contract

Source bytes live once as immutable SourceAsset blobs. Generated artifact keys
are relative to the supplied artifact storage scope.

| Artifact | Purpose |
| --- | --- |
| `document.json` | Schema version, source metadata, title, and sections |
| `evidence.jsonl` | Evidence v2 with page text, offsets, and SourceAsset lineage |
| `manifest.json` | Stable artifact pointers for downstream consumers |
| `ingest/runs/{run_id}/manifest.json` | Whole-run inputs, outputs, failures, parser, and timestamps |

`document.json` includes `schema: "cognityx.ingest.document"` and uses
`cognityx.ingest.document/v1`. Every newly written evidence row uses
`cognityx.ingest.evidence/v2` and carries `source_asset_id`, `bundle_id`,
`context_id`, `source_sha256`, parser identity, sequence number, and `run_id`.
`Evidence.from_dict()` remains compatible with v1 rows that predate those
fields.

Run manifests use `cognityx.ingest.run/v1` and are never overwritten. A folder
run has one run ID, one job ID, and one run manifest even when individual PDFs
fail.

An optional enhancer may attach inferred metadata under `enhancement`; it must
name `cognityx-inference` and never replaces page evidence.

## Execution boundary

Every ingestion runs with a shared `cognityx-resource` `ExecutionContext`
containing opaque run and
correlation IDs and optional principal/governance scope fields. `IngestService`
authorizes `ingest.job.submit` through a `ControlClient` before parsing, then
reports a measured `UsageReport` after artifacts persist. `LocalControlClient`
is the default standalone implementation.

Lifecycle and artifact management use the same seam: `ingest.job.cancel`,
`ingest.result.read`, and `ingest.document.delete`. The local implementation
allows them in standalone mode; a future Control Plane can make those decisions
without changing parser or storage code.

Ingest owns `SUBMITTED`, `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, and
`CANCELLED` lifecycle semantics. When a `cognityx-jobs` repository is supplied,
folder runs append replayable `folder_discovered`, `asset_registered`,
`document_started`, `document_completed`, `document_failed`, and
`run_completed` progress events. Cancellation is checked between documents.

Deleting a document removes only its `ingest/documents/{document_id}/` storage
tree through `cognityx-storage`; it does not erase durable job history.

## SourceAsset registration boundary

The independent SourceAsset Registry persists SourceAssetContext, DocBundle,
SourceAsset and the SourceAsset-to-BlobRef relationship. `cognityx-storage`
owns BlobRef, digest calculation, content-addressed layout, deduplication,
physical object reuse and durable profile routing. Authorization continues to
use the existing Context, Bundle and Source action strings for compatibility;
physical Blob identity is not an authorization resource.

The catalog tables and immutable metadata namespace retain the historical
names `bundles`, `sources`, `source_id`, `bundle.json` and `source.json`.
These are durable compatibility identifiers, not the canonical public domain
vocabulary. New application code uses DocBundle, SourceAsset and `asset_id`;
the stable underlying asset value remains `src-...`.
