# T06 Non-Copying Segmentation Views

## Purpose
Create reusable segmentation views that reference canonical node IDs and spans instead of copying source text.

## Prerequisites
- canonical model and parser fusion boundaries exist.

## Allowed production modules
- `src/cognityx_ingest/structure.py`
- `src/cognityx_ingest/service.py`
- `src/cognityx_ingest/models.py`

## Prohibited scope
- parser-native preservation redesign
- SDK changes
- new retrieval-layer behavior

## Tests to make pass
- strict tests that fail if view records contain copied source text.

## Backward compatibility requirements
- current document generation still works

## Validation commands
- `uv run pytest`
- `uv run mkdocs build --strict`
- `uv build`

## One-PR stop condition
Stop when the derived view API lands in one PR.
