# Source Graph and Provenance Addresses

## Background and purpose

An ingested document is more than a bag of text. It has a source file, pages or
slides, logical sections, paragraphs, exact locations, and links to other parts
of the same document or another document. Cognityx Ingest keeps this connected
map as the **Source Graph**.

The Source Graph answers ordinary questions such as which source owns a
paragraph, which section directly owns it, whether a relation is accepted or
ambiguous, and which frozen source and graph revision support an answer.

It sits after parser-neutral canonical content and before DataForge:

```text
source file
  -> parser-native artifact
  -> canonical content (the only durable owner of source text)
  -> Source Graph and provenance addresses
  -> DataForge paragraph Q/A and composite Knowledge Units
  -> future SDK and CLI readers
```

## Source Graph versus a knowledge graph

The Source Graph records source structure and evidence that Ingest can prove. A
later graph may describe people, organizations, claims, topics, or communities.
That later form is a **semantic Knowledge Graph**, meaning a graph of interpreted
meaning.

T08 does not build that semantic graph. It does not extract entities or claims,
rank paths, create embeddings, or run GraphRAG. A future semantic or retrieval
graph is a replaceable projection. Its `GraphProjectionDescriptor` records which
Source Graph revision and support IDs it came from, but the projection does not
become source truth.

The Source Graph is deterministic because it reuses validated canonical IDs and
explicit relations. The same facts produce the same SHA-256-based graph revision.
It uses immutable JSON records and small in-memory scans, so it does not need a
graph database.

## What the graph contains

### Resources

A resource identifies one immutable source by `resource_id` and SHA-256. A source
hash is a fingerprint of its exact bytes. Business family and version are stored
only when an authoritative caller supplies them. Ingest never guesses a policy
version from a filename, date, lexical order, or model knowledge.

### Presentation units

A presentation unit says where content appeared, such as a page, slide, sheet,
or whole-document surface. It does not decide where a logical section ends.

### Divisions and nodes

A division is a logical container such as a document, section, clause, or
appendix. Each node belongs directly to one deepest division. Parent divisions
refer to child divisions; they do not copy child text. The graph stores node IDs,
ownership, and selector IDs, but canonical content remains the only durable owner
of the text itself.

### Selectors and bindings

A selector describes an exact location, for example a character range or page
rectangle. A `source_path` is a logical relative locator, not a local operating
system path to open. A native binding points from a canonical ID to an opaque
location in a retained parser artifact. The binding metadata can survive after
the parser payload is purged, although the old native pointer cannot then open
the deleted payload.

### Representation lineage is acyclic

A representation can describe a view of another representation. For example, a
cropped figure can point to the full-page image that it came from, and that image
can point to its canonical page or section. This chain is representation
lineage: a trace of which source object each later view represents.

The chain must always finish at a supported canonical subject. It cannot point
back to itself or form a loop through several records. Ingest checks every chain
with a bounded visited set while validating the Source Graph. A cycle in
persisted JSON therefore fails with a typed graph error before traversal or
address resolution, rather than eventually failing with Python recursion.

### Explicit relations

A relation keeps its source, optional accepted target, type, status, epistemic
state, and gold eligibility. **Epistemic state** means what we know about a fact,
for example deterministically derived, human validated, ambiguous, contradicted,
or unresolved.

Default gold traversal follows only concrete supported relations. Ambiguous,
unresolved, contradicted, rejected, or explicitly non-gold relations remain
visible for audit but cannot support a gold answer. Cross-resource relations are
valid when their explicit targets exist.

## Graph revisions

A production graph revision is a fingerprint of the graph facts, not a label
chosen by a caller. `SourceGraphBuilder` first validates the graph facts, then
serializes every persisted fact except `graph_revision` through the same
deterministic JSON projection used for persistence. It calculates SHA-256 over
those compact UTF-8 bytes and stores `sg-` followed by exactly 64 lowercase
hexadecimal characters.

Public validation does not merely check that this field is present. Direct
Python callers and strict JSON readers repeat the graph-fact checks, calculate
the expected fingerprint again, and require exact equality. They do not repair,
normalize, or accept a caller-selected value. Changing a resource hash or
family/version, presentation unit, hierarchy, direct node owner, selector,
representation, native binding, processing activity, artifact descriptor, or
explicit relation invalidates the old revision. Changing an ambiguous
relation's candidates or gold-safety facts also invalidates it. Equivalent
builder input order still produces the same deterministic graph and revision.

For example, copying a valid graph and changing one relation from `references`
to `defines` while retaining its old revision creates contradictory evidence.
`SourceGraph.validate()` and `SourceGraph.from_json_bytes(...)` reject that graph
with a typed revision error before traversal, repository registration, or
address resolution.

The frozen compact fixture is an explicit compatibility exception. It uses
`sg-rev-001`, which predates production content fingerprints, and must remain
byte-for-byte unchanged. Only graphs loaded with `compact_fixture=True` use that
frozen revision rule. A complete production graph cannot use `sg-rev-001`,
`pending`, a UUID, a timestamp, uppercase hexadecimal text, or a random digest.

A repository never treats "latest" as a substitute for a requested revision.
It validates a production graph's content fingerprint before registration, so
two different production graph contents cannot enter the repository under one
revision. Registering different compact fixture content under the same frozen
revision remains a conflict as before.

## Provenance addresses

A provenance address is a structured pointer to evidence. It stores IDs and
locations, not a second copy of source text.

### Strong address

A strong address identifies one immutable source hash, graph revision, resource,
canonical target, and selector set. It is suitable for audit and exact support.

```json
{
  "address_id": "addr-strong-pol-p2",
  "source_sha256": "08ce21d34cc5efbbda589676c3d4b0fdcdcba0162c9afb80cef35bf09f3e4862",
  "graph_revision": "sg-rev-001",
  "resource_id": "res-policy-v2",
  "canonical_target": {"node_id": "pol-p2"},
  "selectors": [{
    "selector_type": "text-position",
    "source_path": "sources/segmentation_policy.md",
    "char_start": 131,
    "char_end": 192
  }]
}
```

A strong address never redirects to a newer policy. A mismatched or explicitly
superseded immutable context is obsolete.

The graph revision inside a production strong address now rests on two proofs.
The builder calculated it from all graph facts, and every public graph consumer
recalculates it before the resolver can run. Changing graph facts and changing
strong addresses to the same forged revision therefore cannot create exact
support. The graph itself fails validation first. Exact resolution still also
requires the source SHA-256, resource, canonical target, and selectors to match.

In a complete production graph, each supplied selector must be provable from a
selector record belonging to the addressed resource. If the address supplies a
`selector_id`, both the ID and every locator fact must match. If it omits the ID,
the complete remaining facts, such as logical path, character range, page,
rectangle, and source-anchor IDs, must exactly match one graph selector. Ingest
does not invent an ID, rewrite the address, or open source or parser bytes to make
a near match succeed.

The compact frozen fixture predates complete production selector records. Its
selectors are a compatibility form: Ingest validates their safe structure, but
does not treat them as production trust evidence. This deliberate exception
keeps the frozen examples byte-identical. A strong address with no selectors can
still be exact when its source hash, graph revision, resource, and target all
match.

### Logical address

A logical address is a stable business request, not immutable evidence. It names
a resource family, a version rule, and a division reference. Resolution can use
only explicit family/version metadata and an injected deterministic policy.

```json
{
  "address_id": "addr-logical-policy-4.2-effective",
  "resource_family_id": "aster-vale-travel-policy",
  "version_rule": "effective-at-query-time",
  "division_reference": "4.2"
}
```

One proven candidate is redirected to a concrete target. Multiple equally valid
candidates are ambiguous. No candidate is unresolved. Version strings are never
sorted to guess which policy is newest.

### Evidence-set address

An evidence-set address preserves ordered claim IDs and ordered strong-address
members for composite support.

```json
{
  "address_id": "addr-evidence-ku-travel-approval",
  "claim_ids": ["claim-rule", "claim-exception", "claim-authority"],
  "member_address_ids": [
    "addr-strong-pol-p2",
    "addr-strong-pol-p5",
    "addr-strong-auth-21"
  ]
}
```

The set is exact only when every required member resolves exactly. A missing,
obsolete, ambiguous, or forbidden member is never replaced with different
evidence.

## Resolution outcomes

The resolver returns exactly six outcomes:

- `exact`: immutable strong evidence, or every evidence-set member, matched.
- `redirected`: a logical address selected one permitted explicit version.
- `ambiguous`: several valid logical candidates remain and policy cannot choose.
- `obsolete`: the requested revision, resource, version, or address is explicitly
  superseded in this resolution context.
- `forbidden`: the target exists, but the injected access policy denied it.
- `unresolved`: the address, family, target, selector, or required member cannot
  be resolved.

No failure becomes fabricated evidence. A forbidden result contains no protected
target IDs, selectors, or candidate details.

### Result shapes are closed

An `AddressResolution` cannot mix fields that tell conflicting stories. A
one-address `exact` result has one target. An evidence-set `exact` result has an
ordered target collection and matching ordered exact member results. A
`redirected` result has exactly one target, while an `ambiguous` result has no
accepted target and may list candidates. `obsolete` and `unresolved` results have
no target or candidate collection, although they may retain safe member results
to explain an incomplete evidence set. A `forbidden` result has no target,
candidate, or member detail at all.

Validation applies these rules recursively to member results, rejects duplicate
target identities, and bounds the graph revision and explanation text. This
closed status shape prevents a future consumer from accidentally treating a
candidate or a partially resolved member as accepted support. The no-leak rule
for `forbidden` remains strongest: even an explanatory member result is removed.

## Parser-payload purge independence

Resolution reads the canonical Source Graph, address catalog, hashes, selectors,
and binding metadata. It does not load parser-native payload bytes. T07 can purge
an eligible raw parser artifact while canonical strong addresses continue to
resolve. Storage and retention policy still govern the surviving artifacts.

## Production behavior and limitations

Normal Ingest persistence adds `source-graph.json` and
`provenance-addresses.json`. The generated catalog contains strong node addresses
because current canonical facts are sufficient for them. Ingest does not generate
logical addresses without explicit family/version metadata, and it does not
generate evidence sets without explicit claim/member intent.

T09 will consume these records for paragraph Q/A and cross-section or
cross-document Knowledge Unit handoff. T10 may add SDK and CLI read surfaces.
T08 does not change current CLI commands, the Python composition root, or the
meaning of the existing `provenance.json` artifact.

T09's consumer trust boundary begins only after Source Graph validation and
address-result validation succeed. DataForge may trust an `exact` ordered closure
as support, but it must never promote an `ambiguous`, `obsolete`, `forbidden`, or
`unresolved` explanation to gold evidence. T08 supplies that validated evidence
boundary. Because strict validation re-proves the production content fingerprint,
T09 inherits a trustworthy graph revision rather than a human-selected label.
T08 still does not implement the T09 handoff itself, and T10 remains responsible
for any future SDK or CLI read surfaces.
