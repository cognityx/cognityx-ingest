# Cognityx Ingest v3.2 Focused Contract

## 1. Stable platform boundary

Storage owns immutable blobs, content-addressing, physical deduplication, profiles and safe physical
cleanup. Jobs owns durable background lifecycle, progress, retries, cancellation and audit events.
Ingest owns source registration coordination, parser execution, parser-native artifacts, canonical source
records, parser decisions, non-copying segmentation views and DataForge handoff artifacts.

## 2. Parser-neutral but lossless

Docling is the initial primary document-intelligence engine. The complete parser-native result is stored as
one independently governed artifact. Cognityx never replaces that result with a reduced copy. A canonical
overlay exposes stable task-neutral concepts and binds them to native objects through artifact IDs and
native pointers.

A future parser may replace or supplement Docling without changing downstream canonical contracts.

## 3. Canonical records

- `Resource`
- `PresentationUnit`
- `Division` with extensible `division_role`
- `ContentNode` with one authoritative canonical text value
- `Representation`
- `SourceSelector`
- `NativeBinding`
- `Relation`
- `ProcessingActivity`
- `ArtifactDescriptor`
- `SegmentationView`
- parser observations, conflicts, decisions and unresolved items

Pages, slides, sheets, frames and time ranges are presentation locations, not logical division boundaries.
Each canonical text node has one deepest direct structural owner. Parent subtree content is reconstructed by
hierarchy and order, never copied.

## 4. Capability knowledge

Every parser profile has exactly three source classes:

1. **parser-discovered**: runtime/package/API inspection plus frozen evidence from official documentation;
2. **human-guided**: approved preferences, restrictions and domain guidance;
3. **auto-learned**: benchmark, run, correction and downstream feedback measurements.

The live registry, not an LLM's pretrained memory, is the routing source of truth.

## 5. Routing modes

- **deterministic**: explicit rules over registry fields;
- **hybrid**: deterministic allowlists and governance, with an LLM proposal inside the boundary;
- **LLM-directed**: the model proposes the full plan, still validated against availability, governance,
  security, cost and schema constraints.

Existing `fixed`, `rule`, `fallback`, `compare` and `agent` parser-policy names remain backward compatible.
T00 freezes the compatibility boundary; later work implements any mapping explicitly.

## 6. Parser fusion

Routing decides which parsers run. Alignment maps observations to common source regions. Fusion retains
agreement, complementary facts and conflicts. Adjudication accepts, rejects or leaves a fact unresolved.
Execution order must not change the accepted canonical result.

## 7. Segmentation

Chunking is a derived source view, not canonical content. Ingest may materialize several reusable
`SegmentationView` records. Views contain node IDs and character or sentence spans, not copied source text.
No single fused chunk boundary becomes canonical. Query-time view routing and rank fusion remain downstream.

## 8. Graph boundary

The Ingest Source Graph connects resources, presentation units, divisions, nodes, objects, representations,
selectors, native bindings, activities and explicit validated references. It may be serialized as JSON; it
does not require a graph database.

Semantic entities, claims, communities, PageRank, GraphRAG indexes and Knowledge Unit relations are
DataForge or retrieval-layer products. Every derived graph projection must bind back to Source Graph support.

## 9. Provenance addresses

- **strong address**: immutable source hash, source-graph revision, canonical node/object/span and selectors;
- **logical address**: business-stable resource family, version rule and division reference;
- **evidence-set address**: ordered strong addresses supporting one claim or Knowledge Unit.

A resolver returns exact, redirected, ambiguous, obsolete, forbidden or unresolved. It never fabricates a
source target.

## 10. Focused downstream proof

Only two consumer paths are mandatory now:

1. DataForge reconstructs one paragraph view, creates Q/A and retains exact support addresses.
2. DataForge follows validated intra- and inter-document relations to assemble one composite Knowledge Unit.
   Ambiguous, contradicted and unresolved evidence is excluded from gold support.
