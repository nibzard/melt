"""Atomic publication and result status (T017, SPEC.md sections 13.3 to 13.5).

The tests prove the publication transaction of the workspace service
end to end against an isolated workspace with real durable storage:

- The six steps of section 13.4 run inside one owning transaction: the
  staged files are verified independently of the runner's report, the
  immutable artifact paths are finalized, every eligibility condition
  of section 13.3 is rechecked inside the transaction — including the
  publication permission gate — the output source versions are
  allocated with service-only run provenance, all target references
  update together, and the run disposition lands with a publication
  receipt before the single commit acknowledges anything.
- A missing, invalid, or non-finite output rejects the entire
  publication, so the previous table and plot remain together.
- A verified but obsolete run archives as superseded historical output
  that no current reference points at.
- Revoked permission blocks the publication and requests cancellation.
- Every output group reports empty, pending, current, stale, or error
  status with resultIsStale and the captured parameters of the retained
  result (section 13.5).

The seeding harness is the one the scheduling tests of T016 use, so
both suites drive the exact service handlers of SPEC.md section 12.6.
Everything runs under the Looper resource prefix, never against the
user's data directory.
"""

from __future__ import annotations

import io
import json

import pandas as pd

from runtime.storage import PlatformAdapter
from runtime.workspace import WorkspaceService, local_environment
from runtime.workspace.publication import target_ineligibility

# The T016 scheduling harness: seeding, invocation, parameters, launch.
from test_scheduling import (
    FULL_CONTRACT,
    LIGHT_CONTRACT,
    LIGHT_SALES_SCRIPT,
    PYTHON,
    REVENUE_PROJECTION,
    SPEC_SALES_SCRIPT,
    FakeClock,
    apply_envelope,
    envelope,
    invoke,
    launch,
    requires_storage_worker,
    sales_action,
    seed_workspace,
    set_parameter,
)

pytestmark = requires_storage_worker

WORKSPACE = "sales-demo"

FILTERED_SOURCE = "analyze-sales-filtered"
MONTHLY_SOURCE = "analyze-sales-monthly"

# The specification script with a guarded fault: the run stages both
# outputs cleanly, then corrupts the plot only when the captured
# minimum is high. The same registration therefore produces one good
# publication and one invalid publication.
TAMPERED_PLOT_SCRIPT = SPEC_SALES_SCRIPT + (
    "from pathlib import Path\n"
    "if ui.params['minimum'] >= 300:\n"
    "    Path('outputs/monthly.png').write_bytes(b'this is not a png file')\n"
)

# The same guard, but removing the staged plot entirely: the runner
# still exits complete, because it counts the outputs the script staged.
MISSING_PLOT_SCRIPT = SPEC_SALES_SCRIPT + (
    "from pathlib import Path\n"
    "if ui.params['minimum'] >= 300:\n"
    "    Path('outputs/monthly.png').unlink()\n"
)

# A light script whose staged table carries a genuine non-finite float
# when the captured minimum is high (SPEC.md section 5.1 rejects these
# during publication in v0.1). The pandas writer turns NaN into nulls,
# so the fault rewrites the staged Parquet through Arrow, which keeps a
# stored NaN distinct from a null cell.
NON_FINITE_SCRIPT = (
    "import pandas as pd\n"
    "import pyarrow as pa\n"
    "import pyarrow.parquet as pq\n"
    "from pathlib import Path\n"
    "from melt import ui\n"
    "\n"
    "sales = pd.read_parquet(ui.input_path('sales'))\n"
    "selected = sales[sales['revenue'] >= ui.params['minimum']]\n"
    "ui.output('filtered', selected)\n"
    "if ui.params['minimum'] >= 300:\n"
    "    path = Path('outputs/filtered.parquet')\n"
    "    table = pq.read_table(path)\n"
    "    values = table.column('revenue').to_pylist()\n"
    "    values[0] = float('nan')\n"
    "    index = table.schema.get_field_index('revenue')\n"
    "    pq.write_table(table.set_column(\n"
    "        index, 'revenue', pa.array(values, type=pa.float64())\n"
    "    ), path)\n"
)


def seed_full(
    adapter: PlatformAdapter,
    *,
    script: str = SPEC_SALES_SCRIPT,
    **kwargs,
):
    """Seed the two-output sales workspace of SPEC.md section 16."""

    return seed_workspace(
        adapter,
        script=script,
        contract=FULL_CONTRACT,
        action_overrides={
            "outputs": {"filtered": "table", "monthly": "image"},
            "targets": {"filtered": "sales", "monthly": "revenue"},
        },
        extra_projections={"revenue": REVENUE_PROJECTION},
        **kwargs,
    )


def run_at(
    service: WorkspaceService,
    seeded,
    value: int,
    request_id: str,
    expected_revision: int,
) -> str:
    """Drive one bound run at one captured minimum value."""

    response = set_parameter(
        service, request_id, value=value, expected_revision=expected_revision
    )
    assert response.ok, response.errors
    dispatched = service.dispatch_due_intents(seeded.clock.advance(300))
    assert [item["status"] for item in dispatched] == ["dispatched"]
    run_id = dispatched[0]["run"]["runId"]
    outcome = launch(service, run_id)
    assert outcome.state == "succeeded", outcome.refusal
    return run_id


def status_of(service: WorkspaceService, group: str = "sales-results") -> dict:
    """Read one output group's result status through inspection."""

    inspected = service.dispatch(
        envelope("status-inspect", method="workspace.inspect")
    )
    assert inspected.ok, inspected.errors
    return inspected.result["outputGroups"][group]


# ---------------------------------------------------------------------------
# The current publication (SPEC.md sections 13.4 and 18.2)
# ---------------------------------------------------------------------------


def test_publication_makes_both_outputs_current_together(
    service_adapter: PlatformAdapter,
):
    seeded = seed_full(service_adapter)
    service = seeded.service
    run_id = run_at(service, seeded, 200, "05-run", 1)
    sequence = service.commit_sequence
    layout_before = json.loads(json.dumps(service.document["layout"]))
    outcome = service.publish_run(run_id)
    assert outcome.outcome == "current"
    assert outcome.commit_sequence == sequence + 1, "one commit acknowledged"

    document = service.document
    # Both outputs allocated service-owned sources with run provenance
    # attached by the service alone (SPEC.md sections 5.1 and 13.4).
    assert document["sources"][FILTERED_SOURCE]["kind"] == "table"
    assert document["sources"][MONTHLY_SOURCE]["kind"] == "image"
    for source_id, output in ((FILTERED_SOURCE, "filtered"), (MONTHLY_SOURCE, "monthly")):
        version = document["sources"][source_id]["versions"]["1"]
        assert version["origin"] == {"kind": "run", "runId": run_id, "output": output}
        assert document["sources"][source_id]["currentVersion"] == 1
    # All target references updated together.
    assert document["projections"]["sales"]["source"] == {
        "sourceId": FILTERED_SOURCE,
        "version": 1,
    }
    assert document["projections"]["revenue"]["source"] == {
        "sourceId": MONTHLY_SOURCE,
        "version": 1,
    }
    # The hero numbers of SPEC.md section 18.2 at minimum 200.
    table = document["sources"][FILTERED_SOURCE]["versions"]["1"]
    frame = pd.read_parquet(io.BytesIO(service.store.artifacts.read(table["artifact"])))
    assert len(frame) == 2
    assert frame.groupby("month", sort=True)["revenue"].sum().tolist() == [
        300.0,
        200.0,
    ]
    plot = document["sources"][MONTHLY_SOURCE]["versions"]["1"]
    assert service.store.artifacts.read(plot["artifact"]).startswith(b"\x89PNG")

    run = service.runs[run_id]
    assert run.disposition == "current"
    assert run.outputs["filtered"].sourceId == FILTERED_SOURCE
    assert run.outputs["monthly"].version == 1
    # The publication receipt landed with the same commit.
    (receipt,) = service.publications.values()
    assert receipt.runId == run_id
    assert receipt.outcome == "current"
    assert receipt.outputs["filtered"].sourceId == FILTERED_SOURCE
    # Publication changed no layout, parameter, generation, or intent.
    assert document["layout"] == layout_before
    assert service.parameter_state["minimum"].revision == 2
    assert service.output_groups["sales-results"].generation == 1
    assert service.pending_intents["sales-results"].status == "dispatched"
    # The notification names every changed record.
    window = service.changes_after(sequence, service.epoch)
    changed = window.changes[-1]["changedRecords"]
    assert f"runs/{run_id}" in changed
    assert any(path.startswith("publications/") for path in changed)
    assert f"sources/{FILTERED_SOURCE}" in changed
    assert f"sources/{MONTHLY_SOURCE}" in changed
    assert "projections/sales" in changed
    assert "projections/revenue" in changed
    # The hidden target pane still received its result (SPEC.md 18.3).
    assert document["layout"]["hidden"] == ["revenue"]
    # Entity revisions advanced for the allocated sources and targets.
    revisions = service.entity_revisions
    assert revisions[f"sources/{FILTERED_SOURCE}"] == 1
    assert revisions[f"sources/{MONTHLY_SOURCE}"] == 1
    assert revisions["projections/sales"] == 2
    assert revisions["projections/revenue"] == 2

    status = status_of(service)
    assert status["status"] == "current"
    assert status["resultIsStale"] is False
    assert status["result"]["runId"] == run_id
    assert status["result"]["capturedParameters"]["minimum"] == {
        "parameter": "minimum",
        "value": 200,
        "stateRevision": 2,
        "stateIncarnation": 1,
    }
    assert status["latestRun"]["disposition"] == "current"

    # Publishing again replays the concluded outcome without a commit.
    again = service.publish_run(run_id)
    assert again.outcome == "current"
    assert again.commit_sequence == service.commit_sequence
    assert len(service.publications) == 1


def test_a_second_publication_supersedes_the_previous_current_run(
    service_adapter: PlatformAdapter,
):
    seeded = seed_full(service_adapter)
    service = seeded.service
    first = run_at(service, seeded, 200, "05-run", 1)
    assert service.publish_run(first).outcome == "current"

    second = run_at(service, seeded, 300, "06-run", 2)
    outcome = service.publish_run(second)
    assert outcome.outcome == "current"
    document = service.document
    # The same stable output sources advanced to version two; the first
    # versions survive byte for byte (SPEC.md sections 5.2 and 13.4).
    assert document["sources"][FILTERED_SOURCE]["currentVersion"] == 2
    assert document["sources"][MONTHLY_SOURCE]["currentVersion"] == 2
    assert set(document["sources"][FILTERED_SOURCE]["versions"]) == {"1", "2"}
    assert document["sources"][FILTERED_SOURCE]["versions"]["1"]["origin"][
        "runId"
    ] == first
    assert document["sources"][FILTERED_SOURCE]["versions"]["2"]["origin"][
        "runId"
    ] == second
    assert document["projections"]["sales"]["source"]["version"] == 2
    assert document["projections"]["revenue"]["source"]["version"] == 2
    # The older current run flipped to superseded in the same commit.
    assert service.runs[first].disposition == "superseded"
    assert service.runs[second].disposition == "current"
    receipts = [r for r in service.publications.values() if r.runId == first]
    assert receipts[0].outcome == "current", "the receipt of its own publication"
    status = status_of(service)
    assert status["status"] == "current"
    assert status["result"]["capturedParameters"]["minimum"]["value"] == 300


def test_a_first_run_that_is_already_obsolete_archives_without_current(
    service_adapter: PlatformAdapter,
):
    seeded = seed_full(service_adapter)
    service = seeded.service
    # Generation one dispatches, then a newer change reserves generation
    # two before the older run publishes.
    response = set_parameter(service, "05-set", value=200, expected_revision=1)
    assert response.ok, response.errors
    dispatched = service.dispatch_due_intents(seeded.clock.advance(300))
    run_id = dispatched[0]["run"]["runId"]
    assert set_parameter(service, "06-newer", value=300, expected_revision=2).ok
    assert service.pending_intents["sales-results"].status == "pending"
    assert launch(service, run_id).state == "succeeded"

    outcome = service.publish_run(run_id)
    assert outcome.outcome == "superseded"
    document = service.document
    # The outputs archived as historical versions: allocated with run
    # provenance, but no current reference points at them and no
    # projection moved (SPEC.md sections 13.3 and 13.4).
    assert document["sources"][FILTERED_SOURCE]["currentVersion"] == 1
    assert document["sources"][FILTERED_SOURCE]["versions"]["1"]["origin"] == {
        "kind": "run",
        "runId": run_id,
        "output": "filtered",
    }
    assert document["projections"]["sales"]["source"] is None
    assert document["projections"]["revenue"]["source"] is None
    run = service.runs[run_id]
    assert run.disposition == "superseded"
    assert run.outputs["filtered"].sourceId == FILTERED_SOURCE
    # The newer pending request owns the status.
    status = status_of(service)
    assert status["status"] == "pending"
    assert "result" not in status


def test_an_obsolete_run_keeps_the_previous_pair_current(
    service_adapter: PlatformAdapter,
):
    seeded = seed_full(service_adapter)
    service = seeded.service
    first = run_at(service, seeded, 200, "05-run", 1)
    assert service.publish_run(first).outcome == "current"
    second = run_at(service, seeded, 300, "06-run", 2)
    # A newer change reserves generation three before the second run
    # publishes: the second run verifies but cannot become current.
    assert set_parameter(service, "07-newer", value=400, expected_revision=3).ok

    outcome = service.publish_run(second)
    assert outcome.outcome == "superseded"
    document = service.document
    assert document["sources"][FILTERED_SOURCE]["currentVersion"] == 1
    assert document["sources"][MONTHLY_SOURCE]["currentVersion"] == 1
    assert document["projections"]["sales"]["source"]["version"] == 1
    assert document["projections"]["revenue"]["source"]["version"] == 1
    assert service.runs[first].disposition == "current"
    assert service.runs[second].disposition == "superseded"
    # The status is pending on the newer request and retains the older
    # result with its captured parameters (SPEC.md sections 13.5, 18.3).
    status = status_of(service)
    assert status["status"] == "pending"
    assert status["resultIsStale"] is True
    assert status["result"]["capturedParameters"]["minimum"]["value"] == 200
    assert service.parameter_state["minimum"].value == 400


def test_a_changed_action_with_a_deleted_target_archives_the_old_output(
    service_adapter: PlatformAdapter,
):
    seeded = seed_full(service_adapter)
    service = seeded.service
    first = run_at(service, seeded, 200, "05-run", 1)
    assert service.publish_run(first).outcome == "current"

    # The second run is admitted and completes against the captured
    # definitions before the document changes underneath it.
    second = run_at(service, seeded, 300, "06-run", 2)

    # Removing a target projection forces the action to change in the
    # same transaction, so the compiled candidate stays valid: the new
    # action maps the light contract onto the surviving table pane.
    script_token = service.capture_artifact_token(
        LIGHT_SALES_SCRIPT.encode("utf-8"), PYTHON
    ).token
    registered = service.dispatch(
        envelope(
            "07-register-light",
            method="action.register",
            payload={
                "scriptToken": script_token,
                "environmentId": local_environment().environment_id,
                "contract": LIGHT_CONTRACT,
            },
        )
    )
    assert registered.ok, registered.errors
    light_id = registered.result["registrationId"]
    service.grant_execution_permission(
        kind="projectExecution",
        registration_id=light_id,
        granted_by="host-permission-control/run-approval",
    )
    replaced = service.apply(
        apply_envelope(
            "08-redefine",
            {
                "actions/analyze-sales": 1,
                # The first publication retargeted the pane, so its
                # entity revision advanced past the creation revision.
                "projections/revenue": service.entity_revisions[
                    "projections/revenue"
                ],
                "layout": service.entity_revisions["layout"],
            },
            [
                {
                    "op": "entity.replace",
                    "collection": "actions",
                    "id": "analyze-sales",
                    "value": sales_action(light_id),
                },
                {
                    "op": "entity.delete",
                    "collection": "projections",
                    "id": "revenue",
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
    assert replaced.ok, replaced.errors

    outcome = service.publish_run(second)
    # The action definition changed during execution, so the run cannot
    # become current: the previous table and plot remain together while
    # the old output archives (SPEC.md sections 12.3, 13.3, and 18.3).
    assert outcome.outcome == "superseded"
    document = service.document
    assert document["projections"]["sales"]["source"]["version"] == 1
    assert document["sources"][FILTERED_SOURCE]["currentVersion"] == 1
    assert document["sources"][MONTHLY_SOURCE]["versions"]["1"] is not None
    status = status_of(service)
    assert status["status"] == "stale"
    assert status["result"]["capturedParameters"]["minimum"]["value"] == 200


# ---------------------------------------------------------------------------
# Rejected publications (SPEC.md sections 13.3 and 13.4)
# ---------------------------------------------------------------------------


def test_an_invalid_output_rejects_the_entire_publication(
    service_adapter: PlatformAdapter,
):
    seeded = seed_full(service_adapter, script=TAMPERED_PLOT_SCRIPT)
    service = seeded.service
    first = run_at(service, seeded, 200, "05-run", 1)
    assert service.publish_run(first).outcome == "current"
    before = json.loads(json.dumps(service.document))

    second = run_at(service, seeded, 300, "06-run", 2)
    sequence = service.commit_sequence
    outcome = service.publish_run(second)
    assert outcome.outcome == "rejected"
    assert outcome.refusal[0].code == "E_ARTIFACT"
    assert service.commit_sequence == sequence + 1
    # Nothing was allocated: the previous table and plot remain together
    # (SPEC.md section 13.4).
    assert service.document == before
    assert service.runs[second].disposition == "rejected"
    assert service.runs[second].outputs == {}
    (receipt,) = [r for r in service.publications.values() if r.runId == second]
    assert receipt.outcome == "rejected"
    status = status_of(service)
    assert status["latestRun"]["disposition"] == "rejected"
    assert status["result"]["capturedParameters"]["minimum"]["value"] == 200


def test_a_missing_output_rejects_the_entire_publication(
    service_adapter: PlatformAdapter,
):
    seeded = seed_full(service_adapter, script=MISSING_PLOT_SCRIPT)
    service = seeded.service
    run_id = run_at(service, seeded, 300, "05-run", 1)
    outcome = service.publish_run(run_id)
    assert outcome.outcome == "rejected"
    assert outcome.refusal[0].code == "E_ARTIFACT"
    assert "no staged file" in outcome.refusal[0].message
    document = service.document
    # Neither projection moved: no partial publication at all.
    assert document["projections"]["sales"]["source"] is None
    assert document["projections"]["revenue"]["source"] is None
    assert FILTERED_SOURCE not in document["sources"]
    assert status_of(service)["status"] == "empty"


def test_non_finite_table_floats_reject_the_publication(
    service_adapter: PlatformAdapter,
):
    seeded = seed_workspace(service_adapter, script=NON_FINITE_SCRIPT)
    service = seeded.service
    run_id = run_at(service, seeded, 300, "05-run", 1)
    outcome = service.publish_run(run_id)
    assert outcome.outcome == "rejected"
    assert outcome.refusal[0].code == "E_TYPE"
    assert "non-finite" in outcome.refusal[0].message
    assert service.document["projections"]["sales"]["source"] is None
    assert service.runs[run_id].disposition == "rejected"


def test_revoked_permission_blocks_publication_and_requests_cancellation(
    service_adapter: PlatformAdapter,
):
    seeded = seed_full(service_adapter)
    service = seeded.service
    run_id = run_at(service, seeded, 200, "05-run", 1)
    grant_id = service.runs[run_id].permissionDecision
    service.revoke_execution_permission(
        grant_id, revoked_by="host-permission-control/revoke"
    )
    outcome = service.publish_run(run_id)
    assert outcome.outcome == "rejected"
    assert outcome.refusal[0].code == "E_PERMISSION"
    assert outcome.cancellation_requested is True
    # The process finished before the revocation; its state stays
    # honest while the publication is refused and the cancellation
    # request is recorded on the receipt (SPEC.md sections 13.3, 13.5).
    assert service.runs[run_id].state == "succeeded"
    assert service.runs[run_id].disposition == "rejected"
    (receipt,) = service.publications.values()
    assert receipt.cancellationRequested is True
    assert service.document["projections"]["sales"]["source"] is None
    assert status_of(service)["status"] == "empty"


def test_target_ineligibility_names_a_broken_target():
    # The eligibility rule of SPEC.md section 13.3: every declared
    # output needs an existing, kind-compatible target. A committed
    # document satisfies this by compilation, so the rule is proven
    # directly against the check the transaction runs.
    action = {"outputs": {"filtered": "table"}, "targets": {"filtered": "revenue"}}
    missing = target_ineligibility(
        {"projections": {"sales": {"kind": "table"}}}, "analyze-sales", action
    )
    assert missing.code == "E_REFERENCE"
    assert "revenue" in missing.message
    mismatched = target_ineligibility(
        {"projections": {"revenue": {"kind": "plot"}}}, "analyze-sales", action
    )
    assert mismatched.code == "E_TYPE"
    healthy = target_ineligibility(
        {"projections": {"sales": {"kind": "table"}}},
        "analyze-sales",
        {"outputs": {"filtered": "table"}, "targets": {"filtered": "sales"}},
    )
    assert healthy is None


def test_publish_run_answers_not_publishable_for_unfinished_runs(
    service_adapter: PlatformAdapter,
):
    seeded = seed_full(service_adapter)
    service = seeded.service
    queued = invoke(service, "05-invoke").result["run"]["runId"]
    sequence = service.commit_sequence
    queued_outcome = service.publish_run(queued)
    assert queued_outcome.outcome == "not-publishable"
    assert queued_outcome.refusal[0].code == "E_CONFLICT"
    assert service.commit_sequence == sequence, "no commit answered the refusal"
    assert service.publications == {}

    # A cancelled run is not eligible either: publication refuses it
    # and the group reports the cancellation as its error state with a
    # cancellation reason (SPEC.md sections 13.3 and 13.5).
    cancelled = service.dispatch(
        envelope("06-cancel", method="run.cancel", payload={"runId": queued})
    )
    assert cancelled.ok, cancelled.errors
    refused = service.publish_run(queued)
    assert refused.outcome == "not-publishable"
    assert service.runs[queued].disposition == "none"
    assert service.publications == {}
    status = status_of(service)
    assert status["status"] == "error"
    assert status["reason"] == "the newest request was cancelled"
    assert "result" not in status


def test_a_failed_run_reports_an_error_status_without_publication(
    service_adapter: PlatformAdapter,
):
    failing = LIGHT_SALES_SCRIPT + "raise RuntimeError('the analysis exploded')\n"
    seeded = seed_workspace(service_adapter, script=failing)
    service = seeded.service
    run_id = invoke(service, "05-invoke").result["run"]["runId"]
    outcome = launch(service, run_id)
    assert outcome.state == "failed"
    refused = service.publish_run(run_id)
    assert refused.outcome == "not-publishable"
    assert service.runs[run_id].disposition == "none"
    assert service.publications == {}
    status = status_of(service)
    assert status["status"] == "error"
    assert status["reason"] == "the newest run failed"
    assert "result" not in status


# ---------------------------------------------------------------------------
# The result status machine (SPEC.md section 13.5)
# ---------------------------------------------------------------------------


def test_the_result_status_moves_empty_pending_current_stale_error(
    service_adapter: PlatformAdapter,
):
    seeded = seed_full(service_adapter)
    service = seeded.service
    # Empty before any result exists.
    assert status_of(service)["status"] == "empty"

    # Pending while the admitted run is queued.
    run_id = invoke(service, "05-invoke").result["run"]["runId"]
    pending = status_of(service)
    assert pending["status"] == "pending"
    assert pending["reason"] == "execution is in flight"

    assert launch(service, run_id).state == "succeeded"
    assert service.publish_run(run_id).outcome == "current"
    assert status_of(service)["status"] == "current"

    # A programmatic change saves its value, emits no event, and marks
    # the affected result stale (SPEC.md sections 9.2 and 18.3).
    response = set_parameter(
        service,
        "06-programmatic",
        value=300,
        expected_revision=1,
        origin="programmatic",
    )
    assert response.ok, response.errors
    stale = status_of(service)
    assert stale["status"] == "stale"
    assert stale["resultIsStale"] is True
    assert "state revision" in stale["staleness"]
    assert stale["result"]["capturedParameters"]["minimum"]["value"] == 0
    assert service.parameter_state["minimum"].value == 300
    assert service.pending_intents["sales-results"].status == "dispatched"

    # A cancelled newest request is an error state that retains the last
    # good result with its older parameters (SPEC.md sections 13.5, 18.3).
    response = set_parameter(service, "07-set", value=400, expected_revision=2)
    assert response.ok, response.errors
    dispatched = service.dispatch_due_intents(seeded.clock.advance(300))
    newest = dispatched[0]["run"]["runId"]
    cancelled = service.dispatch(
        envelope("08-cancel", method="run.cancel", payload={"runId": newest})
    )
    assert cancelled.ok, cancelled.errors
    error = status_of(service)
    assert error["status"] == "error"
    assert error["reason"] == "the newest request was cancelled"
    assert error["resultIsStale"] is True
    assert error["result"]["capturedParameters"]["minimum"]["value"] == 0
    # A cancelled run is not eligible: publication refuses it outright
    # (SPEC.md section 13.3).
    refused = service.publish_run(newest)
    assert refused.outcome == "not-publishable"


# ---------------------------------------------------------------------------
# Durability (SPEC.md sections 13.4 and 14)
# ---------------------------------------------------------------------------


def test_publication_records_rebuild_after_a_restart(
    service_adapter: PlatformAdapter,
):
    seeded = seed_full(service_adapter)
    service = seeded.service
    run_id = run_at(service, seeded, 200, "05-run", 1)
    assert service.publish_run(run_id).outcome == "current"
    document = service.document
    receipt_id = next(iter(service.publications))
    filtered_artifact = document["sources"][FILTERED_SOURCE]["versions"]["1"][
        "artifact"
    ]
    service.close()

    clock = FakeClock()
    with WorkspaceService.open(
        service_adapter, WORKSPACE, clock=clock
    ) as reopened:
        # Startup restored the definitions, state, run disposition,
        # publication receipt, and catalog without executing anything
        # (SPEC.md section 14, steps 2, 3, and 6).
        assert reopened.document == document
        assert reopened.runs[run_id].disposition == "current"
        assert reopened.runs[run_id].outputs["filtered"].version == 1
        assert set(reopened.publications) == {receipt_id}
        assert reopened.store.artifacts.verify(filtered_artifact)
        inspected = reopened.dispatch(
            envelope("06-inspect", method="workspace.inspect")
        )
        assert inspected.ok, inspected.errors
        assert inspected.result["diagnostics"] == []
        assert (
            inspected.result["outputGroups"]["sales-results"]["status"] == "current"
        )
        # A rerun after restart allocates version two of the same
        # sources: the mapping rebuilt from run provenance is stable.
        response = set_parameter(
            reopened, "07-set", value=300, expected_revision=2
        )
        assert response.ok, response.errors
        dispatched = reopened.dispatch_due_intents(clock.advance(10_000))
        second = dispatched[0]["run"]["runId"]
        assert launch(reopened, second).state == "succeeded"
        assert reopened.publish_run(second).outcome == "current"
        assert (
            reopened.document["sources"][FILTERED_SOURCE]["currentVersion"] == 2
        )
        assert reopened.document["projections"]["sales"]["source"]["version"] == 2


def test_the_publication_transaction_is_one_commit_with_all_records(
    service_adapter: PlatformAdapter,
):
    seeded = seed_full(service_adapter)
    service = seeded.service
    run_id = run_at(service, seeded, 200, "05-run", 1)
    sequence = service.commit_sequence
    notifications = len(service.notifications)
    assert service.publish_run(run_id).commit_sequence == sequence + 1
    assert len(service.notifications) == notifications + 1
    # The verified artifacts are committed as records a reopen rebuilds
    # its catalog from, next to the run and the receipt.
    from runtime.workspace import ARTIFACT_KIND, PUBLICATION_KIND, RUN_KIND

    service.close()
    from runtime.storage import WorkspaceStore

    store = WorkspaceStore.open(service_adapter, WORKSPACE)
    try:
        kinds = [record.kind for record in store.records()]
    finally:
        store.close()
    assert kinds.count(PUBLICATION_KIND) == 1
    assert kinds.count(RUN_KIND) >= 1
    assert kinds.count(ARTIFACT_KIND) >= 3, "the import plus both outputs"
