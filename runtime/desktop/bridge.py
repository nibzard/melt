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
        self.table_windows: dict[str, dict] = {}
        self.selection_checks: dict[tuple, bool] = {}
        hello = self.session.request("session.hello", payload={"protocolVersions": [1],
            "languageVersions": ["melt/v0.1", "melt/semantic-draft-1"], "clientVersion": "melt-native/0.1"})
        if not hello.get("ok"):
            self.close()
            raise ValueError(str(hello.get("errors")))

    def close(self):
        self.session.close()

    def _has_row_key(self, version, row_key, key):
        """Validate keyed selection through approved bytes, never display row positions."""
        cache_key = (version["artifact"], row_key, key)
        if cache_key in self.selection_checks:
            return self.selection_checks[cache_key]
        column = next((c for c in version["schema"]["columns"] if c["id"] == row_key), None)
        if column is None:
            return False
        import pyarrow as pa
        import pyarrow.compute as pc
        import pyarrow.parquet as pq
        try:
            if column["type"] == "int64" and str(int(key)) != key:
                return False
            scalar = pa.scalar(int(key) if column["type"] == "int64" else key)
            found = False
            with self.session.service.store.artifacts.open(version["artifact"]) as handle:
                for batch in pq.ParquetFile(handle).iter_batches(batch_size=4096, columns=[column.get("label") or row_key]):
                    if pc.any(pc.equal(batch.column(0), scalar)).as_py():
                        found = True; break
        except (ValueError, OverflowError, pa.ArrowException):
            return False
        if len(self.selection_checks) >= 128:
            self.selection_checks.clear()
        self.selection_checks[cache_key] = found
        return found

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
        pane = {"status": "ready", "source": source, "subscription": status,
                "viewSettings": service.host_view(identity)}
        if kind == "record":
            pane["observer"] = snapshot.get("semantic", {}).get("observers", {}).get(identity)
            return pane
        if not source:
            return {**pane, "status": "empty", "message": "Run the action to create a result."}
        version = document["sources"][source["sourceId"]]["versions"][str(source["version"])]
        if kind == "table":
            settings = service.host_view(identity)
            schema = version["schema"]
            available = {column["id"]: column for column in schema["columns"]}
            from runtime.workspace.reader import PageQuery, validate_query
            sort = [key for key in settings["sort"] if key["column"] in available]
            filters = [item for item in settings["filters"]
                if validate_query(schema, PageQuery(source, filters=[item]))[1] is None]
            window = self.table_windows.get(identity, {})
            offset = window.get("offset", 0) if window.get("source") == source else 0
            self.table_windows[identity] = {"source": source, "offset": offset}
            answer = self.session.request("table.page", payload={"source": source,
                "sort": sort, "filters": filters, "offset": offset, "limit": 64})
            if not answer.get("ok"):
                return {**pane, "status": "unavailable", "message": str(answer.get("errors"))}
            page = answer["result"]
            row_key = schema.get("rowKey")
            selected = settings.get("selectedKey")
            selection_mode = projection["config"].get("selection", "none")
            if (selection_mode != "single" or settings.get("selectionSource") != source or
                settings.get("rowKey") != row_key or
                selected is not None and not self._has_row_key(version, row_key, selected)):
                selected = None
            page["rowKeys"] = ([row[page["columns"].index(row_key)] for row in page["rows"]] if row_key else [])
            page["selectedKey"] = selected
            page["selectionMode"] = selection_mode
            columns = projection["config"].get("columns")
            if columns is not None:
                indices = [page["columns"].index(column) for column in columns]
                page["rows"] = [[row[index] for index in indices] for row in page["rows"]]
                page["columns"] = columns
            pane["table"] = page
            pane["viewSettings"] = {**settings, "sort": sort, "filters": filters}
            pane["columnTypes"] = {key: column["type"] for key, column in available.items()}
            stored = self.session.service.host_view(identity)
            if sort != stored["sort"] or filters != stored["filters"]:
                pane["notice"] = "Removed incompatible sort or filter settings after the source changed."
            return pane
        payload = service.store.artifacts.read(version["artifact"])
        if sha256_hash(payload) != version["artifact"]:
            raise ValueError("The artifact hash does not match its saved reference.")
        if kind == "plot":
            pane["imagePath"] = str(service.store.artifacts.path_for(version["artifact"]))
            pane["imageSchema"] = version["schema"]
            pane["artifact"] = version["artifact"]
            pane["origin"] = version.get("origin")
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
        elif operation == "table.query":
            identity = request["projectionId"]
            displayed = self.view()["panes"].get(identity, {})
            if "table" not in displayed:
                raise ValueError("This projection has no readable table")
            settings = displayed["viewSettings"]
            revision = self.session.service.entity_revisions.get(f"projections/{identity}")
            if (request.get("expectedRevision") != settings["revision"]
                or request.get("expectedProjectionRevision") != revision
                or request.get("source") != displayed["source"]):
                return {"ok": False, "errors": [{"code": "E_CONFLICT", "message": "The displayed table changed; refresh and retry."}], "view": self.view()}
            sort, filters = request.get("sort", settings["sort"]), request.get("filters", settings["filters"])
            payload = {"source": displayed["source"], "sort": sort, "filters": filters,
                       "offset": request.get("offset", 0), "limit": 64}
            answer = self.session.request("table.page", payload=payload)
            if not answer.get("ok"):
                return {"ok": False, "errors": answer.get("errors"), "view": self.view()}
            stored = self.session.service.host_view(identity)
            if sort != stored["sort"] or filters != stored["filters"]:
                self.session.service.save_host_view(identity, {"sort": sort, "filters": filters,
                    "pinned": settings["pinned"], "selectionSource": settings.get("selectionSource"),
                    "rowKey": settings.get("rowKey"), "selectedKey": settings.get("selectedKey")}, expected_revision=settings["revision"],
                    expected_projection_revision=revision)
            self.table_windows[identity] = {"source": displayed["source"], "offset": payload["offset"]}
            result = answer["result"]
        elif operation == "table.select":
            identity = request["projectionId"]
            displayed = self.view()["panes"].get(identity, {})
            settings = displayed.get("viewSettings", {})
            revision = self.session.service.entity_revisions.get(f"projections/{identity}")
            if ("table" not in displayed or request.get("source") != displayed["source"]
                or request.get("expectedRevision") != settings.get("revision")
                or request.get("expectedProjectionRevision") != revision):
                return {"ok": False, "errors": [{"code": "E_CONFLICT", "message": "The displayed table changed."}], "view": self.view()}
            source = displayed["source"]
            document = self.session.service.document
            projection = document["projections"][identity]
            version = document["sources"][source["sourceId"]]["versions"][str(source["version"])]
            row_key = version["schema"].get("rowKey")
            key = request.get("key")
            if (projection["config"].get("selection") != "single" or not row_key or
                (key is not None and (not isinstance(key, str) or not self._has_row_key(version, row_key, key)))):
                return {"ok": False, "errors": [{"code": "E_TYPE", "message": "The selection does not name a current row key."}], "view": self.view()}
            result = self.session.service.save_host_view(identity,
                {"sort": settings["sort"], "filters": settings["filters"], "pinned": settings["pinned"],
                 "selectionSource": source if key is not None else None,
                 "rowKey": row_key if key is not None else None, "selectedKey": key},
                expected_revision=settings["revision"], expected_projection_revision=revision)
        elif operation == "view.pin":
            identity = request["projectionId"]
            if not isinstance(request.get("pinned"), bool):
                raise ValueError("Pinning requires a boolean value")
            current = self.session.service.host_view(identity)
            result = self.session.service.save_host_view(identity,
                {"sort": current["sort"], "filters": current["filters"], "pinned": request["pinned"],
                 "selectionSource": current.get("selectionSource"), "rowKey": current.get("rowKey"),
                 "selectedKey": current.get("selectedKey")},
                expected_revision=request["expectedRevision"],
                expected_projection_revision=request["expectedProjectionRevision"])
        elif operation in {"command", "propose"}:
            # This surface cannot reach the host permission method.
            method = request["method"]
            if method not in {"workspace.apply", "workspace.preview", "control.activate",
                              "action.invoke", "object.select", "selection.clear", "run.cancel"}:
                raise ValueError("Unsupported desktop command.")
            if operation == "propose" and method in {"workspace.apply", "workspace.preview"}:
                active = set(request.get("activeEdits", []))
                document = self.session.service.document or {}
                for item in request.get("operations") or []:
                    if not isinstance(item, dict):
                        continue
                    identity = item.get("id")
                    value = item.get("value")
                    value = value if isinstance(value, dict) else {}
                    destructive = item.get("collection") == "projections" and (
                        item.get("op") == "entity.delete" or
                        (item.get("op") == "entity.replace" and
                         value.get("controls") != document.get("projections", {}).get(identity, {}).get("controls")))
                    protected = destructive and identity in document.get("projections", {}) and (identity in active or
                        self.session.service.host_view(identity)["pinned"])
                    hidden = value.get("hidden", []) if item.get("op") == "layout.replace" else []
                    hidden = hidden if isinstance(hidden, list) else []
                    if protected or (item.get("op") == "layout.replace" and active) or any(
                        self.session.service.host_view(id)["pinned"] for id in hidden if id in document.get("projections", {})):
                        return {"ok": False, "errors": [{"code": "E_CONFLICT", "message": "The change affects a pinned pane or an active edit."}], "view": self.view()}
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


def _json_bytes(value) -> bytes:
    return json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _fit_pane(pane: dict, budget: int) -> dict:
    """Limit a preview by its encoded size, preserving saved artifact bytes."""
    if len(_json_bytes(pane)) <= budget:
        return pane
    if "text" in pane or "table" in pane:
        def preview(count):
            if "text" in pane:
                return {**pane, "text": pane["text"][:count], "truncated": True}
            return {**pane, "table": {**pane["table"],
                "rows": pane["table"]["rows"][:count], "hasMore": True,
                "rowKeys": pane["table"].get("rowKeys", [])[:count],
                "nextOffset": pane["table"]["offset"] + count}}

        low, high = 0, len(pane["text"] if "text" in pane else pane["table"]["rows"])
        while low < high:
            middle = (low + high + 1) // 2
            if len(_json_bytes(preview(middle))) <= budget:
                low = middle
            else:
                high = middle - 1
        result = preview(low)
        if len(_json_bytes(result)) <= budget and ("text" in pane or low > 0):
            return result
    return {"status": "unavailable", "message": "This pane preview exceeds the host frame limit. Inspect its saved source with the harness."}


def _encode_answer(answer: dict) -> bytes:
    payload = _json_bytes(answer)
    if len(payload) > FRAME_LIMIT and "view" in answer:
        # Reserve space for the snapshot and plan, then share the remaining
        # bytes across previews. Never truncate the durable workspace state.
        view = answer["view"]
        panes = view["panes"]
        bounded = {**answer, "view": {**view, "panes": {}}}
        remaining = FRAME_LIMIT - len(_json_bytes(bounded))
        for index, (identity, pane) in enumerate(panes.items()):
            overhead = len(_json_bytes(identity)) + 2  # Key, colon, and comma.
            budget = remaining // (len(panes) - index) - overhead
            preview = _fit_pane(pane, budget)
            bounded["view"]["panes"][identity] = preview
            remaining -= len(_json_bytes(preview)) + overhead
        payload = _json_bytes(bounded)
    return encode_frame(payload, limit=FRAME_LIMIT)


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
            encoded = _encode_answer(answer)
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
