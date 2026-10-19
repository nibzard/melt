"""Output pointers stay bound to their owning action (T045, RFC 001 8.1).

The publication group name of RFC 001 section 8.1 is reusable: one
revision-checked transaction may move an action to another group and
create another action under the freed name. These tests hold the
pointer contract that keeps that reuse honest:

- A following subscriber never receives another action identity's run
  as its current result. Pointer resolution binds to the owning action
  and its stable output identity, never to the group name alone.
- An action with no publication of its own has no current pointer;
  inspection says so instead of reporting the previous owner's run.
- The unchanged owner keeps its subscribers resolving without a second
  execution, before and after a restart.
- Ownership changes preserve the historical artifacts of the previous
  owner and start no Python implicitly.
- Deletion and recreation, group swaps between published actions, and
  changed output contracts all fail closed: no pointer crosses an
  action identity.

The seeding harness reuses the T027 output-independence helpers, so
every step rides the exact service handlers of SPEC.md section 12.6
against isolated durable storage under the Looper resource prefix.
"""

from __future__ import annotations

from melt.schema.commands import parse_command
from runtime.workspace import WorkspaceService, local_environment

from test_output_independence import (
    FILTERED_SOURCE,
    MONTHLY_SOURCE,
    add_subscribers,
    incarnation_of,
    run_and_publish,
    seed_outputs_workspace,
    semantic_action,
    subscriber_of,
    subscriber_projection,
)
from test_scheduling import (
    FULL_CONTRACT,
    SPEC_SALES_SCRIPT,
    requires_storage_worker,
)

pytestmark = requires_storage_worker

WORKSPACE = "outputs-demo"


def command(
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


def apply_command(request_id: str, expected: "dict | None", operations: list):
    return command(
        request_id,
        method="workspace.apply",
        expected=expected,
        operations=operations,
    )


def registration_of(service: WorkspaceService) -> str:
    action = service.state.document["actions"]["analyze-sales"]
    return action["implementation"]["registrationId"]


def grouped_action(registration_id: str, group: str) -> dict:
    """The extension action of RFC 001 section 8.1 under a given group."""

    action = semantic_action(registration_id)
    action["outputGroup"] = group
    return action


def semantic_outputs(service: WorkspaceService) -> dict:
    """The outputs view of one inspection (RFC 001 section 9)."""

    response = service.dispatch(
        command("inspect-owner", method="workspace.inspect")
    )
    assert response.ok, response.errors
    return response.result["semantic"]["outputs"]


def outputs_view(service: WorkspaceService, action_id: str) -> dict:
    """The inspection view of one action's outputs (RFC 001 section 9)."""

    return semantic_outputs(service)[action_id]


def subscriber_in_view(service: WorkspaceService, projection_id: str) -> dict:
    for action in semantic_outputs(service).values():
        for output in action["outputs"].values():
            for subscriber in output["subscribers"]:
                if subscriber["projection"] == projection_id:
                    return subscriber
    raise AssertionError(f"projection {projection_id!r} does not subscribe")


def pointers_of(service: WorkspaceService, action_id: str) -> dict:
    """The current pointer of every output of one action."""

    return {
        name: output["pointer"]
        for name, output in outputs_view(service, action_id)["outputs"].items()
    }


def invoke(service: WorkspaceService, request_id: str, action_id: str):
    return service.dispatch(
        command(
            request_id,
            method="action.invoke",
            expected={
                f"actions/{action_id}": service.state.entity_revisions[
                    f"actions/{action_id}"
                ]
            },
            payload={"actionId": action_id},
        )
    )


def launch(service: WorkspaceService, run_id: str):
    outcome = service.launch_admitted_run(run_id)
    assert outcome.launched, outcome.refusal
    return outcome


def replace_action(
    service: WorkspaceService,
    request_id: str,
    action_id: str,
    value: dict,
    *,
    also: "list[dict] | None" = None,
    expected_extra: "dict | None" = None,
):
    expected = {
        f"actions/{action_id}": service.state.entity_revisions[
            f"actions/{action_id}"
        ]
    }
    expected.update(expected_extra or {})
    operations = [
        {
            "op": "entity.replace",
            "collection": "actions",
            "id": action_id,
            "value": value,
        }
    ]
    operations.extend(also or [])
    return service.apply(apply_command(request_id, expected, operations))


def create_action(
    service: WorkspaceService, request_id: str, action_id: str, value: dict
):
    return service.apply(
        apply_command(
            request_id,
            {f"actions/{action_id}": 0},
            [
                {
                    "op": "entity.create",
                    "collection": "actions",
                    "id": action_id,
                    "value": value,
                }
            ],
        )
    )


def subscribe(
    service: WorkspaceService,
    request_id: str,
    projection_id: str,
    *,
    action: str,
    output: str,
    incarnation: str,
    kind: str = "table",
):
    """Add one following subscriber to any action output."""

    config = (
        {"columns": None, "selection": "none"}
        if kind == "table"
        else {"fit": "contain"}
        if kind == "plot"
        else {"format": "plain"}
    )
    return service.apply(
        apply_command(
            request_id,
            {
                f"projections/{projection_id}": 0,
                "layout": service.state.entity_revisions["layout"],
            },
            [
                {
                    "op": "entity.create",
                    "collection": "projections",
                    "id": projection_id,
                    "value": {
                        "kind": kind,
                        "title": projection_id,
                        "source": {
                            "actionOutput": {
                                "action": action,
                                "output": output,
                                "incarnation": incarnation,
                            },
                            "follow": "current",
                        },
                        "config": config,
                        "controls": [],
                    },
                },
                {
                    "op": "layout.replace",
                    "value": {
                        "root": None,
                        "hidden": [
                            *service.state.document["layout"]["hidden"],
                            projection_id,
                        ],
                    },
                },
            ],
        )
    )


def move_and_reuse_group(
    service: WorkspaceService,
    request_id: str,
    *,
    new_group: str,
    freed_group: str,
):
    """Move the published action away and reuse its freed group name.

    One valid revision-checked transaction performs the whole swap, the
    shape the review reproduced: the published action moves to another
    group and a second action that never ran takes the freed name with
    matching output names and kinds.
    """

    return replace_action(
        service,
        request_id,
        "analyze-sales",
        grouped_action(registration_of(service), new_group),
        also=[
            {
                "op": "entity.create",
                "collection": "actions",
                "id": "other-action",
                "value": grouped_action(registration_of(service), freed_group),
            }
        ],
        expected_extra={"actions/other-action": 0},
    )


# ---------------------------------------------------------------------------
# Group reassignment (RFC 001 section 8.1: one writer per output identity)
# ---------------------------------------------------------------------------


def test_a_freed_group_name_lends_no_run_to_the_new_owner(service_adapter):
    service = seed_outputs_workspace(service_adapter)
    publication, run_id = run_and_publish(service)
    assert publication.outcome == "current"

    assert move_and_reuse_group(
        service,
        "06-swap",
        new_group="moved-results",
        freed_group="sales-results",
    ).ok

    # The new owner has never executed.
    assert all(run.actionId != "other-action" for run in service.runs.values())

    # Inspection reports no current pointer for the never-run owner and
    # keeps the previous owner's pointer under the group it moved to.
    other = outputs_view(service, "other-action")
    assert other["outputGroup"] == "sales-results"
    assert set(other["outputs"]) == {"filtered", "monthly"}
    assert all(
        pointer is None
        for pointer in pointers_of(service, "other-action").values()
    )
    moved = outputs_view(service, "analyze-sales")
    assert moved["outputGroup"] == "moved-results"
    assert moved["outputs"]["filtered"]["pointer"]["runId"] == run_id

    # A following subscriber on the new owner's output resolves empty:
    # it never displays the previous owner's result as current.
    incarnation = service.state.output_incarnations["other-action"]
    assert subscribe(
        service,
        "07-subscribe-other",
        "borrower",
        action="other-action",
        output="filtered",
        incarnation=incarnation,
    ).ok
    subscriber = subscriber_in_view(service, "borrower")
    assert subscriber["status"] == "empty"
    assert "source" not in subscriber
    assert "provenance" not in subscriber


def test_the_moved_owner_keeps_its_subscribers_without_a_new_run(
    service_adapter,
):
    service = seed_outputs_workspace(service_adapter)
    _, run_id = run_and_publish(service)
    incarnation = incarnation_of(service)
    assert add_subscribers(
        service,
        "06-subscribe",
        {"sales": subscriber_projection("filtered", incarnation)},
    ).ok

    assert move_and_reuse_group(
        service,
        "07-swap",
        new_group="moved-results",
        freed_group="sales-results",
    ).ok

    # No execution answered the group swap, and the unchanged owner's
    # subscriber still resolves the saved result.
    assert set(service.runs) == {run_id}
    subscriber = subscriber_of(service, "sales")
    assert subscriber["status"] == "current"
    assert subscriber["source"] == {"sourceId": FILTERED_SOURCE, "version": 1}
    assert subscriber["provenance"]["runId"] == run_id


def test_the_previous_owners_run_stays_historical_and_readable(
    service_adapter,
):
    service = seed_outputs_workspace(service_adapter)
    _, run_id = run_and_publish(service)

    assert move_and_reuse_group(
        service,
        "06-swap",
        new_group="moved-results",
        freed_group="sales-results",
    ).ok

    # The artifacts of the previous owner remain as history, with their
    # run provenance intact and no second execution.
    document = service.state.document
    for source_id in (FILTERED_SOURCE, MONTHLY_SOURCE):
        source = document["sources"][source_id]
        assert source["currentVersion"] == 1
        assert source["versions"]["1"]["origin"]["runId"] == run_id
    assert set(service.runs) == {run_id}
    assert service.runs[run_id].actionId == "analyze-sales"
    assert service.runs[run_id].outputs["filtered"].sourceId == FILTERED_SOURCE


def test_the_old_owners_group_reports_its_own_status(service_adapter):
    service = seed_outputs_workspace(service_adapter)
    _, run_id = run_and_publish(service)

    assert move_and_reuse_group(
        service,
        "06-swap",
        new_group="moved-results",
        freed_group="sales-results",
    ).ok

    # Each action reports the result status of its own group. The moved
    # owner keeps its published result and reports it stale, because the
    # publication contract it captured moved; the never-run owner
    # reports an empty group, not the previous owner's current one.
    statuses = service.output_group_status()
    moved_status = statuses["moved-results"]
    assert moved_status["action"] == "analyze-sales"
    assert moved_status["status"] == "stale"
    assert moved_status["resultIsStale"] is True
    assert moved_status["result"]["runId"] == run_id
    assert moved_status["staleness"]
    new_status = statuses["sales-results"]
    assert new_status["action"] == "other-action"
    assert new_status["status"] == "empty"
    assert new_status["resultIsStale"] is False
    assert "result" not in new_status


# ---------------------------------------------------------------------------
# Group swaps between two published actions
# ---------------------------------------------------------------------------


def test_a_group_swap_between_published_actions_keeps_pointers_apart(
    service_adapter,
):
    service = seed_outputs_workspace(service_adapter)
    _, first_run = run_and_publish(service, "05-invoke")

    assert create_action(
        service,
        "06-create-other",
        "other-action",
        grouped_action(registration_of(service), "other-results"),
    ).ok
    invoked = invoke(service, "07-invoke-other", "other-action")
    assert invoked.ok, invoked.errors
    second_run = invoked.result["run"]["runId"]
    assert launch(service, second_run).state == "succeeded"
    assert service.publish_run(second_run).outcome == "current"

    # Swap the groups of the two published actions in one transaction.
    swapped = service.apply(
        apply_command(
            "08-swap",
            {
                "actions/analyze-sales": service.state.entity_revisions[
                    "actions/analyze-sales"
                ],
                "actions/other-action": service.state.entity_revisions[
                    "actions/other-action"
                ],
            },
            [
                {
                    "op": "entity.replace",
                    "collection": "actions",
                    "id": "analyze-sales",
                    "value": grouped_action(
                        registration_of(service), "other-results"
                    ),
                },
                {
                    "op": "entity.replace",
                    "collection": "actions",
                    "id": "other-action",
                    "value": grouped_action(
                        registration_of(service), "sales-results"
                    ),
                },
            ],
        )
    )
    assert swapped.ok, swapped.errors

    view = outputs_view(service, "analyze-sales")
    assert view["outputGroup"] == "other-results"
    assert view["outputs"]["filtered"]["pointer"]["runId"] == first_run
    other = outputs_view(service, "other-action")
    assert other["outputGroup"] == "sales-results"
    assert other["outputs"]["filtered"]["pointer"]["runId"] == second_run


# ---------------------------------------------------------------------------
# Changed output contracts
# ---------------------------------------------------------------------------


def test_a_renamed_output_contract_has_no_current_pointer(service_adapter):
    service = seed_outputs_workspace(service_adapter)
    _, run_id = run_and_publish(service)
    incarnation = incarnation_of(service)

    # A changed output contract needs a changed registration: the same
    # captured script under a contract that declares other output
    # names advances the registration's immutable revision.
    renamed_contract = {
        "parameters": FULL_CONTRACT["parameters"],
        "inputs": FULL_CONTRACT["inputs"],
        "outputs": {"renamed-table": "table", "renamed-plot": "image"},
    }
    script_token = service.capture_artifact_token(
        SPEC_SALES_SCRIPT.encode("utf-8"), "text/x-python"
    ).token
    registered = service.dispatch(
        command(
            "06-register-renamed",
            method="action.register",
            payload={
                "scriptToken": script_token,
                "environmentId": local_environment().environment_id,
                "contract": renamed_contract,
            },
        )
    )
    assert registered.ok, registered.errors
    registration_id = registered.result["registrationId"]

    renamed = grouped_action(registration_id, "sales-results")
    renamed["outputs"] = dict(renamed_contract["outputs"])
    assert replace_action(service, "07-rename", "analyze-sales", renamed).ok

    # The group keeps its name and its action, but no output name of
    # the published run matches the new contract.
    view = outputs_view(service, "analyze-sales")
    assert set(view["outputs"]) == {"renamed-table", "renamed-plot"}
    assert all(
        pointer is None
        for pointer in pointers_of(service, "analyze-sales").values()
    )

    # A fresh subscriber to a renamed output resolves nothing until a
    # new run publishes under the new contract.
    assert subscribe(
        service,
        "08-subscribe-renamed",
        "fresh",
        action="analyze-sales",
        output="renamed-table",
        incarnation=incarnation,
    ).ok
    subscriber = subscriber_in_view(service, "fresh")
    assert subscriber["status"] == "empty"
    assert "source" not in subscriber


# ---------------------------------------------------------------------------
# Deletion, recreation, and restart
# ---------------------------------------------------------------------------


def test_a_recreated_action_under_the_same_group_has_no_pointer(
    service_adapter,
):
    service = seed_outputs_workspace(service_adapter)
    _, run_id = run_and_publish(service)
    old_incarnation = incarnation_of(service)

    recreated = service.apply(
        apply_command(
            "06-recreate",
            {"actions/analyze-sales": service.state.entity_revisions[
                "actions/analyze-sales"
            ]},
            [
                {
                    "op": "entity.delete",
                    "collection": "actions",
                    "id": "analyze-sales",
                },
                {
                    "op": "entity.create",
                    "collection": "actions",
                    "id": "analyze-sales",
                    "value": semantic_action(registration_of(service)),
                },
            ],
        )
    )
    assert recreated.ok, recreated.errors
    assert incarnation_of(service) != old_incarnation

    # The recreated action is another incarnation under the same group
    # name: it inherits neither the run nor its pointers.
    assert all(
        pointer is None
        for pointer in pointers_of(service, "analyze-sales").values()
    )
    assert service.runs[run_id].disposition == "superseded"

    # The historical artifacts survive the recreation.
    assert service.state.document["sources"][FILTERED_SOURCE][
        "currentVersion"
    ] == 1


def test_a_moved_then_recreated_action_still_retires_its_run(
    service_adapter,
):
    """The retirement of a recreation keys on the owner, not the group.

    A run keeps the group name it was admitted under. An action that
    moved to another group and was then deleted and recreated must
    still lose its current pointer: a group-keyed retirement would miss
    the run entirely and the recreated incarnation would inherit it.
    """

    service = seed_outputs_workspace(service_adapter)
    _, run_id = run_and_publish(service)
    old_incarnation = incarnation_of(service)

    moved = replace_action(
        service,
        "06-move",
        "analyze-sales",
        grouped_action(registration_of(service), "moved-results"),
    )
    assert moved.ok, moved.errors
    assert pointers_of(service, "analyze-sales")["filtered"]["runId"] == run_id

    recreated = service.apply(
        apply_command(
            "07-recreate",
            {"actions/analyze-sales": service.state.entity_revisions[
                "actions/analyze-sales"
            ]},
            [
                {
                    "op": "entity.delete",
                    "collection": "actions",
                    "id": "analyze-sales",
                },
                {
                    "op": "entity.create",
                    "collection": "actions",
                    "id": "analyze-sales",
                    "value": grouped_action(
                        registration_of(service), "moved-results"
                    ),
                },
            ],
        )
    )
    assert recreated.ok, recreated.errors
    assert incarnation_of(service) != old_incarnation

    assert all(
        pointer is None
        for pointer in pointers_of(service, "analyze-sales").values()
    )
    assert service.runs[run_id].disposition == "superseded"
    assert service.runs[run_id].outputGroup == "sales-results"


def test_owner_bound_resolution_survives_a_restart(service_adapter):
    service = seed_outputs_workspace(service_adapter)
    _, run_id = run_and_publish(service)

    assert move_and_reuse_group(
        service,
        "06-swap",
        new_group="moved-results",
        freed_group="sales-results",
    ).ok
    incarnation = service.state.output_incarnations["other-action"]
    assert subscribe(
        service,
        "07-subscribe-other",
        "borrower",
        action="other-action",
        output="filtered",
        incarnation=incarnation,
    ).ok

    service.close()
    reopened = WorkspaceService.open(service_adapter, WORKSPACE)
    try:
        # Inspection and subscriber resolution agree before and after
        # the restart, with no execution in between.
        assert set(reopened.runs) == {run_id}
        assert pointers_of(reopened, "analyze-sales")["filtered"][
            "runId"
        ] == run_id
        assert all(
            pointer is None
            for pointer in pointers_of(reopened, "other-action").values()
        )
        subscriber = subscriber_in_view(reopened, "borrower")
        assert subscriber["status"] == "empty"
        assert "source" not in subscriber
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# The captured identity
# ---------------------------------------------------------------------------


def test_admission_captures_the_output_identity(service_adapter):
    """The snapshot carries the stable identity of the owning action."""

    service = seed_outputs_workspace(service_adapter)
    incarnation = incarnation_of(service)
    response = invoke(service, "05-invoke", "analyze-sales")
    assert response.ok, response.errors
    run = service.runs[response.result["run"]["runId"]]
    assert run.outputIncarnation == incarnation
    assert service.pending_intents["sales-results"].outputIncarnation == (
        incarnation
    )


def test_a_record_without_output_identity_resolves_only_while_unrecreated(
    service_adapter,
):
    """Safe handling of older run records (SPEC.md section 14).

    A run saved before output identity capture holds no incarnation. It
    resolves through its owning action alone, exactly as it did before
    the field existed, so an upgraded workspace keeps its saved result
    readable. A recreation still retires it, because the retirement of
    a deleted action keys on the owner.
    """

    service = seed_outputs_workspace(service_adapter)
    _, run_id = run_and_publish(service)
    service.runs[run_id] = service.runs[run_id].model_copy(
        update={"outputIncarnation": None}
    )

    # The older record still resolves its owner's current pointer.
    assert pointers_of(service, "analyze-sales")["filtered"]["runId"] == run_id

    recreated = service.apply(
        apply_command(
            "06-recreate",
            {"actions/analyze-sales": service.state.entity_revisions[
                "actions/analyze-sales"
            ]},
            [
                {
                    "op": "entity.delete",
                    "collection": "actions",
                    "id": "analyze-sales",
                },
                {
                    "op": "entity.create",
                    "collection": "actions",
                    "id": "analyze-sales",
                    "value": semantic_action(registration_of(service)),
                },
            ],
        )
    )
    assert recreated.ok, recreated.errors
    assert all(
        pointer is None
        for pointer in pointers_of(service, "analyze-sales").values()
    )
