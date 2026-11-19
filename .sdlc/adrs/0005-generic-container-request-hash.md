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
