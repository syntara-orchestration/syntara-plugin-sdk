# Documentation guide

Adds to the root [AGENTS.md](../AGENTS.md); applies when working in `docs/`.

- [README.md](README.md) maps each guide to its task; keep the map current
  when guides are added, renamed, or reorganized.
- The style model is [authoring-guide.md](authoring-guide.md):
  task-oriented sections, plain language, and runnable examples that show
  real output.
- Coverage rules: every author-facing manifest key is described in
  [plugin-language.md](plugin-language.md) with a link to its normative
  schema, and every settings key in [build-settings.md](build-settings.md).
- The normative sources stay in `../contracts/`; these guides explain, they
  do not redefine.
- Accepted design decisions live in `../.sdlc/adrs/` and proposals in
  `../.sdlc/proposals/`; link to a record instead of restating it.
- Validate `../examples/hello-world` when compilation, settings discovery,
  or packaging behavior changes.
