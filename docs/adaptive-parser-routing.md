# Adaptive Parser Routing

## Background And Purpose

A document can be read by more than one parser. One parser may understand the
document hierarchy well, while another may preserve native PDF links and page
labels. Cognityx therefore needs a safe way to decide which parser observations
to request before any parser runs.

This decision is called adaptive parser routing. It produces a bounded list of
parser invocations called a routing plan. It does not parse the document and it
does not combine parser results.

## Where Routing Fits

```text
T03 capability registry
what is registered, available, documented, guided, and measured
                         |
                         v
T04 adaptive routing
which bounded parser invocations should be requested
                         |
                         v
existing ParserRouter
executes fixed, rule, fallback, compare, or agent policies
                         |
                         v
T05 alignment, fusion, and adjudication
compares observations and keeps agreement, conflict, and uncertainty
```

T03 supplies evidence. T04 makes and validates a plan. Existing parser adapters
perform execution. T05 will later decide how observations relate. Keeping those
stages separate prevents a routing recommendation from becoming an accepted
source fact.

## Exactly Three Modes

The order and names are contractual:

1. `deterministic`
2. `hybrid`
3. `llm-directed`

### Deterministic

Explicit typed rules inspect bounded input facts and the live capability
registry. No proposal provider or large language model (LLM) is called. If an
eligible parser cannot satisfy a requirement, the plan is rejected and the
requirement remains unresolved. There is no hidden model fallback.

### Hybrid

A trusted caller supplies a hard boundary first. A proposal provider can suggest
parsers only inside that boundary. Deterministic code then checks the complete
proposal. The provider cannot expand the allowlist, increase the parser-run
budget, permit an external service, change security tags, or rewrite registry
facts.

### LLM-Directed

A provider may propose parser IDs, order, scopes, purposes, and the approved stop
condition. The model is a proposal provider, not an authority. Deterministic
validation still checks every identity, runtime observation, capability,
allowlist, budget, scope, security constraint, and registry version.

No provider is configured by default. Calling hybrid or LLM-directed routing
without one raises a typed proposal error.

## Public Records

### RoutingInputFacts

`RoutingInputFacts` contains only facts needed for planning:

- media type;
- optional native-text ratio from zero to one;
- explicitly required capabilities;
- optional page count and source size;
- optional bounded document class.

It has no source-text, source-byte, parser-native-payload, or local-path field.
For example, a request may say that an `application/pdf` needs hierarchy,
tables, and native links. It cannot contain the PDF text itself.

### RoutingBoundary

`RoutingBoundary` is the hard deterministic policy:

- parser allowlist;
- maximum parser runs;
- whether external services are allowed;
- allowed invocation scopes;
- optional required security tags.

A proposal provider receives this record but cannot modify it.

### ParserInvocation

`ParserInvocation` identifies one parser, an explicit scope, a purpose, and
optional security tags. T04 supports these initial scopes:

- `document`;
- `pages-with-native-links`.

The second scope can describe a future page-targeted invocation. It does not
cause an existing document parser to run on selected pages. The legacy adapter
rejects that scope rather than silently widening it to the whole document.

A purpose says why that exact parser would run. For example, Docling may carry
`hierarchy` and `tables`, while PyMuPDF may carry `native_links`, `page_labels`,
and `geometry`. T04 checks each purpose against that parser's own live registry
record. PyMuPDF support elsewhere in a plan cannot justify a `native_links`
purpose incorrectly attached to Docling. In short, the purpose and supporting
capability must belong to the same parser.

### RoutingProposal

`RoutingProposal` contains untrusted proposed invocations and optional reason,
stop condition, provider, model, request ID, external-service observation, and
security tags. Provider and model identifiers are audit facts only. They do not
make a proposal valid.

### RoutingValidationResult

`RoutingValidationResult` keeps separate checks for:

- allowlist;
- parser-run budget;
- security;
- schema and scope;
- registry identity;
- runtime availability;
- capability eligibility.

Rejected plans retain deterministic reason identifiers. No source content is
included in those reasons.

### RoutingPlan

`RoutingPlan` is the immutable output. A candidate invocation is work that a
rule or provider proposed. A selected invocation is work that passed the whole
deterministic decision and may be considered by later execution orchestration.
Rejected plans always have an empty selected list. Deterministic plans can retain
their candidates under `candidate_invocations`; hybrid and LLM-directed plans
retain candidates in the untrusted proposal.

The plan also exposes the routing schema, mode, validation result, registry
version, exact registry SHA-256 when present, and whether a provider was used. It
has strict dictionary and JSON readers plus deterministic serializers.

The schema is:

```text
cognityx.ingest.routing-plan/v3.2
```

The three frozen fixture shapes remain supported without rewriting their files.
They form an exact compact compatibility boundary for older minimal records.
New service-built records use an exact complete canonical shape. The canonical
shape retains input facts, the deterministic boundary, registry version, full
validation result, and mode-appropriate rule or proposal evidence. A partially
extended record is rejected instead of silently losing some decision context.

Canonical plans also store `registry_sha256`, calculated over the exact
deterministic bytes returned by `ParserCapabilityRegistry.to_json_bytes()`. The
registry version is a readable release label. The digest is the identity of the
exact runtime evidence snapshot used for the decision. Audit tools and future
execution orchestration consume both: facts explain what was needed, the boundary
explains what was permitted, validation explains the outcome, and the digest
identifies the evidence. The complete registry is not copied into the plan.

Readers reject duplicate JSON keys, unknown or missing fields, malformed values,
unsupported combinations, and noncanonical set-like order. They preserve
supplied order and do not repair malformed persisted plans.

## Deterministic Rule Algorithm

Deterministic rules are immutable records rather than one large conditional
function. Each rule declares:

- a stable rule ID;
- explicit requirement triggers;
- parser ID and scope;
- invocation purpose;
- media types;
- an optional minimum native-text ratio.

The reviewed initial flow is:

1. Validate input facts, boundary, registry, and rules.
2. Evaluate rules in fixed order.
3. Require each matched parser to be in the allowlist and live registry.
4. Require runtime availability to be exactly true.
5. Verify every candidate purpose against that candidate parser's own assertions.
6. Produce rule IDs and candidate invocations in evaluation order.
7. Run the shared deterministic validator over the complete candidate set.
8. Promote candidates to selected only when every check passes.
9. On rejection, expose no selected run and retain bounded candidate evidence.
10. Reject unresolved requirements without calling a provider.

For a structured native PDF, the initial policy can prefer Docling for hierarchy
and tables and supplement it with PyMuPDF for native links. A native-link-only
request can select PyMuPDF independently. If the live registry reports Docling
unavailable, Docling is not selected even when official documentation advertises
its structural features.

## Hybrid Algorithm

Hybrid routing follows this sequence:

1. Validate the trusted hard boundary.
2. Give immutable input facts, registry evidence, and boundary to one provider.
3. Treat every returned field as untrusted.
4. Require parser order to follow the deterministic boundary.
5. Check allowlist, run budget, service use, security tags, scopes, registry,
   runtime, and each parser's own declared purposes.
6. Select invocations only when every check passes.
7. Return an accepted or rejected auditable plan.

Malformed provider records are quarantined from the persisted rejected proposal.
The validation result still records why the proposal failed. T04 never repairs
that proposal into an accepted plan.

## LLM-Directed Algorithm

LLM-directed routing binds planning to the validated registry version and calls
one provider. Provider invocation order is preserved as an audit decision, but
every invocation remains subject to deterministic checks. Invented parser IDs,
unsupported scopes, invalid stop conditions, unavailable adapters, excess runs,
or security violations produce a rejected plan with no executable selections.

The one initial stop condition is:

```text
all-required-capabilities-observed-or-explicitly-unresolved
```

T04 validates and records that text. It does not inspect parser outputs or decide
whether the condition has been reached. That belongs to later orchestration.

## Capability Eligibility

Routing requirements use an explicit vocabulary. For example, `hierarchy` maps
to the registry capability `document_hierarchy`, while `geometry` maps to
`bounding_boxes`. This mapping is routing policy, not a fourth capability source.

A parser satisfies a requirement only when:

1. its runtime availability is exactly true; and
2. its assertion status is `available`, `declared`, or
   `declared-when-available`.

For a live request, capability support is not enough by itself. At least one
invocation must name the requirement in its purpose, and that invocation's own
parser must satisfy the mapped capability. A nonempty request cannot be accepted
with only empty-purpose invocations. Extra complementary purposes are retained
only when their parser genuinely supports them.

The statuses `unsupported`, `not-declared`, `unavailable`, and `unknown` do not
satisfy a requirement. Human guidance and measured evidence remain visible to
providers and auditors, but T04 does not parse their prose into hard rules.

## Runtime Conflicts

A conflict such as `declared-but-currently-unavailable` remains part of the T03
registry. T04 neither deletes nor resolves it. False runtime availability blocks
executable selection even when documentation advertises the capability.

## Security And Budget Checks

Every proposal is checked against:

- exact parser allowlist membership;
- positive maximum run count;
- approved scopes;
- external-service permission;
- required security tags;
- duplicate invocations;
- bounded provider metadata;
- approved stop-condition vocabulary.

Routing records store no API key, credential, local path, source text,
parser-native bytes, prompt containing document content, vector, or embedding.

## Legacy Policy Compatibility

Existing execution policy names do not change:

| Existing policy | Explanatory adaptive mode |
| --- | --- |
| `fixed` | `deterministic` |
| `rule` | `deterministic` |
| `fallback` | `deterministic` |
| `compare` | `deterministic` |
| `agent` | `hybrid` |

There is no legacy alias for `llm-directed`, and
`ExtractionPolicy(mode="deterministic")` remains invalid.

`RoutingPlan.to_extraction_policy()` is intentionally narrow. One accepted,
purpose-free document invocation can map to `fixed`; several can map to the
existing `compare` policy. Rejected plans, page scopes, stop conditions,
purposes, or security tags raise `ParserRoutingCompatibilityError`. The adapter
does not execute or fuse parsers.

## Normal CI Boundary

Normal routing tests use deterministic frozen providers. They require no OpenAI,
Groq, local inference server, parser model, live internet, network socket, or
optional large parser dependency. Tests fail if planning reaches parser execution
or existing fusion code.

## T04 Non-Goals And T05 Handoff

T04 ends with a validated invocation plan. It does not align parser observations,
combine pages or blocks, decide which conflicting fact wins, or create a fused
canonical document. T05 will own alignment, fusion, and adjudication after this
task is merged. Segmentation, retention, Source Graph, provenance addresses,
DataForge outputs, SDK/CLI changes, and later T06-T10 behavior are also outside
this increment.
