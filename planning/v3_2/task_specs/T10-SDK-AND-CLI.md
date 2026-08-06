# T10 SDK And CLI

## Purpose

Expose settled v3.2 read surfaces through the user-facing SDK and CLI while preserving existing workflows.

## Prerequisites

- T09 handoff behavior is implemented.
- Current SDK composition root has been inspected before edits.

## Concrete production files allowed to change

- `src/cognityx_ingest/cli.py` in `cognityx-ingest` if component CLI compatibility wrappers are needed.
- `cognityx-sdk` command/composition files identified during that repository's audit.
- `docs/index.md` and related source docs in the affected repository or repositories.

## Prohibited modules and scope

- No parser core redesign.
- No fixture redesign.
- No Storage or Jobs rewrites.

## Exact frozen fixture cases used

- `README.md` normal CLI examples.
- `dataforge/paragraph_qa_contract.json` only if a user-facing read command exposes the handoff.

## Exact test files and node IDs

- `tests/v3_2/test_v3_2_cli_and_python_compatibility.py::test_cli_ingest_paths_remain_supported`
- `tests/v3_2/test_v3_2_cli_and_python_compatibility.py::test_cli_compatibility_aliases_remain_supported`

## Expected artifact/schema/API output

- Existing commands remain valid: `cogni ingest <path>`, `cogni ingest --asset <asset-id>`, `cogni ingest --bundle <bundle-path>`, `cogni job watch <job-id>`, `cogni document show <document-id>`, and `cogni artifact read <document-id> provenance`.
- New user-facing commands expose only settled v3.2 read surfaces.

## Backward compatibility assertions

- Current normal CLI and Python composition root remain supported.

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

Update ordinary-language CLI and SDK docs in the affected repository. Define unfamiliar terms on first use.

## One bounded PR stop condition

Use coordinated repository PRs when SDK changes are needed: one `cognityx-ingest` PR for component behavior/docs and one `cognityx-sdk` PR for the user-facing `cogni` surface. Do not hide cross-repository ownership in one ambiguous PR.
