# Ownership boundaries

| Concern | Owner |
|---|---|
| Immutable source blobs and physical deduplication | cognityx-storage |
| Background job lifecycle and progress | cognityx-jobs |
| SourceAsset registration coordination | cognityx-ingest |
| Parser adapters, routing, observations and fusion | cognityx-ingest |
| Native parser artifact retention metadata | cognityx-ingest, physical deletion through Storage |
| Canonical source graph and non-copying segmentation views | cognityx-ingest |
| User-facing Python composition and `cogni` CLI | cognityx-sdk |
| Semantic KG, Knowledge Units, Q/A, embeddings and training records | DataForge |
| Query-time retrieval, rank fusion and context assembly | Retrieval/DataForge service |
