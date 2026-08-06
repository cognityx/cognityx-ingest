# Canonical Content

## The Problem

Different parsers describe documents differently. One parser may return pages
and text boxes, another may return slides and shapes, and another may return a
time range in audio. Downstream programs should not need parser-specific code to
find the source content and its structure.

Cognityx Ingest therefore writes a new parser-neutral source model called
`canonical-content.json`. Parser-neutral means that its records describe common
source ideas without importing a Docling, PyMuPDF, or another parser-private
class.

The file is additive. It does not replace the existing v2 `document.json`.

## Where It Fits

```text
registered SourceAsset
        |
        v
parser observations ---> parser-native payload + T01 descriptor
        |
        v
v2 document/evidence compatibility artifacts
        |
        v
canonical-content.json
        |
        +----> future T06 segmentation views
        +----> future T08 Source Graph and provenance addresses
        +----> later T09 DataForge handoff projections
```

Storage owns the immutable objects and their physical location. Ingest owns the
canonical records and validation. DataForge later creates questions, answers,
Knowledge Units, semantic graph facts, embeddings, and training records.

## Old V2 And New V3.2

The existing compatibility files remain unchanged:

```text
ingest/documents/<document-id>/document.json
ingest/documents/<document-id>/evidence.jsonl
ingest/documents/<document-id>/provenance.json
ingest/documents/<document-id>/manifest.json
```

T02 adds:

```text
ingest/documents/<document-id>/canonical-content.json
```

Its schema is:

```text
cognityx.ingest.canonical-content/v3.2
```

`document.json` and `evidence.jsonl` are still readable v2 projections. They may
repeat text because existing consumers rely on those fields. This is an explicit
migration exception. The repository as a whole does not yet contain only one
text copy. The text-once rule applies inside `canonical-content.json`.

## Complete Record Model

### CanonicalResource

Represents the logical source and its immutable SourceAsset identity. It records
the source hash, media type, filename, and logical URI but never embeds source
bytes.

### PresentationUnit

Says where content appeared. Examples include a page, slide, sheet, frame, time
range, or document-level surface. It may retain an observed physical index,
typed labels, width, and height. It has no page text field.

A typed presentation label records both the displayed value and why that value
exists. The first two supported types are `pdf-page-label`, read from the PDF
page tree, and `printed-page-label`, observed on the page. These records are not
deduplicated by value: if both sources say `"1"`, both facts remain in fixed type
order. This distinction lets citation and audit tools explain which label they
used without changing the existing v2 `PageRecord` fields.

```json
"labels": [
  {"label_type": "pdf-page-label", "value": "1"},
  {"label_type": "printed-page-label", "value": "1"}
]
```

### Division

Says how content is organized logically. One extensible `division_role` supports
document, section, subsection, clause, appendix, chapter, policy rule, and future
roles. Separate classes such as `SectionDivision` or `ClauseDivision` are not
needed.

A Division stores parent, child, title-node, and direct-node IDs. It does not
store copied title text or copied child text.

### CanonicalText And ContentNode

`CanonicalText` stores exact source text and the SHA-256 of its exact UTF-8 bytes.
A `ContentNode` owns one source occurrence, one deepest direct Division, source
selectors, its kind, and deterministic order.

Equal strings at two locations remain two ContentNodes. For example, two pages
may each print “Approved.” Their text hashes match, but their node IDs and source
selectors differ.

### SourceSelector

Identifies a real source location without storing a quote. It can reference:

- a page or another PresentationUnit;
- a safe source-relative path;
- real character start and end offsets;
- real bounding-box geometry;
- current v2 anchor IDs;
- parser-native anchor IDs retained as opaque location facts.

Character offsets are included only when the source adapter actually knows them.
The current PDF block model has page and anchor facts but no reliable source-text
offset, so T02 does not invent one.

Example selector with real fixture offsets:

```json
{
  "selector_id": "pol-p2:selector:0",
  "selector_type": "text-position",
  "resource_id": "res-policy-v2",
  "presentation_unit_id": "pu-policy-document",
  "source_path": "sources/segmentation_policy.md",
  "char_start": 131,
  "char_end": 192,
  "bbox": null,
  "source_anchor_ids": []
}
```

### Representation

References a non-text or externally stored form, such as an image, table, audio
stream, or layout object. It points to a canonical subject, selectors, optional
artifact, and optional caption ContentNode. It never copies caption or object
text.

A Representation may own a `source_selectors` tuple. This representation-owned
selector keeps a real page, bounding box, source anchor, or parser-native anchor
even when no caption block can provide a ContentNode selector. Audit readers and
future source-address consumers can therefore locate a figure or table without
copying its caption, table cells, OCR text, or object text. `selector_ids` still
references existing selectors when an observed anchor genuinely matches one.
When the parser supplied no location fact at all, the Representation may have no
selector; T02 does not invent geometry, offsets, pages, or anchors.

The root package exports this v3.2 record as `CanonicalRepresentation` because
the established root-level `Representation` name remains the legacy enrichment
record for backward compatibility. The canonical module itself uses the required
name `cognityx_ingest.canonical_content.Representation`.

### NativeBinding

Connects a canonical record to one exact object or location inside a parser-native
artifact. This connection is called a native binding.

```text
canonical_id -> T01 artifact_id -> retained native_pointer
```

Validation requires the real T01 `NativeArtifactDescriptor`. The pointer must be
retained by that descriptor. Cross-descriptor consistency also requires its
artifact ID, payload URI, payload media type, and payload SHA-256 to match the
generic parser-native artifact reference. The generic reference has no
`schema_version` unless the parser payload itself has a known schema; the T01
descriptor schema describes the descriptor, not the payload. T02 does not create
another native reader, duplicate JSON-pointer resolution, or copy native payload
bytes. An empty binding list is valid because current parser adapters may not yet
submit explicit pointers.

### CanonicalRelation

References canonical source and target IDs, status, epistemic state, and evidence
node IDs. A relation with no target must remain explicitly unresolved, ambiguous,
contradicted, or rejected. Legacy free `target_text` is not copied.

T02 stores auditable relations only. Graph traversal, communities, PageRank, and
GraphRAG are not implemented.

### ProcessingActivity

Records the normalization method, run, correlation, contributing parser IDs, and
input/output artifact IDs. It is compact processing lineage, not an execution log.

### CanonicalArtifactDescriptor

References a source, compatibility artifact, canonical artifact, or parser-native
artifact by role, provider-neutral URI, media type, optional hash, and optional
schema. It does not duplicate T01 descriptor behavior or payload bytes.

### CanonicalContentArtifact

The frozen aggregate contains resources, presentation units, divisions, content
nodes, representations, native bindings, relations, activities, and artifact
descriptors. It provides strict `from_dict()`, deterministic `to_dict()` and JSON
serialization, validation, direct-node lookup, and subtree-node lookup.

## The Text-Once Rule

Within `canonical-content.json`, extracted source text appears only here:

```text
content_nodes[*].content.text
```

It does not appear in PresentationUnit, Division, SourceSelector,
Representation, NativeBinding, CanonicalRelation, ProcessingActivity, or artifact
descriptor records. Strict deserialization rejects unknown fields, so a copied
`text`, `quote`, or `subtree_text` field cannot hide in those records.

A Division title is referenced through `title_node_id`. A parent Division's
content is reconstructed from direct nodes and child Divisions; child text is not
copied into the parent.

## Presentation Versus Logical Structure

Consider a policy section that starts near the bottom of page 3 and continues on
page 4:

```text
PresentationUnit page-3 -----\
                              > Division section-4.2
PresentationUnit page-4 -----/
```

The pages say where the content appeared. The section says what the content
means structurally. T02 never creates one Division per page merely because the
source is a PDF.

## Direct And Subtree Reconstruction

Each ContentNode belongs directly to exactly one deepest Division. If section
4.2 contains subsection 4.2.1, the subsection's nodes are direct nodes of 4.2.1,
not also direct nodes of 4.2.

```python
direct = artifact.direct_nodes("section-4.2")
subtree = artifact.subtree_nodes("section-4.2")
```

`direct_nodes` returns only the existing nodes owned by section 4.2.
`subtree_nodes` follows child Division IDs and returns existing node records in
source order. Neither method creates or stores a concatenated parent text value.

## Builder Algorithm

`CanonicalContentBuilder` performs these steps:

1. Create one resource from the registered SourceAsset.
2. Convert current pages to PresentationUnits while preserving page IDs.
3. Preserve PDF and printed page labels as separate typed facts.
4. Create one document-root Division.
5. Convert current logical sections to child Divisions. A missing parent value
   means document root, but a declared parent ID must exist.
6. Rank explicit section membership by hierarchy depth and assign each block to
   one deepest owner.
7. Create one ContentNode for every authoritative text-bearing block.
8. Hash exact UTF-8 text.
9. Create selectors only from observed page, anchor, geometry, path, or offset
   facts.
10. Reference non-text objects through Representation records and preserve their
    observed non-text selectors.
11. Map only safely resolvable relations and omit free target text.
12. Add one processing activity and generic artifact references.
13. Add explicit caller-supplied NativeBindings when present.
14. Validate the complete aggregate before immutable persistence.

The algorithm is parser-neutral. Normal CI uses fake parser observations and
frozen fixtures; Docling is not required.

## Validation

Validation builds temporary in-memory ID indexes and checks:

- duplicate IDs and deterministic ordering;
- missing resources and presentation units;
- missing declared Division parents and inconsistent parent/child links;
- hierarchy cycles;
- exactly one direct owner per ContentNode;
- same-resource ownership;
- title and direct-node references;
- exact UTF-8 SHA-256 values;
- complete, ordered character ranges and bounding boxes;
- valid selector combinations;
- globally unique ContentNode and Representation selector IDs;
- Representation subjects, same-resource selectors, captions, and artifacts;
- relation endpoints and evidence nodes;
- processing input and output artifacts;
- NativeBinding canonical IDs, cross-descriptor payload facts, and retained T01
  pointers.

Public validation raises typed errors rather than raw `KeyError` or
`AssertionError`:

- `CanonicalContentError`
- `CanonicalContentValidationError`
- `CanonicalReferenceError`
- `CanonicalOwnershipError`
- `NativeBindingValidationError`

Errors contain logical IDs, not source text, native payloads, credentials, or
local paths.

## Persistence And Compatibility

`IngestService` writes `canonical-content.json` immutably after v2 document and
evidence output and T01 native descriptors have stable identities. It writes the
public serializer's exact UTF-8 bytes as `application/json`. An idempotent retry
must match both those bytes and that media type; semantically similar JSON with
different spacing is not silently normalized or rewritten. It then adds:

- `canonical_content` to `manifest.json` artifacts;
- `canonical_content` to provenance `artifact_uris`;
- a canonical-content `ArtifactRef` to `IngestResult.artifacts`;
- `canonical_content_key` with a backward-compatible default;
- canonical-content bytes to measured output usage.

Existing document IDs, page IDs, block IDs, section IDs, raw parser keys, parser
artifact IDs, T01 descriptor locations, v2 schema versions, and normal Python or
CLI workflows remain unchanged. Deleting a document removes its document-local
canonical-content file with the document tree. Global native-descriptor retention
and deletion remain T07 responsibilities.

## Consumers

Current consumers are Ingest persistence, audit tests, and Python callers that
choose the new artifact. Future bounded consumers are:

- T06 non-copying segmentation views over node IDs and spans;
- T08 Source Graph and provenance-address services;
- T09 DataForge handoff projections for paragraph Q/A and composite Knowledge
  Units.

DataForge does not normally need parser-native payload bytes.

## T02 Non-Goals

T02 does not implement capability registry, adaptive routing, fusion redesign,
segmentation, reuse, retention, purge, Source Graph repository, provenance
address resolution, graph database, graph traversal, GraphRAG, semantic entities,
Knowledge Units, Q/A generation, embeddings, vector storage, tokenizer behavior,
SDK commands, or CLI changes.
