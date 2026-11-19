# TypeScript steps

A custom-runner plugin whose step type is written in strict TypeScript for
Node.js 22. `normalize-records` does what the Python
[`NormalizeRecordsStep`](../python-steps/python_steps/normalize_records.py)
does: it trims and lowercases every record name and returns the list with its
count. Case mapping and whitespace trimming follow each language's standard
library, so a few rare characters differ (for example, JavaScript `trim()`
removes U+FEFF and keeps U+001C to U+001F, which Python strips).

| File | Purpose |
| --- | --- |
| [`plugin.yaml`](plugin.yaml), [`steps/normalize-records/manifest.yaml`](steps/normalize-records/manifest.yaml) | The plugin and its Action. `entryPoint: handlers.ts#normalizeRecords` is an opaque identifier that only the workload image interprets. |
| [`src/handlers.ts`](src/handlers.ts) | The step type and the table that maps each entry point to its function. |
| [`test/handlers.test.ts`](test/handlers.test.ts) | Unit tests, including the declared input contract (`additionalProperties: false`). |

Validate the workspace with the SDK CLI:

```sh
syntara-plugin validate plugin.yaml
```

The implementation runs with its own toolchain:

```sh
npm ci
npm test
```

`npm test` compiles into `build/` first; that directory and `node_modules/`
are local build output.

To package and run this plugin, the workload image needs a server for the
container ABI that dispatches by step revision ID and resolves the entry
point through the registration manifest; see
[Workload servers in other languages](../../docs/runtime-and-oci.md#workload-servers-in-other-languages).
This example intentionally stops at the implementation: it is a manifest-level
demonstration that a non-Python entry point is a first-class step type, not a
runnable image.
