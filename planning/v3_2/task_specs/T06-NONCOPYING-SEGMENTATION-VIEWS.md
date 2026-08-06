# T06 Non-Copying Segmentation Views

## Purpose

Create reusable segmentation views that reference canonical node IDs and spans instead of copying source text.

## Prerequisites

- T05 fusion and canonical IDs are available.
- Canonical text ownership remains single-copy.

## Concrete production files allowed to change

- `src/cognityx_ingest/models.py`
- `src/cognityx_ingest/service.py`
- `src/cognityx_ingest/structure.py`
- new `src/cognityx_ingest/segmentation_views.py`
- `src/cognityx_ingest/__init__.py`

## Prohibited modules and scope

- No query-time rank fusion.
- No SDK changes.
- No copied text in segment records.

## Exact frozen fixture cases used

- `segmentation_views/views.json`
- `expected/canonical_content.json`

## Exact test files and node IDs

- `tests/v3_2/test_v3_2_segmentation_views.py::test_segmentation_views_reference_ids_and_spans_not_copied_text`
- `tests/v3_2/test_v3_2_production_contract_scaffold.py::test_segmentation_view_api_references_ids_and_spans`

## Expected artifact/schema/API output

- `SegmentationViewService.build("view-paragraph-v1")` returns segments with node spans and no copied text.

## Backward compatibility assertions

- Existing document artifact text remains unchanged.

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

Document segmentation views as derived read models and define why text is reconstructed from IDs/spans.

## One bounded PR stop condition

Open one `cognityx-ingest` PR that makes the T06 strict xfail pass and keeps canonical text tests green.
