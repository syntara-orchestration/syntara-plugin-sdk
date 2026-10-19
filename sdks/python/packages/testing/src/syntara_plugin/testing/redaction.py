"""Sentinel-based secret-leak assertions for step test outcomes."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from .invocation import StepOutcome

if TYPE_CHECKING:
    from pytest import LogCaptureFixture


def secret_sentinel(prefix: str = "syntara-test-secret-") -> str:
    """Return one unique fake secret value for leak assertions."""

    return f"{prefix}{uuid4().hex}"


def _outcome_texts(outcome: StepOutcome) -> list[str]:
    """Collect every caller-visible text an outcome could leak a secret into."""

    texts: list[str] = []
    if outcome.status_message:
        texts.append(outcome.status_message)
    if outcome.error is not None:
        texts.append(outcome.error.message)
    if outcome.raised is not None:
        texts.append(repr(outcome.raised))
    if outcome.output is not None:
        texts.append(json.dumps(outcome.output, default=str, sort_keys=True))
    if outcome.raw is not None:
        texts.append(repr(outcome.raw))
    return texts


def assert_no_secrets(
    value: StepOutcome | str,
    sentinels: Iterable[str],
    *,
    caplog: LogCaptureFixture | None = None,
) -> None:
    """Fail when any sentinel secret appears in an outcome or captured logs.

    Scans the outcome's status message, error message, raised exception,
    serialized business output, raw envelope, and (when supplied) the pytest
    ``caplog`` capture. Secrets must never appear in any of them.

    When scanning logs, raise the capture level around the invocation first:
    pytest captures only records that pass the root logger's effective level,
    which defaults to WARNING. Use ``with caplog.at_level(logging.INFO):``
    around the step invocation so INFO-level leaks are captured.

    Args:
        value: The outcome to scan, or an arbitrary text to check directly.
        sentinels: The secret values that must not appear.
        caplog: The pytest log-capture fixture when logs should be scanned too.
    """

    texts = [value] if isinstance(value, str) else _outcome_texts(value)
    if caplog is not None:
        texts.append(caplog.text)
    leaks = [
        (sentinel, index)
        for sentinel in sentinels
        for index, text in enumerate(texts)
        if sentinel in text
    ]
    if leaks:
        locations = ", ".join(f"{sentinel!r} in text #{index}" for sentinel, index in leaks)
        pytest.fail(f"secret sentinel leaked into the step outcome or logs: {locations}")
