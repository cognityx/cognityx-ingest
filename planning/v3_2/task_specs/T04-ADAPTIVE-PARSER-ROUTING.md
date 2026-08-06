# T04 Adaptive Parser Routing

## Purpose

Implement deterministic, hybrid, and LLM-directed routing modes while retaining current legacy parser-policy names.

## Prerequisites

- T03 registry API is available.
- Current `ExtractionPolicy` behavior is covered.

## Concrete production files allowed to change

- `src/cognityx_ingest/parser.py`
- `src/cognityx_ingest/enhancement.py`
- `src/cognityx_ingest/models.py`
- `src/cognityx_ingest/__init__.py`

## Prohibited modules and scope

- No DataForge output implementation.
- No SDK changes.
- No unbounded live internet discovery in CI.

## Exact frozen fixture cases used

- `routing/deterministic_plan.json`
- `routing/hybrid_plan.json`
- `routing/llm_directed_plan.json`
- `capability_registry/parser_capabilities.json` allowed routing modes

## Exact test files and node IDs

- `tests/v3_2/test_v3_2_routing_and_compatibility.py::test_exactly_three_adaptive_routing_modes`
- `tests/v3_2/test_v3_2_routing_and_compatibility.py::test_routing_plan_files_match_the_three_modes`
- `tests/v3_2/test_v3_2_routing_and_compatibility.py::test_legacy_parser_policy_names_remain_compatible`

## Expected artifact/schema/API output

- Routing plan schema `cognityx.ingest.routing-plan/v3.2`.
- Explicit compatibility mapping from legacy names to new routing modes.

## Backward compatibility assertions

- `fixed`, `rule`, `fallback`, `compare`, and `agent` remain valid `ExtractionPolicy` modes.

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

Document routing mode meanings and the legacy-name compatibility map.

## One bounded PR stop condition

Open one `cognityx-ingest` PR that implements routing modes and leaves legacy parser policies green.
