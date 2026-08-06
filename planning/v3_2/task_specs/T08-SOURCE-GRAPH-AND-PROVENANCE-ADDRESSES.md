# T08 Source Graph And Provenance Addresses

## Purpose
Publish the connected source graph and address types that let downstream systems resolve exact, redirected, ambiguous, obsolete, forbidden, or unresolved provenance targets.

## Prerequisites
- canonical model and retention boundaries are stable.

## Allowed production modules
- `src/cognityx_ingest/references.py`
- `src/cognityx_ingest/service.py`
- `src/cognityx_ingest/models.py`

## Prohibited scope
- broad graph database selection
- DataForge semantic KG work
- SDK changes

## Tests to make pass
- source-graph shape and provenance-address resolution tests.

## Backward compatibility requirements
- existing provenance outputs remain available

## Validation commands
- `uv run pytest`
- `uv run mkdocs build --strict`
- `uv build`

## One-PR stop condition
Stop when the new graph and address surface is merged in one PR.
