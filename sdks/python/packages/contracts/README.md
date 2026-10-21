# Syntara Plugin Contracts

`syntara-plugin-contracts` distributes the versioned Syntara Plugin contract
bundle and runtime APIs. It contains the canonical JSON Schemas, conformance
fixtures, protocol definitions, and the Python APIs used by plugin workload
implementers and the SDK. It also installs the `syntara-plugin-runtime-server`
console script, which a final Python workload image uses to serve the container
ABI.

The source of truth is maintained in the
[Syntara Plugin SDK repository](https://github.com/syntara-orchestration/syntara-plugin-sdk).
Install `syntara-plugin-sdk` to author plugins. Depend on this package directly
when implementing another contract-aware tool or plugin workload runtime.
