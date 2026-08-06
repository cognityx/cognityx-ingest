# T01 Native Parser Preservation

## Purpose

Persist parser-native artifacts byte-for-byte, read them back, and reload them with native pointers intact. This lets Cognityx keep the original parser result while exposing a parser-neutral canonical overlay.

## Prerequisites

- T00 fixture scaffold is merged or checked out.
- `tests/fixtures/v3_2_focused/native_artifacts/docling_document_opaque.json` is unchanged.
- Existing `ExtractionResult.raw_artifact` and `IngestService` raw artifact persistence behavior is understood.

## Concrete production files allowed to change

- `src/cognityx_ingest/service.py`
- `src/cognityx_ingest/parser.py`
- `src/cognityx_ingest/models.py`
- `src/cognityx_ingest/__init__.py`
- new `src/cognityx_ingest/native_artifacts.py`

## Prohibited modules and scope

- No `cognityx-sdk` changes.
- No parser behavior redesign.
- No changes to `tests/fixtures/provenance_v1/main_policy_v2.pdf`.
- No broad canonical model implementation.

## Exact frozen fixture cases used

- `native_artifacts/docling_document_opaque.json`
- `expected/native_bindings.json` binding `bind-pol-p2-docling`

## Exact test files and node IDs

- `tests/v3_2/test_v3_2_native_parser_preservation.py::test_docling_native_artifact_round_trip_preserves_bytes_and_native_pointers`
- `tests/v3_2/test_v3_2_native_parser_preservation.py::test_opaque_docling_artifact_fixture_contains_native_pointers`

## Expected artifact/schema/API output

- `NativeArtifactStore.store(...)` returns an artifact descriptor with `artifact_id`, `sha256`, storage URI, parser ID, and native pointers.
- `NativeArtifactStore.reload(...)` returns the original bytes, same SHA-256, and stored native pointers.

## Backward compatibility assertions

- `cogni ingest <path>` and `IngestService.ingest(...)` continue to work.
- Existing provenance raw artifact records remain readable.

## Targeted validation commands

- `uv run pytest --collect-only -q tests/v3_2`
- `uv run pytest -q tests/v3_2`

## Full validation commands

- `uv sync --extra dev`
- `uv run pytest`
- `python tests/fixtures/v3_2_focused/verify_fixture_pack.py --repo-root .`
- `uv run mkdocs build --strict`
- `uv build`
- `git diff --check`

## Documentation requirements

Document why native artifacts are separate from canonical records and how retention is governed.

## One bounded PR stop condition

Open one `cognityx-ingest` PR that makes the T01 strict xfail pass and keeps full validation green.
