# Python SDK guide

Adds to the root [AGENTS.md](../../AGENTS.md); applies when working in
`sdks/python/`.

Layout: `packages/sdk` is the authoring SDK (compiler, CLI, packaging,
registry, catalog, settings, and the `syntara_plugin.testing` harness),
`packages/contracts` is the shared contracts and runtime distribution also
consumed by the Execution Plane, and `tests/` holds the unit and CLI
contract tests. Dependencies resolve through the root workspace lockfile.

- Run `pytest sdks/python/tests -q` for a fast loop; `make test` adds the
  examples suite.
- After `make install` the checkout exposes `syntara-plugin`; use it for
  manual validation against the examples the change affects, for example
  `syntara-plugin validate examples/hello-world/plugin.yaml --settings examples/hello-world/syntara-plugin.yaml`,
  and `syntara-plugin run` from source or a running image for runtime
  behavior (see [running-locally](../../docs/running-locally.md)).
- The testing harness is a pytest plugin: tests that use it assert that the
  direct and container execution paths agree.
- Generated protobuf code under
  `packages/contracts/src/syntara_plugin/runtime/protocol/` is regenerated,
  never edited.
