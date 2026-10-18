"""The child service process the recovery suite kills (SPEC.md 18.4).

The fault-injection tests of ``test_restart_recovery.py`` drive one
real service in this separate process, because a kill must take down
the owner lock, the storage worker, and every in-flight transaction
together, the way an operating-system kill of the service process
does. The child seeds the sales workspace through the same command
envelopes a host sends, announces its facts on stdout, and then either
kills itself with ``SIGKILL`` at a named kill point of the publication
or mutation transaction, or waits for the parent to kill it while a
project process executes.

The kill points are patched onto the service objects immediately
before the killed call, never before, so seeding commits normally:

- ``publish-before-finalization``: die before the first immutable
  artifact file is written.
- ``publish-mid-finalization``: die after one of the two output
  artifacts finalized, before the second.
- ``publish-after-finalization``: die after every artifact finalized,
  before the owning transaction commits.
- ``publish-after-commit``: die after the transaction committed,
  before the acknowledgement returns.
- ``apply-before-commit`` and ``apply-after-commit``: the same two
  sides of a ``parameter.set`` mutation.
- ``run-during-execution``: block inside the launch while the fresh
  project process runs, and let the parent deliver the kill.

This module also carries the seeding harness the recovery tests share
(``seed_sales_workspace``, ``run_at_minimum``, ``set_minimum``): the
recovery suite keeps its own copy so importing it never disturbs the
module cache of the integration suite's own test modules.

A child that reaches the end of its scenario prints ``completed`` and
exits zero, which fails the test: the point of every scenario is a
kill.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _entry in (REPO_ROOT, REPO_ROOT / "python"):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from melt.schema.commands import parse_command  # noqa: E402
from runtime.storage import PlatformAdapter  # noqa: E402
from runtime.storage.artifacts import ArtifactStore  # noqa: E402
from runtime.workspace import WorkspaceService, local_environment  # noqa: E402

WORKSPACE = "sales-demo"
PARQUET = "application/vnd.apache.parquet"
PYTHON = "text/x-python"

FILTERED_SOURCE = "analyze-sales-filtered"
MONTHLY_SOURCE = "analyze-sales-monthly"

PUBLISH_POINTS = frozenset(
    {
        "publish-before-finalization",
        "publish-mid-finalization",
        "publish-after-finalization",
        "publish-after-commit",
    }
)
APPLY_POINTS = frozenset({"apply-before-commit", "apply-after-commit"})
ALL_POINTS = PUBLISH_POINTS | APPLY_POINTS | {"run-during-execution"}
SERVICE_CHILD_POINTS_ALL = ALL_POINTS
"""The kill points the child offers, under the name the tests import."""

# The example script of SPEC.md section 13.2, copied word for word.
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

# The same loop without the plot, so scenarios that never touch the
# image output stay quick.
LIGHT_SALES_SCRIPT = (
    "import pandas as pd\n"
    "from melt import ui\n"
    "\n"
    "sales = pd.read_parquet(ui.input_path('sales'))\n"
    "selected = sales[sales['revenue'] >= ui.params['minimum']]\n"
    "ui.output('filtered', selected)\n"
)

# The light loop with one durable external effect: every execution
# appends one line outside the workspace tree. A restart that reran
# uncertain Python effects would grow the file (SPEC.md section 18.4).
# The trailing sleep holds the process open, so the parent can kill
# the service while its project process is genuinely mid-execution.
SIDE_EFFECT_SCRIPT = LIGHT_SALES_SCRIPT + (
    "from pathlib import Path\n"
    "import time\n"
    "with open({side_effect!r}, 'a') as handle:\n"
    "    handle.write('ran\\n')\n"
    "time.sleep(8)\n"
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

    path = REPO_ROOT / "scripts" / "build_fixture_artifacts.py"
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
    """Build one parsed command envelope addressed to the workspace."""

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


def set_minimum(
    service: WorkspaceService,
    request_id: str,
    *,
    value,
    expected_revision: int,
    origin: str = "user",
):
    """Commit one parameter change through the mutation transaction."""

    return service.dispatch_as(
        apply_envelope(
            request_id,
            {f"state/minimum": expected_revision},
            [{"op": "parameter.set", "id": "minimum", "value": value}],
        ),
        origin=origin,
    )


def invoke_action(service: WorkspaceService, request_id: str = "05-invoke"):
    return service.dispatch(
        envelope(
            request_id,
            method="action.invoke",
            expected={"actions/analyze-sales": 1},
            payload={"actionId": "analyze-sales"},
        )
    )


def sales_action(registration_id: str) -> dict:
    return {
        "implementation": {"registrationId": registration_id},
        "parameters": {"minimum": {"parameter": "minimum"}},
        "inputs": {"sales": {"sourceId": "sales-input", "version": 1}},
        "outputs": {"filtered": "table", "monthly": "image"},
        "outputGroup": "sales-results",
        "targets": {"filtered": "sales", "monthly": "revenue"},
    }


def light_action(registration_id: str) -> dict:
    """The same loop mapped onto the table pane alone."""

    return {
        "implementation": {"registrationId": registration_id},
        "parameters": {"minimum": {"parameter": "minimum"}},
        "inputs": {"sales": {"sourceId": "sales-input", "version": 1}},
        "outputs": {"filtered": "table"},
        "outputGroup": "sales-results",
        "targets": {"filtered": "sales"},
    }


def seed_sales_workspace(
    adapter: PlatformAdapter,
    *,
    clock: "FakeClock | None" = None,
    script: str = SPEC_SALES_SCRIPT,
    full: bool = True,
) -> SeededWorkspace:
    """Build the sales workspace of SPEC.md section 16.

    Creation, import, registration, and the definitions apply all ride
    the same command envelopes a host sends, so the recovery scenarios
    exercise the exact service handlers of SPEC.md section 12.6. With
    ``full`` the workspace carries both outputs of the specification
    example; otherwise the light single-output variant, for scenarios
    whose script stages only the table.
    """

    clock = clock or FakeClock()
    service = WorkspaceService.open(adapter, WORKSPACE, clock=clock)
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
                "contract": FULL_CONTRACT if full else LIGHT_CONTRACT,
            },
        )
    )
    assert registered.ok, registered.errors
    registration_id = registered.result["registrationId"]

    projections = [
        {
            "op": "entity.create",
            "collection": "projections",
            "id": "sales",
            "value": SALES_PROJECTION,
        }
    ]
    expectations = {
        "actions/analyze-sales": 0,
        "projections/sales": 0,
        "bindings/minimum-rerun": 0,
        "bindings/manual-run": 0,
        "layout": 1,
    }
    if full:
        projections.append(
            {
                "op": "entity.create",
                "collection": "projections",
                "id": "revenue",
                "value": REVENUE_PROJECTION,
            }
        )
        expectations["projections/revenue"] = 0
        layout = {
            "root": {
                "kind": "split",
                "axis": "horizontal",
                "ratio": 0.55,
                "first": {"kind": "pane", "projection": "sales"},
                "second": {"kind": "pane", "projection": "revenue"},
            },
            "hidden": [],
        }
        action = sales_action(registration_id)
    else:
        layout = {"root": {"kind": "pane", "projection": "sales"}, "hidden": []}
        action = light_action(registration_id)

    definitions = service.apply(
        apply_envelope(
            "04-definitions",
            expectations,
            [
                {
                    "op": "entity.create",
                    "collection": "actions",
                    "id": "analyze-sales",
                    "value": action,
                },
                *projections,
                {
                    "op": "entity.create",
                    "collection": "bindings",
                    "id": "minimum-rerun",
                    "value": {
                        "event": {
                            "kind": "parameter.changed",
                            "parameter": "minimum",
                        },
                        "effect": {
                            "kind": "action.invoke",
                            "action": "analyze-sales",
                        },
                        "debounceMs": 250,
                    },
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
                {"op": "layout.replace", "value": layout},
            ],
        )
    )
    assert definitions.ok, definitions.errors

    service.grant_execution_permission(
        kind="projectExecution",
        registration_id=registration_id,
        granted_by="host-permission-control/run-approval",
    )
    return SeededWorkspace(
        service=service, clock=clock, registration_id=registration_id
    )


def run_at_minimum(
    service: WorkspaceService,
    clock: FakeClock,
    value: int,
    request_id: str,
    expected_revision: int = 1,
) -> str:
    """Drive one bound run at one captured minimum value."""

    response = set_minimum(
        service, request_id, value=value, expected_revision=expected_revision
    )
    assert response.ok, response.errors
    dispatched = service.dispatch_due_intents(clock.advance(300))
    assert [item["status"] for item in dispatched] == ["dispatched"], dispatched
    run_id = dispatched[0]["run"]["runId"]
    outcome = service.launch_admitted_run(run_id)
    assert outcome.launched, outcome.refusal
    assert outcome.state == "succeeded", outcome.refusal
    return run_id


# ---------------------------------------------------------------------------
# The child entry point
# ---------------------------------------------------------------------------


def _emit(**facts) -> None:
    """Announce one JSON fact line the parent test reads."""

    print(json.dumps(facts), flush=True)


def _self_kill() -> None:
    """Die the way an external kill does: SIGKILL, no cleanup."""

    os.kill(os.getpid(), signal.SIGKILL)
    time.sleep(600)  # pragma: no cover - SIGKILL cannot be caught


def _arm_append_records(service: WorkspaceService, mode: str) -> None:
    """Patch the owning store so the next commit dies at `mode`."""

    original = service.store.append_records

    def patched(records):
        if mode == "before":
            _self_kill()
        result = original(records)
        if mode == "after":
            _self_kill()
        return result

    service.store.append_records = patched


def _arm_artifact_write(allowed_writes: int) -> None:
    """Patch artifact finalization so write number N+1 dies first."""

    original = ArtifactStore.write
    calls = {"count": 0}

    def patched(self, data):
        calls["count"] += 1
        if calls["count"] > allowed_writes:
            _self_kill()
        return original(self, data)

    ArtifactStore.write = patched


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="isolated service data root")
    parser.add_argument("--point", required=True, choices=sorted(ALL_POINTS))
    parser.add_argument(
        "--side-effect",
        default="",
        help="path the run-during-execution script appends its effect line to",
    )
    args = parser.parse_args(argv)

    adapter = PlatformAdapter.with_root(Path(args.root))
    clock = FakeClock()
    if args.point in APPLY_POINTS:
        script = LIGHT_SALES_SCRIPT
    elif args.point == "run-during-execution":
        if not args.side_effect:
            parser.error("run-during-execution requires --side-effect")
        script = SIDE_EFFECT_SCRIPT.format(side_effect=args.side_effect)
    else:
        script = SPEC_SALES_SCRIPT
    # The side-effect script stages only the table, so its run needs
    # the light single-output contract to complete cleanly.
    full = args.point != "run-during-execution"
    seeded = seed_sales_workspace(
        adapter, clock=clock, script=script, full=full
    )
    service = seeded.service

    if args.point in APPLY_POINTS:
        # The mutation kill point: one committed parameter change with
        # a fixed request identifier the parent retries after the kill.
        _emit(armed=args.point, requestId="20-set")
        _arm_append_records(
            service, "before" if args.point.endswith("before-commit") else "after"
        )
        response = set_minimum(service, "20-set", value=200, expected_revision=1)
        _emit(completed=True, ok=response.ok)
        return 0

    if args.point == "run-during-execution":
        # Announce the run, then block inside the launch while the
        # fresh project process executes. The parent kills this
        # process mid-execution.
        response = set_minimum(service, "05-set", value=200, expected_revision=1)
        assert response.ok, response.errors
        dispatched = service.dispatch_due_intents(clock.advance(300))
        run_id = dispatched[0]["run"]["runId"]
        _emit(armed=args.point, runId=run_id)
        outcome = service.launch_admitted_run(run_id)
        _emit(completed=True, state=outcome.state)
        return 0

    run_id = run_at_minimum(service, clock, 200, "05-set")
    _emit(armed=args.point, runId=run_id)
    if args.point == "publish-before-finalization":
        _arm_artifact_write(0)
    elif args.point == "publish-mid-finalization":
        _arm_artifact_write(1)
    elif args.point == "publish-after-finalization":
        _arm_append_records(service, "before")
    else:
        _arm_append_records(service, "after")
    outcome = service.publish_run(run_id)
    _emit(completed=True, outcome=outcome.outcome)
    return 0


if __name__ == "__main__":
    sys.exit(main())
