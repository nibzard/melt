"""Compiler contract tests (SPEC.md section 11, backlog task T005).

These tests drive the seven compiler stages over the shared fixtures:

- Every valid document compiles against the trusted catalog and emits
  render, state, interaction, action, dependency, identity, and layout
  plans.
- The catalog-checked invalid fixtures fail with exactly the code and
  JSON Pointer that ``fixtures/invalid/manifest.json`` records.
- Equivalent canonical inputs with equal registry versions produce
  equivalent plans.
- The compiler rejects malformed and oversized input without executing
  project code, opening the network, or installing packages.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from melt.schema.canonical import canonicalize
from runtime.compiler import (
    COMPILER_IDENTIFIER,
    ActionRegistry,
    CompileRequest,
    RegistrationEntry,
    compile_document,
    load_catalog,
)

# ---------------------------------------------------------------------------
# Shared inputs
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def catalog_pair(repo_root: Path):
    return load_catalog((repo_root / "fixtures" / "catalog.json").read_text())


@pytest.fixture(scope="module")
def sales_text(repo_root: Path) -> str:
    return (repo_root / "fixtures" / "valid" / "sales-full.json").read_text()


def compile_text(text: str, catalog_pair, states=None) -> object:
    source_catalog, action_registry = catalog_pair
    return compile_document(
        CompileRequest(
            document=text,
            source_catalog=source_catalog,
            action_registry=action_registry,
            parameter_states=states or {},
        )
    )


# ---------------------------------------------------------------------------
# Valid fixtures and the seven plans
# ---------------------------------------------------------------------------


def test_every_valid_document_fixture_compiles(
    repo_root: Path, catalog_pair
) -> None:
    directory = repo_root / "fixtures" / "valid"
    for path in sorted(directory.glob("*.json")):
        if path.name == "document-hashes.json":
            continue
        result = compile_text(path.read_text(), catalog_pair)
        assert result.ok, f"{path.name}: {result.diagnostics}"


def test_sales_example_emits_all_seven_plans(catalog_pair, sales_text) -> None:
    result = compile_text(sales_text, catalog_pair)
    assert result.ok, result.diagnostics
    plan = result.plan
    assert plan.planVersion == 1
    assert plan.language == "melt/v0.1"
    assert plan.compilerVersion == COMPILER_IDENTIFIER
    assert plan.workspaceId == "sales-demo"
    for subplan in (
        plan.render,
        plan.state,
        plan.interaction,
        plan.action,
        plan.dependency,
        plan.identity,
        plan.layout,
    ):
        assert subplan is not None


def test_render_plan_carries_renderer_source_config_controls(
    catalog_pair, sales_text
) -> None:
    plan = compile_text(sales_text, catalog_pair).plan
    entries = plan.render.projections
    assert set(entries) == {"sales", "revenue"}
    assert entries["sales"].renderer.id == "melt.table"
    assert entries["sales"].renderer.contractVersion == 1
    assert entries["sales"].source is None
    assert entries["sales"].config.columns == ("month", "revenue")
    assert entries["revenue"].renderer.id == "melt.plot"
    control_ids = [control.id for control in entries["revenue"].controls]
    assert control_ids == ["minimum", "run"]


def test_state_plan_carries_definitions_and_defaults(
    catalog_pair, sales_text
) -> None:
    plan = compile_text(sales_text, catalog_pair).plan
    entry = plan.state.parameters["minimum"]
    assert entry.type == "number"
    assert entry.default == 0
    assert entry.nullable is False
    assert entry.constraints.minimum == 0
    assert entry.constraints.maximum == 1000000000
    assert entry.scope == "workspace"


def test_interaction_plan_carries_addresses_and_debounce(
    catalog_pair, sales_text
) -> None:
    plan = compile_text(sales_text, catalog_pair).plan
    bindings = plan.interaction.bindings
    assert set(bindings) == {"minimum-rerun", "manual-run"}
    assert bindings["minimum-rerun"].debounceMs == 250
    assert bindings["manual-run"].debounceMs == 0
    assert plan.interaction.eventAddresses == {
        "parameter.changed/minimum": ("minimum-rerun",),
        "control.activate/revenue/run": ("manual-run",),
    }


def test_action_plan_resolves_registration_and_permission(
    catalog_pair, sales_text
) -> None:
    plan = compile_text(sales_text, catalog_pair).plan
    entry = plan.action.actions["analyze-sales"]
    assert entry.registration.registrationId == "sales-script-v1"
    assert entry.registration.revision == 1
    assert entry.registration.requiredPermission == "execute_project_code"
    assert entry.parameters == {"minimum": "minimum"}
    assert entry.targets == {"filtered": "sales", "monthly": "revenue"}
    group = plan.action.outputGroups["sales-results"]
    assert group.action == "analyze-sales"
    assert group.targets == {"filtered": "sales", "monthly": "revenue"}
    assert plan.registrations == {"sales-script-v1": 1}


def test_dependency_plan_indexes_readers(catalog_pair, sales_text) -> None:
    plan = compile_text(sales_text, catalog_pair).plan
    readers = plan.dependency.sources["sales-input"]["1"].readers
    assert readers == ("actions/analyze-sales",)
    parameter = plan.dependency.parameters["minimum"].readers
    assert parameter == (
        "actions/analyze-sales",
        "bindings/minimum-rerun",
        "projections/revenue",
    )
    action = plan.dependency.actions["analyze-sales"]
    assert action.registrationId == "sales-script-v1"
    assert action.parameters == ("minimum",)
    assert action.targets == ("revenue", "sales")


def test_identity_plan_maps_panes_and_controls(catalog_pair, sales_text) -> None:
    plan = compile_text(sales_text, catalog_pair).plan
    assert plan.identity.panes["sales"].pane == "sales"
    assert plan.identity.panes["sales"].kind == "table"
    assert plan.identity.panes["sales"].outputGroup == "sales-results"
    assert plan.identity.panes["revenue"].outputGroup == "sales-results"
    assert plan.identity.controls["revenue/minimum"].parameter == "minimum"
    assert "revenue/run" not in plan.identity.controls


def test_layout_plan_records_validated_placements(
    catalog_pair, sales_text
) -> None:
    plan = compile_text(sales_text, catalog_pair).plan
    assert plan.layout.placements["sales"] .path == "/layout/root/first"
    assert plan.layout.placements["revenue"].path == "/layout/root/second"
    assert plan.layout.hidden == ()


def test_layout_plan_marks_hidden_panes(repo_root: Path, catalog_pair) -> None:
    text = (repo_root / "fixtures" / "valid" / "hidden-panes.json").read_text()
    plan = compile_text(text, catalog_pair).plan
    placement = plan.layout.placements["summary-table"]
    assert placement.placement == "hidden"
    assert placement.path == "/layout/hidden/0"
    assert plan.identity.panes["summary-table"].outputGroup == "sales-summary"


def test_plan_is_immutable(catalog_pair, sales_text) -> None:
    plan = compile_text(sales_text, catalog_pair).plan
    with pytest.raises(Exception):
        plan.workspaceId = "other"


# ---------------------------------------------------------------------------
# Catalog-checked invalid fixtures (manifest entries enforcedBy compiler)
# ---------------------------------------------------------------------------


def test_registry_mismatch_fails_with_expected_diagnostic(
    repo_root: Path, catalog_pair
) -> None:
    text = (repo_root / "fixtures" / "invalid" / "registry-mismatch.json").read_text()
    result = compile_text(text, catalog_pair)
    assert not result.ok
    assert result.plan is None
    assert [(item.code, item.path) for item in result.diagnostics] == [
        ("E_REGISTRATION", "/actions/analyze-sales/parameters/maximum")
    ]


def test_artifact_schema_drift_fails_with_expected_diagnostic(
    repo_root: Path, catalog_pair
) -> None:
    text = (
        repo_root / "fixtures" / "invalid" / "artifact-schema-drift.json"
    ).read_text()
    result = compile_text(text, catalog_pair)
    assert not result.ok
    assert [(item.code, item.path) for item in result.diagnostics] == [
        ("E_ARTIFACT", "/sources/sales-input/versions/1/schema")
    ]


def test_catalog_diagnostics_carry_hints_without_secrets(
    repo_root: Path, catalog_pair
) -> None:
    for name in ("registry-mismatch.json", "artifact-schema-drift.json"):
        text = (repo_root / "fixtures" / "invalid" / name).read_text()
        for diagnostic in compile_text(text, catalog_pair).diagnostics:
            assert diagnostic.hint, name
            assert "sha256:" not in diagnostic.hint


# ---------------------------------------------------------------------------
# Catalog and registry checks
# ---------------------------------------------------------------------------


def test_unknown_artifact_is_rejected(repo_root: Path, catalog_pair) -> None:
    document = json.loads(
        (repo_root / "fixtures" / "valid" / "sales-full.json").read_text()
    )
    document["sources"]["sales-input"]["versions"]["1"]["artifact"] = (
        "sha256:" + "ab" * 32
    )
    result = compile_text(json.dumps(document), catalog_pair)
    pairs = [(item.code, item.path) for item in result.diagnostics]
    assert ("E_ARTIFACT", "/sources/sales-input/versions/1/artifact") in pairs


def test_byte_length_drift_is_rejected(repo_root: Path, catalog_pair) -> None:
    document = json.loads(
        (repo_root / "fixtures" / "valid" / "sales-full.json").read_text()
    )
    document["sources"]["sales-input"]["versions"]["1"]["byteLength"] = 749
    result = compile_text(json.dumps(document), catalog_pair)
    pairs = [(item.code, item.path) for item in result.diagnostics]
    assert ("E_ARTIFACT", "/sources/sales-input/versions/1/byteLength") in pairs


def test_media_type_drift_is_rejected(repo_root: Path, catalog_pair) -> None:
    document = json.loads(
        (repo_root / "fixtures" / "valid" / "sales-full.json").read_text()
    )
    document["sources"]["sales-input"]["versions"]["1"]["mediaType"] = (
        "text/plain"
    )
    result = compile_text(json.dumps(document), catalog_pair)
    pairs = [(item.code, item.path) for item in result.diagnostics]
    assert ("E_ARTIFACT", "/sources/sales-input/versions/1/mediaType") in pairs


def test_unknown_registration_is_rejected(repo_root: Path, catalog_pair) -> None:
    document = json.loads(
        (repo_root / "fixtures" / "valid" / "sales-full.json").read_text()
    )
    document["actions"]["analyze-sales"]["implementation"]["registrationId"] = (
        "other-script-v1"
    )
    result = compile_text(json.dumps(document), catalog_pair)
    pairs = [(item.code, item.path) for item in result.diagnostics]
    assert (
        "E_REGISTRATION",
        "/actions/analyze-sales/implementation/registrationId",
    ) in pairs


def test_missing_registration_input_is_rejected(
    repo_root: Path, catalog_pair
) -> None:
    document = json.loads(
        (repo_root / "fixtures" / "valid" / "sales-full.json").read_text()
    )
    del document["actions"]["analyze-sales"]["inputs"]["sales"]
    result = compile_text(json.dumps(document), catalog_pair)
    pairs = [(item.code, item.path) for item in result.diagnostics]
    assert ("E_REGISTRATION", "/actions/analyze-sales/inputs") in pairs


def test_output_kind_mismatch_is_rejected(repo_root: Path, catalog_pair) -> None:
    document = json.loads(
        (repo_root / "fixtures" / "valid" / "sales-full.json").read_text()
    )
    document["actions"]["analyze-sales"]["outputs"]["monthly"] = "table"
    result = compile_text(json.dumps(document), catalog_pair)
    pairs = [(item.code, item.path) for item in result.diagnostics]
    assert ("E_REGISTRATION", "/actions/analyze-sales/outputs/monthly") in pairs


def test_widened_parameter_constraints_are_rejected(
    repo_root: Path, catalog_pair
) -> None:
    document = json.loads(
        (repo_root / "fixtures" / "valid" / "sales-full.json").read_text()
    )
    document["parameters"]["minimum"]["constraints"] = {"minimum": -10}
    result = compile_text(json.dumps(document), catalog_pair)
    pairs = [(item.code, item.path) for item in result.diagnostics]
    assert ("E_REGISTRATION", "/actions/analyze-sales/parameters/minimum") in pairs


def test_nullable_parameter_cannot_feed_non_nullable_slot(
    repo_root: Path, catalog_pair
) -> None:
    document = json.loads(
        (repo_root / "fixtures" / "valid" / "sales-full.json").read_text()
    )
    document["parameters"]["minimum"]["nullable"] = True
    document["parameters"]["minimum"]["default"] = None
    result = compile_text(json.dumps(document), catalog_pair)
    pairs = [(item.code, item.path) for item in result.diagnostics]
    assert ("E_REGISTRATION", "/actions/analyze-sales/parameters/minimum") in pairs


def test_compiling_without_registry_rejects_actions(sales_text) -> None:
    result = compile_document(CompileRequest(document=sales_text))
    pairs = [(item.code, item.path) for item in result.diagnostics]
    assert (
        "E_REGISTRATION",
        "/actions/analyze-sales/implementation/registrationId",
    ) in pairs


def test_compiling_without_catalog_rejects_cited_artifacts(
    repo_root: Path,
) -> None:
    text = (repo_root / "fixtures" / "valid" / "projection-table.json").read_text()
    result = compile_document(CompileRequest(document=text))
    pairs = [(item.code, item.path) for item in result.diagnostics]
    assert ("E_ARTIFACT", "/sources/sales-input/versions/1/artifact") in pairs


# ---------------------------------------------------------------------------
# Stage 5: state continuity and policy
# ---------------------------------------------------------------------------


def test_state_outside_constraints_reports_migration(
    catalog_pair, sales_text
) -> None:
    result = compile_text(
        sales_text, catalog_pair, states={"minimum": {"value": 2e9, "revision": 3}}
    )
    pairs = [(item.code, item.path) for item in result.diagnostics]
    assert pairs == [("E_STATE_MIGRATION", "/parameters/minimum")]


def test_null_state_on_non_nullable_parameter_reports_migration(
    catalog_pair, sales_text
) -> None:
    result = compile_text(
        sales_text, catalog_pair, states={"minimum": {"value": None, "revision": 1}}
    )
    pairs = [(item.code, item.path) for item in result.diagnostics]
    assert pairs == [("E_STATE_MIGRATION", "/parameters/minimum")]


def test_state_for_undefined_parameter_reports_migration(
    catalog_pair, sales_text
) -> None:
    result = compile_text(
        sales_text, catalog_pair, states={"ceiling": {"value": 1.0, "revision": 1}}
    )
    pairs = [(item.code, item.path) for item in result.diagnostics]
    assert pairs == [("E_STATE_MIGRATION", "/parameters/ceiling")]


def test_valid_state_compiles_cleanly(catalog_pair, sales_text) -> None:
    result = compile_text(
        sales_text, catalog_pair, states={"minimum": {"value": 200.0, "revision": 4}}
    )
    assert result.ok, result.diagnostics


def test_table_column_policy_limit(repo_root: Path, catalog_pair) -> None:
    document = json.loads(
        (repo_root / "fixtures" / "valid" / "projection-table.json").read_text()
    )
    document["projections"]["sales"]["config"]["columns"] = [
        f"column-{index}" for index in range(129)
    ]
    result = compile_text(json.dumps(document), catalog_pair)
    pairs = [(item.code, item.path) for item in result.diagnostics]
    assert ("E_LIMIT", "/projections/sales/config/columns") in pairs


# ---------------------------------------------------------------------------
# Stage 1: decode limits
# ---------------------------------------------------------------------------


def test_oversized_input_is_rejected(sales_text) -> None:
    result = compile_document(
        CompileRequest(document=sales_text, max_input_bytes=64)
    )
    pairs = [(item.code, item.path) for item in result.diagnostics]
    assert pairs == [("E_LIMIT", "")]


def test_malformed_input_is_rejected() -> None:
    result = compile_document(CompileRequest(document='{"language": '))
    codes = [item.code for item in result.diagnostics]
    assert codes == ["E_PARSE"]


# ---------------------------------------------------------------------------
# Determinism and equivalence (SPEC.md section 11.2)
# ---------------------------------------------------------------------------


def reordered_document(sales_text: str) -> str:
    """Re-serialize the sales example with reversed map insertion order."""

    def reorder(value):
        if isinstance(value, dict):
            return {key: reorder(item) for key, item in reversed(list(value.items()))}
        if isinstance(value, list):
            return [reorder(item) for item in value]
        return value

    return json.dumps(reorder(json.loads(sales_text)), indent=1)


def test_equivalent_canonical_inputs_produce_equivalent_plans(
    catalog_pair, sales_text
) -> None:
    first = compile_text(sales_text, catalog_pair).plan
    second = compile_text(reordered_document(sales_text), catalog_pair).plan
    assert first.planKey == second.planKey
    assert canonicalize(first.model_dump(mode="json", by_alias=True)) == (
        canonicalize(second.model_dump(mode="json", by_alias=True))
    )


def test_repeated_compilation_is_deterministic(catalog_pair, sales_text) -> None:
    first = compile_text(sales_text, catalog_pair).plan
    second = compile_text(sales_text, catalog_pair).plan
    assert first.model_dump(mode="json", by_alias=True) == (
        second.model_dump(mode="json", by_alias=True)
    )


def test_registry_revision_change_changes_the_plan_key(
    catalog_pair, sales_text
) -> None:
    source_catalog, action_registry = catalog_pair
    bumped = ActionRegistry(
        {
            registration_id: RegistrationEntry(
                entry.registration_id,
                entry.revision + 1,
                entry.contract,
                entry.required_permission,
            )
            for registration_id, entry in action_registry.registrations.items()
        }
    )
    first = compile_text(sales_text, catalog_pair).plan
    second = compile_document(
        CompileRequest(
            document=sales_text,
            source_catalog=source_catalog,
            action_registry=bumped,
        )
    ).plan
    assert first.planKey != second.planKey


def test_diagnostics_are_sorted_by_path_then_code(
    repo_root: Path, catalog_pair
) -> None:
    document = json.loads(
        (repo_root / "fixtures" / "valid" / "sales-full.json").read_text()
    )
    document["actions"]["analyze-sales"]["outputs"]["monthly"] = "table"
    document["actions"]["analyze-sales"]["outputs"]["extra"] = "text"
    result = compile_text(json.dumps(document), catalog_pair)
    keys = [(item.path, item.code) for item in result.diagnostics]
    assert keys == sorted(keys)


def test_document_hash_covers_definitions_only(catalog_pair, sales_text) -> None:
    first = compile_text(sales_text, catalog_pair).plan
    second = compile_text(
        sales_text,
        catalog_pair,
        states={"minimum": {"value": 200.0, "revision": 9}},
    ).plan
    assert first.documentHash == second.documentHash


# ---------------------------------------------------------------------------
# Boundary: no execution, network, or package installation
# ---------------------------------------------------------------------------


FORBIDDEN_IMPORTS = frozenset(
    {
        "subprocess",
        "socket",
        "ssl",
        "http",
        "urllib",
        "asyncio",
        "requests",
        "pip",
        "ensurepip",
        "venv",
        "ctypes",
    }
)


def test_compiler_modules_import_no_execution_or_network_modules() -> None:
    package = Path(__file__).resolve().parents[2] / "runtime" / "compiler"
    modules = sorted(package.glob("*.py"))
    assert modules, "runtime/compiler holds no modules"
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [item.name.split(".")[0] for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module.split(".")[0]] if node.module else []
            else:
                continue
            for name in names:
                assert name not in FORBIDDEN_IMPORTS, f"{path.name}: {name}"


_COMPILER_IMPORT_PROGRAM = """
import sys

import runtime.compiler

# `socket` and `ssl` arrive transitively through the pinned pydantic
# stack (email.utils); the source-level test bans direct use. What the
# compiler must never pull is the execution stack below.
loaded = sorted(
    name
    for name in ("subprocess", "pyarrow", "pandas", "matplotlib")
    if name in sys.modules
)
print(",".join(loaded))
"""


def test_importing_the_compiler_pulls_no_execution_stack(
    repo_root: Path,
) -> None:
    """A fresh interpreter that imports the compiler loads no execution
    or network stack (SPEC.md section 11.2)."""

    import os
    import subprocess
    import sys

    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(repo_root / "python"), str(repo_root)]
    )
    completed = subprocess.run(
        [sys.executable, "-c", _COMPILER_IMPORT_PROGRAM],
        capture_output=True,
        text=True,
        env=environment,
        check=True,
    )
    assert completed.stdout.strip() == ""


_PLAN_DUMP_PROGRAM = """
import json
import sys

from runtime.compiler import CompileRequest, compile_document, load_catalog

document, catalog_path = sys.argv[1], sys.argv[2]
source_catalog, action_registry = load_catalog(open(catalog_path).read())
result = compile_document(
    CompileRequest(
        document=open(document).read(),
        source_catalog=source_catalog,
        action_registry=action_registry,
    )
)
assert result.ok, result.diagnostics
print(json.dumps(result.plan.model_dump(mode="json", by_alias=True), sort_keys=True))
"""


def test_plans_are_deterministic_across_processes(
    repo_root: Path, catalog_pair, sales_text, tmp_path
) -> None:
    """Hash randomization must not change a compiled plan."""

    import os
    import subprocess
    import sys

    document = tmp_path / "document.json"
    document.write_text(sales_text, encoding="utf-8")
    catalog_path = repo_root / "fixtures" / "catalog.json"
    outputs = []
    for seed in ("1", "2"):
        environment = dict(os.environ, PYTHONHASHSEED=seed)
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(repo_root / "python"), str(repo_root)]
        )
        completed = subprocess.run(
            [sys.executable, "-c", _PLAN_DUMP_PROGRAM, str(document), str(catalog_path)],
            capture_output=True,
            text=True,
            env=environment,
            check=True,
        )
        outputs.append(completed.stdout)
    assert outputs[0] == outputs[1]


def test_document_at_the_projection_limit_compiles(
    repo_root: Path, catalog_pair
) -> None:
    """Thirty-two projections are the bound; the bound must compile."""

    document = json.loads(
        (repo_root / "fixtures" / "valid" / "projection-table.json").read_text()
    )
    first = document["projections"]["sales"]
    document["projections"] = {}
    for index in range(32):
        projection_id = f"table-{index:02d}"
        document["projections"][projection_id] = {
            "kind": "table",
            "title": f"Table {index}",
            "source": first["source"],
            "config": {"columns": None, "selection": "none"},
            "controls": [],
        }
    document["layout"]["root"] = {"kind": "pane", "projection": "table-00"}
    document["layout"]["hidden"] = [
        f"table-{index:02d}" for index in range(1, 32)
    ]
    result = compile_text(json.dumps(document), catalog_pair)
    assert result.ok, result.diagnostics
    assert len(result.plan.render.projections) == 32
    assert len(result.plan.identity.panes) == 32
