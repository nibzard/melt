"""Parameter identity through admission, launch, and publication (T044).

Deleting and recreating a parameter starts a new identity incarnation
under the same identifier, and the recreated state begins again at
revision 1 (SPEC.md section 12.3). A run that captured the previous
incarnation therefore cannot satisfy its eligibility by name, value,
and state revision alone: the older invocation must stay unable to
become current, in `melt/v0.1` and `melt/semantic-draft-1` alike,
across a restart, and whatever it produced stays historical while the
previous visible results survive.

The regressions drive the public service handlers of SPEC.md section
12.6 against real durable storage: one manual or bound invocation at
`minimum=0`, one revision-checked transaction that deletes and
recreates the parameter, then the launch, dispatch, and publication
decisions that must refuse the captured identity. Ordinary unchanged
runs still publish, a fresh invocation after the recreation publishes
against the new identity, and an older saved record that predates
identity capture publishes only while its parameter was never
recreated. Nothing reruns on its own.

Everything runs under the Looper resource prefix, never against the
user's data directory.
"""

from __future__ import annotations

import pytest

from melt.schema.commands import parse_command
from melt.schema.diagnostics import DiagnosticCode
from melt.schema.records import CapturedParameter
from melt.schema.semantic import SEMANTIC_LANGUAGE_ID
from runtime.storage import PlatformAdapter
from runtime.workspace import WorkspaceService

from test_scheduling import (
    MINIMUM_PARAMETER,
    FakeClock,
    apply_envelope,
    envelope,
    invoke,
    launch,
    requires_storage_worker,
    seed_workspace,
    set_parameter,
)

pytestmark = requires_storage_worker

WORKSPACE = "sales-demo"


def recreate(
    service: WorkspaceService,
    request_id: str,
    *,
    default,
    parameter_revision: int = 1,
    state_revision: "int | None" = None,
    set_value=...,
    parameter: str = "minimum",
):
    """Delete and recreate one parameter in a single transaction.

    The transaction is the one the review reproduced the defect with:
    one ``workspace.apply`` whose final candidate holds the parameter
    again under a new default, so the action definition and every
    reference stay valid without a single intermediate state compiling
    (SPEC.md sections 12.2 and 12.3). ``set_value`` optionally supplies
    a ``parameter.set`` of the same transaction, the coalesced-binding
    path that captures its values inside the transaction itself.
    """

    definition = dict(MINIMUM_PARAMETER)
    definition["default"] = default
    expected = {f"parameters/{parameter}": parameter_revision}
    operations = [
        {
            "op": "entity.delete",
            "collection": "parameters",
            "id": parameter,
        },
        {
            "op": "entity.create",
            "collection": "parameters",
            "id": parameter,
            "value": definition,
        },
    ]
    if set_value is not ...:
        expected[f"state/{parameter}"] = (
            state_revision if state_revision is not None else 1
        )
        operations.append(
            {"op": "parameter.set", "id": parameter, "value": set_value}
        )
    return service.dispatch(
        apply_envelope(request_id, expected, operations)
    )


def migrate(service: WorkspaceService, request_id: str):
    """Migrate the workspace to the extension language (RFC 001 8.3)."""

    return service.dispatch(
        parse_command(
            {
                "protocolVersion": 1,
                "workspaceId": WORKSPACE,
                "requestId": request_id,
                "method": "workspace.migrate",
                "payload": {"targetLanguage": SEMANTIC_LANGUAGE_ID},
            }
        )
    )


def status_of(service: WorkspaceService, group: str = "sales-results") -> dict:
    """Read one output group's result status through inspection."""

    inspected = service.dispatch(
        envelope("status-inspect", method="workspace.inspect")
    )
    assert inspected.ok, inspected.errors
    return inspected.result["outputGroups"][group]


def invoke_now(service: WorkspaceService, request_id: str):
    """Invoke the action against its currently observed revision."""

    return invoke_as(service, request_id, service.state.entity_revisions[
        "actions/analyze-sales"
    ])


def invoke_as(service: WorkspaceService, request_id: str, revision: int):
    return service.dispatch(
        envelope(
            request_id,
            method="action.invoke",
            expected={"actions/analyze-sales": revision},
            payload={"actionId": "analyze-sales"},
        )
    )


def admitted_run(service: WorkspaceService, request_id: str, revision: int = 1):
    """Invoke manually and answer the minted queued run id."""

    response = invoke_as(service, request_id, revision)
    assert response.ok, response.errors
    return response.result["run"]["runId"]


def forge_legacy_capture(service: WorkspaceService, run_id: str) -> None:
    """Strip the captured identity data of one run record in place.

    The shape a database written before identity capture carries: the
    captured parameters hold no incarnation and the run holds no
    parameter-id map. The service must read those records and decide
    their eligibility without rerunning anything.
    """

    run = service.runs[run_id]
    service.runs[run_id] = run.model_copy(
        update={
            "parameters": {
                name: captured.model_copy(update={"incarnation": None})
                for name, captured in run.parameters.items()
            },
            "parameterIds": {},
        }
    )


# ---------------------------------------------------------------------------
# The regression: a recreated parameter rejects the older run
# ---------------------------------------------------------------------------


def test_a_recreated_parameter_rejects_the_older_runs_publication(
    service_adapter: PlatformAdapter,
):
    """The defect of the review: same name, same revision, new identity.

    Admitting and executing `minimum=0` at state revision 1, then
    deleting and recreating the parameter with default 300 in one
    transaction, leaves the new state at revision 1 again. Publication
    must refuse the older run its current disposition anyway: the
    captured identity names the previous incarnation, which the
    transaction retired (SPEC.md sections 12.3 and 13.3).
    """

    seeded = seed_workspace(service_adapter)
    service = seeded.service
    run_id = admitted_run(service, "05-invoke")
    assert launch(service, run_id).state == "succeeded"
    assert service.runs[run_id].parameters["minimum"].incarnation == 1

    response = recreate(service, "06-recreate", default=300)
    assert response.ok, response.errors
    # The recreated parameter starts a new identity and a fresh state.
    assert service.state.incarnations["minimum"] == 2
    assert service.parameter_state["minimum"].value == 300
    assert service.parameter_state["minimum"].revision == 1

    publication = service.publish_run(run_id)
    assert publication.outcome == "superseded"
    assert "incarnation" in publication.reason
    run = service.runs[run_id]
    assert run.disposition == "superseded"
    # The verified outputs stayed historical: no target reference moved,
    # so the workspace keeps showing what it showed before (13.3, 13.4).
    assert service.document["projections"]["sales"]["source"] is None
    status = status_of(service)
    assert status["status"] == "empty"
    assert "result" not in status
    assert status["resultIsStale"] is False
    # The publication receipt recorded the superseded outcome unchanged.
    (receipt,) = service.publications.values()
    assert receipt.outcome == "superseded"
    assert receipt.runId == run_id

    # Publishing again replays the concluded outcome without a commit.
    again = service.publish_run(run_id)
    assert again.outcome == "superseded"
    assert again.commit_sequence == service.commit_sequence


def test_recreation_with_the_same_value_still_rejects(
    service_adapter: PlatformAdapter,
):
    """Equal values do not restore an identity (SPEC.md section 12.3)."""

    seeded = seed_workspace(service_adapter)
    service = seeded.service
    run_id = admitted_run(service, "05-invoke")
    assert launch(service, run_id).state == "succeeded"

    response = recreate(service, "06-recreate", default=0)
    assert response.ok, response.errors
    assert service.parameter_state["minimum"].value == 0
    assert service.parameter_state["minimum"].revision == 1

    publication = service.publish_run(run_id)
    assert publication.outcome == "superseded"


def test_the_recreated_parameter_marks_the_previous_result_stale(
    service_adapter: PlatformAdapter,
):
    """A current result that captured the retired identity drifts stale.

    The retained result keeps its sources and stays visible, but its
    captured dependencies no longer hold, so the group reports `stale`
    with the identity reason instead of `current` (SPEC.md sections
    13.3 and 13.5).
    """

    seeded = seed_workspace(service_adapter)
    service = seeded.service
    first = admitted_run(service, "05-invoke")
    assert launch(service, first).state == "succeeded"
    assert service.publish_run(first).outcome == "current"
    assert status_of(service)["status"] == "current"

    response = recreate(service, "06-recreate", default=300)
    assert response.ok, response.errors

    status = status_of(service)
    assert status["status"] == "stale"
    assert status["resultIsStale"] is True
    assert "incarnation" in status["staleness"]
    # The previous visible result survives untouched.
    assert status["result"]["runId"] == first
    assert service.document["projections"]["sales"]["source"] == {
        "sourceId": "analyze-sales-filtered",
        "version": 1,
    }


def test_the_regression_holds_for_the_extension_language(
    service_adapter: PlatformAdapter,
):
    """`melt/semantic-draft-1` refuses the same obsolete run."""

    seeded = seed_workspace(service_adapter)
    service = seeded.service
    run_id = admitted_run(service, "05-invoke")
    assert launch(service, run_id).state == "succeeded"

    migrated = migrate(service, "06-migrate")
    assert migrated.ok, migrated.errors

    response = recreate(service, "07-recreate", default=300)
    assert response.ok, response.errors
    assert service.state.incarnations["minimum"] == 2

    publication = service.publish_run(run_id)
    assert publication.outcome == "superseded"
    assert service.runs[run_id].disposition == "superseded"


def test_the_rejection_survives_a_restart_between_recreation_and_publication(
    service_adapter: PlatformAdapter,
):
    """The identity data is durable, so a restart cannot lose the check.

    The run succeeded before the recreation; the restart reopens the
    committed incarnations and run records, publishes the staged
    outputs, and still refuses the older run, without executing
    anything on its own (SPEC.md sections 12.3 and 14).
    """

    seeded = seed_workspace(service_adapter, clock=FakeClock())
    service = seeded.service
    run_id = admitted_run(service, "05-invoke")
    assert launch(service, run_id).state == "succeeded"
    response = recreate(service, "06-recreate", default=300)
    assert response.ok, response.errors

    service.close()
    with WorkspaceService.open(
        service_adapter, WORKSPACE, clock=FakeClock()
    ) as reopened:
        assert reopened.state.incarnations["minimum"] == 2
        run = reopened.runs[run_id]
        assert run.state == "succeeded"
        assert run.parameters["minimum"].incarnation == 1
        publication = reopened.publish_run(run_id)
        assert publication.outcome == "superseded"
        assert reopened.runs[run_id].disposition == "superseded"
        # Recovery executed nothing and offered no silent rerun.
        assert reopened.dispatch_due_intents(FakeClock().advance(10_000)) == []
        sequence = reopened.commit_sequence
        acknowledged = reopened.publications

    # A lost acknowledgement after the committed conclusion replays
    # the stored superseded outcome after another restart: the retry
    # commits nothing and reruns nothing (SPEC.md sections 12.5, 18.4).
    with WorkspaceService.open(
        service_adapter, WORKSPACE, clock=FakeClock()
    ) as reopened:
        replay = reopened.publish_run(run_id)
        assert replay.outcome == "superseded"
        assert replay.commit_sequence == sequence
        assert len(reopened.publications) == len(acknowledged)


# ---------------------------------------------------------------------------
# Launch and dispatch
# ---------------------------------------------------------------------------


def test_a_recreated_parameter_refuses_the_launch_of_a_captured_run(
    service_adapter: PlatformAdapter,
):
    """The identity check runs immediately before launch too (13.1)."""

    seeded = seed_workspace(service_adapter)
    service = seeded.service
    run_id = admitted_run(service, "05-invoke")

    # The dry run reports the captured run as invalidated before the
    # transaction writes anything (SPEC.md section 12.4).
    previewed = service.dispatch(
        envelope(
            "06-preview",
            method="workspace.preview",
            expected={"parameters/minimum": 1},
            operations=[
                {
                    "op": "entity.delete",
                    "collection": "parameters",
                    "id": "minimum",
                },
                {
                    "op": "entity.create",
                    "collection": "parameters",
                    "id": "minimum",
                    "value": {**MINIMUM_PARAMETER, "default": 300},
                },
            ],
        )
    )
    assert previewed.ok, previewed.errors
    assert previewed.result["invalidatedRuns"] == [run_id]

    response = recreate(service, "07-recreate", default=300)
    assert response.ok, response.errors

    refusal = service.launch_admitted_run(run_id)
    assert not refusal.launched
    assert refusal.refusal[0].code == DiagnosticCode.CONFLICT
    assert "incarnation" in refusal.refusal[0].message
    assert service.runs[run_id].state == "queued"

    # Cancellation clears the refused run; a fresh invocation captures
    # the new identity and runs.
    cancelled = service.dispatch(
        envelope("08-cancel", method="run.cancel", payload={"runId": run_id})
    )
    assert cancelled.ok, cancelled.errors
    second = invoke_now(service, "09-invoke")
    assert second.ok, second.errors
    assert launch(service, second.result["run"]["runId"]).state == "succeeded"
    assert (
        service.publish_run(second.result["run"]["runId"]).outcome == "current"
    )


def test_a_recreated_parameter_expires_a_pending_intent_at_dispatch(
    service_adapter: PlatformAdapter,
):
    """Dispatch rejects the stale captured identity (SPEC.md 9.3)."""

    seeded = seed_workspace(service_adapter)
    service = seeded.service
    response = set_parameter(
        service, "05-set-minimum", value=100, expected_revision=1
    )
    assert response.ok, response.errors
    assert service.pending_intents["sales-results"].status == "pending"

    response = recreate(service, "06-recreate", default=300)
    assert response.ok, response.errors
    runs_before = sorted(service.runs)

    dispatched = service.dispatch_due_intents(seeded.clock.advance(300))
    assert [item["status"] for item in dispatched] == ["expired"]
    assert "incarnation" in dispatched[0]["reason"]
    assert sorted(service.runs) == runs_before
    assert service.pending_intents["sales-results"].status == "expired"


# ---------------------------------------------------------------------------
# The new identity stays usable
# ---------------------------------------------------------------------------


def test_an_ordinary_unchanged_run_still_publishes(
    service_adapter: PlatformAdapter,
):
    """The identity check narrows nothing for unchanged parameters."""

    seeded = seed_workspace(service_adapter)
    service = seeded.service
    run_id = admitted_run(service, "05-invoke")
    assert launch(service, run_id).state == "succeeded"
    assert service.publish_run(run_id).outcome == "current"
    assert status_of(service)["status"] == "current"


def test_a_run_admitted_after_the_recreation_publishes_current(
    service_adapter: PlatformAdapter,
):
    """The workspace keeps working against the new identity."""

    seeded = seed_workspace(service_adapter)
    service = seeded.service
    run_id = admitted_run(service, "05-invoke")
    assert launch(service, run_id).state == "succeeded"

    response = recreate(service, "06-recreate", default=300)
    assert response.ok, response.errors

    second = invoke_now(service, "07-invoke")
    assert second.ok, second.errors
    captured = second.result["intent"]["capturedParameters"]["minimum"]
    assert captured["value"] == 300
    assert captured["stateRevision"] == 1
    assert captured["stateIncarnation"] == 2
    second_run = second.result["run"]["runId"]
    assert service.runs[second_run].parameters["minimum"].incarnation == 2
    assert service.runs[second_run].parameterIds == {"minimum": "minimum"}
    assert launch(service, second_run).state == "succeeded"
    assert service.publish_run(second_run).outcome == "current"
    assert service.document["projections"]["sales"]["source"] == {
        "sourceId": "analyze-sales-filtered",
        "version": 1,
    }


def test_a_binding_fired_inside_the_recreating_transaction_captures_the_new_identity(
    service_adapter: PlatformAdapter,
):
    """A coalesced admission captures the identity its transaction mints.

    The transaction recreates the parameter and supplies its value, so
    the triggered binding captures the final value with the new
    incarnation, not the one the committed state still held while the
    transaction was planned (SPEC.md sections 12.3 and 13.1).
    """

    seeded = seed_workspace(service_adapter)
    service = seeded.service
    response = recreate(
        service, "05-recreate-and-set", default=300, set_value=400
    )
    assert response.ok, response.errors
    assert service.state.incarnations["minimum"] == 2
    assert service.parameter_state["minimum"].value == 400
    admissions = response.result.get("admissions", [])
    assert admissions, "the coalesced binding admitted its intent"
    intent = service.pending_intents["sales-results"]
    assert intent.parameters["minimum"].incarnation == 2
    assert intent.parameterIds == {"minimum": "minimum"}

    dispatched = service.dispatch_due_intents(seeded.clock.advance(300))
    assert [item["status"] for item in dispatched] == ["dispatched"]
    run_id = dispatched[0]["run"]["runId"]
    assert service.runs[run_id].parameters["minimum"].incarnation == 2
    assert launch(service, run_id).state == "succeeded"
    assert service.publish_run(run_id).outcome == "current"


# ---------------------------------------------------------------------------
# Older saved records that lack the identity data
# ---------------------------------------------------------------------------


def test_a_record_without_identity_publishes_only_while_unrecreated(
    service_adapter: PlatformAdapter,
):
    """Safe handling of older run records (SPEC.md section 14).

    A run saved before identity capture holds no incarnation. The
    service reads it as the first incarnation: it keeps publishing
    while its parameter was never recreated — the ordinary upgrade
    path — but a recreation, which only moves the stored incarnation
    above one, can never satisfy it. No uncertain effect reruns.
    """

    seeded = seed_workspace(service_adapter)
    service = seeded.service
    run_id = admitted_run(service, "05-invoke")
    assert launch(service, run_id).state == "succeeded"
    forge_legacy_capture(service, run_id)

    # Unrecreated: the older record still publishes as it always did.
    assert service.publish_run(run_id).outcome == "current"

    # A second older record, admitted before a recreation, must not.
    second = invoke_now(service, "06-invoke")
    assert second.ok, second.errors
    second_run = second.result["run"]["runId"]
    assert launch(service, second_run).state == "succeeded"
    response = recreate(service, "07-recreate", default=300)
    assert response.ok, response.errors
    forge_legacy_capture(service, second_run)
    publication = service.publish_run(second_run)
    assert publication.outcome == "superseded"
    assert service.runs[second_run].disposition == "superseded"
    # The first publication's visible result survived.
    assert service.document["projections"]["sales"]["source"] == {
        "sourceId": "analyze-sales-filtered",
        "version": 1,
    }


# ---------------------------------------------------------------------------
# The captured shape
# ---------------------------------------------------------------------------


def test_admission_captures_the_parameter_identity(
    service_adapter: PlatformAdapter,
):
    """The snapshot carries the identity of every mapped parameter."""

    seeded = seed_workspace(service_adapter)
    service = seeded.service
    response = invoke(service, "05-invoke")
    assert response.ok, response.errors
    run = service.runs[response.result["run"]["runId"]]
    captured = run.parameters["minimum"]
    assert isinstance(captured, CapturedParameter)
    assert captured.value == 0
    assert captured.revision == 1
    assert captured.incarnation == 1
    assert run.parameterIds == {"minimum": "minimum"}
