"""The workspace service: framed commands, transactions, and receipts.

SPEC.md section 12 defines the command contract this service serves:

- Requests arrive as framed JSON on a private interprocess channel: a
  four-byte unsigned big-endian length prefix followed by UTF-8 JSON.
  Frames above the configured limit are rejected before the payload is
  allocated or read (section 12.1). Project stdout is never a control
  channel.
- ``workspace.apply`` plans a mutation transaction against a private
  candidate, validates it once, and commits every change or none
  (section 12.2, :mod:`runtime.workspace.mutations`).
- Each successful mutation stores its request receipt atomically with
  the new snapshot, so a retry with the same identifier and payload
  returns the stored response and reuse with different content fails
  with ``E_REQUEST_REUSE`` (section 12.5).
- Each transaction increments the commit sequence exactly once, and
  every transaction that changed records appends one ordered
  notification guarded by the service epoch.

The service is the sole writer: it holds the workspace ``owner.lock``
through a :class:`~runtime.storage.WorkspaceStore` and commits each
transaction as one atomic append on the owning connection. Because the
store counts one sequence per committed transaction and no other writer
exists, the sequence of the next commit is predicted and verified after
the append.

State restoration on open loads the last committed snapshot, receipts,
and notifications from the record stream without importing or executing
project code (SPEC.md section 14, steps 2 and 3). Backlog task T010
adds the remaining service methods on top of this core.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import ValidationError

from melt.schema.canonical import canonical_hash, model_to_wire, parse_json
from melt.schema.commands import (
    CommandEnvelope,
    CommandResponse,
    MeltCommandError,
    OkResponse,
    RejectedResponse,
    decode_frame,
    encode_frame,
    parse_command,
    parse_response,
)
from melt.schema.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    MeltSchemaError,
    diagnostic_from_pydantic_error,
    sorted_diagnostics,
)
from melt.schema.document import Document, document_semantic_diagnostics
from melt.schema.limits import DEFAULT_LIMITS
from melt.schema.records import RECORD_SCHEMA_VERSION, parse_record
from runtime.compiler.catalog import (
    EMPTY_ACTION_REGISTRY,
    EMPTY_SOURCE_CATALOG,
    ActionRegistry,
    SourceCatalog,
)
from runtime.compiler.compiler import CompileRequest, compile_document
from runtime.storage import PlatformAdapter, WorkspaceStore
from runtime.workspace.mutations import (
    EntityTouch,
    MutationPlan,
    initial_state_from_document,
    plan_mutation,
)
from runtime.workspace.state import (
    LAYOUT_PATH,
    NOTIFICATION_KIND,
    RECEIPT_KIND,
    SNAPSHOT_KIND,
    ParameterStateValue,
    WorkspaceState,
    entity_path,
    pointer_key,
    state_path,
)

SNAPSHOT_REQUIRED = "snapshot-required"
"""The status a subscription returns when ordered changes cannot be sent."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _diagnostic(
    code: str,
    path: str,
    message: str,
    hint: str = "",
) -> Diagnostic:
    return Diagnostic(code=code, path=path, message=message, hint=hint)


def _rejected(request_id: str, *errors: Diagnostic) -> RejectedResponse:
    return RejectedResponse(requestId=request_id, ok=False, errors=errors)


@dataclass(frozen=True)
class NotificationWindow:
    """The answer to one subscription query (SPEC.md section 12.5).

    Ordered changes, or the ``snapshot-required`` status with the reason
    the host must take a full snapshot instead.
    """

    status: str
    changes: tuple[dict, ...] = ()
    reason: str = ""


def _wire_of(value: Any) -> dict:
    return model_to_wire(value) if hasattr(value, "model_dump") else value


class WorkspaceService:
    """One open workspace under single-writer ownership."""

    def __init__(
        self,
        store: WorkspaceStore,
        state: WorkspaceState,
        receipts: dict[str, dict],
        notifications: list[dict],
        *,
        source_catalog: "SourceCatalog | None" = None,
        action_registry: "ActionRegistry | None" = None,
        frame_limit: int | None = None,
    ):
        self._store = store
        self._state = state
        self._receipts = receipts
        self._notifications = notifications
        self._source_catalog = source_catalog or EMPTY_SOURCE_CATALOG
        self._action_registry = action_registry or EMPTY_ACTION_REGISTRY
        self._frame_limit = (
            DEFAULT_LIMITS.control_frame_bytes if frame_limit is None else frame_limit
        )

    # ------------------------------------------------------------------
    # Opening and teardown
    # ------------------------------------------------------------------

    @classmethod
    def open(
        cls,
        platform: PlatformAdapter,
        workspace_id: str,
        *,
        source_catalog: "SourceCatalog | None" = None,
        action_registry: "ActionRegistry | None" = None,
        frame_limit: int | None = None,
        node: str | None = None,
    ) -> "WorkspaceService":
        """Take ownership of one workspace and restore its state."""

        store = WorkspaceStore.open(platform, workspace_id, node=node)
        try:
            state, receipts, notifications = _load_state(store)
        except BaseException:
            store.close()
            raise
        return cls(
            store,
            state,
            receipts,
            notifications,
            source_catalog=source_catalog,
            action_registry=action_registry,
            frame_limit=frame_limit,
        )

    def close(self) -> None:
        """Stop the owning connection and release the lock. Idempotent."""

        self._store.close()

    def __enter__(self) -> "WorkspaceService":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Observed state
    # ------------------------------------------------------------------

    @property
    def workspace_id(self) -> str:
        return self._state.workspace_id

    @property
    def epoch(self) -> int:
        return self._state.epoch

    @property
    def commit_sequence(self) -> int:
        return self._state.commit_sequence

    @property
    def state(self) -> WorkspaceState:
        return self._state

    @property
    def document(self) -> "dict | None":
        """The committed document as a wire tree, or None when absent."""

        return None if self._state.document is None else json.loads(
            json.dumps(self._state.document)
        )

    @property
    def parameter_state(self) -> dict[str, ParameterStateValue]:
        return dict(self._state.parameter_states)

    @property
    def entity_revisions(self) -> dict[str, int]:
        return dict(self._state.entity_revisions)

    @property
    def store(self) -> WorkspaceStore:
        """The owning storage, for recovery and crash tests."""

        return self._store

    def receipt(self, request_id: str) -> "dict | None":
        stored = self._receipts.get(request_id)
        return None if stored is None else dict(stored)

    # ------------------------------------------------------------------
    # Mutations (SPEC.md sections 12.2, 12.3, 12.5)
    # ------------------------------------------------------------------

    def apply(self, envelope: CommandEnvelope) -> CommandResponse:
        """Apply one ``workspace.apply`` transaction."""

        if envelope.workspaceId != self._state.workspace_id:
            return _rejected(
                envelope.requestId,
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    "/workspaceId",
                    f"envelope addresses workspace"
                    f" {envelope.workspaceId!r}, but this service owns"
                    f" {self._state.workspace_id!r}",
                ),
            )
        if envelope.method != "workspace.apply":
            return _rejected(
                envelope.requestId,
                _diagnostic(
                    DiagnosticCode.SCHEMA,
                    "/method",
                    f"method {envelope.method!r} is not implemented by"
                    " this service",
                ),
            )
        payload_hash = canonical_hash(model_to_wire(envelope))
        replay = self._replay_or_reject(envelope.requestId, payload_hash)
        if replay is not None:
            return replay
        plan = plan_mutation(
            self._state,
            envelope,
            source_catalog=self._source_catalog,
            action_registry=self._action_registry,
        )
        if not plan.ok:
            return _rejected(envelope.requestId, *plan.diagnostics)
        return self._commit(
            envelope.requestId, envelope.method, payload_hash, plan
        )

    def create_workspace(
        self, document: "dict | Document", request_id: str
    ) -> CommandResponse:
        """Create the workspace from one document (SPEC.md section 12.6).

        Creation is valid only for an absent workspace whose document
        carries empty ``sources`` and ``actions`` collections, so
        creation stays separate from artifact import and execution
        registration. Every entity starts at revision 1 and every
        parameter stores its default at state revision 1.
        """

        wire = _wire_of(document)
        payload_hash = canonical_hash(
            {
                "protocolVersion": 1,
                "workspaceId": self._state.workspace_id,
                "requestId": request_id,
                "method": "workspace.create",
                "payload": {"document": wire},
            }
        )
        replay = self._replay_or_reject(request_id, payload_hash)
        if replay is not None:
            return replay
        if self._state.present:
            return _rejected(
                request_id,
                _diagnostic(
                    DiagnosticCode.CONFLICT,
                    "/workspaceId",
                    f"workspace {self._state.workspace_id!r} already"
                    " exists; workspace.create is valid only for an"
                    " absent workspace",
                ),
            )
        try:
            parsed = Document.model_validate(wire)
        except ValidationError as error:
            return _rejected(
                request_id,
                *sorted_diagnostics(
                    [
                        diagnostic_from_pydantic_error(
                            item, DiagnosticCode.SCHEMA
                        )
                        for item in error.errors()
                    ]
                ),
            )
        diagnostics: list[Diagnostic] = []
        if parsed.workspaceId != self._state.workspace_id:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    "/workspaceId",
                    f"document names workspace {parsed.workspaceId!r},"
                    f" but this service owns"
                    f" {self._state.workspace_id!r}",
                )
            )
        if parsed.sources:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.SCHEMA,
                    "/sources",
                    "workspace.create requires empty sources; import"
                    " artifacts through source.import",
                )
            )
        if parsed.actions:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.SCHEMA,
                    "/actions",
                    "workspace.create requires empty actions; register"
                    " scripts through action.register",
                )
            )
        diagnostics.extend(document_semantic_diagnostics(parsed))
        result = compile_document(
            CompileRequest(
                document=parsed,
                source_catalog=self._source_catalog,
                action_registry=self._action_registry,
                parameter_states={
                    parameter_id: {"value": parameter.default, "revision": 1}
                    for parameter_id, parameter in parsed.parameters.items()
                },
            )
        )
        if not result.ok:
            diagnostics.extend(result.diagnostics)
        if diagnostics:
            return _rejected(request_id, *sorted_diagnostics(diagnostics))

        touches: dict[str, EntityTouch] = {}
        for collection, entities in (
            ("parameters", parsed.parameters),
            ("actions", parsed.actions),
            ("projections", parsed.projections),
            ("bindings", parsed.bindings),
        ):
            for entity_id in entities:
                touches[entity_path(collection, entity_id)] = EntityTouch(
                    first_operation=0,
                    last_operation=0,
                    existed_before=False,
                    exists_after=True,
                    created=True,
                )
        touches[LAYOUT_PATH] = EntityTouch(
            first_operation=0,
            last_operation=0,
            existed_before=False,
            exists_after=True,
            created=True,
        )
        plan = MutationPlan(
            candidate=model_to_wire(parsed),
            document_hash=result.plan.documentHash if result.plan else None,
            entity_touches=touches,
            new_states=initial_state_from_document(parsed),
            incarnation_bumps={
                parameter_id: 1 for parameter_id in parsed.parameters
            },
            observed_revisions={},
        )
        return self._commit(request_id, "workspace.create", payload_hash, plan)

    def _replay_or_reject(
        self, request_id: str, payload_hash: str
    ) -> "CommandResponse | None":
        """Return the stored response, a reuse rejection, or None."""

        stored = self._receipts.get(request_id)
        if stored is None:
            return None
        if stored.get("requestPayloadHash") != payload_hash:
            return _rejected(
                request_id,
                _diagnostic(
                    DiagnosticCode.REQUEST_REUSE,
                    "/requestId",
                    f"request id {request_id!r} was already used with"
                    " different content",
                    hint="Send the original request unchanged to replay"
                    " its stored response, or use a new request id.",
                ),
            )
        return parse_response(stored["response"])

    def _commit(
        self,
        request_id: str,
        method: str,
        payload_hash: str,
        plan: MutationPlan,
    ) -> CommandResponse:
        """Commit one planned transaction atomically or not at all."""

        # The compiler read set is checked again at commit (SPEC.md 12.2):
        # an intervening mutation to any read record conflicts without
        # applying any operation.
        for path, observed in sorted(plan.observed_revisions.items()):
            current = self._state.current_revision(path)
            if current != observed:
                return _rejected(
                    request_id,
                    _diagnostic(
                        DiagnosticCode.CONFLICT,
                        f"/expected/{pointer_key(path)}",
                        f"record {path!r} changed from revision"
                        f" {observed} to {current} while the transaction"
                        " was planned",
                    ),
                )

        changed = plan.changed_records()
        predicted = self._state.commit_sequence + 1
        new_revisions = plan.new_revisions(self._state)
        for parameter_id, state_value in plan.new_states.items():
            new_revisions[state_path(parameter_id)] = state_value.revision
        response = OkResponse(
            requestId=request_id,
            ok=True,
            commitSequence=predicted,
            result={
                "changedRecords": changed,
                "revisions": dict(sorted(new_revisions.items())),
            },
        )

        entity_revisions = dict(self._state.entity_revisions)
        for path in plan.entity_touches:
            if path in new_revisions:
                entity_revisions[path] = new_revisions[path]
            else:
                entity_revisions.pop(path, None)
        parameter_states = {
            parameter_id: state
            for parameter_id, state in self._state.parameter_states.items()
            if parameter_id not in plan.removed_states
        }
        parameter_states.update(plan.new_states)
        incarnations = dict(self._state.incarnations)
        incarnations.update(plan.incarnation_bumps)
        new_state = WorkspaceState(
            workspace_id=self._state.workspace_id,
            epoch=self._state.epoch,
            commit_sequence=predicted,
            # Store the document in its model-normalized wire form, the
            # same form a reload reproduces, so the canonical document
            # hash stays stable across restarts (SPEC.md section 4.2).
            document=model_to_wire(Document.model_validate(plan.candidate)),
            entity_revisions=entity_revisions,
            parameter_states=parameter_states,
            incarnations=incarnations,
        )

        records: list[tuple[str, dict]] = [
            (SNAPSHOT_KIND, _snapshot_wire(new_state, self._state.commit_sequence)),
            (
                RECEIPT_KIND,
                {
                    "recordType": "commandReceipt",
                    "schemaVersion": RECORD_SCHEMA_VERSION,
                    "requestId": request_id,
                    "method": method,
                    "requestPayloadHash": payload_hash,
                    "response": model_to_wire(response),
                    "commitSequence": predicted,
                    "receivedAt": _utc_now(),
                },
            ),
        ]
        if changed:
            records.append(
                (
                    NOTIFICATION_KIND,
                    {
                        "recordType": "changeNotification",
                        "schemaVersion": RECORD_SCHEMA_VERSION,
                        "workspaceId": new_state.workspace_id,
                        "epoch": new_state.epoch,
                        "commitSequence": predicted,
                        "changedRecords": changed,
                    },
                )
            )
        for kind, data in records:
            parse_record(data)  # refuse to commit a record we would reject

        sequence, _ = self._store.append_records(records)
        if sequence != predicted:
            raise RuntimeError(
                f"commit landed at sequence {sequence} instead of the"
                f" predicted {predicted}; the single-writer sequence"
                " prediction is broken"
            )
        self._state = new_state
        self._receipts[request_id] = dict(records[1][1])
        if changed:
            self._notifications.append(dict(records[-1][1]))
        return response

    # ------------------------------------------------------------------
    # Notifications (SPEC.md section 12.5)
    # ------------------------------------------------------------------

    def changes_after(self, after_sequence: int, epoch: int) -> NotificationWindow:
        """Return the ordered changes after one sequence, or require a
        snapshot.

        A changed epoch or a gap in the notification log means ordered
        replay is impossible; the host must take a full snapshot.
        """

        if epoch != self._state.epoch:
            return NotificationWindow(
                SNAPSHOT_REQUIRED,
                (),
                f"service epoch {self._state.epoch} differs from the"
                f" subscribed epoch {epoch}",
            )
        if after_sequence > self._state.commit_sequence:
            return NotificationWindow(
                SNAPSHOT_REQUIRED,
                (),
                f"after sequence {after_sequence} is beyond the commit"
                f" sequence {self._state.commit_sequence}",
            )
        selected = [
            notification
            for notification in self._notifications
            if notification["commitSequence"] > after_sequence
        ]
        expected = after_sequence + 1
        for notification in selected:
            if notification["commitSequence"] != expected:
                return NotificationWindow(
                    SNAPSHOT_REQUIRED,
                    (),
                    f"notification log has no entry for sequence"
                    f" {expected}; a transaction changed nothing or its"
                    " notification was lost",
                )
            expected += 1
        return NotificationWindow("changes", tuple(selected))

    @property
    def notifications(self) -> tuple[dict, ...]:
        """Every retained notification, oldest first."""

        return tuple(self._notifications)

    # ------------------------------------------------------------------
    # Framed channel (SPEC.md section 12.1)
    # ------------------------------------------------------------------

    def handle_request(self, payload: bytes) -> "CommandResponse | dict":
        """Parse one request payload and return its response.

        A payload that does not decode into an envelope has no request
        identifier, so the transport-level rejection is a bare error
        object rather than a :class:`RejectedResponse`.
        """

        try:
            envelope = parse_command(payload)
        except MeltCommandError as error:
            request_id = _salvage_request_id(payload)
            if request_id is None:
                return {"ok": False, "errors": model_to_wire_diagnostics(error)}
            return _rejected(request_id, *error.diagnostics)
        return self.apply(envelope)

    def process_frame(self, data: bytes) -> bytes:
        """Answer one framed request with one framed response."""

        try:
            payload = decode_frame(data, limit=self._frame_limit)
        except MeltCommandError as error:
            return _encode_json(
                {"ok": False, "errors": model_to_wire_diagnostics(error)}
            )
        response = self.handle_request(payload)
        return _encode_json(
            model_to_wire(response)
            if hasattr(response, "model_dump")
            else response
        )

    def serve(self, read: "Callable[[int], bytes]", write: "Callable[[bytes], None]") -> None:
        """Serve framed requests until the channel reaches clean EOF.

        A frame that cannot be decoded answers with one error frame and
        ends the channel: the byte stream can no longer be
        resynchronized (SPEC.md section 12.1).
        """

        while True:
            try:
                payload = _read_frame_or_none(read, self._frame_limit)
            except MeltCommandError as error:
                write(
                    _encode_json(
                        {"ok": False, "errors": model_to_wire_diagnostics(error)}
                    )
                )
                return
            if payload is None:
                return
            response = self.handle_request(payload)
            write(
                _encode_json(
                    model_to_wire(response)
                    if hasattr(response, "model_dump")
                    else response
                )
            )


# ---------------------------------------------------------------------------
# Loading committed state (SPEC.md section 14, startup steps 2 and 3)
# ---------------------------------------------------------------------------


def _load_state(
    store: WorkspaceStore,
) -> tuple[WorkspaceState, dict[str, dict], list[dict]]:
    """Rebuild the current state from the committed record stream."""

    latest_snapshot = None
    receipts: dict[str, dict] = {}
    notifications: list[dict] = []
    for record in store.records():
        if record.kind == SNAPSHOT_KIND:
            latest_snapshot = record
        elif record.kind == RECEIPT_KIND:
            receipt = parse_record(record.data)
            receipts[receipt.requestId] = dict(record.data)
        elif record.kind == NOTIFICATION_KIND:
            parse_record(record.data)
            notifications.append(dict(record.data))
    if latest_snapshot is None:
        state = WorkspaceState(
            workspace_id=store.workspace_id,
            epoch=store.epoch,
            commit_sequence=store.commit_sequence,
        )
        return state, receipts, notifications
    snapshot = parse_record(latest_snapshot.data)
    state = WorkspaceState(
        workspace_id=store.workspace_id,
        epoch=store.epoch,
        commit_sequence=store.commit_sequence,
        document=model_to_wire(snapshot.document),
        entity_revisions=dict(snapshot.entityRevisions),
        parameter_states={
            parameter_id: ParameterStateValue(
                value=stored.value, revision=stored.revision
            )
            for parameter_id, stored in snapshot.parameterStates.items()
        },
        incarnations=dict(snapshot.incarnations),
    )
    return state, receipts, notifications


def _snapshot_wire(state: WorkspaceState, observed_sequence: int) -> dict:
    """Build the committed snapshot record of one transaction.

    ``commitSequence`` is the sequence the snapshot observed, matching
    the read contract of SPEC.md section 12.6; the notification of the
    same transaction carries the new sequence.
    """

    return {
        "recordType": "snapshot",
        "schemaVersion": RECORD_SCHEMA_VERSION,
        "workspaceId": state.workspace_id,
        "language": "melt/v0.1",
        "epoch": state.epoch,
        "commitSequence": observed_sequence,
        "document": state.document,
        "entityRevisions": dict(state.entity_revisions),
        "parameterStates": {
            parameter_id: {
                "recordType": "parameterState",
                "schemaVersion": RECORD_SCHEMA_VERSION,
                "parameterId": parameter_id,
                "value": stored.value,
                "revision": stored.revision,
            }
            for parameter_id, stored in state.parameter_states.items()
        },
        "incarnations": dict(state.incarnations),
        "activeRuns": [],
        "diagnostics": [],
    }


def model_to_wire_diagnostics(error: MeltSchemaError) -> list[dict]:
    return [model_to_wire(item) for item in error.diagnostics]


def _salvage_request_id(payload: bytes) -> "str | None":
    """Recover a request identifier from an otherwise invalid envelope."""

    try:
        value = parse_json(payload)
    except MeltSchemaError:
        return None
    if not isinstance(value, dict):
        return None
    request_id = value.get("requestId")
    if isinstance(request_id, str) and 1 <= len(request_id) <= 128:
        return request_id
    return None


def _encode_json(value: Any) -> bytes:
    return encode_frame(
        json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
    )


def _read_exact(read: "Callable[[int], bytes]", count: int) -> bytes:
    parts: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = read(remaining)
        if not chunk:
            break
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


def _read_frame_or_none(
    read: "Callable[[int], bytes]", limit: int
) -> "bytes | None":
    """Read one frame; return None on clean end of stream.

    The declared length is checked against the limit before any payload
    byte is read (SPEC.md section 12.1).
    """

    header = _read_exact(read, 4)
    if header == b"":
        return None
    if len(header) < 4:
        raise MeltCommandError(
            (
                Diagnostic(
                    code=DiagnosticCode.PARSE,
                    path="",
                    message="truncated frame header",
                ),
            )
        )
    declared = int.from_bytes(header, "big")
    if declared > limit:
        raise MeltCommandError(
            (
                Diagnostic(
                    code=DiagnosticCode.LIMIT,
                    path="",
                    message=f"declared frame length {declared} exceeds the"
                    f" allowed bound of {limit} bytes",
                ),
            )
        )
    payload = _read_exact(read, declared)
    if len(payload) < declared:
        raise MeltCommandError(
            (
                Diagnostic(
                    code=DiagnosticCode.PARSE,
                    path="",
                    message=f"truncated frame: expected {declared} payload"
                    " bytes",
                ),
            )
        )
    return payload


__all__ = [
    "NotificationWindow",
    "SNAPSHOT_REQUIRED",
    "WorkspaceService",
]
