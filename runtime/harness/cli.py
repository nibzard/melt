"""The command surface of the Linux harness (SPEC.md sections 2.2, 18.2).

Every command follows the same shape: open the workspace service on
the resolved data root, send one request through the framed channel
(:meth:`WorkspaceService.process_frame`, the entry the native host
drives), print the JSON answer, close the service. Reads and mutations
both ride command envelopes; the few host-side operations no envelope
reaches — capturing artifact tokens, granting execution permission,
driving the execution slot, publishing — call the same service methods
the native host calls.

The harness represents one local user (SPEC.md section 2.2). The
``--origin programmatic`` flag of the event-producing commands routes
the same envelope through the agent-adapter path, so the admitted
intent records a programmatic event origin instead of a user one
(sections 2.2 and 9.1).

Exit codes follow the round-1 convention: 0 for a successful answer,
1 for a rejected request (``ok: false``), and 2 for an operational
failure of the harness itself.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from melt.schema.canonical import model_to_wire
from melt.schema.commands import decode_frame, encode_frame
from runtime.storage import PlatformAdapter
from runtime.workspace import WorkspaceService, local_environment

HARNESS_IDENTIFIER = f"melt-harness/0.1.0"
"""The client version the handshake reports (SPEC.md section 12.6)."""

DEFAULT_WORKSPACE = "sales-demo"

MEDIA_TYPES_BY_SUFFIX = {
    ".parquet": "application/vnd.apache.parquet",
    ".png": "image/png",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".py": "text/x-python",
}

_ORIGINS = ("user", "programmatic")


class HarnessError(RuntimeError):
    """An operational failure of the harness itself."""


def _emit(value: Any) -> None:
    json.dump(
        value,
        sys.stdout,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    )
    sys.stdout.write("\n")


def _read_json(argument: "str | None", what: str) -> Any:
    if argument is None:
        return None
    text = argument if argument.lstrip().startswith(("{", "[")) else Path(
        argument
    ).read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise HarnessError(f"{what} is not valid JSON: {error}") from error


def _parse_value(text: str) -> Any:
    """Parse one command-line value as JSON, or keep it a string.

    Numbers and the literals ``true``, ``false``, and ``null`` parse as
    typed values; quoted strings parse as strings; anything else stays
    the exact text the user typed.
    """

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


class HarnessSession:
    """One open workspace service and its framed request channel."""

    def __init__(self, args: argparse.Namespace):
        root = getattr(args, "root", None)
        platform = (
            PlatformAdapter.with_root(Path(root))
            if root
            else PlatformAdapter.from_environment()
        )
        self.workspace = args.workspace
        self._service = WorkspaceService.open(platform, self.workspace)

    def close(self) -> None:
        self._service.close()

    def __enter__(self) -> "HarnessSession":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    @property
    def service(self) -> WorkspaceService:
        return self._service

    def request(
        self,
        method: str,
        *,
        payload: "dict | None" = None,
        expected: "dict | None" = None,
        operations: "list | None" = None,
        request_id: "str | None" = None,
        origin: str = "user",
    ) -> dict:
        """Send one framed request and return the decoded answer."""

        body: dict[str, Any] = {
            "protocolVersion": 1,
            "workspaceId": self.workspace,
            "requestId": request_id or f"harness-{uuid.uuid4().hex[:12]}",
            "method": method,
            "expected": expected,
        }
        if operations is not None:
            body["operations"] = operations
        else:
            body["payload"] = {} if payload is None else payload
        frame = encode_frame(
            json.dumps(body, allow_nan=False).encode("utf-8")
        )
        answer = self._service.process_frame(frame, origin=origin)
        raw, _ = decode_frame(answer)
        return json.loads(raw.decode("utf-8"))

    def snapshot(self) -> dict:
        """The unfiltered ``workspace.inspect`` snapshot."""

        answer = self.request("workspace.inspect")
        if not answer.get("ok"):
            raise HarnessError(
                "workspace.inspect failed: "
                + json.dumps(answer.get("errors", []))
            )
        return answer["result"]

    def revision_of(self, path: str) -> int:
        """The observed revision of one record path; zero when absent."""

        snapshot = self.snapshot()
        if path.startswith("state/"):
            state = snapshot.get("parameterStates", {}).get(
                path[len("state/") :]
            )
            return 0 if state is None else int(state["revision"])
        return int(snapshot.get("entityRevisions", {}).get(path, 0))


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------


def _command_hello(session: HarnessSession, args: argparse.Namespace) -> dict:
    return session.request(
        "session.hello",
        payload={
            "protocolVersions": [1],
            "languageVersions": ["melt/v0.1"],
            "clientVersion": HARNESS_IDENTIFIER,
        },
        request_id=args.request_id,
    )


def _empty_document(workspace: str, title: str) -> dict:
    return {
        "language": "melt/v0.1",
        "workspaceId": workspace,
        "title": title,
        "sources": {},
        "parameters": {},
        "actions": {},
        "projections": {},
        "bindings": {},
        "layout": {"root": None, "hidden": []},
    }


def _command_create(session: HarnessSession, args: argparse.Namespace) -> dict:
    document = _read_json(args.document, "--document")
    if document is None:
        document = _empty_document(session.workspace, args.title)
    return session.request(
        "workspace.create",
        payload={"document": document},
        request_id=args.request_id,
    )


def _capture(
    session: HarnessSession, path: str, media_type: "str | None"
) -> dict:
    """Capture one file's bytes the way a host import operation does."""

    file = Path(path)
    if not file.is_file():
        raise HarnessError(f"no file at {path}")
    if media_type is None:
        media_type = MEDIA_TYPES_BY_SUFFIX.get(file.suffix.lower())
        if media_type is None:
            raise HarnessError(
                f"cannot infer a media type from {file.suffix!r}; pass"
                " --media-type"
            )
    payload = file.read_bytes()
    captured = session.service.capture_artifact_token(payload, media_type)
    return {
        "token": captured.token,
        "kind": captured.kind,
        "artifact": captured.artifact,
        "byteLength": captured.byteLength,
        "mediaType": captured.mediaType,
    }


def _command_import(session: HarnessSession, args: argparse.Namespace) -> dict:
    captured = _capture(session, args.file, args.media_type)
    if captured["kind"] == "script":
        raise HarnessError(
            "a script capture registers through `register`, not"
            " `import` (SPEC.md section 12.6)"
        )
    label = args.label if args.label else Path(args.file).name
    revision = session.revision_of(f"sources/{args.source}")
    return session.request(
        "source.import",
        expected={f"sources/{args.source}": revision},
        payload={
            "sourceId": args.source,
            "artifactToken": captured["token"],
            "originLabel": label,
        },
        request_id=args.request_id,
    )


def _command_register(session: HarnessSession, args: argparse.Namespace) -> dict:
    captured = _capture(session, args.script, args.media_type or "text/x-python")
    contract = _read_json(args.contract, "--contract")
    if contract is None:
        raise HarnessError("register requires --contract (parameters, inputs, outputs)")
    return session.request(
        "action.register",
        payload={
            "scriptToken": captured["token"],
            "environmentId": args.environment,
            "contract": contract,
        },
        request_id=args.request_id,
    )


def _touched_paths(operations: Sequence[Mapping[str, Any]]) -> list[str]:
    """The record paths one operation list touches (SPEC.md 12.2)."""

    paths: list[str] = []
    for operation in operations:
        name = operation.get("op")
        if name in ("entity.create", "entity.replace", "entity.delete"):
            paths.append(f"{operation.get('collection')}/{operation.get('id')}")
        elif name == "parameter.set":
            paths.append(f"state/{operation.get('id')}")
        elif name == "layout.replace":
            paths.append("layout")
    return paths


def _command_apply(session: HarnessSession, args: argparse.Namespace) -> dict:
    operations = _read_json(args.operations, "--operations")
    if not isinstance(operations, list):
        raise HarnessError("--operations must be a JSON array of operations")
    expected = _read_json(args.expected, "--expected")
    if expected is None:
        expected = {
            path: session.revision_of(path) for path in _touched_paths(operations)
        }
    return session.request(
        "workspace.apply",
        expected=expected,
        operations=_substitute(operations, args),
        request_id=args.request_id,
        origin=args.origin,
    )


def _command_preview(session: HarnessSession, args: argparse.Namespace) -> dict:
    operations = _read_json(args.operations, "--operations")
    if not isinstance(operations, list):
        raise HarnessError("--operations must be a JSON array of operations")
    expected = _read_json(args.expected, "--expected")
    if expected is None:
        expected = {
            path: session.revision_of(path) for path in _touched_paths(operations)
        }
    return session.request(
        "workspace.preview",
        expected=expected,
        operations=_substitute(operations, args),
        request_id=args.request_id,
    )


def _substitute(operations: Any, args: argparse.Namespace) -> Any:
    """Replace ``${registration}`` placeholders in one operations file.

    The definitions a user applies cite the registration the previous
    command minted, so the file carries a placeholder instead of a
    machine-specific identifier.
    """

    registration = getattr(args, "registration", None)
    if registration is None:
        return operations
    text = json.dumps(operations)
    text = text.replace("${registration}", registration)
    return json.loads(text)


def _command_set(session: HarnessSession, args: argparse.Namespace) -> dict:
    path = f"state/{args.parameter}"
    revision = (
        args.if_revision
        if args.if_revision is not None
        else session.revision_of(path)
    )
    answer = session.request(
        "workspace.apply",
        expected={path: revision},
        operations=[
            {"op": "parameter.set", "id": args.parameter, "value": _parse_value(args.value)}
        ],
        request_id=args.request_id,
        origin=args.origin,
    )
    return _drive(session, args, answer)


def _command_invoke(session: HarnessSession, args: argparse.Namespace) -> dict:
    path = f"actions/{args.action}"
    revision = (
        args.if_revision
        if args.if_revision is not None
        else session.revision_of(path)
    )
    answer = session.request(
        "action.invoke",
        expected={path: revision},
        payload={"actionId": args.action},
        request_id=args.request_id,
        origin=args.origin,
    )
    return _drive(session, args, answer)


def _command_activate(session: HarnessSession, args: argparse.Namespace) -> dict:
    snapshot = session.snapshot()
    document = snapshot.get("document", {})
    bindings = sorted(
        binding_id
        for binding_id, binding in document.get("bindings", {}).items()
        if (binding.get("event") or {}).get("kind") == "control.activate"
        and (binding.get("event") or {}).get("projection") == args.projection
        and (binding.get("event") or {}).get("control") == args.control
    )
    revisions = snapshot.get("entityRevisions", {})
    expected = {
        f"projections/{args.projection}": (
            args.if_revision
            if args.if_revision is not None
            else int(revisions.get(f"projections/{args.projection}", 0))
        )
    }
    for binding_id in bindings:
        expected[f"bindings/{binding_id}"] = int(
            revisions.get(f"bindings/{binding_id}", 0)
        )
    answer = session.request(
        "control.activate",
        expected=expected,
        payload={"projectionId": args.projection, "controlId": args.control},
        request_id=args.request_id,
        origin=args.origin,
    )
    return _drive(session, args, answer)


def _command_approve(session: HarnessSession, args: argparse.Namespace) -> dict:
    grant = session.service.grant_execution_permission(
        kind=args.kind,
        registration_id=args.registration,
        granted_by=args.granted_by,
        intent_id=args.intent,
    )
    return _drive(
        session,
        args,
        {"granted": True, "grant": model_to_wire(grant)},
    )


def _command_revoke(session: HarnessSession, args: argparse.Namespace) -> dict:
    revoked = session.service.revoke_execution_permission(
        args.grant, revoked_by=args.revoked_by
    )
    return {"revoked": True, "grant": model_to_wire(revoked)}


def _command_pump(session: HarnessSession, args: argparse.Namespace) -> dict:
    return _drive(session, args, {})


def _command_launch(session: HarnessSession, args: argparse.Namespace) -> dict:
    if args.run_id:
        outcome = session.service.launch_admitted_run(args.run_id)
    else:
        outcome = session.service.launch_next_run()
    if outcome is None:
        return {"ok": True, "launched": False, "reason": "no queued run"}
    return {
        "ok": outcome.launched,
        "runId": outcome.run_id,
        "launched": outcome.launched,
        "state": outcome.state,
        "exitCode": outcome.exit_code,
        "staged": outcome.staged,
        "manifestPath": outcome.manifest_path,
        "stdoutPath": outcome.stdout_path,
        "stderrPath": outcome.stderr_path,
        "refusal": [model_to_wire(item) for item in outcome.refusal or ()],
    }


def _command_publish(session: HarnessSession, args: argparse.Namespace) -> dict:
    outcome = session.service.publish_run(args.run_id)
    return {
        "ok": outcome.outcome in ("current", "superseded"),
        "runId": outcome.run_id,
        "outcome": outcome.outcome,
        "reason": outcome.reason,
        "refusal": [model_to_wire(item) for item in outcome.refusal],
        "outputs": outcome.outputs,
        "cancellationRequested": outcome.cancellation_requested,
        "commitSequence": outcome.commit_sequence,
    }


def _command_cancel(session: HarnessSession, args: argparse.Namespace) -> dict:
    return session.request(
        "run.cancel",
        payload={"runId": args.run_id},
        request_id=args.request_id,
    )


def _command_inspect(session: HarnessSession, args: argparse.Namespace) -> dict:
    payload: dict[str, Any] = {}
    if args.collection:
        payload["collection"] = args.collection
    if args.ids:
        payload["ids"] = [item for item in args.ids.split(",") if item]
    return session.request(
        "workspace.inspect", payload=payload, request_id=args.request_id
    )


def _command_plan(session: HarnessSession, args: argparse.Namespace) -> dict:
    from runtime.compiler import CompileRequest, compile_document

    service = session.service
    state = service.state
    if state.document is None:
        return {"ok": False, "errors": [{"code": "E_REFERENCE", "path": "/workspaceId", "message": f"workspace {session.workspace!r} holds no document"}]}
    result = compile_document(
        CompileRequest(
            document=state.document,
            source_catalog=service.source_catalog,
            action_registry=service.action_registry,
            parameter_states={
                parameter_id: {
                    "value": stored.value,
                    "revision": stored.revision,
                }
                for parameter_id, stored in state.parameter_states.items()
            },
        )
    )
    if not result.ok:
        return {
            "ok": False,
            "errors": [model_to_wire(item) for item in result.diagnostics],
        }
    return {"ok": True, "plan": model_to_wire(result.plan)}


def _parse_filters(items: Sequence[str]) -> list[dict]:
    filters = []
    for item in items:
        parts = item.split(":", 2)
        if len(parts) == 2 and parts[1] == "isNull":
            filters.append({"column": parts[0], "operator": "isNull"})
        elif len(parts) == 3:
            filters.append(
                {
                    "column": parts[0],
                    "operator": parts[1],
                    "value": _parse_value(parts[2]),
                }
            )
        else:
            raise HarnessError(
                f"filter {item!r} must read column:operator:value, or"
                " column:isNull"
            )
    return filters


def _parse_sort(items: Sequence[str]) -> list[dict]:
    keys = []
    for item in items:
        column, separator, direction = item.rpartition(":")
        if not separator or direction not in ("asc", "desc"):
            raise HarnessError(f"sort key {item!r} must read column:asc|desc")
        keys.append({"column": column, "direction": direction})
    return keys


def _current_version(session: HarnessSession, source_id: str) -> int:
    snapshot = session.snapshot()
    source = snapshot.get("document", {}).get("sources", {}).get(source_id)
    if source is None:
        raise HarnessError(f"source {source_id!r} does not exist")
    return int(source["currentVersion"])


def _command_table(session: HarnessSession, args: argparse.Namespace) -> dict:
    version = args.version
    if version is None:
        try:
            version = _current_version(session, args.source)
        except HarnessError:
            # An unknown source still rides the envelope: the service
            # answers the structured reference error (SPEC.md 12.7).
            version = 1
    return session.request(
        "table.page",
        payload={
            "source": {"sourceId": args.source, "version": version},
            "sort": _parse_sort(args.sort),
            "filters": _parse_filters(args.filter),
            "offset": args.offset,
            "limit": args.limit,
        },
        request_id=args.request_id,
    )


def _command_run(session: HarnessSession, args: argparse.Namespace) -> dict:
    return session.request(
        "run.inspect",
        payload={"runId": args.run_id},
        request_id=args.request_id,
    )


def _command_runs(session: HarnessSession, args: argparse.Namespace) -> dict:
    return {
        "runs": [
            {
                "runId": run.runId,
                "actionId": run.actionId,
                "state": run.state,
                "generation": run.generation,
                "outputGroup": run.outputGroup,
                "disposition": run.disposition,
                "createdAt": run.createdAt,
                "updatedAt": run.updatedAt,
            }
            for run in sorted(
                session.service.runs.values(), key=lambda item: item.createdAt
            )
        ]
    }


def _command_status(session: HarnessSession, args: argparse.Namespace) -> dict:
    return {"outputGroups": session.service.output_group_status()}


def _command_changes(session: HarnessSession, args: argparse.Namespace) -> dict:
    return session.request(
        "workspace.subscribe",
        payload={
            "afterSequence": args.after,
            "epoch": session.snapshot()["epoch"],
        },
        request_id=args.request_id,
    )


def _command_receipt(session: HarnessSession, args: argparse.Namespace) -> dict:
    return {"receipt": session.service.receipt(args.id)}


def _command_environments(session: HarnessSession, args: argparse.Namespace) -> dict:
    return {
        "environments": [
            model_to_wire(entry)
            for entry in session.service.known_environments().values()
        ]
    }


def _png_dimensions(payload: bytes) -> "tuple[int, int] | None":
    if len(payload) < 24 or payload[12:16] != b"IHDR":
        return None
    return (
        int.from_bytes(payload[16:20], "big"),
        int.from_bytes(payload[20:24], "big"),
    )


def _command_artifact(session: HarnessSession, args: argparse.Namespace) -> dict:
    version_number = (
        args.version
        if args.version is not None
        else _current_version(session, args.source)
    )
    snapshot = session.snapshot()
    version = (
        snapshot.get("document", {})
        .get("sources", {})
        .get(args.source, {})
        .get("versions", {})
        .get(str(version_number))
    )
    if version is None:
        raise HarnessError(
            f"source {args.source!r} holds no version {version_number}"
        )
    payload = session.service.store.artifacts.read(version["artifact"])
    from melt.schema.canonical import sha256_hash

    digest = sha256_hash(payload)
    if digest != version["artifact"]:
        raise HarnessError(
            f"stored artifact bytes hash to {digest}, not to the"
            f" recorded reference {version['artifact']}"
        )
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    report: dict[str, Any] = {
        "source": {"sourceId": args.source, "version": version_number},
        "artifact": version["artifact"],
        "mediaType": version["mediaType"],
        "byteLength": version["byteLength"],
        "schema": version["schema"],
        "origin": version["origin"],
        "readBytes": len(payload),
        "exportedTo": str(Path(args.output).resolve()) if args.output else None,
    }
    dimensions = _png_dimensions(payload)
    if dimensions is not None:
        report["png"] = {"width": dimensions[0], "height": dimensions[1]}
    return report


def _drive(session: HarnessSession, args: argparse.Namespace, answer: dict) -> dict:
    """Advance scheduling inside this session when the caller asks.

    A restart marks queued runs and running timers interrupted rather
    than executing them (SPEC.md sections 9.3 and 14), so admission and
    execution belong to one service session. ``--dispatch`` fires the
    due debounce timers after ``--debounce-wait`` milliseconds,
    ``--launch`` drives the one execution slot, and ``--publish``
    publishes the launched run when it succeeded. The scheduling report
    rides the answer of the command that triggered it.
    """

    import time

    wants_dispatch = getattr(args, "dispatch", False)
    wants_launch = getattr(args, "launch", False)
    wants_publish = getattr(args, "publish", False)
    if not (wants_dispatch or wants_launch or wants_publish):
        return answer
    scheduling: dict[str, Any] = {}
    if wants_dispatch or wants_launch or wants_publish:
        wait_ms = int(getattr(args, "debounce_wait", 0) or 0)
        if wait_ms:
            time.sleep(wait_ms / 1000)
        scheduling["dispatched"] = session.service.dispatch_due_intents()
    if wants_launch or wants_publish:
        outcome = session.service.launch_next_run()
        if outcome is None:
            scheduling["launch"] = {"launched": False, "reason": "no queued run"}
        else:
            scheduling["launch"] = {
                "runId": outcome.run_id,
                "launched": outcome.launched,
                "state": outcome.state,
                "exitCode": outcome.exit_code,
                "refusal": [
                    model_to_wire(item) for item in outcome.refusal or ()
                ],
            }
            if wants_publish and outcome.launched and outcome.state == "succeeded":
                publication = session.service.publish_run(outcome.run_id)
                scheduling["publication"] = {
                    "runId": publication.run_id,
                    "outcome": publication.outcome,
                    "reason": publication.reason,
                    "refusal": [
                        model_to_wire(item) for item in publication.refusal
                    ],
                    "outputs": publication.outputs,
                    "commitSequence": publication.commit_sequence,
                }
    answer["scheduling"] = scheduling
    return answer


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m runtime.harness",
        description=(
            "Drive one Melt workspace from the command line as the local"
            " user (SPEC.md sections 2.2 and 18.2)."
        ),
    )
    parser.add_argument(
        "--root",
        help=(
            "application data root that holds melt/workspaces (default:"
            " $XDG_DATA_HOME or $HOME/.local/share)"
        ),
    )
    parser.add_argument(
        "--workspace",
        default=DEFAULT_WORKSPACE,
        help=f"workspace identifier (default: {DEFAULT_WORKSPACE})",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        command = commands.add_parser(name, help=help_text)
        command.add_argument(
            "--request-id",
            help="explicit request identifier (default: a fresh harness ID)",
        )
        return command

    def add_origin(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--origin",
            choices=_ORIGINS,
            default="user",
            help="event origin of this change (default: user)",
        )

    def add_if_revision(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--if-revision",
            type=int,
            help="expected revision override (default: observed revision)",
        )

    def _add_scheduling_flags(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--dispatch",
            action="store_true",
            help="fire due debounce timers after the change",
        )
        command.add_argument(
            "--debounce-wait",
            type=int,
            default=0,
            metavar="MS",
            help="wait this many milliseconds before dispatching (default: 0)",
        )
        command.add_argument(
            "--launch",
            action="store_true",
            help="drive the execution slot after dispatch",
        )
        command.add_argument(
            "--publish",
            action="store_true",
            help="publish the launched run when it succeeded",
        )

    add("hello", "negotiate protocol and language versions").set_defaults(
        run=_command_hello
    )

    create = add("create", "create the workspace with one document")
    create.add_argument("--document", help="JSON document file (default: an empty workspace)")
    create.add_argument("--title", default="Melt workspace")
    create.set_defaults(run=_command_create)

    import_command = add("import", "import one file as a new source version")
    import_command.add_argument("--source", required=True)
    import_command.add_argument("--file", required=True)
    import_command.add_argument("--label", help="import provenance label (default: file name)")
    import_command.add_argument("--media-type", help="override the inferred media type")
    import_command.set_defaults(run=_command_import)

    register = add("register", "register one captured script")
    register.add_argument("--script", required=True)
    register.add_argument("--contract", required=True, help="JSON contract file")
    register.add_argument(
        "--environment",
        default=local_environment().environment_id,
        help="environment identifier (default: the local interpreter)",
    )
    register.add_argument("--media-type", help=argparse.SUPPRESS)
    register.set_defaults(run=_command_register)

    apply_command = add("apply", "apply one mutation transaction")
    apply_command.add_argument("--operations", required=True, help="JSON operation array or file")
    apply_command.add_argument(
        "--expected", help="JSON expected-revision map or file (default: observed revisions)"
    )
    apply_command.add_argument(
        "--registration",
        help="replace ${registration} in the operations with this identifier",
    )
    add_origin(apply_command)
    apply_command.set_defaults(run=_command_apply)

    preview = add("preview", "dry-run one mutation transaction")
    preview.add_argument("--operations", required=True, help="JSON operation array or file")
    preview.add_argument("--expected", help="JSON expected-revision map or file")
    preview.add_argument("--registration", help=argparse.SUPPRESS)
    preview.set_defaults(run=_command_preview)

    set_command = add("set", "set one parameter value")
    set_command.add_argument("--parameter", required=True)
    set_command.add_argument("--value", required=True)
    add_if_revision(set_command)
    add_origin(set_command)
    _add_scheduling_flags(set_command)
    set_command.set_defaults(run=_command_set)

    invoke = add("invoke", "invoke one action manually")
    invoke.add_argument("--action", required=True)
    add_if_revision(invoke)
    add_origin(invoke)
    _add_scheduling_flags(invoke)
    invoke.set_defaults(run=_command_invoke)

    activate = add("activate", "activate one button control")
    activate.add_argument("--projection", required=True)
    activate.add_argument("--control", required=True)
    activate.add_argument("--if-revision", type=int)
    add_origin(activate)
    _add_scheduling_flags(activate)
    activate.set_defaults(run=_command_activate)

    approve = add("approve", "grant execution permission (host control)")
    approve.add_argument("--registration", required=True)
    approve.add_argument("--kind", choices=("projectExecution", "specificRun"), default="projectExecution")
    approve.add_argument(
        "--granted-by",
        default="harness-permission-control/approve",
        help="identity of the host permission control",
    )
    approve.add_argument("--intent", help="intent pin of a specificRun grant")
    _add_scheduling_flags(approve)
    approve.set_defaults(run=_command_approve)

    revoke = add("revoke", "revoke one execution grant")
    revoke.add_argument("--grant", required=True)
    revoke.add_argument(
        "--revoked-by",
        default="harness-permission-control/revoke",
        help="identity of the host permission control",
    )
    revoke.set_defaults(run=_command_revoke)

    pump = add("pump", "dispatch due timers and optionally drive execution")
    _add_scheduling_flags(pump)
    pump.set_defaults(run=_command_pump)

    launch = add("launch", "launch the queued run in the execution slot")
    launch.add_argument(
        "--run-id", help="run identifier (default: the oldest queued run)"
    )
    launch.set_defaults(run=_command_launch)

    publish = add("publish", "publish one completed run")
    publish.add_argument("--run-id", required=True)
    publish.set_defaults(run=_command_publish)

    cancel = add("cancel", "cancel one run")
    cancel.add_argument("--run-id", required=True)
    cancel.set_defaults(run=_command_cancel)

    inspect = add("inspect", "inspect the committed snapshot")
    inspect.add_argument("--collection", help="bound the answer to one collection")
    inspect.add_argument("--ids", help="comma-separated entity identifiers")
    inspect.set_defaults(run=_command_inspect)

    add("plan", "compile the committed document and print the plan").set_defaults(
        run=_command_plan
    )

    table = add("table", "read one bounded table page")
    table.add_argument("--source", required=True)
    table.add_argument("--version", type=int, help="source version (default: current)")
    table.add_argument("--offset", type=int, default=0)
    table.add_argument("--limit", type=int, default=50)
    table.add_argument("--sort", action="append", default=[], help="column:asc|desc")
    table.add_argument(
        "--filter",
        action="append",
        default=[],
        help="column:operator:value (or column:isNull)",
    )
    table.set_defaults(run=_command_table)

    run_command = add("run", "inspect one run")
    run_command.add_argument("--run-id", required=True)
    run_command.set_defaults(run=_command_run)

    add("runs", "list every run record").set_defaults(run=_command_runs)
    add("status", "report every output group status").set_defaults(run=_command_status)

    changes = add("changes", "subscribe to ordered changes")
    changes.add_argument("--after", type=int, default=0)
    changes.set_defaults(run=_command_changes)

    receipt = add("receipt", "read one stored request receipt")
    receipt.add_argument("--id", required=True)
    receipt.set_defaults(run=_command_receipt)

    add("environments", "list the known script environments").set_defaults(
        run=_command_environments
    )

    artifact = add("artifact", "inspect and export one source artifact")
    artifact.add_argument("--source", required=True)
    artifact.add_argument("--version", type=int, help="source version (default: current)")
    artifact.add_argument("--output", help="write the bytes to this file")
    artifact.set_defaults(run=_command_artifact)

    return parser


def main(argv: "Sequence[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        with HarnessSession(args) as session:
            answer = args.run(session, args)
    except HarnessError as error:
        print(f"harness: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # operational failure of the harness run
        print(f"harness: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    _emit(answer)
    if isinstance(answer, Mapping) and answer.get("ok") is False:
        return 1
    return 0


__all__ = ["HarnessError", "HarnessSession", "build_parser", "main"]
