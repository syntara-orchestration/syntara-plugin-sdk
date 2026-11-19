# ADR 0014: Language-neutral custom-runner entry points

- Status: Accepted
- Date: 2026-11-13

## Decision

`spec.runtime.entryPoint` is an opaque handler identifier in every shared
contract: step type manifests, the plugin descriptor, resolved execution data,
and the registration manifest. The shared schema bounds it to 1 to 256
printable ASCII characters without whitespace and nothing more. Its syntax
belongs to the language of the workload image.

The image-level server dispatches each `Execute` call by the compiled
`step_revision_id`. It resolves that revision to a handler through the
registration manifest in its image, interpreting the recorded entry point by
its own rule. The Execution Plane and the SDK client never parse the entry
point; it travels only in bootstrap files, never on the container ABI.

Python `module:Class` is the Python adapter's convention, enforced only where
the adapter binds or imports a Python class: `InstalledStepManifest`, the
runtime server, and the testing harness. No planning path (source mode,
test generation) decides a target's language from the identifier's syntax:
the harness classifies an entry point it cannot load as not bindable as
Python, source mode turns that into image-mode guidance, and test generation
selects by load attempt, reporting targets that do not load instead of
generating failing modules for them.

## Consequences

A workload in Go, TypeScript, or any language that serves the
container ABI can be validated, built, published, and run in image mode with
the existing CLI. The repository ships Go and TypeScript examples as
manifest-level demonstrations of a non-Python entry point: the step
implementation and its unit tests in the language itself, stopping short of a
runnable image. They are not language SDKs and not container ABI reference
implementations; the server contract for other languages is documented in
the runtime guide.

The bundle digest changed, so descriptors compiled against the previous
contract bundle must be recompiled. Existing Python entry points stay valid.
Source mode and generated test suites remain Python-only; other languages test
with their own tooling and image mode.
