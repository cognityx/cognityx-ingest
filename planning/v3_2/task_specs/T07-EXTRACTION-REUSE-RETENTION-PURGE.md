# T07 Extraction Reuse Retention Purge

## Purpose

Define extraction identity, reuse, retention, legal hold, purge eligibility, and tombstone behavior while protecting canonical content and active bindings.

## Prerequisites

- T06 view records and native bindings exist.
- Current SourceAsset cleanup behavior remains understood.

## Concrete production files allowed to change

- `src/cognityx_ingest/cleanup.py`
- `src/cognityx_ingest/service.py`
- `src/cognityx_ingest/source_assets.py`
- `src/cognityx_ingest/models.py`

## Prohibited modules and scope

- No direct physical deletion outside Storage-owned cleanup.
- No SDK changes.
- No deletion of canonical content, selectors, compact lineage, or active bindings.

## Exact frozen fixture cases used

- `retention/retention_cases.json` artifacts `art-docling-001`, `art-old-parser-001`, and `art-legal-hold-001`.

## Exact test files and node IDs

- `tests/v3_2/test_v3_2_retention_and_reuse.py::test_retention_fixture_covers_reuse_hold_and_purge`

## Expected artifact/schema/API output

- Retention records expose active reference protection, legal hold blocking, purge eligibility, and post-purge tombstones.

## Backward compatibility assertions

- Existing source-asset cleanup commands remain owner-scoped and explicit.

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

Explain retention and purge as metadata decisions before any Storage-owned physical deletion.

## One bounded PR stop condition

Open one `cognityx-ingest` PR that passes retention fixture tests and existing cleanup tests.
