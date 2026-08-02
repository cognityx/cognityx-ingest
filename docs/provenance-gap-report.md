# Provenance Fixture Gap Report

Cognityx Ingest must turn a source PDF into trustworthy pages, sections,
objects, and references. This report compares that ordinary-language goal with
the frozen Lunavane policy fixture before production parsing changes begin.

## Where This Work Fits

```text
frozen policy PDF + human-written expected truth
                       ↓
               Cognityx Ingest tests
                       ↓
       canonical provenance.json with stable anchors
                       ↓
                   DataForge
```

The expected truth is technically called the test oracle. It describes the
source document independently of Basic, PyMuPDF, Docling, or Cognityx
Inference. Tests compare the canonical Cognityx result with that oracle rather
than copying the current implementation's output.

## Current Result

The provenance suite has 42 focused checks when both optional parser extras
are installed:

- 34 pass; and
- 8 are strict expected failures linked to the gap identifiers below.

In the default dependency environment, the two optional parser modules skip
cleanly. Locked local capability runs were also completed with PyMuPDF 1.28.0
and Docling 2.117.0.

The production increments now cover P-04 through P-10 using normalized
parser observations and backend-neutral deterministic structure. Later parsing
objectives remain executable expected failures.

## Already Passing

- `P-01`: the PDF SHA-256, SourceAsset identity, and immutable Blob lineage are
  preserved.
- `P-02`: identical bytes under different filenames and bundles retain separate
  logical SourceAssets while reusing one physical Blob.
- `P-03`: all 19 zero-based physical indexes and one-based sequence numbers are
  stable.
- `P-06`: canonical content blocks preserve stable page-local IDs, typed reading
  order, exact parser text, page identity, observed bounding boxes, method and
  confidence.
- `P-07`: numbered headings and appendices preserve number, title, level,
  parent, path and heading anchors without using a model.
- `P-08`: sections sharing one page have distinct exact block spans, while
  repeated headers and footers remain outside section content.
- `P-09`: Section 4.3 continues across physical pages 4 and 5 only when
  unheaded top-page content flows from the active section before heading 4.4.
  The global heading interval also carries parent Section 4 across both pages,
  and provenance includes a resolved canonical continuation relation.
- `P-10`: Section 4.4 ends on page 5 and records an explicit deterministic
  false-continuation status because page 6 starts with heading 5. Its rejected
  continuation relation retains the source anchor and reason without inventing
  a target.
- `P-11` and `P-12`: Table 9-1 is one logical 52-row, five-column table across
  physical pages 10–12. Its ordered parts retain repeated headers, merged group
  rows, parser anchors and Section 9.2 ownership without duplicating data rows.
- `P-13` and `P-14`: Figure 10-1 retains image and caption anchors under Section
  10.2. Footnotes 1 and 2 retain marker/note anchors, exact text and owning
  sections. Canonical relations connect captions and markers to stable objects.
- Basic parser capability: exact page text and fixture canaries are present,
  while richer structure is honestly absent.
- `P-26`: bounded inference accepts only existing allowlisted anchors and
  rejects invented anchors deterministically.
- `P-27` and `P-28`: immutable `provenance.json` contains the complete rich
  structure, lineage, decisions, relation eligibility and stable artifact
  Storage URIs. A DataForge-only consumer builds candidate spans without
  receiving or reopening the PDF.
- `D-01` through `D-04`: the handoff retains document, SourceAsset, SHA-256,
  section, page, block and evidence identities and validates referenced
  anchors before downstream use.
- `D-05`: the handoff has no vector-database or embedding schema.
- `P-30`: stored v1 documents remain readable.
- Existing lifecycle tests already cover safe failure, document/run deletion,
  logical SourceAsset deletion, and Storage-owned Blob cleanup boundaries.

## Configuration Gaps

- Default CI does not install `cognityx-ingest[pymupdf]` or
  `cognityx-ingest[docling]`, so the optional capability modules skip there.
  Dedicated locked local runs pass each backend's declared baseline capability.
- The visually verified PDF is the authoritative acceptance input. Editable
  source-authoring files and Office conversion are outside the ingest contract
  and do not block these tests.
- The normal UV cache points at a read-only mounted location. A task-specific
  cache works, but dependency resolution also needs network access.

## Parser Limitations

- The Basic parser correctly provides page text but does not claim blocks,
  headings, tables, figures, links, or footnotes.
- The PyMuPDF adapter observes native labels separately from visible labels and
  deterministically classifies recurring positioned headers and footers. The
  frozen PDF's visible labels are `i`, `ii`, `1`–`14`, and `A-1`–`C-1`.
- The PyMuPDF adapter retains native link rectangles. Cognityx maps those
  observations to canonical source blocks and preserves the visible link text,
  destination, method, and confidence without exposing parser-private types.
- The Docling adapter records text blocks, table/figure objects, and heading
  candidates. Cognityx-owned deterministic stages assemble canonical table
  parts, cells, merged spans, repeated headers, figure ownership, footnotes,
  and caption relations from normalized observations.

## Canonical Fusion

### GAP-PARSER-FUSION

`compare` performs deterministic fact-level fusion. It aligns physical pages,
retains PyMuPDF-native links, labels, and rectangles alongside Docling
structure, tables, figures, and captions, and uses Basic text only when richer
backends have no usable text. Equivalent facts are deduplicated with stable
IDs; source backend, method, confidence, raw parser artifacts, and explicit
conflict diagnostics remain auditable. Reversing backend order produces the
same canonical result.

## Deterministic Logic Required

### Deterministic relations

The following use literal detection and exact lookup:

- `P-15` and `P-16`: exact and plural numbered-section references;
- `P-17` and `P-18`: appendix and printed-page references;
- `P-19` and `P-20`: native hyperlinks and visible plain URLs; and
- `P-23`: explicit unresolved output for the absent Moonlit Conduct Handbook.

`P-21` and `P-22` cross-document/version resolution remain fixture-blocked.

Models must not be used to invent missing documents or resolve exact numbered
references.

## Bounded Inference Required

### GAP-BOUNDED-AMBIGUITY

The only main-fixture case that genuinely needs optional model help is `P-24`,
the phrase “the relevant travel rule.” It has two plausible candidates:
Section 10.2 and the related travel manual's Section 6.4. The deterministic
pipeline must create the ambiguity task. Cognityx Inference may propose one of
those existing anchors, but deterministic validation remains authoritative.

`P-25`, `P-26`, and `P-29` also require complete decision/configuration identity
around that bounded task. The proposal validation boundary already passes; the
fixture-driven ambiguity task and reproducible canonical decision identity do
not yet exist.

## DataForge Handoff

### GAP-DATAFORGE-RICH

The handoff is readable without the PDF and contains the rich anchors and
relations needed by `D-02` through `D-04` and `D-06`. Every derived unit must
retain document, SourceAsset, SHA-256, section, page, block, and evidence IDs.
Only concrete observed or resolved relations are marked gold; rejected,
ambiguous and unresolved results remain non-gold. DataForge generation logic
does not belong in Ingest.

## Fixture Gaps

### GAP-FIXTURE-SOURCES

The authoritative main fixture is the visually verified PDF stored as
`main_policy_v2.pdf`. Editable source-authoring material is optional and does
not block PDF ingestion, canonical provenance, or the DataForge handoff.

The following fixtures are still required:

- related travel-policy v1 DOCX/PDF;
- related travel-policy v2 DOCX/PDF; and
- a mixed scanned/native PDF for OCR routing and page-identity tests.

Until the travel fixtures arrive, cross-document and supersedes relations are
correctly expected to remain unresolved. Until the mixed PDF arrives, no OCR
acceptance claim can be made.

## Objective Status

| Objectives | Baseline status | Required owner |
| --- | --- | --- |
| P-01–P-03 | Pass | Existing SourceAsset and canonical page flow |
| P-04–P-05 | Pass | Deterministic visible-label and repeated-region analysis |
| P-06–P-08 | Pass | Deterministic block typing, hierarchy and same-page spans |
| P-09–P-10 | Pass | Deterministic true and false continuation handling |
| P-11–P-12 | Pass | One logical multi-page table with auditable parts |
| P-13–P-14 | Pass | Owned figures, captions and footnotes with relations |
| P-15–P-20 | Pass | Deterministic relation detection and exact lookup |
| P-21–P-22 | Fixture-blocked, then deterministic | Related travel fixtures |
| P-23 | Pass | Deterministic unresolved emission |
| P-24 | Expected failure | Deterministic ambiguity task, optional bounded inference |
| P-25 | Pass | Canonical fusion and parser decision trace |
| P-26 | Pass | Existing bounded inference validator |
| P-27–P-28 | Pass | Complete provenance-only DataForge handoff |
| P-29 | Expected failure | Stable fusion and decision identity |
| P-30 | Pass | Existing compatibility reader |
| D-01 | Pass | Existing Storage URI handoff |
| D-02–D-04 | Pass | Validated rich canonical provenance anchors |
| D-05 | Pass | Existing backend-neutral contract |
| D-06 | Pass | Explicit gold eligibility and unresolved safeguards |

## Deferred Roadmap

Bounded ambiguity remains deferred until its related travel-policy fixture
arrives. Cross-document travel-policy resolution, mixed native/scanned OCR and
DataForge dataset generation also remain intentionally outside this increment.
