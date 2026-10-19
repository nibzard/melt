"""Private framed host channel. Project stdout never reaches this channel."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import uuid

from melt.schema.canonical import model_to_wire, parse_json, sha256_hash
from melt.schema.commands import encode_frame, read_frame
from runtime.harness.cli import HarnessSession, _command_plan
from runtime.harness.test_renderer import TestRenderer

FRAME_LIMIT = 1_048_576
RENDERERS = {"melt.table": 1, "melt.plot": 1, "melt.document": 1,
             "melt.graph": 1, "melt.record": 1}


def launcher(root: Path, workspace: str) -> dict:
    """A local reference, with no code, credentials, grants, or data copy."""
    return {"format": "melt-launcher/v1", "root": str(root.resolve()), "workspaceId": workspace}


class DesktopBridge:
    def __init__(self, root: Path, workspace: str):
        self.root, self.workspace = root.resolve(), workspace
        self.session = HarnessSession(argparse.Namespace(root=self.root, workspace=workspace))
        self.owned_runs: set[str] = set()
        hello = self.session.request("session.hello", payload={"protocolVersions": [1],
            "languageVersions": ["melt/v0.1", "melt/semantic-draft-1"], "clientVersion": "melt-native/0.1"})
        if not hello.get("ok"):
            self.close()
            raise ValueError(str(hello.get("errors")))

    def close(self):
        self.session.close()

    def _poll(self):
        service = self.session.service
        service.wait_for_execution(timeout=0)
        for run_id in list(self.owned_runs):
            run = service.runs[run_id]
            if run.state in {"succeeded", "failed", "cancelled", "interrupted", "unknown"}:
                if run.state == "succeeded":
                    service.publish_run(run_id)
                self.owned_runs.remove(run_id)
        self._drive()

    def _drive(self):
        service = self.session.service
        service.dispatch_due_intents()
        if service.execution_run_id is None:
            queued = service.queued_run_ids()
            if queued:
                outcome = service.launch_admitted_run(queued[0], wait=False)
                if outcome.launched:
                    self.owned_runs.add(outcome.run_id)

    def view(self) -> dict:
        snapshot = self.session.snapshot()
        document = snapshot.get("document") or {}
        planned = _command_plan(self.session, argparse.Namespace())
        plan = planned.get("plan")
        panes = {}
        for identity, projection in document.get("projections", {}).items():
            try:
                panes[identity] = self._pane(identity, projection, snapshot)
            except (OSError, ValueError, KeyError, RuntimeError) as error:
                panes[identity] = {"status": "unavailable", "message": str(error)}
        return {"snapshot": snapshot, "plan": plan, "panes": panes,
                "planErrors": planned.get("errors", []), "launcher": launcher(self.root, self.workspace)}

    def _pane(self, identity, projection, snapshot):
        service = self.session.service
        document = snapshot["document"]
        renderer = projection.get("renderer", {})
        kind = projection["kind"]
        source = projection.get("source")
        status = None
        if source and "actionOutput" in source:
            source = None
            # Resolve outputs through inspection's service-owned mapping.
            for action in (snapshot.get("semantic", {}).get("outputs") or {}).values():
                for output in action["outputs"].values():
                    for subscriber in output["subscribers"]:
                        if subscriber["projection"] == identity:
                            status = subscriber
                            source = subscriber.get("source")
        pane = {"status": "ready", "source": source, "subscription": status}
        if kind == "record":
            pane["observer"] = snapshot.get("semantic", {}).get("observers", {}).get(identity)
            return pane
        if not source:
            return {**pane, "status": "empty", "message": "Run the action to create a result."}
        version = document["sources"][source["sourceId"]]["versions"][str(source["version"])]
        if kind == "table":
            answer = self.session.request("table.page", payload={"source": source,
                "sort": [], "filters": [], "offset": 0, "limit": 64})
            if not answer.get("ok"):
                return {**pane, "status": "unavailable", "message": str(answer.get("errors"))}
            page = answer["result"]
            columns = projection["config"].get("columns")
            if columns is not None:
                indices = [page["columns"].index(column) for column in columns]
                page["rows"] = [[row[index] for index in indices] for row in page["rows"]]
                page["columns"] = columns
            pane["table"] = page
            return pane
        payload = service.store.artifacts.read(version["artifact"])
        if sha256_hash(payload) != version["artifact"]:
            raise ValueError("The artifact hash does not match its saved reference.")
        if kind == "plot":
            pane["imagePath"] = str(service.store.artifacts.path_for(version["artifact"]))
            pane["imageSchema"] = version["schema"]
        elif kind == "document":
            text = payload.decode("utf-8")
            pane["text"] = text[:48_000]
            pane["truncated"] = len(text) > 48_000
        elif kind == "graph":
            pane["graph"] = json.loads(payload)
            pane["observer"] = snapshot.get("semantic", {}).get("observers", {}).get(identity)
            # Capture guards for the displayed nodes, never rebuild on click.
            events = {}
            host = TestRenderer(self.session, installed_renderers=RENDERERS)
            for node in version["schema"].get("nodes", []):
                method, body, expected = host.event_request(projection=identity,
                    event="object.select", key=node["key"])
                events[node["nodeId"]] = {"method": method, "payload": body, "expected": expected}
            pane["events"] = events
        else:
            pane.update(status="unavailable", message=f"Unsupported renderer: {renderer or kind}")
        return pane

    def handle(self, request: dict) -> dict:
        if not isinstance(request, dict):
            raise ValueError("A host request must be an object.")
        operation = request.get("operation")
        result = None
        if operation in {"snapshot", "poll"}:
            if operation == "poll":
                self._poll()
        elif operation in {"command", "propose"}:
            # This surface cannot reach the host permission method.
            method = request["method"]
            if method not in {"workspace.apply", "workspace.preview", "control.activate",
                              "action.invoke", "object.select", "selection.clear", "run.cancel"}:
                raise ValueError("Unsupported desktop command.")
            result = self.session.request(method, payload=request.get("payload"),
                operations=request.get("operations"), expected=request.get("expected"),
                origin="programmatic" if operation == "propose" else "user",
                request_id=request.get("requestId") or uuid.uuid4().hex)
            if not result.get("ok"):
                return {"ok": False, "errors": result.get("errors"), "view": self.view()}
        elif operation == "approve":
            # Only a host-generated permission control sends this message.
            result = model_to_wire(self.session.service.grant_execution_permission(
                kind="projectExecution", registration_id=request["registrationId"],
                granted_by="native:explicit-permission-control"))
        elif operation == "drive":
            self._drive()
        else:
            raise ValueError("Unsupported host operation.")
        return {"ok": True, "result": result, "view": self.view()}


def serve(bridge: DesktopBridge, reader, writer):
    while True:
        first = reader.read(1)
        if not first:
            return
        # read_frame requires an exact-read callable; buffered pipes provide it.
        prefix = first

        def read(count):
            nonlocal prefix
            taken, prefix = prefix, b""
            return taken + reader.read(count-len(taken))

        payload = read_frame(read, limit=FRAME_LIMIT)
        try:
            answer = bridge.handle(parse_json(payload.decode("utf-8")))
            encoded = encode_frame(json.dumps(answer, allow_nan=False).encode(), limit=FRAME_LIMIT)
        except Exception as error:
            encoded = encode_frame(json.dumps({"ok": False, "errors": [{"message": str(error)}]}).encode())
        writer.write(encoded)
        writer.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()
    bridge = DesktopBridge(args.root, args.workspace)
    try:
        serve(bridge, sys.stdin.buffer, sys.stdout.buffer)
    finally:
        bridge.close()


if __name__ == "__main__":
    main()
