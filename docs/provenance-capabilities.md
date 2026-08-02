# Provenance Capabilities and Enterprise Readiness

## Purpose

Cognityx Ingest converts a source PDF into a canonical, auditable, parser-independent provenance package that downstream systems can use without reopening the PDF.

The current handoff is:

```text
source PDF
    ↓
SourceAsset + immutable SHA-256 identity
    ↓
Basic / PyMuPDF / Docling observations
    ↓
Cognityx deterministic normalization and fact-level fusion
    ↓
pages + blocks + sections + objects + relations + evidence
    ↓
provenance.json
    ↓
DataForge knowledge-unit and training-data preparation
```

The main design principle is simple:

> A downstream record must be traceable to the exact source bytes, document, section, page, block, evidence, object, relation, method, and decision that created it.

## Current validated baseline

As of 2 August 2026:

- baseline commit: `f21bbc11b800997899dd88ff0e8752a7251c4d45`;
- frozen PDF SHA-256: `73a2dc18cc0ed79419a2208db93cc151e0a1fe092c96ed4322e449207f22630c`;
- full repository test result: **143 passed, 4 xfailed**;
- PyMuPDF, Docling, parser-fusion, DataForge handoff, strict documentation, package build, and diff checks pass;
- the source PDF remains byte-identical throughout the test-driven implementation.

The validated profile is strong for **native, structured, policy-style PDFs**. It is not yet a universal claim for every enterprise document type.

## Objective numbering

`P-01` through `P-30` are the formal provenance objectives defined by the frozen test specification.

This page adds:

- `P-00` as an umbrella description of the complete provenance contract; and
- `P-31` through `P-33` as proposed enterprise-readiness extensions.

The proposed objectives should become formal acceptance tests before Cognityx is described as fully enterprise-ready.

## Status legend

| Status | Meaning |
| --- | --- |
| **Pass** | Implemented and verified by production-facing tests. |
| **Deferred** | Deliberately not completed because a required fixture or bounded decision path is unavailable. |
| **Partial** | A useful foundation exists, but enterprise acceptance is not complete. |
| **Proposed** | New enterprise-readiness objective not present in the original P-01–P-30 specification. |

---

## P-00 — Canonical provenance contract

**Status: Pass for the validated native structured-PDF profile**

Cognityx produces one immutable `provenance.json` package containing source lineage, pages, blocks, section spans, document objects, relations, decisions, evidence, unresolved states, parser identity, and artifact URIs.

The contract is backend-neutral. DataForge does not need to understand PyMuPDF, Docling, Basic-parser internals, or reopen the source PDF.

**Remaining for enterprise readiness:** validate the same contract across scanned documents, larger real-world corpora, additional layouts, and production-scale workloads.

---

# Source, identity, and page provenance

## P-01 — Exact source identity

**Status: Pass**

The canonical document retains the exact frozen PDF SHA-256 and SourceAsset identity. Re-ingesting the same bytes preserves source lineage.

This prevents a later answer, knowledge unit, or training record from being detached from the exact source bytes that produced it.

## P-02 — Logical occurrence identity

**Status: Pass**

The same physical bytes may appear under different filenames, bundles, or business contexts. Storage may deduplicate the physical Blob, while Cognityx preserves every logical SourceAsset occurrence, filename, alias, bundle, and context.

This separates physical deduplication from business meaning.

## P-03 — Physical page identity

**Status: Pass**

Every page has:

- stable zero-based physical page index;
- one-based sequence number;
- stable canonical page ID;
- ordered block anchors.

The physical page identity remains stable even when printed labels use another numbering system.

## P-04 — Printed page identity

**Status: Pass**

Visible page labels are stored separately from physical page indexes and native PDF labels.

The test fixture verifies:

- Roman front matter: `i`, `ii`;
- Arabic body pages: `1` through `14`;
- appendix labels: `A-1`, `B-1`, `C-1`.

This allows a reference such as “printed page A-1” to resolve correctly rather than being confused with the seventeenth physical PDF page.

## P-05 — Header and footer handling

**Status: Pass**

Repeated headers and footers are detected using text recurrence and page geometry.

They are:

- preserved as auditable repeated-region observations;
- linked to exact page and block anchors;
- excluded from normal section and evidence text;
- never silently discarded.

---

# Blocks, hierarchy, and section spans

## P-06 — Block reading order

**Status: Pass**

Canonical blocks preserve:

- stable block ID;
- physical page;
- reading-order position;
- block type;
- exact text;
- observed bounding box;
- method;
- confidence.

Supported block types include headings, paragraphs, lists, tables, figures, captions, footnotes, hyperlinks, URLs, callouts, headers, and footers.

## P-07 — Section hierarchy

**Status: Pass**

Numbered sections and appendices are detected deterministically.

Each section preserves:

- section number;
- exact title;
- hierarchy level;
- parent section;
- section path;
- heading block;
- method and confidence.

The canonical hierarchy is Cognityx-owned and does not expose parser-private classes.

## P-08 — Same-page section boundaries

**Status: Pass**

Two or more sections on the same physical page receive distinct exact block spans.

A section begins at its own heading and ends immediately before the next heading at the same or a higher hierarchy level. A section is not assigned every block on a page merely because it appears on that page.

## P-09 — Cross-page section span

**Status: Pass**

Section spans are built over the global ordered content-block stream, not page-by-page fragments.

A section and its parent can therefore cross physical page boundaries while retaining:

- ordered page IDs;
- ordered block IDs;
- exact start and end anchors;
- evidence from every included page.

## P-10 — Continuation relation

**Status: Pass**

True continuation is represented explicitly with:

- source block;
- target block;
- `continues_on` relation;
- deterministic status;
- method;
- confidence.

A deliberate false-continuation case is recorded as rejected with a source anchor, no invented target, and an explicit reason.

---

# Tables, figures, captions, and footnotes

## P-11 — Table structure

**Status: Pass**

Canonical tables preserve:

- stable logical table ID;
- caption and caption anchor;
- owner section;
- columns, rows, and cells;
- exact source anchors;
- merged-cell and group-row information;
- parser observation source;
- method and confidence.

## P-12 — Cross-page table

**Status: Pass**

Table 9-1 is represented as one logical table across three physical pages.

The canonical object retains:

- five columns;
- 52 data rows;
- ordered page parts;
- repeated continuation headers;
- merged group rows;
- page and parser anchors;
- Section 9.2 ownership.

Repeated headers and group rows are not counted repeatedly as ordinary data rows.

## P-13 — Figure and caption

**Status: Pass**

Figure 10-1 retains:

- stable object ID;
- image/source-object anchor;
- physical page;
- owner Section 10.2;
- caption text and caption anchor;
- text-to-figure relation;
- observation method and confidence.

## P-14 — Footnote provenance

**Status: Pass**

Footnotes retain:

- marker anchor;
- note anchor;
- exact note text;
- physical page;
- owning subsection;
- marker-to-footnote relation;
- method and confidence.

A footnote remains an independently traceable evidence object rather than being flattened into unrelated page text.

---

# Deterministic references and unresolved states

## P-15 — Exact section reference

**Status: Pass**

Literal references such as `Section 7.2` resolve deterministically to the canonical section anchor.

A model is not used for exact numbered references.

## P-16 — Multiple section reference

**Status: Pass**

A phrase such as `Sections 7.2 and 11.3` creates two validated relations with one exact source anchor and two canonical targets.

## P-17 — Appendix reference

**Status: Pass**

Forward and reverse appendix references resolve to canonical appendix anchors.

Native PDF links and visible textual appendix references remain distinguishable but can point to the same canonical target.

## P-18 — Printed-page reference

**Status: Pass**

Printed-page references resolve using visible printed labels rather than physical index alone.

This is required for Roman front matter and appendix labels such as `A-1`.

## P-19 — Native hyperlink

**Status: Pass**

Embedded internal and external PDF links retain:

- exact source rectangle;
- mapped canonical source block;
- visible link text;
- internal target or external URI;
- native observation method;
- confidence.

The frozen test oracle was corrected when repeated PDF inspection proved that the internal native link was attached to `Appendix B`, not `Section 7.2`. The source PDF was not modified.

## P-20 — Plain URL

**Status: Pass**

A visible non-embedded URL is detected deterministically and stored as a URL relation with literal text, source block, target URI, status, method, and confidence.

## P-21 — Cross-document reference

**Status: Deferred — fixture blocked**

The design requires a reference containing document title, document ID, version, and section to resolve to the correct related SourceAsset and section.

The required related travel-policy fixtures do not yet exist. Cognityx therefore keeps these references unresolved rather than fabricating targets.

**Required next step:** create and freeze related travel-policy v1 and v2 documents with authoritative ground truth.

## P-22 — Version relationship

**Status: Deferred — fixture blocked**

The intended capability is to preserve `supersedes` and `superseded_by` relations without losing the older document’s provenance.

The implementation requires the related versioned travel-policy fixtures.

## P-23 — Unresolved reference

**Status: Pass**

A missing target, such as the fictional Moonlit Conduct Handbook, is emitted explicitly with:

- literal text;
- exact source anchor;
- unresolved status;
- deterministic method;
- confidence;
- reason such as `document_not_in_corpus`.

The reference is never silently dropped and is never promoted to gold provenance.

## P-24 — Ambiguous reference

**Status: Deferred**

A phrase such as `the relevant travel rule` has more than one plausible target.

The required behavior is:

1. deterministic candidate discovery;
2. explicit ambiguity task;
3. optional bounded Cognityx Inference proposal;
4. allowlist validation;
5. authoritative deterministic acceptance or rejection;
6. preservation of the complete decision trace.

The second candidate belongs to the missing travel-policy fixture, so end-to-end ambiguity resolution remains deferred.

---

# Parser control, safety, artifacts, and reproducibility

## P-25 — Decision trace and parser fusion

**Status: Pass for parser selection and fact-level fusion**

Cognityx performs backend-neutral fact-level fusion across:

- Basic parser;
- PyMuPDF;
- Docling.

It retains complementary facts rather than selecting one winner and discarding the others.

The result preserves:

- parser backend;
- parser version;
- source method;
- confidence;
- raw parser artifact URI;
- deterministic conflict diagnostics;
- stable canonical IDs;
- parser-order-independent canonical output.

## P-26 — Inference safety

**Status: Pass**

A model proposal may only select an allowed target and relation type.

Proposals containing nonexistent anchors or disallowed relation types are rejected and retained as failed decisions. The model cannot invent canonical provenance.

## P-27 — Canonical provenance artifact

**Status: Pass**

`provenance.json` version 2 contains:

- run and job identity;
- context and bundle identity;
- SourceAsset and SHA-256;
- document identity and aliases;
- pages and printed labels;
- blocks and reading order;
- repeated regions;
- sections and exact spans;
- tables, figures, captions, and footnotes;
- evidence;
- resolved relations;
- ambiguous and unresolved collections;
- decisions;
- parser and fusion identity;
- stable artifact Storage URIs.

## P-28 — DataForge handoff

**Status: Pass**

DataForge can:

- load `provenance.json` directly by Storage URI;
- build candidate source spans;
- validate that every referenced anchor exists;
- retain document, asset, SHA-256, section, page, block, and evidence identities;
- avoid reopening the source PDF.

Ingest does not generate questions, answers, summaries, embeddings, vectors, or training records. Those remain DataForge responsibilities.

## P-29 — Reproducibility and stable decision identity

**Status: Deferred / partial**

Parser-order independence and stable canonical IDs pass.

The remaining gap is complete decision/configuration identity for the fixture-driven bounded ambiguity path, including:

- candidate set;
- model/provider profile;
- model settings;
- configuration hash;
- accepted or rejected proposal;
- reproducible decision identity.

This objective remains tied to P-24 and the missing related travel-policy fixture.

## P-30 — Backward compatibility

**Status: Pass**

Stored v1 canonical documents remain readable.

New v2 output is not silently downgraded, and older records are not made unreadable by the richer provenance contract.

---

# Proposed enterprise-readiness extensions

## P-31 — Mixed native and scanned OCR provenance

**Status: Proposed and pending**

Enterprise PDFs often mix:

- native text pages;
- scanned image pages;
- partial OCR layers;
- rotated pages;
- damaged or duplicate text layers.

The required enterprise behavior should include:

- deterministic page-level native/OCR routing;
- no loss of physical page identity;
- OCR engine, version, language, method, and confidence;
- bounding boxes for OCR text;
- preservation of source image/page anchors;
- duplicate native/OCR text suppression;
- explicit low-confidence or unreadable regions;
- mixed-document acceptance fixture and regression tests.

A mixed native/scanned fixture is still missing.

## P-32 — Enterprise security, scale, and operations

**Status: Proposed; foundation exists but acceptance is incomplete**

The current architecture already uses Cognityx Storage, Jobs, ExecutionContext, SourceAsset identity, immutable artifacts, lifecycle boundaries, and safe deletion rules.

Full enterprise readiness should add formal acceptance for:

- authorization and delegated worker capabilities;
- tenant/project/workspace isolation;
- data-classification enforcement;
- encryption and secret handling;
- quota and reservation enforcement;
- concurrency and back-pressure;
- cancellation and restart recovery;
- idempotent retries;
- large-folder and large-document ingestion;
- observability, metrics, traces, and audit events;
- storage retention and legal-hold policies;
- parser sandboxing and resource limits;
- operational SLOs and failure budgets.

These capabilities should be tested at the complete `cogni` workflow level, not only as isolated parser tests.

## P-33 — Enterprise validation and release certification

**Status: Proposed and pending**

The current proof is strong but centered on one deliberately difficult synthetic policy PDF.

A fully enterprise-ready claim requires a broader certification corpus covering:

- HR policies and SOPs;
- EHS and manufacturing procedures;
- finance and audit policies;
- IT and security standards;
- multi-column reports;
- forms and annexures;
- documents with poor fonts or malformed PDF structure;
- multiple languages;
- real document versions and cross-document links;
- large tables and diagram-heavy pages.

The certification should measure:

- page and block accuracy;
- section-boundary accuracy;
- table reconstruction accuracy;
- reference precision and recall;
- false-relation rate;
- unresolved-detection quality;
- OCR confidence calibration;
- parser-fusion stability;
- throughput, latency, memory, and cost;
- repeatability across software upgrades;
- human review and release sign-off.

Only after these gates pass should Cognityx claim universal enterprise provenance readiness.

---

# DataForge handoff objectives

The provenance work also defines six downstream objectives.

| ID | Status | DataForge capability |
| --- | --- | --- |
| `D-01` | Pass | Load `provenance.json` directly by Storage URI. |
| `D-02` | Pass | Retain document, SourceAsset, SHA-256, section, page, block, and evidence IDs on every knowledge unit. |
| `D-03` | Pass at handoff level | Expand a candidate with cited appendix, table, section, figure, or footnote evidence without reparsing the PDF. |
| `D-04` | Handoff foundation pass; generation pending | Support reusable representation identity using content, anchors, method, model, and configuration hash. Actual summaries/questions remain DataForge work. |
| `D-05` | Pass | Keep Ingest independent of vector databases and embeddings. |
| `D-06` | Pass | Mark only concrete observed/resolved relations as gold; rejected, ambiguous, and unresolved relations remain non-gold. |

---

# What Cognityx can claim now

A defensible current statement is:

> Cognityx Ingest provides a strong, parser-independent, provenance-rich ingestion contract for native structured policy PDFs. It preserves exact source identity, page systems, reading order, section hierarchy, cross-page spans, tables, figures, footnotes, references, unresolved states, parser decisions, and DataForge-ready lineage in an immutable provenance artifact.

The system is ready to begin the research workflow:

```text
provenance.json
    ↓
DataForge candidate spans
    ↓
knowledge-unit discovery
    ↓
provenance-aware questions and answers
    ↓
LoRA training
    ↓
model-generated provenance evaluation
    ↓
exact retrieval and verification
```

# What Cognityx should not claim yet

Cognityx should not yet claim that it has solved provenance for all enterprise documents.

The following remain outside the validated boundary:

- related-document and version resolution;
- complete bounded ambiguity resolution;
- mixed native/scanned OCR;
- multilingual and highly irregular layouts;
- corpus-scale operational certification;
- DataForge dataset generation;
- training effectiveness and model-generated provenance accuracy.

# Recommended next work

The immediate next phase should move out of Ingest and into DataForge.

The first DataForge production increment should:

1. load one `provenance.json`;
2. select one exact section span;
3. construct one self-contained knowledge unit;
4. retain all lineage and anchor IDs;
5. classify every supporting relation as gold, non-gold, ambiguous, or unresolved;
6. emit no question yet.

After this foundation passes, DataForge can add:

- multi-aspect knowledge-unit discovery;
- question and reference-answer generation;
- exact provenance output targets;
- reusable representation identity;
- training-dataset manifests;
- evaluation datasets;
- LoRA experiment integration.

# Enterprise-readiness roadmap

| Stage | Main deliverable | Objectives |
| --- | --- | --- |
| 1. Current baseline | Strong native structured-PDF provenance | P-00–P-20, P-23, P-25–P-28, P-30 |
| 2. Related documents | Cross-document and version graph | P-21, P-22 |
| 3. Bounded ambiguity | Validated inference-assisted decisions | P-24, P-29 |
| 4. Mixed documents | Native/OCR page routing and confidence | P-31 |
| 5. Enterprise operations | Security, scale, resilience, observability | P-32 |
| 6. Certification | Real-corpus quality and release gates | P-33 |
| 7. Research execution | Knowledge units, Q/A, LoRA, provenance evaluation | DataForge and Training |

