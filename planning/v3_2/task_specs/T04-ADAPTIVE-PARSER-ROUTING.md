# T04 Adaptive Parser Routing

## Purpose
Route parser work with three bounded modes: deterministic, hybrid, and LLM-directed.

## Prerequisites
- parser capability registry is available.

## Allowed production modules
- `src/cognityx_ingest/parser.py`
- `src/cognityx_ingest/enhancement.py`
- `src/cognityx_ingest/service.py`

## Prohibited scope
- DataForge outputs
- SDK surface changes
- uncontrolled model access

## Tests to make pass
- routing mode tests and compatibility tests for existing policy names.

## Backward compatibility requirements
- current parser policy names remain accepted

## Validation commands
- `uv run pytest`
- `uv run mkdocs build --strict`
- `uv build`

## One-PR stop condition
Stop once routing is deterministic, bounded, and merged behind one PR.
