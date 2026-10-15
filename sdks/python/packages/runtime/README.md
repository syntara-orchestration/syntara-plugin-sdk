# Syntara Plugin Runtime

`syntara-plugin-runtime` provides the Python container-runtime interfaces for
Syntara plugins. It consumes `syntara-plugin-contracts` and implements the
versioned workload protocol used between a plugin workload and Syntara.

The source of truth is maintained in the
[Syntara Plugin SDK repository](https://github.com/syntara-orchestration/syntara-plugin-sdk).
Plugin authors normally install `syntara-plugin-sdk`; this package is intended
for custom workload implementers.
