# T07 Extraction Reuse Retention Purge

## Purpose
Define when extracted artifacts may be reused, retained, or purged, while protecting canonical content and active bindings.

## Prerequisites
- segmentation views and native artifact retention are defined.

## Allowed production modules
- `src/cognityx_ingest/cleanup.py`
- `src/cognityx_ingest/service.py`
- `src/cognityx_ingest/source_assets.py`

## Prohibited scope
- SDK changes
- destructive physical cleanup changes outside the established boundary

## Tests to make pass
- reuse and retention boundary tests.

## Backward compatibility requirements
- current source-asset and cleanup commands remain stable

## Validation commands
- `uv run pytest`
- `uv run mkdocs build --strict`
- `uv build`

## One-PR stop condition
Stop once purge boundaries are explicit and tested in one PR.
