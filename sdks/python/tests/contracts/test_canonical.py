"""Tests for normative contract canonicalization and request hashing."""

from __future__ import annotations

import math
from typing import Any

import pytest
from syntara_plugin.contracts import (
    CanonicalJsonError,
    canonical_json_bytes,
    canonical_json_digest,
    container_workload_request_digest,
)


def _request() -> dict[str, Any]:
    """Return a compact generic request with already-populated hash fields."""
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "ContainerWorkloadRequest",
        "workItem": {
            "inputs": {"title": "Example"},
            "requestHash": "sha256:" + "a" * 64,
        },
        "policySnapshot": {
            "effectiveCapabilities": ["network.github-api"],
            "requestHash": "sha256:" + "b" * 64,
        },
    }


def test_canonical_json_uses_stable_compact_key_ordering() -> None:
    """Mapping insertion order cannot change a contract digest."""
    first = {"z": [True, None], "a": {"number": 1}}
    second = {"a": {"number": 1}, "z": [True, None]}

    assert canonical_json_bytes(first) == b'{"a":{"number":1},"z":[true,null]}'
    assert canonical_json_digest(first) == canonical_json_digest(second)


def test_canonical_json_sorts_keys_by_utf16_code_units() -> None:
    """RFC 8785 Appendix B sorting vector: keys order by UTF-16 code units."""
    value = {
        "€": "Euro Sign",
        "\r": "Carriage Return",
        "דּ": "Hebrew Letter Dalet With Dagesh",
        "1": "One",
        "\U0001f600": "Emoji: Grinning Face",
        "\u0080": "Control",
        "ö": "Latin Small Letter O With Diaeresis",
    }

    assert canonical_json_bytes(value) == (
        b'{"\\r":"Carriage Return","1":"One",'
        b'"\xc2\x80":"Control",'
        b'"\xc3\xb6":"Latin Small Letter O With Diaeresis",'
        b'"\xe2\x82\xac":"Euro Sign",'
        b'"\xf0\x9f\x98\x80":"Emoji: Grinning Face",'
        b'"\xef\xac\xb3":"Hebrew Letter Dalet With Dagesh"}'
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0"),
        (-0.0, "0"),
        (1, "1"),
        (1.0, "1"),
        (1.0000000000000000000000001, "1"),
        (20, "20"),
        (20.0, "20"),
        (123, "123"),
        (130.0, "130"),
        (2500, "2500"),
        (1e-6, "0.000001"),
        (1e-7, "1e-7"),
        (1e20, "100000000000000000000"),
        (1e21, "1e+21"),
        (9007199254740992, "9007199254740992"),
        (1000000000000000000000, "1e+21"),
        (1.5, "1.5"),
        (2.45, "2.45"),
        (1.5e300, "1.5e+300"),
        (-1.7976931348623157e308, "-1.7976931348623157e+308"),
    ],
)
def test_canonical_json_serializes_numbers_per_ecmascript(
    value: float | int, expected: str
) -> None:
    """RFC 8785 Appendix B number vectors: ECMAScript Number::toString form."""
    assert canonical_json_bytes(value) == expected.encode("utf-8")


def test_canonical_json_treats_json_one_and_one_point_zero_as_the_same_number() -> None:
    """A formatting-only numeric edit cannot change a compiled digest."""
    assert canonical_json_bytes({"maximum": 1}) == canonical_json_bytes({"maximum": 1.0})
    assert canonical_json_digest({"maximum": 1}) == canonical_json_digest({"maximum": 1.0})


@pytest.mark.parametrize(
    "value",
    [
        2**53 + 1,
        -(2**53 + 1),
        10**400,
        -(10**400),
        10**21 + 1,
    ],
)
def test_canonical_json_rejects_integers_a_double_cannot_carry_exactly(value: int) -> None:
    """RFC 8785's I-JSON model: unrepresentable integers have no canonical form.

    Silently rounding them would make distinct documents collide on one digest,
    defeating every hash built on this serializer.
    """
    with pytest.raises(CanonicalJsonError):
        canonical_json_bytes({"value": value})


def test_canonical_json_accepts_powers_of_two_above_two_to_the_fifty_third() -> None:
    """Exact doubles serialize per ECMAScript's shortest round-trip selection."""
    assert canonical_json_bytes(2**53) == b"9007199254740992"
    # ECMAScript picks the shortest decimal whose double value round-trips,
    # which for 2**63 is 9223372036854776000, not the exact digit expansion.
    assert canonical_json_bytes(2**63) == b"9223372036854776000"


@pytest.mark.parametrize("value", [math.nan, {"value": math.inf}, {"set": {"not-json"}}])
def test_canonical_json_rejects_non_json_values(value: object) -> None:
    """Hashes never silently coerce lossy or non-portable Python values."""
    with pytest.raises(CanonicalJsonError):
        canonical_json_bytes(value)


def test_canonical_json_rejects_lone_surrogates() -> None:
    """RFC 8785 excludes unpaired UTF-16 surrogates from canonical output."""
    with pytest.raises(CanonicalJsonError, match="unpaired UTF-16 surrogates"):
        canonical_json_bytes({"key": "\ud800"})
    with pytest.raises(CanonicalJsonError, match="unpaired UTF-16 surrogates"):
        canonical_json_bytes({"\ud800": 1})


def test_canonical_json_rejections_carry_specific_reasons() -> None:
    """The blanket wrapper does not erase the detailed serializer messages."""
    with pytest.raises(CanonicalJsonError, match="not exactly representable"):
        canonical_json_bytes({"value": 2**53 + 1})
    with pytest.raises(CanonicalJsonError, match="exceeds the IEEE 754 double range"):
        canonical_json_bytes({"value": 10**400})
    with pytest.raises(CanonicalJsonError, match="non-finite"):
        canonical_json_bytes({"value": math.inf})


def test_container_request_hash_ignores_only_the_mirrored_hash_fields() -> None:
    """A claimed hash cannot influence its own recomputation."""
    request = _request()
    expected = container_workload_request_digest(request)

    request["workItem"]["requestHash"] = "sha256:" + "c" * 64
    request["policySnapshot"]["requestHash"] = "sha256:" + "d" * 64
    assert container_workload_request_digest(request) == expected

    request["workItem"]["inputs"]["title"] = "Tampered"
    assert container_workload_request_digest(request) != expected
