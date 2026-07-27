# Cognityx Ingest

Ingest PDFs into source-addressable, canonical document artifacts. The initial
scope is deliberately narrow: local files and folders of PDFs.

The deterministic pipeline registers source bytes, extracts page text,
normalizes page sections and evidence, then persists artifacts through
`cognityx-storage`. `cognityx-jobs` is optional for durable lifecycle events.

LLM assistance is optional and routed only through `cognityx-inference`.
The optional `inference` package extra installs that client integration; normal
PDF ingestion does not load a model runtime.

Source registration is also available as a separate first-stage capability.
It creates Context, Bundle and Source resources over immutable storage bytes
without parsing the file. See [Source Storage](sources.md).

## CLI

```bash
cognityx-ingest ingest ./policy.pdf --storage-root /tmp/cognityx-storage
cognityx-ingest ingest ./pdf-folder --storage-root /tmp/cognityx-storage --owner-id alex
```

The command prints each run ID, optional job ID, document ID, manifest key, and
logical `storage://` artifact references. The `ingest` word is optional for
backward compatibility.

## Lifecycle And Artifacts

The CLI stores jobs in `<storage-root>/.cognityx-ingest/jobs.sqlite3` by
default. Use `--jobs-database /path/to/jobs.sqlite3` to select another durable
SQLite database.

```bash
# Submit an ingest and copy document_id/job_id from the JSON output.
cognityx-ingest ingest ./policy.pdf --storage-root /tmp/cognityx-storage --owner-id alex

# View only alex's jobs, then view one record and its ordered events.
cognityx-ingest jobs list --storage-root /tmp/cognityx-storage --owner-id alex
cognityx-ingest jobs show <job-id> --storage-root /tmp/cognityx-storage --owner-id alex

# Request cancellation of a queued or running job.
cognityx-ingest jobs cancel <job-id> --storage-root /tmp/cognityx-storage --owner-id alex

# Inspect generated canonical data and read one artifact as JSON-safe output.
cognityx-ingest documents list --storage-root /tmp/cognityx-storage
cognityx-ingest documents show <document-id> --storage-root /tmp/cognityx-storage
cognityx-ingest artifacts read <document-id> evidence --storage-root /tmp/cognityx-storage

# Permanently delete only this document's source and generated artifacts.
cognityx-ingest documents delete <document-id> --yes --storage-root /tmp/cognityx-storage
```

`jobs cancel` records a durable cancellation request. The current local
synchronous parser does not yet check for cancellation while parsing, so it
cannot interrupt work that has already completed. Document deletion retains
the durable job history and requires `--yes`.

## Python API

```python
from cognityx_ingest import ExecutionContext, IngestService
from cognityx_storage import LocalStorageBackend, StorageClient

storage = StorageClient(LocalStorageBackend("/tmp/cognityx-storage")).for_shared_data()
context = ExecutionContext(
    run_id="run-123",
    correlation_id="request-456",
    principal_id="alex",
    tenant_id="tenant-a",
    scopes={"environment": "development"},
)
result = IngestService(storage).ingest("policy.pdf", context=context)

print(result.document.document_id)
print(result.usage.pages)
for artifact in result.artifacts:
    print(artifact.artifact_id, artifact.uri)
```

## Lifecycle Management API

```python
from cognityx_ingest import ExecutionContext, IngestManager
from cognityx_jobs import JobRepository

jobs = JobRepository("/tmp/cognityx-storage/.cognityx-ingest/jobs.sqlite3")
manager = IngestManager(storage, jobs)
context = ExecutionContext(run_id="run-789", correlation_id="request-999", principal_id="alex")

for job in manager.list_jobs(context, owner_id="alex"):
    print(job["job_id"], job["state"])

details = manager.show_document(context, "pdf-0123456789abcdef")
evidence_jsonl = manager.read_artifact(context, "pdf-0123456789abcdef", "evidence")

# This is irreversible for the selected document artifacts only.
manager.delete_document(context, "pdf-0123456789abcdef")
```

## Custom Control Client

The local client allows standalone operation. A future deployment can replace
it without changing the parser or canonical-document contract.

```python
from cognityx_ingest import ControlDecision, IngestService, UsageReport

class CompanyControl:
    def authorize(self, context, action, resource=None, request=None):
        assert action in {"ingest.job.submit", "ingest.job.cancel", "ingest.result.read", "ingest.document.delete"}
        return ControlDecision(allowed=True, limits={"max_document_size": 100_000_000})

    def report_usage(self, context, usage: UsageReport):
        print(usage.documents, usage.pages, usage.duration_ms)

service = IngestService(storage, control=CompanyControl())
result = service.ingest("policy.pdf", context=context)
```

`ControlDecision.allowed=False` raises `IngestAuthorizationError`. Currently,
the service can enforce `max_document_size` and `max_pages`; policy evaluation
remains outside this repository.
