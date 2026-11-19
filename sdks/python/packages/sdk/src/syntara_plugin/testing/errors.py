"""Harness-level usage errors."""

from __future__ import annotations


class HarnessUsageError(ValueError):
    """Raised when the harness is used in a way the SDK contract forbids."""


class NotPythonStepTypeError(HarnessUsageError):
    """Raised when an entry point cannot resolve to a Python step type.

    The shared contract treats ``spec.runtime.entryPoint`` as opaque, so even
    a ``module:Class``-shaped identifier may belong to another language
    adapter. This error states that the Python harness cannot load the entry
    point (wrong shape, or its module is absent from the workspace), not that
    the target's language is known.
    """
