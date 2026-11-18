# Shared deliberate failure vocabulary

The shared runtime contract defines a closed registry of `ErrorResult`
category and code pairs. A step type may extend an error with fields through
`spec.error`, but may not define additional codes. This lets CLI and workload
consumers interpret the same failure without interpreting plugin-specific codes.

A nonzero wrapper status is a valid deliberate failure. `retryable` advises
whether another attempt with the same step definition and workflow input may
succeed; the Execution Plane owns retry policy. A failure without a valid
wrapper remains a `ContainerFailure`, with its own runtime and transport code
registry and execution-completion outcome. The registries are independent.

The [runtime contract](../../contracts/README.md) lists the current pairs and
the extension rules. The credential authenticate and test operations remain
outside this decision.
