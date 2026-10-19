"""Outputs independent of panes through the real service (T027,
RFC 001 sections 8.1, 8.2, and 10.4).

The tests drive the extension publication loop end to end against an
isolated workspace with real durable storage:

- An action with no projection targets — no subscribers at all — still
  runs and publishes: eligibility depends on the action and its output
  contract, never on pane existence.
- Two following projections added after publication resolve the same
  saved output without a second execution. Subscribe-after-publication
  resolves the existing result; it never triggers a run.
- Removing a subscriber during another run leaves the publication
  valid: the remaining subscriber advances, and the removed pane is
  gone.
- A pinned subscriber freezes one version of the output's stable
  logical source and reports its version status while the following
  subscribers advance.
- An obsolete generation archives superseded and advances nothing.
- A missing output rejects the entire publication, so both subscribers
  keep the previous versions together.
- Deleting the action requires removing its subscribers in the same
  transaction, and a recreated action issues another incarnation the
  old subscribers cannot satisfy.

The seeding harness reuses the T016 scheduling helpers, so every step
rides the exact service handlers of SPEC.md section 12.6. Everything
runs under the Looper resource prefix, never against the user's data
directory.
"""

from __future__ import annotations

from runtime.storage import PlatformAdapter
from runtime.workspace import WorkspaceService, local_environment

from test_scheduling import (
    FULL_CONTRACT,
    SPEC_SALES_SCRIPT,
    FakeClock,
    requires_storage_worker,
)

pytestmark = requires_storage_worker

WORKSPACE = "outputs-demo"


def envelope(
    request_id: str,
    *,
    method: str,
    payload: "dict | None" = None,
    expected: "dict | None" = None,
    operations: "list | None" = None,
):
    from melt.schema.commands import parse_command

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


def invoke(service: WorkspaceService, request_id: str = "05-invoke"):
    return service.dispatch(
        envelope(
            request_id,
            method="action.invoke",
            expected={"actions/analyze-sales": 1},
            payload={"actionId": "analyze-sales"},
        )
    )


def launch(service: WorkspaceService, run_id: str):
    outcome = service.launch_admitted_run(run_id)
    assert outcome.launched, outcome.refusal
    return outcome

FILTERED_SOURCE = "analyze-sales-filtered"
MONTHLY_SOURCE = "analyze-sales-monthly"

PARQUET = "application/vnd.apache.parquet"
PYTHON = "text/x-python"

MINIMUM_PARAMETER = {
    "type": "number",
    "default": 0,
    "nullable": False,
    "constraints": {"minimum": 0, "maximum": 1000000000},
}

# The specification script with a guarded fault: the run stages both
# outputs cleanly, then removes the plot only when the captured
# minimum is high. The publication of that run must reject entirely
# (RFC 001 section 8.2 keeps the v0.1 atomicity rule).
MISSING_PLOT_SCRIPT = SPEC_SALES_SCRIPT + (
    "from pathlib import Path\n"
    "if ui.params['minimum'] >= 300:\n"
    "    Path('outputs/monthly.png').unlink()\n"
)


def codes(response):
    return [(error.code, error.path) for error in response.errors]


def semantic_action(registration_id: str) -> dict:
    """The extension action of RFC 001 section 8.1: no targets."""

    return {
        "implementation": {"registrationId": registration_id},
        "parameters": {"minimum": {"parameter": "minimum"}},
        "inputs": {"sales": {"sourceId": "sales-input", "version": 1}},
        "outputs": {"filtered": "table", "monthly": "image"},
        "outputGroup": "sales-results",
    }


def subscriber_projection(output: str, incarnation: str, **overrides) -> dict:
    projection = {
        "kind": "table",
        "title": f"Subscriber of {output}",
        "source": {
            "actionOutput": {
                "action": "analyze-sales",
                "output": output,
                "incarnation": incarnation,
            },
            "follow": "current",
        },
        "config": {"columns": None, "selection": "none"},
        "controls": [],
    }
    projection.update(overrides)
    return projection


def seed_outputs_workspace(
    adapter: PlatformAdapter,
    *,
    script: str = SPEC_SALES_SCRIPT,
    projections: "dict[str, dict] | None" = None,
) -> WorkspaceService:
    """Build one extension workspace around a pane-free action."""

    from test_scheduling import BUILDER

    service = WorkspaceService.open(adapter, WORKSPACE, clock=FakeClock())
    created = service.create_workspace(
        {
            "language": "melt/semantic-draft-1",
            "workspaceId": WORKSPACE,
            "title": "Sales analysis",
            "sources": {},
            "parameters": {"minimum": MINIMUM_PARAMETER},
            "actions": {},
            "projections": {},
            "bindings": {},
            "collections": {},
            "state": {},
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

    script_token = service.capture_artifact_token(
        script.encode("utf-8"), PYTHON
    ).token
    registered = service.dispatch(
        envelope(
            "03-register",
            method="action.register",
            payload={
                "scriptToken": script_token,
                "environmentId": local_environment().environment_id,
                "contract": FULL_CONTRACT,
            },
        )
    )
    assert registered.ok, registered.errors
    registration_id = registered.result["registrationId"]

    projections = projections or {}
    operations = [
        {
            "op": "entity.create",
            "collection": "actions",
            "id": "analyze-sales",
            "value": semantic_action(registration_id),
        }
    ]
    operations.extend(
        {
            "op": "entity.create",
            "collection": "projections",
            "id": projection_id,
            "value": definition,
        }
        for projection_id, definition in sorted(projections.items())
    )
    operations.append(
        {
            "op": "layout.replace",
            "value": {"root": None, "hidden": sorted(projections)},
        }
    )
    definitions = service.apply(
        apply_envelope(
            "04-definitions",
            {
                "actions/analyze-sales": 0,
                **{f"projections/{name}": 0 for name in projections},
                "layout": 1,
            },
            operations,
        )
    )
    assert definitions.ok, definitions.errors

    service.grant_execution_permission(
        kind="projectExecution",
        registration_id=registration_id,
        granted_by="host-permission-control/run-approval",
    )
    return service


def run_and_publish(service: WorkspaceService, request_id: str = "05-invoke"):
    """Invoke, launch, and publish one run; return its pieces."""

    invoked = invoke(service, request_id)
    assert invoked.ok, invoked.errors
    run_id = invoked.result["run"]["runId"]
    outcome = launch(service, run_id)
    assert outcome.state == "succeeded", outcome.refusal
    return service.publish_run(run_id), run_id


def semantic_view(service: WorkspaceService) -> dict:
    response = service.dispatch(envelope("inspect", method="workspace.inspect"))
    assert response.ok, response.errors
    return response.result["semantic"]


def outputs_view(service: WorkspaceService) -> dict:
    return semantic_view(service)["outputs"]["analyze-sales"]


def subscriber_of(service: WorkspaceService, projection_id: str) -> dict:
    for output in outputs_view(service)["outputs"].values():
        for subscriber in output["subscribers"]:
            if subscriber["projection"] == projection_id:
                return subscriber
    raise AssertionError(f"projection {projection_id!r} does not subscribe")


def add_subscribers(
    service: WorkspaceService,
    request_id: str,
    projections: "dict[str, dict]",
    *,
    hidden: "list[str] | None" = None,
):
    """Create subscriber projections and place them in the layout."""

    hidden = sorted(projections) if hidden is None else sorted(hidden)
    document = service.state.document
    placed = [
        projection_id
        for projection_id in document["projections"]
        if projection_id not in ("__removed__",)
    ]
    operations = [
        {
            "op": "entity.create",
            "collection": "projections",
            "id": projection_id,
            "value": definition,
        }
        for projection_id, definition in sorted(projections.items())
    ]
    operations.append(
        {
            "op": "layout.replace",
            "value": {
                "root": None,
                "hidden": sorted([*placed, *projections]),
            },
        }
    )
    return service.apply(
        apply_envelope(
            request_id,
            {
                **{f"projections/{name}": 0 for name in projections},
                "layout": service.state.entity_revisions["layout"],
            },
            operations,
        )
    )


def incarnation_of(service: WorkspaceService) -> str:
    incarnation = outputs_view(service)["incarnation"]
    assert incarnation, "the action holds no issued output incarnation"
    return incarnation


# ---------------------------------------------------------------------------
# Publication without panes (RFC 001 sections 8.1 and 10.4)
# ---------------------------------------------------------------------------


def test_an_action_publishes_with_no_projections(service_adapter):
    service = seed_outputs_workspace(service_adapter)
    publication, run_id = run_and_publish(service)

    # The publication became current with no pane involved.
    assert publication.outcome == "current"
    assert publication.outputs == {
        "filtered": {"sourceId": FILTERED_SOURCE, "version": 1},
        "monthly": {"sourceId": MONTHLY_SOURCE, "version": 1},
    }
    document = service.state.document
    assert document["projections"] == {}
    for source_id in (FILTERED_SOURCE, MONTHLY_SOURCE):
        source = document["sources"][source_id]
        assert source["currentVersion"] == 1
        origin = source["versions"]["1"]["origin"]
        assert origin == {"kind": "run", "runId": run_id, "output": {
            "filtered": "filtered", "monthly": "monthly"
        }.get(origin["output"])}
    status = service.output_group_status()["sales-results"]
    assert status["status"] == "current"
    assert status["result"]["outputs"]["filtered"] == {
        "sourceId": FILTERED_SOURCE,
        "version": 1,
    }

    # The inspection view carries the stable identity and the pointer.
    view = outputs_view(service)
    assert view["incarnation"] == incarnation_of(service)
    assert view["outputGroup"] == "sales-results"
    assert view["outputs"]["filtered"]["pointer"] == {
        "sourceId": FILTERED_SOURCE,
        "version": 1,
        "runId": run_id,
        "generation": 1,
    }
    assert view["outputs"]["filtered"]["subscribers"] == []


def test_two_following_projections_resolve_the_saved_output(
    service_adapter,
):
    service = seed_outputs_workspace(service_adapter)
    publication, run_id = run_and_publish(service)
    incarnation = incarnation_of(service)

    answer = add_subscribers(
        service,
        "06-subscribe",
        {
            "sales": subscriber_projection("filtered", incarnation),
            "revenue": {
                "kind": "plot",
                "title": "Revenue by month",
                "source": {
                    "actionOutput": {
                        "action": "analyze-sales",
                        "output": "monthly",
                        "incarnation": incarnation,
                    },
                    "follow": "current",
                },
                "config": {"fit": "contain"},
                "controls": [],
            },
        },
    )
    assert answer.ok, answer.errors

    # Subscribe-after-publication resolves the existing output and
    # never triggers a run (RFC 001 section 8.2).
    runs_after = set(service.runs)
    assert runs_after == {run_id}
    for projection_id, source_id in (
        ("sales", FILTERED_SOURCE),
        ("revenue", MONTHLY_SOURCE),
    ):
        subscriber = subscriber_of(service, projection_id)
        assert subscriber["follow"] == "current"
        assert subscriber["status"] == "current"
        assert subscriber["source"] == {"sourceId": source_id, "version": 1}
        assert subscriber["provenance"]["runId"] == run_id

    # The compiled plan lowers the subscriptions and the index.
    from runtime.compiler import CompileRequest, compile_document

    result = compile_document(
        CompileRequest(
            document=service.state.document,
            source_catalog=service.source_catalog,
            action_registry=service.action_registry,
            output_incarnations=service.state.output_incarnations,
        )
    )
    assert result.ok, result.diagnostics
    dependency = result.plan.dependency.outputs["analyze-sales/filtered"]
    assert [item.projection for item in dependency.subscribers] == ["sales"]


def test_removing_one_subscriber_during_another_run_keeps_publication(
    service_adapter,
):
    service = seed_outputs_workspace(service_adapter)
    run_and_publish(service)
    incarnation = incarnation_of(service)
    assert add_subscribers(
        service,
        "06-subscribe",
        {
            "sales": subscriber_projection("filtered", incarnation),
            "mirror": subscriber_projection("filtered", incarnation),
        },
    ).ok

    # A second run executes and stages its outputs; the pane removal
    # lands in the window between completion and publication, exactly
    # as the hero workflow moves a pane (SPEC.md section 18.2).
    invoked = invoke(service, "07-invoke")
    assert invoked.ok, invoked.errors
    second_run = invoked.result["run"]["runId"]
    outcome = launch(service, second_run)
    assert outcome.state == "succeeded"

    removed = service.apply(
        apply_envelope(
            "08-remove",
            {
                "projections/mirror": service.state.entity_revisions[
                    "projections/mirror"
                ],
                "layout": service.state.entity_revisions["layout"],
            },
            [
                {
                    "op": "entity.delete",
                    "collection": "projections",
                    "id": "mirror",
                },
                {
                    "op": "layout.replace",
                    "value": {"root": None, "hidden": ["sales"]},
                },
            ],
        )
    )
    assert removed.ok, removed.errors

    publication = service.publish_run(second_run)
    assert publication.outcome == "current"
    assert publication.outputs["filtered"]["version"] == 2
    subscriber = subscriber_of(service, "sales")
    assert subscriber["source"] == {"sourceId": FILTERED_SOURCE, "version": 2}
    assert "mirror" not in service.state.document["projections"]


def test_a_hidden_subscriber_still_advances(service_adapter):
    service = seed_outputs_workspace(service_adapter)
    run_and_publish(service)
    incarnation = incarnation_of(service)
    assert add_subscribers(
        service,
        "06-subscribe",
        {
            "sales": subscriber_projection("filtered", incarnation),
            "attic": subscriber_projection("filtered", incarnation),
        },
        hidden=["attic", "sales"],
    ).ok
    publication, _ = run_and_publish(service, "07-invoke")
    assert publication.outcome == "current"
    for projection_id in ("sales", "attic"):
        assert subscriber_of(service, projection_id)["source"]["version"] == 2


def test_a_pinned_subscriber_does_not_advance(service_adapter):
    service = seed_outputs_workspace(service_adapter)
    run_and_publish(service)
    incarnation = incarnation_of(service)
    assert add_subscribers(
        service,
        "06-subscribe",
        {
            "sales": subscriber_projection("filtered", incarnation),
            "frozen": {
                **subscriber_projection("filtered", incarnation),
                "source": {
                    "actionOutput": {
                        "action": "analyze-sales",
                        "output": "filtered",
                        "incarnation": incarnation,
                    },
                    "follow": "pinned",
                    "version": 1,
                },
            },
        },
    ).ok

    publication, _ = run_and_publish(service, "07-invoke")
    assert publication.outcome == "current"
    assert publication.outputs["filtered"]["version"] == 2

    following = subscriber_of(service, "sales")
    assert following["status"] == "current"
    assert following["source"]["version"] == 2
    pinned = subscriber_of(service, "frozen")
    assert pinned["status"] == "pinned"
    assert pinned["source"] == {"sourceId": FILTERED_SOURCE, "version": 1}
    assert pinned["stale"] is True


def test_an_obsolete_generation_does_not_advance_the_pointers(
    service_adapter,
):
    service = seed_outputs_workspace(service_adapter)
    run_and_publish(service)

    # Two invocations reserve two generations; the older run launches
    # and succeeds, but only the newest generation may publish current.
    first = invoke(service, "07-invoke")
    assert first.ok, first.errors
    older_run = first.result["run"]["runId"]
    second = invoke(service, "08-invoke")
    assert second.ok, second.errors
    newer_run = second.result["run"]["runId"]

    outcome = launch(service, older_run)
    assert outcome.state == "succeeded"
    superseded = service.publish_run(older_run)
    assert superseded.outcome == "superseded"

    pointer = outputs_view(service)["outputs"]["filtered"]["pointer"]
    assert pointer is None or pointer["version"] == 1
    status = service.output_group_status()["sales-results"]
    assert status["status"] in ("pending", "current")

    outcome = launch(service, newer_run)
    assert outcome.state == "succeeded"
    current = service.publish_run(newer_run)
    assert current.outcome == "current"
    # The superseded run archived its outputs as historical versions,
    # so the current publication allocates the next version after it.
    assert current.outputs["filtered"]["version"] == 3
    pointer = outputs_view(service)["outputs"]["filtered"]["pointer"]
    assert pointer["version"] == 3
    assert pointer["generation"] == 3


def test_a_missing_output_rejects_the_publication_and_keeps_subscribers(
    service_adapter,
):
    service = seed_outputs_workspace(service_adapter, script=MISSING_PLOT_SCRIPT)
    run_and_publish(service)
    incarnation = incarnation_of(service)
    assert add_subscribers(
        service,
        "06-subscribe",
        {
            "sales": subscriber_projection("filtered", incarnation),
            "revenue": {
                "kind": "plot",
                "title": "Revenue by month",
                "source": {
                    "actionOutput": {
                        "action": "analyze-sales",
                        "output": "monthly",
                        "incarnation": incarnation,
                    },
                    "follow": "current",
                },
                "config": {"fit": "contain"},
                "controls": [],
            },
        },
    ).ok

    # A captured minimum of 300 removes the staged plot: the entire
    # publication rejects and both subscribers keep version 1 together.
    changed = service.apply(
        apply_envelope(
            "07-set",
            {"state/minimum": 1},
            [{"op": "parameter.set", "id": "minimum", "value": 300}],
        )
    )
    assert changed.ok, changed.errors
    invoked = invoke(service, "08-invoke")
    assert invoked.ok, invoked.errors
    run_id = invoked.result["run"]["runId"]
    assert launch(service, run_id).state == "succeeded"
    publication = service.publish_run(run_id)
    assert publication.outcome == "rejected"
    assert [error.code for error in publication.refusal] == ["E_ARTIFACT"]

    for projection_id, source_id in (
        ("sales", FILTERED_SOURCE),
        ("revenue", MONTHLY_SOURCE),
    ):
        subscriber = subscriber_of(service, projection_id)
        assert subscriber["source"]["version"] == 1
    pointer = outputs_view(service)["outputs"]["monthly"]["pointer"]
    assert pointer["version"] == 1


def test_incompatible_projection_settings_do_not_invalidate_publication(
    service_adapter,
):
    service = seed_outputs_workspace(service_adapter)
    run_and_publish(service)
    incarnation = incarnation_of(service)
    # A pane's preferred columns are no part of an action's output
    # validity (RFC 001 section 8.2): the projection configures a
    # column the published schema does not carry, and publication
    # stays valid all the same.
    assert add_subscribers(
        service,
        "06-subscribe",
        {
            "odd": {
                **subscriber_projection("filtered", incarnation),
                "config": {
                    "columns": ["month", "not_a_column"],
                    "selection": "none",
                },
            }
        },
    ).ok
    publication, _ = run_and_publish(service, "07-invoke")
    assert publication.outcome == "current"
    subscriber = subscriber_of(service, "odd")
    assert subscriber["status"] == "current"
    assert subscriber["source"]["version"] == 2


def test_deleting_the_action_takes_its_subscribers_and_invalidates(
    service_adapter,
):
    service = seed_outputs_workspace(service_adapter)
    run_and_publish(service)
    incarnation = incarnation_of(service)
    assert add_subscribers(
        service,
        "06-subscribe",
        {"sales": subscriber_projection("filtered", incarnation)},
    ).ok

    # The lone delete dangles the subscription: the compiler rejects it.
    lone = service.apply(
        apply_envelope(
            "07-lone-delete",
            {"actions/analyze-sales": 1},
            [
                {
                    "op": "entity.delete",
                    "collection": "actions",
                    "id": "analyze-sales",
                }
            ],
        )
    )
    assert not lone.ok
    assert ("E_REFERENCE", "/projections/sales/source/actionOutput/action") in (
        codes(lone)
    )

    combined = service.apply(
        apply_envelope(
            "08-delete",
            {
                "actions/analyze-sales": 1,
                "projections/sales": service.state.entity_revisions[
                    "projections/sales"
                ],
                "layout": service.state.entity_revisions["layout"],
            },
            [
                {
                    "op": "entity.delete",
                    "collection": "actions",
                    "id": "analyze-sales",
                },
                {
                    "op": "entity.delete",
                    "collection": "projections",
                    "id": "sales",
                },
                {
                    "op": "layout.replace",
                    "value": {"root": None, "hidden": []},
                },
            ],
        )
    )
    assert combined.ok, combined.errors
    document = service.state.document
    assert document["actions"] == {}
    assert document["projections"] == {}
    # The saved sources stay readable after the action went away.
    assert document["sources"][FILTERED_SOURCE]["currentVersion"] == 1
    assert "analyze-sales" not in service.state.output_incarnations
    response = service.dispatch(
        envelope("09-inspect", method="workspace.inspect")
    )
    assert response.ok, response.errors


def test_a_recreated_action_issues_an_incarnation_old_subscribers_fail(
    service_adapter,
):
    service = seed_outputs_workspace(service_adapter)
    run_and_publish(service)
    old_incarnation = incarnation_of(service)
    assert add_subscribers(
        service,
        "06-subscribe",
        {"sales": subscriber_projection("filtered", old_incarnation)},
    ).ok

    replaced = service.apply(
        apply_envelope(
            "07-recreate",
            {
                "actions/analyze-sales": 1,
                "projections/sales": service.state.entity_revisions[
                    "projections/sales"
                ],
                "layout": service.state.entity_revisions["layout"],
            },
            [
                {
                    "op": "entity.delete",
                    "collection": "actions",
                    "id": "analyze-sales",
                },
                {
                    "op": "entity.delete",
                    "collection": "projections",
                    "id": "sales",
                },
                {
                    "op": "entity.create",
                    "collection": "actions",
                    "id": "analyze-sales",
                    "value": semantic_action(
                        next(iter(service.registrations))
                    ),
                },
                {
                    "op": "layout.replace",
                    "value": {"root": None, "hidden": []},
                },
            ],
        )
    )
    assert replaced.ok, replaced.errors
    new_incarnation = incarnation_of(service)
    assert new_incarnation != old_incarnation

    # The old incarnation cannot attach to the new calculation.
    stale = add_subscribers(
        service,
        "08-stale-subscription",
        {"sales": subscriber_projection("filtered", old_incarnation)},
    )
    assert not stale.ok
    assert (
        "E_CONFLICT",
        "/projections/sales/source/actionOutput/incarnation",
    ) in codes(stale)

    fresh = add_subscribers(
        service,
        "09-fresh-subscription",
        {"sales": subscriber_projection("filtered", new_incarnation)},
    )
    assert fresh.ok, fresh.errors
    # No publication happened since the recreate: the fresh subscriber
    # follows an output with no current result yet.
    subscriber = subscriber_of(service, "sales")
    assert subscriber["status"] == "empty"


def test_a_pinned_subscription_needs_a_published_version(service_adapter):
    service = seed_outputs_workspace(service_adapter)
    incarnation = incarnation_of(service)
    pinned = add_subscribers(
        service,
        "06-pin-nothing",
        {
            "frozen": {
                **subscriber_projection("filtered", incarnation),
                "source": {
                    "actionOutput": {
                        "action": "analyze-sales",
                        "output": "filtered",
                        "incarnation": incarnation,
                    },
                    "follow": "pinned",
                    "version": 1,
                },
            }
        },
    )
    assert not pinned.ok
    assert ("E_REFERENCE", "/projections/frozen/source/version") in codes(pinned)

    run_and_publish(service)
    works = add_subscribers(
        service,
        "07-pin-published",
        {
            "frozen": {
                **subscriber_projection("filtered", incarnation),
                "source": {
                    "actionOutput": {
                        "action": "analyze-sales",
                        "output": "filtered",
                        "incarnation": incarnation,
                    },
                    "follow": "pinned",
                    "version": 1,
                },
            }
        },
    )
    assert works.ok, works.errors
    assert subscriber_of(service, "frozen")["status"] == "pinned"


def test_the_extension_state_survives_publication_and_restart(
    service_adapter,
):
    service = seed_outputs_workspace(service_adapter)
    run_and_publish(service)
    incarnation = incarnation_of(service)
    assert add_subscribers(
        service,
        "06-subscribe",
        {"sales": subscriber_projection("filtered", incarnation)},
    ).ok
    sequence = service.commit_sequence

    service.close()
    reopened = WorkspaceService.open(service_adapter, WORKSPACE)
    try:
        assert reopened.state.language == "melt/semantic-draft-1"
        assert reopened.state.output_incarnations == {
            "analyze-sales": incarnation
        }
        assert reopened.commit_sequence == sequence
        subscriber = subscriber_of(reopened, "sales")
        assert subscriber["source"] == {
            "sourceId": FILTERED_SOURCE,
            "version": 1,
        }
    finally:
        reopened.close()
