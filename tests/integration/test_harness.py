"""The Linux command-line harness (T018, SPEC.md sections 2.2 and 18.2).

A local user drives the full loop through real command-line processes:
workspace creation, source import, script registration, definition
apply, parameter edits, action invocation, and inspection of plans,
state, table pages, and PNG artifacts. Every event-producing command
records its event origin, and the user-versus-programmatic split is
exercised on the command surface itself.

Each invocation opens the workspace service, sends its request through
the framed channel the later native host drives, prints one JSON
answer, and closes the service again. The loop therefore also proves
that nothing between two commands needs a live service process: state,
bindings, sources, and layout restore from the durable store.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

import pytest

from runtime.storage import storage_worker_available

STORAGE_AVAILABLE, STORAGE_UNAVAILABLE_REASON = storage_worker_available()

requires_storage_worker = pytest.mark.skipif(
    not STORAGE_AVAILABLE, reason=STORAGE_UNAVAILABLE_REASON
)

pytestmark = requires_storage_worker

WORKSPACE = "sales-demo"


def _harness_command(repo_root, data_root, *arguments):
    environment = dict(os.environ)
    python_path = [str(repo_root / "python"), str(repo_root)]
    if existing := environment.get("PYTHONPATH"):
        python_path.append(existing)
    environment["PYTHONPATH"] = os.pathsep.join(python_path)
    return [
        sys.executable,
        "-m",
        "runtime.harness",
        "--root",
        str(data_root),
        "--workspace",
        WORKSPACE,
        *arguments,
    ]


def run_harness(repo_root, data_root, *arguments):
    """Run one harness command; return its exit code and JSON answer."""

    completed = subprocess.run(
        _harness_command(repo_root, data_root, *arguments),
        capture_output=True,
        text=True,
        cwd=repo_root,
        timeout=120,
    )
    assert completed.stderr == "", completed.stderr
    answer = None
    if completed.stdout.strip():
        answer = json.loads(completed.stdout)
    return completed.returncode, answer


@pytest.fixture
def data_root(service_root):
    """One isolated harness data root per test."""

    return service_root / f"harness-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def sales_workspace(repo_root, data_root):
    """The created, imported, registered, and applied sales demo."""

    code, hello = run_harness(repo_root, data_root, "hello")
    assert code == 0 and hello["ok"] is True
    assert hello["result"]["language"] == "melt/v0.1"

    code, created = run_harness(
        repo_root,
        data_root,
        "create",
        "--document",
        "fixtures/sales/workspace.json",
        "--request-id",
        "01-create",
    )
    assert code == 0, created
    assert created["ok"] is True

    code, imported = run_harness(
        repo_root,
        data_root,
        "import",
        "--source",
        "sales-input",
        "--file",
        "fixtures/sales/sales.parquet",
        "--label",
        "sales.csv",
    )
    assert code == 0, imported
    assert imported["result"]["source"] == {
        "sourceId": "sales-input",
        "version": 1,
    }

    code, registered = run_harness(
        repo_root,
        data_root,
        "register",
        "--script",
        "fixtures/sales/sales-script.py",
        "--contract",
        "fixtures/sales/contract.json",
    )
    assert code == 0, registered
    registration = registered["result"]["registrationId"]

    code, applied = run_harness(
        repo_root,
        data_root,
        "apply",
        "--operations",
        "fixtures/sales/definitions.json",
        "--registration",
        registration,
    )
    assert code == 0, applied
    assert applied["ok"] is True
    return {"registration": registration}


def test_the_full_loop_drives_from_the_command_line(
    repo_root, data_root, sales_workspace
):
    registration = sales_workspace["registration"]

    # A programmatic parameter edit commits without firing the binding.
    code, quiet = run_harness(
        repo_root,
        data_root,
        "set",
        "--parameter",
        "minimum",
        "--value",
        "150",
        "--origin",
        "programmatic",
    )
    assert code == 0, quiet
    assert quiet["ok"] is True
    code, status = run_harness(repo_root, data_root, "status")
    assert code == 0
    # No run was requested: the output group holds no result and no
    # pending intent (SPEC.md sections 9.2 and 13.5).
    assert status["outputGroups"]["sales-results"]["status"] == "empty"
    assert status["outputGroups"]["sales-results"]["generation"] == 0

    # The compiled plan inspects from the command line.
    code, plan = run_harness(repo_root, data_root, "plan")
    assert code == 0, plan
    assert plan["ok"] is True
    render = plan["plan"]["render"]["projections"]
    assert sorted(render) == ["revenue", "sales"]
    assert render["sales"]["renderer"]["id"] == "melt.table"
    assert render["revenue"]["renderer"]["id"] == "melt.plot"
    interaction = plan["plan"]["interaction"]
    assert sorted(interaction["bindings"]) == [
        "manual-run",
        "minimum-rerun",
    ]

    # Approve, invoke, launch, and publish as the local user.
    code, granted = run_harness(
        repo_root, data_root, "approve", "--registration", registration
    )
    assert code == 0, granted
    assert granted["granted"] is True

    code, invoked = run_harness(
        repo_root,
        data_root,
        "invoke",
        "--action",
        "analyze-sales",
        "--launch",
        "--publish",
    )
    assert code == 0, invoked
    assert invoked["ok"] is True
    assert invoked["result"]["origin"] == "user"
    scheduling = invoked["scheduling"]
    assert scheduling["launch"]["launched"] is True
    assert scheduling["launch"]["state"] == "succeeded"
    publication = scheduling["publication"]
    assert publication["outcome"] == "current"
    assert sorted(publication["outputs"]) == ["filtered", "monthly"]

    # The run captured the committed programmatic value.
    run_id = scheduling["launch"]["runId"]
    code, inspected_run = run_harness(
        repo_root, data_root, "run", "--run-id", run_id
    )
    assert code == 0, inspected_run
    captured = inspected_run["result"]["run"]["parameters"]["minimum"]
    assert captured["value"] == 150
    assert inspected_run["result"]["run"]["inputs"]["sales"] == {
        "sourceId": "sales-input",
        "version": 1,
    }

    # Table pages and PNG artifacts inspect from the command line.
    code, page = run_harness(
        repo_root,
        data_root,
        "table",
        "--source",
        "analyze-sales-filtered",
    )
    assert code == 0, page
    assert page["result"]["rows"] == [["2026-01", 300.0], ["2026-02", 200.0]]
    assert page["result"]["hasMore"] is False

    exported = data_root / f"exported-{uuid.uuid4().hex[:8]}.png"
    code, artifact = run_harness(
        repo_root,
        data_root,
        "artifact",
        "--source",
        "analyze-sales-monthly",
        "--output",
        str(exported),
    )
    assert code == 0, artifact
    assert artifact["mediaType"] == "image/png"
    assert artifact["png"]["width"] > 0
    assert artifact["png"]["height"] > 0
    assert exported.is_file()
    assert exported.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    # A user parameter edit fires the bound action after its debounce.
    code, rerun = run_harness(
        repo_root,
        data_root,
        "set",
        "--parameter",
        "minimum",
        "--value",
        "50",
        "--dispatch",
        "--debounce-wait",
        "400",
        "--launch",
        "--publish",
    )
    assert code == 0, rerun
    assert rerun["ok"] is True
    publication = rerun["scheduling"]["publication"]
    assert publication["outcome"] == "current"

    code, page = run_harness(
        repo_root,
        data_root,
        "table",
        "--source",
        "analyze-sales-filtered",
        "--filter",
        "month:eq:2026-01",
    )
    assert code == 0, page
    assert page["result"]["rows"] == [["2026-01", 100.0], ["2026-01", 300.0]]

    # Saved state restores in a fresh command: the value, the binding
    # revisions, the sources, and the layout all survived the process.
    code, snapshot = run_harness(repo_root, data_root, "inspect")
    assert code == 0, snapshot
    result = snapshot["result"]
    assert result["parameterStates"]["minimum"] == {"value": 50.0, "revision": 3}
    assert result["document"]["layout"]["root"]["kind"] == "split"
    assert sorted(result["document"]["sources"]) == [
        "analyze-sales-filtered",
        "analyze-sales-monthly",
        "sales-input",
    ]
    assert sorted(result["document"]["bindings"]) == [
        "manual-run",
        "minimum-rerun",
    ]

    # Ordered changes and stored receipts inspect too.
    code, changes = run_harness(repo_root, data_root, "changes", "--after", "0")
    assert code == 0, changes
    assert changes["ok"] is True
    code, receipt = run_harness(
        repo_root, data_root, "receipt", "--id", "01-create"
    )
    assert code == 0, receipt
    assert receipt["receipt"]["requestId"] == "01-create"


def test_event_origin_is_recorded_on_the_command_surface(
    repo_root, data_root, sales_workspace
):
    registration = sales_workspace["registration"]
    code, granted = run_harness(
        repo_root, data_root, "approve", "--registration", registration
    )
    assert code == 0, granted

    code, activated = run_harness(
        repo_root,
        data_root,
        "activate",
        "--projection",
        "revenue",
        "--control",
        "run",
        "--origin",
        "programmatic",
    )
    assert code == 0, activated
    assert activated["ok"] is True
    # The admitted intent records the programmatic caller (SPEC.md
    # sections 2.2 and 9.1), through the same handlers as the host.
    assert activated["result"]["origin"] == "programmatic"
    assert activated["result"]["intent"]["origin"] == "programmatic"

    code, invoked = run_harness(
        repo_root, data_root, "invoke", "--action", "analyze-sales"
    )
    assert code == 0, invoked
    assert invoked["result"]["origin"] == "user"
    assert invoked["result"]["intent"]["origin"] == "user"


def test_rejected_requests_exit_one_and_operational_failures_exit_two(
    repo_root, data_root, sales_workspace
):
    code, rejected = run_harness(
        repo_root,
        data_root,
        "set",
        "--parameter",
        "ghost",
        "--value",
        "1",
    )
    assert code == 1
    assert rejected["ok"] is False
    assert rejected["errors"][0]["code"] in (
        "E_STATE_MIGRATION",
        "E_REFERENCE",
    )

    code, answer = run_harness(
        repo_root,
        data_root,
        "table",
        "--source",
        "ghost",
    )
    assert code == 1
    assert answer["errors"][0]["code"] == "E_REFERENCE"

    completed = subprocess.run(
        _harness_command(
            repo_root,
            data_root,
            "import",
            "--source",
            "nope",
            "--file",
            "fixtures/sales/absent.parquet",
        ),
        capture_output=True,
        text=True,
        cwd=repo_root,
        timeout=120,
    )
    assert completed.returncode == 2
    assert "no file at" in completed.stderr
