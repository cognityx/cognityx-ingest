# Source Assets and Doc Bundles

SourceAsset registration captures an externally supplied digital object and
returns a durable Cognityx resource identity. After registration, downstream
work uses `asset_id`; it does not need the original filesystem path.

```text
ResourceContext
      -> DocBundle
      -> SourceAsset
      -> BlobRef
```

`SourceAssetContext` is the catalog's durable read model of the shared
`ResourceContext`; callers do not construct a second Context.

## Deletion and restoration

SourceAsset and DocBundle deletion is an auditable logical soft deletion. It
records lifecycle fields in SQLite and publishes an append-only lifecycle event;
it never deletes the physical Blob. Shared Blobs remain available until Storage
garbage collection proves that no live SourceAsset references them.

```text
Asset A ─┐
Asset B ─┼──→ shared Blob
Asset C ─┘
```

Deleted resources are excluded from normal list/show/open/locate operations and
can be inspected with `list_deleted_assets()` and
`list_deleted_doc_bundles()`. Re-registering identical bytes in the same
bundle restores the original `src-...` identity with status `restored`.

Bundle deletion requires `recursive=True` when live assets or child bundles
exist. Recursive deletion tombstones descendants before the target bundle.

## Explicit Blob cleanup

Use `SourceAssetCleanupService` to enumerate live BlobRefs through Ingest and
delegate physical cleanup to Storage:

```python
from datetime import timedelta
from cognityx_ingest import SourceAssetCleanupService

cleanup = SourceAssetCleanupService(registry=registry, storage_runtime=runtime)
plan = cleanup.plan_blobs(execution, older_than=timedelta(days=7))
result = cleanup.execute_blobs(execution, plan)
```

Planning is dry-run only. Storage revalidates references, CAS identity, object
age, and metadata before deletion. The default grace period is seven days, and
planned bytes are not reclaimed bytes until deletion succeeds.

Registration accepts any digital file without parsing it. Later capabilities
may begin from `asset_id`, but not every SourceAsset becomes a parsed document:

```text
SourceAsset        raw registered input
ParsedDocument     later structured result for document-like input
Audio transcript   possible later derivative of an audio SourceAsset
Image representation possible later derivative of an image SourceAsset
DatasetRevision    DataForge-owned processed artifact
```

## CLI

The normal starting point needs only a file. `StorageRuntime.load()` selects
the Storage profile and role using the standard Cognityx configuration
precedence.

```bash
cognityx-ingest assets add report.pdf
cognityx-ingest assets add recording.mp3 --bundle research/interviews
cognityx-ingest assets add /data/contracts --bundle legal
cognityx-ingest assets add /data/contracts --bundle legal --structure flat
cognityx-ingest doc-bundles list
cognityx-ingest doc-bundles create enterprise/policies/hr
cognityx-ingest assets list --bundle research/interviews
cognityx-ingest assets show src-...
cognityx-ingest assets locate src-...
cognityx-ingest doc-bundles locate bun-...
```

The first add lazily creates the current Context's `default` DocBundle.
Repeating an add with identical bytes in the same DocBundle returns the
original `asset_id` and `status: already_registered`. Identical bytes in a
different DocBundle receive a new logical `src-...` asset identity while
physical reuse follows the configured Storage dedup policy.

The historical commands remain compatible and write warnings only to stderr:

```bash
cognityx-ingest sources add report.pdf
cognityx-ingest bundles list
```

Compatibility JSON retains `source_id`; canonical `assets` JSON uses
`asset_id`. Both values are the same stable `src-...` identity.

### Complete folders

`assets add` accepts either one regular file or a directory:

```bash
cognityx-ingest assets add /data/contracts \
  --bundle legal \
  --structure preserve

cognityx-ingest assets add /data/contracts \
  --bundle legal \
  --structure flat

cognityx-ingest assets add /data/contracts \
  --bundle legal \
  --no-recursive
```

`preserve` reproduces relative folders as nested DocBundles. With the example
root `/data/contracts`, `india/agreement.pdf` is placed in `legal/india`; the
source root name is not repeated beneath an explicit bundle. When `--bundle`
is omitted, the source directory name becomes the root bundle. `flat` places
all discovered files directly in that root bundle. Empty directories do not
create child bundles.

Folder traversal is deterministic and does not follow symlinks. It skips file
symlinks, directory symlinks, special filesystem entries, and Cognityx
Storage/catalog files encountered beneath the selected tree. `--no-recursive`
limits discovery to direct child files.

Folder registration is synchronous in Job 5A. Files are independently
transactional, so a failed file is reported without rolling back successful
files. Rerunning the same folder safely reuses existing Assets and restores
logically deleted Assets. Durable background execution for large folders will
be added separately.

Use `--storage-root /path/to/storage` when selecting a local storage root.
This is a local-development shortcut that creates the built-in filesystem
Storage Runtime. For an explicit runtime configuration, use:

```bash
cognityx-ingest assets add report.pdf \
  --storage-config .cognityx/storage.toml
```

`--storage-root` and `--storage-config` are mutually exclusive. Storage
configuration otherwise follows `COGNITYX_STORAGE_CONFIG`, project
`.cognityx/storage.toml`, user configuration, and the built-in local fallback.

The SQLite catalog is resolved through the Storage `catalog` role at
`catalog/ingest/source_catalog.sqlite3`. Existing installations with
`<source-asset-root>/.cognityx-ingest/source_catalog.sqlite3` continue using
that file in place. Resolution precedence is explicit `--catalog-path`,
`COGNITYX_INGEST_CATALOG`, an existing legacy catalog, then the catalog role.
If both legacy and catalog-role databases exist, pass `--catalog-path` rather
than allowing split-brain selection. The catalog requires native path,
random-write and file-locking capabilities; it is never stored as a Blob or
object-storage item.

## Context resolution

Context is optional. The simple local command continues to work:

```bash
cognityx-ingest assets add report.pdf
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
cognityx-ingest assets add report.pdf --context context.json \
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

The complete Storage `BlobRef` is persisted on every new SourceAsset. Its
durable location is provider-neutral, for example
`storage://local-main/source-assets/blob-domains/...`, never a `file://` path.
Use read-only inspection when an operator needs the local backing path:

```bash
cognityx-ingest assets locate src-...
cognityx-ingest doc-bundles locate bun-...
```

`assets locate` returns `asset_id`, `blob_id`, `blob_uri`, durable
`profile_name`, backend diagnostics and `local_path` when the recorded profile
is already local. It never downloads, copies or materializes data. A readable
non-local backend returns `local_path: null`. `doc-bundles locate` identifies
the logical metadata namespace, not the source-byte location.

## Python API

```python
from cognityx_ingest import SourceAssetRegistry
from cognityx_resource import ResourceContext, ExecutionContext
assets = SourceAssetRegistry.load()
context = ExecutionContext.create(ResourceContext(
    principal_id="alice",
    tenant_id="tenant-a",
    project_id="research",
))

result = assets.register_asset(context, "interview.mp3", bundle="research/interviews")
print(result.asset_id, result.status)

asset = assets.show_asset(context, result.asset_id)
print(asset.asset_id, asset.ref)

with assets.open_asset(context, result.asset_id) as blob:
    assert blob.read()
```

Register a complete tree through the same registry:

```python
result = assets.register_path(
    context,
    "/data/contracts",
    bundle="legal",
    structure="preserve",
)

print(result.batch_id, result.created_count, result.failed_count)
for item in result.items:
    print(item.relative_path, item.bundle_path, item.status)
```

A file passed to `register_path()` returns the existing
`SourceAssetRegistrationResult`. A directory returns
`SourceAssetBatchResult`. One `ExecutionContext` covers the complete batch.
Optional `progress` and `cancellation_requested` callbacks prepare the
synchronous engine for future durable Jobs integration without creating
threads or workers now.

For an explicit Runtime or recovery/testing catalog path:

```python
from cognityx_storage import StorageRuntime

runtime = StorageRuntime.load(config_file=".cognityx/storage.toml")
assets = SourceAssetRegistry.load(
    runtime=runtime,
    catalog_path="/tmp/source_catalog.sqlite3",
)
print(assets.catalog_info())
```

For physical inspection only:

```python
location = assets.locate_asset(context, result.asset_id)
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
| DocBundle and SourceAsset records | hashing and SHA-256 |
| catalog and SourceAsset-to-BlobRef relationship | CAS keys and Blob IDs |
| SourceAsset/DocBundle authorization | dedup scope and domain |
| same-DocBundle logical equality | physical reuse, URI and profile routing |

Registration captures an unpublished snapshot with
`runtime.blobs("source_asset").prepare_file(...)`. Ingest uses
`bundle_id + prepared.digest` to create or reuse its logical SourceAsset. A
duplicate SourceAsset discards the prepared snapshot without publishing a
Blob; an accepted SourceAsset publishes the exact staged bytes and persists
the returned BlobRef.
Ingest does not calculate a second digest, CAS key or dedup domain.

For `dedup_scope = "none"`, the final duplicate check and winning publication
are arbitrated with the Source catalog write transaction. The potentially
large caller-file capture occurs before that lock. Concurrent identical
registrations in one DocBundle therefore produce one SourceAsset and one
referenced Blob, while identical content accepted into two different
DocBundles produces two SourceAssets and two physical Blobs.

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

Raw SourceAsset bytes appear only beneath `blob-domains`. The catalog records
the durable BlobRef relationship but never uses Blob identity for
authorization. Reads remain scoped by current Context and `asset_id`.

The SQLite schema and durable metadata keys intentionally retain the historical
terms `sources`, `source_id`, `bundles`, `source.json` and `bundle.json`.
Existing catalogs and immutable metadata therefore need no naming migration;
the public domain API is SourceAsset and DocBundle.

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
