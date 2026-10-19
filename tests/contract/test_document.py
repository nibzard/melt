"""Document model contract tests (SPEC.md sections 4 through 10).

Every test asserts a diagnostic code and a JSON Pointer path, so the
invalid fixtures of backlog task T004 can reuse the same expectations.
"""

from __future__ import annotations

import json

import pytest

from melt.schema.canonical import document_hash, model_to_wire
from melt.schema.document import (
    MeltDocumentError,
    document_semantic_diagnostics,
    parse_document,
    validate_document,
)
from tests.conftest import FIXTURE_HASH

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


def codes(document) -> list[tuple[str, str]]:
    """Return the (code, path) pairs of a document's diagnostics."""
    return [(item.code, item.path) for item in validate_document(document)]


def first_code(document) -> tuple[str, str]:
    pairs = codes(document)
    assert pairs, "expected at least one diagnostic"
    return pairs[0]


def as_json(document) -> str:
    return json.dumps(document)


# ---------------------------------------------------------------------------
# Valid documents
# ---------------------------------------------------------------------------


def test_empty_document_is_valid():
    document = parse_document(EMPTY_DOCUMENT)
    assert document.workspaceId == "sales-demo"
    assert document.sources == {}
    assert document.layout.root is None


def test_sales_example_is_valid(sales_document):
    document = parse_document(as_json(sales_document))
    assert document.parameters["minimum"].default == 0.0
    assert document.projections["revenue"].kind == "plot"
    assert document.bindings["manual-run"].debounceMs == 0
    assert document.sources["sales-input"].versions["1"].schema.columns[
        0
    ].id == "month"


def test_document_hash_is_stable_across_key_order(sales_document):
    reordered = dict(reversed(list(sales_document.items())))
    assert document_hash(parse_document(sales_document)) == document_hash(
        parse_document(reordered)
    )


def test_document_model_round_trips(sales_document):
    """The normalized wire form reparses to an equal model."""
    document = parse_document(sales_document)
    wire = model_to_wire(document)
    assert parse_document(wire) == document
    assert document_hash(parse_document(wire)) == document_hash(document)


def test_equivalent_documents_hash_identically(sales_document):
    """Optional nulls do not change the canonical hash."""
    verbose = json.loads(json.dumps(sales_document))
    verbose["parameters"]["minimum"]["constraints"] = {
        "minimum": 0,
        "maximum": 1000000000,
        "maxLength": None,
        "enum": None,
    }
    assert parse_document(sales_document) == parse_document(verbose)
    assert document_hash(parse_document(sales_document)) == document_hash(
        parse_document(verbose)
    )


def test_hidden_projections_keep_definitions(sales_document_copy):
    sales_document_copy["layout"] = {
        "root": {"kind": "pane", "projection": "sales"},
        "hidden": ["revenue"],
    }
    parse_document(sales_document_copy)


def test_nullable_parameter_defaults_to_null(sales_document_copy):
    sales_document_copy["parameters"]["minimum"] = {
        "type": "string",
        "default": None,
        "nullable": True,
        "constraints": {"enum": ["low", "high"]},
    }
    sales_document_copy["projections"]["revenue"]["controls"][0] = {
        "id": "minimum",
        "kind": "select",
        "label": "Minimum",
        "parameter": "minimum",
        "options": [
            {"value": "low", "label": "Low"},
            {"value": "high", "label": "High"},
        ],
    }
    parse_document(sales_document_copy)


def test_diagnostics_are_sorted_by_path_then_code(sales_document_copy):
    from melt.schema.document import Document

    sales_document_copy["parameters"]["minimum"]["default"] = 2000000001
    sales_document_copy["actions"]["analyze-sales"]["targets"]["monthly"] = "sales"
    diagnostics = document_semantic_diagnostics(
        Document.model_validate(sales_document_copy)
    )
    keys = [(item.path, item.code) for item in diagnostics]
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# Encoding rules (SPEC.md section 4.2)
# ---------------------------------------------------------------------------


def test_unknown_field_is_rejected():
    document = dict(EMPTY_DOCUMENT, surprise=True)
    assert first_code(document) == ("E_SCHEMA", "/surprise")


def test_unknown_nested_field_is_rejected(sales_document_copy):
    sales_document_copy["parameters"]["minimum"]["extra"] = 1
    assert first_code(sales_document_copy) == ("E_SCHEMA", "/parameters/minimum/extra")


def test_duplicate_keys_are_rejected_with_a_pointer(sales_document_copy):
    text = as_json(sales_document_copy).replace(
        '"title": "Sales analysis",', '"title": "A", "title": "B",'
    )
    assert first_code(text) == ("E_PARSE", "/title")


def test_unsupported_language_version_is_rejected():
    document = dict(EMPTY_DOCUMENT, language="melt/v0.2")
    assert first_code(document) == ("E_VERSION", "/language")


def test_non_finite_numbers_are_rejected(sales_document_copy):
    for literal in ("NaN", "Infinity", "-Infinity"):
        text = as_json(sales_document_copy).replace('"default": 0,', f'"default": {literal},')
        assert first_code(text)[0] == "E_PARSE"
    text = as_json(sales_document_copy).replace('"default": 0,', '"default": 1e999,')
    assert first_code(text)[0] == "E_PARSE"


def test_unsafe_integers_are_rejected(sales_document_copy):
    sales_document_copy["sources"]["sales-input"]["versions"]["1"]["byteLength"] = 2**53
    assert first_code(sales_document_copy) == (
        "E_TYPE",
        "/sources/sales-input/versions/1/byteLength",
    )


def test_unsafe_integer_defaults_are_rejected(sales_document_copy):
    """Parameter defaults reject unsafe integers before rounding (T037).

    Regression: `9007199254740993` once parsed to the float
    `9007199254740992.0` and committed silently.
    """

    sales_document_copy["parameters"]["minimum"]["default"] = 2**53 + 1
    assert first_code(sales_document_copy) == (
        "E_TYPE",
        "/parameters/minimum/default",
    )


def test_scalar_union_failure_collapses_to_one_diagnostic(sales_document_copy):
    """One rejected scalar field reports one decisive diagnostic (T040).

    Regression: an unsafe integer default emitted three sibling
    diagnostics, one per `JsonScalar` branch: the decisive safe-range
    message plus `Input should be a valid boolean` and `Input should be
    a valid string`, all `E_TYPE` at the same path.
    """

    sales_document_copy["parameters"]["minimum"]["default"] = 2**53 + 1
    diagnostics = validate_document(sales_document_copy)
    assert [(item.code, item.path) for item in diagnostics] == [
        ("E_TYPE", "/parameters/minimum/default")
    ]
    assert "safe integer range" in diagnostics[0].message


def test_non_scalar_default_collapses_to_one_diagnostic(sales_document_copy):
    """A value that no scalar branch accepts reports one diagnostic."""

    sales_document_copy["parameters"]["minimum"]["default"] = []
    diagnostics = validate_document(sales_document_copy)
    assert [(item.code, item.path) for item in diagnostics] == [
        ("E_TYPE", "/parameters/minimum/default")
    ]
    assert diagnostics[0].message == "expected a JSON number, no coercion"


def test_branch_named_unknown_fields_do_not_collapse():
    """Distinct diagnostics survive beside the collapse rule (T040).

    Two unknown fields named like scalar branch markers under an entity
    named `default` report two separate diagnostics. The collapse rule
    merges only the branch errors of one scalar union, never unrelated
    errors that happen to carry marker-like names.
    """

    document = dict(EMPTY_DOCUMENT)
    document["projections"] = {
        "default": {
            "kind": "table",
            "title": "Sales",
            "source": None,
            "config": {"columns": None, "selection": "none"},
            "controls": [],
            "bool": 1,
            "str": 2,
        }
    }
    pairs = codes(document)
    assert ("E_SCHEMA", "/projections/default/bool") in pairs
    assert ("E_SCHEMA", "/projections/default/str") in pairs


def test_negative_unsafe_integers_are_rejected(sales_document_copy):
    # The rejection comes from the safe range itself, not from the
    # constraint minimum of the sales fixture, which this default also
    # breaks.
    sales_document_copy["parameters"]["minimum"]["default"] = -(2**53)
    assert first_code(sales_document_copy) == (
        "E_TYPE",
        "/parameters/minimum/default",
    )


def test_unsafe_integer_constraint_bounds_are_rejected(sales_document_copy):
    sales_document_copy["parameters"]["minimum"]["constraints"] = {
        "minimum": 2**53 + 1
    }
    assert first_code(sales_document_copy) == (
        "E_TYPE",
        "/parameters/minimum/constraints/minimum",
    )
    copy = json.loads(json.dumps(sales_document_copy))
    copy["parameters"]["minimum"]["constraints"] = {"maximum": -(2**53) - 1}
    assert first_code(copy) == (
        "E_TYPE",
        "/parameters/minimum/constraints/maximum",
    )


def test_safe_integer_bounds_are_accepted(sales_document_copy):
    """The inclusive bounds of the safe range stay valid numbers."""

    sales_document_copy["parameters"]["minimum"]["default"] = 2**53 - 1
    sales_document_copy["parameters"]["minimum"]["constraints"] = {
        "minimum": -(2**53 - 1),
        "maximum": 2**53 - 1,
    }
    document = parse_document(sales_document_copy)
    assert document.parameters["minimum"].default == 9007199254740991.0
    assert document.parameters["minimum"].constraints.minimum == -9007199254740991.0


def test_invalid_entity_ids_are_rejected():
    for bad_id in ("Bad", "1abc", "has space", "x" * 65, "UPPER", "a$b"):
        document = dict(EMPTY_DOCUMENT)
        document["parameters"] = {bad_id: {
            "type": "boolean", "default": False, "nullable": False, "constraints": {}
        }}
        assert ("E_SCHEMA", f"/parameters/{bad_id}") in codes(document), bad_id


def test_strings_are_not_coerced_into_numbers_or_booleans(sales_document_copy):
    sales_document_copy["parameters"]["minimum"]["default"] = "0"
    assert first_code(sales_document_copy) == ("E_TYPE", "/parameters/minimum/default")
    sales_document_copy["parameters"]["minimum"]["default"] = True
    assert first_code(sales_document_copy) == ("E_TYPE", "/parameters/minimum/default")


def test_missing_required_field_is_rejected(sales_document_copy):
    del sales_document_copy["projections"]["sales"]["title"]
    assert first_code(sales_document_copy) == ("E_SCHEMA", "/projections/sales/title")


def test_missing_collection_is_rejected():
    document = dict(EMPTY_DOCUMENT)
    del document["bindings"]
    assert first_code(document) == ("E_SCHEMA", "/bindings")


# ---------------------------------------------------------------------------
# Sources (SPEC.md section 5)
# ---------------------------------------------------------------------------


def test_current_version_must_exist(sales_document_copy):
    sales_document_copy["sources"]["sales-input"]["currentVersion"] = 2
    assert first_code(sales_document_copy) == (
        "E_REFERENCE",
        "/sources/sales-input/currentVersion",
    )


def test_version_keys_are_canonical_decimals(sales_document_copy):
    versions = sales_document_copy["sources"]["sales-input"]["versions"]
    versions["01"] = versions["1"]
    assert first_code(sales_document_copy) == (
        "E_SCHEMA",
        "/sources/sales-input/versions/01",
    )


def test_artifact_hash_format_is_enforced(sales_document_copy):
    sales_document_copy["sources"]["sales-input"]["versions"]["1"]["artifact"] = (
        "sha256:ABC"
    )
    assert first_code(sales_document_copy) == (
        "E_ARTIFACT",
        "/sources/sales-input/versions/1/artifact",
    )


def test_media_type_must_match_the_kind(sales_document_copy):
    sales_document_copy["sources"]["sales-input"]["versions"]["1"]["mediaType"] = (
        "text/plain"
    )
    assert first_code(sales_document_copy) == (
        "E_TYPE",
        "/sources/sales-input/versions/1/mediaType",
    )


def test_schema_must_match_the_kind(sales_document_copy):
    sales_document_copy["sources"]["sales-input"]["versions"]["1"]["schema"] = {
        "encoding": "utf-8"
    }
    assert first_code(sales_document_copy) == (
        "E_TYPE",
        "/sources/sales-input/versions/1/schema",
    )


def test_decimal_columns_need_precision_and_scale(sales_document_copy):
    columns = sales_document_copy["sources"]["sales-input"]["versions"]["1"]["schema"][
        "columns"
    ]
    columns.append({"id": "amount", "type": "decimal", "nullable": False})
    assert first_code(sales_document_copy)[0] == "E_SCHEMA"


def test_decimal_precision_is_bounded(sales_document_copy):
    columns = sales_document_copy["sources"]["sales-input"]["versions"]["1"]["schema"][
        "columns"
    ]
    columns.append(
        {"id": "amount", "type": "decimal", "nullable": False, "precision": 39, "scale": 2}
    )
    assert first_code(sales_document_copy)[0] == "E_SCHEMA"


def test_decimal_scale_cannot_exceed_precision(sales_document_copy):
    columns = sales_document_copy["sources"]["sales-input"]["versions"]["1"]["schema"][
        "columns"
    ]
    columns.append(
        {"id": "amount", "type": "decimal", "nullable": False, "precision": 3, "scale": 4}
    )
    assert first_code(sales_document_copy)[0] == "E_SCHEMA"


def test_duplicate_column_ids_are_rejected(sales_document_copy):
    schema = sales_document_copy["sources"]["sales-input"]["versions"]["1"]["schema"]
    schema["columns"].append(dict(schema["columns"][0]))
    assert first_code(sales_document_copy)[0] == "E_SCHEMA"


def test_row_key_must_be_a_column(sales_document_copy):
    schema = sales_document_copy["sources"]["sales-input"]["versions"]["1"]["schema"]
    schema["rowKey"] = "missing"
    assert first_code(sales_document_copy)[0] == "E_REFERENCE"


def test_image_dimensions_have_limits():
    document = dict(EMPTY_DOCUMENT)
    document["sources"] = {
        "big": {
            "kind": "image",
            "currentVersion": 1,
            "versions": {
                "1": {
                    "artifact": FIXTURE_HASH,
                    "mediaType": "image/png",
                    "byteLength": 10,
                    "schema": {"width": 20000, "height": 100},
                    "origin": {"kind": "import", "label": "big.png"},
                }
            },
        }
    }
    assert first_code(document)[0] == "E_LIMIT"


def test_run_origin_shape_is_enforced(sales_document_copy):
    sales_document_copy["sources"]["sales-input"]["versions"]["1"]["origin"] = {
        "kind": "run"
    }
    assert first_code(sales_document_copy)[0] == "E_SCHEMA"


# ---------------------------------------------------------------------------
# Parameters (SPEC.md section 6)
# ---------------------------------------------------------------------------


def test_default_must_satisfy_constraints(sales_document_copy):
    sales_document_copy["parameters"]["minimum"]["default"] = -1
    assert first_code(sales_document_copy) == ("E_TYPE", "/parameters/minimum/default")


def test_default_above_maximum_is_rejected(sales_document_copy):
    sales_document_copy["parameters"]["minimum"]["default"] = 1000000001
    assert first_code(sales_document_copy) == ("E_TYPE", "/parameters/minimum/default")


def test_null_default_requires_nullable(sales_document_copy):
    sales_document_copy["parameters"]["minimum"]["default"] = None
    assert first_code(sales_document_copy) == ("E_TYPE", "/parameters/minimum/default")


def test_boolean_constraints_must_be_empty(sales_document_copy):
    sales_document_copy["parameters"]["minimum"] = {
        "type": "boolean",
        "default": False,
        "nullable": False,
        "constraints": {"minimum": 0},
    }
    assert first_code(sales_document_copy) == (
        "E_SCHEMA",
        "/parameters/minimum/constraints",
    )


def test_string_constraints_reject_numeric_fields(sales_document_copy):
    sales_document_copy["parameters"]["minimum"] = {
        "type": "string",
        "default": "a",
        "nullable": False,
        "constraints": {"minimum": 0},
    }
    assert first_code(sales_document_copy) == (
        "E_SCHEMA",
        "/parameters/minimum/constraints",
    )


def test_integer_constraints_hold_integers(sales_document_copy):
    sales_document_copy["parameters"]["minimum"] = {
        "type": "integer",
        "default": 0,
        "nullable": False,
        "constraints": {"minimum": 0.5, "maximum": 10},
    }
    assert first_code(sales_document_copy) == (
        "E_TYPE",
        "/parameters/minimum/constraints/minimum",
    )


def test_minimum_cannot_exceed_maximum(sales_document_copy):
    sales_document_copy["parameters"]["minimum"]["constraints"] = {
        "minimum": 10,
        "maximum": 5,
    }
    assert first_code(sales_document_copy) == (
        "E_SCHEMA",
        "/parameters/minimum/constraints",
    )


def test_string_enum_values_are_unique(sales_document_copy):
    sales_document_copy["parameters"]["minimum"] = {
        "type": "string",
        "default": "a",
        "nullable": False,
        "constraints": {"enum": ["a", "a"]},
    }
    assert first_code(sales_document_copy) == (
        "E_SCHEMA",
        "/parameters/minimum/constraints/enum",
    )


def test_string_default_must_fit_max_length(sales_document_copy):
    sales_document_copy["parameters"]["minimum"] = {
        "type": "string",
        "default": "abcdef",
        "nullable": False,
        "constraints": {"maxLength": 3},
    }
    assert first_code(sales_document_copy) == ("E_TYPE", "/parameters/minimum/default")


# ---------------------------------------------------------------------------
# Controls (SPEC.md section 6.3)
# ---------------------------------------------------------------------------


def test_control_parameter_must_exist(sales_document_copy):
    control = sales_document_copy["projections"]["revenue"]["controls"][0]
    control["parameter"] = "missing"
    assert first_code(sales_document_copy) == (
        "E_REFERENCE",
        "/projections/revenue/controls/0/parameter",
    )


def test_number_control_needs_a_numeric_parameter(sales_document_copy):
    sales_document_copy["parameters"]["minimum"] = {
        "type": "string",
        "default": "0",
        "nullable": False,
        "constraints": {},
    }
    assert first_code(sales_document_copy) == (
        "E_TYPE",
        "/projections/revenue/controls/0/kind",
    )


def test_toggle_needs_a_non_null_boolean(sales_document_copy):
    sales_document_copy["projections"]["revenue"]["controls"][0] = {
        "id": "minimum",
        "kind": "toggle",
        "label": "Minimum",
        "parameter": "minimum",
    }
    assert first_code(sales_document_copy) == (
        "E_TYPE",
        "/projections/revenue/controls/0/kind",
    )


def test_select_options_must_match_the_enum(sales_document_copy):
    sales_document_copy["parameters"]["minimum"] = {
        "type": "string",
        "default": "low",
        "nullable": False,
        "constraints": {"enum": ["low", "high"]},
    }
    sales_document_copy["projections"]["revenue"]["controls"][0] = {
        "id": "minimum",
        "kind": "select",
        "label": "Minimum",
        "parameter": "minimum",
        "options": [{"value": "low", "label": "Low"}],
    }
    assert first_code(sales_document_copy) == (
        "E_TYPE",
        "/projections/revenue/controls/0/options",
    )


def test_duplicate_control_ids_are_rejected(sales_document_copy):
    controls = sales_document_copy["projections"]["revenue"]["controls"]
    controls[0] = dict(controls[1])
    assert first_code(sales_document_copy) == (
        "E_SCHEMA",
        "/projections/revenue/controls/1/id",
    )


def test_controls_per_projection_are_limited(sales_document_copy):
    controls = sales_document_copy["projections"]["revenue"]["controls"]
    controls.extend(
        {"id": f"c{i}", "kind": "button", "label": f"C{i}"} for i in range(33)
    )
    assert first_code(sales_document_copy) == (
        "E_LIMIT",
        "/projections/revenue/controls",
    )


# ---------------------------------------------------------------------------
# Actions (SPEC.md section 7)
# ---------------------------------------------------------------------------


def test_action_parameter_reference_must_exist(sales_document_copy):
    sales_document_copy["actions"]["analyze-sales"]["parameters"]["minimum"][
        "parameter"
    ] = "missing"
    assert first_code(sales_document_copy) == (
        "E_REFERENCE",
        "/actions/analyze-sales/parameters/minimum/parameter",
    )


def test_action_input_source_must_exist(sales_document_copy):
    sales_document_copy["actions"]["analyze-sales"]["inputs"]["sales"]["sourceId"] = (
        "missing"
    )
    assert first_code(sales_document_copy) == (
        "E_REFERENCE",
        "/actions/analyze-sales/inputs/sales",
    )


def test_action_input_version_must_exist(sales_document_copy):
    sales_document_copy["actions"]["analyze-sales"]["inputs"]["sales"]["version"] = 9
    assert first_code(sales_document_copy) == (
        "E_REFERENCE",
        "/actions/analyze-sales/inputs/sales/version",
    )


def test_declared_output_needs_a_target(sales_document_copy):
    del sales_document_copy["actions"]["analyze-sales"]["targets"]["monthly"]
    assert first_code(sales_document_copy) == (
        "E_OUTPUT_OWNER",
        "/actions/analyze-sales/targets/monthly",
    )


def test_target_needs_a_declared_output(sales_document_copy):
    sales_document_copy["actions"]["analyze-sales"]["targets"]["extra"] = "sales"
    assert first_code(sales_document_copy) == (
        "E_OUTPUT_OWNER",
        "/actions/analyze-sales/targets/extra",
    )


def test_action_cannot_target_one_projection_twice(sales_document_copy):
    sales_document_copy["actions"]["analyze-sales"]["targets"]["monthly"] = "sales"
    assert (
        "E_OUTPUT_OWNER",
        "/actions/analyze-sales/targets/monthly",
    ) in codes(sales_document_copy)


def test_output_kind_must_match_projection_kind(sales_document_copy):
    sales_document_copy["actions"]["analyze-sales"]["outputs"]["monthly"] = "text"
    assert first_code(sales_document_copy) == (
        "E_TYPE",
        "/actions/analyze-sales/targets/monthly",
    )


def test_target_projection_must_exist(sales_document_copy):
    sales_document_copy["actions"]["analyze-sales"]["targets"]["monthly"] = "missing"
    assert first_code(sales_document_copy) == (
        "E_REFERENCE",
        "/actions/analyze-sales/targets/monthly",
    )


def _clone_action(sales_document_copy, new_id):
    import copy

    action = copy.deepcopy(sales_document_copy["actions"]["analyze-sales"])
    action["outputGroup"] = "other-group"
    sales_document_copy["actions"][new_id] = action
    return action


def test_one_action_owns_each_output_group(sales_document_copy):
    _clone_action(sales_document_copy, "second-action")
    sales_document_copy["actions"]["second-action"]["outputGroup"] = "sales-results"
    assert first_code(sales_document_copy) == (
        "E_OUTPUT_OWNER",
        "/actions/second-action/outputGroup",
    )


def test_projection_belongs_to_one_output_group(sales_document_copy):
    _clone_action(sales_document_copy, "second-action")
    sales_document_copy["actions"]["second-action"]["targets"] = {
        "filtered2": "sales"
    }
    sales_document_copy["actions"]["second-action"]["outputs"] = {"filtered2": "table"}
    assert first_code(sales_document_copy) == (
        "E_OUTPUT_OWNER",
        "/actions/second-action/targets/filtered2",
    )


# ---------------------------------------------------------------------------
# Projections (SPEC.md section 8)
# ---------------------------------------------------------------------------


def test_null_source_needs_an_action_target(sales_document_copy):
    sales_document_copy["actions"]["analyze-sales"]["targets"] = {"monthly": "revenue"}
    sales_document_copy["actions"]["analyze-sales"]["outputs"] = {"monthly": "image"}
    assert first_code(sales_document_copy) == (
        "E_REFERENCE",
        "/projections/sales/source",
    )


def test_projection_source_must_exist(sales_document_copy):
    sales_document_copy["projections"]["sales"]["source"] = {
        "sourceId": "missing",
        "version": 1,
    }
    assert first_code(sales_document_copy) == (
        "E_REFERENCE",
        "/projections/sales/source",
    )


def test_projection_source_kind_must_match(sales_document_copy):
    sales_document_copy["projections"]["revenue"]["source"] = {
        "sourceId": "sales-input",
        "version": 1,
    }
    assert first_code(sales_document_copy) == (
        "E_TYPE",
        "/projections/revenue/source",
    )


def test_single_selection_needs_a_row_key(sales_document_copy):
    sales_document_copy["projections"]["sales"]["source"] = {
        "sourceId": "sales-input",
        "version": 1,
    }
    sales_document_copy["projections"]["sales"]["config"]["selection"] = "single"
    assert first_code(sales_document_copy) == (
        "E_TYPE",
        "/projections/sales/config/selection",
    )


def test_configured_columns_must_exist_in_the_schema(sales_document_copy):
    sales_document_copy["projections"]["sales"]["source"] = {
        "sourceId": "sales-input",
        "version": 1,
    }
    sales_document_copy["projections"]["sales"]["config"]["columns"] = ["month", "nope"]
    assert first_code(sales_document_copy) == (
        "E_REFERENCE",
        "/projections/sales/config/columns/1",
    )


def test_plot_requires_fit_contain(sales_document_copy):
    sales_document_copy["projections"]["revenue"]["config"] = {"fit": "fill"}
    assert first_code(sales_document_copy) == (
        "E_TYPE",
        "/projections/revenue/config/fit",
    )


def test_document_projection_accepts_markdown(sales_document_copy):
    revenue = sales_document_copy["projections"]["revenue"]
    revenue["kind"] = "document"
    revenue["config"] = {"format": "markdown"}
    sales_document_copy["actions"]["analyze-sales"]["outputs"]["monthly"] = "text"
    parse_document(sales_document_copy)


# ---------------------------------------------------------------------------
# Bindings (SPEC.md section 9)
# ---------------------------------------------------------------------------


def test_binding_parameter_must_exist(sales_document_copy):
    sales_document_copy["bindings"]["minimum-rerun"]["event"]["parameter"] = "missing"
    assert first_code(sales_document_copy) == (
        "E_REFERENCE",
        "/bindings/minimum-rerun/event/parameter",
    )


def test_binding_action_must_exist(sales_document_copy):
    sales_document_copy["bindings"]["minimum-rerun"]["effect"]["action"] = "missing"
    assert first_code(sales_document_copy) == (
        "E_REFERENCE",
        "/bindings/minimum-rerun/effect/action",
    )


def test_only_buttons_emit_activation(sales_document_copy):
    sales_document_copy["bindings"]["manual-run"]["event"]["control"] = "minimum"
    assert first_code(sales_document_copy) == (
        "E_TYPE",
        "/bindings/manual-run/event/control",
    )


def test_button_bindings_require_zero_debounce(sales_document_copy):
    sales_document_copy["bindings"]["manual-run"]["debounceMs"] = 50
    assert first_code(sales_document_copy) == (
        "E_SCHEMA",
        "/bindings/manual-run/debounceMs",
    )


def test_debounce_is_bounded(sales_document_copy):
    sales_document_copy["bindings"]["minimum-rerun"]["debounceMs"] = 5001
    assert first_code(sales_document_copy) == (
        "E_SCHEMA",
        "/bindings/minimum-rerun/debounceMs",
    )


def test_one_binding_per_button(sales_document_copy):
    sales_document_copy["bindings"]["another"] = dict(
        sales_document_copy["bindings"]["manual-run"]
    )
    assert first_code(sales_document_copy) == (
        "E_SCHEMA",
        "/bindings/another/event",
    )


def test_one_binding_per_parameter_action_pair(sales_document_copy):
    sales_document_copy["bindings"]["another"] = dict(
        sales_document_copy["bindings"]["minimum-rerun"]
    )
    assert first_code(sales_document_copy) == (
        "E_SCHEMA",
        "/bindings/another/event",
    )


# ---------------------------------------------------------------------------
# Layout (SPEC.md section 10)
# ---------------------------------------------------------------------------


def test_layout_references_must_exist(sales_document_copy):
    sales_document_copy["layout"]["root"]["first"]["projection"] = "missing"
    assert first_code(sales_document_copy) == (
        "E_LAYOUT",
        "/layout/root/first/projection",
    )


def test_duplicate_placement_is_rejected(sales_document_copy):
    sales_document_copy["layout"]["root"]["second"]["projection"] = "sales"
    assert first_code(sales_document_copy) == (
        "E_LAYOUT",
        "/layout/root/second/projection",
    )


def test_hidden_duplicate_is_rejected(sales_document_copy):
    sales_document_copy["layout"]["root"]["second"]["projection"] = "sales"
    sales_document_copy["layout"]["root"]["second"] = {
        "kind": "pane",
        "projection": "revenue",
    }
    sales_document_copy["layout"]["hidden"] = ["revenue"]
    assert first_code(sales_document_copy)[0] == "E_LAYOUT"


def test_unplaced_projection_is_rejected(sales_document_copy):
    sales_document_copy["layout"] = {
        "root": {"kind": "pane", "projection": "sales"},
        "hidden": [],
    }
    assert first_code(sales_document_copy) == (
        "E_LAYOUT",
        "/projections/revenue",
    )


def _extra_table_projection(index: int) -> dict:
    return {
        "kind": "table",
        "title": f"Extra {index}",
        "source": {"sourceId": "sales-input", "version": 1},
        "config": {"columns": None, "selection": "none"},
        "controls": [],
    }


def test_layout_depth_is_bounded(sales_document_copy):
    for index in range(7):
        sales_document_copy["projections"][f"extra-{index}"] = _extra_table_projection(
            index
        )
    node = {
        "kind": "split",
        "axis": "horizontal",
        "ratio": 0.5,
        "first": {"kind": "pane", "projection": "sales"},
        "second": {"kind": "pane", "projection": "revenue"},
    }
    for index in range(7):
        node = {
            "kind": "split",
            "axis": "vertical",
            "ratio": 0.5,
            "first": node,
            "second": {"kind": "pane", "projection": f"extra-{index}"},
        }
    sales_document_copy["layout"]["root"] = node
    assert first_code(sales_document_copy)[0] == "E_LAYOUT"


def test_split_ratio_is_bounded(sales_document_copy):
    for ratio in (0.05, 0.95):
        sales_document_copy["layout"]["root"]["ratio"] = ratio
        assert first_code(sales_document_copy)[0] == "E_LAYOUT"


def test_layout_at_maximum_depth_is_valid(sales_document_copy):
    for index in range(6):
        sales_document_copy["projections"][f"extra-{index}"] = _extra_table_projection(
            index
        )
    node = {
        "kind": "split",
        "axis": "horizontal",
        "ratio": 0.5,
        "first": {"kind": "pane", "projection": "sales"},
        "second": {"kind": "pane", "projection": "revenue"},
    }
    for index in range(6):
        node = {
            "kind": "split",
            "axis": "vertical",
            "ratio": 0.5,
            "first": node,
            "second": {"kind": "pane", "projection": f"extra-{index}"},
        }
    sales_document_copy["layout"]["root"] = node
    parse_document(sales_document_copy)


# ---------------------------------------------------------------------------
# Default limits (SPEC.md section 15.2)
# ---------------------------------------------------------------------------


def _fill(document, collection, count, factory):
    document[collection] = {f"e{i}": factory(i) for i in range(count)}


def test_projection_count_is_limited(sales_document_copy):
    sales_document_copy["actions"]["analyze-sales"]["targets"] = {}
    sales_document_copy["actions"]["analyze-sales"]["outputs"] = {}
    sales_document_copy["bindings"] = {}
    sales_document_copy["projections"] = {
        f"p{i}": {
            "kind": "document",
            "title": "T",
            "source": None,
            "config": {"format": "plain"},
            "controls": [],
        }
        for i in range(33)
    }
    sales_document_copy["layout"] = {
        "root": {"kind": "pane", "projection": "p0"},
        "hidden": [f"p{i}" for i in range(1, 33)],
    }
    assert first_code(sales_document_copy) == ("E_LIMIT", "/projections")


def test_parameter_count_is_limited(sales_document_copy):
    extra = {
        f"p{i}": {
            "type": "boolean", "default": False, "nullable": False, "constraints": {}
        }
        for i in range(257)
    }
    sales_document_copy["parameters"].update(extra)
    assert first_code(sales_document_copy) == ("E_LIMIT", "/parameters")


def test_action_count_is_limited(sales_document_copy):
    import copy

    template = copy.deepcopy(sales_document_copy["actions"]["analyze-sales"])
    sales_document_copy["actions"] = {
        f"a{i}": dict(template, outputGroup=f"g{i}") for i in range(65)
    }
    assert first_code(sales_document_copy) == ("E_LIMIT", "/actions")


def test_binding_count_is_limited(sales_document_copy):
    template = dict(sales_document_copy["bindings"]["minimum-rerun"])
    sales_document_copy["bindings"] = {
        f"b{i}": dict(template, effect={"kind": "action.invoke", "action": "missing"})
        for i in range(257)
    }
    # The reference errors sort before the limit error; the limit is
    # still reported.
    assert ("E_LIMIT", "/bindings") in codes(sales_document_copy)


def test_parse_document_raises_with_sorted_diagnostics(sales_document_copy):
    sales_document_copy["parameters"]["minimum"]["default"] = -5
    with pytest.raises(MeltDocumentError) as error:
        parse_document(sales_document_copy)
    keys = [(item.path, item.code) for item in error.value.diagnostics]
    assert keys == sorted(keys)
