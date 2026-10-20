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
from runtime.desktop.bridge import DesktopBridge, FRAME_LIMIT, _encode_answer, launcher, serve
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


def test_large_document_previews_share_the_response_budget(tmp_path):
    bridge = DesktopBridge(tmp_path, "large-preview")
    try:
        document = {"language": "melt/v0.1", "workspaceId": "large-preview", "title": "Large preview",
            "sources": {}, "parameters": {}, "actions": {}, "projections": {}, "bindings": {},
            "layout": {"root": None, "hidden": []}}
        created = bridge.session.service.create_workspace(document, "create-large-preview")
        assert created.ok, created.errors
        text = ('A😀\n"' * 12_000)[:48_000]
        token = bridge.session.service.capture_artifact_token(text.encode(), "text/plain")
        imported = bridge.session.request("source.import", expected={"sources/text": 0},
            payload={"sourceId": "text", "artifactToken": token.token, "originLabel": "large.txt"})
        assert imported["ok"], imported
        identities = [f"document-{index}" for index in range(32)]
        operations = [{"op": "entity.create", "collection": "projections", "id": identity,
            "value": {"kind": "document", "title": identity,
                "source": {"sourceId": "text", "version": 1}, "config": {"format": "plain"}, "controls": []}}
            for identity in identities]
        operations.append({"op": "layout.replace", "value": {
            "root": {"kind": "pane", "projection": identities[0]}, "hidden": identities[1:]}})
        applied = bridge.session.request("workspace.apply",
            expected={**{f"projections/{identity}": 0 for identity in identities}, "layout": 1}, operations=operations)
        assert applied["ok"], applied
        for operation in ("snapshot", "poll"):
            response = io.BytesIO()
            serve(bridge, io.BytesIO(encode_frame(json.dumps({"operation": operation}).encode())), response)
            payload, end = decode_frame(response.getvalue())
            assert len(payload) <= FRAME_LIMIT
            assert end == len(response.getvalue())
            answer = json.loads(payload)
            assert answer["ok"], answer
            assert answer["view"]["snapshot"] == bridge.view()["snapshot"]
            assert answer["view"]["plan"] == bridge.view()["plan"]
            assert len(answer["view"]["panes"]) == 32
            for pane in answer["view"]["panes"].values():
                assert pane["status"] == "ready"
                assert pane["text"] and text.startswith(pane["text"])
                assert pane["truncated"]
        artifact = bridge.session.service.store.artifacts.read(
            answer["view"]["snapshot"]["document"]["sources"]["text"]["versions"]["1"]["artifact"])
        assert artifact.decode() == text
    finally:
        bridge.close()


def test_large_table_previews_preserve_complete_rows():
    rows = [[str(index), "😀" * 5000] for index in range(64)]
    table = {"columns": ["id", "text"], "rows": rows, "offset": 100, "hasMore": False, "totalRows": 164}
    answer = {"ok": True, "view": {"snapshot": {}, "plan": {},
        "panes": {"table": {"status": "ready", "table": table},
                  "summary": {"status": "ready", "text": "Small summary", "truncated": False}}}}
    frame = _encode_answer(answer)
    payload, end = decode_frame(frame)
    assert end == len(frame)
    assert len(payload) <= FRAME_LIMIT
    panes = json.loads(payload)["view"]["panes"]
    preview = panes["table"]["table"]
    assert 0 < len(preview["rows"]) < 64
    assert preview["rows"] == rows[:len(preview["rows"])]
    assert preview["totalRows"] == 164
    assert preview["nextOffset"] == 100 + len(preview["rows"])
    assert preview["hasMore"]
    assert panes["summary"] == answer["view"]["panes"]["summary"]
    assert table["rows"] == rows and not table["hasMore"]


def install_paged_table(bridge):
    import pyarrow as pa
    import pyarrow.parquet as pq
    sink = pa.BufferOutputStream()
    pq.write_table(pa.table({"index": list(range(130)), "label": [f"Row {i}" for i in range(130)]}), sink)
    service = bridge.session.service
    token = service.capture_artifact_token(sink.getvalue().to_pybytes(), "application/vnd.apache.parquet")
    imported = bridge.session.request("source.import", expected={"sources/paged": 0},
        payload={"sourceId": "paged", "artifactToken": token.token, "originLabel": "paged.parquet"})
    assert imported["ok"], imported
    view = bridge.view()
    definition = {**view["snapshot"]["document"]["projections"]["daily"],
        "source": {"sourceId": "paged", "version": 1}, "config": {"columns": None, "selection": "none"}}
    applied = bridge.session.request("workspace.apply",
        expected={"projections/daily": view["snapshot"]["entityRevisions"]["projections/daily"]},
        operations=[{"op": "entity.replace", "collection": "projections", "id": "daily", "value": definition}])
    assert applied["ok"], applied


def table_query(bridge, **changes):
    view = bridge.view()
    pane = view["panes"]["daily"]
    return {"operation": "table.query", "projectionId": "daily", "source": pane["source"],
        "expectedRevision": pane["viewSettings"]["revision"],
        "expectedProjectionRevision": view["snapshot"]["entityRevisions"]["projections/daily"], **changes}


def test_table_paging_sort_filter_and_restart_are_host_state(cafe_root):
    bridge = open_bridge(cafe_root)
    try:
        install_paged_table(bridge)
        before = bridge.view()
        definitions = before["snapshot"]["document"]
        run_count = len(bridge.session.service.runs)
        assert len(before["panes"]["daily"]["table"]["rows"]) == 64
        stale = table_query(bridge, sort=[])
        response = bridge.handle(table_query(bridge,
            sort=[{"column": "index", "direction": "desc"}],
            filters=[{"column": "index", "operator": "gte", "value": 65}]))
        assert response["ok"], response
        pane = response["view"]["panes"]["daily"]
        assert pane["table"]["rows"][0] == ["129", "Row 129"]
        response = bridge.handle(table_query(bridge, offset=pane["table"]["nextOffset"]))
        assert response["ok"]
        assert response["view"]["panes"]["daily"]["table"]["rows"] == [["65", "Row 65"]]
        assert bridge.handle(stale)["ok"] is False
        assert bridge.view()["snapshot"]["document"] == definitions
        assert len(bridge.session.service.runs) == run_count
    finally:
        bridge.close()
    bridge = open_bridge(cafe_root)
    try:
        pane = bridge.view()["panes"]["daily"]
        assert pane["viewSettings"]["revision"] == 1
        assert pane["table"]["offset"] == 0
        assert pane["table"]["rows"][0][0] == "129"
        assert pane["viewSettings"]["filters"][0]["value"] == 65
    finally:
        bridge.close()


def test_bad_table_filter_does_not_commit_or_lose_the_previous_page(cafe_root):
    bridge = open_bridge(cafe_root)
    try:
        install_paged_table(bridge)
        before = bridge.view()
        sequence = bridge.session.service.commit_sequence
        answer = bridge.handle(table_query(bridge,
            filters=[{"column": "index", "operator": "eq", "value": 1e30}]))
        assert not answer["ok"]
        assert answer["errors"][0]["code"] == "E_TYPE"
        assert bridge.session.service.commit_sequence == sequence
        assert answer["view"]["panes"]["daily"] == before["panes"]["daily"]
    finally:
        bridge.close()


def test_recreating_a_projection_resets_host_settings_atomically(cafe_root):
    bridge = open_bridge(cafe_root)
    try:
        install_paged_table(bridge)
        assert bridge.handle(table_query(bridge, sort=[{"column": "index", "direction": "desc"}]))["ok"]
        view = bridge.view()
        stale = table_query(bridge)
        definition = view["snapshot"]["document"]["projections"]["daily"]
        response = bridge.session.request("workspace.apply",
            expected={"projections/daily": view["snapshot"]["entityRevisions"]["projections/daily"]},
            operations=[{"op": "entity.delete", "collection": "projections", "id": "daily"},
                {"op": "entity.create", "collection": "projections", "id": "daily", "value": definition}])
        assert response["ok"], response
        assert bridge.view()["panes"]["daily"]["viewSettings"]["sort"] == []
        assert bridge.handle(stale)["ok"] is False
    finally:
        bridge.close()
    bridge = open_bridge(cafe_root)
    try:
        assert bridge.view()["panes"]["daily"]["viewSettings"]["sort"] == []
    finally:
        bridge.close()


def test_pinned_panes_and_active_edits_reject_destructive_proposals(cafe_root):
    bridge = open_bridge(cafe_root)
    try:
        view = bridge.view()
        revision = view["snapshot"]["entityRevisions"]["projections/daily"]
        pinned = bridge.handle({"operation": "view.pin", "projectionId": "daily", "pinned": True,
            "expectedRevision": 0, "expectedProjectionRevision": revision})
        assert pinned["ok"]
        sequence = bridge.session.service.commit_sequence
        original = view["snapshot"]["document"]["layout"]
        # Hide a pane in a structurally valid layout, keeping every other pane.
        def remove(node):
            if node["kind"] == "pane":
                return None if node["projection"] == "daily" else node
            first, second = remove(node["first"]), remove(node["second"])
            if first is None:
                return second
            if second is None:
                return first
            return {**node, "first": first, "second": second}
        replacement = {"root": remove(original["root"]), "hidden": ["daily"]}
        rejected = bridge.handle({"operation": "propose", "method": "workspace.preview",
            "expected": {"layout": view["snapshot"]["entityRevisions"]["layout"]},
            "operations": [{"op": "layout.replace", "value": replacement}]})
        assert not rejected["ok"] and rejected["errors"][0]["code"] == "E_CONFLICT"
        assert bridge.handle({"operation": "view.pin", "projectionId": "daily", "pinned": False,
            "expectedRevision": 1, "expectedProjectionRevision": revision})["ok"]
        sequence = bridge.session.service.commit_sequence
        rejected = bridge.handle({"operation": "propose", "method": "workspace.apply",
            "expected": {"projections/daily": revision}, "activeEdits": ["daily"],
            "operations": [{"op": "entity.delete", "collection": "projections", "id": "daily"}]})
        assert not rejected["ok"]
        assert bridge.session.service.commit_sequence == sequence
        assert bridge.handle({"operation": "view.pin", "projectionId": "daily", "pinned": True,
            "expectedRevision": 2, "expectedProjectionRevision": revision})["ok"]
    finally:
        bridge.close()
    bridge = open_bridge(cafe_root)
    try:
        assert bridge.view()["panes"]["daily"]["viewSettings"]["pinned"]
    finally:
        bridge.close()


@pytest.mark.parametrize("key,expected", [("0", True), ("129", True), ("130", False),
    ("01", False), ("1.0", False), ("9223372036854775808", False)])
def test_keyed_selection_uses_approved_artifact_bytes(cafe_root, key, expected):
    bridge = open_bridge(cafe_root)
    try:
        install_paged_table(bridge)
        version = bridge.view()["snapshot"]["document"]["sources"]["paged"]["versions"]["1"]
        assert bridge._has_row_key(version, "index", key) is expected
        assert bridge._has_row_key(version, "index", key) is expected  # Cached immutable result.
    finally:
        bridge.close()
