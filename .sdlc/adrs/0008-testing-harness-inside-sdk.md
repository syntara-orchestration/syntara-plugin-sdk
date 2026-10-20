# ADR 0008: Testing harness ships inside the SDK distribution

- Status: Accepted
- Date: 2026-10-20
- Supersedes: the package-layout decision in ADR 0007 (decision 1); the rest
  of ADR 0007 still stands.

## Context

ADR 0007 shipped the local test harness as a fourth workspace package,
`syntara-plugin-testing`, alongside the sdk distribution. During review the
question arose of whether the harness belongs inside `syntara-plugin-sdk`
instead: one less package to version and release, one install for authors,
and no separate distribution name to teach.

The original reasons for the split were the pytest dependency, the layering
(the harness needs both the runtime executor and the sdk compiler), and
release cadence. On re-examination, only the first is real: the sdk package
already depends on contracts and runtime, so no cycle appears, and the
harness's fixture API can version with the sdk contract it wraps.

## Decision

1. `syntara_plugin.testing` moves inside the sdk distribution
   (`sdks/python/packages/sdk/src/syntara_plugin/testing/`). The import path
   is unchanged, so no author code changes; only the distribution name does.
2. pytest is an optional extra: `pip install "syntara-plugin-sdk[testing]"`.
   The function core (invocation, bindings, factories, conformance,
   chaining, redaction) imports and runs without pytest; the
   `pytest11` entry point is inert metadata unless pytest is installed, and
   the fixtures load whenever pytest is present, whether it arrived through
   the extra or the user's own environment.
3. `assert_no_secrets` raises `SecretLeakError`, an `AssertionError`
   subclass, instead of calling `pytest.fail`, so the redaction helper needs
   no pytest and fails tests under any runner.
4. The runtime package remains untouched: it still does not depend on the
   sdk, and nothing in it knows about the harness.

## Consequences

- One distribution installs the CLI, the compiler, and the harness; the
  workspace has three packages again.
- Users who install the SDK without the extra keep a fully working function
  core; only the auto-loaded pytest fixtures require the extra (or any
  pytest install).
- The generated tests from `syntara-plugin test-init` import
  `syntara_plugin.testing` exactly as before.
- Harness API changes now ride the sdk package's version train.
