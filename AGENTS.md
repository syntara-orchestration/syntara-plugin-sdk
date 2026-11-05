# Syntara Plugin SDK agent guide

This repository builds, validates, packages, and publishes Syntara plugins:
the language-neutral contracts in `contracts/` (JSON Schemas, conformance
fixtures, the container protobuf ABI), the Python SDK and `syntara-plugin`
CLI, and the shared contracts package the Execution Plane also consumes.
Repository layout, setup, and the contract-change procedure live in
[docs/developer-guide.md](docs/developer-guide.md); read it before touching
`contracts/`.

This file is the shared guide for any coding agent. `CLAUDE.md` imports it
for Claude Code and adds Claude-specific notes: update this file first, and
keep the two in agreement. Area-specific guidance lives in nested
`AGENTS.md` files under `contracts/`, `sdks/python/`, and `docs/`: read the
one for any subtree you touch, on top of the rules here.

## Related repositories

- [syntara](https://github.com/syntara-orchestration/syntara): the orchestrator; accepts user intent and dispatches work.
- [syntara-execution-plane](https://github.com/syntara-orchestration/syntara-execution-plane): runs dispatched work; consumes this repo's contracts package and container gRPC ABI.
- [syntara-step-types](https://github.com/syntara-orchestration/syntara-step-types): container-packaged step types built on this SDK.

A change to `contracts/`, to runtime ABI surfaces, or to packaged behavior
can break the Execution Plane and downstream step types: check the impact in
those repositories before proposing the change.

## Commands

`uv` is the toolchain. `pyproject.toml` and `uv.lock` are the source of truth
for dependencies; regenerate the lockfile when dependency metadata changes.

- `make install`: sync the development environment
- `make test`: unit, CLI, contract, and examples suite
- `make test-integration`: opt-in Podman and registry runtime-image tests
- `make lint` and `make check`: ruff through pre-commit
- `make generate-container-protocol`: regenerate the protobuf bindings after a `.proto` change; generated `*_pb2*` files are never hand-edited

## Ground rules

- Ask the software engineer whenever the path is ambiguous; do not guess.
- Open a pull request only when the engineer asks; never open one on your own.
- Contracts and the container ABI are shared with the Execution Plane: follow
  the contract-change procedure in the developer guide, regenerate protocol
  code, update fixtures, and check downstream impact. The SDK is pre-release,
  so prefer a clear forward-only contract over a compatibility shim.
- Keep new and changed production code fully type-annotated; the
  distributions ship `Typing :: Typed`.
- Never commit secrets, registry credentials, or generated build output, and
  keep credentials out of error messages, logs, and test output.
- Before completing any change, run `make lint` and `make test`.
- After a material change, review `README.md`, `contracts/`, `docs/`, and
  `.sdlc/` for needed updates, and report that review at handoff, including
  when nothing needed updating.
- Ticket numbers never appear in PR titles, descriptions, or commit
  subjects; the `AAP-XXXXX` key rides in the branch name
  (`aap-101129-agent-guides` style) and nowhere else.

## Work categories

### Feature development

Run the stages in order: research the requirement and the affected code,
write an implementation plan for the engineer to review, implement the
feature with tests, validate manually by running the commands and examples
the change affects, have another agent with clean context review the diff,
then close with a summary. Ambiguity at any stage goes to the engineer.

### Fix issues

Verify and reproduce the issue locally before changing anything. When it
depends on an external service, credentials, or tokens, reproduce against a
mock or fake service instead of skipping the reproduction. Once the cause is
understood, plan the fix with its impact in mind, then add a red-green test:
the failing test first, the fix that turns it green second.

### Docs

Direct and to the point, with a few runnable examples where they earn their
place; follow the approach, section flow, and tone of
[docs/authoring-guide.md](docs/authoring-guide.md). Run only the documented
commands and output blocks the change affects against the current CLI before
submitting: docs that drift from real behavior are bugs.

## After pushing, while the PR is open

- CI runs the Python test matrix (3.12 to 3.14), pre-commit, the package
  build with its install smoke test, and the opt-in runtime-image integration
  job. Failures are usually caused by the change just pushed: read the
  failing job's log and fix the cause instead of re-running and hoping.
- Check the SonarCloud results on the PR and fix every finding on New Code:
  new issues including code smells, Security Hotspots, and Duplication. There
  is no coverage gate yet; check coverage too once one exists.

## Pull requests

- Use the `gh` CLI for PR interaction when it is installed.
- Follow `.github/pull_request_template.md`: concise, direct sections a
  software engineer would write, assuming the reader knows the repo.
- Match the conventional-commit style of recent history for the title, for
  example `fix(runtime): ...` or `docs: ...`.
- Assign the PR to the engineer handling it: self-assign when you open it
  (`gh pr create --assignee @me`).
