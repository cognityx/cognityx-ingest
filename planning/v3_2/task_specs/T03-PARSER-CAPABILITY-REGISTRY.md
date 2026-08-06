# T03 Parser Capability Registry

## Purpose
Store parser capabilities from live registry evidence in exactly three source classes.

## Prerequisites
- T01 and T02 boundaries are stable.

## Allowed production modules
- `src/cognityx_ingest/parser.py`
- `src/cognityx_ingest/enhancement.py`
- `src/cognityx_ingest/service.py`

## Prohibited scope
- SDK changes
- graph or DataForge design changes
- removing current parser compatibility names

## Tests to make pass
- strict tests for the three capability-source classes and registry-backed lookups.

## Backward compatibility requirements
- current parser-policy names stay compatible

## Validation commands
- `uv run pytest`
- `uv run mkdocs build --strict`
- `uv build`

## One-PR stop condition
Stop after the registry is exposed and the tests are stable in one PR.
