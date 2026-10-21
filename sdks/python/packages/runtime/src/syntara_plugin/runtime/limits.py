"""Transport-independent invocation capture budgets."""

DEFAULT_MAX_OUTPUT_BYTES = 1_048_576
"""Default bytes retained independently for stdout and stderr by script runners."""


def validate_max_output_bytes(value: int) -> None:
    """Require a positive uint32 byte budget, with no coercion or unlimited sentinel."""

    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 4_294_967_295:
        raise ValueError("max_output_bytes must be an integer between 1 and 4294967295")
