# Cognityx Ingest

Ingest PDFs into source-addressable, canonical document artifacts. The initial
scope is deliberately narrow: local files and folders of PDFs.

The deterministic pipeline registers source bytes, extracts page text,
normalizes page sections and evidence, then persists artifacts through
`cognityx-storage`. `cognityx-jobs` is optional for durable lifecycle events.

LLM assistance is optional and routed only through `cognityx-inference`.
The optional `inference` package extra installs that client integration; normal
PDF ingestion does not load a model runtime.

## CLI

```bash
cognityx-ingest ingest ./policy.pdf --storage-root /tmp/cognityx-storage
cognityx-ingest ingest ./pdf-folder --storage-root /tmp/cognityx-storage --owner-id alex
```

The command prints each run ID, optional job ID, document ID, manifest key, and
logical `storage://` artifact references. The `ingest` word is optional for
backward compatibility.

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

## Custom Control Client

The local client allows standalone operation. A future deployment can replace
it without changing the parser or canonical-document contract.

```python
from cognityx_ingest import ControlDecision, IngestService, UsageReport

class CompanyControl:
    def authorize(self, context, action, resource=None, request=None):
        assert action == "ingest.job.submit"
        return ControlDecision(allowed=True, limits={"max_document_size": 100_000_000})

    def report_usage(self, context, usage: UsageReport):
        print(usage.documents, usage.pages, usage.duration_ms)

service = IngestService(storage, control=CompanyControl())
result = service.ingest("policy.pdf", context=context)
```

`ControlDecision.allowed=False` raises `IngestAuthorizationError`. Currently,
the service can enforce `max_document_size` and `max_pages`; policy evaluation
remains outside this repository.
