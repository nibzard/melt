"""Scheduling, snapshots, and generations (T016, SPEC.md sections 7.3, 9.3, 13.1).

The tests prove the scheduling loop of the workspace service end to end
against an isolated workspace with real durable storage:

- Every admission captures the full invocation snapshot of section 7.3:
  registration revision, script hash, environment fingerprint, input
  source references, parameter values with their state revisions, the
  action revision, the output group revision, the generation, and the
  permission decision reference.
- A qualifying event reserves its generation in the admitting
  transaction, so older runs become ineligible for current publication
  immediately, not when a timer fires. A generation is never reused.
- Bindings that a committed user change fires coalesce into one intent
  per action with the final parameter values; repeated events replace
  the pending intent of the output group and restart its timer.
- The service owns the debounce timers: dispatch rechecks definitions
  and authority, expires stale intents, and mints runs from the
  captured values alone.
- Launch rechecks registration availability and the environment
  fingerprint immediately before the fresh process starts, runs the
  specification's own sales script with the hero numbers of section
  18.2, and never lets a revoked grant execute.
- A restart marks pending timers and active jobs interrupted, keeps the
  reserved generations, and never executes anything on its own.

Everything runs under the Looper resource prefix, never against the
user's data directory.
"""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pytest

from melt.schema.commands import parse_command
from runtime.storage import PlatformAdapter, WorkspaceStore, storage_worker_available
from runtime.workspace import RUN_KIND, WorkspaceService, local_environment

STORAGE_AVAILABLE, STORAGE_UNAVAILABLE_REASON = storage_worker_available()

requires_storage_worker = pytest.mark.skipif(
    not STORAGE_AVAILABLE, reason=STORAGE_UNAVAILABLE_REASON
)

pytestmark = requires_storage_worker

WORKSPACE = "sales-demo"

PARQUET = "application/vnd.apache.parquet"
PYTHON = "text/x-python"

# The example script of SPEC.md section 13.2, copied word for word
# except for the figure close the example performs after staging.
SPEC_SALES_SCRIPT = (
    "import pandas as pd\n"
    "import matplotlib.pyplot as plt\n"
    "from melt import ui\n"
    "\n"
    "sales = pd.read_parquet(ui.input_path('sales'))\n"
    "selected = sales[sales['revenue'] >= ui.params['minimum']]\n"
    "monthly = selected.groupby('month', sort=True)['revenue'].sum()\n"
    "fig, ax = plt.subplots()\n"
    "monthly.plot(ax=ax)\n"
    "ax.set_ylabel('Revenue')\n"
    "ui.output('filtered', selected)\n"
    "ui.output('monthly', fig)\n"
    "plt.close(fig)\n"
)

# The same loop without the plot, so scheduling tests that do launch
# stay quick.
LIGHT_SALES_SCRIPT = (
    "import pandas as pd\n"
    "from melt import ui\n"
    "\n"
    "sales = pd.read_parquet(ui.input_path('sales'))\n"
    "selected = sales[sales['revenue'] >= ui.params['minimum']]\n"
    "ui.output('filtered', selected)\n"
)

FULL_CONTRACT = {
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

LIGHT_CONTRACT = {
    "parameters": {
        "minimum": {
            "type": "number",
            "nullable": False,
            "constraints": {"minimum": 0, "maximum": 1000000000},
        }
    },
    "inputs": {"sales": "table"},
    "outputs": {"filtered": "table"},
}

MINIMUM_PARAMETER = {
    "type": "number",
    "default": 0,
    "nullable": False,
    "constraints": {"minimum": 0, "maximum": 1000000000},
}

SALES_PROJECTION = {
    "kind": "table",
    "title": "Sales",
    "source": None,
    "config": {"columns": ["month", "revenue"], "selection": "none"},
    "controls": [
        {
            "id": "minimum",
            "kind": "number",
            "label": "Minimum revenue",
            "parameter": "minimum",
        },
        {"id": "run", "kind": "button", "label": "Run analysis"},
    ],
}

REVENUE_PROJECTION = {
    "kind": "plot",
    "title": "Revenue by month",
    "source": None,
    "config": {"fit": "contain"},
    "controls": [],
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


class FakeClock:
    """A controllable scheduling clock."""

    def __init__(self, epoch: float = 1_760_000_000.0):
        self.epoch = epoch

    def __call__(self) -> float:
        return self.epoch

    def advance(self, milliseconds: float) -> float:
        self.epoch += milliseconds / 1000
        return self.epoch


@dataclass
class SeededWorkspace:
    """One prepared sales workspace and the identifiers it minted."""

    service: WorkspaceService
    clock: FakeClock
    registration_id: str


def envelope(
    request_id: str,
    *,
    method: str,
    payload: "dict | None" = None,
    expected: "dict | None" = None,
    operations: "list | None" = None,
):
    body = {
        "protocolVersion": 1,
        "workspaceId": WORKSPACE,
        "requestId": request_id,
        "method": method,
        "expected": expected,
    }
    if operations is not None:
        body["operations"] = operations
    else:
        body["payload"] = payload if payload is not None else {}
    return parse_command(body)


def apply_envelope(request_id: str, expected: "dict | None", operations: list):
    return envelope(
        request_id, method="workspace.apply", expected=expected, operations=operations
    )


def sales_action(registration_id: str, **overrides) -> dict:
    action = {
        "implementation": {"registrationId": registration_id},
        "parameters": {"minimum": {"parameter": "minimum"}},
        "inputs": {"sales": {"sourceId": "sales-input", "version": 1}},
        "outputs": {"filtered": "table"},
        "outputGroup": "sales-results",
        "targets": {"filtered": "sales"},
    }
    action.update(overrides)
    return action


def seed_workspace(
    adapter: PlatformAdapter,
    *,
    clock: "FakeClock | None" = None,
    debounce_ms: int = 250,
    script: str = LIGHT_SALES_SCRIPT,
    contract: dict | None = None,
    parameters: "dict | None" = None,
    action_overrides: "dict | None" = None,
    extra_bindings: "dict[str, dict] | None" = None,
    extra_projections: "dict[str, dict] | None" = None,
    grant: bool = True,
) -> SeededWorkspace:
    """Build the sales workspace through the real service operations.

    Creation, import, registration, and the definitions apply all ride
    the same command envelope a host would send, so the scheduling
    tests exercise the exact service handlers of SPEC.md section 12.6.
    """

    clock = clock or FakeClock()
    contract = contract or LIGHT_CONTRACT
    parameter_definitions = parameters or {"minimum": MINIMUM_PARAMETER}
    service = WorkspaceService.open(adapter, WORKSPACE, clock=clock)
    created = service.create_workspace(
        {
            "language": "melt/v0.1",
            "workspaceId": WORKSPACE,
            "title": "Sales analysis",
            "sources": {},
            "parameters": parameter_definitions,
            "actions": {},
            "projections": {},
            "bindings": {},
            "layout": {"root": None, "hidden": []},
        },
        "01-create",
    )
    assert created.ok, created.errors

    token = service.capture_artifact_token(
        BUILDER.sales_parquet_bytes(), PARQUET
    ).token
    imported = service.dispatch(
        envelope(
            "02-import",
            method="source.import",
            expected={"sources/sales-input": 0},
            payload={
                "sourceId": "sales-input",
                "artifactToken": token,
                "originLabel": "sales.csv",
            },
        )
    )
    assert imported.ok, imported.errors

    script_token = service.capture_artifact_token(script.encode("utf-8"), PYTHON).token
    registered = service.dispatch(
        envelope(
            "03-register",
            method="action.register",
            payload={
                "scriptToken": script_token,
                "environmentId": local_environment().environment_id,
                "contract": contract,
            },
        )
    )
    assert registered.ok, registered.errors
    registration_id = registered.result["registrationId"]

    bindings = {
        "minimum-rerun": {
            "event": {"kind": "parameter.changed", "parameter": "minimum"},
            "effect": {"kind": "action.invoke", "action": "analyze-sales"},
            "debounceMs": debounce_ms,
        },
        "manual-run": {
            "event": {
                "kind": "control.activate",
                "projection": "sales",
                "control": "run",
            },
            "effect": {"kind": "action.invoke", "action": "analyze-sales"},
            "debounceMs": 0,
        },
    }
    bindings.update(extra_bindings or {})
    extras = extra_projections or {}
    definitions = service.apply(
        apply_envelope(
            "04-definitions",
            {
                "actions/analyze-sales": 0,
                "projections/sales": 0,
                **{f"projections/{name}": 0 for name in extras},
                **{f"bindings/{name}": 0 for name in bindings},
                "layout": 1,
            },
            [
                {
                    "op": "entity.create",
                    "collection": "actions",
                    "id": "analyze-sales",
                    "value": sales_action(registration_id, **(action_overrides or {})),
                },
                {
                    "op": "entity.create",
                    "collection": "projections",
                    "id": "sales",
                    "value": SALES_PROJECTION,
                },
                *(
                    {
                        "op": "entity.create",
                        "collection": "projections",
                        "id": name,
                        "value": projection,
                    }
                    for name, projection in extras.items()
                ),
                *(
                    {
                        "op": "entity.create",
                        "collection": "bindings",
                        "id": name,
                        "value": binding,
                    }
                    for name, binding in bindings.items()
                ),
                {
                    "op": "layout.replace",
                    "value": {
                        "root": {"kind": "pane", "projection": "sales"},
                        "hidden": sorted(extras),
                    },
                },
            ],
        )
    )
    assert definitions.ok, definitions.errors

    if grant:
        service.grant_execution_permission(
            kind="projectExecution",
            registration_id=registration_id,
            granted_by="host-permission-control/run-approval",
        )
    return SeededWorkspace(
        service=service, clock=clock, registration_id=registration_id
    )


def invoke(service: WorkspaceService, request_id: str = "05-invoke"):
    return service.dispatch(
        envelope(
            request_id,
            method="action.invoke",
            expected={"actions/analyze-sales": 1},
            payload={"actionId": "analyze-sales"},
        )
    )


def activate(service: WorkspaceService, request_id: str = "05-activate"):
    return service.dispatch(
        envelope(
            request_id,
            method="control.activate",
            expected={"projections/sales": 1, "bindings/manual-run": 1},
            payload={"projectionId": "sales", "controlId": "run"},
        )
    )


def set_parameter(
    service: WorkspaceService,
    request_id: str,
    *,
    value,
    expected_revision: int,
    origin: str = "user",
    parameter: str = "minimum",
):
    envelope_to_send = apply_envelope(
        request_id,
        {f"state/{parameter}": expected_revision},
        [{"op": "parameter.set", "id": parameter, "value": value}],
    )
    return service.dispatch_as(envelope_to_send, origin=origin)


def launch(service: WorkspaceService, run_id: str):
    outcome = service.launch_admitted_run(run_id)
    assert outcome.launched, outcome.refusal
    return outcome


# ---------------------------------------------------------------------------
# Manual invocation and the launch loop (SPEC.md sections 13.1 and 13.2)
# ---------------------------------------------------------------------------


def test_manual_invocation_admits_a_run_with_the_full_snapshot(
    service_adapter: PlatformAdapter,
):
    seeded = seed_workspace(service_adapter)
    service = seeded.service
    sequence = service.commit_sequence
    response = invoke(service)
    assert response.ok, response.errors
    result = response.result
    assert result["status"] == "granted"
    assert result["origin"] == "user"
    # The admitting transaction reserved the generation and minted the
    # queued run in one commit (SPEC.md sections 9.3 and 13.1).
    assert service.commit_sequence == sequence + 1
    run = service.runs[result["run"]["runId"]]
    registration = service.registrations[seeded.registration_id]
    assert run.state == "queued"
    assert run.disposition == "none"
    assert run.generation == 1
    assert run.outputGroup == "sales-results"
    assert run.outputGroupRevision == 1
    assert run.actionId == "analyze-sales"
    assert run.actionRevision == 1
    assert run.registrationId == seeded.registration_id
    assert run.registrationRevision == 1
    assert run.scriptArtifact == registration.script.artifact
    assert run.environmentFingerprint == registration.environmentFingerprint
    assert run.inputs["sales"].sourceId == "sales-input"
    assert run.inputs["sales"].version == 1
    # The invocation snapshot carries the parameter value and revision
    # (SPEC.md section 7.3).
    assert run.parameters["minimum"].value == 0
    assert run.parameters["minimum"].revision == 1
    assert run.permissionDecision is not None
    assert service.output_groups["sales-results"].generation == 1
    assert service.pending_intents["sales-results"].status == "dispatched"


def test_manual_invocation_launches_the_specification_script(
    service_adapter: PlatformAdapter,
):
    seeded = seed_workspace(
        service_adapter,
        script=SPEC_SALES_SCRIPT,
        contract=FULL_CONTRACT,
        action_overrides={
            "outputs": {"filtered": "table", "monthly": "image"},
            "targets": {"filtered": "sales", "monthly": "revenue"},
        },
        extra_projections={"revenue": REVENUE_PROJECTION},
    )
    service = seeded.service
    set_parameter(service, "05-set-minimum", value=200, expected_revision=1)
    dispatched = service.dispatch_due_intents(seeded.clock.advance(300))
    assert [item["status"] for item in dispatched] == ["dispatched"]
    run_id = dispatched[0]["run"]["runId"]

    outcome = launch(service, run_id)
    assert outcome.state == "succeeded"
    assert outcome.exit_code == 0
    assert outcome.staged["runId"] == run_id
    # The hero numbers of SPEC.md section 18.2 at minimum 200.
    filtered = pd.read_parquet(outcome.staged["outputs"]["filtered"]["path"])
    assert len(filtered) == 2
    monthly = filtered.groupby("month", sort=True)["revenue"].sum()
    assert monthly.tolist() == [300.0, 200.0]
    assert outcome.staged["outputs"]["monthly"]["kind"] == "image"
    # The run record reached its terminal state durably.
    assert service.runs[run_id].state == "succeeded"
    assert service.runs[run_id].disposition == "none"

    # The manifest and the script live in the run's private staging
    # tree inside the workspace (SPEC.md sections 13.2 and 14).
    staging = service.store.layout.staging_directory(run_id)
    manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["runId"] == run_id
    assert manifest["workspaceId"] == WORKSPACE
    assert manifest["actionId"] == "analyze-sales"
    assert manifest["parameters"] == {"minimum": 200}
    assert manifest["outputs"] == {"filtered": "table", "monthly": "image"}
    assert manifest["stagingDirectory"] == str(staging)
    input_path = Path(manifest["inputs"]["sales"])
    assert input_path.is_file()
    assert input_path.read_bytes() == BUILDER.sales_parquet_bytes()
    assert (staging / "script.py").read_text(encoding="utf-8") == SPEC_SALES_SCRIPT
    # The captured input is the immutable artifact, addressed by hash.
    document = service.document
    artifact = document["sources"]["sales-input"]["versions"]["1"]["artifact"]
    assert input_path == service.store.artifacts.path_for(artifact)


def test_a_failing_script_marks_the_run_failed(
    service_adapter: PlatformAdapter,
):
    failing = LIGHT_SALES_SCRIPT + "raise RuntimeError('the analysis exploded')\n"
    seeded = seed_workspace(service_adapter, script=failing)
    service = seeded.service
    response = invoke(service)
    run_id = response.result["run"]["runId"]
    outcome = service.launch_admitted_run(run_id)
    assert outcome.launched
    assert outcome.state == "failed"
    assert outcome.exit_code == 1
    assert "the analysis exploded" in Path(outcome.stderr_path).read_text(
        encoding="utf-8"
    )
    assert service.runs[run_id].state == "failed"
    assert service.runs[run_id].disposition == "none"


def test_duplicate_invocation_admits_at_most_one_run(
    service_adapter: PlatformAdapter,
):
    seeded = seed_workspace(service_adapter)
    service = seeded.service
    first = invoke(service, "05-invoke")
    assert first.ok, first.errors
    sequence = service.commit_sequence
    replay = invoke(service, "05-invoke")
    assert replay.ok, replay.errors
    assert replay.result["run"]["runId"] == first.result["run"]["runId"]
    assert service.commit_sequence == sequence, "the receipt replayed, no new commit"
    assert len(service.runs) == 1


# ---------------------------------------------------------------------------
# Generations and immediate ineligibility (SPEC.md sections 9.3 and 13.1)
# ---------------------------------------------------------------------------


def test_a_bound_change_reserves_a_generation_immediately(
    service_adapter: PlatformAdapter,
):
    seeded = seed_workspace(service_adapter)
    service = seeded.service
    # A manual invocation admits a queued run at generation one.
    older = invoke(service).result["run"]["runId"]
    assert service.run_is_newest_generation(older)

    sequence = service.commit_sequence
    response = set_parameter(service, "05-set", value=200, expected_revision=1)
    assert response.ok, response.errors
    # The reservation happened in the committing transaction itself:
    # one commit, one new generation, one pending intent.
    assert service.commit_sequence == sequence + 1
    group = service.output_groups["sales-results"]
    assert group.generation == 2, "the timer has not fired, the generation exists"
    assert service.pending_intents["sales-results"].status == "pending"
    # The older run is ineligible the moment the reservation landed,
    # not when the debounce timer fires (SPEC.md section 9.3).
    assert not service.run_is_newest_generation(older)
    assert response.result["admissions"][0]["generation"] == 2
    # The notification names the scheduling records the commit changed.
    window = service.changes_after(sequence, service.epoch)
    assert "outputGroups/sales-results" in window.changes[-1]["changedRecords"]
    assert any(
        path.startswith("intents/") for path in window.changes[-1]["changedRecords"]
    )


def test_an_invalid_parameter_edit_reserves_nothing(
    service_adapter: PlatformAdapter,
):
    seeded = seed_workspace(service_adapter)
    service = seeded.service
    sequence = service.commit_sequence
    rejected = set_parameter(service, "05-invalid", value=-5, expected_revision=1)
    assert rejected.ok is False
    assert rejected.errors[0].code == "E_TYPE"
    assert service.commit_sequence == sequence
    assert service.output_groups == {}
    assert service.pending_intents == {}
    assert service.parameter_state["minimum"].revision == 1


def test_an_unbound_parameter_change_reserves_no_generation(
    service_adapter: PlatformAdapter,
):
    ceiling = dict(MINIMUM_PARAMETER)
    seeded = seed_workspace(
        service_adapter,
        parameters={
            "minimum": MINIMUM_PARAMETER,
            "ceiling": ceiling,
        },
    )
    service = seeded.service
    response = set_parameter(
        service, "05-set-ceiling", value=500, expected_revision=1, parameter="ceiling"
    )
    assert response.ok, response.errors
    assert service.parameter_state["ceiling"].value == 500
    # An unbound change commits its state and reserves nothing (SPEC.md
    # section 9.3).
    assert service.output_groups == {}
    assert service.pending_intents == {}
    assert "admissions" not in response.result


def test_a_programmatic_change_commits_without_events(
    service_adapter: PlatformAdapter,
):
    seeded = seed_workspace(service_adapter)
    service = seeded.service
    sequence = service.commit_sequence
    response = set_parameter(
        service,
        "05-programmatic",
        value=200,
        expected_revision=1,
        origin="programmatic",
    )
    assert response.ok, response.errors
    assert service.parameter_state["minimum"].value == 200
    assert service.parameter_state["minimum"].revision == 2
    # Programmatic changes emit no user event: no generation, no
    # intent, no run (SPEC.md sections 9.2 and 18.3).
    assert service.commit_sequence == sequence + 1
    assert service.output_groups == {}
    assert service.pending_intents == {}
    assert service.runs == {}


# ---------------------------------------------------------------------------
# Coalescing and debounce (SPEC.md sections 9.3 and 13.1)
# ---------------------------------------------------------------------------


def test_three_changes_during_the_window_leave_one_newest_intent(
    service_adapter: PlatformAdapter,
):
    seeded = seed_workspace(service_adapter)
    service = seeded.service
    for index, value in enumerate((100, 200, 300)):
        response = set_parameter(
            service, f"05-set-{index}", value=value, expected_revision=index + 1
        )
        assert response.ok, response.errors
        # Each commit replaced the pending request and restarted its
        # timer; one pending intent remains (SPEC.md sections 9.3 and
        # 18.3).
        assert len(service.pending_intents) == 1
    group = service.output_groups["sales-results"]
    assert group.generation == 3, "each qualifying event reserved one generation"
    intent = service.pending_intents["sales-results"]
    assert intent.status == "pending"
    assert intent.generation == 3
    assert intent.debounceMs == 250
    # The newest intent captured the final parameter values.
    assert intent.parameters["minimum"].value == 300
    assert intent.parameters["minimum"].revision == 4

    # Nothing dispatched before the timer: the captured values alone
    # ride with the run (SPEC.md sections 7.3 and 9.3).
    assert service.dispatch_due_intents(seeded.clock.advance(249)) == []
    assert service.runs == {}
    dispatched = service.dispatch_due_intents(seeded.clock.advance(1))
    assert [item["status"] for item in dispatched] == ["dispatched"]
    run = service.runs[dispatched[0]["run"]["runId"]]
    assert run.parameters["minimum"].value == 300
    assert run.parameters["minimum"].revision == 4
    assert run.generation == 3
    assert service.pending_intents["sales-results"].status == "dispatched"


def test_coalesced_bindings_form_one_intent_with_final_values(
    service_adapter: PlatformAdapter,
):
    contract = {
        "parameters": {
            "minimum": FULL_CONTRACT["parameters"]["minimum"],
            "ceiling": {
                "type": "number",
                "nullable": False,
                "constraints": {"minimum": 0, "maximum": 1000000000},
            },
        },
        "inputs": {"sales": "table"},
        "outputs": {"filtered": "table"},
    }
    seeded = seed_workspace(
        service_adapter,
        contract=contract,
        parameters={"minimum": MINIMUM_PARAMETER, "ceiling": MINIMUM_PARAMETER},
        action_overrides={
            "parameters": {
                "minimum": {"parameter": "minimum"},
                "ceiling": {"parameter": "ceiling"},
            },
        },
        extra_bindings={
            "ceiling-rerun": {
                "event": {"kind": "parameter.changed", "parameter": "ceiling"},
                "effect": {"kind": "action.invoke", "action": "analyze-sales"},
                "debounceMs": 100,
            },
        },
    )
    service = seeded.service
    # One transaction changes both bound parameters: the two bindings
    # coalesce into one intent with the final values (SPEC.md 13.1).
    response = service.apply(
        apply_envelope(
            "05-set-both",
            {"state/minimum": 1, "state/ceiling": 1},
            [
                {"op": "parameter.set", "id": "minimum", "value": 100},
                {"op": "parameter.set", "id": "ceiling", "value": 900},
            ],
        )
    )
    assert response.ok, response.errors
    admissions = response.result["admissions"]
    assert len(admissions) == 1, "one intent, not one per binding"
    assert admissions[0]["actionId"] == "analyze-sales"
    assert service.output_groups["sales-results"].generation == 1
    intent = service.pending_intents["sales-results"]
    assert intent.parameters["minimum"].value == 100
    assert intent.parameters["ceiling"].value == 900
    assert intent.debounceMs == 250, "the coalesced timer is the longest one"
    assert len(service.runs) == 0


def test_a_transaction_that_replaces_and_triggers_captures_the_new_revision(
    service_adapter: PlatformAdapter,
):
    seeded = seed_workspace(service_adapter)
    service = seeded.service
    # One transaction replaces the action and changes the bound
    # parameter: the triggered admission captures the revision this
    # transaction writes, so it is not stale against its own commit.
    response = service.apply(
        apply_envelope(
            "05-replace-and-set",
            {"actions/analyze-sales": 1, "state/minimum": 1},
            [
                {
                    "op": "entity.replace",
                    "collection": "actions",
                    "id": "analyze-sales",
                    "value": sales_action(seeded.registration_id),
                },
                {"op": "parameter.set", "id": "minimum", "value": 200},
            ],
        )
    )
    assert response.ok, response.errors
    intent = service.pending_intents["sales-results"]
    assert intent.status == "pending"
    assert intent.actionRevision == 2, "the captured revision is the commit's"
    assert intent.parameters["minimum"].value == 200
    dispatched = service.dispatch_due_intents(seeded.clock.advance(300))
    assert [item["status"] for item in dispatched] == ["dispatched"]
    run = service.runs[dispatched[0]["run"]["runId"]]
    assert run.actionRevision == 2


def test_dispatch_expires_an_intent_whose_definitions_went_stale(
    service_adapter: PlatformAdapter,
):
    seeded = seed_workspace(service_adapter)
    service = seeded.service
    assert set_parameter(service, "05-set", value=200, expected_revision=1).ok
    # Even an equivalent action replacement invalidates the admitted
    # work (SPEC.md sections 12.3 and 13.3).
    replaced = service.apply(
        apply_envelope(
            "06-replace-action",
            {"actions/analyze-sales": 1},
            [
                {
                    "op": "entity.replace",
                    "collection": "actions",
                    "id": "analyze-sales",
                    "value": sales_action(seeded.registration_id),
                }
            ],
        )
    )
    assert replaced.ok, replaced.errors
    assert service.pending_intents["sales-results"].status == "pending"
    dispatched = service.dispatch_due_intents(seeded.clock.advance(300))
    assert [item["status"] for item in dispatched] == ["expired"]
    assert "moved to revision 2" in dispatched[0]["reason"]
    assert service.runs == {}, "a stale definition never dispatches"
    assert service.pending_intents["sales-results"].status == "expired"


def test_output_group_revisions_follow_the_publication_contract(
    service_adapter: PlatformAdapter,
):
    seeded = seed_workspace(service_adapter)
    service = seeded.service
    assert set_parameter(service, "05-set", value=200, expected_revision=1).ok
    assert service.output_groups["sales-results"].revision == 1
    # A projection title change leaves the group revision alone
    # (SPEC.md section 13.3).
    retitled = service.apply(
        apply_envelope(
            "06-retitle",
            {"projections/sales": 1},
            [
                {
                    "op": "entity.replace",
                    "collection": "projections",
                    "id": "sales",
                    "value": dict(SALES_PROJECTION, title="Revenue ledger"),
                }
            ],
        )
    )
    assert retitled.ok, retitled.errors
    assert service.output_groups["sales-results"].revision == 1
    assert service.pending_intents["sales-results"].status == "pending"
    # Changing the output group the action owns is a contract change.
    moved = service.apply(
        apply_envelope(
            "07-move-group",
            {"actions/analyze-sales": 1},
            [
                {
                    "op": "entity.replace",
                    "collection": "actions",
                    "id": "analyze-sales",
                    "value": sales_action(
                        seeded.registration_id, outputGroup="results-v2"
                    ),
                }
            ],
        )
    )
    assert moved.ok, moved.errors
    assert service.output_groups["sales-results"].revision == 2
    # The pending intent of the old group cannot dispatch against the
    # bumped revision.
    dispatched = service.dispatch_due_intents(seeded.clock.advance(300))
    assert [item["status"] for item in dispatched] == ["expired"]
    assert service.runs == {}


# ---------------------------------------------------------------------------
# Approval and dispatch (SPEC.md sections 12.6 and 13.1)
# ---------------------------------------------------------------------------


def test_approval_required_then_a_pinned_grant_dispatches(
    service_adapter: PlatformAdapter,
):
    seeded = seed_workspace(service_adapter, grant=False)
    service = seeded.service
    sequence = service.commit_sequence
    response = activate(service, "05-activate")
    assert response.ok, response.errors
    result = response.result
    assert result["status"] == "approvalRequired"
    assert result["reason"] == "missing-grant"
    # The approval-required admission still reserved its generation and
    # persisted its intent (SPEC.md sections 9.3 and 12.6).
    assert service.commit_sequence == sequence + 1
    intent = result["intent"]
    assert intent["generation"] == 1
    assert intent["outputGroupRevision"] == 1
    assert service.output_groups["sales-results"].generation == 1
    assert service.pending_intents["sales-results"].status == "approvalRequired"
    assert service.runs == {}

    # The host approval pins exactly the captured intent, including the
    # generation and the output group revision.
    grant = service.grant_execution_permission(
        kind="specificRun",
        registration_id=seeded.registration_id,
        granted_by="host-permission-control/run-approval",
        action_id=intent["actionId"],
        action_revision=intent["actionRevision"],
        registration_revision=intent["registrationRevision"],
        output_group=intent["outputGroup"],
        output_group_revision=intent["outputGroupRevision"],
        intent_id=intent["intentRef"],
        generation=intent["generation"],
    )
    # The grant dispatched the intent it covers: one queued run with
    # the permission decision stored on the record.
    dispatched_intent = service.pending_intents["sales-results"]
    assert dispatched_intent.status == "dispatched"
    run_id = next(iter(service.runs))
    assert service.runs[run_id].permissionDecision == grant.grantId
    assert service.runs[run_id].state == "queued"
    outcome = launch(service, run_id)
    assert outcome.state == "succeeded"

    # A newer request is a new intent: the older approval never
    # executes it (SPEC.md section 12.6).
    newer = activate(service, "06-activate")
    assert newer.result["status"] == "approvalRequired"
    assert newer.result["intent"]["generation"] == 2
    assert newer.result["intent"]["intentRef"] != intent["intentRef"]
    assert service.pending_intents["sales-results"].status == "approvalRequired"


def test_a_revoked_grant_refuses_the_launch_and_cancels(
    service_adapter: PlatformAdapter,
):
    seeded = seed_workspace(service_adapter)
    service = seeded.service
    run_id = invoke(service).result["run"]["runId"]
    grant_id = service.runs[run_id].permissionDecision
    service.revoke_execution_permission(
        grant_id, revoked_by="host-permission-control/revoke"
    )
    outcome = service.launch_admitted_run(run_id)
    assert outcome.launched is False
    assert outcome.state == "cancelled"
    assert outcome.refusal[0].code == "E_PERMISSION"
    # Revocation requested cancellation of the run (SPEC.md 13.5).
    assert service.runs[run_id].state == "cancelled"
    assert service.runs[run_id].disposition == "none"


def test_launch_refuses_a_registration_that_drifted(
    service_adapter: PlatformAdapter,
):
    seeded = seed_workspace(service_adapter)
    service = seeded.service
    run_id = invoke(service).result["run"]["runId"]
    # A changed contract over the same capture advances the immutable
    # registration revision (SPEC.md section 7.2): the admitted run
    # pinned the older revision and cannot launch against it.
    script_token = service.capture_artifact_token(
        LIGHT_SALES_SCRIPT.encode("utf-8"), PYTHON
    ).token
    advanced = service.dispatch(
        envelope(
            "06-reregister",
            method="action.register",
            payload={
                "scriptToken": script_token,
                "environmentId": local_environment().environment_id,
                "contract": {
                    "parameters": LIGHT_CONTRACT["parameters"],
                    "inputs": {"sales": "table"},
                    "outputs": {"filtered": "table", "extra": "text"},
                },
            },
        )
    )
    assert advanced.ok, advanced.errors
    assert advanced.result["registrationId"] == seeded.registration_id
    assert advanced.result["revision"] == 2
    outcome = service.launch_admitted_run(run_id)
    assert outcome.launched is False
    assert outcome.refusal[0].code == "E_REGISTRATION"
    assert "moved to revision 2" in outcome.refusal[0].message
    # The refusal is not a failure of the run: it stays queued and no
    # execution was claimed (SPEC.md section 13.1).
    assert outcome.state == "queued"
    assert service.runs[run_id].state == "queued"


def test_a_fixture_registration_can_admit_but_never_launch(
    service_adapter: PlatformAdapter,
):
    # A registry entry without stored script bytes captures a snapshot
    # through its cited facts, but the launch path refuses it: only a
    # service-captured script can execute (SPEC.md sections 7.2 and 13.1).
    from runtime.compiler.catalog import ActionRegistry, RegistrationEntry
    from melt.schema.commands import RegistrationContract

    fixture_contract = {
        "parameters": LIGHT_CONTRACT["parameters"],
        "inputs": {},
        "outputs": {"filtered": "table"},
    }
    contract = RegistrationContract.model_validate(fixture_contract)
    registry = ActionRegistry(
        {
            "fixture-script-v1": RegistrationEntry(
                "fixture-script-v1",
                1,
                contract,
                script_artifact="sha256:" + "4d" * 32,
                environment_fingerprint=local_environment().fingerprint,
            )
        }
    )
    clock = FakeClock()
    service = WorkspaceService.open(
        service_adapter, WORKSPACE, action_registry=registry, clock=clock
    )
    created = service.create_workspace(
        {
            "language": "melt/v0.1",
            "workspaceId": WORKSPACE,
            "title": "Sales analysis",
            "sources": {},
            "parameters": {"minimum": MINIMUM_PARAMETER},
            "actions": {},
            "projections": {},
            "bindings": {},
            "layout": {"root": None, "hidden": []},
        },
        "01-create",
    )
    assert created.ok, created.errors
    definitions = service.apply(
        apply_envelope(
            "02-definitions",
            {
                "actions/analyze-sales": 0,
                "projections/sales": 0,
                "bindings/manual-run": 0,
                "layout": 1,
            },
            [
                {
                    "op": "entity.create",
                    "collection": "actions",
                    "id": "analyze-sales",
                    "value": sales_action("fixture-script-v1", inputs={}),
                },
                {
                    "op": "entity.create",
                    "collection": "projections",
                    "id": "sales",
                    "value": SALES_PROJECTION,
                },
                {
                    "op": "entity.create",
                    "collection": "bindings",
                    "id": "manual-run",
                    "value": {
                        "event": {
                            "kind": "control.activate",
                            "projection": "sales",
                            "control": "run",
                        },
                        "effect": {
                            "kind": "action.invoke",
                            "action": "analyze-sales",
                        },
                        "debounceMs": 0,
                    },
                },
                {
                    "op": "layout.replace",
                    "value": {
                        "root": {"kind": "pane", "projection": "sales"},
                        "hidden": [],
                    },
                },
            ],
        )
    )
    assert definitions.ok, definitions.errors
    service.grant_execution_permission(
        kind="projectExecution",
        registration_id="fixture-script-v1",
        granted_by="host-permission-control/run-approval",
    )
    response = invoke(service, "03-invoke")
    assert response.ok, response.errors
    run_id = response.result["run"]["runId"]
    assert service.runs[run_id].scriptArtifact == "sha256:" + "4d" * 32
    outcome = service.launch_admitted_run(run_id)
    assert outcome.launched is False
    assert outcome.refusal[0].code == "E_REGISTRATION"
    assert "no captured script" in outcome.refusal[0].message
    assert service.runs[run_id].state == "queued"


# ---------------------------------------------------------------------------
# Restart (SPEC.md sections 9.3 and 14)
# ---------------------------------------------------------------------------


def test_restart_marks_pending_work_interrupted_and_keeps_generations(
    service_adapter: PlatformAdapter,
):
    seeded = seed_workspace(service_adapter)
    service = seeded.service
    older_run = invoke(service, "05-invoke").result["run"]["runId"]
    assert set_parameter(service, "06-set", value=200, expected_revision=1).ok
    intent_ref = service.pending_intents["sales-results"].intentRef
    service.close()

    with WorkspaceService.open(service_adapter, WORKSPACE) as reopened:
        # The restart marked the active job and the pending timer
        # interrupted; it executed nothing and reserved nothing new
        # (SPEC.md sections 9.3 and 14, startup step 5).
        assert reopened.runs[older_run].state == "interrupted"
        intent = reopened.pending_intents["sales-results"]
        assert intent.status == "interrupted"
        assert intent.intentRef == intent_ref
        assert intent.reason == "service-restart"
        # The reserved generations survived.
        assert reopened.output_groups["sales-results"].generation == 2
        assert reopened.run_is_newest_generation(older_run) is False
        assert reopened.dispatch_due_intents(FakeClock().advance(10_000)) == []
        assert len(reopened.runs) == 1, "no run was admitted on restart"

    # A second restart finds nothing left to mark.
    with WorkspaceService.open(service_adapter, WORKSPACE) as again:
        assert again.runs[older_run].state == "interrupted"
        assert again.pending_intents["sales-results"].status == "interrupted"


def test_an_approval_required_intent_survives_a_restart(
    service_adapter: PlatformAdapter,
):
    seeded = seed_workspace(service_adapter, grant=False)
    seeded.service.close()
    with WorkspaceService.open(service_adapter, WORKSPACE) as service:
        response = activate(service, "05-activate")
        assert response.result["status"] == "approvalRequired"
        intent = response.result["intent"]
        service.close()
    with WorkspaceService.open(service_adapter, WORKSPACE) as reopened:
        # An approval-required request holds no work of its own: it
        # waits for the host approval across the restart.
        stored = reopened.pending_intents["sales-results"]
        assert stored.status == "approvalRequired"
        assert stored.intentRef == intent["intentRef"]
        grant = reopened.grant_execution_permission(
            kind="specificRun",
            registration_id=stored.registrationId,
            granted_by="host-permission-control/run-approval",
            action_id=stored.actionId,
            action_revision=stored.actionRevision,
            registration_revision=stored.registrationRevision,
            output_group=stored.outputGroup,
            output_group_revision=stored.outputGroupRevision,
            intent_id=stored.intentRef,
            generation=stored.generation,
        )
        assert reopened.pending_intents["sales-results"].status == "dispatched"
        run_id = next(iter(reopened.runs))
        assert reopened.runs[run_id].permissionDecision == grant.grantId


# ---------------------------------------------------------------------------
# The single execution slot (SPEC.md section 13.1)
# ---------------------------------------------------------------------------


def test_launch_next_run_drives_one_execution_at_a_time(
    service_adapter: PlatformAdapter,
):
    seeded = seed_workspace(service_adapter)
    service = seeded.service
    assert service.launch_next_run() is None, "nothing is queued yet"
    first = invoke(service, "05-invoke").result["run"]["runId"]
    second = activate(service, "06-activate").result["run"]["runId"]
    assert set(service.runs) == {first, second}
    outcome = service.launch_next_run()
    assert outcome.launched
    assert outcome.run_id in {first, second}
    # The other queued run launches only after the first completed:
    # one project execution at a time per workspace.
    remaining = (set(service.runs) - {outcome.run_id}).pop()
    assert service.runs[outcome.run_id].state == "succeeded"
    assert service.runs[remaining].state == "queued"
    second_outcome = service.launch_next_run()
    assert second_outcome.launched
    assert second_outcome.run_id == remaining
    assert service.runs[remaining].state == "succeeded"


def test_launch_refuses_a_run_that_is_not_queued(
    service_adapter: PlatformAdapter,
):
    seeded = seed_workspace(service_adapter)
    service = seeded.service
    run_id = invoke(service).result["run"]["runId"]
    cancelled = service.dispatch(
        envelope(
            "06-cancel",
            method="run.cancel",
            payload={"runId": run_id},
        )
    )
    assert cancelled.ok, cancelled.errors
    outcome = service.launch_admitted_run(run_id)
    assert outcome.launched is False
    assert outcome.refusal[0].code == "E_CONFLICT"
    assert "only a queued run can launch" in outcome.refusal[0].message


def test_scheduling_records_rebuild_from_the_record_stream(
    service_adapter: PlatformAdapter,
):
    seeded = seed_workspace(service_adapter)
    service = seeded.service
    invoke(service, "05-invoke")
    assert set_parameter(service, "06-set", value=200, expected_revision=1).ok
    service.close()
    store = WorkspaceStore.open(service_adapter, WORKSPACE)
    try:
        kinds = [record.kind for record in store.records()]
        run_records = [
            record.data for record in store.records() if record.kind == RUN_KIND
        ]
    finally:
        store.close()
    from runtime.workspace import OUTPUT_GROUP_KIND, PENDING_INTENT_KIND

    assert OUTPUT_GROUP_KIND in kinds
    assert PENDING_INTENT_KIND in kinds
    assert [record["state"] for record in run_records] == ["queued"]
