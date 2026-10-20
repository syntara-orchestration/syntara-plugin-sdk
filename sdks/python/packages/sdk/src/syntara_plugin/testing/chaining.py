"""Sequential step chaining with explicit seams for composed-operation tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator
from syntara_plugin.contracts import canonical_json_digest
from syntara_plugin.runtime import DriftReport, StepBinding, compare_model_to_schema

from .context_builder import build_execution_context
from .invocation import StepOutcome, invoke_step

if TYPE_CHECKING:
    from syntara_plugin.runtime import ExecutionContext

Seam = Callable[[Mapping[str, Any]], Mapping[str, Any]]


def identity_seam(output: Mapping[str, Any]) -> Mapping[str, Any]:
    """Pass one step's output through as the next step's input unchanged."""

    return output


def check_seam_compatible(source: StepBinding, target: StepBinding) -> DriftReport:
    """Statically prove the declared contracts fit across one seam.

    The check reuses the model-versus-schema drift engine with the ``output``
    direction: it fails when the source's output model can emit a value the
    target's declared input schema would reject. It validates the declared
    contracts only; the seam transform itself is not inspected here.
    :func:`chain_steps` validates each seam's actual output against the
    target's input schema at execution time and fails fast naming the seam,
    so a mismatched transform never reaches the target's business hook.
    """

    issues = compare_model_to_schema(
        source.step.output_model(),
        target.descriptor.input_schema,
        direction="output",
    )
    return DriftReport(
        step_identity=(
            f"{source.descriptor.step_identity}-to-{target.descriptor.step_identity}"
        ),
        version=None,
        input_schema_digest=canonical_json_digest(target.descriptor.input_schema),
        output_schema_digest=canonical_json_digest(source.descriptor.output_schema),
        issues=issues,
    )


def _seam_issues(seam_output: Mapping[str, Any], target: StepBinding) -> tuple[str, ...]:
    """Validate one seam's output against the target's declared input schema.

    Reports the failing validator and location only; jsonschema messages
    include the offending value, which could carry a secret sentinel.
    """

    errors = sorted(
        Draft202012Validator(target.descriptor.input_schema).iter_errors(seam_output),
        key=lambda error: list(error.path),
    )
    return tuple(
        f"{list(error.path) or ['<root>']}: failed {error.validator} validation"
        for error in errors
    )


@dataclass(frozen=True)
class ChainOutcome:
    """The per-link outcomes of one sequential chain execution.

    When a seam's output fails the next link's declared input schema,
    ``failed_link`` names the link that could not run and ``seam_issues``
    carries the schema violations (validator and location only, never the
    offending values).
    """

    links: tuple[StepOutcome, ...]
    output: Mapping[str, Any] | None
    failed_link: int | None
    failed_step_identity: str | None
    seam_issues: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether every link completed successfully."""

        return self.failed_link is None


def chain_steps(
    bindings: Sequence[StepBinding],
    inputs: Mapping[str, Any],
    *,
    context: ExecutionContext | None = None,
    seams: Sequence[Seam] | None = None,
) -> ChainOutcome:
    """Execute steps sequentially, passing each output through a seam.

    This is sequential dataflow for tests only. It is deliberately not a
    workflow engine: no branching, parallelism, fan-in, retries, or
    persistence. Orchestration belongs to the platform and to
    ``WorkflowStep.run`` implementations; when a chain needs control flow,
    implement it as a Workflow step and test the step.

    Args:
        bindings: The step bindings to execute, in order.
        inputs: The first link's raw inputs.
        context: Base context; each link receives its own invocation id and
            shares the workflow-run id.
        seams: One callable per adjacent pair mapping a link's output to the
            next link's inputs. Defaults to identity pass-through, which is
            only correct when the schemas actually match; prefer explicit
            seams plus :func:`check_seam_compatible`.
    """

    if not bindings:
        message = "chain_steps requires at least one step binding"
        raise ValueError(message)
    resolved_seams = list(seams) if seams is not None else [identity_seam] * (
        len(bindings) - 1
    )
    if len(resolved_seams) != len(bindings) - 1:
        message = (
            f"expected {len(bindings) - 1} seams for {len(bindings)} steps, "
            f"got {len(resolved_seams)}"
        )
        raise ValueError(message)
    base = context or build_execution_context()
    run_id = base.workflow_run_id or f"{base.invocation_id}-run"
    outcomes: list[StepOutcome] = []
    current: Mapping[str, Any] = inputs
    for index, binding in enumerate(bindings):
        link_context = build_execution_context(
            invocation_id=f"{base.invocation_id}-link-{index}",
            workflow_run_id=run_id,
            runtime_context=base.runtime_context,
            cancelled=base.cancelled,
        )
        outcome = invoke_step(binding, current, context=link_context)
        outcomes.append(outcome)
        if outcome.raised is not None or not outcome.ok:
            return ChainOutcome(
                tuple(outcomes), None, index, binding.descriptor.step_identity
            )
        if index < len(resolved_seams):
            # A successful wrapper always carries a business output; narrow
            # the optional type for the seam, which requires a Mapping.
            assert outcome.output is not None
            current = resolved_seams[index](outcome.output)
            seam_issues = _seam_issues(current, bindings[index + 1])
            if seam_issues:
                return ChainOutcome(
                    tuple(outcomes),
                    None,
                    index + 1,
                    bindings[index + 1].descriptor.step_identity,
                    seam_issues,
                )
    return ChainOutcome(tuple(outcomes), outcomes[-1].output, None, None)
