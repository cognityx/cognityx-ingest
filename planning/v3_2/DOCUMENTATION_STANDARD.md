# v3.2 Documentation Standard

## Why This Standard Exists

Code is easier to maintain when a reader can understand why it exists before
studying its syntax. Every v3.2 task therefore documents the problem, the main
algorithm, the ownership boundary, and the people or programs that consume the
result. A list of fields or a restatement of a function signature is not enough.

This standard begins with T01 and applies to T02-T10.

## Module Documentation

Every new architectural module explains these sections explicitly:

- **Purpose**: the problem this module solves;
- **Design principles**: the rules that shape its implementation;
- **Processing flow**: the main algorithm or sequence;
- **Primary consumers**: the code, services, operators, or users that read its
  output;
- **Ownership boundary**: what this repository owns and what belongs to another
  component;
- **Non-goals**: nearby work intentionally left outside the module.

For native parser artifacts, the module also explains why original parser bytes
are separate from the stable Cognityx document. `IngestService` writes them.
Future T02 NativeBinding records, T07 retention behavior, audit tools, and future
SDK read APIs consume their descriptors. DataForge normally reads canonical and
provenance records, not raw parser payloads. The native-artifact store does not
parse, route, or fuse documents.

## Class Documentation

Every public class documents:

- its responsibility;
- who constructs it;
- who uses it;
- invariants that must always hold;
- lifecycle and persistence behavior;
- thread-safety assumptions when concurrency is relevant.

An invariant is a rule that remains true throughout the object's life. For
example, a native descriptor's SHA-256 must always identify its exact payload
bytes.

## Public Method And Function Documentation

Every non-trivial public method or function documents:

- who calls it and in what context;
- the algorithm or major steps;
- arguments and return value;
- side effects;
- idempotency and immutability behavior;
- typed failures;
- security or trust boundaries when untrusted data is read.

Idempotent means that repeating the same request has the same safe result. For
example, storing the same artifact twice returns the existing descriptor rather
than writing a second payload.

## Private Algorithm Documentation

A private helper needs a concise docstring when it implements hashing,
immutable-write checks, pointer resolution, descriptor-key construction, or
integrity verification. The docstring explains the reason or invariant rather
than narrating obvious syntax.

## Touched Existing Code

When a task changes an existing persistence or composition method, improve its
docstring enough to explain:

- why it calls the new component;
- which old outputs must remain compatible;
- which later modules depend on the result.

## Automated Presence Checks

Documentation tests may use Python's `ast` or `inspect` modules to verify that
required docstrings and concepts are present. These tests enforce objective
presence and structure. Reviewers still evaluate whether the prose is accurate,
plain, and useful.
