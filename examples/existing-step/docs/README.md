# Existing Step

This plugin is the checked-in end-to-end prototype. It contains one Python
implementation each for Action, Control, and Trigger, and uses the manifest
entry point as the single source of truth for loading each implementation.

The local tests execute every step directly and through the in-process
container adapter. The repository's opt-in Podman integration test packages
the same plugin and executes it through the shared container runtime image.

## Run locally

From the repository root:

```sh
uv run --no-sync pytest examples/existing-step/tests -q
```

The tests compile the checked-in plugin, load each implementation through the
manifest-declared `module:Class` entry point, and compare the direct and
in-process container outcomes for valid and invalid inputs.

## Run the containerized prototype

The repository's CI job and local integration target package the plugin wheel,
install it beside `syntara-plugin-contracts`, build the checked-in
`Containerfile`, start the runtime server with mutual TLS, and invoke all three
steps over `syntara.container/v1alpha1`:

```sh
make test-integration
```

This proves the SDK packaging and container-adapter path. It does not start
Automation Orchestrator or the Execution Plane. The current standalone
Execution Plane checkout still vendors the older `syntara.node.v1` node
protocol, so dispatch through that service remains a separate protocol-alignment
and platform-integration step. Platform-owned runners and workflow selection in
Automation Orchestrator also remain separate from this publisher-owned plugin
prototype.
