"""Sentinel-based secret-leak assertions for step test outcomes."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import TYPE_CHECKING
from uuid import uuid4

from .invocation import StepOutcome

if TYPE_CHECKING:
    # Only the annotation needs pytest; the function core stays importable
    # without it, and any object with a ``text`` attribute works at runtime.
    from pytest import LogCaptureFixture


class SecretLeakError(AssertionError):
    """Raised when a sentinel secret appears where it must not."""


def secret_sentinel(prefix: str = "syntara-test-secret-") -> str:
    """Return one unique fake secret value for leak assertions."""

    return f"{prefix}{uuid4().hex}"


def _safe_repr(value: object) -> str:
    """Render one value defensively; a broken ``__repr__`` must not break scanning."""

    try:
        return repr(value)
    except Exception:  # noqa: BLE001 - never let a custom repr raise past the scan
        return f"<unrepresentable {type(value).__name__}>"


def _safe_str(value: object) -> str:
    """Render one value's str defensively."""

    try:
        return str(value)
    except Exception:  # noqa: BLE001 - never let a custom __str__ raise past the scan
        return f"<unprintable {type(value).__name__}>"


def _exception_texts(error: BaseException, seen: set[int]) -> list[str]:
    """Collect every text one exception and its chain carry.

    Covers the message, args, notes, and the explicit (``__cause__``) and
    implicit (``__context__``) chains; the visited set stops cycles, and each
    value renders through safe repr/str so payloads hidden in a custom
    ``__repr__`` that discards them are still scanned through ``args``.
    """

    if id(error) in seen:
        return []
    seen.add(id(error))
    texts = [_safe_repr(error), _safe_str(error)]
    texts.extend(_safe_repr(arg) for arg in error.args)
    for note in getattr(error, "__notes__", None) or ():
        texts.append(_safe_repr(note))
    for chained in (error.__cause__, error.__context__):
        if chained is not None:
            texts.extend(_exception_texts(chained, seen))
    return texts


def _outcome_texts(outcome: StepOutcome) -> list[tuple[str, str]]:
    """Collect every caller-visible text an outcome could leak a secret into."""

    texts: list[tuple[str, str]] = []
    if outcome.status_message:
        texts.append(("status message", outcome.status_message))
    if outcome.error is not None:
        texts.append(("error message", outcome.error.message))
    if outcome.raised is not None:
        for text in _exception_texts(outcome.raised, set()):
            texts.append(("raised exception", text))
    if outcome.output is not None:
        texts.append(("business output", json.dumps(outcome.output, default=str, sort_keys=True)))
    if outcome.raw is not None:
        texts.append(("raw envelope", repr(outcome.raw)))
    return texts


def assert_no_secrets(
    value: StepOutcome | str,
    sentinels: Iterable[str],
    *,
    caplog: LogCaptureFixture | None = None,
) -> None:
    """Fail when any sentinel secret appears in an outcome or captured logs.

    Scans the outcome's status message, error message, raised exception
    including its whole chain (message, args, notes, ``__cause__``, and
    ``__context__``), serialized business output, raw envelope, and (when
    supplied) the pytest ``caplog`` capture. Secrets must never appear in any
    of them.

    When scanning logs, raise the capture level around the invocation first:
    pytest captures only records that pass the root logger's effective level,
    which defaults to WARNING. Use ``with caplog.at_level(logging.INFO):``
    around the step invocation so INFO-level leaks are captured.

    The failure message never repeats the sentinel value: it names the
    sentinel by position and length and the location it leaked into, so CI
    output cannot echo the secret through the report itself. Leaks raise
    :class:`SecretLeakError`, an ``AssertionError`` subclass, so the helper
    fails tests under any runner and the function core needs no pytest.

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
        raise SecretLeakError(
            f"secret sentinel leaked into the step outcome or logs: {locations}"
        )
