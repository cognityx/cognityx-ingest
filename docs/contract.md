# Output contract

Source bytes live once as immutable SourceAsset blobs. Generated artifact keys
are relative to the supplied artifact storage scope.

| Artifact | Purpose |
| --- | --- |
| `document.json` | Versioned pages, blocks, sections, objects, relations and decisions |
| `evidence.jsonl` | Evidence v2 with exact source anchors, page facts and SourceAsset lineage |
| `canonical-content.json` | Additive v3.2 parser-neutral resources, structure, text nodes, selectors and bindings |
| `provenance.json` | Complete DataForge handoff without reopening the PDF |
| `parser/{backend}.json` | Optional raw parser output for audit and comparison |
| `ingest/native-artifacts/{artifact-id}.json` | Small descriptor used to verify and reload one raw parser output |
| `manifest.json` | Stable artifact pointers for downstream consumers |
| `ingest/runs/{run_id}/manifest.json` | Whole-run inputs, outputs, failures, parser, and timestamps |

`document.json` includes `schema: "cognityx.ingest.document"` and uses
`cognityx.ingest.document/v2`. Every newly written evidence row uses
`cognityx.ingest.evidence/v2` and carries `source_asset_id`, `bundle_id`,
`context_id`, `source_sha256`, parser identity, sequence number, and `run_id`.
`Evidence.from_dict()` remains compatible with v1 rows that predate those
fields. `CanonicalDocument.from_dict()` and `Evidence.from_dict()` continue to
read stored v1 records.

The additive `canonical-content.json` uses
`cognityx.ingest.canonical-content/v3.2`. It keeps exact extracted text only in
`content_nodes[*].content.text`; presentation units, logical Divisions,
selectors, representations, relations, processing activities and bindings use
IDs and observed source facts. The existing v2 files may still repeat text for
backward compatibility, so the text-once rule applies to the v3.2 artifact rather
than the repository as a whole. See [Canonical Content](canonical-content.md).

Run manifests use `cognityx.ingest.run/v2` and are never overwritten. A folder
run has one run ID, one job ID, and one run manifest even when individual PDFs
fail.

The parser-native payload is the exact output returned by a parser. Ingest keeps
that output at its existing `parser/{backend}.json` path and stores a separate
descriptor containing its parser identity, SHA-256, byte count, media type,
logical URI, retention classification, and optional native pointers. Reloading a
payload recomputes its byte count and SHA-256 before returning it. The descriptor
does not replace or reduce the original bytes; see [Native Parser
Artifacts](native-parser-artifacts.md).

The parser capability registry is a separate versioned evidence snapshot. It
keeps current adapter/package observations, frozen official documentation,
approved human guidance, and measured outcomes in three distinct source
classes. It does not choose a parser or change extraction output. See [Parser
Capability Registry](parser-capability-registry.md).

The separate routing-plan schema is
`cognityx.ingest.routing-plan/v3.2`. It records deterministic, hybrid, or
LLM-directed parser invocations plus the hard validation result. A routing plan
does not execute a parser and does not combine parser outputs. T03 registry facts
remain unchanged, existing `ExtractionPolicy` values remain the execution
compatibility boundary, and later T05 work owns alignment and fusion. See
[Adaptive Parser Routing](adaptive-parser-routing.md).

The immutable provenance artifact is the complete package another program
needs to understand the document without opening the PDF again. This handoff
is technically called `provenance.json`. Version 2 records the run and job,
the SourceAsset and its SHA-256, the bundle and document, aliases, and stable
`storage://` locations for every generated artifact. The SourceAsset keeps its
separate `sourceasset://` logical identity because Storage, not Ingest, owns
the raw Blob.

The artifact records physical page indexes, PDF and
printed labels, typed reading-order blocks, exact section spans across one or
more pages, evidence, observed objects, relations, decision records and
unresolved items.
Each numbered section carries its number, title, hierarchy level, parent, path,
heading block, start and end block anchors, ordered page IDs and content block
IDs. Its span follows the complete reading-order stream until the next heading
at the same or a higher level. Parent spans therefore contain their child
sections even when the content crosses a page boundary.

A genuine continuation records its source and target block anchors,
deterministic status, method and confidence. It also appears in the canonical
relation collection as a resolved `continues_on` relation. When the following
page begins with a peer or higher-level heading, the final source anchor is
retained as a rejected `continues_on` relation with no invented target and a
deterministic reason. DataForge can consume these fields without reopening the
PDF. Observed facts, parser results, deterministic rules and inference
proposals retain separate `method` and confidence fields. Multi-page table
identity is represented as one logical table with stable ownership, caption and
caption anchor, typed columns and ordered data rows, and ordered physical-page
parts. Each part retains its canonical source block, parser-observation anchors,
repeated-header status and merged group-row span. Repeated headers and group
rows are audit facts, not duplicate data rows. Richer figure or footnote
ownership is also canonical: figures retain image and caption anchors, page
geometry and owning sections; footnotes retain their visible marker, marker
anchor, note anchor, exact note text and owning section. Resolved relations tie
captions and markers to stable object IDs so consumers do not need to infer
ownership from the source PDF.

Every relation has a `gold` eligibility flag for downstream use. Only an
observed or deterministically resolved relation with a concrete target is
eligible. Rejected, ambiguous and unresolved results are always non-gold and
remain visible for audit. The explicit `ambiguous` and `unresolved`
collections let DataForge avoid silently treating uncertainty as truth.

## Using the handoff

DataForge reads the `provenance` URI returned by ingest and can form candidate
knowledge spans from each section's ordered page, block and evidence IDs. It
must retain the `document_id`, `asset_id`, source SHA-256 and those anchor IDs
on every later record. It must not reopen the PDF to rediscover structure or
provenance. Ingest does not create embeddings, vectors, questions, answers or
training records; those remain DataForge responsibilities.

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
