# Source Storage

Source storage registers an external file once and returns a durable Cognityx
resource identity. After registration, downstream work uses `source_id`; it
does not need the original filesystem path.

```text
ExecutionContext -> Context -> Bundle -> Source -> immutable Blob
```

This capability deliberately does not parse files, create documents, run jobs,
or build RAG indexes. Those later capabilities will begin from `source_id`.

## CLI

The normal starting point needs only a file. The default local storage root is
used unless `--storage-root` is supplied.

```bash
cognityx-ingest sources add report.pdf
cognityx-ingest sources add report.pdf --bundle phd/rag
cognityx-ingest bundles list
cognityx-ingest bundles create enterprise/policies/hr
cognityx-ingest sources list --bundle phd/rag
cognityx-ingest sources show src-... 
cognityx-ingest sources locate src-...
cognityx-ingest bundles locate bun-...
```

The first add lazily creates the current Context's `default` Bundle. Repeating
an add with identical bytes in the same Bundle returns the original
`source_id` and `status: already_registered`. Identical bytes in a different
Bundle receive a new logical `source_id` while reusing the immutable blob.

Use `--storage-root /path/to/storage` when selecting a local storage root.
The source catalog is persisted at
`<storage-root>/.cognityx-ingest/source_catalog.sqlite3`; it is metadata owned
by Ingest, not a storage-folder index.

## Context resolution

Context is optional. The simple local command continues to work:

```bash
cognityx-ingest sources add report.pdf
```

The effective Context is selected in this order: explicit CLI fields, then one
base file selected from `--context`, `COGNITYX_CONTEXT_FILE`, project
`.cognityx/context.json`, the user context file
`$XDG_CONFIG_HOME/cognityx/context.json` (or
`COGNITYX_USER_CONTEXT_FILE`), then a minimal local Context.

The selected JSON file contains only stable governance descriptors:

```json
{
  "context_type": "user",
  "principal_id": "alice",
  "tenant_id": "acme",
  "project_id": "genai",
  "workspace_id": "research",
  "scopes": {"repo": "cognityx-ingest", "function": "source-registration"}
}
```

`run_id`, correlation IDs, credentials and other execution state are rejected.
They are generated for each command. Override only the fields you need:

```bash
cognityx-ingest sources add report.pdf --context context.json \
  --workspace-id testing --scope function=experiment --scope environment=dev
```

`--context-type system` supports service work without a human principal.

## Deduplication and locations

`COGNITYX_DEDUP_SCOPE` is deployment configuration, not an upload argument.
It accepts `tenant` (default), `context`, or `platform`.

```bash
export COGNITYX_DEDUP_SCOPE=tenant
```

Under the default, identical bytes share a Blob only inside the same tenant.
Contexts with no tenant are isolated by principal; system Contexts are isolated
from user fallback domains. Blob bytes use the logical namespace:

```text
blob-domains/<dedup-domain>/sha256/<first-two>/<next-two>/<sha256>
```

The returned/persisted durable location is provider-neutral, for example
`storage://shared/blob-domains/...`, never a `file://` path. Use read-only
inspection when an operator needs the local backing path:

```bash
cognityx-ingest sources locate src-...
cognityx-ingest bundles locate bun-...
```

`sources locate` returns `source_id`, `blob_id`, `blob_uri`, backend identity
and `local_path` when the active backend is already local. It never downloads,
copies or materializes data. `bundles locate` identifies the logical metadata
namespace, not the source-byte location.

## Python API

```python
from cognityx_ingest import ExecutionContext, SourceRegistry
from cognityx_storage import LocalStorageBackend, StorageClient

root = "/tmp/cognityx-storage"
storage = StorageClient(LocalStorageBackend(root)).for_shared_data()
sources = SourceRegistry(storage, f"{root}/.cognityx-ingest/source_catalog.sqlite3")
context = ExecutionContext(
    run_id="request-1",
    correlation_id="correlation-1",
    principal_id="alice",
    tenant_id="tenant-a",
    project_id="research",
)

result = sources.register_file(context, "report.pdf", bundle="phd/rag")
print(result.source_id, result.status)

with sources.open(context, result.source_id) as blob:
    assert blob.read()
```

For physical inspection only:

```python
location = sources.locate_source(context, result.source_id)
print(location.blob_uri, location.local_path)
```

`ExecutionContext.run_id` and `correlation_id` are intentionally excluded from
Context identity. Equivalent scope descriptors resolve to the same
`context_id`; a changed relevant descriptor resolves a different Context.
System work can use `context_type="system"` and service descriptors in
`scopes`.

## Storage and authorization boundary

Blobs are written through `cognityx-storage` using opaque logical keys under
the shared trust-domain blob namespace. The catalog records the blob relationship but never
uses blob identity for authorization. Source reads are scoped by both current
Context and `source_id`.

The source service calls the existing control seam with:

```text
ingest.bundle.create
ingest.bundle.read
ingest.source.create
ingest.source.read
ingest.source.list
```

`LocalControlClient` allows standalone use. No ACL, user, group, role, or
cloud-provider policy model is introduced here.

## Migration

On first opening an existing Source Storage v1 catalog, Ingest detects the
legacy globally unique SHA-256 Blob table. It creates domain-specific Blob
records, copies each referenced legacy Blob into the configured dedup domain,
updates Sources to the new Blob IDs, and retains source readability. Legacy
objects may remain as unreferenced storage data; this phase intentionally does
not implement Blob garbage collection.
