"""Default limit table tests (SPEC.md section 15.2)."""

from __future__ import annotations

import pytest

from melt.schema.commands import parse_command, parse_payload
from melt.schema.diagnostics import DiagnosticCode, MeltSchemaError
from melt.schema.document import validate_document
from melt.schema.limits import DEFAULT_LIMITS, ServiceLimits, limit_diagnostic


def test_default_limits_match_the_specification():
    limits = DEFAULT_LIMITS
    assert limits.control_frame_bytes == 1024 * 1024
    assert limits.operations_per_mutation == 128
    assert limits.max_projections == 32
    assert limits.max_parameters == 256
    assert limits.max_actions == 64
    assert limits.max_bindings == 256
    assert limits.max_controls_per_projection == 32
    assert limits.max_layout_depth == 8
    assert limits.table_page_rows == 256
    assert limits.table_page_columns == 128
    assert limits.table_page_bytes == 1024 * 1024
    assert limits.native_table_cache_bytes == 64 * 1024 * 1024
    assert limits.png_max_axis == 16384
    assert limits.png_max_total_pixels == 64_000_000
    assert limits.displayed_text_bytes == 10 * 1024 * 1024
    assert limits.run_log_bytes_per_stream == 10 * 1024 * 1024
    assert limits.staged_output_bytes_per_run == 1024 * 1024 * 1024


def test_limits_are_frozen_service_policy():
    limits = ServiceLimits()
    assert limits == DEFAULT_LIMITS
    try:
        limits.max_projections = 1000  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("service limits must stay frozen")


def test_limit_diagnostic_names_the_resource_and_bound():
    diagnostic = limit_diagnostic("operations per mutation", 128, "/operations")
    assert diagnostic.code == DiagnosticCode.LIMIT
    assert diagnostic.path == "/operations"
    assert "operations per mutation" in diagnostic.message
    assert "128" in diagnostic.message


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


def operation(count: int) -> dict:
    return {
        "protocolVersion": 1,
        "workspaceId": "sales-demo",
        "requestId": "01-limits",
        "method": "workspace.apply",
        "operations": [
            {"op": "parameter.set", "id": "minimum", "value": 1}
        ]
        * count,
    }


def over_limit_document(collection: str, count: int) -> dict:
    import copy

    document = copy.deepcopy(EMPTY_DOCUMENT)
    if collection == "projections":
        document["projections"] = {
            f"pane-{index}": {
                "kind": "document",
                "title": "Notes",
                "source": None,
                "config": {"format": "plain"},
                "controls": [],
            }
            for index in range(count)
        }
    return document


def error_of(action) -> MeltSchemaError:
    with pytest.raises(MeltSchemaError) as error:
        action()
    return error.value


def test_enforced_limits_name_the_resource_and_the_bound():
    """Every enforced limit reports `E_LIMIT` with both parts named.

    SPEC.md section 15.2 requires the error to name the resource and
    the allowed bound. The cases below walk one enforcement point of
    each limit the schema and command layers own today.
    """

    # Operations per mutation (bound 128).
    error = error_of(lambda: parse_command(operation(129)))
    assert error.diagnostics[0].code == "E_LIMIT"
    assert error.diagnostics[0].path == "/operations"
    assert "operations per mutation" in error.diagnostics[0].message
    assert "128" in error.diagnostics[0].message

    # Table page rows (bound 256).
    error = error_of(
        lambda: parse_payload(
            "table.page",
            {"source": {"sourceId": "sales", "version": 1}, "offset": 0, "limit": 257},
        )
    )
    assert error.diagnostics[0].code == "E_LIMIT"
    assert error.diagnostics[0].path == "/payload/limit"
    assert "table page rows" in error.diagnostics[0].message
    assert "256" in error.diagnostics[0].message

    # Projections (bound 32).
    diagnostics = validate_document(over_limit_document("projections", 33))
    limit = next(item for item in diagnostics if item.code == "E_LIMIT")
    assert limit.path == "/projections"
    assert "projections" in limit.message
    assert "32" in limit.message

    # Controls per projection (bound 32).
    document = over_limit_document("projections", 1)
    document["projections"]["pane-0"]["controls"] = [
        {"id": f"button-{index}", "kind": "button", "label": "Run"}
        for index in range(33)
    ]
    diagnostics = validate_document(document)
    limit = next(item for item in diagnostics if item.code == "E_LIMIT")
    assert limit.path == "/projections/pane-0/controls"
    assert "controls per projection" in limit.message
    assert "32" in limit.message
