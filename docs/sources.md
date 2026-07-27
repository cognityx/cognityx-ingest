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
```

The first add lazily creates the current Context's `default` Bundle. Repeating
an add with identical bytes in the same Bundle returns the original
`source_id` and `status: already_registered`. Identical bytes in a different
Bundle receive a new logical `source_id` while reusing the immutable blob.

Use `--storage-root /path/to/storage` when selecting a local storage root.
The source catalog is persisted at
`<storage-root>/.cognityx-ingest/source_catalog.sqlite3`; it is metadata owned
by Ingest, not a storage-folder index.

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

`ExecutionContext.run_id` and `correlation_id` are intentionally excluded from
Context identity. Equivalent scope descriptors resolve to the same
`context_id`; a changed relevant descriptor resolves a different Context.
System work can use `context_type="system"` and service descriptors in
`scopes`.

## Storage and authorization boundary

Blobs are written through `cognityx-storage` using opaque logical keys under
the shared blob namespace. The catalog records the blob relationship but never
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
