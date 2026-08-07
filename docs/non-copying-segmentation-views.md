# Non-Copying Segmentation Views

## Background and purpose

One source document can be useful in several shapes. A question-answering job may
want one paragraph at a time. Search may want a short sentence with its neighbours.
Another reader may search a small passage but return the whole section around it.

Cognityx Ingest represents each shape as a **segmentation view**. A segmentation
view is a derived read model: it is another way to read established content, not
another owner of that content. This module sits after canonical content and parser
alignment, and before retention, provenance-address, DataForge, and retrieval work:

```text
source and parser-native artifacts
              |
              v
canonical content and T05 parser decisions
              |
              v
non-copying segmentation views (T06)
              |
              +--> retention and reuse policy (T07)
              +--> source graph and provenance addresses (T08)
              +--> DataForge and retrieval consumers
```

The ordinary-language rule is simple: store each extracted passage once, then
point to it whenever another view needs it.

## Canonical content and segmentation views

Canonical content is the stable parser-neutral description of what Ingest found.
Its `ContentNode.content.text` field owns the extracted text. A segmentation view
contains references to those nodes. It cannot change node text, node identity, or
division ownership.

This distinction matters because six views can disagree about boundaries without
creating six copies of the document. It also means a later bug or policy change in
one view cannot silently rewrite canonical content.

## NodeSpan

A `NodeSpan` is the core reference:

```python
NodeSpan(node_id="pol-p2")
NodeSpan(node_id="pol-p5", char_start=0, char_end=62)
```

The first form references the complete canonical node. The second references the
half-open range `[0, 62)`, which means character 0 is included and character 62 is
not. Both endpoints must appear together, and the end must fit the canonical text.

A span does not carry text, a source path, a selector, an excerpt, normalized text,
or an embedding. Those values would create another source-text owner.

## Why segment text is absent

`SegmentationSegment.text` is a compatibility property that always returns `None`.
It is not a stored field. JSON output does not contain `"text": null`; the field is
absent entirely.

This is stricter than merely promising not to use the field. Strict readers reject
text-like fields, and a service bound to canonical content also checks that complete
canonical passages do not appear inside any serialized view value.

## The six strategies

The strategy vocabulary is frozen:

1. `paragraph`
2. `direct-division`
3. `parser-native-structure`
4. `sentence-safe-fixed-size`
5. `sentence-window`
6. `parent-child`

Each strategy is an alternative view. They are never averaged or fused into one
canonical chunk boundary.

## Paragraph example

The paragraph strategy creates one segment for every canonical `ContentNode` whose
kind is `paragraph`, in canonical order:

```python
service = SegmentationViewService.from_canonical(canonical_content)
view = service.build_paragraph("paragraph-v1")
```

Adjacent paragraphs remain separate. Each segment contains one whole-node span.
The focused fixture maps `para-1` through `para-5` to `pol-p1` through `pol-p5`.

## Direct-division example

A division is a logical part of a document, such as a section. A direct-division
view references only the nodes owned directly by that division:

```python
view = service.build_direct_division(
    "section-direct-v1",
    division_ids=("div-policy-4.2",),
)
```

The builder follows `CanonicalContentArtifact.direct_nodes(...)`. It does not copy
the section text and does not include descendant content when the strategy says
direct.

## Parser-native structure example

Some parser or chunker boundaries carry useful observations that do not belong in
the parser-neutral canonical model. A parser-native view retains three facts:

- the retained T01 native artifact identity;
- the chunker's native pointer, such as `#/chunks/0`;
- the canonical node spans supported by that chunk.

The pointer is evidence of the parser/chunker observation. It does not become a
canonical boundary. T06 requires a real retained artifact identity but does not
pretend that a chunker pointer is necessarily a JSON pointer inside the opaque
parser payload. It does not import Docling private classes, reopen the PDF, rerun
Docling, or copy native chunk text.

## Sentence-safe fixed-size boundary

The fixed-size strategy groups sentence-aligned spans under a token budget. T06
does not install or download a tokenizer. Application composition injects a narrow
`TokenCounter` with one method:

```python
class LocalCounter:
    def count_tokens(self, text: str) -> int:
        return len(text.split())
```

Counting sees a transient canonical slice while building references. The text is
not written into the segment. If one sentence exceeds the budget, T06 keeps that
sentence intact rather than cutting through it. Partial canonical nodes are
represented with character ranges.

## Sentence-window example

A sentence-window segment keeps its matching **seed** separate from its surrounding
**context**:

```text
context: pol-p1
seed:    pol-p2
context: pol-p3
```

The roles remain separate fields. T06 does not flatten the three references into a
new authoritative node or copied passage.

## Parent-child example

Parent-child retrieval separates the small unit used to match from the larger unit
returned to the caller:

```text
retrieval span: pol-p2
return scope:   div-policy-4.2
```

The child remains a `NodeSpan`. The return scope remains a canonical division ID.
When requested, the service reconstructs the division through canonical subtree
APIs. It does not store a duplicate parent chunk.

## Read-time reconstruction

Text is available when a caller actually needs it:

```python
text = service.resolve_span(NodeSpan("pol-p2"))
slices = service.resolve_segment_spans("para-2")
```

The service looks up the canonical node, applies an optional character range, and
returns the transient value. Multi-node segments return an ordered tuple of slices
instead of inventing joining rules. Reconstruction does not mutate or persist the
canonical artifact, view, segment, or result.

## Deterministic identity

Every view has a `cache_identity`. It is a SHA-256 over:

- the SHA-256 of the exact canonical-content bytes;
- the strategy;
- the deterministic profile;
- the exact segment-reference structure.

The identity contains no source text. T06 does not create a physical cache; the
identity is available so T07 can govern reuse, retention, and purge consistently.

## Native artifact relationship

T01 owns immutable parser-native payloads, descriptors, hashes, and retained native
pointers. T06 only refers to a real native artifact from a parser-native view. It
does not change T01 paths, payload bytes, retention classes, or pointer meaning.

## Overlapping views

A paragraph view may reference all of `pol-p2`. A fixed-size view may place it next
to `pol-p1`. A sentence window may use it as a seed, while a parent-child view uses
it as a retrieval child. All are valid at the same time.

These overlaps do not rewrite the canonical node. No view owns more authority than
another merely because it has a different boundary.

## No fused canonical chunks

T06 intentionally exposes no `canonical_chunks`, `fused_chunks`, `winning_view`, or
`accepted_boundary` API. Parser fusion in T05 decides facts, not universal chunk
boundaries. Different segmentation strategies solve different consumer problems,
so forcing them into one winner would discard useful information.

## T05 relationship

T05 aligns parser observations and records agreement, conflict, adjudication, and
unresolved states. T06 may use established canonical IDs and retained parser-native
identity, but it does not redo alignment, reinterpret conflicts, or modify T05
observation and fusion artifacts.

## T07 retention boundary

T06 creates deterministic serializable values and cache identities. It does not
create an always-written artifact, database, cache store, retention scheduler, or
purge operation. T07 owns whether a physical extraction or view is reused, held,
retained, or safely removed.

## Retrieval and DataForge boundary

Ingest creates candidate source views. Retrieval/DataForge decides which view fits
a user query, performs ranking or rank fusion, assembles context, creates semantic
Knowledge Units, and produces training records. Those consumers can reconstruct
text from canonical IDs and later attach provenance addresses without asking T06
to become a query engine.

## T06 non-goals

T06 does not provide:

- query-time strategy selection;
- a retrieval or vector index;
- embeddings or reranking;
- neighbour expansion during a query;
- overlap deduplication;
- a trained segmentation router;
- parser execution, alignment, fusion, or adjudication;
- a database or physical retention policy;
- source graph or provenance-address resolution;
- semantic Knowledge Units or training examples;
- SDK or CLI changes.

The existing `cogni` commands and Python ingest composition continue to work as
before because T06 is an additive read-model API.
