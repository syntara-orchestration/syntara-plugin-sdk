# ADR 0005: Use one shared canonical hash for generic container requests

- Status: Accepted
- Date: 2026-10-07

## Context

Syntara's control plane composes a generic `ContainerWorkloadRequest` from an
immutable installed action, evaluated input, and server-owned execution-policy
references. The Execution Plane must reject a request that changes after that
composition and before it is persisted or launched. A local implementation in
either service would let JSON serialization differences silently weaken that
check, especially once non-Python SDKs or adapters exist.

## Decision

The shared `syntara-plugin-contracts` API defines the reference request hash.
It serializes the complete request as UTF-8 JSON with sorted object keys,
compact separators, and no non-finite numbers, then returns a `sha256:`
digest. Before hashing, it removes only the mirrored `requestHash` fields from
the work item and policy snapshot. Removing those two fields avoids a circular
representation; every other request value is committed by the digest.

Syntara uses this helper while composing a credentialless work item. The
Execution Plane independently recomputes the same digest before it accepts a
container payload, and requires both mirrored fields to equal it. Future
language SDKs implement this exact rule and prove compatibility with the shared
conformance cases before composing or verifying a generic request.

## Consequences

- A change to an input, action route, runtime context, capability, policy
  snapshot, timeout, or integration handle invalidates the request hash.
- The hash is an integrity check for trusted component handoff, not a
  credential, signature, authorization decision, or replacement for mTLS.
- The rule belongs in the contracts package rather than the SDK authoring API,
  Syntara backend, or Execution Plane so those components cannot drift.
- Any semantic change to the preimage or canonical JSON representation requires
  a new versioned runtime contract and cross-language conformance coverage.

## Amendment: RFC 8785 canonicalization (AAP-102213)

The shared canonical JSON form was tightened from "UTF-8 JSON with sorted
object keys and compact separators" to strict
[RFC 8785 (JSON Canonicalization Scheme)](https://www.rfc-editor.org/rfc/rfc8785):
UTF-16 code-unit key ordering, ECMAScript `Number::toString` serialization, and
minimal string escaping. This makes the form normative by reference instead of
by description, so non-Python SDKs can prove byte compatibility against the
RFC's published test vectors. The preimage rule (remove only the two mirrored
`requestHash` fields) is unchanged. As the SDK is pre-release, the resulting
digest values changed forward-only; every consumer recomputes them from the
same contracts package rather than pinning them.

### Exception to the versioning rule

The consequence above requires a new versioned runtime contract for any
semantic change to the canonical JSON representation. This change takes a
documented exception to that rule, and only because every Syntara runtime
contract is still pre-release (`v1alpha1`), no released consumer pins a digest
across package versions, and both the composing and verifying sides recompute
from the same `syntara-plugin-contracts` package rather than persisting an
independent serialization.

Consumers coordinate the upgrade as follows:

- The request composer and the Execution Plane verifier upgrade
  `syntara-plugin-contracts` in the same change train. This repository's
  Execution Plane compatibility check gates that upgrade: it recomputes
  request hashes with the candidate package and fails on any divergence.
- A digest computed by the previous canonicalization is never accepted
  silently. Verification is fail-closed: a mismatched `requestHash` rejects
  the request, and the caller recomposes it under the current package.
- Once any runtime contract reaches a stable (non-alpha) version, this
  exception lapses: every later semantic change to the canonical form
  requires a new versioned runtime contract and cross-language conformance
  coverage, with no forward-only digest migration.

### Cross-language conformance coverage

The rule is provable across languages against
`contracts/fixtures/runtime-workload-request.canonicalization.valid.json`.
That fixture composes a request whose inputs exercise the three behaviors a
near-JCS implementation gets wrong: number serialization (`1.0` canonicalizes
to `1`, `1e21` to `1e+21`), UTF-16 code-unit key ordering (a U+10000 key sorts
before a U+FFFD key, the opposite of code-point order), and minimal
string escaping (control characters, quotes, backslash, and non-ASCII text).
Both mirrored `requestHash` fields carry the digest of that request under the
current rule, so any language implementation removes the two fields,
canonicalizes, and must reproduce the pinned digest byte-for-byte.
