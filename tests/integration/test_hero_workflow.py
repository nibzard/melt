"""The hero workflow of SPEC.md section 18.2 (backlog task T019).

One local user drives the six hero steps through real command-line
harness processes on the hero input — rows (2026-01, 100),
(2026-01, 300), and (2026-02, 200):

1. Import the input and create the document without executing it.
2. Approve and run the action; publish both outputs together.
3. Set minimum to 200; verify zero model calls.
4. Move a pane while execution runs; preserve that layout after
   publication.
5. Restart offline; restore values, bindings, sources, and layout
   without execution.
6. Invoke again explicitly and verify the captured input and parameter
   values.

At minimum zero the output table holds three rows; at minimum 200 it
holds two rows and the monthly totals become 300 and 200.

Every harness command runs with fake model credentials in its
environment. The workflow still completes, and no persisted byte under
the data root carries them, because a project process receives a
credential-free environment (SPEC.md sections 2.2, 15.1, and 18.2):
the loop makes zero model calls.

Step 4 runs inside the window today's service offers: the one
execution slot blocks the owning session while a project process runs,
and a restart marks queued work interrupted, so the pane moves after
the run's process completed and staged its outputs but before its
publication — the same execution's results are in flight — and the
moved layout must survive that publication. A mutation concurrent
with a live process needs the non-blocking execution slot of backlog
task T035 and lands with the race suite of T021.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from runtime.storage import storage_worker_available

STORAGE_AVAILABLE, STORAGE_UNAVAILABLE_REASON = storage_worker_available()

requires_storage_worker = pytest.mark.skipif(
    not STORAGE_AVAILABLE, reason=STORAGE_UNAVAILABLE_REASON
)

pytestmark = requires_storage_worker

WORKSPACE = "sales-demo"

CREDENTIAL_MARKER = "hero-zero-model-calls"
"""The substring every fake credential value carries."""

FAKE_MODEL_CREDENTIALS = {
    "ANTHROPIC_API_KEY": f"sk-ant-{CREDENTIAL_MARKER}",
    "OPENAI_API_KEY": f"sk-openai-{CREDENTIAL_MARKER}",
    "GEMINI_API_KEY": f"gemini-{CREDENTIAL_MARKER}",
    "EXAMPLE_PROVIDER_API_KEY": f"suffix-matched-{CREDENTIAL_MARKER}",
}
"""Credentials no step of the hero workflow may need or persist.

The set exercises both the named list and the suffix rule of
``runtime.execution.environment``.
"""

HERO_ROWS = [["2026-01", 100.0], ["2026-01", 300.0], ["2026-02", 200.0]]
"""The hero input of SPEC.md section 18.2, in stored row order."""

MOVED_LAYOUT = {
    "root": {
        "kind": "split",
        "axis": "vertical",
        "ratio": 0.55,
        "first": {"kind": "pane", "projection": "revenue"},
        "second": {"kind": "pane", "projection": "sales"},
    },
    "hidden": [],
}
"""The pane move of step 4: the plot moves above the table."""


def _harness_command(repo_root, data_root, *arguments):
    environment = dict(os.environ)
    environment.update(FAKE_MODEL_CREDENTIALS)
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
    """Run one hero harness command; return its exit code and answer.

    The command inherits the fake model credentials, so every step of
    the workflow proves it runs without a model (SPEC.md section 2.2).
    """

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
    """One isolated hero data root per test."""

    return service_root / f"hero-{uuid.uuid4().hex[:12]}"


def _page(repo_root, data_root, source, *filters):
    """One table page of a source, optionally filtered by month."""

    arguments = ["table", "--source", source]
    for column, value in filters:
        arguments.extend(("--filter", f"{column}:eq:{value}"))
    code, page = run_harness(repo_root, data_root, *arguments)
    assert code == 0, page
    assert page["ok"] is True, page
    return page["result"]


def _monthly_total(repo_root, data_root, month):
    """The published monthly total of one month, read through table.page."""

    rows = _page(
        repo_root, data_root, "analyze-sales-filtered", ("month", month)
    )["rows"]
    assert rows, f"no published row for {month}"
    return sum(row[1] for row in rows if row[0] == month)


def _assert_no_persisted_credential(root: Path) -> None:
    """Fail when any persisted byte under the root carries a credential.

    The database, the artifact store, the run logs, and every staged
    manifest are scanned, so a credential that reached a project
    process or a persisted record cannot hide (SPEC.md section 15.1).
    """

    needle = CREDENTIAL_MARKER.encode("utf-8")
    for path in sorted(root.rglob("*")):
        if path.is_file():
            content = path.read_bytes()
            assert needle not in content, (
                f"model credential material reached {path}"
            )


def test_the_hero_workflow_passes_through_the_harness(repo_root, data_root):
    # ------------------------------------------------------------------
    # Step 1: import the input and create the document without
    # executing it.
    # ------------------------------------------------------------------
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
        "hero-01-create",
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

    # The imported input holds exactly the three hero rows.
    input_page = _page(repo_root, data_root, "sales-input")
    assert input_page["rows"] == HERO_ROWS
    assert input_page["hasMore"] is False

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

    # Nothing executed: no run exists and no result is pending.
    code, runs = run_harness(repo_root, data_root, "runs")
    assert code == 0
    assert runs["runs"] == []
    code, status = run_harness(repo_root, data_root, "status")
    assert code == 0
    assert status["outputGroups"]["sales-results"]["status"] == "empty"
    assert status["outputGroups"]["sales-results"]["generation"] == 0

    # The compiled plan inspects before anything ran.
    code, plan = run_harness(repo_root, data_root, "plan")
    assert code == 0, plan
    assert plan["ok"] is True
    render = plan["plan"]["render"]["projections"]
    assert render["sales"]["renderer"]["id"] == "melt.table"
    assert render["revenue"]["renderer"]["id"] == "melt.plot"
    assert sorted(plan["plan"]["interaction"]["bindings"]) == [
        "manual-run",
        "minimum-rerun",
    ]
    assert plan["plan"]["layout"]["root"]["axis"] == "horizontal"

    # ------------------------------------------------------------------
    # Step 2: approve and run the action; publish both outputs together.
    # ------------------------------------------------------------------
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
    launch = invoked["scheduling"]["launch"]
    assert launch["launched"] is True
    assert launch["state"] == "succeeded"
    assert launch["exitCode"] == 0
    run_a = launch["runId"]
    publication = invoked["scheduling"]["publication"]
    assert publication["outcome"] == "current"
    assert publication["outputs"] == {
        "filtered": {"sourceId": "analyze-sales-filtered", "version": 1},
        "monthly": {"sourceId": "analyze-sales-monthly", "version": 1},
    }
    assert publication["commitSequence"] > 0

    # The run captured the committed invocation: minimum zero and the
    # imported input version.
    code, inspected_a = run_harness(
        repo_root, data_root, "run", "--run-id", run_a
    )
    assert code == 0, inspected_a
    record_a = inspected_a["result"]["run"]
    assert record_a["inputs"]["sales"] == {
        "sourceId": "sales-input",
        "version": 1,
    }
    assert record_a["parameters"]["minimum"]["value"] == 0

    # Both outputs became sources together, with run provenance, and
    # both target projections point at that one publication.
    code, snapshot = run_harness(repo_root, data_root, "inspect")
    assert code == 0, snapshot
    document = snapshot["result"]["document"]
    assert document["sources"]["analyze-sales-filtered"]["currentVersion"] == 1
    assert document["sources"]["analyze-sales-monthly"]["currentVersion"] == 1
    for source_id, output in (
        ("analyze-sales-filtered", "filtered"),
        ("analyze-sales-monthly", "monthly"),
    ):
        version = document["sources"][source_id]["versions"]["1"]
        assert version["origin"] == {
            "kind": "run",
            "runId": run_a,
            "output": output,
        }
    assert document["projections"]["sales"]["source"] == {
        "sourceId": "analyze-sales-filtered",
        "version": 1,
    }
    assert document["projections"]["revenue"]["source"] == {
        "sourceId": "analyze-sales-monthly",
        "version": 1,
    }

    # At minimum zero the output table has three rows.
    assert _page(repo_root, data_root, "analyze-sales-filtered")["rows"] == (
        HERO_ROWS
    )

    # The plot publishes as a real PNG artifact.
    exported = data_root / "hero-monthly.png"
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
    assert artifact["readBytes"] == artifact["byteLength"]
    assert exported.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    # ------------------------------------------------------------------
    # Step 3: set minimum to 200; verify zero model calls.
    # ------------------------------------------------------------------
    code, rerun = run_harness(
        repo_root,
        data_root,
        "set",
        "--parameter",
        "minimum",
        "--value",
        "200",
        "--dispatch",
        "--debounce-wait",
        "400",
        "--launch",
        "--publish",
    )
    assert code == 0, rerun
    assert rerun["ok"] is True
    # The user edit fired the bound action after its debounce and the
    # dispatched intent minted the run.
    dispatched = rerun["scheduling"]["dispatched"]
    assert [item["status"] for item in dispatched] == ["dispatched"]
    run_b = dispatched[0]["run"]["runId"]
    publication_b = rerun["scheduling"]["publication"]
    assert publication_b["outcome"] == "current"
    assert publication_b["outputs"] == {
        "filtered": {"sourceId": "analyze-sales-filtered", "version": 2},
        "monthly": {"sourceId": "analyze-sales-monthly", "version": 2},
    }

    code, inspected_b = run_harness(
        repo_root, data_root, "run", "--run-id", run_b
    )
    assert code == 0, inspected_b
    assert inspected_b["result"]["run"]["parameters"]["minimum"]["value"] == 200

    # At minimum 200 the output table has two rows and the monthly
    # totals become 300 and 200.
    assert _page(repo_root, data_root, "analyze-sales-filtered")["rows"] == [
        ["2026-01", 300.0],
        ["2026-02", 200.0],
    ]
    assert _monthly_total(repo_root, data_root, "2026-01") == 300
    assert _monthly_total(repo_root, data_root, "2026-02") == 200

    # The older run stayed historical with a superseded disposition.
    code, runs = run_harness(repo_root, data_root, "runs")
    assert code == 0
    dispositions = {run["runId"]: run["disposition"] for run in runs["runs"]}
    assert dispositions[run_a] == "superseded"
    assert dispositions[run_b] == "current"

    # Zero model calls: the runs completed although the environment
    # carried fake credentials, and no persisted byte — database,
    # artifacts, logs, staged manifests — carries them.
    _assert_no_persisted_credential(data_root)

    # ------------------------------------------------------------------
    # Step 4: move a pane while execution runs; preserve that layout
    # after publication.
    # ------------------------------------------------------------------
    code, again = run_harness(
        repo_root,
        data_root,
        "set",
        "--parameter",
        "minimum",
        "--value",
        "0",
        "--dispatch",
        "--debounce-wait",
        "400",
        "--launch",
    )
    assert code == 0, again
    assert again["ok"] is True
    run_c = again["scheduling"]["launch"]["runId"]
    assert again["scheduling"]["launch"]["state"] == "succeeded"

    # The execution staged its outputs and its publication is pending;
    # the pane moves in that window of the same execution.
    code, moved = run_harness(
        repo_root,
        data_root,
        "apply",
        "--operations",
        "fixtures/sales/layout-move.json",
    )
    assert code == 0, moved
    assert moved["ok"] is True

    code, published = run_harness(
        repo_root, data_root, "publish", "--run-id", run_c
    )
    assert code == 0, published
    assert published["ok"] is True
    # The layout change never invalidated the publication (SPEC.md
    # section 18.3).
    assert published["outcome"] == "current"
    assert published["outputs"] == {
        "filtered": {"sourceId": "analyze-sales-filtered", "version": 3},
        "monthly": {"sourceId": "analyze-sales-monthly", "version": 3},
    }

    # The moved layout is the saved tree after publication, and the
    # compiled plan reflects it.
    code, snapshot = run_harness(repo_root, data_root, "inspect")
    assert code == 0, snapshot
    assert snapshot["result"]["document"]["layout"] == MOVED_LAYOUT
    code, plan = run_harness(repo_root, data_root, "plan")
    assert code == 0, plan
    assert plan["plan"]["layout"]["root"]["axis"] == "vertical"

    # The newly published table again holds the three rows of minimum
    # zero, so the publication really landed.
    assert _page(repo_root, data_root, "analyze-sales-filtered")["rows"] == (
        HERO_ROWS
    )

    # ------------------------------------------------------------------
    # Step 5: restart offline; restore values, bindings, sources, and
    # layout without execution.
    # ------------------------------------------------------------------
    # One more user edit leaves a pending debounce timer behind; the
    # command's process then exits, closing the service.
    code, pending = run_harness(
        repo_root,
        data_root,
        "set",
        "--parameter",
        "minimum",
        "--value",
        "200",
    )
    assert code == 0, pending
    assert pending["ok"] is True

    # The next command reopens the workspace offline: values, bindings,
    # sources, and layout restore from the durable store.
    code, snapshot = run_harness(repo_root, data_root, "inspect")
    assert code == 0, snapshot
    result = snapshot["result"]
    assert result["parameterStates"]["minimum"] == {
        "value": 200.0,
        "revision": 4,
    }
    assert sorted(result["document"]["bindings"]) == [
        "manual-run",
        "minimum-rerun",
    ]
    assert {
        source_id: source["currentVersion"]
        for source_id, source in result["document"]["sources"].items()
    } == {
        "sales-input": 1,
        "analyze-sales-filtered": 3,
        "analyze-sales-monthly": 3,
    }
    assert result["document"]["layout"] == MOVED_LAYOUT

    # Without execution: the restart interrupted the pending request
    # and executed nothing — no fourth run exists.
    code, runs = run_harness(repo_root, data_root, "runs")
    assert code == 0
    assert [(run["state"], run["disposition"]) for run in runs["runs"]] == [
        ("succeeded", "superseded"),
        ("succeeded", "superseded"),
        ("succeeded", "current"),
    ]
    code, status = run_harness(repo_root, data_root, "status")
    assert code == 0
    entry = status["outputGroups"]["sales-results"]
    assert entry["status"] == "error"
    assert entry["reason"] == "service-restart"
    # The retained result stays readable with the parameters that
    # produced it, and reports its own drift.
    assert entry["resultIsStale"] is True
    assert entry["result"]["capturedParameters"]["minimum"]["value"] == 0
    assert entry["result"]["outputs"] == {
        "filtered": {"sourceId": "analyze-sales-filtered", "version": 3},
        "monthly": {"sourceId": "analyze-sales-monthly", "version": 3},
    }

    # ------------------------------------------------------------------
    # Step 6: invoke again explicitly and verify the captured input and
    # parameter values.
    # ------------------------------------------------------------------
    code, invoked = run_harness(
        repo_root, data_root, "invoke", "--action", "analyze-sales"
    )
    assert code == 0, invoked
    assert invoked["ok"] is True
    assert invoked["result"]["status"] == "granted"
    assert invoked["result"]["origin"] == "user"
    intent = invoked["result"]["intent"]
    assert intent["capturedParameters"]["minimum"] == {
        "parameter": "minimum",
        "value": 200.0,
        "stateRevision": 4,
        "stateIncarnation": 1,
    }
    assert intent["capturedInputs"] == {
        "sales": {"sourceId": "sales-input", "version": 1}
    }
    run_d = invoked["result"]["run"]["runId"]

    # The admitted run record carries the same captured snapshot. It
    # was never executed: reopening the workspace marked it
    # interrupted instead (SPEC.md sections 9.3 and 14).
    code, inspected_d = run_harness(
        repo_root, data_root, "run", "--run-id", run_d
    )
    assert code == 0, inspected_d
    record_d = inspected_d["result"]["run"]
    assert record_d["state"] == "interrupted"
    assert record_d["inputs"]["sales"] == {
        "sourceId": "sales-input",
        "version": 1,
    }
    assert record_d["parameters"]["minimum"]["value"] == 200

    # The whole workflow stays offline: still no persisted credential.
    _assert_no_persisted_credential(data_root)
