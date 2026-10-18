"""Permission grant records (T014, SPEC.md sections 15.1 and 17).

Grants are versioned persisted records that live outside every
exported document. The record model enforces the kind-specific scope:
a specific-run grant pins the intent it covers, and a project
execution grant covers a whole trusted registration and pins nothing.
"""

from __future__ import annotations

import pytest

from melt.schema.records import (
    RECORD_TYPES,
    MeltRecordError,
    PermissionGrantRecord,
    parse_record,
    record_to_wire,
)

GRANTED_AT = "2026-10-18T00:00:00Z"
REVOKED_AT = "2026-10-18T01:00:00Z"


def project_grant(**overrides) -> dict:
    grant = {
        "recordType": "permissionGrant",
        "schemaVersion": 1,
        "grantId": "meltgrant_project01",
        "workspaceId": "sales-demo",
        "kind": "projectExecution",
        "requiredPermission": "execute_project_code",
        "registrationId": "reg-abc123",
        "grantedBy": "host-permission-control/run-approval",
        "grantedAt": GRANTED_AT,
    }
    grant.update(overrides)
    return grant


def specific_grant(**overrides) -> dict:
    grant = {
        "recordType": "permissionGrant",
        "schemaVersion": 1,
        "grantId": "meltgrant_specific01",
        "workspaceId": "sales-demo",
        "kind": "specificRun",
        "requiredPermission": "execute_project_code",
        "registrationId": "reg-abc123",
        "registrationRevision": 1,
        "actionId": "analyze-sales",
        "actionRevision": 1,
        "outputGroup": "sales-results",
        "intentId": "meltintent_01",
        "grantedBy": "host-permission-control/run-approval",
        "grantedAt": GRANTED_AT,
    }
    grant.update(overrides)
    return grant


def codes_of(data: dict) -> list[tuple[str, str]]:
    with pytest.raises(MeltRecordError) as error:
        parse_record(data)
    return [(item.code, item.path) for item in error.value.diagnostics]


def test_permission_grant_is_a_versioned_record_type():
    assert RECORD_TYPES["permissionGrant"] == 1
    record = parse_record(project_grant())
    assert isinstance(record, PermissionGrantRecord)
    assert record.kind == "projectExecution"
    assert record.revokedAt is None


def test_grants_round_trip_through_the_wire_form():
    for wire in (project_grant(), specific_grant()):
        record = parse_record(wire)
        # The wire form carries unset optional fields as nulls; parsing
        # it back yields the same record.
        assert parse_record(record_to_wire(record)) == record


def test_a_specific_run_grant_pins_its_intent():
    record = parse_record(specific_grant())
    assert record.intentId == "meltintent_01"
    assert record.actionRevision == 1
    assert record.generation is None
    # The scheduling dimensions stay optional until T016 pins them.
    pinned = parse_record(
        specific_grant(outputGroupRevision=3, generation=7)
    )
    assert (pinned.outputGroupRevision, pinned.generation) == (3, 7)


def test_a_specific_run_grant_requires_its_intent_pins():
    for missing in (
        "actionId",
        "actionRevision",
        "registrationRevision",
        "outputGroup",
        "intentId",
    ):
        grant = specific_grant()
        del grant[missing]
        assert codes_of(grant)[0] == ("E_SCHEMA", "/"), missing


def test_a_project_grant_pins_no_single_intent():
    pins = {
        "intentId": "meltintent_01",
        "actionId": "analyze-sales",
        "actionRevision": 1,
        "registrationRevision": 1,
        "outputGroup": "sales-results",
        "outputGroupRevision": 1,
        "generation": 1,
    }
    for pinned, value in pins.items():
        grant = project_grant()
        grant[pinned] = value
        assert codes_of(grant)[0] == ("E_SCHEMA", "/"), pinned


def test_revocation_is_part_of_the_record():
    record = parse_record(
        project_grant(revokedAt=REVOKED_AT, revokedBy="host-permission-control/revoke")
    )
    assert record.revokedAt == REVOKED_AT
    assert record.revokedBy == "host-permission-control/revoke"


def test_unknown_grant_kinds_and_fields_are_rejected():
    assert codes_of(project_grant(kind="forever"))[0] == ("E_TYPE", "/kind")
    assert codes_of(project_grant(permission="execute"))[0] == (
        "E_SCHEMA",
        "/permission",
    )
    assert codes_of(project_grant(schemaVersion=2))[0] == (
        "E_VERSION",
        "/schemaVersion",
    )
    assert codes_of({"recordType": "permission", "schemaVersion": 1})[0] == (
        "E_VERSION",
        "/recordType",
    )
