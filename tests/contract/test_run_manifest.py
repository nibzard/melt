"""Run manifest contract (T015, SPEC.md section 13.2).

The manifest is the one file the runner trusts. Every field is
validated before a process starts, so a malformed manifest is an
operational failure of the run, never a failure of the script.
"""

from __future__ import annotations

import json

import pytest

from melt.runner.manifest import (
    RUN_MANIFEST_VERSION,
    MeltManifestError,
    build_manifest,
    manifest_to_wire,
    parse_manifest,
)


def manifest_document(**overrides) -> dict:
    """Return one valid manifest with optional field overrides."""

    document = {
        "manifestVersion": RUN_MANIFEST_VERSION,
        "runId": "run-0001",
        "workspaceId": "sales-demo",
        "actionId": "analyze-sales",
        "scriptPath": "/workspace/staging/run-0001/script.py",
        "parameters": {"minimum": 200},
        "inputs": {"sales": "/workspace/artifacts/abc123"},
        "outputs": {"filtered": "table", "monthly": "image"},
        "stagingDirectory": "/workspace/staging/run-0001",
    }
    document.update(overrides)
    return document


def parse_error_of(document) -> "tuple[str, str]":
    """Return the single (code, path) pair one invalid document reports."""

    with pytest.raises(MeltManifestError) as failure:
        parse_manifest(document)
    diagnostics = failure.value.diagnostics
    assert len(diagnostics) == 1, [str(item) for item in diagnostics]
    return diagnostics[0].code, diagnostics[0].path


def test_valid_manifest_parses_with_every_field():
    manifest = parse_manifest(manifest_document())
    assert manifest.runId == "run-0001"
    assert manifest.workspaceId == "sales-demo"
    assert manifest.actionId == "analyze-sales"
    assert manifest.parameters == {"minimum": 200}
    assert manifest.inputs == {"sales": "/workspace/artifacts/abc123"}
    assert manifest.outputs == {"filtered": "table", "monthly": "image"}
    assert manifest.stagingDirectory == "/workspace/staging/run-0001"


def test_manifest_round_trips_through_its_wire_shape():
    built = build_manifest(
        run_id="run-0002",
        workspace_id="sales-demo",
        action_id="analyze-sales",
        script_path="/workspace/staging/run-0002/script.py",
        parameters={"minimum": 0, "label": "all", "strict": True},
        inputs={"sales": "/workspace/artifacts/abc123"},
        outputs={"filtered": "table"},
        staging_directory="/workspace/staging/run-0002",
    )
    wire = manifest_to_wire(built)
    assert parse_manifest(json.dumps(wire)) == built


def test_unsupported_version_reports_e_version_before_other_fields():
    # The version check runs before any other validation, so a future
    # format identifies itself even when its other fields are unknown.
    code, path = parse_error_of(manifest_document(manifestVersion=2))
    assert (code, path) == ("E_VERSION", "/manifestVersion")
    code, path = parse_error_of(
        {key: value for key, value in manifest_document().items()
         if key != "manifestVersion"}
    )
    assert (code, path) == ("E_VERSION", "/manifestVersion")


def test_unknown_fields_are_refused():
    code, path = parse_error_of(
        manifest_document(unexpected="the service never sends this")
    )
    assert (code, path) == ("E_SCHEMA", "/unexpected")


def test_paths_must_be_absolute():
    code, path = parse_error_of(manifest_document(scriptPath="script.py"))
    assert (code, path) == ("E_SCHEMA", "/scriptPath")
    code, path = parse_error_of(
        manifest_document(inputs={"sales": "artifacts/abc123"})
    )
    assert (code, path) == ("E_SCHEMA", "/inputs/sales")
    code, path = parse_error_of(
        manifest_document(stagingDirectory="staging/run-0001")
    )
    assert (code, path) == ("E_SCHEMA", "/stagingDirectory")


def test_output_kinds_are_limited_to_the_source_kinds():
    code, path = parse_error_of(
        manifest_document(outputs={"monthly": "movie"})
    )
    assert (code, path) == ("E_TYPE", "/outputs/monthly")


def test_run_identifiers_follow_the_service_syntax():
    code, path = parse_error_of(manifest_document(runId="../escape"))
    assert (code, path) == ("E_SCHEMA", "/runId")
    code, path = parse_error_of(manifest_document(runId="run with spaces"))
    assert (code, path) == ("E_SCHEMA", "/runId")


def test_names_use_entity_identifier_syntax():
    code, path = parse_error_of(manifest_document(parameters={"Min": 0}))
    assert (code, path) == ("E_SCHEMA", "/parameters/Min")
    code, path = parse_error_of(
        manifest_document(outputs={"Monthly!": "image"})
    )
    assert (code, path) == ("E_SCHEMA", "/outputs/Monthly!")


def test_captured_values_are_json_scalars():
    # A non-scalar value fails every branch of the scalar union, and
    # the failure collapses into one decisive diagnostic at the
    # captured field (T040).
    with pytest.raises(MeltManifestError) as failure:
        parse_manifest(manifest_document(parameters={"minimum": {"nested": True}}))
    assert [(item.code, item.path) for item in failure.value.diagnostics] == [
        ("E_TYPE", "/parameters/minimum")
    ]
    with pytest.raises(MeltManifestError) as failure:
        parse_manifest(manifest_document(parameters={"minimum": [1]}))
    assert [(item.code, item.path) for item in failure.value.diagnostics] == [
        ("E_TYPE", "/parameters/minimum")
    ]


def test_unsafe_captured_value_collapses_to_one_diagnostic():
    """An unsafe captured integer reports the safe-range message (T040).

    Regression: the value failed every `JsonScalar` branch, so the
    manifest reported three sibling `E_TYPE` diagnostics at
    `/parameters/minimum`, one per branch.
    """

    with pytest.raises(MeltManifestError) as failure:
        parse_manifest(
            manifest_document(parameters={"minimum": 2**53 + 1})
        )
    diagnostics = failure.value.diagnostics
    assert [(item.code, item.path) for item in diagnostics] == [
        ("E_TYPE", "/parameters/minimum")
    ]
    assert "safe integer range" in diagnostics[0].message


def test_invalid_captured_value_under_neutral_key_collapses_to_one_diagnostic():
    """One invalid captured value under any key reports one diagnostic (T043).

    Regression: a captured value sits under an arbitrary entity-ID
    map key, so a key that names no scalar-union holder field, as
    `threshold` does not, defeated the holder lookup. The manifest
    reported three sibling `E_TYPE` diagnostics under
    `/parameters/threshold`, one per branch, each with its raw
    pydantic branch marker in the path. The holder is now read from
    the enclosing map field name.
    """

    with pytest.raises(MeltManifestError) as failure:
        parse_manifest(manifest_document(parameters={"threshold": {"bad": 1}}))
    diagnostics = failure.value.diagnostics
    assert [(item.code, item.path) for item in diagnostics] == [
        ("E_TYPE", "/parameters/threshold")
    ]
    assert diagnostics[0].message == "expected a JSON number, no coercion"


def test_key_that_looks_like_a_branch_marker_keeps_its_path():
    """A marker-shaped entity ID stays in the path (T043).

    Entity IDs match `[a-z][a-z0-9_-]{0,63}`, so `bool` is a valid
    key. The collapse removes the branch marker, never the key the
    parameter is captured under.
    """

    with pytest.raises(MeltManifestError) as failure:
        parse_manifest(manifest_document(parameters={"bool": {"bad": 1}}))
    assert [(item.code, item.path) for item in failure.value.diagnostics] == [
        ("E_TYPE", "/parameters/bool")
    ]


def test_null_captured_values_parse_for_nullable_slots():
    manifest = parse_manifest(manifest_document(parameters={"minimum": None}))
    assert manifest.parameters == {"minimum": None}


def test_duplicate_keys_are_refused_by_the_json_reader():
    text = (
        '{"manifestVersion": 1, "manifestVersion": 1, "runId": "run-0001",'
        ' "workspaceId": "sales-demo", "actionId": "a",'
        ' "scriptPath": "/s/script.py", "parameters": {}, "inputs": {},'
        ' "outputs": {}, "stagingDirectory": "/s"}'
    )
    with pytest.raises(MeltManifestError) as failure:
        parse_manifest(text)
    assert failure.value.diagnostics[0].code == "E_PARSE"


def test_non_finite_numbers_are_refused():
    text = json.dumps(manifest_document()).replace(
        '"minimum": 200', '"minimum": NaN'
    )
    with pytest.raises(MeltManifestError) as failure:
        parse_manifest(text)
    assert failure.value.diagnostics[0].code == "E_PARSE"


def test_missing_required_fields_are_reported():
    document = manifest_document()
    del document["outputs"]
    code, path = parse_error_of(document)
    assert (code, path) == ("E_SCHEMA", "/outputs")


def test_empty_collections_are_valid():
    manifest = parse_manifest(
        manifest_document(parameters={}, inputs={}, outputs={})
    )
    assert manifest.parameters == {}
    assert manifest.inputs == {}
    assert manifest.outputs == {}
