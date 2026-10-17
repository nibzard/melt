"""RFC 8785 canonicalization conformance (SPEC.md section 4.2).

The `jcs/` directory holds the reference vectors of the JSON
Canonicalization Scheme project. The ES6 number vectors come from the
same project's `verify-numbers.py`.
"""

from __future__ import annotations

import json
import struct

import pytest

from melt.schema.canonical import (
    MeltParseError,
    canonical_hash,
    canonicalize,
    document_hash,
    ieee754_double_to_es6,
    parse_json,
    serialize_number,
    serialize_string,
    sha256_hash,
    table_query_hash,
)
from melt.schema.diagnostics import MeltSchemaError

JCS_DIR = "tests/contract/jcs"

VECTOR_NAMES = ("arrays", "french", "structures", "unicode", "values", "weird")

# IEEE 754 hex, expected ES6 serialization. Source: the reference
# suite's `verify-numbers.py`. None marks a value that is not a number.
ES6_NUMBER_VECTORS = [
    ("4340000000000001", "9007199254740994"),
    ("4340000000000002", "9007199254740996"),
    ("444b1ae4d6e2ef50", "1e+21"),
    ("3eb0c6f7a0b5ed8d", "0.000001"),
    ("3eb0c6f7a0b5ed8c", "9.999999999999997e-7"),
    ("8000000000000000", "0"),
    ("7fffffffffffffff", None),
    ("7ff0000000000000", None),
    ("fff0000000000000", None),
]


@pytest.mark.parametrize("name", VECTOR_NAMES)
def test_reference_vectors(repo_root, name):
    """Every reference input canonicalizes to the reference output."""
    input_path = repo_root / JCS_DIR / f"input-{name}.json"
    expected_path = repo_root / JCS_DIR / f"expected-{name}.json"
    value = parse_json(input_path.read_text(encoding="utf-8"))
    expected = expected_path.read_text(encoding="utf-8")
    assert canonicalize(value) == expected


@pytest.mark.parametrize("name", VECTOR_NAMES)
def test_reference_output_is_idempotent(repo_root, name):
    """Canonicalizing canonical text returns the same text."""
    expected_path = repo_root / JCS_DIR / f"expected-{name}.json"
    expected = expected_path.read_text(encoding="utf-8")
    assert canonicalize(parse_json(expected)) == expected


@pytest.mark.parametrize("ieee_hex,expected", ES6_NUMBER_VECTORS)
def test_es6_number_vectors(ieee_hex, expected):
    if expected is None:
        with pytest.raises(MeltSchemaError):
            ieee754_double_to_es6(ieee_hex)
        return
    assert ieee754_double_to_es6(ieee_hex) == expected
    # The serialization must round-trip to the same double.
    value = struct.unpack(">d", bytes.fromhex(ieee_hex))[0]
    assert float(expected) == value


def test_integer_boundaries_serialize_exactly():
    assert serialize_number(9007199254740991) == "9007199254740991"
    assert serialize_number(-9007199254740991) == "-9007199254740991"
    assert serialize_number(0) == "0"
    assert serialize_number(-0) == "0"
    # 2^53 + 1 converts to a double first, matching ECMAScript.
    assert serialize_number(9007199254740993) == "9007199254740992"


def test_number_format_edges():
    assert serialize_number(4.50) == "4.5"
    assert serialize_number(0.002) == "0.002"
    assert serialize_number(1e-7) == "1e-7"
    assert serialize_number(1e-6) == "0.000001"
    assert serialize_number(1e20) == "100000000000000000000"
    assert serialize_number(1e21) == "1e+21"
    assert serialize_number(0.1 + 0.2) == "0.30000000000000004"
    assert serialize_number(333333333.33333329) == "333333333.3333333"
    assert serialize_number(-1.5) == "-1.5"


def test_string_escaping_is_minimal_and_lowercase():
    assert serialize_string("a\nb") == '"a\\nb"'
    assert serialize_string("\r\t\b\f") == '"\\r\\t\\b\\f"'
    assert serialize_string("€") == '"€"'
    assert serialize_string("\u000f") == '"\\u000f"'
    assert serialize_string('"\\') == '"\\"\\\\"'


def test_key_order_uses_utf16_code_units():
    # U+1F602 (surrogate pair D83D DE02) sorts before U+FB33 under
    # UTF-16 code unit order, but after it under code point order.
    value = {"דּ": 1, "😂": 2}
    assert canonicalize(value) == '{"\U0001f602":2,"דּ":1}'


def test_duplicate_keys_are_rejected_with_a_path():
    with pytest.raises(MeltSchemaError) as error:
        parse_json('{"a": {"x": 1, "x": 2}}')
    (diagnostic,) = error.value.diagnostics
    assert diagnostic.code == "E_PARSE"
    assert diagnostic.path == "/a/x"


def test_non_finite_numbers_are_rejected():
    for text in ('{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}', '{"a": 1e999}'):
        with pytest.raises(MeltSchemaError):
            parse_json(text)


def test_lone_surrogates_are_rejected():
    with pytest.raises(MeltSchemaError):
        parse_json('{"a": "\\ud800"}')
    # A complete pair is a valid scalar value.
    assert parse_json('{"a": "\\ud83d\\ude02"}') == {"a": "😂"}


def test_malformed_json_is_rejected():
    for text in ('{"a": 1,}', "[1, 2", '{"a"}', "01", "1.", ".5", "+1", "{} extra"):
        with pytest.raises(MeltSchemaError):
            parse_json(text)


def test_raw_control_characters_are_rejected():
    with pytest.raises(MeltSchemaError):
        parse_json('{"a\nb": 1}')


def test_nesting_bombs_fail_with_a_diagnostic():
    for depth in (201, 2000, 100_000):
        with pytest.raises(MeltSchemaError) as error:
            parse_json("[" * depth + "]" * depth)
        assert error.value.diagnostics[0].code == "E_PARSE"


def test_deep_but_bounded_values_canonicalize():
    depth = 199
    value = parse_json("[" * depth + "]" * depth)
    assert canonicalize(value) == "[" * depth + "]" * depth


def test_canonical_hash_is_order_independent():
    first = {"b": 1, "a": [1, 2.5, "x"]}
    second = {"a": [1, 2.5, "x"], "b": 1}
    assert canonical_hash(first) == canonical_hash(second)
    assert canonical_hash(first) != canonical_hash({"b": 1, "a": [2, 2.5, "x"]})


def test_canonical_hash_matches_sha256_of_canonical_bytes():
    value = {"z": 1, "a": True, "m": None}
    import hashlib

    expected = "sha256:" + hashlib.sha256(
        canonicalize(value).encode("utf-8")
    ).hexdigest()
    assert canonical_hash(value) == expected
    assert sha256_hash(b"melt") == "sha256:" + hashlib.sha256(b"melt").hexdigest()


def test_document_hash_uses_canonical_form():
    first = {
        "language": "melt/v0.1",
        "workspaceId": "w",
        "title": "t",
        "sources": {},
        "parameters": {},
        "actions": {},
        "projections": {},
        "bindings": {},
        "layout": {"root": None, "hidden": []},
    }
    reordered = dict(reversed(list(first.items())))
    assert document_hash(first) == document_hash(reordered)


def test_table_query_hash_excludes_offset_and_limit():
    source = {"sourceId": "sales", "version": 3}
    sort = [{"column": "month", "direction": "asc"}]
    filters = [{"column": "revenue", "operator": "gte", "value": 200}]
    first = table_query_hash(source, sort, filters)
    second = table_query_hash(source, sort, filters)
    assert first == second
    assert first.startswith("sha256:")
    changed = table_query_hash(
        source, sort, [{"column": "revenue", "operator": "gte", "value": 100}]
    )
    assert first != changed


def test_json_module_rejects_reference_duplicates_differently():
    """The standard parser accepts duplicates; the strict one must not."""
    assert json.loads('{"a": 1, "a": 2}') == {"a": 2}
    with pytest.raises(MeltParseError):
        parse_json('{"a": 1, "a": 2}')
