# T02 Generalized Canonical Model

## Purpose
Introduce the richer canonical overlay that can represent resources, presentation units, divisions, content nodes, representations, selectors, native bindings, relations, activities, and artifact descriptors.

## Prerequisites
- T01 behavior is settled.
- The parser-native artifact boundary is stable.

## Allowed production modules
- `src/cognityx_ingest/models.py`
- `src/cognityx_ingest/service.py`
- `src/cognityx_ingest/structure.py`
- `src/cognityx_ingest/references.py`

## Prohibited scope
- CLI redesign
- SDK changes
- unrelated Storage or Jobs rewrites

## Tests to make pass
- strict tests for canonical node ownership, spans, and selector binding.

## Backward compatibility requirements
- existing document, section, object, and provenance outputs remain readable

## Validation commands
- `uv run pytest`
- `uv run mkdocs build --strict`
- `uv build`

## One-PR stop condition
Stop after the generalized model is introduced and validated in one PR.
