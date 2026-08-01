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
cogni bundle create research/reports
cogni asset add report.pdf --bundle research/reports

# Ingest a path, an existing asset, or a complete bundle.
cogni ingest report.pdf
cogni ingest --asset src-...
cogni ingest --bundle research/reports

# Check work and reconnect to ordered progress.
cogni job status <job-id>
cogni job events <job-id>
cogni job watch <job-id>
cogni job cancel <job-id>

# Inspect or remove generated results.
cogni runs list
cogni runs show <run-id>
cogni document list
cogni document show <document-id>
cogni artifact read <document-id> provenance
```

The ingest command returns stable run, job, bundle, asset, and document IDs.
A normal DataForge user does not need to know the internal storage filenames.

## Structure And Ambiguity

The baseline parser keeps page text. Docling can provide richer document
structure, while PyMuPDF can provide PDF-native page labels, outlines, links,
annotations and object locations. A parser plugin is an interchangeable reader
that produces the same Cognityx output regardless of its internal library.

Extraction can use one fixed parser, deterministic routing, ordered fallback,
multi-parser comparison, or bounded model-assisted selection. Every output
records which parsers were considered, which one ran, and why it was selected.

Native PDF facts remain separate from deterministic rules and model proposals.
When configured, Cognityx Inference may propose a relationship for an unresolved
reference. Ingest accepts it only when its source and target are existing stable
anchors and its relationship type is allowed. Rejected proposals remain listed
for later review.

Local inference can start through a named Cognityx Inference server profile.
That profile selects and loads the approved model before the worker becomes
ready. Advanced operators configure this in an inference TOML file; ordinary
ingest commands do not require model terminology.

```toml
[inference]
base_url = "http://127.0.0.1:8000"
manager_url = "http://127.0.0.1:8000"
auto_start_local = true
discovery_policy = "require_existing"
max_calls = 8
max_output_tokens = 400

[[inference.targets]]
provider = "local"
model = "Qwen/Qwen3-8B"
backend = "vllm"
profile = "int4"
server_profile = "qwen3-8b-int4"
```

The SDK accepts this as the advanced `--inference-config` override. It can also
be selected with `COGNITYX_INGEST_INFERENCE_CONFIG`. Local hardware discovery
does not start silently under the recommended `require_existing` policy.

## Deletion And Cleanup

Deletion is split deliberately so one action cannot unexpectedly remove raw
source bytes:

- `cogni asset delete` and `cogni bundle delete` mark logical records as
  deleted. The stored bytes remain available while anything still references
  them.
- `cogni runs delete` removes only generated metadata for that run. It does not
  remove generated documents or SourceAssets.
- `cogni document delete` removes only the selected generated document and its
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

The `cognityx-ingest` command; plural `assets`, `doc-bundles`, `jobs`,
`documents`, and `artifacts`; historical `sources` and `bundles`; the short
form without the `ingest` subcommand; ID-only `--bundle-id`; and
`--storage-root` remain compatibility forms. New application documentation and
scripts use singular resource commands, bundle paths, and normal Storage
Runtime loading.

The old `source.pdf` generated-artifact command is no longer valid. Raw source
bytes live in the SourceAsset Blob and are inspected through `cogni asset`.
