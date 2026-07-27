# Cognityx Ingest

`cognityx-ingest` durably registers any digital input as a SourceAsset. Its
current extraction pipeline turns PDFs into canonical Cognityx document
artifacts with source, page, and text-span provenance. Image, audio, video and
other modality-specific processing will be added separately.

## Contract

Each content-addressed document persists the original PDF plus:

- `document.json`: canonical document, source descriptor, and sections.
- `evidence.jsonl`: one page-level evidence record per extracted page.
- `manifest.json`: stable pointers to all artifacts.

The default parser is deterministic. Optional semantic enhancement is explicitly
non-authoritative and must use `cognityx-inference`; evidence always retains its
direct page provenance.

Install the `inference` extra only when constructing a `SectionEnhancer` with
`CognityxInferenceClient`.

## Quick start

```bash
cognityx-ingest ingest report.pdf --storage-root /tmp/cognityx-storage
```

```python
from cognityx_ingest import IngestService
from cognityx_storage import LocalStorageBackend, StorageClient

storage = StorageClient(LocalStorageBackend("/tmp/cognityx-storage")).for_shared_data()
result = IngestService(storage).ingest("report.pdf")
print(result.document.document_id)
```

The legacy short CLI form, `cognityx-ingest report.pdf --storage-root ...`, remains supported.

Registering SourceAssets independently uses the Storage Runtime `source_asset`
role and persists a durable Storage-owned `BlobRef`:

```bash
cognityx-ingest assets add report.pdf
cognityx-ingest assets add interview.mp3 --bundle research/interviews \
  --storage-config .cognityx/storage.toml
cognityx-ingest doc-bundles list
```

Ingest owns SourceAsset and DocBundle records; `cognityx-storage` owns
hashing, CAS layout, deduplication, physical Blob reuse and profile routing.
The historical `sources` and `bundles` commands remain compatibility aliases.
See [Source Assets](docs/sources.md) for the CLI, Python API and automatic
legacy catalog migration.

Manage local execution with `jobs list`, `jobs show`, `jobs cancel`,
`documents list`, `documents show`, `artifacts read`, and `documents delete
--yes`. Full commands and Python API examples are in the documentation.

See the [documentation](docs/index.md) for the complete CLI and Python API examples.
