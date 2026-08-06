# Existing Asset And Fixture Audit

## Purpose

This audit records the current repository state for v3.2 T00 so later tasks can build on stable fixtures,
contracts, and production seams without changing parsing behavior.

## Frozen design inputs

- `design_input/v3_2/Cognityx_Ingest_v3_2_Adaptive_Segmentation_Source_Graph_and_Provenance_Address_Plan.docx`
- `design_input/v3_2/Cognityx_Ingest_v3_2_Focused_Fixture_Pack.zip`
- `design_input/v3_2/Cognityx_Ingest_v3_2_Focused_Fixture_Pack.zip.sha256`

The ZIP checksum was verified before installation. The original pack checksum is preserved separately from
the repository-install checksum manifest.

## Reused base fixture

- `tests/fixtures/provenance_v1/main_policy_v2.pdf`
- Required SHA-256: `73a2dc18cc0ed79419a2208db93cc151e0a1fe092c96ed4322e449207f22630c`

This PDF is reused as-is. It is not replaced, duplicated, or regenerated.

## Newly installed delta fixture

- `tests/fixtures/v3_2_focused/README.md`
- `tests/fixtures/v3_2_focused/fixture_manifest.json`
- `tests/fixtures/v3_2_focused/repo_install_manifest.sha256sums.txt`
- `tests/fixtures/v3_2_focused/capability_registry/parser_capabilities.json`
- `tests/fixtures/v3_2_focused/expected/*.json`
- `tests/fixtures/v3_2_focused/parser_observations/*.json`
- `tests/fixtures/v3_2_focused/routing/*.json`
- `tests/fixtures/v3_2_focused/retention/*.json`
- `tests/fixtures/v3_2_focused/segmentation_views/views.json`
- `tests/fixtures/v3_2_focused/dataforge/*.json`
- `tests/fixtures/v3_2_focused/native_artifacts/*.json`
- `tests/fixtures/v3_2_focused/sources/*`

## Repository surfaces audited

- `src/cognityx_ingest/cli.py`
- `src/cognityx_ingest/service.py`
- `src/cognityx_ingest/parser.py`
- `src/cognityx_ingest/structure.py`
- `src/cognityx_ingest/enhancement.py`
- `src/cognityx_ingest/source_assets.py`
- `tests/provenance/*`
- `tests/test_service.py`
- `tests/test_lifecycle_cli.py`
- `docs/index.md`
- `docs/contract.md`

## Current supported behavior

- `cogni ingest <path>` remains supported.
- `cogni ingest --asset <asset-id>` remains supported.
- `cogni ingest --bundle <bundle-path>` remains supported.
- `cogni job watch <job-id>` remains supported.
- `cogni document show <document-id>` remains supported.
- `cogni artifact read <document-id> provenance` remains supported.
- The Python composition root remains centered on `IngestService`, `SourceAssetRegistry`, `StorageClient`,
  and `ExecutionContext`.

## Deferred scope

- parser-native preservation APIs beyond the existing store/read/reload seam
- generalized canonical model
- parser capability registry
- adaptive parser routing
- parser alignment/fusion/adjudication
- non-copying segmentation views
- extraction reuse/retention/purge controls
- source graph and provenance address APIs
- DataForge handoff APIs beyond the existing contract
- SDK-facing surface updates
