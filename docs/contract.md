# Output contract

## Where the contract fits

Cognityx Ingest turns registered source bytes into durable parser-neutral content,
source structure, and exact evidence references. It sits between SourceAsset
registration and downstream work such as DataForge question generation. The
[Source Graph and provenance-address contract](source-graph-and-provenance-addresses.md)
adds a connected, text-free map and deterministic evidence lookup without
changing the existing document, evidence, or provenance artifacts.

Source bytes live once as immutable SourceAsset blobs. Generated artifact keys
are relative to the supplied artifact storage scope.

| Artifact | Purpose |
| --- | --- |
| `document.json` | Versioned pages, blocks, sections, objects, relations and decisions |
| `evidence.jsonl` | Evidence v2 with exact source anchors, page facts and SourceAsset lineage |
| `canonical-content.json` | Additive v3.2 parser-neutral resources, structure, text nodes, selectors and bindings |
| `source-graph.json` | Additive v3.2 connected source structure, ownership, explicit relations and revision |
| `provenance-addresses.json` | Additive v3.2 generated strong evidence addresses; logical/evidence-set records require explicit intent |
| `provenance.json` | Complete DataForge handoff without reopening the PDF |
| `parser/{backend}.json` | Optional raw parser output for audit and comparison |
| `parser/observations.json` | Additive v3.2 exact parser observations and source-region locations |
| `parser/fusion-decisions.json` | Additive v3.2 alignment evidence, retained policies, and adjudication decisions |
| `ingest/native-artifacts/{artifact-id}.json` | Small descriptor used to verify and reload one raw parser output |
| `manifest.json` | Stable artifact pointers for downstream consumers |
| `ingest/runs/{run_id}/manifest.json` | Whole-run inputs, outputs, failures, parser, timestamps, and additive DataForge source-reference bundles |

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
fail. The additive `dataforge_source_refs` array contains exactly one reference
bundle per successful T08-capable document, in result order. Each bundle points
to provenance v2, canonical content, the Source Graph, and provenance addresses
through logical `storage://` locations. These values exactly match the document's
provenance `artifact_uris`; failed documents get no placeholder. Existing
`document_manifest_refs`, `evidence_refs`, and `provenance_refs` are unchanged.
No source text or parser-native payload location appears in the new bundle.

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
compatibility boundary, and T05 owns alignment and fusion. See
[Adaptive Parser Routing](adaptive-parser-routing.md).

After compare-mode parser execution, T05 records each parser fact separately,
groups facts by their parser-native source region, aligns those regions using
source-location evidence, classifies agreement,
complementary facts, conflict, and unresolved evidence, then applies explicit
fact-specific or bounded-family policies. Confidence alone never selects a
winner, and missing page confidence remains absent.

The existing `cognityx.ingest.parser-fusion/v1` raw artifact remains readable.
The additive observation set is stored at `parser/observations.json`; its exact
bytes are bound into `parser/fusion-decisions.json` by set ID and SHA-256. The
fusion artifact retains complete data-only policies so strict reload validation
can replay every decision. Compatibility `ExtractionResult` values remain
available, but conflict or unresolved projections are not accepted gold evidence.
See [Parser Alignment, Fusion, and
Adjudication](parser-alignment-fusion-adjudication.md).

T06 segmentation views are optional derived read models over the canonical
artifact. They use `cognityx.ingest.segmentation-views/v3.2` and contain canonical
node IDs, optional character ranges, division IDs, native chunk pointers, and
bounded strategy metadata. They never contain copied source text. Several views
may overlap or disagree, and none becomes a fused canonical chunk boundary. Text
is reconstructed from `ContentNode.content.text` only when a caller requests it.
T06 does not add an always-written ingest artifact or physical cache; T07 owns
reuse, retention, and purge decisions. See [Non-Copying Segmentation
Views](non-copying-segmentation-views.md).

T07 identifies a reusable parser extraction from exactly six values: source
SHA-256, parser ID, parser version, parser-configuration hash, model version, and
logical scope. Exact validated records may be reused only after T01 reloads and
verifies the native payload. Active NativeBinding, canonically verified
parser-native view, and explicit downstream-consumer references protect the
payload. Retention expiry does not override active references or legal hold.

Purge planning produces metadata only. Cognityx Storage remains responsible for
physical deletion. T07 records `purged` and a compact tombstone only after it
rechecks current policy and read-only Storage existence reports the native
payload absent. Canonical content, selectors, bindings, T05 decisions, T06 views,
provenance, and the immutable T01 descriptor survive. See [Extraction Reuse,
Retention, and Purge](extraction-reuse-retention-purge.md).

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
