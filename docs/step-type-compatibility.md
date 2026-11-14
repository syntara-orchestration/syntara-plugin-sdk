# Step type compatibility check

Every pull request that can affect the SDK packages builds them and runs the
maintained step types from
[syntara-step-types](https://github.com/syntara-orchestration/syntara-step-types)
against those builds. A change that breaks a real step type shows up on the
pull request, with the failing step type, the stage, and an analysis of the
cause, before anything is released.

The check is the workflow
[`.github/workflows/step-types-compat.yml`](../.github/workflows/step-types-compat.yml);
its tooling lives in [`tests/step_types`](../tests/step_types).

## What runs

1. **Changes**: pull requests that touch only documentation skip the check.
   Everything else runs it.
2. **Discover step types**: checks out syntara-step-types at the pinned
   revision and reads the step types its own Makefile tests (`SDK_PLUGINS`).
   An empty list fails the check, so it can never pass while testing nothing.
3. **One job per step type**, each with six numbered stages:

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
4. **compat-report**: aggregates every job into one verdict and one summary
   table, and compares failures against `devel` (the baseline, below).

## Reading a failure

Each job prints a stage table and, when a stage fails, a failure panel:

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
summary repeats the analysis, and each job uploads a
`step-types-compat-<step type>` artifact with a JSON report and every stage's
log.

### When a failure reports unknown

An `unknown` failure still fails the check; it means no rule recognized the
signature, so the analysis says what it knows and stops. To turn it into a
rule:

1. Open the leg's `step-types-compat-<step type>` artifact and read
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
The check re-runs by itself and then reports the same analysis as warnings
without failing. Removing the label re-runs it again with failures enforced.
The gate (`compat-report`) decides from the label as it reads it when it runs,
so a stale result never outlives a label change. Coordinate the migration with
the step-types maintainers and share the report.

Until step-types migrates a break that is already on `devel`, the check stays
red on `devel` pushes; that red is migration debt waiting on step-types, not a
flaky check, and it clears when the pin moves to a step-types revision that
works with the SDK. Pull requests, however, are judged against `devel` as it
is, so an inherited break does not fail them (next section). The label is only
needed on the pull request that introduces a break.

## Failures already on devel

Every merge to `devel` runs this same workflow and uploads one report per step
type. Those reports are the *baseline*: when a pull request leg fails, the gate
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

Only the first failing stage is recorded per step type, so while a failure is
inherited the stages after it are unchecked; the summary names them. For
settings migrations, the adapter's re-run on the migrated copy is compared as
well, so a second break hiding behind an inherited migration still fails the
check.

Not every failure can be inherited: `infra` and `unknown` failures never are
(they stay red; re-run or classify them), and a few rules have no identity
stable enough to compare. The policy lives in `tests/step_types/baseline.py`,
and a test fails when the classifier gains a rule the policy does not know.

The baseline needs the `devel` push run for the pull request's base commit to
have finished. A pull request checked in the minutes after a merge fails with
`no completed devel run ... yet`; re-running the failed jobs clears it once
that run finishes. When only the gate failed (after a label change, for
example), re-running the `compat-report` job alone is enough, and the legs
are reused. Baseline reports are kept for 90 days.

## The pinned revision

The step-types revision is one line in the workflow:

```yaml
STEP_TYPES_REF: ${{ inputs.step_types_ref || '<commit SHA>' }}
```

To move the pin, open a pull request that changes that SHA; the check runs
against the new revision before the change merges. Every job shows the pin's
age and warns when it is more than 14 days old.

To try another revision without changing the pin, run the workflow manually
from the Actions tab and set `step_types_ref` to a commit, branch, or tag. The
manual run also has a `report_only` option.

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
- Only the first failure per step type is compared against `devel`; the stages
  after it are unchecked while that failure is inherited (the adapter's
  migrated re-run is compared too).
- The baseline is the `devel` push run for the pull request's base commit;
  until that run completes, inherited failures cannot be recognized.
- Workload images install `syntara-plugin-contracts` from published releases
  (step-types' lock file and Containerfiles), not from the pull request. Changes
  to the contracts package are exercised through the authoring and source-mode
  stages only.
- Built images are inspected, not executed. Source-mode runs use the candidate
  on both sides of the runtime protocol, so a protocol change between the
  platform host and an already-built image is outside this check. The
  Execution Plane compatibility workflow covers dispatching an image through
  the host.
- Classification reads error text and traceback frames. When the SDK reports
  structured error categories, the check will read them instead.
- The `http-request` example calls a public HTTP endpoint, so an outage there
  can fail the run stage. Such failures are classified as `infra` and retried
  once.
