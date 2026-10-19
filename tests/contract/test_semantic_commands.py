"""Extension operations of the command envelope (T025, RFC 001 4, 5.2).

`melt/semantic-draft-1` adds two entity collections and one state
operation to the mutation vocabulary:

- `entity.create`, `entity.replace`, and `entity.delete` over
  `collections` and `state`,
- `state.set`, the reference assignment a typed event binding will
  drive (RFC 001 section 5.2 names the same effect kind).

The envelope parses them under either language; the workspace service
refuses them for a `melt/v0.1` workspace, which the integration suite
proves. Here the contract is the wire shape: sorted diagnostics with
precise pointers, a reference that must carry its incarnation, and an
entity value that must match its declared collection.
"""

from __future__ import annotations

import pytest

from melt.schema.commands import (
    MeltCommandError,
    command_semantic_diagnostics,
    parse_command,
)


def envelope(operations, expected=None):
    body = {
        "protocolVersion": 1,
        "workspaceId": "supplier-demo",
        "requestId": "r1",
        "method": "workspace.apply",
        "operations": operations,
    }
    if expected is not None:
        body["expected"] = expected
    return body


def errors_of(operations):
    with pytest.raises(MeltCommandError) as raised:
        parse_command(envelope(operations))
    return [(item.code, item.path) for item in raised.value.diagnostics]


REFERENCE = {
    "collection": "suppliers",
    "incarnation": "inc-issued-by-service",
    "key": "S-42",
}

COLLECTION_VALUE = {
    "source": {"sourceId": "supplier-records", "version": 3},
    "key": "supplier_id",
}

STATE_VALUE = {
    "type": {"kind": "ref", "collection": "suppliers"},
    "nullable": True,
    "default": None,
    "scope": "workspace",
}


def test_state_set_carries_one_reference_or_null():
    parsed = parse_command(
        envelope(
            [{"op": "state.set", "id": "selected_supplier", "value": REFERENCE}],
            expected={"refstate/selected_supplier": 2},
        )
    )
    operation = parsed.operations[0]
    assert operation.op == "state.set"
    assert operation.value.collection == "suppliers"
    assert operation.value.incarnation == "inc-issued-by-service"
    assert operation.value.key == "S-42"


def test_state_set_clears_with_null():
    parsed = parse_command(
        envelope([{"op": "state.set", "id": "selected_supplier", "value": None}])
    )
    assert parsed.operations[0].value is None


def test_a_reference_requires_its_incarnation():
    missing = dict(REFERENCE)
    del missing["incarnation"]
    assert ("E_SCHEMA", "/operations/0/value/incarnation") in errors_of(
        [{"op": "state.set", "id": "selected_supplier", "value": missing}]
    )


def test_a_reference_key_may_be_any_string():
    parsed = parse_command(
        envelope(
            [{"op": "state.set", "id": "s", "value": dict(REFERENCE, key="")}]
        )
    )
    assert parsed.operations[0].value.key == ""


def test_collection_entities_parse_and_type_check():
    parsed = parse_command(
        envelope(
            [
                {
                    "op": "entity.create",
                    "collection": "collections",
                    "id": "suppliers",
                    "value": COLLECTION_VALUE,
                },
                {
                    "op": "entity.create",
                    "collection": "state",
                    "id": "selected_supplier",
                    "value": STATE_VALUE,
                },
            ]
        )
    )
    assert parsed.operations[0].value.source.version == 3
    assert parsed.operations[1].value.type.collection == "suppliers"


def test_collection_entity_rejects_a_parameter_value():
    assert ("E_TYPE", "/operations/0/value") in errors_of(
        [
            {
                "op": "entity.create",
                "collection": "collections",
                "id": "suppliers",
                "value": {
                    "type": "number",
                    "default": 0,
                    "nullable": False,
                    "constraints": {},
                },
            }
        ]
    )


def test_state_entity_rejects_a_collection_value():
    assert ("E_TYPE", "/operations/0/value") in errors_of(
        [
            {
                "op": "entity.create",
                "collection": "state",
                "id": "selected_supplier",
                "value": COLLECTION_VALUE,
            }
        ]
    )


def test_state_entity_requires_the_extension_fields():
    assert errors_of(
        [
            {
                "op": "entity.create",
                "collection": "state",
                "id": "selected_supplier",
                "value": {"kind": "table", "title": "t", "source": None,
                          "config": {"columns": None, "selection": "none"},
                          "controls": []},
            }
        ]
    )


def test_workspace_create_accepts_a_semantic_document():
    document = {
        "language": "melt/semantic-draft-1",
        "workspaceId": "supplier-demo",
        "title": "Suppliers",
        "sources": {},
        "parameters": {},
        "actions": {},
        "projections": {},
        "bindings": {},
        "collections": {},
        "state": {},
        "layout": {"root": None, "hidden": []},
    }
    parsed = parse_command(
        {
            "protocolVersion": 1,
            "workspaceId": "supplier-demo",
            "requestId": "r2",
            "method": "workspace.create",
            "payload": {"document": document},
        }
    )
    diagnostics = command_semantic_diagnostics(parsed)
    assert diagnostics == []


def test_workspace_create_reports_semantic_document_diagnostics():
    document = {
        "language": "melt/semantic-draft-1",
        "workspaceId": "supplier-demo",
        "title": "Suppliers",
        "sources": {},
        "parameters": {},
        "actions": {},
        "projections": {},
        "bindings": {},
        "collections": {
            "suppliers": {
                "source": {"sourceId": "supplier-records", "version": 3},
                "key": "supplier_id",
            }
        },
        "state": {},
        "layout": {"root": None, "hidden": []},
    }
    with pytest.raises(MeltCommandError) as raised:
        parse_command(
            {
                "protocolVersion": 1,
                "workspaceId": "supplier-demo",
                "requestId": "r3",
                "method": "workspace.create",
                "payload": {"document": document},
            }
        )
    assert (
        "E_REFERENCE",
        "/payload/document/collections/suppliers/source/sourceId",
    ) in [(item.code, item.path) for item in raised.value.diagnostics]


def test_workspace_create_still_rejects_unknown_v01_fields():
    document = {
        "language": "melt/v0.1",
        "workspaceId": "supplier-demo",
        "title": "Suppliers",
        "sources": {},
        "parameters": {},
        "actions": {},
        "projections": {},
        "bindings": {},
        "collections": {},
        "layout": {"root": None, "hidden": []},
    }
    with pytest.raises(MeltCommandError) as raised:
        parse_command(
            {
                "protocolVersion": 1,
                "workspaceId": "supplier-demo",
                "requestId": "r4",
                "method": "workspace.create",
                "payload": {"document": document},
            }
        )
    assert ("E_SCHEMA", "/payload/document/collections") in [
        (item.code, item.path) for item in raised.value.diagnostics
    ]
