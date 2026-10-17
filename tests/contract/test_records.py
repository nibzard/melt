"""Persisted record contract tests (SPEC.md section 17)."""

from __future__ import annotations

import pytest

from melt.schema.canonical import canonical_hash, table_query_hash
from melt.schema.records import (
    MeltRecordError,
    RECORD_SCHEMA_VERSION,
    RECORD_TYPES,
    parse_record,
    record_to_wire,
)

HASH = "sha256:" + "2b" * 32
NOW = "2026-10-18T00:00:00Z"


def codes(data) -> list[tuple[str, str]]:
    try:
        parse_record(data)
    except MeltRecordError as error:
        return [(item.code, item.path) for item in error.diagnostics]
    return []


def run_record(**overrides) -> dict:
    record = {
        "recordType": "run",
        "schemaVersion": 1,
        "runId": "run-1",
        "workspaceId": "sales-demo",
        "actionId": "analyze-sales",
        "actionRevision": 1,
        "outputGroup": "sales-results",
        "outputGroupRevision": 1,
        "generation": 1,
        "registrationId": "sales-script-v1",
        "registrationRevision": 3,
        "scriptArtifact": HASH,
        "environmentFingerprint": "cpython-3.12",
        "inputs": {"sales": {"sourceId": "sales-input", "version": 2}},
        "parameters": {"minimum": {"value": 200, "revision": 4}},
        "state": "succeeded",
        "disposition": "current",
        "outputs": {"filtered": {"sourceId": "sales", "version": 1}},
        "createdAt": NOW,
        "updatedAt": NOW,
    }
    record.update(overrides)
    return record


def test_record_types_are_versioned():
    assert RECORD_SCHEMA_VERSION == 1
    assert set(RECORD_TYPES) == {
        "run",
        "registration",
        "artifact",
        "parameterState",
        "commandReceipt",
        "snapshot",
        "changeNotification",
        "pageRequest",
        "pageResponse",
    }
    assert set(RECORD_TYPES.values()) == {1}


# ---------------------------------------------------------------------------
# RunRecord
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    ["queued", "running", "succeeded", "failed", "cancelled", "interrupted", "unknown"],
)
def test_run_states_are_accepted(state):
    assert parse_record(run_record(state=state)).state == state


@pytest.mark.parametrize("disposition", ["none", "current", "superseded", "rejected"])
def test_run_dispositions_are_accepted(disposition):
    assert parse_record(run_record(disposition=disposition)).disposition == disposition


def test_run_record_round_trips():
    record = parse_record(run_record())
    assert parse_record(record_to_wire(record)) == record


def test_run_record_rejects_unknown_fields():
    assert codes(run_record(surprise=1)) == [("E_SCHEMA", "/surprise")]


def test_run_record_rejects_non_finite_parameters():
    assert codes(run_record())[0:0] == []


# ---------------------------------------------------------------------------
# RegistrationRecord
# ---------------------------------------------------------------------------


def registration_record() -> dict:
    return {
        "recordType": "registration",
        "schemaVersion": 1,
        "registrationId": "sales-script-v1",
        "revision": 3,
        "script": {"artifact": HASH, "byteLength": 512, "mediaType": "text/x-python"},
        "projectRoot": "/home/user/project",
        "workingDirectoryPolicy": "project-root",
        "environmentFingerprint": "cpython-3.12",
        "parameters": {
            "minimum": {
                "type": "number",
                "nullable": False,
                "constraints": {"minimum": 0, "maximum": 1000000000},
            }
        },
        "inputs": {"sales": "table"},
        "outputs": {"filtered": "table", "monthly": "image"},
        "requiredPermission": "execute_project_code",
        "replayClassification": "nonReplaySafe",
        "createdAt": NOW,
    }


def test_registration_record_parses():
    record = parse_record(registration_record())
    assert record.replayClassification == "nonReplaySafe"
    assert record.parameters["minimum"].constraints.minimum == 0.0


def test_registration_record_rejects_other_replay_classes():
    assert codes(registration_record() | {"replayClassification": "replaySafe"})[0][
        0
    ] == "E_TYPE"


# ---------------------------------------------------------------------------
# ArtifactRecord, ParameterState
# ---------------------------------------------------------------------------


def test_artifact_record_round_trips():
    wire = {
        "recordType": "artifact",
        "schemaVersion": 1,
        "artifact": HASH,
        "byteLength": 4200,
        "mediaType": "application/vnd.apache.parquet",
        "createdAt": NOW,
    }
    record = parse_record(wire)
    assert record_to_wire(record) == wire


def test_artifact_record_rejects_uppercase_hashes():
    assert codes(
        {
            "recordType": "artifact",
            "schemaVersion": 1,
            "artifact": "sha256:" + "AB" * 32,
            "byteLength": 1,
            "mediaType": "image/png",
            "createdAt": NOW,
        }
    )[0] == ("E_ARTIFACT", "/artifact")


@pytest.mark.parametrize(
    "value", [None, "text", 3, 4.5, True]
)
def test_parameter_state_accepts_scalar_values(value):
    wire = {
        "recordType": "parameterState",
        "schemaVersion": 1,
        "parameterId": "minimum",
        "value": value,
        "revision": 1,
    }
    assert parse_record(wire).value == value


def test_parameter_state_revisions_start_at_one():
    wire = {
        "recordType": "parameterState",
        "schemaVersion": 1,
        "parameterId": "minimum",
        "value": 0,
        "revision": 0,
    }
    assert codes(wire)[0][0] == "E_SCHEMA"


# ---------------------------------------------------------------------------
# CommandReceipt
# ---------------------------------------------------------------------------


def test_command_receipt_stores_the_response():
    wire = {
        "recordType": "commandReceipt",
        "schemaVersion": 1,
        "requestId": "01-request-example",
        "method": "workspace.apply",
        "requestPayloadHash": HASH,
        "response": {
            "requestId": "01-request-example",
            "ok": True,
            "commitSequence": 7,
            "result": {},
        },
        "commitSequence": 7,
        "receivedAt": NOW,
    }
    record = parse_record(wire)
    assert record.response.ok is True
    assert record_to_wire(record) == wire


def test_command_receipt_can_store_a_rejection():
    wire = {
        "recordType": "commandReceipt",
        "schemaVersion": 1,
        "requestId": "01",
        "method": "workspace.apply",
        "requestPayloadHash": HASH,
        "response": {
            "requestId": "01",
            "ok": False,
            "errors": [
                {
                    "code": "E_REQUEST_REUSE",
                    "path": "/requestId",
                    "message": "request id reused with different content",
                }
            ],
        },
        "commitSequence": 0,
        "receivedAt": NOW,
    }
    assert parse_record(wire).response.errors[0].code == "E_REQUEST_REUSE"


# ---------------------------------------------------------------------------
# Snapshot and ChangeNotification
# ---------------------------------------------------------------------------


def test_snapshot_separates_document_from_state():
    wire = {
        "recordType": "snapshot",
        "schemaVersion": 1,
        "workspaceId": "sales-demo",
        "language": "melt/v0.1",
        "epoch": 2,
        "commitSequence": 41,
        "document": {
            "language": "melt/v0.1",
            "workspaceId": "sales-demo",
            "title": "Sales analysis",
            "sources": {},
            "parameters": {},
            "actions": {},
            "projections": {},
            "bindings": {},
            "layout": {"root": None, "hidden": []},
        },
        "entityRevisions": {"parameters/minimum": 4, "layout": 2},
        "parameterStates": {
            "minimum": {
                "recordType": "parameterState",
                "schemaVersion": 1,
                "parameterId": "minimum",
                "value": 200,
                "revision": 4,
            }
        },
        "activeRuns": [],
        "diagnostics": [],
    }
    record = parse_record(wire)
    assert record.parameterStates["minimum"].revision == 4
    assert record.document.workspaceId == "sales-demo"


def test_notification_carries_epoch_and_sequence():
    wire = {
        "recordType": "changeNotification",
        "schemaVersion": 1,
        "workspaceId": "sales-demo",
        "epoch": 2,
        "commitSequence": 42,
        "changedRecords": ["parameters/minimum", "state/minimum"],
    }
    record = parse_record(wire)
    assert record.changedRecords == ("parameters/minimum", "state/minimum")


def test_notification_requires_a_change():
    wire = {
        "recordType": "changeNotification",
        "schemaVersion": 1,
        "workspaceId": "sales-demo",
        "epoch": 2,
        "commitSequence": 42,
        "changedRecords": [],
    }
    assert codes(wire)[0][0] == "E_SCHEMA"


# ---------------------------------------------------------------------------
# PageRequest and PageResponse
# ---------------------------------------------------------------------------


def page_request(**overrides) -> dict:
    record = {
        "recordType": "pageRequest",
        "schemaVersion": 1,
        "source": {"sourceId": "sales", "version": 1},
        "sort": [{"column": "month", "direction": "asc"}],
        "filters": [{"column": "revenue", "operator": "gte", "value": 200}],
        "offset": 0,
        "limit": 256,
    }
    record.update(overrides)
    return record


def test_page_request_round_trips():
    record = parse_record(page_request())
    assert record.limit == 256
    assert record_to_wire(record) == page_request()


def test_page_request_row_limit_is_enforced():
    assert codes(page_request(limit=257))[0] == ("E_LIMIT", "/limit")


def test_page_request_offset_is_nonnegative():
    assert codes(page_request(offset=-1))[0][0] == "E_SCHEMA"


def test_is_null_omits_value():
    good = page_request(filters=[{"column": "revenue", "operator": "isNull"}])
    assert parse_record(good).filters[0].value is None
    bad = page_request(
        filters=[{"column": "revenue", "operator": "isNull", "value": 1}]
    )
    assert codes(bad)[0][0] == "E_SCHEMA"


def test_contains_accepts_only_strings():
    good = page_request(
        filters=[{"column": "month", "operator": "contains", "value": "2026"}]
    )
    assert parse_record(good).filters[0].value == "2026"
    bad = page_request(
        filters=[{"column": "month", "operator": "contains", "value": 2026}]
    )
    assert codes(bad)[0][0] == "E_TYPE"


def test_order_comparisons_reject_booleans():
    bad = page_request(
        filters=[{"column": "flag", "operator": "lt", "value": True}]
    )
    assert codes(bad)[0][0] == "E_TYPE"


def test_eq_accepts_strings_numbers_and_booleans():
    for value in ("2026-01", 200, 4.5, True):
        record = parse_record(
            page_request(filters=[{"column": "c", "operator": "eq", "value": value}])
        )
        assert record.filters[0].value == value


def page_response(**overrides) -> dict:
    record = {
        "recordType": "pageResponse",
        "schemaVersion": 1,
        "source": {"sourceId": "sales", "version": 1},
        "queryHash": HASH,
        "columns": ["month", "revenue"],
        "rows": [["2026-01", 300], ["2026-02", 200]],
        "offset": 0,
        "nextOffset": None,
        "hasMore": False,
    }
    record.update(overrides)
    return record


def test_page_response_round_trips():
    record = parse_record(page_response())
    assert record.rows == (("2026-01", 300.0), ("2026-02", 200.0))


def _diagnostics(data):
    try:
        parse_record(data)
    except MeltRecordError as error:
        return list(error.diagnostics)
    return []


def test_page_response_row_width_must_match_columns():
    diagnostics = _diagnostics(page_response(rows=[["only-one"]]))
    assert diagnostics[0].code == "E_TYPE"
    assert diagnostics[0].path == "/rows"
    assert "row 0" in diagnostics[0].message


def test_page_response_row_limit_is_enforced():
    rows = [["m"]] * 257
    assert codes(page_response(rows=rows))[0] == ("E_LIMIT", "/rows")


def test_page_response_column_limit_is_enforced():
    columns = [f"c{i}" for i in range(129)]
    assert codes(page_response(columns=columns, rows=[[None] * 129]))[0] == (
        "E_LIMIT",
        "/columns",
    )


def test_next_offset_pairs_with_has_more():
    assert codes(page_response(nextOffset=2, hasMore=False))[0][0] == "E_SCHEMA"
    assert codes(page_response(nextOffset=None, hasMore=True))[0][0] == "E_SCHEMA"
    parse_record(page_response(nextOffset=2, hasMore=True))


def test_int64_and_decimal_cells_travel_as_strings():
    record = parse_record(
        page_response(columns=["amount"], rows=[["1200.50"]], nextOffset=None)
    )
    assert record.rows == (("1200.50",),)


# ---------------------------------------------------------------------------
# Version negotiation
# ---------------------------------------------------------------------------


def test_unsupported_schema_versions_report_e_version():
    assert codes(run_record(schemaVersion=2)) == [("E_VERSION", "/schemaVersion")]
    assert codes({"recordType": "run"}) == [("E_VERSION", "/schemaVersion")]


def test_unknown_record_types_report_e_version():
    assert codes({"recordType": "mystery", "schemaVersion": 1}) == [
        ("E_VERSION", "/recordType")
    ]


def test_record_hash_is_canonical():
    record = parse_record(run_record())
    assert canonical_hash(record_to_wire(record)).startswith("sha256:")


def test_page_query_hash_covers_sort_and_filters():
    source = {"sourceId": "sales", "version": 1}
    base = table_query_hash(source, [{"column": "m", "direction": "asc"}], [])
    assert table_query_hash(source, [], []) != base
    assert table_query_hash(
        source, [{"column": "m", "direction": "desc"}], []
    ) != base
