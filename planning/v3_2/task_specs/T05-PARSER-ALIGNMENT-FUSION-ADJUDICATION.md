# T05 Parser Alignment Fusion Adjudication

## Purpose
Align observations from multiple parsers, preserve complementary facts, and adjudicate conflicts without changing the accepted canonical result.

## Prerequisites
- T03 and T04 are in place.

## Allowed production modules
- `src/cognityx_ingest/parser.py`
- `src/cognityx_ingest/service.py`
- `src/cognityx_ingest/structure.py`

## Prohibited scope
- segmentation view redesign
- DataForge export changes
- SDK changes

## Tests to make pass
- multi-parser fusion, conflict, and adjudication tests.

## Backward compatibility requirements
- existing single-parser results remain unchanged

## Validation commands
- `uv run pytest`
- `uv run mkdocs build --strict`
- `uv build`

## One-PR stop condition
Stop once fusion behavior is merged and stable in one PR.
