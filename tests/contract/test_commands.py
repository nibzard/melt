"""Command envelope and framing contract tests (SPEC.md section 12)."""

from __future__ import annotations

import io
import json

import pytest

from melt.schema.commands import (
    MeltCommandError,
    decode_frame,
    encode_frame,
    parse_command,
    parse_payload,
    parse_response,
    read_frame,
)
from melt.schema.diagnostics import MeltSchemaError

EMPTY_DOCUMENT = {
    "language": "melt/v0.1",
    "workspaceId": "sales-demo",
    "title": "Sales analysis",
    "sources": {},
    "parameters": {},
    "actions": {},
    "projections": {},
    "bindings": {},
    "layout": {"root": None, "hidden": []},
}


def envelope(**overrides) -> dict:
    result = {
        "protocolVersion": 1,
        "workspaceId": "sales-demo",
        "requestId": "01-request-example",
        "method": "workspace.apply",
        "expected": {"parameters/minimum": 1, "layout": 1},
        "operations": [],
    }
    result.update(overrides)
    return result


def codes(data) -> list[tuple[str, str]]:
    try:
        parse_command(data)
    except MeltCommandError as error:
        return [(item.code, item.path) for item in error.diagnostics]
    return []


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def test_apply_envelope_with_operations_parses():
    parsed = parse_command(
        envelope(
            operations=[
                {
                    "op": "entity.create",
                    "collection": "parameters",
                    "id": "minimum",
                    "value": {
                        "type": "number",
                        "default": 0,
                        "nullable": False,
                        "constraints": {"minimum": 0},
                    },
                },
                {"op": "parameter.set", "id": "minimum", "value": 200},
                {"op": "layout.replace", "value": {"root": None, "hidden": []}},
            ]
        )
    )
    assert parsed.requestId == "01-request-example"
    assert parsed.expected == {"parameters/minimum": 1, "layout": 1}
    assert parsed.operations[0].value.type == "number"
    assert parsed.operations[1].value == 200.0


def test_unknown_envelope_field_is_rejected():
    assert codes(envelope(surprise=1)) == [("E_SCHEMA", "/surprise")]


def test_unsupported_protocol_version_is_rejected():
    assert codes(envelope(protocolVersion=2)) == [("E_VERSION", "/protocolVersion")]


def test_unknown_method_is_rejected():
    assert codes(envelope(method="workspace.explode"))[0][0] == "E_TYPE"


def test_apply_cannot_carry_a_payload():
    assert codes(envelope(payload={}))[0] == ("E_SCHEMA", "/")


def test_payload_methods_cannot_carry_operations():
    request = envelope(
        method="session.hello",
        operations=[{"op": "parameter.set", "id": "minimum", "value": 1}],
    )
    request.pop("expected", None)
    request["payload"] = {
        "protocolVersions": [1],
        "languageVersions": ["melt/v0.1"],
        "clientVersion": "harness",
    }
    assert ("E_SCHEMA", "/") == codes(request)[0]


def test_payload_methods_require_a_payload():
    request = envelope(method="session.hello", operations=None, expected=None)
    assert "E_SCHEMA" in (code for code, _ in codes(request))


def test_request_ids_are_bounded():
    request = envelope(requestId="x" * 129)
    assert codes(request)[0] == ("E_SCHEMA", "/requestId")


def test_expected_revisions_are_nonnegative_safe_integers():
    request = envelope(expected={"parameters/minimum": -1})
    assert codes(request)[0][0] == "E_SCHEMA"
    request = envelope(expected={"parameters/minimum": 2**53})
    # JSON Pointer escapes the slash inside the record path key.
    assert codes(request)[0] == ("E_TYPE", "/expected/parameters~1minimum")


def test_parameter_set_rejects_unsafe_integer_values():
    """parameter.set rejects unsafe integers before rounding (T037).

    Regression: a value of `9007199254740993` once parsed to the float
    `9007199254740992.0`, so the service committed a rounded value
    (SPEC.md section 4.2 rejects instead of coercing).
    """

    request = envelope(
        operations=[{"op": "parameter.set", "id": "minimum", "value": 2**53 + 1}]
    )
    assert ("E_TYPE", "/operations/0/value") in codes(request)
    request = envelope(
        operations=[{"op": "parameter.set", "id": "minimum", "value": -(2**53)}]
    )
    assert ("E_TYPE", "/operations/0/value") in codes(request)


def test_parameter_set_scalar_failure_collapses_to_one_diagnostic():
    """One rejected parameter.set value reports one diagnostic (T040).

    Regression: the value failed every `JsonScalar` branch, so the
    command reported three sibling `E_TYPE` diagnostics at
    `/operations/0/value`, one per branch.
    """

    request = envelope(
        operations=[{"op": "parameter.set", "id": "minimum", "value": 2**53 + 1}]
    )
    with pytest.raises(MeltCommandError) as raised:
        parse_command(request)
    diagnostics = raised.value.diagnostics
    assert [(item.code, item.path) for item in diagnostics] == [
        ("E_TYPE", "/operations/0/value")
    ]
    assert "safe integer range" in diagnostics[0].message


def test_parameter_set_accepts_safe_integer_bounds():
    parsed = parse_command(
        envelope(
            operations=[
                {"op": "parameter.set", "id": "minimum", "value": 2**53 - 1},
                {"op": "parameter.set", "id": "minimum", "value": -(2**53 - 1)},
            ]
        )
    )
    assert [operation.value for operation in parsed.operations] == [
        9007199254740991.0,
        -9007199254740991.0,
    ]


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def test_operations_per_mutation_are_limited():
    operation = {"op": "parameter.set", "id": "minimum", "value": 1}
    request = envelope(operations=[operation] * 129)
    assert codes(request)[0] == ("E_LIMIT", "/operations")


def test_entity_value_must_match_the_collection():
    request = envelope(
        operations=[
            {
                "op": "entity.create",
                "collection": "bindings",
                "id": "b",
                "value": {
                    "type": "number",
                    "default": 0,
                    "nullable": False,
                    "constraints": {},
                },
            }
        ]
    )
    assert codes(request) == [("E_TYPE", "/operations/0/value")]


def test_entity_delete_needs_no_value():
    parsed = parse_command(
        envelope(
            operations=[{"op": "entity.delete", "collection": "bindings", "id": "b"}]
        )
    )
    assert parsed.operations[0].op == "entity.delete"


def test_unknown_operation_is_rejected():
    request = envelope(operations=[{"op": "entity.rename", "id": "x"}])
    assert codes(request)[0][0] in ("E_TYPE", "E_SCHEMA")


def test_parameter_set_accepts_null_and_scalars():
    parsed = parse_command(
        envelope(
            operations=[
                {"op": "parameter.set", "id": "a", "value": None},
                {"op": "parameter.set", "id": "b", "value": "text"},
                {"op": "parameter.set", "id": "c", "value": True},
            ]
        )
    )
    assert [operation.value for operation in parsed.operations] == [None, "text", True]


def test_parameter_set_rejects_non_finite_values():
    text = json.dumps(envelope(operations=[{"op": "parameter.set", "id": "a", "value": 1}]))
    text = text.replace('"value": 1}', '"value": NaN}')
    assert codes(text)[0][0] == "E_PARSE"


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------


def test_session_hello_payload_parses():
    parsed = parse_payload(
        "session.hello",
        {
            "protocolVersions": [1],
            "languageVersions": ["melt/v0.1"],
            "clientVersion": "harness",
        },
    )
    assert parsed.clientVersion == "harness"


def test_workspace_create_embeds_document_validation():
    document = dict(EMPTY_DOCUMENT, language="melt/v0.2")
    with pytest.raises(MeltCommandError) as error:
        parse_payload("workspace.create", {"document": document})
    assert error.value.diagnostics[0].path == "/payload/document/language"
    assert error.value.diagnostics[0].code == "E_VERSION"


def test_table_page_payload_enforces_the_row_limit():
    payload = {
        "source": {"sourceId": "sales", "version": 1},
        "sort": [],
        "filters": [],
        "offset": 0,
        "limit": 257,
    }
    with pytest.raises(MeltCommandError) as error:
        parse_payload("table.page", payload)
    assert error.value.diagnostics[0].code == "E_LIMIT"


def test_subscription_payload_parses():
    parsed = parse_payload(
        "workspace.subscribe", {"afterSequence": 12, "epoch": 3}
    )
    assert parsed.afterSequence == 12


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


def test_ok_response_parses():
    response = parse_response(
        {
            "requestId": "r",
            "ok": True,
            "commitSequence": 4,
            "result": {"source": {"sourceId": "s", "version": 1}},
        }
    )
    assert response.ok is True
    assert response.commitSequence == 4


def test_rejected_response_requires_diagnostics():
    response = parse_response(
        {
            "requestId": "r",
            "ok": False,
            "errors": [
                {
                    "code": "E_CONFLICT",
                    "path": "/parameters/minimum",
                    "message": "expected revision 1",
                }
            ],
        }
    )
    assert response.errors[0].code == "E_CONFLICT"


def test_rejected_response_needs_at_least_one_error():
    with pytest.raises(MeltCommandError):
        parse_response({"requestId": "r", "ok": False, "errors": []})


def test_response_result_rejects_non_json_values():
    text = '{"requestId": "r", "ok": true, "commitSequence": 0, "result": {"x": NaN}}'
    with pytest.raises(MeltCommandError):
        parse_response(text)


def test_response_rejects_unknown_codes():
    with pytest.raises(MeltCommandError):
        parse_response(
            {
                "requestId": "r",
                "ok": False,
                "errors": [{"code": "E_NOPE", "path": "/", "message": "x"}],
            }
        )


# ---------------------------------------------------------------------------
# Framed IPC
# ---------------------------------------------------------------------------


def test_frames_round_trip():
    payload = b'{"method": "workspace.apply"}'
    frame = encode_frame(payload)
    assert frame == len(payload).to_bytes(4, "big") + payload
    decoded, offset = decode_frame(frame)
    assert decoded == payload
    assert offset == len(frame)


def test_multiple_frames_decode_in_order():
    first = encode_frame(b"one")
    second = encode_frame(b"two")
    buffer = first + second
    payload_a, offset = decode_frame(buffer)
    payload_b, offset = decode_frame(buffer, offset=offset)
    assert (payload_a, payload_b) == (b"one", b"two")
    assert offset == len(buffer)


def test_frames_above_the_limit_are_rejected_before_reading():
    frame = encode_frame(b"x" * 100)
    with pytest.raises(MeltCommandError) as error:
        decode_frame(frame, limit=10)
    diagnostic = error.value.diagnostics[0]
    assert diagnostic.code == "E_LIMIT"
    assert "100" in diagnostic.message
    assert "10" in diagnostic.message


def test_encode_rejects_oversized_payloads():
    with pytest.raises(MeltCommandError):
        encode_frame(b"x" * 11, limit=10)


def test_truncated_frames_are_rejected():
    frame = encode_frame(b"payload")
    with pytest.raises(MeltCommandError) as header_error:
        decode_frame(frame[:3])
    assert header_error.value.diagnostics[0].code == "E_PARSE"
    with pytest.raises(MeltCommandError) as body_error:
        decode_frame(frame[:-2])
    assert body_error.value.diagnostics[0].code == "E_PARSE"


def test_read_frame_reads_from_a_stream():
    stream = io.BytesIO(encode_frame(b"streamed") + encode_frame(b"frames"))
    assert read_frame(stream.read) == b"streamed"
    assert read_frame(stream.read) == b"frames"


def test_frame_limit_defaults_to_the_service_limit():
    from melt.schema.limits import DEFAULT_LIMITS

    assert DEFAULT_LIMITS.control_frame_bytes == 1024 * 1024
    header = (2**21).to_bytes(4, "big")
    with pytest.raises(MeltSchemaError):
        decode_frame(header)
