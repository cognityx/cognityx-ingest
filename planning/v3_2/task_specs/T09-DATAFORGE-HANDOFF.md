# T09 DataForge Handoff

## Purpose

Produce the two focused downstream proof artifacts: paragraph Q/A support and one composite cross-source Knowledge Unit.

## Prerequisites

- T08 source graph and provenance addresses are available.
- DataForge consumer contract is confirmed against its current repository.

## Concrete production files allowed to change

- `src/cognityx_ingest/service.py`
- `src/cognityx_ingest/references.py`
- `src/cognityx_ingest/models.py`
- DataForge consumer files only in a coordinated `cognityx-dataforge` PR.

## Prohibited modules and scope

- No generalized KU ontology.
- No embeddings or vector database work.
- No SDK changes.

## Exact frozen fixture cases used

- `dataforge/paragraph_qa_contract.json`
- `dataforge/composite_ku_contract.json`

## Exact test files and node IDs

- `tests/v3_2/test_v3_2_dataforge_handoff.py::test_dataforge_paragraph_qa_contract`
- `tests/v3_2/test_v3_2_dataforge_handoff.py::test_dataforge_composite_ku_contract`

## Expected artifact/schema/API output

- Paragraph Q/A handoff schema `cognityx.dataforge.paragraph-qa-handoff/v1`.
- Composite KU handoff schema `cognityx.dataforge.composite-ku-handoff/v1`.
- Gold support excludes `rel-ambiguous-example`.

## Backward compatibility assertions

- Existing DataForge handoff tests continue to pass.

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

Document exactly which fields DataForge may consume and which fields remain out of scope.

## One bounded PR stop condition

Open coordinated `cognityx-ingest` and `cognityx-dataforge` PRs only if both repositories need changes; otherwise one ingest PR is sufficient.
