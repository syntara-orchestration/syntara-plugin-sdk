# Step type compatibility check

Every pull request that can affect the SDK packages builds them and runs the
maintained step types from
[syntara-step-types](https://github.com/syntara-orchestration/syntara-step-types)
against those builds. A change that breaks a real step type shows up on the
pull request, with the failing step type, the stage, and an analysis of the
cause, before anything is released.

The **Step type compatibility** job in [CI](../.github/workflows/ci.yml) calls
[its reusable workflow](../.github/workflows/step-types-compat.yml); its tooling
lives in [`tests/step_types`](../tests/step_types).

## What runs

1. **Changes**: pull requests that touch only documentation skip the check.
   Everything else runs it.
2. **Discover step types**: checks out syntara-step-types at the pinned
   revision and reads the step types its own Makefile tests (`SDK_PLUGINS`).
   An empty list fails the check, so it can never pass while testing nothing.
3. **Step type tests**: one job runs each discovered step type sequentially, each with six numbered stages:

   | Stage | What it does |
   | --- | --- |
   | 1 preflight | Checks the step-types layout and its make targets. |
   | 2 environment | Points step-types at the wheels built from this pull request through `[tool.uv.sources]`, re-locks, runs `make install`, and verifies the installed SDK really is the candidate build. |
   | 3 validate | `make sdk-plugin-validate` |
   | 4 run | `make sdk-plugin-run` (source mode, with step-types' own output expectation) |
   | 5 registry | Starts a throwaway OCI registry on `localhost:5000`. |
   | 6 build-inspect | Restores step-types' own lock file, re-verifies the candidate SDK, and runs `make sdk-plugin-build-inspect` (image build, plugin archive, inspection, archive assertions). |

   Stages 3, 4, and 6 are step-types' own make targets, unmodified, so a
   change to that flow on the step-types side is picked up on the next pin.
   While they run, every Python process records which SDK it imported, and a
   stage fails unless all of them used this pull request's wheels: a result
   produced by the published SDK says nothing about the change.
4. **compat-report**: the final step aggregates all reports into one verdict
   and compares failures against `devel` (the baseline, below). The enclosing
   **Step type compatibility** job feeds the **CI Required Checks** job; require
   that final job in branch protection. On `devel` pushes and manual runs the
   same final step enforces the results without a baseline.

## Reading a failure

Each step type prints a stage table and, when a stage fails, a failure panel:

- **Category**: where the failure comes from:

  | Category | Meaning |
  | --- | --- |
  | `misconfiguration` | The candidate SDK rejects step-types' plugin settings or workspace. |
  | `contract` | A boundary changed: dependencies, CLI arguments, CLI JSON output, or the runtime protocol. |
  | `implementation` | The failure is inside step-types code or its output. The location is a fact; the cause may still be a behavior change in the pull request. |
  | `sdk-regression` | The candidate SDK itself crashed or built an inconsistent set of wheels. |
  | `infra` | Missing tools, registry, container engine, or network trouble, including a service the example input calls being unavailable. Retried once, except timeouts. |
  | `fixture-drift` | The pinned step-types checkout no longer matches what the check expects. |
  | `unknown` | No rule matched; read the full output. |

  `infra` and `fixture-drift` failures are reported as "not verified" rather
  than "incompatible": the check could not reach a verdict about the change.

  The analysis reads the JSON behind a failed stage, which step-types' make
  targets otherwise reduce to jq's `false`: compiler diagnostics are classified
  by their `code`, an error a step reported itself by its `error.code`. In any
  stage, a traceback decides which side the failure is on, even when the
  exception looks like a network error; such failures are still retried once.

- **Analysis**: the rule that matched, with its confidence. `deterministic`
  means an exact signature from SDK, uv, make, or jq output; `inferred` means
  the location comes from traceback frames or a broad signal; `unclassified`
  means nothing matched.
- **Evidence**: the matched lines, verbatim.
- **What to do**: the next step for that rule, and the command that
  reproduces it locally.

Collapsible groups below the panel hold the failed command's full output, the
traceback frames labeled by side (SDK, step-types, third-party), and the
environment details (candidate wheel hashes and installed versions). The job
summary repeats the analysis, and the job uploads a
`step-types-compat-grouped` artifact with every JSON report and stage log.

### When a failure reports unknown

An `unknown` failure still fails the check; it means no rule recognized the
signature, so the analysis says what it knows and stops. To turn it into a
rule:

1. Open the `step-types-compat-grouped` artifact and read
   `<step type>.json` and the failing stage's log.
2. Find the stable signature: an error code, an exact message line, or a
   traceback location.
3. Add a rule to `tests/step_types/classifier.py` with a test that captures
   the real output, and prove the test can fail by breaking the rule.

The gate's summary prints the analysis mix for the run (`failure analysis:
2 deterministic, 1 unclassified`, plus a pointer here whenever any failure is
unclassified), so unknown failures are visible without opening the logs.
Three sync tests keep the classifier current with the SDK: one fails when a
new `FailureCode` is added without a mapping, and two run the CLI against a
scratch workspace and fail when an error message or diagnostic code the rules
match changes.

### Mechanical migrations

For a few settings changes the check knows the migration. It applies it to the
throwaway step-types checkout, re-runs the remaining stages, and attaches the
diff as `<step type>-migration.patch`. Nothing is committed or pushed, and a
successful migration never turns the check green: it shows that the break is
migratable and gives the step-types maintainers the patch. The panel says what
the patch does not cover.

## Intentional breaking changes

When the break is intended, add the `breaking-change` label to the pull request.
CI re-runs and reports the same analysis as warnings without failing. Removing
the label re-runs CI with failures enforced. A new pull-request event cancels
the preceding CI run, and the required check starts immediately to remain
pending until the current run finishes. The compatibility report reads the
label when it runs. Coordinate the migration with
the step-types maintainers and share the report.

Until step-types migrates a break that is already on `devel`, the check stays
red on `devel` pushes; that red is migration debt waiting on step-types, not a
flaky check, and it clears when the pin moves to a step-types revision that
works with the SDK. Pull requests, however, are judged against `devel` as it
is, so an inherited break does not fail them (next section). The label is only
needed on the pull request that introduces a break.

## Failures already on devel

Every merge to `devel` runs this same job and uploads one report per step
type in its grouped artifact. Those reports are the *baseline*: when a pull
request test fails, the gate
reads the baseline for exactly the `devel` commit the pull request is tested
against (the merge commit's base) and compares the failures. Reading it needs
only the workflow's read-only token.

- A failure `devel` already carries, with the same pin, stage, rule, and
  identity (the broken settings keys or diagnostic codes, or the traceback
  frame a crash was located on), is **inherited**: the summary shows it as
  `incompatible, inherited` with the `devel` commit it comes from, and it does
  not fail the check.
- A failure that is new or worse (one more or one different broken field, a
  different rule or stage, a migration that used to pass and now fails)
  **fails the check** as usual.
- A pull request that breaks strictly less than `devel` (fewer broken fields)
  is inherited too, and the summary says so.

The runner records each compatibility command's result and continues to the
next step type. The final `compat-report` step evaluates every report; setup,
runner, and report-upload failures still fail the job. If the compatibility command exits
unsuccessfully after writing a non-failing report, the gate marks that report
as a runner error and fails. A new compatibility failure, tooling failure,
or discovery failure also fails the aggregate check. This lets a later pull
request pass when its failure is already on `devel`, even if the pull request
that introduced it was marked `breaking-change`. The label is needed only on
the pull request that introduces an intentional break.

A failing validate or run stage does not stop the check: the later stages
still run, every failing stage is recorded, and each one is compared against
the baseline, so a new break hiding behind an inherited one still fails the
check. A failure in preflight, environment, or the registry does block the
stages after it (nothing downstream can test the candidate without them);
when the blocking failure is inherited, the step type is reported as
`not verified, inherited` with the stages that did not run, rather than as
compatible. For settings migrations, the adapter's re-run on the migrated
copy is compared as well.

Not every failure can be inherited: `infra` and `unknown` failures never are
(they stay red; re-run or classify them), and a few rules have no identity
stable enough to compare. The policy lives in `tests/step_types/baseline.py`,
and a test fails when the classifier gains a rule the policy does not know.

The baseline is a completed `devel` push run for the pull request's base
commit, not a manually overridden run. When its reports are unavailable
(the push is still running, was cancelled, or its artifacts expired), the job
checks out the base commit, builds its wheels, and reruns the same step types
against the revision that commit pins (a pin
bump stays strictly judged: the pull request's new pin never becomes its own
baseline), so the pull request is self-sufficient and nobody refreshes
anything by hand. If recomputation fails, passing step types still pass; a
failing step type is treated as new unless the breaking-change label applies.
Partial baseline reports are discarded. Re-run all jobs to repeat the tests
and final report. The required CI gate checks jobs from the current attempt;
if a partial retry omits required jobs, it fails and asks for a full retry.

## The pinned revision

The step-types revision is one line in the reusable workflow:

```yaml
STEP_TYPES_REF: ${{ inputs.step_types_ref || '<commit SHA>' }}
```

To move the pin, open a pull request that changes that SHA; the check runs
against the new revision before the change merges. The job shows the pin's
age and warns when it is more than 14 days old.

To try another revision without changing the pin, run the workflow manually
from the CI workflow in the Actions tab and set `step_types_ref` to a commit,
branch, or tag. The manual run also has a `report_only` option.

## Running it locally

```sh
make test-step-types                               # every step type, pinned revision
make test-step-types STEP_TYPES_PLUGIN=http-request
make test-step-types STEP_TYPES_REF=devel          # another revision
make test-step-types STEP_TYPES_MODE=report-only:dispatch   # report without failing
make test-step-types-helpers                       # unit tests for the tooling
```

This needs uv, git, make, jq, and Podman. It builds the wheels from the working
tree (uncommitted changes included), works in `.cache/step-types-compat`, and
reuses a registry that is already listening on `localhost:5000` (images are
pushed to it).

## Known limitations

- The check surfaces compatibility regressions; it cannot guarantee that it
  detects every breaking change.
- Stages blocked by a failed preflight, environment, or registry stage remain
  unchecked; those step types are reported as `not verified` rather than
  compatible (the adapter's migrated re-run is compared too).
- If the `devel` baseline is unavailable, the job recomputes it before testing
  the candidate. A missing or invalid baseline cannot excuse a failing step type.
- The tests capture raw command output in the uploaded artifacts. That is fine
  for the public example step types; redact before pointing the check at
  inputs that carry real credentials.
- Workload images install `syntara-plugin-contracts` from published releases
  (step-types' lock file and Containerfiles), not from the pull request. Changes
  to the contracts package are exercised through the authoring and source-mode
  stages only. The host/image digest boundary those stages cannot see — an
  already-built image whose schema digests and step revision IDs were
  computed by an older contracts package — is covered instead by
  `test_candidate_host_rejects_an_image_hashed_by_the_published_serializer`
  in `sdks/python/tests/sdk/test_authoring.py`, which verifies the candidate
  fails closed on a registration manifest hashed by the previous serializer.
- Built images are inspected, not executed. Source-mode runs use the candidate
  on both sides of the runtime protocol, so a protocol change between the
  platform host and an already-built image is outside this check. The
  Execution Plane Integration CI job covers dispatching an image through the
  host.
- Classification reads error text and traceback frames. When the SDK reports
  structured error categories, the check will read them instead.
- The `http-request` example calls a public HTTP endpoint, so an outage there
  can fail the run stage. Such failures are classified as `infra` and retried
  once.
