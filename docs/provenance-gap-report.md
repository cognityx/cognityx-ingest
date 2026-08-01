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

## Baseline Result

The Jobs 1–4 baseline adds 23 focused checks when both optional parser extras
are installed:

- 12 pass; and
- 11 are strict expected failures linked to the gap identifiers below.

In the default dependency environment, the two optional parser modules skip
cleanly. Locked local capability runs were also completed with PyMuPDF 1.28.0
and Docling 2.117.0.

No production file under `src/` was changed.

## Already Passing

- `P-01`: the PDF SHA-256, SourceAsset identity, and immutable Blob lineage are
  preserved.
- `P-02`: identical bytes under different filenames and bundles retain separate
  logical SourceAssets while reusing one physical Blob.
- `P-03`: all 19 zero-based physical indexes and one-based sequence numbers are
  stable.
- Basic parser capability: exact page text and fixture canaries are present,
  while richer structure is honestly absent.
- `P-26`: bounded inference accepts only existing allowlisted anchors and
  rejects invented anchors deterministically.
- `P-27`, partial: `provenance.json` is immutable and contains pages, blocks,
  sections, evidence, relations, decisions, and unresolved collections. The
  collections do not yet contain all rich fixture facts.
- `P-28` and `D-01`, baseline: DataForge can load `provenance.json` by Storage
  URI without reopening the PDF.
- `D-05`: the handoff has no vector-database or embedding schema.
- `P-30`: stored v1 documents remain readable.
- Existing lifecycle tests already cover safe failure, document/run deletion,
  logical SourceAsset deletion, and Storage-owned Blob cleanup boundaries.

## Configuration Gaps

- Default CI does not install `cognityx-ingest[pymupdf]` or
  `cognityx-ingest[docling]`, so the optional capability modules skip there.
  Dedicated locked local runs pass each backend's declared baseline capability.
- No approved Office converter is installed and the source DOCX is absent. All
  19 supplied PDF pages were rendered and visually inspected, but visual
  comparison against a newly converted DOCX cannot be performed.
- The normal UV cache points at a read-only mounted location. A task-specific
  cache works, but dependency resolution also needs network access.

## Parser Limitations

- The Basic parser correctly provides page text but does not claim blocks,
  headings, tables, figures, links, or footnotes.
- The current PyMuPDF adapter can observe blocks, images, links, annotations,
  and native page labels. It currently treats native labels as printed labels,
  but this PDF's native labels are `1`–`19` while visible labels are `i`, `ii`,
  `1`–`14`, and `A-1`–`C-1`.
- The current PyMuPDF relation shape does not retain the complete source text
  rectangle required for native hyperlink provenance.
- The current Docling normalization records text blocks, table/figure objects,
  and heading candidates, but not canonical table cells, merged spans,
  repeated headers, table page parts, footnote ownership, or complete caption
  relations.

## Canonical Fusion Required

### GAP-PARSER-FUSION

`compare` currently selects one parser result by richness score. It does not
combine complementary facts. The rich profile must retain PyMuPDF-native links,
labels, and rectangles alongside Docling structure, tables, figures, and
captions. This is required by the rich-profile acceptance decision and `P-25`.

## Deterministic Logic Required

### GAP-RICH-STRUCTURE

The following work must be deterministic or parser-observed, not delegated to
a model:

- `P-04` and `P-05`: visible page-label and repeated header/footer analysis;
- `P-06` through `P-08`: typed reading order, hierarchy, and exact block-level
  section boundaries;
- `P-09` and `P-10`: Section 4.3 continuation across physical indexes 4–5 and
  rejection of the deliberate false continuation after Section 4.4;
- `P-11` and `P-12`: one logical Table 9-1 across indexes 10–12, including 52
  data rows, five columns, repeated headers, and merged five-column group rows;
  and
- `P-13` and `P-14`: figure/caption and footnote-marker ownership.

### GAP-DETERMINISTIC-RELATIONS

The following must use literal detection and exact lookup:

- `P-15` and `P-16`: exact and plural numbered-section references;
- `P-17` and `P-18`: appendix and printed-page references;
- `P-19` and `P-20`: native hyperlinks and visible plain URLs;
- `P-21` and `P-22`: cross-document/version resolution once related fixtures
  exist; and
- `P-23`: explicit unresolved output for the absent Moonlit Conduct Handbook.

Models must not be used to invent missing documents or resolve exact numbered
references.

## Bounded Inference Required

The only main-fixture case that genuinely needs optional model help is `P-24`,
the phrase “the relevant travel rule.” It has two plausible candidates:
Section 10.2 and the related travel manual's Section 6.4. The deterministic
pipeline must create the ambiguity task. Cognityx Inference may propose one of
those existing anchors, but deterministic validation remains authoritative.

`P-25`, `P-26`, and `P-29` also require complete decision/configuration identity
around that bounded task. The proposal validation boundary already passes; the
fixture-driven ambiguity task and reproducible canonical decision identity do
not yet exist.

## DataForge Gap

### GAP-DATAFORGE-RICH

The baseline handoff is readable without the PDF, but `D-02` through `D-04` and
`D-06` require the rich anchors and relations above. Every derived unit must
retain document, SourceAsset, SHA-256, section, page, block, and evidence IDs.
Ambiguous and unresolved relations must remain non-gold. DataForge generation
logic does not belong in Ingest.

## Fixture Gaps

### GAP-FIXTURE-SOURCES

The supplied inputs contain the verified main-policy PDF and the specification
DOCX, but not the editable main-policy DOCX. The repository therefore freezes
the supplied PDF and extracted diagram without fabricating an editable source
or claiming a new conversion.

The following fixtures are still required:

- `main_policy_v2.docx`, for approved conversion and visual equivalence;
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
| P-04–P-14 | Expected failure | Parser observations plus deterministic canonical structure |
| P-15–P-20 | Expected failure | Deterministic relation detection |
| P-21–P-22 | Fixture-blocked, then deterministic | Related travel fixtures |
| P-23 | Expected failure | Deterministic unresolved emission |
| P-24 | Expected failure | Deterministic ambiguity task, optional bounded inference |
| P-25 | Partial | Canonical fusion and decision trace |
| P-26 | Pass | Existing bounded inference validator |
| P-27–P-28 | Partial | Rich canonical output and DataForge anchors |
| P-29 | Expected failure | Stable fusion and decision identity |
| P-30 | Pass | Existing compatibility reader |
| D-01 | Pass | Existing Storage URI handoff |
| D-02–D-04 | Partial/expected failure | Rich canonical provenance |
| D-05 | Pass | Existing backend-neutral contract |
| D-06 | Expected failure | Relation strength and unresolved safeguards |

## Next Increment

Production work must begin with printed labels and repeated header/footer
handling. Each later group remains separate: section boundaries, continuation,
table identity, deterministic references, parser fusion, bounded ambiguity,
then the complete DataForge handoff.
