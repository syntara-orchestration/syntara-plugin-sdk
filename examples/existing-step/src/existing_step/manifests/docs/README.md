# Existing Step

This plugin is the checked-in end-to-end prototype. It contains one Python
implementation each for Action, Control, and Trigger. The plugin workspace
(`plugin.yaml`, `steps/`, and this document) is packaged inside the Python
package under `src/existing_step/manifests/`, so the wheel and the source
tree expose the same layout. `create_bindings` delegates to the SDK's
`load_plugin_bindings` wrapper, which derives each identity from the
packaged manifests and uses the manifest entry point as the single source
of truth for loading each implementation.

The SDK's end-to-end suite executes every step directly and through the
in-process container adapter. The repository's opt-in Podman integration test
packages the same plugin and executes it through the shared container runtime
image.

## Run the SDK end-to-end suite

From the repository root:

```sh
uv run --no-sync pytest sdks/python/tests/integration/test_existing_step_e2e.py -q
```

The suite compiles this checked-in plugin, loads each implementation through
the manifest-declared `module:Class` entry point, and compares the direct and
in-process container outcomes for valid and invalid inputs.

## Run the containerized prototype

The repository's CI job and local integration target package the plugin wheel,
install it beside `syntara-plugin-contracts`, build a workload image from the
checked-in `Containerfile`, embed the compiler-generated registration manifest,
start the runtime server with mutual TLS, and invoke all three steps over
`syntara.container/v1alpha1`:

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

The checked-in `Containerfile` is the base workload recipe; it is not a
standalone runnable image. Use `syntara-plugin build --with-workload` for a
publisher image: that command compiles the descriptor, generates the immutable
registration manifest, and adds it at
`/opt/syntara/registration-manifest.json`. A custom-runner server intentionally
refuses to start without that manifest. The integration test performs the same
final-layer step explicitly so it can exercise the image recipe in isolation.
