"""Normative RFC 8785 canonical JSON and generic-container request hashing helpers."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal
from typing import Any

# ECMAScript switches to exponent notation only above this decimal magnitude
# (Number::toString step 6: n > 21) and below 1e-7 (step 8: n <= -6).
_ECMASCRIPT_EXPONENT_UPPER = 21
_ECMASCRIPT_EXPONENT_LOWER = -6
# Unicode noncharacters: the U+FDD0..U+FDEF block and the two code points
# ending in U+FFFE or U+FFFF on every plane. RFC 8785 builds on the I-JSON
# input model, which excludes them from string values and member names.
_NONCHARACTER_FIRST = 0xFDD0
_NONCHARACTER_LAST = 0xFDEF
# Python's json.dumps with ensure_ascii=False applies exactly the RFC 8785
# string escaping rules: the two-character escapes for backslash, quote, and
# the short control names, plus \u00XX for every other control character.
_JSON_STRING = json.JSONEncoder(ensure_ascii=False, check_circular=False).encode


class CanonicalJsonError(ValueError):
    """A value cannot be represented by the Syntara canonical JSON form."""


def canonical_json_bytes(value: object) -> bytes:
    """Return the RFC 8785 (JCS) serialization of one JSON value.

    Keys are sorted by UTF-16 code unit, numbers use ECMAScript
    ``Number::toString`` form, and strings use the minimal JCS escaping. The
    result is UTF-8 without a trailing newline. Other language SDKs must
    implement RFC 8785 exactly before they calculate a compatible digest.
    """

    fragments: list[str] = []
    try:
        _serialize(value, fragments)
        return "".join(fragments).encode("utf-8")
    except CanonicalJsonError:
        raise
    except UnicodeEncodeError as error:
        raise CanonicalJsonError(
            "strings must not contain unpaired UTF-16 surrogates"
        ) from error
    except (TypeError, ValueError, RecursionError) as error:
        raise CanonicalJsonError("value is not canonical JSON") from error


def canonical_json_digest(value: object) -> str:
    """Return the SHA-256 digest of one canonical JSON value."""

    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"


def container_workload_request_digest(request: Mapping[str, Any]) -> str:
    """Hash a container workload request without its two mirrored hash fields.

    The work-item and policy snapshot each carry the resulting digest. Removing
    both fields before hashing avoids a circular representation while ensuring
    every other execution, policy, and runtime value is committed.
    """

    try:
        canonical_request = deepcopy(dict(request))
        for section_name in ("workItem", "policySnapshot"):
            section = canonical_request.get(section_name)
            if isinstance(section, Mapping):
                section_copy = dict(section)
                section_copy.pop("requestHash", None)
                canonical_request[section_name] = section_copy
    except (TypeError, ValueError) as error:
        raise CanonicalJsonError("container workload request is not canonical JSON") from error
    return canonical_json_digest(canonical_request)


def _serialize(value: object, fragments: list[str]) -> None:
    if value is None:
        fragments.append("null")
    elif value is True:
        fragments.append("true")
    elif value is False:
        fragments.append("false")
    elif isinstance(value, str):
        _validate_string(value)
        fragments.append(_JSON_STRING(value))
    elif isinstance(value, int):
        fragments.append(_ecmascript_integer(value))
    elif isinstance(value, float):
        fragments.append(_ecmascript_number(value))
    elif isinstance(value, Mapping):
        _serialize_object(value, fragments)
    elif isinstance(value, (list, tuple)):
        _serialize_array(value, fragments)
    else:
        raise CanonicalJsonError(f"value of type {type(value).__name__} is not JSON")


def _serialize_object(value: Mapping[Any, Any], fragments: list[str]) -> None:
    """Append one object with keys ordered by UTF-16 code unit."""

    fragments.append("{")
    try:
        items = sorted(value.items(), key=lambda item: _sort_key(item[0]))
    except TypeError as error:
        raise CanonicalJsonError("object keys must be strings") from error
    for index, (key, item) in enumerate(items):
        if index:
            fragments.append(",")
        _validate_string(key)
        fragments.append(_JSON_STRING(key))
        fragments.append(":")
        _serialize(item, fragments)
    fragments.append("}")


def _serialize_array(value: list[Any] | tuple[Any, ...], fragments: list[str]) -> None:
    """Append one array, preserving element order."""

    fragments.append("[")
    for index, item in enumerate(value):
        if index:
            fragments.append(",")
        _serialize(item, fragments)
    fragments.append("]")


def _validate_string(value: str) -> None:
    """Reject Unicode noncharacters per the I-JSON input model (RFC 7493)."""

    for character in value:
        code = ord(character)
        if (
            _NONCHARACTER_FIRST <= code <= _NONCHARACTER_LAST
            or (code & 0xFFFE) == 0xFFFE
        ):
            raise CanonicalJsonError(
                f"strings must not contain the Unicode noncharacter U+{code:04X}"
            )


def _sort_key(key: object) -> bytes:
    """Return the RFC 8785 object-key ordering key: UTF-16 code units."""

    if not isinstance(key, str):
        raise TypeError("object keys must be strings")
    return key.encode("utf-16-be")


def _ecmascript_integer(value: int) -> str:
    """Serialize one integer per its exact IEEE 754 double representation.

    RFC 8785 builds on the I-JSON input model: a number the double format
    cannot carry exactly has no canonical form. Python keeps arbitrary
    precision integers, so an integer that rounds (every value above 2**53
    that is not a power-of-two multiple) or overflows the double range is
    rejected instead of silently hashed as a different number.
    """

    try:
        as_double = float(value)
    except OverflowError as error:
        raise CanonicalJsonError(
            f"integer {value} exceeds the IEEE 754 double range"
        ) from error
    # int() recovers the double's value and differs when that conversion rounded.
    if int(as_double) != value:
        raise CanonicalJsonError(
            f"integer {value} is not exactly representable as an IEEE 754 double"
        )
    return _ecmascript_number(as_double)


def _ecmascript_number(value: float) -> str:
    """Serialize one finite float per ECMAScript ``Number::toString``.

    RFC 8785 section 3.2.2.3 requires the ECMAScript serialization, in which
    ``1e21`` gains an exponent while ``1e20`` does not, integral values drop
    the trailing ``.0``, and negative zero collapses to ``0``. Python ints are
    routed through this form as well so that JSON ``1`` and ``1.0`` produce
    the same canonical bytes, as they do in an ECMAScript implementation.
    """

    if math.isnan(value) or math.isinf(value):
        raise CanonicalJsonError("non-finite numbers cannot be canonical JSON")
    # ±0.0 are the only falsy finite floats, so negative zero collapses to "0".
    if not value:
        return "0"
    negative = value < 0
    # repr() is Python's shortest round-trip form, which matches the shortest
    # digit sequence the ECMAScript algorithm selects for IEEE 754 doubles.
    decimal = Decimal(repr(abs(value)))
    _, raw_digits, exponent = decimal.as_tuple()
    assert isinstance(exponent, int)
    digits = "".join(str(digit) for digit in raw_digits).rstrip("0") or "0"
    length = len(digits)
    # The decimal point sits after n significant digits: value = digits * 10**(n - length).
    point = exponent + len(raw_digits)
    if length <= point <= _ECMASCRIPT_EXPONENT_UPPER:
        rendered = digits + "0" * (point - length)
    elif 0 < point <= _ECMASCRIPT_EXPONENT_UPPER:
        rendered = f"{digits[:point]}.{digits[point:]}"
    elif _ECMASCRIPT_EXPONENT_LOWER < point <= 0:
        rendered = f"0.{'0' * -point}{digits}"
    elif length == 1:
        rendered = f"{digits}e{_exponent(point - 1)}"
    else:
        rendered = f"{digits[0]}.{digits[1:]}e{_exponent(point - 1)}"
    return f"-{rendered}" if negative else rendered


def _exponent(power: int) -> str:
    return f"{'+' if power >= 0 else '-'}{abs(power)}"
