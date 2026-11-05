# Contracts guide

Adds to the root [AGENTS.md](../AGENTS.md); applies when working in
`contracts/`.

This tree is the language-neutral source of truth shared by every SDK and by
the Execution Plane: versioned JSON Schemas (`schemas/`), cross-language
conformance fixtures (`fixtures/`), and the container gRPC ABI
(`proto/syntara_plugin/runtime/protocol/container.proto`).
[README.md](README.md) explains each contract; read it first.

Change a contract deliberately, in this order:

1. Update the schema or protobuf here first; never adapt code around a
   private interpretation of it.
2. Regenerate the protocol bindings with `make generate-container-protocol`
   from the repository root. Generated `*_pb2*` files are never hand-edited.
3. Update the affected conformance fixtures; every invalid fixture is
   rejected for exactly one reason.
4. Add or update focused tests in `sdks/python/tests`.
5. Update the references in `docs/` and `.sdlc/`.
6. Run `make test`, then check the downstream impact: the Execution Plane
   consumes this package and speaks this ABI, and step types build on it.

The SDK is pre-release: prefer a clear forward-only contract over a
compatibility shim, and let removed keys fail validation rather than
silently select legacy behavior. Record larger decisions as ADRs in
`.sdlc/adrs/`.
