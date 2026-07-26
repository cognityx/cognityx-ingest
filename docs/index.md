# Cognityx Ingest

Ingest PDFs into source-addressable, canonical document artifacts. The initial
scope is deliberately narrow: local files and folders of PDFs.

The deterministic pipeline registers source bytes, extracts page text,
normalizes page sections and evidence, then persists artifacts through
`cognityx-storage`. `cognityx-jobs` is optional for durable lifecycle events.

LLM assistance is optional and routed only through `cognityx-inference`.
The optional `inference` package extra installs that client integration; normal
PDF ingestion does not load a model runtime.
