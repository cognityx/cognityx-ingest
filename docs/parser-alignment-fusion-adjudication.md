# Parser Alignment, Fusion, and Adjudication

## Background and Purpose

A document can be read by more than one parser. One parser may preserve tables
and headings well, while another may preserve links and page labels well. Their
answers can agree, add different useful facts, disagree, or lack enough evidence
for a safe decision.

T05 keeps those answers visible. It records what each parser observed, determines
which answers refer to the same source region, and only then applies a reviewed
rule to decide what can be accepted. This complete decision trail is technically
called parser alignment, fusion, and adjudication.

## Application Map

```text
T03 capability evidence
        |
        v
T04 parser routing plan
        |
        v
existing parser execution
        |
        v
T05 source-region aggregation and alignment
        |
        v
T05 fact fusion
        |
        v
T05 adjudication
        |
        v
compatibility ExtractionResult + observations.json + fusion-decisions.json
        |
        v
T06 segmentation views later
```

Routing decides which parsers should run. T05 does not route or run a parser.
It receives completed parser results. T06 will later describe reusable source
segments without copying source text.

## Problem Solved

The older compare path returned one value for each legacy field. That shape is
still useful, but the choice alone cannot explain whether parsers agreed or
whether one value was selected only so older code could continue working.

T05 adds an immutable decision artifact. It records every observation by ID,
the source-location evidence used to group observations, the policy applied to
each fact, and whether evidence is suitable for gold support. Gold support means
evidence approved for trusted downstream training or evaluation.

## Observation Model

A `ParserObservation` records one parser's exact value for one fact. For example,
Docling may observe `object_type=table` while another parser observes
`object_type=paragraph_text` for the same region.

Each observation retains parser ID and version, fact name, exact typed value
hash, method, optional confidence, source region, epistemic state, and stable
occurrence and observation IDs. An epistemic state explains how the fact is
known, such as observed, inferred, ambiguous, contradicted, or unresolved.
Native artifact IDs and pointers appear only when a caller has real T01 identity.

The value is stored once in the observation set. Decisions reference observation
IDs instead of copying accepted text repeatedly. `ParserObservationSet` is not a
temporary calculation: Ingest writes its exact deterministic bytes to
`parser/observations.json`. Audit tools, later segmentation work, and later
Source Graph work can reload that file and resolve every observation ID retained
by a decision.

`ObservationValue` accepts JSON-safe scalar, sequence, and mapping values. It
preserves strings exactly, rejects non-finite numbers, serializes deterministic
JSON, and hashes those exact bytes. Mutable caller collections are not retained.

## Source-Region Model

An `ObservationSourceRegion` says where an observation came from without quoting
the source text. It can use a resource, physical page, presentation unit, anchor,
selector, character span, bounding box, or text-span digest.

A bounding box is four coordinates around a region. A selector is a stable
source-location record such as a page and character range. At least one real
locator is required, coordinates must be finite and ordered, and source text is
never copied into the region record.

## Alignment Algorithm

Alignment asks whether two parser-native source regions describe the same place.
It does not decide whether their fact values agree.

Before cross-parser matching, T05 groups every observation with the same
`source_region_id`. For example, one parser block's text, block type, bounding
box, and reading order become one initial region. This happens even when only one
parser ran. Repeated location fields must be compatible; contradictory page,
anchor, character-span, or geometry evidence fails validation.

Cross-parser matching compares those region aggregates once. It does not compare
every text fact with every geometry or structure fact. Alignment records retain
deterministic representative observation IDs for compatibility and explicit
left and right source-region IDs to make their region-level meaning clear.

The deterministic priority is:

1. Exact explicit source-region ID.
2. Exact resource, page, and source-anchor identity.
3. Exact shared selector.
4. Exact character span.
5. Unique mutual-best bounding-box overlap on the same page and compatible fact family.
6. Exact normalized text digest plus occurrence when stronger location evidence is absent.

Text normalization collapses whitespace only for alignment. It never changes the
original value. Semantic similarity, embeddings, LLM calls, fuzzy paraphrase
matching, and arbitrary nearest-neighbor choices are excluded.

## Exact, Candidate, and Ambiguous Alignment

Exact evidence uses matching source identities or spans. An accepted candidate
uses geometry only when each observation is the other's unique best match above
the configured overlap threshold.

Geometry uses intersection over union (IoU), which compares the shared rectangle
area with the total covered area. Coordinates are never averaged. When two
candidates tie or remain plausible, T05 records both as ambiguous and does not
greedily connect them. Ambiguous alignment produces unresolved decisions and is
never gold support.

Evidence priority is operational rather than descriptive. If region A has an
exact anchor match with region B and also overlaps region C, the exact A-B edge
is accepted. The weaker A-C geometry candidate remains in the audit trail as
`superseded`, but it cannot make the accepted A-B group ambiguous. Rejected and
superseded edges never change group state.

## Fact Fusion and Four States

After alignment, T05 groups observations by source region and fact. Fusion asks
how those facts relate, but it does not execute policy by itself.

- `agreement`: at least two parsers report the same fact with identical typed bytes;
- `complementary`: distinct facts can coexist, such as a caption and image geometry;
- `conflict`: the same fact has incompatible values or segmentation boundaries;
- `unresolved`: evidence or alignment is insufficient for a safe decision.

These names are stable. Conflict and unresolved evidence are never renamed as
agreement merely because a compatibility field needs one value.

## Adjudication Policies

Adjudication applies a fact-specific reviewed rule. A
`FactAdjudicationPolicy` is bounded data, not a Python callable or expression.
Its strategies have these concrete behaviors:

- `exact-agreement` accepts equivalent bytes only when at least two distinct
  parser IDs support them; incompatible values remain conflict with no winner.
- `retain-complementary` accepts an independently valid fact but never renames
  incompatible values for the same fact as complementary.
- `preserve-conflict` retains and rejects every incompatible observation without
  choosing one.
- `prefer-explicit-value` accepts only observations matching an exact reviewed
  value, rejects the others, and keeps the state as conflict.
- `require-review` makes incompatible values unresolved, accepts none, and
  records a required review action.
- `preserve-segmentation-variants` retains split and merged alternatives as
  conflict under the reviewed resolution without creating a T06 boundary.

Policies may target one exact fact or one bounded fact family: textual,
geometry, segmentation, structure, relation, or object. An exact fact policy
overrides its family policy. Duplicate ownership and
`retain_all_observations=false` are rejected because T05 never deletes evidence.

There is no global backend-precedence table. A policy can accept one observation
inside a conflict, but the state remains `conflict` and every rejected
observation remains referenced. The fusion artifact stores complete policy
records, including strategy, preferred values, resolution, and flags. Strict
reload validation reapplies those records and requires identical decisions.

## Confidence Behavior

Confidence is supporting evidence. T05 does not average unrelated confidence
values, treat missing confidence as zero, or choose a winner solely because one
number is larger. Confidence does not make evidence gold eligible.

An `ExtractedPage` has no page-level confidence field. Its T05 observations
therefore record `confidence=null`; they never manufacture `1.0`. Existing
legacy fact-source confidence remains unchanged where compatibility requires it.

A future policy may use a typed minimum confidence only when the policy and its
tests state that rule explicitly.

## Seven Frozen Examples

### Agreement Text

Docling and PyMuPDF return the exact same approval sentence. The state is
`agreement`; both observation IDs are accepted and retained.

### Complementary Link and Structure

One parser reports `owner_division` and another reports `native_link_target`.
Both facts are accepted separately. They are complementary, not merged into one
invented value.

### Split Versus Merged

Docling reports two blocks while PyMuPDF reports one block covering the same
paragraph. The state remains `conflict`, with resolution
`align-by-source-span-then-preserve-both-segmentation-observations`.

Both segmentation observations remain available. T05 does not create a
canonical chunk boundary; that would cross into T06.

### Reading-Order Conflict

The parsers disagree about two-column reading order. The state is `unresolved`,
no observation is accepted, gold eligibility is false, and the required action
is `selective-review-or-third-parser`.

### Bounding-Box Conflict

The parsers report different coordinates. The state is `conflict`, resolution
is `fact-specific-policy`, and both boxes remain unchanged. T05 does not average
their coordinates.

### Table Versus Text

One parser recognizes a table while another reports paragraph text. The reviewed
policy accepts `table` with reason `richer-validated-structure`, but the state
remains `conflict` and the rejected observation remains retained.

### Caption and Image Geometry

One parser reports a caption and another reports an image bounding box. Both
facts are accepted separately as `complementary` evidence.

## Compatibility Projection

Existing callers still receive `ExtractionResult`. This is a compatibility
projection: a one-value view needed by the old record shape.

Agreement projects an accepted observation. Complementary facts project each
independent accepted fact. A conflict with an explicitly accepted observation
projects that observation while retaining conflict state. If conflict or
unresolved evidence has no accepted observation, the old deterministic field is
marked as a compatibility projection with false gold eligibility.

The authoritative state remains in `ParserFusionArtifact`. Compatibility fields
must not be described as accepted evidence merely because they are populated.

## v1 Compatibility and v3.2 Artifact

The existing raw compare artifact remains available as
`cognityx.ingest.parser-fusion/v1`. The additive decision artifact uses
`cognityx.ingest.parser-fusion/v3.2`.

The v1 bytes continue through the parser-native compatibility path. The v3.2
artifact is Cognityx processing output, not parser-native data.

## Persistence Layout

For document `<document-id>`, `IngestService` writes:

```text
ingest/documents/<document-id>/parser/observations.json
ingest/documents/<document-id>/parser/fusion-decisions.json
```

Both artifacts use `application/json`. Their stable artifact IDs are
`art-<document-id>-parser_observations` and
`art-<document-id>-parser_fusion_decisions`. The fusion artifact records both the
observation-set ID and SHA-256 over exact `ParserObservationSet.to_json_bytes()`.
Ingest reloads both public artifacts and validates the complete binding before it
writes either one.

Manifest, artifact references, and output-byte accounting include both files.
Provenance summarizes only the observation schema, set ID, artifact URI,
SHA-256, observation count, and parser IDs; it does not copy observation values.
Fusion provenance retains its decision summary and binding.

Existing document-prefix deletion removes both files. T07 owns future retention
and purge policy.

## Identity and Integrity Validation

Strict readers recompute every public stable ID from its documented canonical
identity. This covers observations, observation sets, alignment evidence,
aligned groups, fact decisions, region decisions, and the fusion aggregate.
A syntactically valid replacement ID is rejected.

Validation also recomputes parser and version indexes, exact state counts,
region membership, representative endpoints, group parser IDs, and one region
summary per final region. It reruns region alignment with the persisted threshold
and reapplies retained policy records. Changing observation bytes while keeping
the same observation-set ID fails the SHA binding; changing policy strategy,
preferred values, or resolution fails fusion integrity.

## Canonical Fact Sources

Compatibility page and block fact sources can carry parser ID, method,
confidence, observation ID, decision ID, adjudication state, accepted or
rejected status, compatibility-projection status, and gold eligibility.

Canonical text nodes retain complete T05 references through optional typed fact
sources. Empty metadata is omitted, preserving older canonical-content bytes and
readability.

## Consumers

- `ParserRouter(mode="compare")` calls T05 after parser execution.
- `IngestService` validates and persists both additive artifacts.
- Canonical-content builders retain fact-source references.
- Audit tools inspect observations, policies, conflicts, and unresolved states.
- T06 can later consume canonical IDs and spans for segmentation views.
- T08 can later use accepted source evidence when building the Source Graph.
- DataForge can later exclude unsafe evidence from gold support.

T06 may consume references but remains the owner of non-copying segmentation
views. T08 may consume accepted evidence but remains the owner of source graph
nodes and provenance-address resolution. T05 creates neither API.

## Gold-Eligibility Rules

Gold eligibility is explicit. It does not come from confidence alone.

- Ambiguous alignment is never gold eligible.
- Unresolved decisions are never gold eligible.
- Conflict regions are never gold eligible as a whole.
- An unaccepted conflict observation is never gold support.
- Ambiguous or unresolved relations remain excluded from validated support.

## T05 Non-Goals

T05 does not route or execute parsers, call a proposal provider, network service,
or LLM, store another copy of parser-native payloads, materialize segmentation
views, implement a graph database or Source Graph repository, resolve provenance
addresses, generate DataForge paragraph Q/A or Knowledge Units, create embeddings
or vector indexes, or change SDK and CLI behavior.

## T06 and T08 Handoff Boundaries

T06 receives canonical IDs, selectors, spans, and retained segmentation
observations. It may create non-copying reusable views, but it must not turn one
T05 segmentation conflict into authoritative copied content.

T08 receives accepted and retained evidence identities for Source Graph and
provenance-address work. T05 does not create graph repositories or resolve
candidate targets. Ambiguous relation targets remain ambiguous and non-gold.
