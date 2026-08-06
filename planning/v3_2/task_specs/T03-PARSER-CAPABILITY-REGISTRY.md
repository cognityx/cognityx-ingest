# T03 Parser Capability Registry

## Purpose

Expose parser capabilities from exactly three source classes: parser-discovered, human-guided, and auto-learned. Routing must use this registry rather than model memory.

## Prerequisites

- T02 canonical records are available.
- Current `ParserRouter` and `ExtractionPolicy` behavior is preserved.

## Concrete production files allowed to change

- `src/cognityx_ingest/parser.py`
- `src/cognityx_ingest/enhancement.py`
- `src/cognityx_ingest/models.py`
- `src/cognityx_ingest/__init__.py`
- new `src/cognityx_ingest/parser_capabilities.py`

## Prohibited modules and scope

- No SDK changes.
- No adaptive routing implementation beyond registry lookup.
- No changes to parser extraction output semantics.

## Exact frozen fixture cases used

- `capability_registry/parser_capabilities.json` parser IDs `docling`, `pymupdf`, and `future-parser`.

## Exact test files and node IDs

- `tests/v3_2/test_v3_2_capability_registry.py::test_exactly_three_capability_source_classes`
- `tests/v3_2/test_v3_2_capability_registry.py::test_capability_registry_contains_real_parsers`
- `tests/v3_2/test_v3_2_production_contract_scaffold.py::test_parser_capability_registry_api_exposes_three_source_classes`

## Expected artifact/schema/API output

- `ParserCapabilityRegistry.from_router(router).get("docling")` returns a record with `capability_source_classes == ("parser-discovered", "human-guided", "auto-learned")`.

## Backward compatibility assertions

- Existing `ExtractionPolicy` legacy names remain accepted.

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

Document the three source classes and define why the registry, not an LLM, is authoritative.

## One bounded PR stop condition

Open one `cognityx-ingest` PR that makes the T03 strict xfail pass and keeps legacy parser tests green.
