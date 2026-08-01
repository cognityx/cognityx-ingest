# Output contract

Source bytes live once as immutable SourceAsset blobs. Generated artifact keys
are relative to the supplied artifact storage scope.

| Artifact | Purpose |
| --- | --- |
| `document.json` | Versioned pages, blocks, sections, objects, relations and decisions |
| `evidence.jsonl` | Evidence v2 with exact source anchors, page facts and SourceAsset lineage |
| `provenance.json` | Complete DataForge handoff without reopening the PDF |
| `parser/{backend}.json` | Optional raw parser output for audit and comparison |
| `manifest.json` | Stable artifact pointers for downstream consumers |
| `ingest/runs/{run_id}/manifest.json` | Whole-run inputs, outputs, failures, parser, and timestamps |

`document.json` includes `schema: "cognityx.ingest.document"` and uses
`cognityx.ingest.document/v2`. Every newly written evidence row uses
`cognityx.ingest.evidence/v2` and carries `source_asset_id`, `bundle_id`,
`context_id`, `source_sha256`, parser identity, sequence number, and `run_id`.
`Evidence.from_dict()` remains compatible with v1 rows that predate those
fields. `CanonicalDocument.from_dict()` and `Evidence.from_dict()` continue to
read stored v1 records.

Run manifests use `cognityx.ingest.run/v2` and are never overwritten. A folder
run has one run ID, one job ID, and one run manifest even when individual PDFs
fail.

The immutable provenance artifact records physical page indexes, PDF and
printed labels, reading-order blocks, cross-page sections, tables, figures,
captions, footnotes, exact evidence, relations, decision records and unresolved
items. Observed facts, parser results, deterministic rules and inference
proposals retain separate `method` and confidence fields.

Bounded inference uses `CognityxInferenceClient`. A named local server profile
starts the worker and loads its configured model; external providers must pass
availability, capability and data-classification checks. The response is only
a proposal. Deterministic validation rejects invented anchors and disallowed
relationship types. No chain-of-thought is stored.

Reusable representations are identified by source content hash, sorted source
anchor IDs, representation type, generation method, model version and
configuration hash. Lightweight `KnowledgeUnit`, `RetrievalUnit`,
`Representation` and `IndexBinding` records describe the later handoff but do
not generate vectors, questions, summaries, graph data or SQL bindings.

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
It also does not delete the SourceAsset Blob. Deleting a run removes only that
run's generated metadata; documents and SourceAssets remain separate.

## SourceAsset registration boundary

The independent SourceAsset Registry persists SourceAssetContext, DocBundle,
SourceAsset and the SourceAsset-to-BlobRef relationship. `cognityx-storage`
owns BlobRef, digest calculation, content-addressed layout, deduplication,
physical object reuse and durable profile routing. Authorization continues to
use the existing Context, Bundle and Source action strings for compatibility;
physical Blob identity is not an authorization resource.

Physical cleanup is Storage-owned. SourceAsset and DocBundle deletion first
remove logical references; Storage garbage collection later plans candidates,
checks live references again, and removes only unreferenced Blobs. A future
always-running Storage cleanup service will automate this same safe process.

The catalog tables and immutable metadata namespace retain the historical
names `bundles`, `sources`, `source_id`, `bundle.json` and `source.json`.
These are durable compatibility identifiers, not the canonical public domain
vocabulary. New application code uses DocBundle, SourceAsset and `asset_id`;
the stable underlying asset value remains `src-...`.
