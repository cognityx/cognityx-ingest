# Source Storage

Source storage registers an external file once and returns a durable Cognityx
resource identity. After registration, downstream work uses `source_id`; it
does not need the original filesystem path.

```text
ResourceContext -> ExecutionContext -> Context -> Bundle -> Source -> immutable Blob
```

This capability deliberately does not parse files, create documents, run jobs,
or build RAG indexes. Those later capabilities will begin from `source_id`.

## CLI

The normal starting point needs only a file. `StorageRuntime.load()` selects
the Storage profile and role using the standard Cognityx configuration
precedence.

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
This is a local-development shortcut that creates the built-in filesystem
Storage Runtime. For an explicit runtime configuration, use:

```bash
cognityx-ingest sources add report.pdf \
  --storage-config .cognityx/storage.toml
```

`--storage-root` and `--storage-config` are mutually exclusive. Storage
configuration otherwise follows `COGNITYX_STORAGE_CONFIG`, project
`.cognityx/storage.toml`, user configuration, and the built-in local fallback.

The source catalog is persisted at
`<storage-root>/.cognityx-ingest/source_catalog.sqlite3`; it is metadata owned
by Ingest, not a storage-folder index. With a non-filesystem profile or a
configuration whose local root cannot be derived, pass
`--catalog-path /path/to/source_catalog.sqlite3`. This job deliberately does
not move the catalog into a Storage role.

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

The shared definitions come from `cognityx-resource`. `ResourceContext`
contains stable governance descriptors; `ExecutionContext` identifies one
operation in that stable Context:

```python
from cognityx_resource import ResourceContext, ExecutionContext

resource_context = ResourceContext(
    tenant_id="acme",
    project_id="genai",
    principal_id="alice",
)
execution = ExecutionContext.create(resource_context)

print(execution.context_id)  # stable
print(execution.run_id)      # unique to this operation
```

For compatibility, `from cognityx_ingest import ExecutionContext` continues to
work and refers to the same shared implementation.

## Deduplication and locations

Deduplication is Storage configuration. Configure the `source_asset` role:

```toml
[storage.roles.source_asset]
profile = "local-main"
namespace = "source-assets"
dedup_scope = "tenant"
```

Supported values are `tenant` (default), `context`, `platform`, and `none`.
Under the default, identical bytes share a Blob only inside the same tenant.
Contexts with no tenant are isolated by principal; system Contexts are isolated
from user fallback domains. Blob bytes use the logical namespace:

```text
source-assets/
  blob-domains/<dedup-domain>/sha256/<first-two>/<next-two>/<sha256>
```

`COGNITYX_DEDUP_SCOPE` is deprecated and ignored. It may produce a compatibility
warning; it does not override Storage Runtime configuration.

The complete Storage `BlobRef` is persisted on every new Source. Its durable
location is provider-neutral, for example
`storage://local-main/source-assets/blob-domains/...`, never a `file://` path.
Use read-only inspection when an operator needs the local backing path:

```bash
cognityx-ingest sources locate src-...
cognityx-ingest bundles locate bun-...
```

`sources locate` returns `source_id`, `blob_id`, `blob_uri`, durable
`profile_name`, backend diagnostics and `local_path` when the recorded profile
is already local. It never downloads, copies or materializes data. A readable
non-local backend returns `local_path: null`. `bundles locate` identifies the
logical metadata namespace, not the source-byte location.

## Python API

```python
from cognityx_ingest import SourceRegistry
from cognityx_resource import ResourceContext, ExecutionContext
from cognityx_storage import StorageRuntime

runtime = StorageRuntime.load()
sources = SourceRegistry(
    runtime,
    "/path/to/source_catalog.sqlite3",
)
context = ExecutionContext.create(ResourceContext(
    principal_id="alice",
    tenant_id="tenant-a",
    project_id="research",
))

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

For a direct local-development root:

```python
from cognityx_storage import StorageConfig, StorageRuntime

runtime = StorageRuntime.from_config(
    StorageConfig.built_in(root="/tmp/cognityx-storage")
)
```

## Storage and authorization boundary

The ownership boundary is explicit:

| Ingest owns | Storage owns |
| --- | --- |
| Context relationship | `BlobRef` |
| Bundle and Source records | hashing and SHA-256 |
| Source catalog and Source-to-BlobRef relationship | CAS keys and Blob IDs |
| Source/Bundle authorization | dedup scope and domain |
| same-Bundle logical equality | physical reuse, URI and profile routing |

Registration calls `runtime.blobs("source_asset").put_file(...)`. Ingest then
uses `bundle_id + blob_ref.digest` to create or reuse its logical Source.
Ingest does not calculate a second digest, CAS key or dedup domain.

Inspectable JSON metadata is written through
`runtime.for_role("source_asset")`:

```text
source-assets/
  source-contexts/<context_id>/
    context.json
    bundles/<bundle_id>/
      bundle.json
      sources/<source_id>/source.json
```

Raw source bytes appear only beneath `blob-domains`. The catalog records the
durable BlobRef relationship but never uses Blob identity for authorization.
Source reads remain scoped by current Context and `source_id`.

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

On first opening an existing catalog, Ingest adds nullable
`sources.blob_ref_json` and migrates every unmigrated Source through the
Storage Blob API. Both the original global-SHA Blob table and the later
domain-scoped Ingest Blob table are supported directly.

Migration reconstructs the stored `ResourceContext`, verifies its computed
Context ID, validates the legacy bytes against catalog digest and size, calls
`BlobStore.put_stream()`, and commits one Source at a time. It is therefore
restartable after interruption. A mismatch raises a clear migration error
without replacing the Source mapping.

The old `blobs` table is retained solely as legacy migration state. New
registration, lookup, deduplication, authorization, reads and locate operations
do not use it. Existing IDs remain unchanged, authoritative metadata is
republished under the `source_asset` role, and old bytes/metadata may remain as
unreferenced legacy data. Garbage collection is out of scope.
