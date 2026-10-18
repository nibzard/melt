"""Action registration registry (T013, SPEC.md sections 7.2 and 12.6).

The tests prove the registration loop through the real service and its
durable storage:

- ``action.register`` redeems a captured script token, rereads the
  stored bytes, verifies their hash and size, and commits one trusted
  registration record: identifier, immutable revision, script artifact
  and hash, project root policy, environment fingerprint, parameter
  and input contract, declared outputs, required permission, and the
  fixed ``nonReplaySafe`` classification.
- Registration never executes the script: no run record, no process,
  no side effect of the captured bytes.
- Editing the script produces a new registration; re-registering the
  identical capture answers the stored registration; a changed
  contract over the same capture advances the immutable revision.
- Registrations use durable request receipts, refuse unknown
  environments and non-script tokens, and require a created workspace.
- Definitions that cite a minted registration apply cleanly, and a
  mapping that leaves a registration-bounded constraint dimension open
  is refused (SPEC.md section 7.2, exact satisfaction).
- A restart rebuilds the registry from the committed registration
  records without rereading script bytes or executing anything.
- An action whose registration is unavailable after restoration stays
  disabled while its projections remain readable.

Everything runs against an isolated temporary root named with the
Looper resource prefix, never against the user's data directory.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from melt.schema.canonical import model_to_wire
from melt.schema.commands import parse_command
from runtime.storage import PlatformAdapter, storage_worker_available
from runtime.workspace import (
    REGISTRATION_KIND,
    RUN_KIND,
    WorkspaceService,
    local_environment,
)

STORAGE_AVAILABLE, STORAGE_UNAVAILABLE_REASON = storage_worker_available()

requires_storage_worker = pytest.mark.skipif(
    not STORAGE_AVAILABLE, reason=STORAGE_UNAVAILABLE_REASON
)

pytestmark = requires_storage_worker

WORKSPACE = "sales-demo"

PARQUET = "application/vnd.apache.parquet"
PYTHON = "text/x-python"

MARKER_NAME = "registration-executed-marker"

SALES_SCRIPT = (
    "import pandas as pd\n"
    "from melt import ui\n"
    "\n"
    "sales = pd.read_parquet(ui.input_path('sales'))\n"
    "selected = sales[sales['revenue'] >= ui.params['minimum']]\n"
    "ui.output('filtered', selected)\n"
)

EDITED_SALES_SCRIPT = SALES_SCRIPT + "ui.output('monthly', selected)\n"

SALES_CONTRACT = {
    "parameters": {
        "minimum": {
            "type": "number",
            "nullable": False,
            "constraints": {"minimum": 0, "maximum": 1000000000},
        }
    },
    "inputs": {"sales": "table"},
    "outputs": {"filtered": "table", "monthly": "image"},
}

LEAN_CONTRACT = {
    "parameters": {
        "minimum": {
            "type": "number",
            "nullable": False,
            "constraints": {"minimum": 0},
        }
    },
    "inputs": {"sales": "table"},
    "outputs": {"filtered": "table"},
}

EMPTY_DOCUMENT = {
    "language": "melt/v0.1",
    "workspaceId": WORKSPACE,
    "title": "Sales analysis",
    "sources": {},
    "parameters": {},
    "actions": {},
    "projections": {},
    "bindings": {},
    "layout": {"root": None, "hidden": []},
}


def _load_builder():
    """Load scripts/build_fixture_artifacts.py as a module."""

    path = (
        Path(__file__).resolve().parents[2] / "scripts" / "build_fixture_artifacts.py"
    )
    spec = importlib.util.spec_from_file_location("build_fixture_artifacts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = _load_builder()


def sales_parquet() -> bytes:
    return BUILDER.sales_parquet_bytes()


def register_envelope(
    request_id: str,
    token: str,
    contract: dict,
    *,
    environment_id: str = None,
):
    return parse_command(
        {
            "protocolVersion": 1,
            "workspaceId": WORKSPACE,
            "requestId": request_id,
            "method": "action.register",
            "payload": {
                "scriptToken": token,
                "environmentId": environment_id or local_environment().environment_id,
                "contract": contract,
            },
        }
    )


def apply_envelope(request_id: str, expected: dict, operations: list):
    return parse_command(
        {
            "protocolVersion": 1,
            "workspaceId": WORKSPACE,
            "requestId": request_id,
            "method": "workspace.apply",
            "expected": expected,
            "operations": operations,
        }
    )


@pytest.fixture
def service(service_adapter: PlatformAdapter):
    """One created workspace, closed with the test."""

    opened = WorkspaceService.open(service_adapter, WORKSPACE)
    created = opened.create_workspace(
        json.loads(json.dumps(EMPTY_DOCUMENT)), "01-create"
    )
    assert created.ok, created.errors
    yield opened
    opened.close()


def capture_script(service: WorkspaceService, script: str) -> str:
    return service.capture_artifact_token(script.encode("utf-8"), PYTHON).token


def register(
    service: WorkspaceService,
    request_id: str,
    script: str,
    contract: dict = SALES_CONTRACT,
):
    return service.dispatch(
        register_envelope(request_id, capture_script(service, script), contract)
    )


def import_sales(service: WorkspaceService, request_id: str):
    token = service.capture_artifact_token(sales_parquet(), PARQUET).token
    return service.dispatch(
        parse_command(
            {
                "protocolVersion": 1,
                "workspaceId": WORKSPACE,
                "requestId": request_id,
                "method": "source.import",
                "expected": {"sources/sales-input": 0},
                "payload": {
                    "sourceId": "sales-input",
                    "artifactToken": token,
                    "originLabel": "sales.parquet",
                },
            }
        )
    )


# ---------------------------------------------------------------------------
# The stored registration record (SPEC.md section 7.2)
# ---------------------------------------------------------------------------


def test_registration_stores_every_recorded_fact(service: WorkspaceService):
    response = register(service, "02-register", SALES_SCRIPT)
    assert response.ok, response.errors
    environment = local_environment()
    assert response.result["registrationId"].startswith("reg-")
    assert response.result["revision"] == 1
    assert response.result["created"] is True
    assert response.result["script"]["byteLength"] == len(SALES_SCRIPT.encode())
    assert response.result["script"]["mediaType"] == PYTHON
    assert response.result["script"]["artifact"].startswith("sha256:")
    assert response.result["environmentFingerprint"] == environment.fingerprint
    assert response.result["projectRoot"] == "workspace-contained"
    assert response.result["workingDirectoryPolicy"] == "private-staging"
    assert response.result["requiredPermission"] == "execute_project_code"
    assert response.result["replayClassification"] == "nonReplaySafe"
    # The committed record carries every fact of the registration.
    records = service.store.records_of_kind(REGISTRATION_KIND)
    assert len(records) == 1
    record = records[0].data
    assert record["registrationId"] == response.result["registrationId"]
    assert record["revision"] == 1
    assert record["script"] == response.result["script"]
    assert record["projectRoot"] == "workspace-contained"
    assert record["workingDirectoryPolicy"] == "private-staging"
    assert record["environmentFingerprint"] == environment.fingerprint
    assert record["parameters"] == SALES_CONTRACT["parameters"]
    assert record["inputs"] == SALES_CONTRACT["inputs"]
    assert record["outputs"] == SALES_CONTRACT["outputs"]
    assert record["requiredPermission"] == "execute_project_code"
    assert record["replayClassification"] == "nonReplaySafe"
    # The captured bytes are durable and hash to the recorded artifact.
    assert (
        service.store.read_artifact(record["script"]["artifact"])
        == SALES_SCRIPT.encode()
    )
    # The registry exposes the registration to the compiler.
    entry = service.action_registry.entry(record["registrationId"])
    assert entry is not None
    assert entry.revision == 1
    assert entry.contract.inputs == {"sales": "table"}
    assert entry.contract.outputs == {"filtered": "table", "monthly": "image"}
    assert entry.contract.parameters["minimum"].constraints.minimum == 0.0
    # The record revision and the change notification name it.
    path = f"registrations/{record['registrationId']}"
    assert service.entity_revisions[path] == 1
    assert service.notifications[-1]["changedRecords"] == [path]


def test_registration_never_executes_the_script(service: WorkspaceService):
    script = (
        "from pathlib import Path\n"
        f"Path('{MARKER_NAME}').write_text('the service ran the script')\n"
        "raise SystemExit('registration must not execute the script')\n"
    )
    response = register(service, "02-register", script)
    assert response.ok, response.errors
    # No run record, no execution, no side effect of the captured bytes.
    assert service.store.records_of_kind(RUN_KIND) == []
    assert service.runs == {}
    assert not list(service.store.directory.rglob(MARKER_NAME))
    assert not Path.cwd().joinpath(MARKER_NAME).exists()


def test_script_edits_create_new_registrations(service: WorkspaceService):
    first = register(service, "02-register", SALES_SCRIPT)
    assert first.ok, first.errors
    second = register(service, "03-register-edited", EDITED_SALES_SCRIPT)
    assert second.ok, second.errors
    assert second.result["registrationId"] != first.result["registrationId"]
    assert second.result["revision"] == 1
    assert second.result["created"] is True
    # Both registrations stay committed with their own captured bytes.
    records = service.store.records_of_kind(REGISTRATION_KIND)
    assert len(records) == 2
    artifacts = {record.data["script"]["artifact"] for record in records}
    assert artifacts == {
        first.result["script"]["artifact"],
        second.result["script"]["artifact"],
    }
    assert service.action_registry.entry(first.result["registrationId"]) is not None
    assert service.action_registry.entry(second.result["registrationId"]) is not None


def test_identical_reregistration_answers_the_stored_registration(
    service: WorkspaceService,
):
    first = register(service, "02-register", SALES_SCRIPT)
    assert first.ok, first.errors
    again = register(service, "03-register-again", SALES_SCRIPT)
    assert again.ok, again.errors
    assert again.result["registrationId"] == first.result["registrationId"]
    assert again.result["revision"] == first.result["revision"]
    assert again.result["created"] is False
    # Nothing new was committed beyond the receipt of the request.
    assert len(service.store.records_of_kind(REGISTRATION_KIND)) == 1
    assert service.entity_revisions[
        f"registrations/{first.result['registrationId']}"
    ] == 1


def test_changed_contract_advances_the_immutable_revision(
    service: WorkspaceService,
):
    first = register(service, "02-register", SALES_SCRIPT)
    assert first.ok, first.errors
    registration_id = first.result["registrationId"]
    revised = register(service, "03-register-lean", SALES_SCRIPT, LEAN_CONTRACT)
    assert revised.ok, revised.errors
    assert revised.result["registrationId"] == registration_id
    assert revised.result["revision"] == 2
    assert revised.result["created"] is True
    # The registry exposes the newest revision; both stay committed.
    assert service.action_registry.entry(registration_id).revision == 2
    records = service.store.records_of_kind(REGISTRATION_KIND)
    assert sorted(record.data["revision"] for record in records) == [1, 2]
    assert service.entity_revisions[f"registrations/{registration_id}"] == 2
    assert service.notifications[-1]["changedRecords"] == [
        f"registrations/{registration_id}"
    ]


# ---------------------------------------------------------------------------
# Tokens, environments, receipts (SPEC.md sections 12.5 and 12.6)
# ---------------------------------------------------------------------------


def test_registration_rejects_unknown_and_non_script_tokens(
    service: WorkspaceService,
):
    unknown = service.dispatch(
        register_envelope("02-unknown", "melttok_nobody", SALES_CONTRACT)
    )
    assert unknown.ok is False
    assert [(error.code, error.path) for error in unknown.errors] == [
        ("E_ARTIFACT", "/payload/scriptToken")
    ]

    parquet_token = service.capture_artifact_token(sales_parquet(), PARQUET).token
    refused = service.dispatch(
        register_envelope("02-parquet", parquet_token, SALES_CONTRACT)
    )
    assert refused.ok is False
    assert [(error.code, error.path) for error in refused.errors] == [
        ("E_ARTIFACT", "/payload/scriptToken")
    ]
    assert "script" in refused.errors[0].message
    assert service.store.records_of_kind(REGISTRATION_KIND) == []


def test_registration_rejects_an_unknown_environment(service: WorkspaceService):
    answer = service.dispatch(
        register_envelope(
            "02-env",
            capture_script(service, SALES_SCRIPT),
            SALES_CONTRACT,
            environment_id="env-that-the-service-never-resolved",
        )
    )
    assert answer.ok is False
    assert [(error.code, error.path) for error in answer.errors] == [
        ("E_REFERENCE", "/payload/environmentId")
    ]
    assert service.store.records_of_kind(REGISTRATION_KIND) == []


def test_registration_rejects_mismatched_contract_constraints(
    service: WorkspaceService,
):
    contract = {
        "parameters": {
            "minimum": {
                "type": "number",
                "nullable": False,
                "constraints": {"minimum": 0, "maxLength": 8},
            }
        },
        "inputs": {},
        "outputs": {},
    }
    answer = service.dispatch(
        register_envelope("02-contract", capture_script(service, SALES_SCRIPT), contract)
    )
    assert answer.ok is False
    assert [(error.code, error.path) for error in answer.errors] == [
        (
            "E_SCHEMA",
            "/payload/contract/parameters/minimum/constraints",
        )
    ]
    assert service.store.records_of_kind(REGISTRATION_KIND) == []


def test_a_contract_cannot_declare_a_permission(service: WorkspaceService):
    contract = {
        "parameters": {},
        "inputs": {},
        "outputs": {},
        "requiredPermission": "something-the-document-wants",
    }
    # The envelope itself is refused: a registration contract carries
    # no permission grant (SPEC.md section 12.6).
    payload = json.dumps(
        {
            "protocolVersion": 1,
            "workspaceId": WORKSPACE,
            "requestId": "02-permission",
            "method": "action.register",
            "payload": {
                "scriptToken": capture_script(service, SALES_SCRIPT),
                "environmentId": local_environment().environment_id,
                "contract": contract,
            },
        }
    ).encode("utf-8")
    answer = service.handle_request(payload)
    assert answer.ok is False
    assert answer.errors[0].code == "E_SCHEMA"
    assert answer.errors[0].path.startswith("/payload/contract")
    assert service.store.records_of_kind(REGISTRATION_KIND) == []


def test_registration_replays_its_receipt_and_refuses_reuse(
    service: WorkspaceService,
):
    token = capture_script(service, SALES_SCRIPT)
    envelope = register_envelope("02-register", token, SALES_CONTRACT)
    first = service.dispatch(envelope)
    assert first.ok, first.errors
    replay = service.dispatch(
        register_envelope("02-register", token, SALES_CONTRACT)
    )
    assert replay.ok, replay.errors
    assert model_to_wire(replay) == model_to_wire(first)
    assert len(service.store.records_of_kind(REGISTRATION_KIND)) == 1
    other = service.dispatch(
        register_envelope("02-register", token, LEAN_CONTRACT)
    )
    assert other.ok is False
    assert [(error.code, error.path) for error in other.errors] == [
        ("E_REQUEST_REUSE", "/requestId")
    ]


def test_registration_requires_a_created_workspace(service_adapter: PlatformAdapter):
    service = WorkspaceService.open(service_adapter, WORKSPACE)
    try:
        answer = service.dispatch(
            register_envelope(
                "02-register", capture_script(service, SALES_SCRIPT), SALES_CONTRACT
            )
        )
        assert answer.ok is False
        assert [(error.code, error.path) for error in answer.errors] == [
            ("E_REFERENCE", "/workspaceId")
        ]
    finally:
        service.close()


# ---------------------------------------------------------------------------
# Registered contracts admit definitions (SPEC.md sections 7.2 and 12.6)
# ---------------------------------------------------------------------------


def sales_definitions(registration_id: str, parameter_constraints: dict) -> list:
    """The apply operations of the interactive sales path."""

    return [
        {
            "op": "entity.create",
            "collection": "parameters",
            "id": "minimum",
            "value": {
                "type": "number",
                "default": 0,
                "nullable": False,
                "constraints": parameter_constraints,
            },
        },
        {
            "op": "entity.create",
            "collection": "actions",
            "id": "analyze-sales",
            "value": {
                "implementation": {"registrationId": registration_id},
                "parameters": {"minimum": {"parameter": "minimum"}},
                "inputs": {"sales": {"sourceId": "sales-input", "version": 1}},
                "outputs": {"filtered": "table", "monthly": "image"},
                "outputGroup": "sales-results",
                "targets": {"filtered": "sales", "monthly": "revenue"},
            },
        },
        {
            "op": "entity.create",
            "collection": "projections",
            "id": "sales",
            "value": {
                "kind": "table",
                "title": "Sales",
                "source": None,
                "config": {"columns": ["month", "revenue"], "selection": "none"},
                "controls": [],
            },
        },
        {
            "op": "entity.create",
            "collection": "projections",
            "id": "revenue",
            "value": {
                "kind": "plot",
                "title": "Revenue by month",
                "source": None,
                "config": {"fit": "contain"},
                "controls": [
                    {"id": "run", "kind": "button", "label": "Run analysis"}
                ],
            },
        },
        {
            "op": "entity.create",
            "collection": "bindings",
            "id": "manual-run",
            "value": {
                "event": {
                    "kind": "control.activate",
                    "projection": "revenue",
                    "control": "run",
                },
                "effect": {"kind": "action.invoke", "action": "analyze-sales"},
                "debounceMs": 0,
            },
        },
        {
            "op": "layout.replace",
            "value": {
                "root": {
                    "kind": "split",
                    "axis": "horizontal",
                    "ratio": 0.55,
                    "first": {"kind": "pane", "projection": "sales"},
                    "second": {"kind": "pane", "projection": "revenue"},
                },
                "hidden": [],
            },
        },
    ]


def registered_workspace(service: WorkspaceService) -> str:
    """Register the sales script, import the table, apply definitions."""

    registration = register(service, "02-register", SALES_SCRIPT)
    assert registration.ok, registration.errors
    registration_id = registration.result["registrationId"]
    assert import_sales(service, "03-import").ok
    applied = service.dispatch(
        apply_envelope(
            "04-definitions",
            {
                "parameters/minimum": 0,
                "actions/analyze-sales": 0,
                "projections/sales": 0,
                "projections/revenue": 0,
                "bindings/manual-run": 0,
                "layout": 1,
            },
            sales_definitions(
                registration_id,
                {"minimum": 0, "maximum": 1000000000},
            ),
        )
    )
    assert applied.ok, applied.errors
    return registration_id


def test_registered_contracts_admit_action_definitions(service: WorkspaceService):
    registration_id = registered_workspace(service)
    inspect = service.dispatch(
        parse_command(
            {
                "protocolVersion": 1,
                "workspaceId": WORKSPACE,
                "requestId": "05-inspect",
                "method": "workspace.inspect",
                "payload": {},
            }
        )
    )
    assert inspect.ok, inspect.errors
    assert inspect.result["diagnostics"] == []
    assert (
        inspect.result["entityRevisions"][f"registrations/{registration_id}"] == 1
    )
    # A bound activation resolves the registration and stops at the
    # missing permission grant (SPEC.md sections 12.6 and 13.1).
    activation = service.dispatch(
        parse_command(
            {
                "protocolVersion": 1,
                "workspaceId": WORKSPACE,
                "requestId": "06-activate",
                "method": "control.activate",
                "expected": {"projections/revenue": 1, "bindings/manual-run": 1},
                "payload": {"projectionId": "revenue", "controlId": "run"},
            }
        )
    )
    assert activation.ok, activation.errors
    assert activation.result["status"] == "approvalRequired"
    assert activation.result["requiredPermission"] == "execute_project_code"


def test_open_constraint_dimensions_cannot_feed_bounded_slots(
    service: WorkspaceService,
):
    """A mapping must exactly satisfy the registration contract.

    The registration bounds the mapped parameter with a minimum and a
    maximum. A workspace parameter that leaves either dimension open
    would accept values the registration rejects, so the apply is
    refused (SPEC.md section 7.2).
    """

    registration = register(service, "02-register", SALES_SCRIPT)
    assert registration.ok, registration.errors
    assert import_sales(service, "03-import").ok
    cases = {
        "no constraints": {},
        "minimum without maximum": {"minimum": 0},
        "maximum without minimum": {"maximum": 1000000000},
    }
    for label, constraints in cases.items():
        applied = service.dispatch(
            apply_envelope(
                f"04-definitions-{label.replace(' ', '-')}",
                {
                    "parameters/minimum": 0,
                    "actions/analyze-sales": 0,
                    "projections/sales": 0,
                    "projections/revenue": 0,
                    "bindings/manual-run": 0,
                    "layout": 1,
                },
                sales_definitions(
                    registration.result["registrationId"], constraints
                ),
            )
        )
        assert applied.ok is False, label
        pairs = [(error.code, error.path) for error in applied.errors]
        assert (
            "E_REGISTRATION",
            "/actions/analyze-sales/parameters/minimum",
        ) in pairs, label
    # Nothing was applied: the workspace still holds no actions.
    assert service.document["actions"] == {}


# ---------------------------------------------------------------------------
# Restart recovery (SPEC.md sections 7.2 and 14)
# ---------------------------------------------------------------------------


def test_restart_restores_the_registry(service_adapter: PlatformAdapter):
    service = WorkspaceService.open(service_adapter, WORKSPACE)
    try:
        created = service.create_workspace(
            json.loads(json.dumps(EMPTY_DOCUMENT)), "01-create"
        )
        assert created.ok, created.errors
        registration_id = registered_workspace(service)
        stale_token = capture_script(service, EDITED_SALES_SCRIPT)
    finally:
        service.close()

    reopened = WorkspaceService.open(service_adapter, WORKSPACE)
    try:
        # The registration came back from its committed record.
        assert list(reopened.registrations) == [registration_id]
        entry = reopened.action_registry.entry(registration_id)
        assert entry is not None
        assert entry.revision == 1
        assert entry.contract.outputs == {"filtered": "table", "monthly": "image"}
        assert (
            reopened.entity_revisions[f"registrations/{registration_id}"] == 1
        )
        # The restored workspace compiles cleanly: the saved action
        # resolves against the rebuilt registry.
        inspect = reopened.dispatch(
            parse_command(
                {
                    "protocolVersion": 1,
                    "workspaceId": WORKSPACE,
                    "requestId": "07-inspect",
                    "method": "workspace.inspect",
                    "payload": {},
                }
            )
        )
        assert inspect.ok, inspect.errors
        assert inspect.result["diagnostics"] == []
        # A restart withdraws every outstanding token.
        refused = reopened.dispatch(
            register_envelope("08-stale", stale_token, SALES_CONTRACT)
        )
        assert refused.ok is False
        assert refused.errors[0].code == "E_ARTIFACT"
        # The identical capture still resolves to the stored
        # registration: the registry answers from its rebuilt record,
        # and no second registration is committed.
        again = register(reopened, "09-register-again", SALES_SCRIPT)
        assert again.ok, again.errors
        assert again.result["registrationId"] == registration_id
        assert again.result["created"] is False
        assert len(reopened.store.records_of_kind(REGISTRATION_KIND)) == 1
    finally:
        reopened.close()


def test_unknown_registration_disables_actions_but_keeps_projections_readable(
    service_adapter: PlatformAdapter,
):
    """Restoration survives an unavailable registration (7.2, 14).

    The definitions cite a fixture registration. After a restart
    without that fixture registry, the action is disabled while the
    saved projections stay readable.
    """

    from melt.schema.commands import RegistrationContract
    from runtime.compiler.catalog import ActionRegistry, RegistrationEntry

    contract = RegistrationContract.model_validate(
        {
            "parameters": {},
            "inputs": {},
            "outputs": {"summary": "table"},
        }
    )
    registry = ActionRegistry(
        {"summarize-script-v1": RegistrationEntry("summarize-script-v1", 1, contract)}
    )
    service = WorkspaceService.open(service_adapter, WORKSPACE, action_registry=registry)
    try:
        created = service.create_workspace(
            json.loads(json.dumps(EMPTY_DOCUMENT)), "01-create"
        )
        assert created.ok, created.errors
        applied = service.dispatch(
            apply_envelope(
                "02-definitions",
                {
                    "actions/summarize-sales": 0,
                    "projections/summary-table": 0,
                    "bindings/manual-run": 0,
                    "layout": 1,
                },
                [
                    {
                        "op": "entity.create",
                        "collection": "actions",
                        "id": "summarize-sales",
                        "value": {
                            "implementation": {
                                "registrationId": "summarize-script-v1"
                            },
                            "parameters": {},
                            "inputs": {},
                            "outputs": {"summary": "table"},
                            "outputGroup": "sales-summary",
                            "targets": {"summary": "summary-table"},
                        },
                    },
                    {
                        "op": "entity.create",
                        "collection": "projections",
                        "id": "summary-table",
                        "value": {
                            "kind": "table",
                            "title": "Summary",
                            "source": None,
                            "config": {"columns": None, "selection": "none"},
                            "controls": [
                                {"id": "run", "kind": "button", "label": "Run"}
                            ],
                        },
                    },
                    {
                        "op": "entity.create",
                        "collection": "bindings",
                        "id": "manual-run",
                        "value": {
                            "event": {
                                "kind": "control.activate",
                                "projection": "summary-table",
                                "control": "run",
                            },
                            "effect": {
                                "kind": "action.invoke",
                                "action": "summarize-sales",
                            },
                            "debounceMs": 0,
                        },
                    },
                    {
                        "op": "layout.replace",
                        "value": {"root": None, "hidden": ["summary-table"]},
                    },
                ],
            )
        )
        assert applied.ok, applied.errors
    finally:
        service.close()

    # Reopened without the fixture registry, the registration is
    # unavailable: the action is disabled, the workspace stays open.
    reopened = WorkspaceService.open(service_adapter, WORKSPACE)
    try:
        inspect = reopened.dispatch(
            parse_command(
                {
                    "protocolVersion": 1,
                    "workspaceId": WORKSPACE,
                    "requestId": "03-inspect",
                    "method": "workspace.inspect",
                    "payload": {},
                }
            )
        )
        assert inspect.ok, inspect.errors
        pairs = [
            (item["code"], item["path"]) for item in inspect.result["diagnostics"]
        ]
        assert (
            "E_REGISTRATION",
            "/actions/summarize-sales/implementation/registrationId",
        ) in pairs
        # The saved projections remain readable.
        projections = inspect.result["document"]["projections"]
        assert projections["summary-table"]["title"] == "Summary"
        # An activation of the disabled action is refused, not run.
        activation = reopened.dispatch(
            parse_command(
                {
                    "protocolVersion": 1,
                    "workspaceId": WORKSPACE,
                    "requestId": "04-activate",
                    "method": "control.activate",
                    "expected": {
                        "projections/summary-table": 1,
                        "bindings/manual-run": 1,
                    },
                    "payload": {"projectionId": "summary-table", "controlId": "run"},
                }
            )
        )
        assert activation.ok is False
        assert activation.errors[0].code == "E_REGISTRATION"
        assert "disabled" in activation.errors[0].message
    finally:
        reopened.close()
