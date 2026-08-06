# Cognityx Ingest v3.2 Focused Fixture Pack

**Pack version:** `cognityx.ingest.fixture/v3.2.0`  
**Frozen on:** 6 August 2026  
**Purpose:** exact fixture and acceptance-contract input for the T00 fixture-first Codex task.

This is the authoritative **v3.2 delta fixture pack**. It must be used together with the existing frozen
`provenance_v1` fixture already in `cognityx/cognityx-ingest`. It deliberately does not duplicate the
existing PDF or its expected records.

## Mandatory existing base fixture

The repository must contain:

```text
tests/fixtures/provenance_v1/main_policy_v2.pdf
```

Expected source SHA-256:

```text
73a2dc18cc0ed79419a2208db93cc151e0a1fe092c96ed4322e449207f22630c
```

The verifier fails when the file is absent or has different bytes. Codex must reuse the existing fixture;
it must not generate a replacement PDF.

## What this pack freezes

- parser-native artifact preservation without flattening;
- parser-neutral canonical overlay through IDs, selectors and `NativeBinding` records;
- exactly three parser-capability sources: parser-discovered, human-guided and auto-learned;
- exactly three adaptive routing modes: deterministic, hybrid and LLM-directed;
- multi-parser observations, complementary facts, conflicts and adjudication states;
- non-copying segmentation views over canonical node IDs and spans;
- extraction identity, reuse, retention, legal hold, purge and tombstones;
- a connected source/provenance graph, not a semantic Knowledge Graph;
- strong, logical and evidence-set provenance addresses;
- two DataForge handoffs only: paragraph Q/A and one composite cross-source Knowledge Unit;
- compatibility expectations for the existing `cogni` CLI and Python client.

## Ground-truth boundary

Files under `sources/` are synthetic frozen source truth created specifically for v3.2 contract tests.
Files under `expected/`, `parser_observations/`, `segmentation_views/`, `routing/`, `retention/` and
`dataforge/` are manually authored expected records for those sources. They are not captured production
output.

The opaque Docling artifact is a **lossless-preservation fixture**, not a Docling conformance sample.
A separate optional runtime test must create a real `DoclingDocument`, persist it byte-for-byte, reload it,
and validate native pointers. This distinction prevents the fixture from pretending that a hand-authored
JSON sample is an authoritative Docling schema example.

## Installation into the repository

From the root of `cognityx-ingest`:

```bash
python /path/to/this-pack/install_into_repo.py .
python tests/fixtures/v3_2_focused/verify_fixture_pack.py --repo-root .
```

The installer copies only the delta fixture directory and planning inputs. It does not overwrite the
existing `provenance_v1` fixture or production code.

## Normal CLI remains stable

```bash
cogni ingest document.pdf
cogni ingest --asset src-...
cogni ingest --bundle research/reports
cogni job watch job-...
cogni document show doc-...
cogni artifact read doc-... provenance
```

Future capability, view and retention commands are additive and must not alter these workflows.
