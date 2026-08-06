# Parser Capability Registry

## The Problem

Cognityx can register several programs that read a document. These programs are
called parser plugins. A plugin can be registered even when its optional package
is missing, and official documentation can describe a feature that is not usable
on the current machine.

Those facts must not be compressed into one “supports tables” checkbox. Cognityx
therefore keeps a parser capability registry: a versioned snapshot of what was
observed, documented, approved, and measured.

An LLM's memory is not authoritative. Model knowledge can be old, can omit the
installed package version, and cannot prove which plugin is registered in this
process. An LLM may later propose a routing plan, but it must read the live
registry and remain inside its evidence and governance boundaries.

## Where It Fits

```text
registered ParserRouter plugins
              |
              +--> bounded package/runtime inspection
              |
frozen official evidence + human guidance + measured outcomes
              |
              v
      ParserCapabilityRegistry       T03: evidence only
              |
              v
      future adaptive routing        T04: decisions
              |
              v
       existing parser execution
```

T03 gathers and preserves evidence. It does not select or invoke a parser. T04
will consume this registry when adaptive routing is implemented.

## Exactly Three Sources

Every parser record always contains these three capability-source classes in
this exact order:

1. `parser-discovered`
2. `human-guided`
3. `auto-learned`

An empty source still exists. For example, a newly registered custom parser may
have no human recommendation and no benchmark measurement, but it still exposes
all three classes.

### Parser-Discovered

This source contains factual observations about the parser itself:

- whether its plugin is registered;
- whether its optional dependency is importable;
- its installed package version when available;
- its adapter module and class;
- frozen evidence from official documentation, repositories, and release notes;
- capability assertions such as `declared`, `available`, or `unsupported`.

Internet research is a way to refresh official parser evidence. It is not a
fourth source class. Ordinary CI reads frozen evidence and makes no internet
request.

### Human-Guided

This source contains reviewed preferences and restrictions. A condition such as
“exact PDF annotations are required” can recommend PyMuPDF as a complementary
parser. That recommendation remains human guidance; it is not rewritten as an
official parser fact.

### Auto-Learned

This source contains measurements from a named benchmark, production run,
correction, or downstream feedback profile. Each measurement retains its
document class, metric, numeric value, and sample count.

Values are not assumed to be percentages. A latency measurement can be `125.4`
milliseconds, while recall can be `0.94`. The registry requires a finite number
but leaves metric-specific interpretation to a later consumer. Missing
measurements remain empty rather than being fabricated.

## Runtime Versus Documentation

Runtime availability and documented capability answer different questions.

For example:

```text
official documentation: document hierarchy is declared
router observation:      Docling plugin is registered
package observation:     docling is not importable
```

All three facts remain visible. A `declared-but-currently-unavailable` conflict
can summarize the disagreement, but it does not remove the documented assertion
or runtime observation.

`ParserRuntimeProbe.runtime_available` is only a derived runtime fact. It is
`True` when plugin registration and dependency importability are both observed,
`False` when either is known missing, and `None` when the evidence is incomplete.
It is not a routing recommendation.

## Live Runtime Probe

`ParserCapabilityRegistry.from_router(router)` asks `ParserRouter` for an
immutable tuple of registered plugins in parser-ID order. It never reads the
router's mutable private dictionary.

For the built-in adapters, discovery uses static metadata:

| Parser ID | Import module | Distribution |
| --- | --- | --- |
| `basic` | `pypdf` | `pypdf` |
| `pymupdf` | `fitz` | `PyMuPDF` |
| `docling` | `docling` | `docling` |

The probe uses Python's module-spec and package-metadata readers. It does not
import Docling, initialize a model, open a PDF, or call `extract_document()`.
A custom parser with no declared package metadata remains registered while its
dependency and version facts remain unknown.

## Frozen Official Evidence

`OfficialDocumentationEvidence` records an evidence ID, public HTTP(S) URL,
retrieval date, and short summary. Loading this record does not fetch its URL.
The checked-in v3.2 fixture contains frozen Docling and PyMuPDF evidence and is
validated without rewriting the fixture.

Local-file URLs, local network addresses, malformed URLs, impossible dates, and
duplicate evidence IDs fail strict validation.

## Catalog Overlay

An optional catalog can be combined with current runtime observations:

```python
catalog = ParserCapabilityRegistry.from_json_bytes(frozen_bytes)
live = ParserCapabilityRegistry.from_router(router, catalog=catalog)
```

The overlay follows a narrow algorithm:

1. Validate the supplied immutable catalog.
2. Snapshot registered plugins without executing them.
3. Form the union of router and catalog parser IDs.
4. Replace only each catalog record's runtime probe.
5. Retain official documents, capability assertions, human guidance, learned
   measurements, and explicit conflicts.
6. Add bounded advertised-but-unavailable conflicts where needed.
7. Keep catalog-only parsers as unregistered live observations.
8. Add router-only parsers with empty human and learned evidence.
9. Return a new registry without modifying the catalog.

This process never converts model memory or an undocumented assumption into a
capability fact.

## Deterministic Serialization

The registry schema is:

```text
cognityx.ingest.parser-capability-registry/v3.2
```

Records, official evidence, assertions, guidance, measurements, and conflicts
use stable ordering. JSON keys are sorted, compact separators are used, Unicode
is preserved, and one newline terminates the artifact. Equivalent registries
therefore produce identical UTF-8 bytes.

Strict readers reject unsupported field shapes, duplicate identities, invalid
statuses, malformed runtime versions, non-finite values, negative sample counts,
and invalid ordering. Public APIs raise parser-capability errors rather than raw
JSON, mapping, import, or package-metadata exceptions.

The initial assertion statuses are `available`, `declared`,
`declared-when-available`, `unsupported`, `not-declared`, `unavailable`, and
`unknown`. Adding another status requires a reviewed schema-policy update; an
unrecognized string is not silently accepted as evidence.

## Consumers

Current consumers are tests, audits, and Python applications that need factual
parser inventory. T04 will be the first routing consumer. It must keep
availability, governance, cost, security, and evidence constraints visible when
it chooses among parser plans.

The three allowed future routing-mode names are retained as metadata:

- `deterministic`
- `hybrid`
- `llm-directed`

Their presence does not implement those modes. Existing `ExtractionPolicy`
names `fixed`, `rule`, `fallback`, `compare`, and `agent` remain unchanged.

## Normal CI Boundary

Normal registry construction:

- makes no live internet request;
- requires no Docling or PyMuPDF installation;
- downloads no model;
- parses no document;
- stores no credentials, secrets, or local filesystem paths.

The official evidence fixture is read locally. A future explicit research or
refresh workflow may update evidence through review, but that workflow is not
part of T03.

## T03 Non-Goals

T03 does not implement parser scoring, selection, execution changes, adaptive
routing, alignment, fusion, segmentation, retention, Source Graph, provenance
address resolution, DataForge processing, SDK commands, or CLI commands. Those
remain bounded later tasks, beginning with the T04 routing handoff.
