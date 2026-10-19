"""Exercise the native host boundary on real durable café state."""
import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from melt.schema.canonical import model_to_wire
from melt.schema.commands import decode_frame, encode_frame, read_frame, MeltCommandError
from runtime.desktop.bridge import DesktopBridge, FRAME_LIMIT, launcher, serve
from runtime.storage import storage_worker_available

AVAILABLE, REASON = storage_worker_available()
pytestmark = pytest.mark.skipif(not AVAILABLE, reason=REASON)
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def cafe_template(tmp_path_factory):
    target = tmp_path_factory.mktemp("cafe-host") / "results with spaces"
    result = subprocess.run([sys.executable, str(REPO / "examples/04-cafe/run.py"), "--output", str(target)],
                            cwd=REPO, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "€3,280.00" in (target / "baseline.md").read_text()
    assert "€2,836.00" in (target / "summary.md").read_text()
    assert "€560.15" in (target / "break_even.md").read_text()
    return target


@pytest.fixture
def cafe_root(cafe_template, tmp_path):
    root = tmp_path / "app data with spaces"
    shutil.copytree(cafe_template / "data", root)
    return root


def open_bridge(root):
    return DesktopBridge(root, "little-hours")


def set_parameter(bridge, identity, value, revision=None):
    if revision is None:
        revision = bridge.view()["snapshot"]["parameterStates"][identity]["revision"]
    return bridge.handle({"operation": "command", "method": "workspace.apply",
        "expected": {f"state/{identity}": revision},
        "operations": [{"op": "parameter.set", "id": identity, "value": value}]})


def activate(bridge):
    revisions = bridge.view()["snapshot"]["entityRevisions"]
    return bridge.handle({"operation": "command", "method": "control.activate",
        "payload": {"projectionId": "summary", "controlId": "run"},
        "expected": {key: revisions[key] for key in ("projections/summary", "bindings/run-cafe")}})


def test_close_and_reopen_restore_views_without_execution(cafe_root):
    bridge = open_bridge(cafe_root)
    try:
        before = bridge.view()
        runs = {key: value.model_dump() for key, value in bridge.session.service.runs.items()}
        assert all(pane["status"] == "ready" for pane in before["panes"].values())
        assert len(before["panes"]["daily"]["table"]["rows"]) == 7
        assert Path(before["panes"]["profit"]["imagePath"]).read_bytes().startswith(b"\x89PNG")
        assert "€2,836.00" in before["panes"]["summary"]["text"]
    finally:
        bridge.close()
    bridge = open_bridge(cafe_root)
    try:
        after = bridge.handle({"operation": "poll"})["view"]
        assert after["panes"] == before["panes"]
        assert after["plan"] == before["plan"]
        assert after["snapshot"]["parameterStates"] == before["snapshot"]["parameterStates"]
        assert {key: value.model_dump() for key, value in bridge.session.service.runs.items()} == runs
        assert launcher(cafe_root, "little-hours") == after["launcher"]
        assert set(after["launcher"]) == {"format", "root", "workspaceId"}
    finally:
        bridge.close()


def test_stale_parameter_edit_keeps_newer_state(cafe_root):
    bridge = open_bridge(cafe_root)
    try:
        revision = bridge.view()["snapshot"]["parameterStates"]["bean_increase"]["revision"]
        assert set_parameter(bridge, "bean_increase", 20, revision)["ok"]
        rejected = set_parameter(bridge, "bean_increase", 40, revision)
        assert rejected["ok"] is False
        assert rejected["view"]["snapshot"]["parameterStates"]["bean_increase"]["value"] == 20
        assert len(bridge.session.service.runs) == 2
        assert "€2,836.00" in rejected["view"]["panes"]["summary"]["text"]
    finally:
        bridge.close()


def test_run_publishes_new_results_and_polls_remain_responsive(cafe_root):
    bridge = open_bridge(cafe_root)
    try:
        set_parameter(bridge, "bean_increase", 0)
        set_parameter(bridge, "open_mondays", True)
        assert activate(bridge)["ok"]
        assert bridge.handle({"operation": "drive"})["ok"]
        assert len(bridge.session.service.runs) == 3
        deadline = time.monotonic() + 30
        while bridge.owned_runs and time.monotonic() < deadline:
            response = bridge.handle({"operation": "poll"})
            assert response["ok"]
            time.sleep(.02)
        assert not bridge.owned_runs
        assert "€3,280.00" in response["view"]["panes"]["summary"]["text"]
        assert all(pane["status"] == "ready" for pane in response["view"]["panes"].values())
    finally:
        bridge.close()


def test_poll_dispatches_a_due_parameter_binding(cafe_root):
    bridge = open_bridge(cafe_root)
    try:
        result = bridge.handle({"operation": "propose", "method": "workspace.apply",
            "expected": {"bindings/auto-cafe": 0}, "operations": [{"op": "entity.create",
            "collection": "bindings", "id": "auto-cafe", "value": {
                "event": {"kind": "parameter.changed", "parameter": "bean_increase"},
                "effect": {"kind": "action.invoke", "action": "analyze-cafe"}, "debounceMs": 20}}]})
        assert result["ok"]
        assert set_parameter(bridge, "bean_increase", 20)["ok"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            response = bridge.handle({"operation": "poll"})
            assert response["ok"]
            if "€3,085.60" in response["view"]["panes"]["summary"]["text"]:
                break
            time.sleep(.02)
        else:
            pytest.fail("The parameter binding did not publish its new result.")
        assert len(bridge.session.service.runs) == 3
    finally:
        bridge.close()


def test_proposal_cannot_grant_execution(cafe_root):
    bridge = open_bridge(cafe_root)
    try:
        service = bridge.session.service
        for grant in list(service.grants):
            service.revoke_execution_permission(grant_id=grant, revoked_by="test:host")
        before = len(service.grants)
        with pytest.raises(ValueError, match="Unsupported desktop command"):
            bridge.handle({"operation": "propose", "method": "permission.grant"})
        assert len(service.grants) == before
        activate(bridge)
        bridge.handle({"operation": "drive"})
        assert len(service.runs) == 2
        registration = bridge.view()["snapshot"]["document"]["actions"]["analyze-cafe"]["implementation"]["registrationId"]
        assert bridge.handle({"operation": "approve", "registrationId": registration})["ok"]
        assert len(service.grants) == before + 1
    finally:
        bridge.close()


def test_framing_rejects_oversized_input_before_reading(cafe_root):
    bridge = open_bridge(cafe_root)
    try:
        reader = io.BytesIO((FRAME_LIMIT+1).to_bytes(4, "big") + b"unread")
        with pytest.raises(MeltCommandError):
            serve(bridge, reader, io.BytesIO())
        assert reader.tell() == 4
        response = io.BytesIO()
        serve(bridge, io.BytesIO(encode_frame(b'{"operation":"snapshot"}')), response)
        payload, end = decode_frame(response.getvalue())
        assert end == len(response.getvalue())
        assert json.loads(payload)["ok"]
    finally:
        bridge.close()


def test_missing_artifact_degrades_one_pane(cafe_root):
    bridge = open_bridge(cafe_root)
    try:
        view = bridge.view()
        Path(view["panes"]["profit"]["imagePath"]).unlink()
        after = bridge.view()
        assert after["panes"]["profit"]["status"] == "unavailable"
        assert after["panes"]["summary"]["status"] == "ready"
        assert len(bridge.session.service.runs) == 2
    finally:
        bridge.close()


def test_actual_child_channel_returns_only_framed_results(cafe_root):
    result = subprocess.run([sys.executable, "-m", "runtime.desktop", "--root", str(cafe_root),
        "--workspace", "little-hours"], cwd=REPO,
        input=encode_frame(b'{"operation":"snapshot"}') + encode_frame(b'{"operation":"unknown"}'),
        capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode()
    first, offset = decode_frame(result.stdout)
    second, end = decode_frame(result.stdout, offset=offset)
    assert end == len(result.stdout)
    assert json.loads(first)["ok"]
    assert json.loads(second)["ok"] is False


def test_child_eof_stops_active_worker_and_reopens_without_replay(cafe_root, tmp_path):
    marker = tmp_path / "worker-pid.txt"
    bridge = open_bridge(cafe_root)
    try:
        service = bridge.session.service
        snapshot = bridge.view()["snapshot"]
        action = dict(snapshot["document"]["actions"]["analyze-cafe"])
        contract = service.action_registry.entry(action["implementation"]["registrationId"]).contract
        script = ("from pathlib import Path\nimport os, time\n"
                  f"Path({str(marker)!r}).write_text(str(os.getpid()))\n"
                  "time.sleep(60)\n" + (REPO / "examples/04-cafe/analyze.py").read_text())
        token = service.capture_artifact_token(script.encode(), "text/x-python")
        registered = bridge.session.request("action.register", payload={"scriptToken": token.token,
            "environmentId": "python-local", "contract": model_to_wire(contract)})
        assert registered["ok"]
        registration = registered["result"]["registrationId"]
        action["implementation"] = {"registrationId": registration}
        result = bridge.handle({"operation": "propose", "method": "workspace.apply",
            "expected": {"actions/analyze-cafe": snapshot["entityRevisions"]["actions/analyze-cafe"]},
            "operations": [{"op": "entity.replace", "collection": "actions", "id": "analyze-cafe", "value": action}]})
        assert result["ok"]
        bridge.handle({"operation": "approve", "registrationId": registration})
        saved = bridge.view()
        revisions = saved["snapshot"]["entityRevisions"]
    finally:
        bridge.close()

    child = subprocess.Popen([sys.executable, "-m", "runtime.desktop", "--root", str(cafe_root),
        "--workspace", "little-hours"], cwd=REPO, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def request(message):
        child.stdin.write(encode_frame(json.dumps(message).encode()))
        child.stdin.flush()
        return json.loads(read_frame(child.stdout.read))

    try:
        activated = request({"operation": "command", "method": "control.activate",
            "payload": {"projectionId": "summary", "controlId": "run"},
            "expected": {key: revisions[key] for key in ("projections/summary", "bindings/run-cafe")}})
        assert activated["ok"]
        assert request({"operation": "drive"})["ok"]
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            assert child.poll() is None
            time.sleep(.02)
        assert marker.exists(), "The owned worker did not start."
        worker_pid = int(marker.read_text())
        assert request({"operation": "snapshot"})["view"]["snapshot"]["activeRuns"]
        # This is the same EOF operation the native window uses on close.
        child.stdin.close()
        assert child.wait(timeout=10) == 0, child.stderr.read().decode()
        with pytest.raises(ProcessLookupError):
            os.kill(worker_pid, 0)
    finally:
        if not child.stdin.closed:
            child.stdin.close()
        if child.poll() is None:
            child.wait(timeout=10)
        child.stdout.close()
        child.stderr.close()

    reopened = open_bridge(cafe_root)
    try:
        restored = reopened.handle({"operation": "poll"})["view"]
        assert restored["panes"] == saved["panes"]
        assert restored["snapshot"]["parameterStates"] == saved["snapshot"]["parameterStates"]
        assert restored["snapshot"]["document"]["layout"] == saved["snapshot"]["document"]["layout"]
        assert len(reopened.session.service.runs) == 3
        latest = max(reopened.session.service.runs.values(), key=lambda run: run.generation)
        assert latest.state == "interrupted"
        assert latest.disposition == "none"
        assert marker.read_text() == str(worker_pid)
    finally:
        reopened.close()


def test_graph_events_use_displayed_revisions_and_update_both_inspectors(tmp_path):
    output = tmp_path / "linked"
    completed = subprocess.run([sys.executable, str(REPO / "examples/03-linked-views/run.py"),
        "--output", str(output)], cwd=REPO, capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stderr
    bridge = DesktopBridge(output / "data", "linked-views")
    try:
        view = bridge.view()
        assert view["panes"]["network"]["status"] == "ready"
        events = view["panes"]["network"]["events"]
        request = {"operation": "command", **events["s-42"]}
        assert bridge.handle(request)["ok"]
        after = bridge.view()
        for identity in ("inspector", "risk-inspector"):
            assert after["panes"][identity]["observer"]["record"]["cells"]["supplier_id"] == "S-42"
        # Another click from the old displayed selection cannot overwrite it.
        assert bridge.handle({"operation": "command", **events["s-73"]})["ok"] is False
        assert not bridge.session.service.runs
    finally:
        bridge.close()
