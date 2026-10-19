"""The race suite of SPEC.md section 18.3 (backlog task T021).

Eleven scenarios mutate the workspace while one run executes for real.
Each test launches the specification's sales script through a start and
release gate inside the run's private staging directory: the process
writes ``race-started`` when it begins to execute and then waits for
``race-release``. The gate proves the child is alive at the exact
moment the test commits its mutation, so every "during execution" case
races a live process instead of the dispatch-to-launch window the
earlier suites used.

The launch path of round 1 blocked its caller until the process exits
(``WorkspaceService.launch_admitted_run``), so each test drives that
call from one helper thread and mutates through the same service
handlers from the test thread while the launcher parks inside the
process wait. The mutations therefore interleave with a live project
process under both drivers: this blocking one and the non-blocking
execution slot of backlog task T035, whose framed-channel liveness and
signal-driven cancellation the execution-slot suite proves
(``tests/integration/test_execution_slot.py``).

Scenario map (SPEC.md section 18.3, rows in table order):

- Run A finishes after a parameter change but before debounce fires:
  ``test_a_run_finishing_before_debounce_fires_cannot_become_current``
- Three parameter changes occur during a run:
  ``test_three_changes_during_a_run_leave_one_newest_pending_request``
- A newer run fails:
  ``test_a_newer_failed_run_keeps_the_last_good_result``
- Action definition changes during execution:
  ``test_an_action_change_during_execution_archives_the_old_output``
- Target pane is hidden:
  ``test_a_hidden_target_pane_still_receives_its_publication``
- Target pane is deleted:
  ``test_a_deleted_target_pane_rejects_the_entire_publication``
- Layout or title changes:
  ``test_layout_and_title_changes_keep_the_publication_valid``
- Parameter changes and changes back:
  ``test_a_parameter_round_trip_still_prevents_old_publication``
- Programmatic parameter update:
  ``test_a_programmatic_update_saves_and_marks_the_result_stale``
- Duplicate activation request:
  ``test_a_duplicate_activation_request_admits_at_most_one_run``
- Permission is revoked during execution:
  ``test_revoked_permission_during_execution_blocks_publication``

The deleted-target row deserves one note. A committed document of
v0.1 cannot hold an action whose target projection is gone, because
compilation rejects the dangling reference, so deleting a target pane
arrives in one transaction that also redefines the action onto its
surviving targets (SPEC.md sections 12.2 and 12.3). The expected result
— the entire current publication is rejected — is proven as such a
transaction landing mid-execution: no output becomes current, not even
the one whose target still exists, and the output group revision bumps
so the whole group's publication is invalidated. The broken-target
branch of the publication guard keeps its direct proof in
``test_publication.target_ineligibility``.
"""

from __future__ import annotations

import threading
import time

from runtime.storage import PlatformAdapter
from runtime.workspace import WorkspaceService, local_environment

# The T016 scheduling harness: seeding, invocation, parameters, launch.
from test_scheduling import (
    FULL_CONTRACT,
    LIGHT_CONTRACT,
    LIGHT_SALES_SCRIPT,
    PYTHON,
    REVENUE_PROJECTION,
    SALES_PROJECTION,
    activate,
    apply_envelope,
    envelope,
    invoke,
    requires_storage_worker,
    sales_action,
    seed_workspace,
    set_parameter,
)

pytestmark = requires_storage_worker

FILTERED_SOURCE = "analyze-sales-filtered"
MONTHLY_SOURCE = "analyze-sales-monthly"

STARTED_NAME = "race-started"
"""The marker one gated process writes when it begins to execute."""

RELEASE_NAME = "race-release"
"""The marker one test writes when the gated process may finish."""

GATE_TIMEOUT_SECONDS = 120.0
"""The backstop deadline every gated script and gate wait uses."""

POLL_SECONDS = 0.005

FAILING_THRESHOLD = 200
"""The captured minimum at and above which the guarded script fails."""


def gated_script(*, full: bool = False, fail_at_minimum: "int | None" = None) -> str:
    """Return the sales loop behind the staging-directory gate.

    The process writes its started marker as its first action, then
    waits for the release marker the test controls. Only then does it
    run the specification's analysis and stage its outputs, so nothing
    is staged while the race window is open.
    """

    lines = [
        "import time",
        "from pathlib import Path",
        "import pandas as pd",
        "from melt import ui",
        "",
        "control = Path.cwd()",
        f"(control / {STARTED_NAME!r}).write_text('', encoding='utf-8')",
        f"deadline = time.monotonic() + {int(GATE_TIMEOUT_SECONDS)}",
        f"while not (control / {RELEASE_NAME!r}).exists():",
        "    if time.monotonic() > deadline:",
        "        raise RuntimeError('the race gate never opened')",
        f"    time.sleep({POLL_SECONDS})",
        "sales = pd.read_parquet(ui.input_path('sales'))",
        "selected = sales[sales['revenue'] >= ui.params['minimum']]",
        "ui.output('filtered', selected)",
    ]
    if full:
        lines.extend(
            [
                "import matplotlib.pyplot as plt",
                "monthly = selected.groupby('month', sort=True)['revenue'].sum()",
                "fig, ax = plt.subplots()",
                "monthly.plot(ax=ax)",
                "ax.set_ylabel('Revenue')",
                "ui.output('monthly', fig)",
                "plt.close(fig)",
            ]
        )
    if fail_at_minimum is not None:
        lines.extend(
            [
                f"if ui.params['minimum'] >= {fail_at_minimum}:",
                "    raise RuntimeError("
                f"        'the analysis exploded at minimum {fail_at_minimum}'"
                "    )",
            ]
        )
    return "\n".join(lines) + "\n"


class ExecutingRun:
    """One admitted run held mid-execution against its live process.

    Entering starts ``launch_admitted_run`` on a helper thread and waits
    until the process itself reports that it executes. Inside the
    ``with`` block the service keeps answering the test thread, which is
    the interleaving the non-blocking execution slot of backlog task
    T035 will give the framed channel. Leaving writes the release
    marker and joins the launcher, so the run always reaches a terminal
    state before the block ends, even when an assertion failed inside.
    """

    def __init__(
        self,
        service: WorkspaceService,
        run_id: str,
        *,
        timeout: float = GATE_TIMEOUT_SECONDS,
    ):
        self._service = service
        self.run_id = run_id
        self.staging = service.store.layout.staging_directory(run_id)
        self.started_marker = self.staging / STARTED_NAME
        self.release_marker = self.staging / RELEASE_NAME
        self.timeout = timeout
        self._outcome = None
        self._error: "BaseException | None" = None
        self._thread = threading.Thread(
            target=self._drive, name=f"race-launch-{run_id}"
        )

    def _drive(self) -> None:
        try:
            self._outcome = self._service.launch_admitted_run(self.run_id)
        except BaseException as error:  # pragma: no cover - surfaced below
            self._error = error

    def __enter__(self) -> "ExecutingRun":
        self.staging.mkdir(parents=True, exist_ok=True)
        # A fresh staging directory holds neither marker; the unlinks are
        # a backstop so a gate left behind can never skip the wait.
        self.release_marker.unlink(missing_ok=True)
        self.started_marker.unlink(missing_ok=True)
        self._thread.start()
        deadline = time.monotonic() + self.timeout
        while not self.started_marker.is_file():
            if self._error is not None:
                raise AssertionError(
                    f"the launch of run {self.run_id!r} raised before the"
                    f" process started: {self._error}"
                ) from self._error
            if not self._thread.is_alive():
                outcome = self._outcome
                refusal = (
                    "no outcome"
                    if outcome is None
                    else "; ".join(str(item.message) for item in outcome.refusal)
                )
                raise AssertionError(
                    f"run {self.run_id!r} left the launch path without"
                    f" reporting execution: state"
                    f" {self._service.runs[self.run_id].state!r},"
                    f" refusal: {refusal}"
                )
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"run {self.run_id!r} did not report execution within"
                    f" {self.timeout} seconds; state"
                    f" {self._service.runs[self.run_id].state!r}"
                )
            time.sleep(POLL_SECONDS)
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.release_marker.write_text("", encoding="utf-8")
        self._thread.join(timeout=self.timeout)
        if self._thread.is_alive():  # pragma: no cover - gate backstop
            raise AssertionError(
                f"the launcher of run {self.run_id!r} did not return after"
                f" {self.timeout} seconds with the gate open"
            )
        return False

    @property
    def outcome(self):
        """The launch outcome, available once the block has ended."""

        if self._error is not None:
            raise AssertionError(
                f"the launch of run {self.run_id!r} raised:"
                f" {self._error}"
            ) from self._error
        return self._outcome

    @property
    def state(self) -> str:
        """The current run state, read live from the service."""

        return self._service.runs[self.run_id].state


def seed_race(
    adapter: PlatformAdapter,
    *,
    script: str,
    full: bool = False,
    grant: bool = True,
    open_kwargs: "dict | None" = None,
):
    """Seed the sales workspace behind one gated script.

    ``full`` registers the two-output contract of SPEC.md section 16
    with the plot pane; the default registers the light table-only
    contract the scheduling suite uses. ``open_kwargs`` reach the
    ``WorkspaceService.open`` call of the seeding helper.
    """

    if not full:
        return seed_workspace(
            adapter, script=script, grant=grant, open_kwargs=open_kwargs
        )
    return seed_workspace(
        adapter,
        script=script,
        contract=FULL_CONTRACT,
        action_overrides={
            "outputs": {"filtered": "table", "monthly": "image"},
            "targets": {"filtered": "sales", "monthly": "revenue"},
        },
        extra_projections={"revenue": REVENUE_PROJECTION},
        grant=grant,
        open_kwargs=open_kwargs,
    )


def revision_of(service: WorkspaceService, path: str) -> int:
    """Read one entity revision for an expectation map."""

    return service.entity_revisions[path]


def invoke_current(service: WorkspaceService, request_id: str):
    """Invoke the action against its current revision."""

    return service.dispatch(
        envelope(
            request_id,
            method="action.invoke",
            expected={
                "actions/analyze-sales": revision_of(
                    service, "actions/analyze-sales"
                )
            },
            payload={"actionId": "analyze-sales"},
        )
    )


def dispatch_newest(seeded, service: WorkspaceService) -> str:
    """Fire the due debounce timer and return the minted run ID."""

    dispatched = service.dispatch_due_intents(seeded.clock.advance(300))
    assert [item["status"] for item in dispatched] == ["dispatched"]
    return dispatched[0]["run"]["runId"]


def status_of(service: WorkspaceService, group: str = "sales-results") -> dict:
    """Read one output group's result status through inspection."""

    inspected = service.dispatch(
        envelope("status-inspect", method="workspace.inspect")
    )
    assert inspected.ok, inspected.errors
    return inspected.result["outputGroups"][group]


def show_both_panes(seeded, service: WorkspaceService) -> None:
    """Place the seeded plot pane beside the table in the visible tree."""

    response = service.apply(
        apply_envelope(
            "10-both-visible",
            {"layout": revision_of(service, "layout")},
            [
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
                }
            ],
        )
    )
    assert response.ok, response.errors


# ---------------------------------------------------------------------------
# Row 1: a run finishing after a change but before the debounce fires
# ---------------------------------------------------------------------------


def test_a_run_finishing_before_debounce_fires_cannot_become_current(
    service_adapter: PlatformAdapter,
):
    seeded = seed_race(service_adapter, script=gated_script())
    service = seeded.service
    run_a = invoke(service, "05-invoke").result["run"]["runId"]

    with ExecutingRun(service, run_a) as executing:
        assert executing.state == "running"
        response = set_parameter(service, "06-set", value=200, expected_revision=1)
        assert response.ok, response.errors
        # The reservation landed while the process was alive: the timer
        # has not fired and already owns the publication (SPEC.md 9.3).
        assert service.output_groups["sales-results"].generation == 2
        assert service.pending_intents["sales-results"].status == "pending"
        assert response.result["admissions"][0]["generation"] == 2
        assert executing.state == "running"

    assert executing.outcome.launched
    assert executing.outcome.state == "succeeded"
    # The debounce never fired: the fake clock did not move.
    outcome = service.publish_run(run_a)
    assert outcome.outcome == "superseded"
    assert "reserved generation 2" in outcome.reason
    assert service.document["projections"]["sales"]["source"] is None
    status = status_of(service)
    assert status["status"] == "pending"
    assert "result" not in status

    # The pending request still owns the newest generation and runs with
    # its captured value once the timer fires.
    run_b = dispatch_newest(seeded, service)
    with ExecutingRun(service, run_b) as newest:
        assert newest.state == "running"
        captured = service.runs[run_b].parameters["minimum"]
        assert captured.value == 200
        assert captured.revision == 2
    assert service.publish_run(run_b).outcome == "current"
    # Run A archived its attempt as historical version one, so the
    # current publication of run B is version two of the same source.
    assert service.document["projections"]["sales"]["source"] == {
        "sourceId": FILTERED_SOURCE,
        "version": 2,
    }


# ---------------------------------------------------------------------------
# Row 2: three parameter changes during a run
# ---------------------------------------------------------------------------


def test_three_changes_during_a_run_leave_one_newest_pending_request(
    service_adapter: PlatformAdapter,
):
    seeded = seed_race(service_adapter, script=gated_script())
    service = seeded.service
    run_a = invoke(service, "05-invoke").result["run"]["runId"]

    with ExecutingRun(service, run_a) as executing:
        assert executing.state == "running"
        for index, value in enumerate((100, 200, 300)):
            response = set_parameter(
                service, f"06-set-{index}", value=value, expected_revision=index + 1
            )
            assert response.ok, response.errors
            # Every commit replaced the pending request and restarted its
            # timer; one intent remains (SPEC.md sections 9.3 and 18.3).
            assert len(service.pending_intents) == 1
            assert executing.state == "running"
        assert service.output_groups["sales-results"].generation == 4
        intent = service.pending_intents["sales-results"]
        assert intent.status == "pending"
        assert intent.generation == 4
        assert intent.parameters["minimum"].value == 300
        assert intent.parameters["minimum"].revision == 4

    assert executing.outcome.state == "succeeded"
    assert service.publish_run(run_a).outcome == "superseded"

    # One newest request dispatches with the final captured values.
    run_b = dispatch_newest(seeded, service)
    with ExecutingRun(service, run_b) as newest:
        assert newest.state == "running"
        run = service.runs[run_b]
        assert run.parameters["minimum"].value == 300
        assert run.parameters["minimum"].revision == 4
        assert run.generation == 4
    assert service.publish_run(run_b).outcome == "current"


# ---------------------------------------------------------------------------
# Row 3: a newer run fails
# ---------------------------------------------------------------------------


def test_a_newer_failed_run_keeps_the_last_good_result(
    service_adapter: PlatformAdapter,
):
    script = gated_script(full=True, fail_at_minimum=FAILING_THRESHOLD)
    seeded = seed_race(service_adapter, script=script, full=True)
    service = seeded.service

    # The last good result publishes at minimum zero: three rows.
    good = invoke(service, "05-invoke").result["run"]["runId"]
    with ExecutingRun(service, good) as executing:
        assert executing.state == "running"
    assert executing.outcome.state == "succeeded"
    assert service.publish_run(good).outcome == "current"
    document = service.document
    assert document["sources"][FILTERED_SOURCE]["currentVersion"] == 1
    assert document["sources"][MONTHLY_SOURCE]["currentVersion"] == 1

    # The newer run fails while it holds the newest generation.
    assert set_parameter(service, "06-set", value=300, expected_revision=1).ok
    failed_run = dispatch_newest(seeded, service)
    with ExecutingRun(service, failed_run) as executing:
        assert executing.state == "running"
        assert service.output_groups["sales-results"].generation == 2
    assert executing.outcome.state == "failed"
    assert executing.outcome.exit_code != 0

    # A failed process cannot publish, and the last good pair stays
    # together with its older parameters on display (SPEC.md 13.5).
    refused = service.publish_run(failed_run)
    assert refused.outcome == "not-publishable"
    assert refused.refusal[0].code == "E_CONFLICT"
    document = service.document
    assert document["sources"][FILTERED_SOURCE]["currentVersion"] == 1
    assert document["sources"][MONTHLY_SOURCE]["currentVersion"] == 1
    assert document["projections"]["sales"]["source"]["version"] == 1
    assert document["projections"]["revenue"]["source"]["version"] == 1
    status = status_of(service)
    assert status["status"] == "error"
    assert status["reason"] == "the newest run failed"
    assert status["resultIsStale"] is True
    assert status["result"]["capturedParameters"]["minimum"]["value"] == 0
    assert service.parameter_state["minimum"].value == 300


# ---------------------------------------------------------------------------
# Row 4: action definition changes during execution
# ---------------------------------------------------------------------------


def test_an_action_change_during_execution_archives_the_old_output(
    service_adapter: PlatformAdapter,
):
    seeded = seed_race(
        service_adapter, script=gated_script(full=True), full=True
    )
    service = seeded.service
    good = invoke(service, "05-invoke").result["run"]["runId"]
    with ExecutingRun(service, good) as executing:
        assert executing.state == "running"
    assert service.publish_run(good).outcome == "current"

    assert set_parameter(service, "06-set", value=300, expected_revision=1).ok
    changed = dispatch_newest(seeded, service)
    with ExecutingRun(service, changed) as executing:
        assert executing.state == "running"
        assert service.runs[changed].actionRevision == 1
        # An equivalent replacement still invalidates the admitted run
        # (SPEC.md sections 12.3 and 13.3).
        replaced = service.apply(
            apply_envelope(
                "07-replace-action",
                {"actions/analyze-sales": 1},
                [
                    {
                        "op": "entity.replace",
                        "collection": "actions",
                        "id": "analyze-sales",
                        "value": sales_action(
                            seeded.registration_id,
                            outputs={"filtered": "table", "monthly": "image"},
                            targets={"filtered": "sales", "monthly": "revenue"},
                        ),
                    }
                ],
            )
        )
        assert replaced.ok, replaced.errors
        assert service.entity_revisions["actions/analyze-sales"] == 2
        # The change invalidated eligibility, not the run record: the
        # process keeps executing and reports its result honestly.
        assert executing.state == "running"

    assert executing.outcome.state == "succeeded"
    outcome = service.publish_run(changed)
    assert outcome.outcome == "superseded"
    assert "moved to revision 2" in outcome.reason
    document = service.document
    # The old output archived as history while the previous pair stayed
    # current together (SPEC.md sections 13.3 and 13.4).
    assert document["projections"]["sales"]["source"]["version"] == 1
    assert document["projections"]["revenue"]["source"]["version"] == 1
    assert document["sources"][FILTERED_SOURCE]["currentVersion"] == 1
    assert document["sources"][FILTERED_SOURCE]["versions"]["2"]["origin"] == {
        "kind": "run",
        "runId": changed,
        "output": "filtered",
    }
    status = status_of(service)
    assert status["status"] == "stale"
    assert status["result"]["capturedParameters"]["minimum"]["value"] == 0

    # A new invocation must capture the new revision; nothing reruns on
    # its own (SPEC.md section 18.3).
    assert service.pending_intents["sales-results"].status == "dispatched"
    assert set(service.runs) == {good, changed}
    response = invoke_current(service, "08-invoke-again")
    assert response.ok, response.errors
    assert response.result["intent"]["actionRevision"] == 2


# ---------------------------------------------------------------------------
# Row 5: the target pane is hidden
# ---------------------------------------------------------------------------


def test_a_hidden_target_pane_still_receives_its_publication(
    service_adapter: PlatformAdapter,
):
    seeded = seed_race(
        service_adapter, script=gated_script(full=True), full=True
    )
    service = seeded.service
    show_both_panes(seeded, service)
    assert set_parameter(service, "05-set", value=200, expected_revision=1).ok
    run_id = dispatch_newest(seeded, service)

    with ExecutingRun(service, run_id) as executing:
        assert executing.state == "running"
        hidden = service.apply(
            apply_envelope(
                "06-hide-plot",
                {"layout": revision_of(service, "layout")},
                [
                    {
                        "op": "layout.replace",
                        "value": {
                            "root": {"kind": "pane", "projection": "sales"},
                            "hidden": ["revenue"],
                        },
                    }
                ],
            )
        )
        assert hidden.ok, hidden.errors
        assert executing.state == "running"

    assert executing.outcome.state == "succeeded"
    assert service.publish_run(run_id).outcome == "current"
    document = service.document
    # Hidden projections keep their state and receive results, and the
    # publication changed no layout (SPEC.md sections 10 and 18.3).
    assert document["layout"]["hidden"] == ["revenue"]
    assert document["layout"]["root"] == {"kind": "pane", "projection": "sales"}
    assert document["projections"]["revenue"]["source"] == {
        "sourceId": MONTHLY_SOURCE,
        "version": 1,
    }
    assert document["projections"]["sales"]["source"]["version"] == 1
    assert service.output_groups["sales-results"].revision == 1
    assert status_of(service)["status"] == "current"


# ---------------------------------------------------------------------------
# Row 6: the target pane is deleted
# ---------------------------------------------------------------------------


def test_a_deleted_target_pane_rejects_the_entire_publication(
    service_adapter: PlatformAdapter,
):
    seeded = seed_race(
        service_adapter, script=gated_script(full=True), full=True
    )
    service = seeded.service
    show_both_panes(seeded, service)
    good = invoke(service, "05-invoke").result["run"]["runId"]
    with ExecutingRun(service, good) as executing:
        assert executing.state == "running"
    assert service.publish_run(good).outcome == "current"

    assert set_parameter(service, "06-set", value=300, expected_revision=1).ok
    changed = dispatch_newest(seeded, service)
    with ExecutingRun(service, changed) as executing:
        assert executing.state == "running"
        # Deleting a target pane rides with the action redefinition that
        # keeps the candidate compilable (SPEC.md sections 12.2, 12.3).
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
        deleted = service.apply(
            apply_envelope(
                "08-delete-pane",
                {
                    "actions/analyze-sales": 1,
                    "projections/revenue": revision_of(
                        service, "projections/revenue"
                    ),
                    "layout": revision_of(service, "layout"),
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
        assert deleted.ok, deleted.errors
        assert "revenue" not in service.document["projections"]
        # Removing a target projection invalidates the whole output
        # group publication (SPEC.md section 12.3).
        assert service.output_groups["sales-results"].revision == 2
        assert executing.state == "running"

    assert executing.outcome.state == "succeeded"
    outcome = service.publish_run(changed)
    assert outcome.outcome == "superseded"
    document = service.document
    # The entire publication was refused: not even the surviving table
    # target moved, and the previous pair stays together (SPEC.md 18.3).
    assert "revenue" not in document["projections"]
    assert document["projections"]["sales"]["source"]["version"] == 1
    assert document["sources"][FILTERED_SOURCE]["currentVersion"] == 1
    assert document["sources"][MONTHLY_SOURCE]["currentVersion"] == 1
    status = status_of(service)
    assert status["status"] == "stale"
    assert status["result"]["capturedParameters"]["minimum"]["value"] == 0


# ---------------------------------------------------------------------------
# Row 7: layout or title changes
# ---------------------------------------------------------------------------


def test_layout_and_title_changes_keep_the_publication_valid(
    service_adapter: PlatformAdapter,
):
    seeded = seed_race(
        service_adapter, script=gated_script(full=True), full=True
    )
    service = seeded.service
    show_both_panes(seeded, service)
    assert set_parameter(service, "05-set", value=200, expected_revision=1).ok
    run_id = dispatch_newest(seeded, service)

    with ExecutingRun(service, run_id) as executing:
        assert executing.state == "running"
        moved = service.apply(
            apply_envelope(
                "06-retitle-and-move",
                {
                    "projections/sales": revision_of(service, "projections/sales"),
                    "layout": revision_of(service, "layout"),
                },
                [
                    {
                        "op": "entity.replace",
                        "collection": "projections",
                        "id": "sales",
                        "value": dict(SALES_PROJECTION, title="Revenue ledger"),
                    },
                    {
                        "op": "layout.replace",
                        "value": {
                            "root": {
                                "kind": "split",
                                "axis": "vertical",
                                "ratio": 0.75,
                                "first": {
                                    "kind": "pane",
                                    "projection": "sales"
                                },
                                "second": {
                                    "kind": "pane",
                                    "projection": "revenue"
                                },
                            },
                            "hidden": [],
                        },
                    },
                ],
            )
        )
        assert moved.ok, moved.errors
        assert executing.state == "running"

    assert executing.outcome.state == "succeeded"
    assert service.publish_run(run_id).outcome == "current"
    document = service.document
    # A title or layout change never invalidates a result, and the
    # publication preserved both exactly as committed mid-execution
    # (SPEC.md sections 10 and 13.3).
    assert document["projections"]["sales"]["title"] == "Revenue ledger"
    assert document["layout"]["root"]["axis"] == "vertical"
    assert document["layout"]["root"]["ratio"] == 0.75
    assert document["projections"]["sales"]["source"]["version"] == 1
    assert document["projections"]["revenue"]["source"]["version"] == 1
    assert service.output_groups["sales-results"].revision == 1
    assert status_of(service)["status"] == "current"


# ---------------------------------------------------------------------------
# Row 8: a parameter changes and changes back
# ---------------------------------------------------------------------------


def test_a_parameter_round_trip_still_prevents_old_publication(
    service_adapter: PlatformAdapter,
):
    seeded = seed_race(service_adapter, script=gated_script())
    service = seeded.service
    run_a = invoke(service, "05-invoke").result["run"]["runId"]
    assert service.runs[run_a].parameters["minimum"].value == 0
    assert service.runs[run_a].parameters["minimum"].revision == 1

    with ExecutingRun(service, run_a) as executing:
        assert executing.state == "running"
        assert set_parameter(service, "06-up", value=200, expected_revision=1).ok
        assert set_parameter(service, "07-back", value=0, expected_revision=2).ok
        # The value returned to the captured one; the state revision and
        # the reserved generations did not (SPEC.md sections 6.2, 18.3).
        assert service.parameter_state["minimum"].value == 0
        assert service.parameter_state["minimum"].revision == 3
        intent = service.pending_intents["sales-results"]
        assert intent.parameters["minimum"].value == 0
        assert intent.parameters["minimum"].revision == 3
        assert service.output_groups["sales-results"].generation == 3

    assert executing.outcome.state == "succeeded"
    outcome = service.publish_run(run_a)
    assert outcome.outcome == "superseded"
    assert "reserved generation 3" in outcome.reason
    assert service.document["projections"]["sales"]["source"] is None

    # The newest request carries the same value at the new revision and
    # may publish; equal value never resurrects the older run.
    run_b = dispatch_newest(seeded, service)
    with ExecutingRun(service, run_b) as newest:
        assert newest.state == "running"
        captured = service.runs[run_b].parameters["minimum"]
        assert captured.value == 0
        assert captured.revision == 3
    assert service.publish_run(run_b).outcome == "current"

    # A round trip that never reserves a generation still moves the
    # state revision, which is enough to mark the current result stale.
    programmatic = set_parameter(
        service,
        "08-up-programmatic",
        value=500,
        expected_revision=3,
        origin="programmatic",
    )
    assert programmatic.ok, programmatic.errors
    assert set_parameter(
        service,
        "09-back-programmatic",
        value=0,
        expected_revision=4,
        origin="programmatic",
    ).ok
    assert service.parameter_state["minimum"].value == 0
    assert service.parameter_state["minimum"].revision == 5
    assert service.output_groups["sales-results"].generation == 3
    status = status_of(service)
    assert status["status"] == "stale"
    assert "state revision 5" in status["staleness"]
    assert "the run captured revision 3" in status["staleness"]


# ---------------------------------------------------------------------------
# Row 9: a programmatic parameter update
# ---------------------------------------------------------------------------


def test_a_programmatic_update_saves_and_marks_the_result_stale(
    service_adapter: PlatformAdapter,
):
    seeded = seed_race(service_adapter, script=gated_script())
    service = seeded.service
    run_id = invoke(service, "05-invoke").result["run"]["runId"]
    with ExecutingRun(service, run_id) as executing:
        assert executing.state == "running"
    assert service.publish_run(run_id).outcome == "current"
    assert status_of(service)["status"] == "current"

    sequence = service.commit_sequence
    response = set_parameter(
        service,
        "06-programmatic",
        value=300,
        expected_revision=1,
        origin="programmatic",
    )
    assert response.ok, response.errors
    # The value saved with no user event: one commit, no generation, no
    # intent, no run (SPEC.md sections 9.2 and 18.3).
    assert service.commit_sequence == sequence + 1
    assert service.parameter_state["minimum"].value == 300
    assert service.parameter_state["minimum"].revision == 2
    assert service.output_groups["sales-results"].generation == 1
    assert service.pending_intents["sales-results"].status == "dispatched"
    assert len(service.runs) == 1
    # The affected result is stale and still shows its older parameters.
    status = status_of(service)
    assert status["status"] == "stale"
    assert status["resultIsStale"] is True
    assert "state revision" in status["staleness"]
    assert status["result"]["capturedParameters"]["minimum"]["value"] == 0

    # The same edit as a user change does fire the binding: a reserved
    # generation and one pending intent appear.
    user = set_parameter(
        service, "07-user", value=400, expected_revision=2, origin="user"
    )
    assert user.ok, user.errors
    assert service.output_groups["sales-results"].generation == 2
    assert service.pending_intents["sales-results"].status == "pending"


# ---------------------------------------------------------------------------
# Row 10: a duplicate activation request
# ---------------------------------------------------------------------------


def test_a_duplicate_activation_request_admits_at_most_one_run(
    service_adapter: PlatformAdapter,
):
    seeded = seed_race(service_adapter, script=gated_script())
    service = seeded.service
    first = activate(service, "05-activate")
    assert first.ok, first.errors
    run_id = first.result["run"]["runId"]

    with ExecutingRun(service, run_id) as executing:
        assert executing.state == "running"
        sequence = service.commit_sequence
        # The same activation replayed while the run executes: the
        # stored receipt answers and admits nothing new (SPEC.md 12.6).
        replay = activate(service, "05-activate")
        assert replay.ok, replay.errors
        assert replay.result["run"]["runId"] == run_id
        assert service.commit_sequence == sequence
        assert len(service.runs) == 1
        assert service.output_groups["sales-results"].generation == 1
        assert executing.state == "running"

    assert executing.outcome.state == "succeeded"
    assert service.publish_run(run_id).outcome == "current"


# ---------------------------------------------------------------------------
# Row 11: permission is revoked during execution
# ---------------------------------------------------------------------------


def test_revoked_permission_during_execution_blocks_publication(
    service_adapter: PlatformAdapter,
):
    seeded = seed_race(
        service_adapter, script=gated_script(full=True), full=True
    )
    service = seeded.service
    run_id = invoke(service, "05-invoke").result["run"]["runId"]

    with ExecutingRun(service, run_id) as executing:
        assert executing.state == "running"
        grant_id = service.runs[run_id].permissionDecision
        service.revoke_execution_permission(
            grant_id, revoked_by="host-permission-control/revoke"
        )
        # Revocation blocks publication immediately; the live process
        # keeps its honest state until it finishes (SPEC.md section 13.5).
        assert executing.state == "running"
        decision = service.execution_authority_for_run(run_id, at="publication")
        assert decision.granted is False

    assert executing.outcome.state == "succeeded"
    outcome = service.publish_run(run_id)
    assert outcome.outcome == "rejected"
    assert outcome.refusal[0].code == "E_PERMISSION"
    assert outcome.cancellation_requested is True
    (receipt,) = [
        record
        for record in service.publications.values()
        if record.runId == run_id
    ]
    assert receipt.cancellationRequested is True
    # Nothing became current: no output source was allocated at all.
    document = service.document
    assert FILTERED_SOURCE not in document["sources"]
    assert MONTHLY_SOURCE not in document["sources"]
    assert document["projections"]["sales"]["source"] is None
    assert document["projections"]["revenue"]["source"] is None
    assert service.runs[run_id].disposition == "rejected"
    assert status_of(service)["status"] == "empty"
