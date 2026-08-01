# Cognityx Ingest

Cognityx Ingest turns PDFs into structured documents and page-level text. It
also keeps a trace from every page back to the original registered file. That
trace is technically called provenance.

## Where It Fits

```text
file, folder, or existing SourceAsset
                 ↓
          Cognityx Ingest
                 ↓
 document + page evidence + stable IDs
                 ↓
             DataForge
```

A SourceAsset is the recorded original file, such as `report.pdf`. A DocBundle
is a named collection of SourceAssets, similar to a folder. Storage keeps the
bytes; Ingest keeps the logical records and generated text; Jobs keeps status
and progress.

## Preferred Commands

The simple path needs no physical storage option. `StorageRuntime.load()`
selects the configured storage roles automatically.

```bash
# Organize original files.
cogni doc-bundles create research/reports
cogni assets add report.pdf --bundle research/reports

# Ingest a path, an existing asset, or a complete bundle.
cogni ingest report.pdf
cogni ingest --asset src-...
cogni ingest --bundle-id bun-...

# Check work and reconnect to ordered progress.
cogni jobs status <job-id>
cogni jobs events <job-id>
cogni jobs watch <job-id>
cogni jobs cancel <job-id>

# Inspect or remove generated results.
cogni runs list
cogni runs show <run-id>
cogni documents list
cogni documents show <document-id>
cogni artifacts read <document-id> evidence
```

The ingest command returns stable run, job, bundle, asset, and document IDs.
A normal DataForge user does not need to know the internal storage filenames.

## Deletion And Cleanup

Deletion is split deliberately so one action cannot unexpectedly remove raw
source bytes:

- `cogni assets delete` and `cogni doc-bundles delete` mark logical records as
  deleted. The stored bytes remain available while anything still references
  them.
- `cogni runs delete` removes only generated metadata for that run. It does not
  remove generated documents or SourceAssets.
- `cogni documents delete` removes only the selected generated document and its
  generated evidence. It does not remove the SourceAsset or job history.
- `cogni cleanup blobs` asks Storage to find raw blobs with no live reference.
  Planning is the default; physical deletion requires `--yes`, and Storage
  checks references again immediately before deletion.

This separation means deleting an extracted document does not silently delete
the original PDF.

## Advanced Configuration

Normal commands load the standard Storage Runtime. An operator may select an
explicit configuration when testing profile routing:

```bash
cogni ingest report.pdf --storage-config .cognityx/storage.toml
```

## Future Roadmap

The following work is intentionally deferred:

- Storage will gain an always-running cleanup service that periodically plans
  and removes unreferenced blobs according to retention policy. It must use the
  same reference checks and safe cleanup boundary as the current explicit
  command; it will not make Ingest a blob owner.
- Ingest may accept external object or web references without first copying the
  bytes. This reference-only mode must define checksum, availability, and
  permission guarantees before implementation.
- Large folder runs may move to real distributed workers. The current engine is
  synchronous and checks cancellation between documents; no worker framework is
  introduced here.

## Deprecated / Compatibility

The `cognityx-ingest` command, `sources` and `bundles` aliases, the short form
without the `ingest` subcommand, `--bundle` for bundle ingestion, and
`--storage-root` remain temporarily available. They emit compatibility
warnings. New application documentation and scripts should use `cogni`,
`assets`, `doc-bundles`, `--bundle-id`, and normal Storage Runtime loading.

The old `source.pdf` generated-artifact command is no longer valid. Raw source
bytes live in the SourceAsset Blob and are inspected through `cogni assets`.
