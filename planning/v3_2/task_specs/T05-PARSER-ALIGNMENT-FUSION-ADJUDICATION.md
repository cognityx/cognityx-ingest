# T05 Parser Alignment, Fusion, and Adjudication

## Purpose

Keep each parser's observations visible, align observations through real source
location evidence, classify agreement and disagreement, and apply explicit
fact-specific policies. The result is one backward-compatible
`ExtractionResult` plus an additive auditable v3.2 decision artifact.

T04 decides which parsers should run. Existing parser adapters execute them.
T05 begins only after those parsers return and performs no routing, parser,
network, provider, or LLM call.

## Prerequisites

- T01 native parser artifacts and descriptors are merged and unchanged.
- T02 parser-neutral canonical content is merged and remains readable.
- T03 capability evidence is merged and remains the routing source of truth.
- T04 routing is merged and remains separate from parser execution and fusion.
- `ParserRouter(mode="compare")` and `ExtractionResult` remain the compatibility seams.

## Exact Production Modules Allowed to Change

- `src/cognityx_ingest/parser_fusion.py`: new immutable T05 records, validation,
  deterministic alignment, fact fusion, policies, adjudication, artifact, and service.
- `src/cognityx_ingest/parser.py`: optional `ExtractionResult.fusion_artifact`,
  preservation through result copying, thin `_fuse_results()` delegation, and the
  isolated legacy compatibility projection.
- `src/cognityx_ingest/service.py`: additive document-local fusion artifact,
  manifest/provenance references, artifact handles, and output-byte accounting.
- `src/cognityx_ingest/canonical_content.py`: optional typed T05 fact-source
  references only; existing T02 records and empty serialization remain compatible.
- `src/cognityx_ingest/models.py`: optional `IngestResult.fusion_artifact_key` only.
- `src/cognityx_ingest/__init__.py`: deliberate T05 public exports only.

No other production module is authorized.

## Public Records and Service

- `ObservationValue`
- `ObservationSourceRegion`
- `ParserObservation`
- `ParserObservationSet`
- `AlignmentEvidence`
- `AlignedObservationGroup`
- `FactAdjudicationPolicy`
- `FactFusionDecision`
- `RegionFusionDecision`
- `ParserFusionArtifact`
- `FusionOutcome`
- `ParserFusionService`
- bounded typed T05 errors

The four exact states are `agreement`, `complementary`, `conflict`, and
`unresolved`.

## Main Algorithm

1. Sort completed parser results by stable parser identity.
2. Adapt page, block, object, relation, and section facts into exact observations.
3. Align by explicit region, anchor, selector, span, unique mutual-best geometry,
   or exact text digest and occurrence in that priority order.
4. Keep ambiguous candidates rather than choosing a nearest result.
5. Group aligned observations by source region and fact.
6. Compare canonical typed value bytes and classify the four states.
7. Apply bounded fact-specific policy without global backend precedence.
8. Summarize regions with unresolved before conflict before complementary before agreement.
9. Serialize deterministic v3.2 decision bytes that reference observation IDs.
10. Create the existing one-value extraction projection without changing the
    authoritative T05 state.

## Prohibited Scope

- No T06 `SegmentationView` API or materialization.
- No T07 extraction reuse, retention, legal hold, or purge policy.
- No T08 Source Graph repository, graph database, or provenance-address resolver.
- No T09 DataForge paragraph Q/A or composite Knowledge Unit generation.
- No T10 SDK or CLI changes.
- No `cognityx-sdk` modification.
- No parser execution from `parser_fusion.py`.
- No capability discovery or routing-plan creation.
- No proposal provider, network, inference dependency, or LLM adjudication.
- No embeddings, semantic similarity, vector index, tokenizer, or fuzzy matching.
- No parser-private Docling classes or duplicate native-artifact store.
- No copied source text in source-region or decision records.
- No confidence-only winner and no averaged bounding boxes.

## Exact Tests to Make Pass

- `tests/v3_2/test_v3_2_fusion_adjudication.py`
- `tests/v3_2/test_v3_2_parser_observations.py`
- `tests/v3_2/test_v3_2_observation_alignment.py`
- `tests/v3_2/test_v3_2_fact_fusion.py`
- `tests/v3_2/test_v3_2_adjudication_policies.py`
- `tests/v3_2/test_v3_2_fusion_persistence.py`
- `tests/v3_2/test_v3_2_fusion_compatibility.py`
- `tests/v3_2/test_v3_2_fusion_validation.py`
- `tests/v3_2/test_v3_2_canonical_contract.py`
- `tests/v3_2/test_v3_2_native_parser_preservation.py`
- `tests/v3_2/test_v3_2_routing_and_compatibility.py`
- `tests/v3_2/test_v3_2_documentation_contract.py`

The seven frozen fixture cases must produce their exact state, accepted facts,
resolution, required action, and retained observations. The frozen ambiguous
relation remains non-gold through the production adjudication seam.

## Backward-Compatibility Requirements

- `fixed`, `rule`, `fallback`, `compare`, and `agent` remain accepted policies.
- Existing parser IDs, raw paths, raw parser keys, and native descriptors remain stable.
- Existing `cognityx.ingest.parser-fusion/v1` bytes remain readable.
- `cognityx.ingest.parser-fusion/v3.2` is separate and additive.
- Existing `ExtractionResult` constructors remain valid through a default field.
- `_with_selection()` preserves optional fusion bytes.
- Single-parser fixed, rule, and fallback results remain unchanged.
- Compare mode remains deterministic and independent of parser execution order.
- Canonical-content fixtures and records without T05 metadata serialize unchanged.
- Existing Python composition methods and CLI workflows remain unchanged.
- Conflicting, ambiguous, unresolved, or rejected evidence is never silently gold.

## Persistence Contract

- Key: `ingest/documents/<document-id>/parser/fusion-decisions.json`
- Media type: `application/json`
- Schema: `cognityx.ingest.parser-fusion/v3.2`
- Artifact ID: `art-<document-id>-parser_fusion_decisions`
- Storage route: direct immutable Cognityx processing artifact, not `NativeArtifactStore`
- Cleanup: existing recursive document-prefix deletion
- Future retention owner: T07

## Documentation Requirements

- `docs/parser-alignment-fusion-adjudication.md`
- MkDocs navigation and API reference
- focused contract update only where the additive artifact changes current behavior
- architectural module, every public class, public method, and material private
  algorithm documented according to `DOCUMENTATION_STANDARD.md`

## Validation Commands

```bash
uv sync --extra dev
uv run pytest -q \
  tests/v3_2/test_v3_2_fusion_adjudication.py \
  tests/v3_2/test_v3_2_parser_observations.py \
  tests/v3_2/test_v3_2_observation_alignment.py \
  tests/v3_2/test_v3_2_fact_fusion.py \
  tests/v3_2/test_v3_2_adjudication_policies.py \
  tests/v3_2/test_v3_2_fusion_persistence.py \
  tests/v3_2/test_v3_2_fusion_compatibility.py \
  tests/v3_2/test_v3_2_fusion_validation.py \
  tests/v3_2/test_v3_2_canonical_contract.py \
  tests/v3_2/test_v3_2_native_parser_preservation.py \
  tests/v3_2/test_v3_2_routing_and_compatibility.py \
  tests/v3_2/test_v3_2_documentation_contract.py
uv run pytest -q tests/v3_2
uv run pytest
uv run python tests/fixtures/v3_2_focused/verify_fixture_pack.py --repo-root .
uv run mkdocs build --strict
uv build
git diff --check
```

## One-PR Stop Condition

Open one draft `cognityx-ingest` PR containing only T05 production code, tests,
planning, and documentation. Stop after the draft PR and required CI inspection.
Do not implement T06 or modify `cognityx-sdk`.
