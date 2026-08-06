# T08 Source Graph And Provenance Addresses

## Purpose

Publish the connected source graph and provenance address resolver for exact, redirected, ambiguous, obsolete, forbidden, and unresolved outcomes.

## Prerequisites

- T07 retention metadata is stable.
- Canonical nodes, relations, and selectors are available.

## Concrete production files allowed to change

- `src/cognityx_ingest/models.py`
- `src/cognityx_ingest/references.py`
- `src/cognityx_ingest/service.py`
- new `src/cognityx_ingest/source_graph.py`
- `src/cognityx_ingest/__init__.py`

## Prohibited modules and scope

- No graph database selection.
- No semantic Knowledge Graph implementation.
- No SDK changes.

## Exact frozen fixture cases used

- `expected/source_graph.json` graph revision `sg-rev-001`
- `expected/provenance_addresses.json` strong address `addr-strong-pol-p2`

## Exact test files and node IDs

- `tests/v3_2/test_v3_2_production_contract_scaffold.py::test_source_graph_and_provenance_resolver_api_returns_exact`
- `tests/v3_2/test_v3_2_fusion_adjudication.py::test_ambiguous_and_unresolved_relations_are_never_gold_support`

## Expected artifact/schema/API output

- `SourceGraphRepository.load("sg-rev-001")` returns the frozen graph.
- `ProvenanceAddressResolver.resolve("addr-strong-pol-p2")` returns `exact` and node `pol-p2`.

## Backward compatibility assertions

- Existing provenance artifact remains available as `cogni artifact read <document-id> provenance`.

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

Define strong, logical, and evidence-set addresses with one plain-language example.

## One bounded PR stop condition

Open one `cognityx-ingest` PR that makes the T08 strict xfail pass and keeps current provenance tests green.
