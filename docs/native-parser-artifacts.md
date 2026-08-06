# Native Parser Artifacts

## The Problem

A document parser can observe more than Cognityx needs in its stable document
record. For example, Docling may return its own text objects, table structures,
and pointers. Reducing that output to pages and paragraphs would throw useful
evidence away.

Cognityx Ingest now keeps the parser's original bytes exactly as received. This
original output is called the **parser-native payload**. A separate small JSON
record describes where those bytes live and how to verify them. That record is
called the **native descriptor**.

## Where It Fits

```text
source PDF
    |
    v
parser -> parser-native payload ---------> audit or parser-specific reader
    |                 |
    |                 +-> native descriptor -> future NativeBinding / retention
    v
canonical document + provenance --------> DataForge and normal consumers
```

The **canonical document** is the stable parser-neutral view used by normal
Cognityx consumers. A future T02 record will connect canonical objects to native
pointers; that connection is called a **NativeBinding**. T01 preserves the bytes
and pointers needed for that later connection but does not create the generalized
binding model.

DataForge normally reads the canonical document and provenance handoff. It does
not need the raw parser payload for ordinary paragraph, question-and-answer, or
Knowledge Unit work.

## Storage And Reload Flow

`IngestService` keeps the existing payload location:

```text
ingest/documents/<document-id>/parser/<backend>.json
```

It uses the existing parser `ArtifactRef` ID as the native artifact ID. The
descriptor is stored separately:

```text
ingest/native-artifacts/<artifact-id>.json
```

The sequence is:

1. Compute SHA-256 and byte count from the original bytes.
2. Publish the payload only when its key is absent.
3. If the payload already exists, read and hash it. Accept only an exact identity
   match.
4. Publish the descriptor only when its artifact ID is absent.
5. If an equivalent descriptor already exists, return it as an idempotent retry.
6. On reload, read the descriptor, load the payload, and recompute size and
   SHA-256 before returning any bytes.
7. For JSON media types, resolve stored local JSON pointers such as
   `#/texts/0`. Pointer checking never rewrites the payload.

An immutable object is one that cannot be replaced after publication. Changed
bytes or changed metadata under an existing identity raise a conflict instead of
silently overwriting evidence.

## Consumers And Ownership

`IngestService` writes payloads and descriptors. Audit tools and future SDK read
APIs can reload them. T02 will use descriptors when it introduces generalized
NativeBinding records. T07 will decide reuse, expiry, legal hold, and purge
behavior.

Cognityx Storage owns physical writes and reads. Ingest owns artifact identity,
descriptor metadata, and verification. Parser libraries own the meaning of
their native payloads. `NativeArtifactStore` does not import Docling, PyMuPDF, or
another parser-private class.

## Backward Compatibility

Existing callers continue to receive the same:

- `raw_parser_key` and `raw_parser_keys` values;
- `parser/<backend>.json` payload locations;
- parser `ArtifactRef` IDs and URIs;
- manifest artifact records;
- provenance `backend` and `uri` fields;
- v1 and v2 canonical readers.

Provenance now adds the parser version, SHA-256, byte count, media type,
descriptor URI, retention class, and native pointers. Parsers that return no raw
artifact still work and do not receive a fabricated payload.

## Python Example

```python
from cognityx_ingest import NativeArtifactStore
from cognityx_resource import ExecutionContext

context = ExecutionContext(
    run_id="run-123",
    correlation_id="request-456",
    principal_id="operator",
)
store = NativeArtifactStore(storage, context)

descriptor = store.store(
    artifact_id="art-pdf-123-parser_raw",
    parser_id="docling",
    parser_version="2.50.0",
    payload=docling_json_bytes,
    media_type="application/vnd.docling.document+json",
    native_pointers=("#/texts/0", "#/texts/1"),
    payload_key="ingest/documents/pdf-123/parser/docling.json",
)

# This reads only the small descriptor.
metadata = store.read(descriptor.artifact_id)

# This returns bytes only after size, hash, and JSON-pointer checks pass.
native = store.reload(descriptor.artifact_id)
assert native.payload == docling_json_bytes
```

The example assumes `storage` is the same scoped `StorageClient` used by the
application composition root.

## Failure Modes

- `NativeArtifactNotFoundError`: descriptor or payload is absent.
- `NativeArtifactConflictError`: an existing identity has different bytes or
  metadata.
- `NativeArtifactIntegrityError`: stored size, SHA-256, URI, or descriptor data
  is invalid.
- `NativePointerError`: a JSON payload or one of its local pointers is invalid.
- `NativeArtifactError`: another bounded storage or validation failure occurred.

Errors identify logical artifact IDs. They do not include parser payload
contents, credentials, or local operating-system paths.

## T01 Limits

T01 does not generalize canonical records, infer semantic ownership from native
pointers, reuse extraction across documents, expire artifacts, enforce legal
holds, purge data, change parser routing or fusion, or add SDK/CLI commands.
Generalized NativeBinding belongs to T02. Reuse, retention, and purge belong to
T07.
