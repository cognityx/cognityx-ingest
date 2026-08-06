# T05 Parser Alignment Fusion Adjudication

## Purpose

Separate parser routing from parser fusion. Align parser observations to common source regions, preserve complementary facts, and retain unresolved or conflicting states.

## Prerequisites

- T04 routing can invoke multiple parsers.
- Canonical node and selector IDs are available.

## Concrete production files allowed to change

- `src/cognityx_ingest/parser.py`
- `src/cognityx_ingest/service.py`
- `src/cognityx_ingest/structure.py`
- `src/cognityx_ingest/models.py`

## Prohibited modules and scope

- No segmentation view materialization.
- No DataForge handoff implementation.
- No SDK changes.

## Exact frozen fixture cases used

- `parser_observations/fusion_cases.json`
- `expected/source_graph.json` relation `rel-ambiguous-example`

## Exact test files and node IDs

- `tests/v3_2/test_v3_2_fusion_adjudication.py::test_fusion_cases_preserve_agreement_conflict_and_unresolved_states`
- `tests/v3_2/test_v3_2_fusion_adjudication.py::test_ambiguous_and_unresolved_relations_are_never_gold_support`

## Expected artifact/schema/API output

- Fusion records preserve `agreement`, `complementary`, `conflict`, and `unresolved` states.
- Ambiguous relations are never gold support.

## Backward compatibility assertions

- Single-parser extraction remains deterministic.

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

Explain alignment, fusion, and adjudication as separate steps with one concrete example.

## One bounded PR stop condition

Open one `cognityx-ingest` PR that passes fusion fixture cases and existing parser tests.
