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
2. Build the proposed descriptor and check whether that artifact ID already has
   one before writing any payload bytes.
3. If the existing descriptor differs, reject the request without touching its
   requested payload key. If it is equivalent, verify the existing payload and
   return the existing descriptor.
4. When the artifact ID is free, publish the payload only when its key is absent.
   If the payload already exists, read and hash it. Accept only an exact identity
   match.
5. Publish the descriptor with Storage's create-only operation. This makes the
   decision indivisible for competing writers, which is technically called an
   **atomic publication**.
6. If another writer wins with an equivalent descriptor, verify and retain the
   shared payload. If an incompatible writer wins, remove only an alternate
   payload definitely created by the losing call and not referenced by the
   winner.
7. On reload, read the descriptor, load the payload, and recompute size and
   SHA-256 before returning any bytes.
8. For JSON media types, resolve stored local JSON pointers such as
   `#/texts/0`. Pointer checking never rewrites the payload.

An immutable object is one that cannot be replaced after publication. Changed
bytes or changed metadata under an existing identity raise a conflict instead of
silently overwriting evidence.

## Safe Payload Locations

A native descriptor is allowed to point only to one of these locations:

```text
<configured-native-prefix>/...
ingest/documents/<document-id>/parser/<backend-file>
```

The first location is the current native-artifact area. The second is the exact
shape used by existing Ingest parser outputs and is retained for backward
compatibility. A descriptor cannot redirect reload to a canonical document,
provenance record, source blob, or configuration object, even if that object's
bytes happen to match the descriptor's hash.

Storage deliberately treats a logical key as an opaque address. In other words,
Storage knows how to protect and retrieve an object but not what that object
means. Ingest knows whether an address represents parser-native evidence, so the
namespace authorization check belongs in `NativeArtifactStore`.

Artifact, parser, retention, run, and correlation IDs are portable single-part
tokens. They contain 1 to 128 ASCII letters, digits, dots, underscores, or
hyphens and begin with a letter or digit. This boundary rejects path separators,
whitespace, control characters, and URI query or fragment syntax before an ID can
become part of a descriptor key or diagnostic.

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
  metadata. A conflict found during descriptor preflight creates no payload; a
  losing publication race removes only its own unreferenced payload.
- `NativeArtifactIntegrityError`: stored size, SHA-256, URI, or descriptor data
  is invalid, including a payload key outside the approved namespaces.
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
