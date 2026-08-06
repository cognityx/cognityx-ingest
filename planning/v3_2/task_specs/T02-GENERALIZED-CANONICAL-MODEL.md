# T02 Generalized Canonical Model

## Purpose

Add the parser-neutral canonical overlay for resources, presentation units, divisions, content nodes, representations, selectors, native bindings, relations, processing activities, and artifact descriptors.

## Prerequisites

- T01 native artifact storage is available.
- Current v1/v2 document artifacts remain readable.

## Concrete production files allowed to change

- `src/cognityx_ingest/models.py`
- `src/cognityx_ingest/service.py`
- `src/cognityx_ingest/structure.py`
- `src/cognityx_ingest/references.py`
- `src/cognityx_ingest/__init__.py`

## Prohibited modules and scope

- No SDK changes.
- No Storage or Jobs rewrites.
- No source fixture edits.

## Exact frozen fixture cases used

- `expected/canonical_content.json`
- `expected/native_bindings.json`

## Exact test files and node IDs

- `tests/v3_2/test_v3_2_canonical_contract.py::test_expected_canonical_content_fixture_matches_contract`
- `tests/v3_2/test_v3_2_canonical_contract.py::test_canonical_document_still_preserves_current_python_ingest_shape`

## Expected artifact/schema/API output

- Canonical content schema `cognityx.ingest.canonical-content/v3.2`.
- Canonical text appears under `content_nodes[*].content.text`.
- Source selectors and native bindings reference IDs and spans rather than copied text.

## Backward compatibility assertions

- Existing document, evidence, provenance, and manifest artifacts remain available.

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

Explain canonical text ownership in ordinary language and define native binding on first use.

## One bounded PR stop condition

Open one `cognityx-ingest` PR that introduces the model and passes focused plus full validation.
