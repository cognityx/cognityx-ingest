# Extraction Reuse, Retention, and Purge

## Background and purpose

A document parser may produce a large, parser-specific result. For example,
Docling may produce a JSON document containing its own blocks and pointers.
Cognityx keeps those original bytes because they can explain how stable canonical
content was produced. Keeping every parser result forever is expensive, but
deleting one too early can break a consumer that still follows a native pointer.

T07 provides the policy layer between those two risks. It answers four questions:

1. Is this exactly the same extraction that was run before?
2. Does any current consumer still require the parser-native payload?
3. Has retention expired, and is deletion blocked by a legal hold?
4. After Storage removes the payload, what compact facts must remain?

The result is **extraction retention metadata**: small records describing identity,
references, state, policy, and hashes. The metadata never contains parser payload
bytes or another copy of canonical source text.

## Where it fits

```text
original SourceAsset bytes
          |
          v
parser execution and T01 native artifact
          |
          +------> T02 canonical content and NativeBinding metadata
          |
          +------> T05 observations and fusion decisions
          |
          +------> T06 non-copying segmentation views
          |
          v
T07 exact identity and retention metadata
          |
          +------> advisory purge candidates
          |
          v
Storage-owned physical deletion
          |
          v
T07 post-deletion tombstone

T08 Source Graph and provenance addresses come later.
T09 DataForge and T10 SDK/CLI controls come later.
```

Normal `cogni ingest` behavior does not change in T07. Current parser interfaces do
not always reveal every parser, model, adapter, and pipeline setting before a
parser runs. Ingest therefore does not pretend that it can safely skip parsing.
Only application code that already has a complete extraction identity may ask for
exact reuse.

## Extraction identity in ordinary language

Two parser results are reusable only when they came from the same source bytes and
the same complete execution settings. This definition is called the **extraction
identity**. It has exactly six fields:

| Field | Meaning |
| --- | --- |
| `source_sha256` | SHA-256 of the source bytes |
| `parser_id` | Exact parser identity, such as `docling` |
| `parser_version` | Explicit parser version |
| `parser_configuration_hash` | Hash of all execution-affecting parser, adapter, and pipeline settings |
| `model_version` | Exact model version, or the documented value `none` |
| `scope` | Logical reuse boundary, such as a tenant and collection |

The public `ExtractionIdentity` hashes compact sorted-key UTF-8 JSON over exactly
these six fields. Changing any one field changes the digest. Adapter and pipeline
identity belong inside `parser_configuration_hash`; they are not extra top-level
fields because the v3.2 formula is frozen.

`ExtractionIdentity.from_configuration(...)` accepts explicit JSON-safe settings
and hashes them deterministically. Mapping insertion order does not matter. It
rejects secrets, credentials, API tokens, run IDs, local paths, temporary paths,
non-finite numbers, and unsupported objects. Those values are either unsafe to
retain or unrelated to the reusable parser result.

Configuration key checks compare meaning rather than spelling. For example,
`apiKey`, `api_key`, and `api-key` all mean “API key” and are rejected. This
normalization covers camel case, underscores, hyphens, and spaces. Exact secret
words are rejected, but harmless technical words that merely contain the same
letters are not. A `tokenizer` setting is valid. A portable model identifier such
as `Qwen/Qwen3-8B` is also valid, including when it appears under `model_path`;
absolute paths, traversal such as `../cache`, Windows paths, and `file://` values
remain forbidden operational data.

An incomplete identity is not a weaker cache key. It is non-reusable. Matching
only a source hash or parser class can return the wrong result when an OCR option,
table mode, model, adapter, or pipeline changed.

Registration establishes initial lifecycle state exactly once. An equivalent
retry proves that the immutable artifact and extraction identity agree, then
returns the current record without applying the retry's old reference list. This
is **identity idempotency**: repeating the identity claim is safe. It is not
**lifecycle replay**. If consumer A was explicitly removed, a late copy of the
original registration cannot add A again, even after retention expires or while a
legal hold is active. It also cannot change state, hold, tombstone, update audit
fields, or append another event. Only explicit reference and policy operations own
those later lifecycle changes, and concurrent retries follow the same rule.

## Exact reuse flow

`ExtractionRetentionService.acquire_reusable(...)` does no fuzzy matching and
never executes a parser. Its processing flow is:

1. Hash the caller's already complete six-field identity.
2. In one immediate SQLite transaction, find the exact validated record.
3. Add the new active reference before a purge planner can see the record as free.
4. Reload the payload through `NativeArtifactStore.reload(...)`.
5. Let T01 verify byte count, SHA-256, descriptor identity, and supported pointers.
6. Compare the descriptor with the immutable T07 record.
7. Return a reusable result only after every check succeeds.

If the payload is missing or corrupt, T07 removes only the reference inserted by
that acquisition and raises `ExtractionReuseIntegrityError`. A reference that
already existed belongs to another lifecycle and remains in place. An exact miss,
an expired record, or a purged record returns a normal non-reuse result.

Legal hold does not block reading or reuse. A legal hold means “do not delete”; it
does not mean “do not inspect.”

## Active references

An **active reference** is a stable identity for a current consumer that needs the
raw parser payload. It is not every record that mentions a parser or payload hash.

The `collect_reference_ids(...)` helper can collect three reviewed kinds:

- A canonical `NativeBinding` whose `artifact_id` matches contributes its
  `binding_id`.
- A parser-native T06 view whose profile names the artifact contributes its
  collision-safe `segview-<sha256>` reference.
- A downstream consumer contributes an explicit stable ID, for example
  `consumer-dataforge-1`.

Canonical content is validated against authoritative T01 descriptors before its
bindings are accepted. T06 view sets must pass
`SegmentationViewService.validate_view_set(...)`. Calling the value-level
`SegmentationViewSet.validate()` alone does not prove that a set belongs to the
available canonical content. This stronger canonical-bound validation prevents a
foreign view from blocking deletion of the wrong artifact.

The T06 reference digest covers the exact canonical-content SHA-256, the view ID,
and the view's cache identity. Two independently valid view sets may both contain
`view-main`; they still receive different retention references when their
canonical content or view definition differs. NativeBinding IDs and explicit
downstream consumer IDs keep their existing spelling.

T05 observations and fusion decisions do not automatically become raw-payload
references. They are compact accepted or auditable evidence and survive
independently. A caller may add an explicit reference if a reviewed T05 consumer
truly needs the native payload.

Reference rows are context scoped, sorted when read, and deduplicated. Removing a
reference is always an explicit lifecycle action. Expiry, planning, or a failed
unrelated operation never silently removes one.

## Retention state and legal hold

The durable state machine has three values:

```text
validated -> retention-expired -> purged
```

- `validated` means the payload may be selected for exact reuse after integrity
  checks.
- `retention-expired` means no new reuse is selected and the payload may become a
  purge candidate.
- `purged` means Storage has removed the payload and T07 has recorded a tombstone.

The transition is one way. T07 never resurrects a purged record. A later parser
run with the same six-field identity may register a new artifact ID while the old
tombstone remains as history.

A **legal hold** is an independent durable switch used when policy, litigation,
or audit requires preservation. Enabling or releasing it is authorized,
context-scoped, idempotent, and auditable. It blocks purge even when retention has
expired and no active references remain.

## Purge eligibility algorithm

Purge eligibility is derived from current facts. It is not a writable boolean.
The exact precedence is:

1. A purged record is protected with reason `already purged`.
2. Legal hold protects it with reason `legal hold blocks purge`.
3. Active references protect it with reason `active references remain`.
4. Any state other than `retention-expired` protects it with reason
   `retention has not expired`.
5. Only an expired record with no hold and no references is eligible.

`ExtractionRetentionService.plan_purge(...)` returns immutable eligible and
protected candidate tuples. Candidates contain artifact IDs, extraction identity,
logical Storage keys, and hashes. Planning reads metadata only. It does not call
`StorageClient.delete`, filesystem `unlink`, `rmtree`, a backend-private deletion
API, a parser, a model provider, or an LLM.

## A plan is not deletion authority

A purge plan is an advisory snapshot. Consider this sequence:

1. An expired artifact has no references, so a plan marks it eligible.
2. DataForge starts work and adds an active reference.
3. A worker later presents the old plan.

Deleting from the old plan would be unsafe. T07 therefore does not provide a
physical delete method. Ingest exposes logical candidates to a Storage-owned
cleanup boundary. Storage owns provider behavior, object deletion, retries, and
physical safety.

After Storage reports that it removed a payload, application composition calls
`finalize_purge(...)`. Finalization uses the same context-bound
`NativeArtifactStore` that registration and reuse use. It reads and validates the
immutable descriptor, calls `reload(...)`, and accepts only one result: the
payload is absent while the same descriptor can still be read and still matches
the retention record. A present payload, missing descriptor, corrupt payload,
invalid native pointer, or Storage failure stops finalization.

The supported public flow is deliberately narrow:

```text
application or operator
          |
          v
ExtractionRetentionService.finalize_purge()
          |
          v
same T01 NativeArtifactStore: read -> reload -> read
          |
          v
internal registry transaction
          |
          v
purged metadata + tombstone + event
```

After the read-only T01 check, the service creates a small internal handoff bound
to the artifact ID, extraction identity, artifact SHA-256, and logical Storage
key. These matching fields are not proof that Storage bytes are absent. They only
carry the result of the service's immediately preceding T01 observation into the
transaction. Applications must never manufacture an “absence proof” from catalog
metadata, and the registry therefore exposes no supported public purge-finalizer
that accepts one.

The registry's underscore-prefixed finalization seam exists only to finish the
service operation under `BEGIN IMMEDIATE`. It rechecks current state, legal hold,
and active references and compares every internal handoff field with the live
record before committing. It accepts no callback, appears in no ordinary public
API or example, and the service accepts no second Storage client, so checking the
wrong backend is structurally unavailable. If a new reference or hold appeared,
finalization fails even if an old plan said eligible.

This separation is a trust boundary, not cryptographic attestation. T01 remains
the authoritative observation boundary because its descriptor and payload share
one configured Storage scope. Storage remains responsible for physical deletion;
T07 only observes the outcome and records metadata after the safe service path.

This algorithm is called **stale-plan revalidation**. It means a previous decision
cannot overrule newer safety information.

## Tombstones and information that survives

After successful finalization, the record enters `purged` and contains a compact
`RetentionTombstone` with:

- parser ID;
- parser version;
- source SHA-256;
- parser artifact SHA-256;
- deletion reason.

The surrounding record still keeps the extraction identity, parser configuration
hash, model version, scope, artifact ID, logical key, and audit timestamps. The
immutable T01 `NativeArtifactDescriptor` also survives. It can prove what used to
exist even though `NativeArtifactStore.reload(...)` can no longer load payload
bytes.

A production purged record accepts its tombstone only when parser ID, parser
version, source SHA-256, and artifact SHA-256 exactly equal the surrounding
immutable record. Reformatting or recomputing valid tombstone JSON cannot bypass
this cross-binding. A mismatch is treated as catalog corruption rather than
historical truth. The standalone fixture tombstone can still carry its frozen
non-production marker; the stricter equality applies when a tombstone is embedded
in a production `ExtractionRetentionRecord`.

Raw parser payload purge never means “delete the document.” T07 does not delete or
rewrite:

- canonical `ContentNode.content.text`;
- `SourceSelector` records;
- canonical `NativeBinding` metadata;
- T05 observations and fusion decisions;
- T06 segmentation view records;
- provenance and compact lineage;
- SourceAsset bytes;
- retention records or tombstones.

These records either own stable content or explain how accepted content was
produced. They remain available after a parser-private audit payload expires.

## Durable repository design

T07 reuses the existing SourceAsset SQLite catalog instead of creating another
database. The migration is additive:

- `extraction_retention_records` stores immutable identity, descriptor facts,
  state, hold, audit fields, and optional tombstone JSON.
- `extraction_references` stores deduplicated context/artifact/reference rows.
- `extraction_retention_events` stores append-only changed-state facts in database
  sequence order.

The existing contexts, bundles, sources, SourceAsset deduplication, and blob GC
behavior remain unchanged. Race-sensitive methods use `BEGIN IMMEDIATE`, so reuse
acquisition, reference changes, hold changes, expiry, and finalization serialize
with competing writers.

The event vocabulary is exactly `registered`, `reference-added`,
`reference-removed`, `legal-hold-enabled`, `legal-hold-released`,
`retention-expired`, and `purged`. Registration includes its initial references in
the registered state and emits only `registered`; later reference changes receive
their own events. Every event is inserted in the same transaction as the change.
An idempotent retry produces no event and does not update audit timestamps. A
failed reuse integrity check records the reference addition and its compensating
removal because both rows really changed. Purge history survives as long as its
retention record and is available through
`list_extraction_retention_events(...)`, ordered and scoped to the caller's
context.

Raw SQLite exceptions, SQL statements, backend paths, payload bytes, source text,
and credentials do not cross the public boundary. Callers receive bounded T07
errors such as `ExtractionRetentionConflictError`,
`ExtractionReuseIntegrityError`, `ExtractionPurgeBlockedError`, and
`ExtractionPurgeFinalizationError`.

## Python composition example

```python
from cognityx_ingest.cleanup import ExtractionRetentionService
from cognityx_ingest.models import ExtractionIdentity

identity = ExtractionIdentity.from_configuration(
    source_sha256=source_sha256,
    parser_id="docling",
    parser_version="2.5.0",
    parser_configuration={
        "adapter": "canonical-v3-2",
        "pipeline": "standard",
        "table_mode": "accurate",
    },
    model_version="none",
    scope="tenant-a/policies",
)

retention = ExtractionRetentionService(
    registry=source_asset_registry,
    native_artifacts=native_artifact_store,
)

# Registration occurs after T01 has stored and verified the exact payload.
record = retention.register_extraction(
    execution,
    identity,
    descriptor,
    reference_ids=("bind-policy-heading-docling",),
)

# Exact reuse is available only when composition already knows all six fields.
result = retention.acquire_reusable(
    execution,
    identity,
    "consumer-dataforge-1",
)
```

The example does not add this lookup to normal `IngestService`. Doing so without
complete pre-parser settings would create unsafe cache hits.

## Existing cleanup compatibility

`SourceAssetCleanupService.plan_blobs(...)` and `execute_blobs(...)` continue to
govern content-addressed SourceAsset blobs. They retain grace-period planning,
live-reference rechecks, batching, authorization, and Storage-owned deletion.

T07 parser extraction retention is a separate metadata domain. It does not merge
native artifact purge into `cogni cleanup blobs`, add a new CLI command, or change
normal SourceAsset deduplication.

## Ownership and non-goals

T07 owns exact identity, retained-artifact metadata, exact reuse lookup, active
references, expiry, legal hold, purge eligibility, advisory plans, safe
finalization, and tombstones.

T07 does not own parser choice or execution, routing, fusion, physical deletion,
Storage internals, SourceAsset blob GC, T08 Source Graph and provenance addresses,
DataForge generation, retrieval, SDK, or CLI configuration. Those boundaries keep
policy decisions reviewable and prevent one service from becoming an ambiguous
owner of both evidence and destructive storage operations.
