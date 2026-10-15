# Syntara Plugin Contracts

`syntara-plugin-contracts` distributes the versioned Syntara Plugin contract
bundle for Python consumers. It contains the canonical JSON Schemas,
conformance fixtures, and protocol definitions used by the Syntara Plugin SDK
and runtime.

The source of truth is maintained in the
[Syntara Plugin SDK repository](https://github.com/syntara-orchestration/syntara-plugin-sdk).
Install `syntara-plugin-sdk` to author plugins; depend on this package directly
only when implementing another contract-aware tool or runtime.
