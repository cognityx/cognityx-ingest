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

The frozen fixture uses `sg-rev-001`. Production revisions use `sg-` followed by
a SHA-256 over all persisted graph facts except the revision field itself. Input
order does not change the revision. A changed resource, hierarchy, node binding,
selector, activity, artifact reference, or relation does change it.

A repository never treats "latest" as a substitute for a requested revision.
Registering different content under the same revision is a conflict.

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
