"""Sentinel-based secret-leak assertions for step test outcomes."""

from __future__ import annotations

import json
from collections.abc import Iterable
from uuid import uuid4

import pytest

from .invocation import StepOutcome


def secret_sentinel(prefix: str = "syntara-test-secret-") -> str:
    """Return one unique fake secret value for leak assertions."""

    return f"{prefix}{uuid4().hex}"


def _outcome_texts(outcome: StepOutcome) -> list[tuple[str, str]]:
    """Collect every caller-visible text an outcome could leak a secret into."""

    texts: list[tuple[str, str]] = []
    if outcome.status_message:
        texts.append(("status message", outcome.status_message))
    if outcome.error is not None:
        texts.append(("error message", outcome.error.message))
    if outcome.raised is not None:
        texts.append(("raised exception", repr(outcome.raised)))
    if outcome.output is not None:
        texts.append(("business output", json.dumps(outcome.output, default=str, sort_keys=True)))
    if outcome.raw is not None:
        texts.append(("raw envelope", repr(outcome.raw)))
    return texts


def assert_no_secrets(
    value: StepOutcome | str,
    sentinels: Iterable[str],
    *,
    caplog: pytest.LogCaptureFixture | None = None,
) -> None:
    """Fail when any sentinel secret appears in an outcome or captured logs.

    Scans the outcome's status message, error message, raised exception,
    serialized business output, raw envelope, and (when supplied) the pytest
    ``caplog`` capture. Secrets must never appear in any of them.

    When scanning logs, raise the capture level around the invocation first:
    pytest captures only records that pass the root logger's effective level,
    which defaults to WARNING. Use ``with caplog.at_level(logging.INFO):``
    around the step invocation so INFO-level leaks are captured.

    The failure message never repeats the sentinel value: it names the
    sentinel by position and length and the location it leaked into, so CI
    output cannot echo the secret through the report itself.

    Args:
        value: The outcome to scan, or an arbitrary text to check directly.
        sentinels: The secret values that must not appear.
        caplog: The pytest log-capture fixture when logs should be scanned too.
    """

    texts: list[tuple[str, str]] = (
        [("supplied text", value)] if isinstance(value, str) else _outcome_texts(value)
    )
    if caplog is not None:
        texts.append(("captured logs", caplog.text))
    sentinel_list = list(sentinels)
    leaks = [
        (sentinel_index, location)
        for sentinel_index, sentinel in enumerate(sentinel_list)
        for location, text in texts
        if sentinel in text
    ]
    if leaks:
        locations = "; ".join(
            f"sentinel #{sentinel_index} ({len(sentinel_list[sentinel_index])} chars) "
            f"leaked into the {location}"
            for sentinel_index, location in leaks
        )
        pytest.fail(f"secret sentinel leaked into the step outcome or logs: {locations}")
