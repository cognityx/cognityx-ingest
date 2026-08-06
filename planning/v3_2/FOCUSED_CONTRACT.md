# Focused Contract

## Simple purpose

v3.2 freezes the shape of a richer ingest contract while keeping the current ingest product usable today.
It adds tests, audit notes, and future task boundaries around the existing PDF ingestion flow.

## What this task protects

- the current `cogni ingest` compatibility workflow
- the current Python ingest composition root
- the frozen provenance base fixture and the new v3.2 delta fixture
- the distinction between supported behavior now and missing behavior later

## What stays unchanged in T00

- production parsing behavior
- `cognityx-sdk`
- normal CLI syntax
- existing storage, job, and source-asset behavior
- existing parser selection behavior

## Future contract shape

The future contract is organized around:

- parser-native artifact preservation
- a canonical overlay with IDs and spans
- exactly three capability-source classes
- exactly three adaptive routing modes
- parser fusion and adjudication
- non-copying segmentation views
- retention and purge boundaries
- source graph and provenance addresses
- DataForge handoff artifacts
