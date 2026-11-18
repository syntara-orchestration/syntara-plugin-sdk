# ADR 0014: Enforce JSON Schema authoring conformance

- Status: Accepted
- Date: 2026-11-18
- Reference: AAP-102214, ANSTRAT-2422

## Decision

Canonical bundle, authoring, compilation, and invocation share one JSON Schema
contract, enforced as follows.

**Draft 2020-12 only.** Authored schemas — inline or asset-backed documents
for `spec.input`, `spec.output`, `spec.error`, and trigger
`spec.configuration` — must declare
`$schema: https://json-schema.org/draft/2020-12/schema` explicitly. The
compiler rejects a schema that declares any other dialect
(`SCHEMA_DOCUMENT_UNSUPPORTED_DRAFT`) and rejects a schema that omits the
declaration (`SCHEMA_DOCUMENT_DIALECT_MISSING`) rather than defaulting it.
Neither case is accepted, so the contract is unambiguous and authoring tools
never need to guess a dialect.

**A contract-defined asserted format set.** `common.schema.json` declares the
set in `$defs.assertedFormats`: `date-time` (strict RFC 3339 subset) and
`uri` (absolute URI with a scheme). The Python contracts package derives the
set from the contract document and builds one shared `FormatChecker` that
registers exactly those names; every validator in the SDK, CLI, and runtime is
built from it (`build_validator`). Format names outside the set — including
jsonschema built-ins such as `email`, `ipv4`, and `date` — are annotations
only and never reject a value. Previously, call sites constructed ad-hoc
`FormatChecker()` instances whose asserted set depended on installed extras,
so the same schema could behave differently by environment and by code path.

**ErrorResult base composition and closed fallback.** When a step type
declares `spec.error`, a deliberate failure's error result is validated
against the SDK base composition (`errorResultBase`) followed by the declared
schema. When no `spec.error` is declared, the runtime binding supplies the
closed fallback (`runtime.schema.json#/$defs/errorResult`,
`additionalProperties: false`), so undeclared step type-specific error fields
are rejected. The recorded error-schema digest always covers the effective
error schema that was validated.

## Consequences

- Existing authored schemas in workspaces, examples, and fixtures must add
  the `$schema` declaration; scaffolding (`syntara-plugin init`) injects it
  into every generated schema. This is a forward-only change: the SDK is
  pre-release, so no compatibility shim or dialect defaulting is provided.
- Schemas that relied on environment-asserted formats (for example `email`)
  stop rejecting values; schemas using `date-time` and `uri` are now asserted
  consistently on every path. Tests that encoded the old environment-dependent
  behavior were updated to the contract semantics.
- The asserted format set changes only by a contract change: extend the enum
  in `common.schema.json` and register the matching check in the contracts
  package. The bundle loader fails loudly if the contract declares a name
  with no registered check.
- The Execution Plane, which consumes the contracts package, inherits the
  shared checker and the stricter authored-schema rules; downstream step
  types must add `$schema` declarations when they upgrade.
