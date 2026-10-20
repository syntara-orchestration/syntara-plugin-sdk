"""Harness-level usage errors."""

from __future__ import annotations


class HarnessUsageError(ValueError):
    """Raised when the harness is used in a way the SDK contract forbids."""
