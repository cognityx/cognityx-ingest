# T09 DataForge Handoff

## Purpose
Produce the focused downstream proof artifacts for paragraph Q/A and one composite Knowledge Unit handoff.

## Prerequisites
- source graph and provenance addresses are stable.

## Allowed production modules
- `src/cognityx_ingest/service.py`
- `src/cognityx_ingest/references.py`
- `src/cognityx_ingest/models.py`

## Prohibited scope
- SDK changes
- general DataForge ontology work
- broad retrieval redesign

## Tests to make pass
- focused handoff tests for the two required downstream paths.

## Backward compatibility requirements
- existing ingest outputs remain readable

## Validation commands
- `uv run pytest`
- `uv run mkdocs build --strict`
- `uv build`

## One-PR stop condition
Stop when the focused handoff is merged in one PR.
