# ADR 0012: Container Toolbox host-service boundary

- Status: Accepted
- Date: 2026-11-10

## Decision

Each `ExecutionContext` exposes a read-only `toolbox` property whose value is
an invocation-scoped `ContainerToolbox`. It is always present and currently
has no service operations. The executor retains the same toolbox when it binds
the context to the invocation's declared credentials. Direct execution, the
local runner, and the testing harness all supply the empty surface. The
container callback and the public surface share the invocation lifetime;
completion, failure, cancellation, or disconnect revokes it. Direct execution
closes it when the executor returns, including on failure.

The public toolbox has no credential resolver. Plugin credential access remains
`context.credential(name).read()`. Internally, the credential provider delegates
the existing callback transport through a private invocation-bound Toolbox
capability. This does not change the correlated sensitive credential messages,
logical-name authorization, limits, or lifecycle in
[ADR 0011](0011-credential-callback.md). Credential resolution and mapping to
authority identifiers remain host responsibilities.

Future Toolbox operations need explicit typed, capability-scoped contracts and
host implementations before they are exposed. The host authorizes each
operation against its trusted invocation assignment. A Toolbox operation must
not provide a generic host command, arbitrary code execution, or an untyped
payload dispatch API.

## Consequences

The empty public surface adds no container protocol operation or ABI change.
The SDK and host must agree on authorization, ownership, limits, cancellation,
errors, and protocol compatibility before adding each host service.
