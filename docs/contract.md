# Output contract

Artifact keys are relative to the supplied `StorageClient` scope and live under
`ingest/documents/{document_id}/`, where `document_id` is derived from the PDF's
SHA-256 digest.

| Artifact | Purpose |
| --- | --- |
| `source.pdf` | Original immutable source bytes |
| `document.json` | Schema version, source metadata, title, and sections |
| `evidence.jsonl` | Page text plus page number and character offsets |
| `manifest.json` | Stable artifact pointers for downstream consumers |

`document.json` includes `schema: "cognityx.ingest.document"` and uses
`cognityx.ingest.document/v1`. Future DataForge work can
consume these artifacts, but DataForge is not implemented or required here.

An optional enhancer may attach inferred metadata under `enhancement`; it must
name `cognityx-inference` and never replaces page evidence.

## Execution boundary

Every ingestion runs with an `ExecutionContext` containing opaque run and
correlation IDs and optional principal/governance scope fields. `IngestService`
authorizes `ingest.job.submit` through a `ControlClient` before parsing, then
reports a measured `UsageReport` after artifacts persist. `LocalControlClient`
is the default standalone implementation.

Ingest owns `SUBMITTED`, `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, and
`CANCELLED` lifecycle semantics. When a `cognityx-jobs` repository is supplied,
the service records the corresponding submission, queue, start, and completion
or failure events there.
