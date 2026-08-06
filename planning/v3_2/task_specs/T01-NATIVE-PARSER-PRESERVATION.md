# T01 Native Parser Preservation

## Purpose
Preserve the full parser-native result as a retained artifact instead of flattening it into the shared canonical layer.

## Prerequisites
- T00 fixture scaffold is present.
- Existing PDF ingestion and storage behavior remain green.

## Allowed production modules
- `src/cognityx_ingest/parser.py`
- `src/cognityx_ingest/service.py`
- parser-native persistence helpers in `src/cognityx_ingest/*` if needed

## Prohibited scope
- `cognityx-sdk`
- unrelated source-asset lifecycle behavior
- broad canonical model redesign

## Tests to make pass
- strict tests that call the native-artifact store/read/reload seam and verify byte/hash preservation plus valid native pointers

## Backward compatibility requirements
- current `cogni ingest` workflows keep working
- current Python ingest composition remains valid

## Validation commands
- `uv run pytest`
- `uv run mkdocs build --strict`
- `uv build`

## One-PR stop condition
Stop once the native-artifact preservation feature is merged behind one PR and the existing CLI remains stable.
