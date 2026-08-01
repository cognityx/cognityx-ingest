# Cognityx Ingest

[![CI](https://github.com/cognityx/cognityx-ingest/actions/workflows/ci.yml/badge.svg)](https://github.com/cognityx/cognityx-ingest/actions/workflows/ci.yml)

Cognityx Ingest turns a PDF into structured pages that later programs can use.
It first records the original file, then extracts its text while keeping a
trace back to that original file. This trace is technically called provenance.

```text
local file or existing asset
        ↓
  Cognityx Ingest
        ↓
documents and page evidence
        ↓
     DataForge
```

The application-facing command is `cogni`:

```bash
cogni asset add report.pdf --bundle research/reports
cogni ingest report.pdf
cogni ingest --asset src-...
cogni ingest --bundle legal/hr
```

Ingest decides which logical asset and document a file belongs to.
`cognityx-storage` owns the original bytes, SHA-256 hashing, content-addressed
storage (CAS), duplicate-byte reuse, profile routing, and safe blob cleanup.
`cognityx-jobs` stores job status and ordered progress events.

See the [documentation](docs/index.md) for lifecycle commands, deletion rules,
the Python API, compatibility behavior, and the future roadmap.

The default parser keeps page text. Optional Docling and PyMuPDF plugins add
rich structure and PDF-native facts. When an ambiguity remains, an explicitly
configured Cognityx Inference client may propose a resolution. Ingest validates
every proposed source anchor before accepting it; the model never rewrites
observed source facts.

## Development

```bash
uv sync --extra dev
uv run pytest
uv run mkdocs build --strict
uv build
```
