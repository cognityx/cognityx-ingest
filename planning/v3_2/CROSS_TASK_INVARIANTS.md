# Cross Task Invariants

1. Existing SourceAsset, Storage, Jobs, parser, table, object, reference, cleanup, and v1/v2 compatibility behavior is preserved unless a test proves a required change.
2. The existing normal `cogni` commands and Python ingest methods remain supported.
3. Source bytes and parser-native artifacts are immutable while retained.
4. Canonical text is stored once; divisions, provenance, views, KUs and training records reference IDs/spans.
5. Native parser richness is never discarded merely to fit the common abstraction.
6. Canonical field sources retain parser/model, version, method, confidence and conflicts.
7. Observed, deterministic, parser-inferred, model-inferred, human-validated, ambiguous, contradicted and unresolved states remain distinguishable.
8. Parser routing and parser fusion are separate stages.
9. The capability registry has exactly three source classes.
10. Adaptive routing has exactly three higher-level modes.
11. Segmentation views never become authoritative copied content.
12. Raw parser purge cannot delete canonical content, source selectors, compact lineage or active bindings.
13. Normal CI does not require live internet or optional large parser models.
14. Frozen source hashes and expected records cannot be silently changed to make tests pass.
15. Strict xfail calls the intended production seam and represents genuine missing behaviour.
