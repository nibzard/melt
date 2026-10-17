"""RFC 8785 canonicalization and canonical hashing (SPEC.md section 4.2).

This module provides:

- A strict JSON reader. It rejects duplicate object keys, `NaN` and
  infinity, values that overflow to infinity, lone surrogates, raw
  control characters, leading zeros, and trailing data. Every failure
  carries a JSON Pointer path.
- An RFC 8785 serializer (JSON Canonicalization Scheme). It sorts
  object keys by UTF-16 code unit, preserves array order, and formats
  numbers with ECMAScript `Number::toString` semantics.
- Canonical SHA-256 helpers, including the document hash and the table
  query hash from SPEC.md section 12.7.

Conformance fixtures for this module live in `tests/contract/jcs/`.
They are the reference test vectors of the JSON Canonicalization
Scheme project, which RFC 8785 appendix H points at.
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
from typing import Any, Iterable, Mapping, Sequence

from melt.schema.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    MeltSchemaError,
    with_code,
)

# Any JSON value tree that the strict reader returns. The alias exists
# for documentation; the reader returns plain Python values.
JsonValue = Any

_SAFE_INTEGER = 2**53
# Nesting cap for decoded input. Melt documents nest far below this;
# the cap exists so a nesting bomb fails with a diagnostic instead of
# exhausting the interpreter stack (SPEC.md section 11.2, stage 1).
_MAX_NESTING = 200

_WHITESPACE = " \t\n\r"
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_STRING_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
_SHORT_ESCAPES = {
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


class MeltParseError(MeltSchemaError):
    """Raised when input is not well-formed JSON."""

    def __init__(self, path: str, message: str, *, offset: int | None = None):
        text = f"{message} at byte {offset}" if offset is not None else message
        super().__init__(
            (Diagnostic(code=DiagnosticCode.PARSE, path=path, message=text),)
        )


def _check_scalar_string(value: str, path: str) -> None:
    """Reject strings that do not hold valid Unicode scalar values."""
    for character in value:
        if 0xD800 <= ord(character) <= 0xDFFF:
            raise MeltParseError(
                path,
                "string holds a lone surrogate, which is not a valid"
                " Unicode scalar value",
            )


class _Parser:
    """Recursive descent JSON reader with path tracking."""

    def __init__(self, text: str):
        self._text = text
        self._length = len(text)

    def parse(self) -> Any:
        if self._text.startswith("﻿"):
            raise MeltParseError("", "byte order mark is not allowed", offset=0)
        value = self._value([], 0)
        end = self._skip_whitespace(value[1])
        if end != self._length:
            raise MeltParseError(
                "", "trailing data after the JSON value", offset=end
            )
        return value[0]

    def _check_depth(self, path: list, index: int, depth: int) -> None:
        if depth > _MAX_NESTING:
            raise MeltParseError(
                _pointer(path),
                f"JSON nesting exceeds {_MAX_NESTING} levels",
                offset=index,
            )

    def _skip_whitespace(self, index: int) -> int:
        while index < self._length and self._text[index] in _WHITESPACE:
            index += 1
        return index

    def _fail(self, path: list, index: int, message: str) -> None:
        raise MeltParseError(_pointer(path), message, offset=index)

    def _value(self, path: list, index: int, depth: int = 0) -> tuple[Any, int]:
        index = self._skip_whitespace(index)
        if index >= self._length:
            self._fail(path, index, "unexpected end of input")
        character = self._text[index]
        if character == "{":
            self._check_depth(path, index, depth + 1)
            return self._object(path, index, depth + 1)
        if character == "[":
            self._check_depth(path, index, depth + 1)
            return self._array(path, index, depth + 1)
        if character == '"':
            return self._string(path, index)
        if character == "t" and self._text.startswith("true", index):
            return True, index + 4
        if character == "f" and self._text.startswith("false", index):
            return False, index + 5
        if character == "n" and self._text.startswith("null", index):
            return None, index + 4
        if character == "-" or character.isdigit():
            return self._number(path, index)
        self._fail(path, index, f"unexpected character {character!r}")

    def _object(self, path: list, index: int, depth: int) -> tuple[dict, int]:
        start = index
        index = self._skip_whitespace(index + 1)
        result: dict[str, Any] = {}
        if index < self._length and self._text[index] == "}":
            return result, index + 1
        while True:
            index = self._skip_whitespace(index)
            if index >= self._length or self._text[index] != '"':
                self._fail(path, index, "expected an object key")
            key, index = self._string(path, index)
            if key in result:
                raise MeltParseError(
                    _pointer(path + [key]),
                    f"duplicate object key {key!r}",
                    offset=start,
                )
            index = self._skip_whitespace(index)
            if index >= self._length or self._text[index] != ":":
                self._fail(path, index, "expected ':' after an object key")
            value, index = self._value(path + [key], index + 1, depth)
            result[key] = value
            index = self._skip_whitespace(index)
            if index >= self._length:
                self._fail(path, index, "unterminated object")
            if self._text[index] == ",":
                index += 1
                continue
            if self._text[index] == "}":
                return result, index + 1
            self._fail(path, index, "expected ',' or '}' in an object")

    def _array(self, path: list, index: int, depth: int) -> tuple[list, int]:
        index = self._skip_whitespace(index + 1)
        result: list[Any] = []
        if index < self._length and self._text[index] == "]":
            return result, index + 1
        position = 0
        while True:
            value, index = self._value(path + [position], index, depth)
            result.append(value)
            position += 1
            index = self._skip_whitespace(index)
            if index >= self._length:
                self._fail(path, index, "unterminated array")
            if self._text[index] == ",":
                index += 1
                continue
            if self._text[index] == "]":
                return result, index + 1
            self._fail(path, index, "expected ',' or ']' in an array")

    def _string(self, path: list, index: int) -> tuple[str, int]:
        # Opening quote is at `index`.
        index += 1
        chunks: list[str] = []
        while index < self._length:
            character = self._text[index]
            if character == '"':
                value = "".join(chunks)
                _check_scalar_string(value, _pointer(path))
                return value, index + 1
            if character == "\\":
                index = self._escape(path, chunks, index)
                continue
            if ord(character) < 0x20:
                self._fail(
                    path,
                    index,
                    f"raw control character U+{ord(character):04X} in a string",
                )
            chunks.append(character)
            index += 1
        self._fail(path, index, "unterminated string")

    def _escape(self, path: list, chunks: list[str], index: int) -> int:
        escape_index = index + 1
        if escape_index >= self._length:
            self._fail(path, escape_index, "unterminated escape sequence")
        escape = self._text[escape_index]
        if escape == "u":
            code = self._hex_quad(path, escape_index + 1)
            index = escape_index + 5
            if 0xD800 <= code <= 0xDBFF:
                if not self._text.startswith("\\u", index):
                    self._fail(
                        path,
                        index,
                        "high surrogate is not followed by a low surrogate",
                    )
                low = self._hex_quad(path, index + 2)
                if not 0xDC00 <= low <= 0xDFFF:
                    self._fail(
                        path,
                        index,
                        "high surrogate is not followed by a low surrogate",
                    )
                chunks.append(
                    chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00))
                )
                return index + 6
            if 0xDC00 <= code <= 0xDFFF:
                self._fail(path, escape_index, "lone low surrogate in a string")
            chunks.append(chr(code))
            return index
        if escape in _STRING_ESCAPES:
            chunks.append(_STRING_ESCAPES[escape])
            return escape_index + 1
        self._fail(path, escape_index, f"invalid escape sequence \\{escape}")

    def _hex_quad(self, path: list, index: int) -> int:
        digits = self._text[index : index + 4]
        if len(digits) != 4 or any(
            character not in "0123456789abcdefABCDEF" for character in digits
        ):
            self._fail(path, index, "invalid \\u escape sequence")
        return int(digits, 16)

    def _number(self, path: list, index: int) -> tuple[int | float, int]:
        match = _NUMBER.match(self._text, index)
        if match is None or match.start() != index:
            self._fail(path, index, "invalid number")
        token = match.group(0)
        end = match.end()
        # Reject "-0"? JSON allows -0. Reject "01"? The regex already
        # only allows a leading zero alone. Reject "1." and ".5": the
        # regex requires digits after the dot, and a bare dot never
        # starts a match.
        if "." in token or "e" in token or "E" in token:
            value = float(token)
            if not math.isfinite(value):
                self._fail(path, index, f"number {token!r} is not finite")
            return value, end
        return int(token), end


def _pointer(path: Iterable) -> str:
    parts: list[str] = []
    for part in path:
        text = part if isinstance(part, str) else str(part)
        parts.append(text.replace("~", "~0").replace("/", "~1"))
    return "/" + "/".join(parts)


def parse_json(data: "str | bytes | bytearray") -> Any:
    """Parse JSON strictly and return the value tree.

    The reader rejects malformed input, duplicate keys, non-finite
    numbers, and strings without valid Unicode scalar values.
    """

    if isinstance(data, (bytes, bytearray)):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise MeltParseError("", f"input is not valid UTF-8: {error}") from error
    else:
        text = data
    return _Parser(text).parse()


def _check_nesting(value: Any, depth: int, path: str) -> None:
    if depth > _MAX_NESTING:
        raise MeltSchemaError(
            (
                Diagnostic(
                    code=DiagnosticCode.PARSE,
                    path=path,
                    message=f"JSON nesting exceeds {_MAX_NESTING} levels",
                ),
            )
        )
    if isinstance(value, dict):
        for key, item in value.items():
            _check_nesting(item, depth + 1, f"{path}/{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_nesting(item, depth + 1, f"{path}/{index}")


def validate_json_tree(value: Any, path: str = "") -> None:
    """Check that a value is a plain JSON tree.

    Use this on values that enter through `Any` typed fields, such as
    command payloads and command results. It rejects non-finite
    numbers, strings without valid Unicode scalar values, and any
    non-JSON object.
    """

    _check_nesting(value, 0, path)
    _check_json_value(value, path)


def _check_json_value(value: Any, path: str) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MeltSchemaError(
                _scalar_error(path, "number is not finite", DiagnosticCode.TYPE)
            )
        return
    if isinstance(value, str):
        _check_scalar_string(value, path)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_json_value(item, f"{path}/{index}")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise MeltSchemaError(
                    _scalar_error(path, "object key is not a string")
                )
            _check_scalar_string(key, path)
            _check_json_value(item, f"{path}/{key}")
        return
    raise MeltSchemaError(
        _scalar_error(path, f"value of type {type(value).__name__} is not JSON")
    )


def _scalar_error(path: str, message: str, code: str = DiagnosticCode.SCHEMA):
    return (
        Diagnostic(
            code=code,
            path=path,
            message=message,
        ),
    )


def serialize_number(value: "int | float") -> str:
    """Serialize one number with ECMAScript `Number::toString` rules.

    This is the number serialization that RFC 8785 section 3.2.2.3
    requires. Integers at or below 2^53 in magnitude print exactly.
    Larger integers convert to IEEE 754 doubles first, matching
    ECMAScript `JSON.parse`.
    """

    if isinstance(value, bool):
        raise MeltSchemaError(_scalar_error("", "boolean is not a number"))
    if isinstance(value, int):
        if -_SAFE_INTEGER <= value <= _SAFE_INTEGER:
            return str(value)
        value = float(value)
    if not isinstance(value, float):
        raise MeltSchemaError(_scalar_error("", "value is not a JSON number"))
    if not math.isfinite(value):
        raise MeltSchemaError(_scalar_error("", "number is not finite"))
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    digits, count, point = _split_decimal(repr(-value if value < 0 else value))
    if digits == "":
        return "0"
    if count <= point <= 21:
        return sign + digits + "0" * (point - count)
    if 0 < point <= 21:
        return sign + digits[:point] + "." + digits[point:]
    if -6 < point <= 0:
        return sign + "0." + "0" * (-point) + digits
    exponent = point - 1
    exponent_text = f"+{exponent}" if exponent >= 0 else str(exponent)
    if count == 1:
        return sign + digits + "e" + exponent_text
    return sign + digits[0] + "." + digits[1:] + "e" + exponent_text


def _split_decimal(text: str) -> tuple[str, int, int]:
    """Split a Python float repr into digits, digit count, and point.

    The returned triple `(digits, count, point)` follows the ECMAScript
    notation: the value equals `digits` times `10 ** (point - count)`.
    """

    mantissa, _, exponent_text = text.partition("e")
    exponent = int(exponent_text) if exponent_text else 0
    integer_part, _, fraction_part = mantissa.partition(".")
    if fraction_part and float(mantissa).is_integer():
        # Python repr writes integral floats with a `.0` suffix. That
        # zero is padding, not a significant digit.
        fraction_part = ""
    raw_digits = integer_part + fraction_part
    stripped = raw_digits.lstrip("0")
    leading = len(raw_digits) - len(stripped)
    point = len(integer_part) + exponent - leading
    return stripped, len(stripped), point


def serialize_string(value: str) -> str:
    r"""Serialize one string with minimal JSON escaping.

    Only `"`, `\`, and control characters below 0x20 are escaped.
    Every other character is written as-is, per RFC 8785. Escaped
    control characters use lowercase hexadecimal digits.
    """

    chunks = ['"']
    for character in value:
        code = ord(character)
        if 0xD800 <= code <= 0xDFFF:
            raise MeltSchemaError(
                _scalar_error("", "lone surrogate is not a Unicode scalar value")
            )
        if character == '"':
            chunks.append('\\"')
        elif character == "\\":
            chunks.append("\\\\")
        elif character in _SHORT_ESCAPES:
            chunks.append(_SHORT_ESCAPES[character])
        elif code < 0x20:
            chunks.append(f"\\u{code:04x}")
        else:
            chunks.append(character)
    chunks.append('"')
    return "".join(chunks)


def _sort_key(key: str) -> bytes:
    # RFC 8785 sorts on UTF-16 code units. Big-endian UTF-16 bytes give
    # the same order under bytewise comparison.
    return key.encode("utf-16-be")


def canonicalize(value: Any) -> str:
    """Return the RFC 8785 canonical JSON text of a value tree."""

    validate_json_tree(value)
    return _canonicalize(value)


def _canonicalize(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return serialize_string(value)
    if isinstance(value, (int, float)):
        return serialize_number(value)
    if isinstance(value, list):
        return "[" + ",".join(_canonicalize(item) for item in value) + "]"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda item: _sort_key(item[0]))
        return (
            "{"
            + ",".join(
                f"{serialize_string(key)}:{_canonicalize(item)}"
                for key, item in items
            )
            + "}"
        )
    raise MeltSchemaError(
        _scalar_error("", f"value of type {type(value).__name__} is not JSON")
    )


def canonical_bytes(value: Any) -> bytes:
    """Return the UTF-8 encoding of the canonical JSON text."""
    return canonicalize(value).encode("utf-8")


def sha256_hash(data: bytes) -> str:
    """Return the lowercase artifact hash string for raw bytes."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_hash(value: Any) -> str:
    """Return the artifact hash of the canonical JSON encoding."""
    return sha256_hash(canonical_bytes(value))


def model_to_wire(model: Any) -> dict:
    """Convert a Pydantic model into its wire-shaped value tree."""
    return model.model_dump(mode="json", by_alias=True)


def document_hash(document: "Any") -> str:
    """Return the canonical hash of a document value tree or model.

    The hash covers definitions only. Runtime state and permission
    grants live outside the document, so they cannot enter the hash
    (SPEC.md section 4.2).
    """

    value = model_to_wire(document) if hasattr(document, "model_dump") else document
    return canonical_hash(value)


def table_query_hash(
    source: Mapping,
    sort: "Sequence[Mapping] | None",
    filters: "Sequence[Mapping] | None",
) -> str:
    """Return the query hash of a table page request.

    The hash covers the source, the sort keys, and the filters. It
    excludes the offset and the limit (SPEC.md section 12.7).
    """

    query = {
        "source": dict(source),
        "sort": [dict(item) for item in sort or ()],
        "filters": [dict(item) for item in filters or ()],
    }
    return canonical_hash(query)


def ieee754_double_to_es6(hex_digits: str) -> str:
    """Format the IEEE 754 double whose hex digits are given.

    Use this helper in tests to drive the ES6 number vectors.
    """

    value = struct.unpack(">d", bytes.fromhex(hex_digits))[0]
    return serialize_number(value)


__all__ = [
    "MeltParseError",
    "canonical_bytes",
    "canonical_hash",
    "canonicalize",
    "document_hash",
    "ieee754_double_to_es6",
    "model_to_wire",
    "parse_json",
    "serialize_number",
    "serialize_string",
    "sha256_hash",
    "table_query_hash",
    "validate_json_tree",
    "with_code",
]
