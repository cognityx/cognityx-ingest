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

`document.json` uses `cognityx.ingest.document/v1`. Future DataForge work can
consume these artifacts, but DataForge is not implemented or required here.

An optional enhancer may attach inferred metadata under `enhancement`; it must
name `cognityx-inference` and never replaces page evidence.
