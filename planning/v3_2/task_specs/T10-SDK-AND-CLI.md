# T10 SDK And CLI

## Purpose
Expose the new ingest contract cleanly through the user-facing SDK and CLI without breaking the normal workflows.

## Prerequisites
- the underlying ingest contract is settled.

## Allowed production modules
- `cognityx-sdk` repository surfaces only
- `src/cognityx_ingest/cli.py`
- compatibility wrappers in `src/cognityx_ingest/*` if needed

## Prohibited scope
- parser core redesign
- fixture redesign
- unrelated Storage or Jobs rewrites

## Tests to make pass
- CLI compatibility tests and Python composition tests.

## Backward compatibility requirements
- `cogni ingest <path>`
- `cogni ingest --asset <asset-id>`
- `cogni ingest --bundle <bundle-path>`
- `cogni job watch <job-id>`
- `cogni document show <document-id>`
- `cogni artifact read <document-id> provenance`

## Validation commands
- `uv run pytest`
- `uv run mkdocs build --strict`
- `uv build`

## One-PR stop condition
Stop when the user-facing surface is compatible and the PR is ready.
