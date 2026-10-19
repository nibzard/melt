"""The Linux harness drives the semantic extension (T026 review repair).

The command-line harness must speak the workspace language before it
can create, mutate, or inspect a `melt/semantic-draft-1` workspace:
its ``session.hello`` offers both identifiers, and a semantic workspace
refuses a client that cannot speak its stored language
(RFC 001 section 1; the service-side proof is
``test_hello_negotiates_the_workspace_language``).

Every command runs as one real harness process, so the loop also
proves that nothing between two commands needs a live service process:
the committed selection, the observer views, and the graph source
restore from the durable store (SPEC.md section 2.2).
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

WORKSPACE = "supplier-demo"


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
    return service_root / f"semantic-harness-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def supplier_workspace(repo_root, data_root):
    """The supplier fixture built through harness processes alone."""

    code, hello = run_harness(repo_root, data_root, "hello")
    assert code == 0 and hello["ok"] is True
    # The harness offers both languages, so an empty workspace keeps
    # the stable default until a document decides.
    assert hello["result"]["language"] == "melt/v0.1"

    code, created = run_harness(
        repo_root,
        data_root,
        "create",
        "--language",
        "melt/semantic-draft-1",
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
        "supplier-records",
        "--file",
        "fixtures/semantic/suppliers.parquet",
        "--request-id",
        "02-import-records",
    )
    assert code == 0, imported
    assert imported["result"]["kind"] == "table"

    code, imported = run_harness(
        repo_root,
        data_root,
        "import",
        "--source",
        "supplier-network",
        "--file",
        "fixtures/semantic/supplier-network.graph.json",
        "--request-id",
        "03-import-network",
    )
    assert code == 0, imported
    assert imported["result"]["kind"] == "graph"
    assert imported["result"]["mediaType"] == "application/vnd.melt.graph+json"

    code, applied = run_harness(
        repo_root,
        data_root,
        "apply",
        "--operations",
        "fixtures/semantic/definitions.json",
        "--request-id",
        "04-definitions",
    )
    assert code == 0, applied
    assert applied["ok"] is True

    # The handshake of a semantic workspace answers the stored language.
    code, hello = run_harness(repo_root, data_root, "hello")
    assert code == 0 and hello["ok"] is True
    assert hello["result"]["language"] == "melt/semantic-draft-1"

    # The compiled plan carries the extension sections.
    code, plan = run_harness(repo_root, data_root, "plan")
    assert code == 0 and plan["ok"] is True
    assert sorted(plan["plan"]["interaction"]["eventAddresses"]) == [
        "object.select/network",
        "selection.clear/network",
    ]
    assert plan["plan"]["dependency"]["referenceStates"]["selected_supplier"]
    assert plan["plan"]["identity"]["nodes"]["network/s-42"]["key"] == "S-42"
    return data_root


def test_hello_refuses_a_client_that_speaks_only_v01(repo_root, data_root):
    """A v0.1-only client is refused against a semantic workspace.

    The harness itself offers both identifiers; this test speaks to the
    workspace directly with a one-language hello to pin the handshake
    contract the harness repair satisfies.
    """

    run_harness(repo_root, data_root, "create", "--language", "melt/semantic-draft-1")
    from melt.schema.commands import parse_command
    from runtime.storage import PlatformAdapter
    from runtime.workspace import WorkspaceService

    service = WorkspaceService.open(
        PlatformAdapter.with_root(data_root), WORKSPACE
    )
    try:
        answer = service.dispatch(
            parse_command(
                {
                    "protocolVersion": 1,
                    "workspaceId": WORKSPACE,
                    "requestId": "only-v01",
                    "method": "session.hello",
                    "payload": {
                        "protocolVersions": [1],
                        "languageVersions": ["melt/v0.1"],
                        "clientVersion": "test",
                    },
                }
            )
        )
        assert not answer.ok
        assert answer.errors[0].code == "E_VERSION"
        assert answer.errors[0].path == "/payload/languageVersions"
    finally:
        service.close()


def test_the_harness_selection_loop_runs_offline(
    repo_root, supplier_workspace
):
    root = supplier_workspace

    code, rendered = run_harness(repo_root, root, "render")
    assert code == 0
    assert rendered["selected"]["selected_supplier"]["value"] is None
    inspector = next(
        pane
        for pane in rendered["panes"]
        if pane["projection"] == "inspector"
    )
    assert inspector["display"] == "empty"

    code, selected = run_harness(
        repo_root,
        root,
        "select",
        "--projection",
        "network",
        "--key",
        "S-42",
        "--request-id",
        "05-select",
    )
    assert code == 0, selected
    assert selected["result"]["status"] == "committed"
    assert selected["result"]["stateRevision"] == 2
    assert selected["result"]["value"]["key"] == "S-42"

    # Each command reopened the durable store: the render below proves
    # the committed selection and its views restored without any
    # execution (RFC 001 sections 10.2 and 14).
    code, rendered = run_harness(repo_root, root, "render")
    assert code == 0
    assert rendered["selected"]["selected_supplier"]["revision"] == 2
    network = next(
        pane for pane in rendered["panes"] if pane["projection"] == "network"
    )
    assert network["highlightedNodes"] == ["s-42"]
    assert [node["highlighted"] for node in network["nodes"]] == [True, False]
    inspector = next(
        pane for pane in rendered["panes"] if pane["projection"] == "inspector"
    )
    assert inspector["display"] == "record"
    assert inspector["record"]["cells"] == {
        "supplier_id": "S-42",
        "name": "Alpha Components",
    }

    # The event replays from its receipt when the frame is identical:
    # the guard override pins the expected state revision the original
    # request carried.
    code, replay = run_harness(
        repo_root,
        root,
        "select",
        "--projection",
        "network",
        "--key",
        "S-42",
        "--guard",
        "refstate/selected_supplier=1",
        "--request-id",
        "05-select",
    )
    assert code == 0 and replay["result"] == selected["result"]

    # A reused request id with different content is refused.
    code, reused = run_harness(
        repo_root,
        root,
        "select",
        "--projection",
        "network",
        "--key",
        "S-73",
        "--request-id",
        "05-select",
    )
    assert code == 1
    assert reused["errors"][0]["code"] == "E_REQUEST_REUSE"


def test_clearing_through_the_harness(repo_root, supplier_workspace):
    root = supplier_workspace
    run_harness(repo_root, root, "select", "--projection", "network", "--key", "S-42")
    code, cleared = run_harness(
        repo_root,
        root,
        "clear",
        "--projection",
        "network",
        "--request-id",
        "06-clear",
    )
    assert code == 0, cleared
    assert cleared["result"]["status"] == "committed"
    assert cleared["result"]["value"] is None
    code, rendered = run_harness(repo_root, root, "render")
    assert code == 0
    assert rendered["selected"]["selected_supplier"]["value"] is None
    assert rendered["selected"]["selected_supplier"]["revision"] == 3


def test_a_stale_guard_replays_an_old_gesture_and_is_rejected(
    repo_root, supplier_workspace
):
    root = supplier_workspace
    code, inspected = run_harness(repo_root, root, "inspect")
    assert code == 0
    collection_revision = inspected["result"]["entityRevisions"][
        "collections/suppliers"
    ]
    code, selected = run_harness(
        repo_root, root, "select", "--projection", "network", "--key", "S-42"
    )
    assert code == 0
    code, rejected = run_harness(
        repo_root,
        root,
        "select",
        "--projection",
        "network",
        "--key",
        "S-73",
        "--guard",
        f"collections/suppliers={collection_revision - 1}",
        "--request-id",
        "06-stale",
    )
    assert code == 1, rejected
    assert rejected["ok"] is False
    assert rejected["errors"][0]["code"] == "E_CONFLICT"
    assert (
        rejected["errors"][0]["path"] == "/expected/collections~1suppliers"
    )


def test_the_agent_patch_applies_through_the_harness(
    repo_root, supplier_workspace
):
    root = supplier_workspace
    run_harness(repo_root, root, "select", "--projection", "network", "--key", "S-42")
    code, before = run_harness(repo_root, root, "inspect")
    assert code == 0

    code, patched = run_harness(
        repo_root,
        root,
        "apply",
        "--operations",
        "fixtures/semantic/patches/second-inspector.json",
        "--request-id",
        "07-agent-patch",
    )
    assert code == 0, patched
    assert patched["ok"] is True

    code, after = run_harness(repo_root, root, "inspect")
    assert code == 0
    state_before = before["result"]["semantic"]["referenceStates"]
    state_after = after["result"]["semantic"]["referenceStates"]
    assert state_after == state_before
    assert (
        after["result"]["entityRevisions"]["projections/risk-inspector"] == 1
    )

    code, rendered = run_harness(repo_root, root, "render")
    assert code == 0
    by_id = {pane["projection"]: pane for pane in rendered["panes"]}
    assert by_id["risk-inspector"]["display"] == "record"
    assert by_id["risk-inspector"]["record"]["cells"] == {"supplier_id": "S-42"}
    assert by_id["inspector"]["record"]["cells"] == {
        "supplier_id": "S-42",
        "name": "Alpha Components",
    }
    assert by_id["network"]["highlightedNodes"] == ["s-42"]


def test_a_degraded_host_renders_the_unavailable_fallback(
    repo_root, supplier_workspace
):
    """RFC 001 section 10.5 through harness processes alone.

    A host that ships no `melt.graph` renderer still inspects every
    saved source and the committed semantic state, and its render
    degrades to the fallback pane only. The missing renderer refuses to
    originate a gesture; it never reinterprets the contract.
    """

    root = supplier_workspace
    run_harness(repo_root, root, "select", "--projection", "network", "--key", "S-42")

    # Inspection stays complete through the degraded host: the saved
    # sources, the committed selection with its resolution, the
    # observer views, and the pinned renderer versions.
    code, inspected = run_harness(repo_root, root, "inspect")
    assert code == 0 and inspected["ok"] is True
    result = inspected["result"]
    assert set(result["document"]["sources"]) == {
        "supplier-records",
        "supplier-network",
    }
    semantic = result["semantic"]
    assert semantic["referenceStates"]["selected_supplier"]["value"]["key"] == "S-42"
    assert semantic["referenceStates"]["selected_supplier"]["resolution"] == {
        "status": "resolved"
    }
    assert semantic["renderers"]["network"] == {
        "id": "melt.graph",
        "contractVersion": 1,
    }

    # The render of the host that ships no graph renderer: the graph
    # pane is the unavailable fallback, the inspector keeps resolving,
    # and the semantic state stays visible.
    degraded = [
        "--renderer",
        "melt.table=1",
        "--renderer",
        "melt.plot=1",
        "--renderer",
        "melt.document=1",
        "--renderer",
        "melt.record=1",
    ]
    code, rendered = run_harness(repo_root, root, "render", *degraded)
    assert code == 0
    by_id = {pane["projection"]: pane for pane in rendered["panes"]}
    graph = by_id["network"]
    assert graph["rendererStatus"] == "unavailable"
    assert graph["renderer"] == "melt.graph"
    assert graph["source"] == {"sourceId": "supplier-network", "version": 1}
    assert graph["diagnostics"][0]["code"] == "E_VERSION"
    assert graph["diagnostics"][0]["path"] == "/projections/network/renderer"
    assert by_id["inspector"]["record"]["cells"] == {
        "supplier_id": "S-42",
        "name": "Alpha Components",
    }
    assert rendered["selected"]["selected_supplier"]["value"]["key"] == "S-42"

    # The gesture from the missing renderer is refused before it is
    # built; the harness answers an operational failure (exit 2).
    completed = subprocess.run(
        _harness_command(
            repo_root,
            root,
            "select",
            "--projection",
            "network",
            "--key",
            "S-73",
            *degraded,
        ),
        capture_output=True,
        text=True,
        cwd=repo_root,
        timeout=120,
    )
    assert completed.returncode == 2
    assert "unavailable" in completed.stderr

    # A host that ships the pinned renderer set renders every pane.
    code, rendered = run_harness(
        repo_root, root, "render", *degraded, "--renderer", "melt.graph=1"
    )
    assert code == 0
    by_id = {pane["projection"]: pane for pane in rendered["panes"]}
    assert "rendererStatus" not in by_id["network"]
    assert by_id["network"]["highlightedNodes"] == ["s-42"]
