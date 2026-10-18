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

The service methods of section 12.6 ride the same envelope. Methods
other than ``workspace.apply`` and ``workspace.preview`` carry a
``payload``; only the methods with revision preconditions carry
``expected``. Reads answer with the sequence they observed. Admission
answers ``approvalRequired`` as a successful result when the permission
grant is missing: it never grants permission and never reports an
execution that did not happen.

``source.import`` registers one captured artifact as a new immutable
source version (SPEC.md sections 5 and 12.6). The service rereads the
stored bytes, verifies their content hash and size, measures the schema
the bytes carry, and appends the version with import provenance in one
atomic transaction. Run provenance is attached only by the publication
path; no document command reaches it. The verified artifact records
rebuild the imported source catalog on restart, so saved sources
compile again without rereading any bytes.

The service is the sole writer: it holds the workspace ``owner.lock``
through a :class:`~runtime.storage.WorkspaceStore` and commits each
transaction as one atomic append on the owning connection. Because the
store counts one sequence per committed transaction and no other writer
exists, the sequence of the next commit is predicted and verified after
the append.

State restoration on open loads the last committed snapshot, receipts,
notifications, and run records from the record stream without importing
or executing project code (SPEC.md section 14, steps 2 and 3).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

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
    parse_payload,
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
from melt.schema.limits import DEFAULT_LIMITS, ServiceLimits, limits_wire
from melt.schema.records import (
    RECORD_SCHEMA_VERSION,
    RunRecord,
    parse_record,
    record_to_wire,
)
from runtime.compiler.catalog import (
    EMPTY_ACTION_REGISTRY,
    EMPTY_SOURCE_CATALOG,
    ActionRegistry,
    CatalogArtifact,
    SourceCatalog,
)
from runtime.compiler.compiler import CompileRequest, compile_document
from runtime.storage import PlatformAdapter, WorkspaceStore
from runtime.workspace.importer import (
    MeasuredArtifact,
    MeltImportError,
    append_source_version,
    import_version,
    register_artifact,
)
from runtime.workspace.inspection import (
    active_run_records,
    current_diagnostics,
    preview_effects,
    snapshot_result,
)
from runtime.workspace.mutations import (
    EntityTouch,
    MutationPlan,
    expectation_diagnostics,
    initial_state_from_document,
    plan_mutation,
)
from runtime.workspace.state import (
    ARTIFACT_KIND,
    LAYOUT_PATH,
    NOTIFICATION_KIND,
    RECEIPT_KIND,
    RUN_KIND,
    SNAPSHOT_KIND,
    ParameterStateValue,
    WorkspaceState,
    entity_path,
    pointer_key,
    run_path,
    source_path,
    state_path,
)
from runtime.workspace.tokens import CapturedArtifact, TokenStore

SNAPSHOT_REQUIRED = "snapshot-required"
"""The status a subscription returns when ordered changes cannot be sent."""

SERVICE_VERSION = "0.1.0"
SERVICE_IDENTIFIER = f"melt-service/{SERVICE_VERSION}"
"""The service version a handshake reports (SPEC.md section 12.6)."""

SUPPORTED_PROTOCOL_VERSIONS = (1,)
"""The protocol framing versions this service speaks."""

LANGUAGE_VERSION = "melt/v0.1"
"""The only language version this service compiles."""

REVISION_PRECONDITION_METHODS = frozenset(
    (
        "workspace.apply",
        "workspace.preview",
        "source.import",
        "action.invoke",
        "control.activate",
    )
)
"""The methods whose envelope may carry an expected map (section 12.6)."""

UNANSWERED_METHODS = frozenset(
    ("action.register", "action.invoke", "table.page")
)
"""Methods this service does not answer yet.

Each arrives with the subsystem that owns it: the registration
registry, invocation scheduling, and the trusted table reader. Until
then the methods are refused, never silently accepted.
"""

ACTIVE_RUN_STATES = ("queued", "running")


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


def _parsed_payload(envelope: CommandEnvelope) -> "Any | MeltCommandError":
    """Parse one method payload, or return the error to answer with."""

    try:
        return parse_payload(envelope.method, envelope.payload)
    except MeltCommandError as error:
        return error


class WorkspaceService:
    """One open workspace under single-writer ownership."""

    def __init__(
        self,
        store: WorkspaceStore,
        state: WorkspaceState,
        receipts: dict[str, dict],
        notifications: list[dict],
        *,
        runs: "Mapping[str, RunRecord] | None" = None,
        source_catalog: "SourceCatalog | None" = None,
        action_registry: "ActionRegistry | None" = None,
        imports: "SourceCatalog | None" = None,
        frame_limit: int | None = None,
        limits: ServiceLimits = DEFAULT_LIMITS,
    ):
        self._store = store
        self._state = state
        self._receipts = receipts
        self._notifications = notifications
        self._runs: dict[str, RunRecord] = dict(runs or {})
        self._fixture_catalog = source_catalog or EMPTY_SOURCE_CATALOG
        self._imports = imports or SourceCatalog()
        self._source_catalog = self._merge_catalog()
        self._action_registry = action_registry or EMPTY_ACTION_REGISTRY
        self._limits = limits
        self._frame_limit = (
            DEFAULT_LIMITS.control_frame_bytes if frame_limit is None else frame_limit
        )
        self._tokens = TokenStore(state.workspace_id, store.artifacts)
        self._client: "dict | None" = None

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
        limits: ServiceLimits = DEFAULT_LIMITS,
        node: str | None = None,
    ) -> "WorkspaceService":
        """Take ownership of one workspace and restore its state."""

        store = WorkspaceStore.open(platform, workspace_id, node=node)
        try:
            state, receipts, notifications, runs, imports = _load_state(store)
        except BaseException:
            store.close()
            raise
        return cls(
            store,
            state,
            receipts,
            notifications,
            runs=runs,
            source_catalog=source_catalog,
            action_registry=action_registry,
            imports=imports,
            frame_limit=frame_limit,
            limits=limits,
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
    def runs(self) -> dict[str, RunRecord]:
        """Every known run record, keyed by run identifier."""

        return dict(self._runs)

    @property
    def client(self) -> "dict | None":
        """The negotiated session of the last ``session.hello``."""

        return None if self._client is None else dict(self._client)

    @property
    def store(self) -> WorkspaceStore:
        """The owning storage, for recovery and crash tests."""

        return self._store

    @property
    def source_catalog(self) -> SourceCatalog:
        """The trusted source catalog this service compiles against.

        Imported artifacts override fixture entries, because the service
        measured those bytes itself (SPEC.md sections 5.1 and 11.1).
        """

        return SourceCatalog(dict(self._source_catalog.artifacts))

    def _merge_catalog(self) -> SourceCatalog:
        return SourceCatalog(
            {**self._fixture_catalog.artifacts, **self._imports.artifacts}
        )

    def _catalog_entry_of(self, measured: MeasuredArtifact) -> CatalogArtifact:
        """Return the catalog entry one measured artifact contributes."""

        return CatalogArtifact.model_validate(
            {
                "path": f"artifacts/{measured.artifact[len('sha256:') :]}",
                "kind": measured.kind,
                "mediaType": measured.mediaType,
                "byteLength": measured.byteLength,
                "schema": measured.schema,
            }
        )

    def receipt(self, request_id: str) -> "dict | None":
        stored = self._receipts.get(request_id)
        return None if stored is None else dict(stored)

    # ------------------------------------------------------------------
    # Method dispatch (SPEC.md section 12.6)
    # ------------------------------------------------------------------

    def dispatch(self, envelope: CommandEnvelope) -> CommandResponse:
        """Route one parsed envelope to the handler of its method.

        Every method except the session handshake addresses the
        workspace this service owns. Only methods with revision
        preconditions may carry ``expected``; the envelope model keeps
        operation methods and payload methods apart.
        """

        if envelope.method == "session.hello":
            return self._session_hello(envelope)
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
        if (
            envelope.method not in REVISION_PRECONDITION_METHODS
            and envelope.expected is not None
        ):
            return _rejected(
                envelope.requestId,
                _diagnostic(
                    DiagnosticCode.SCHEMA,
                    "/expected",
                    f"method {envelope.method!r} has no revision"
                    " precondition and carries no expected map",
                ),
            )
        if envelope.method in UNANSWERED_METHODS:
            return _rejected(
                envelope.requestId,
                _diagnostic(
                    DiagnosticCode.SCHEMA,
                    "/method",
                    f"method {envelope.method!r} is not implemented by"
                    " this service",
                ),
            )
        handlers: dict[str, Callable[[CommandEnvelope], CommandResponse]] = {
            "workspace.apply": self.apply,
            "workspace.preview": self.preview,
            "workspace.create": self._workspace_create,
            "workspace.inspect": self._workspace_inspect,
            "workspace.subscribe": self._workspace_subscribe,
            "source.import": self._source_import,
            "control.activate": self._control_activate,
            "run.cancel": self._run_cancel,
            "run.inspect": self._run_inspect,
        }
        handler = handlers.get(envelope.method)
        if handler is None:  # pragma: no cover - the method set is closed
            return _rejected(
                envelope.requestId,
                _diagnostic(
                    DiagnosticCode.SCHEMA,
                    "/method",
                    f"method {envelope.method!r} is not implemented by"
                    " this service",
                ),
            )
        return handler(envelope)

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

    def preview(self, envelope: CommandEnvelope) -> CommandResponse:
        """Answer one ``workspace.preview`` dry run without writing.

        The preview plans the same transaction an apply would and
        reports its effects: diagnostics, affected record paths,
        controller replacements, state resets, invalidated runs, and the
        candidate document hash. It reserves no revision, stores no
        receipt, and grants no permission, so applying after a preview
        still requires a fresh, successful concurrency check (SPEC.md
        section 12.4).
        """

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
        if envelope.method != "workspace.preview":
            return _rejected(
                envelope.requestId,
                _diagnostic(
                    DiagnosticCode.SCHEMA,
                    "/method",
                    f"method {envelope.method!r} is not implemented by"
                    " this service",
                ),
            )
        plan = plan_mutation(
            self._state,
            envelope,
            source_catalog=self._source_catalog,
            action_registry=self._action_registry,
        )
        if not plan.ok:
            return _rejected(envelope.requestId, *plan.diagnostics)
        result = preview_effects(self._state, plan, self._runs)
        return OkResponse(
            requestId=envelope.requestId,
            ok=True,
            commitSequence=self._state.commit_sequence,
            result=result,
        )

    def create_workspace(
        self, document: "dict | Document", request_id: str
    ) -> CommandResponse:
        """Create the workspace from one document (SPEC.md section 12.6).

        Creation is valid only for an absent workspace whose document
        carries empty ``sources`` and ``actions`` collections, so
        creation stays separate from artifact import and execution
        registration. Every entity starts at revision 1 and every
        parameter stores its default at state revision 1. The result is
        the initial snapshot an inspection would return.
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
        return self._commit(
            request_id,
            "workspace.create",
            payload_hash,
            plan,
            result_of=lambda new_state: snapshot_result(
                new_state, self._runs, []
            ),
        )

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
        *,
        result_of: "Callable[[WorkspaceState], dict] | None" = None,
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
        if result_of is None:
            result = {
                "changedRecords": changed,
                "revisions": dict(sorted(new_revisions.items())),
            }
        else:
            result = result_of(new_state)
        active = tuple(record_to_wire(run) for run in active_run_records(self._runs))
        return self._append_transaction(
            request_id,
            method,
            payload_hash,
            new_state,
            changed=changed,
            result=result,
            leading_records=[
                (
                    SNAPSHOT_KIND,
                    _snapshot_wire(
                        new_state, self._state.commit_sequence, active
                    ),
                )
            ],
        )

    def _append_transaction(
        self,
        request_id: str,
        method: str,
        payload_hash: str,
        new_state: WorkspaceState,
        *,
        changed: "Sequence[str]",
        result: dict,
        leading_records: "Sequence[tuple[str, dict]]" = (),
        trailing_records: "Sequence[tuple[str, dict]]" = (),
    ) -> OkResponse:
        """Append one transaction's records atomically and adopt its state.

        The receipt sits between the leading records (the committed
        snapshot) and the trailing records (run and artifact records),
        and the notification lands last, so every consumer that reads
        the stream in order sees a change only after its receipt.
        """

        predicted = new_state.commit_sequence
        response = OkResponse(
            requestId=request_id,
            ok=True,
            commitSequence=predicted,
            result=result,
        )
        receipt = (
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
        )
        records: list[tuple[str, dict]] = [*leading_records, receipt]
        records.extend(trailing_records)
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
                        "changedRecords": list(changed),
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
        self._receipts[request_id] = dict(receipt[1])
        if changed:
            self._notifications.append(dict(records[-1][1]))
        return response

    def _commit_receipted(
        self,
        request_id: str,
        method: str,
        payload_hash: str,
        result: dict,
        *,
        changed: "Sequence[str]" = (),
        run_records: "Sequence[RunRecord]" = (),
    ) -> OkResponse:
        """Commit one processed request with its receipt.

        Admission and cancellation store durable receipts so a retry
        replays the stored answer and reuse with different content fails
        (SPEC.md sections 12.5 and 12.6). The transaction advances the
        commit sequence once even when it changed no record; it appends
        one notification only when records changed.
        """

        new_state = self._state.with_commit(self._state.commit_sequence + 1)
        response = self._append_transaction(
            request_id,
            method,
            payload_hash,
            new_state,
            changed=changed,
            result=result,
            trailing_records=[
                (RUN_KIND, record_to_wire(run)) for run in run_records
            ],
        )
        for run in run_records:
            self._runs[run.runId] = run
        return response

    # ------------------------------------------------------------------
    # Session, inspection, and subscription (SPEC.md sections 12.4, 12.6)
    # ------------------------------------------------------------------

    def _session_hello(self, envelope: CommandEnvelope) -> CommandResponse:
        """Negotiate versions and report the epoch and the limits.

        The handshake precedes workspace routing: it answers for this
        service process, touches no workspace state, and stores no
        receipt. The epoch it reports belongs to the workspace this
        service owns.
        """

        parsed = _parsed_payload(envelope)
        if isinstance(parsed, MeltCommandError):
            return _rejected(envelope.requestId, *parsed.diagnostics)
        common = sorted(
            set(parsed.protocolVersions) & set(SUPPORTED_PROTOCOL_VERSIONS)
        )
        if not common:
            return _rejected(
                envelope.requestId,
                _diagnostic(
                    DiagnosticCode.VERSION,
                    "/payload/protocolVersions",
                    "no common protocol version; this service speaks"
                    f" {list(SUPPORTED_PROTOCOL_VERSIONS)}",
                ),
            )
        if LANGUAGE_VERSION not in parsed.languageVersions:
            return _rejected(
                envelope.requestId,
                _diagnostic(
                    DiagnosticCode.VERSION,
                    "/payload/languageVersions",
                    "no common language version; this service compiles"
                    f" {LANGUAGE_VERSION!r}",
                ),
            )
        selected = common[-1]
        self._client = {
            "clientVersion": parsed.clientVersion,
            "protocolVersion": selected,
            "language": LANGUAGE_VERSION,
        }
        result = {
            "protocolVersion": selected,
            "language": LANGUAGE_VERSION,
            "serverVersion": SERVICE_IDENTIFIER,
            "clientVersion": parsed.clientVersion,
            "epoch": self._state.epoch,
            "limits": limits_wire(self._limits),
        }
        return OkResponse(
            requestId=envelope.requestId,
            ok=True,
            commitSequence=self._state.commit_sequence,
            result=result,
        )

    def _workspace_create(self, envelope: CommandEnvelope) -> CommandResponse:
        parsed = _parsed_payload(envelope)
        if isinstance(parsed, MeltCommandError):
            return _rejected(envelope.requestId, *parsed.diagnostics)
        return self.create_workspace(parsed.document, envelope.requestId)

    def _workspace_inspect(self, envelope: CommandEnvelope) -> CommandResponse:
        """Return the bounded snapshot of the committed workspace.

        Reads answer with the sequence they observed and store no
        receipt. The diagnostics recompile the committed document
        against the current trusted inputs, so a registration or
        artifact that drifted is named while the saved definitions stay
        readable (SPEC.md sections 7.2 and 12.4).
        """

        parsed = _parsed_payload(envelope)
        if isinstance(parsed, MeltCommandError):
            return _rejected(envelope.requestId, *parsed.diagnostics)
        if not self._state.present:
            return _rejected(
                envelope.requestId,
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    "/workspaceId",
                    f"workspace {self._state.workspace_id!r} holds no"
                    " document; create the workspace before inspecting"
                    " it",
                ),
            )
        diagnostics = current_diagnostics(
            self._state, self._source_catalog, self._action_registry
        )
        result = snapshot_result(
            self._state,
            self._runs,
            diagnostics,
            collection=parsed.collection,
            ids=parsed.ids,
        )
        return OkResponse(
            requestId=envelope.requestId,
            ok=True,
            commitSequence=self._state.commit_sequence,
            result=result,
        )

    def _workspace_subscribe(
        self, envelope: CommandEnvelope
    ) -> CommandResponse:
        """Answer one subscription query (SPEC.md section 12.5).

        Ordered changes answer with the observed sequence. A changed
        epoch or a gap in the notification log answers the successful
        ``snapshot-required`` status instead, so the host takes a full
        snapshot rather than replaying a broken order.
        """

        parsed = _parsed_payload(envelope)
        if isinstance(parsed, MeltCommandError):
            return _rejected(envelope.requestId, *parsed.diagnostics)
        window = self.changes_after(parsed.afterSequence, parsed.epoch)
        result: dict = {"status": window.status}
        if window.status == "changes":
            result["changes"] = [dict(item) for item in window.changes]
        else:
            result["reason"] = window.reason
        return OkResponse(
            requestId=envelope.requestId,
            ok=True,
            commitSequence=self._state.commit_sequence,
            result=result,
        )

    # ------------------------------------------------------------------
    # Source import (SPEC.md sections 5, 12.6)
    # ------------------------------------------------------------------

    def _source_import(self, envelope: CommandEnvelope) -> CommandResponse:
        """Register one captured artifact as a new source version.

        The artifact token stands for bytes the service itself captured
        and stored; a client cannot supply a path or forge a record
        (SPEC.md sections 5.1 and 12.6). The service rereads the stored
        bytes, verifies their hash and size, measures their schema, and
        appends one immutable version to the logical source. ``expected``
        carries the source record's revision, or zero for a new source,
        so a concurrent import surfaces as a conflict instead of a
        silent overwrite. Imports use durable request receipts like
        every other admission.
        """

        payload_hash = canonical_hash(model_to_wire(envelope))
        replay = self._replay_or_reject(envelope.requestId, payload_hash)
        if replay is not None:
            return replay
        parsed = _parsed_payload(envelope)
        if isinstance(parsed, MeltCommandError):
            return _rejected(envelope.requestId, *parsed.diagnostics)
        if not self._state.present:
            return _rejected(
                envelope.requestId,
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    "/workspaceId",
                    f"workspace {self._state.workspace_id!r} holds no"
                    " document; create the workspace before importing"
                    " sources",
                ),
            )
        record = source_path(parsed.sourceId)
        diagnostics = expectation_diagnostics(
            self._state, dict(envelope.expected or {}), [record]
        )
        if diagnostics:
            return _rejected(envelope.requestId, *sorted_diagnostics(diagnostics))
        captured = self._tokens.redeem(parsed.artifactToken)
        if captured is None:
            return _rejected(
                envelope.requestId,
                _diagnostic(
                    DiagnosticCode.ARTIFACT,
                    "/payload/artifactToken",
                    "artifact token is unknown, expired, or scoped to"
                    " another workspace",
                    hint="Capture the artifact again through the host"
                    " import operation; a restart withdraws every"
                    " outstanding token.",
                ),
            )
        try:
            measured = register_artifact(self._store.artifacts, captured)
            version = import_version(measured, parsed.originLabel)
            candidate, number = append_source_version(
                self._state.document, parsed.sourceId, measured, version
            )
        except MeltImportError as error:
            return _rejected(envelope.requestId, *error.diagnostics)

        entry = self._catalog_entry_of(measured)
        catalog = SourceCatalog(
            {**self._source_catalog.artifacts, measured.artifact: entry}
        )
        result = compile_document(
            CompileRequest(
                document=candidate,
                source_catalog=catalog,
                action_registry=self._action_registry,
                parameter_states={
                    parameter_id: {
                        "value": stored.value,
                        "revision": stored.revision,
                    }
                    for parameter_id, stored in self._state.parameter_states.items()
                },
            )
        )
        if not result.ok:
            return _rejected(envelope.requestId, *result.diagnostics)

        entity_revisions = dict(self._state.entity_revisions)
        new_revision = self._state.current_revision(record) + 1
        entity_revisions[record] = new_revision
        new_state = WorkspaceState(
            workspace_id=self._state.workspace_id,
            epoch=self._state.epoch,
            commit_sequence=self._state.commit_sequence + 1,
            # The normalized wire form keeps the canonical document hash
            # stable across restarts (SPEC.md section 4.2).
            document=model_to_wire(Document.model_validate(candidate)),
            entity_revisions=entity_revisions,
            parameter_states=dict(self._state.parameter_states),
            incarnations=dict(self._state.incarnations),
        )
        active = tuple(record_to_wire(run) for run in active_run_records(self._runs))
        trailing: list[tuple[str, dict]] = []
        if measured.artifact not in self._imports.artifacts:
            # The first registration of these bytes commits the verified
            # artifact record the reopened catalog rebuilds from.
            trailing.append(
                (
                    ARTIFACT_KIND,
                    {
                        "recordType": "artifact",
                        "schemaVersion": RECORD_SCHEMA_VERSION,
                        "artifact": measured.artifact,
                        "kind": measured.kind,
                        "mediaType": measured.mediaType,
                        "byteLength": measured.byteLength,
                        "schema": measured.schema,
                        "createdAt": _utc_now(),
                    },
                )
            )
        import_result = {
            "source": {"sourceId": parsed.sourceId, "version": number},
            "sourceRevision": new_revision,
            "artifact": measured.artifact,
            "kind": measured.kind,
            "mediaType": measured.mediaType,
            "byteLength": measured.byteLength,
            "schema": measured.schema,
        }
        response = self._append_transaction(
            envelope.requestId,
            envelope.method,
            payload_hash,
            new_state,
            changed=[record],
            result=import_result,
            leading_records=[
                (SNAPSHOT_KIND, _snapshot_wire(new_state, self._state.commit_sequence, active))
            ],
            trailing_records=trailing,
        )
        self._imports = SourceCatalog(
            {**self._imports.artifacts, measured.artifact: entry}
        )
        self._source_catalog = self._merge_catalog()
        return response

    # ------------------------------------------------------------------
    # Control activation and runs (SPEC.md sections 12.6, 13.1)
    # ------------------------------------------------------------------

    def _control_activate(self, envelope: CommandEnvelope) -> CommandResponse:
        """Route one button activation through its binding.

        The activation needs the projection revision and the revision of
        every binding that connects the control to an action (SPEC.md
        section 12.6). A control without a binding is a reported no-op.
        A bound activation is admitted: with the permission grant
        missing it answers the successful ``approvalRequired`` result,
        which is an admission state, never a permission grant and never
        a claimed execution (SPEC.md sections 12.6 and 13.1).
        """

        payload_hash = canonical_hash(model_to_wire(envelope))
        replay = self._replay_or_reject(envelope.requestId, payload_hash)
        if replay is not None:
            return replay
        parsed = _parsed_payload(envelope)
        if isinstance(parsed, MeltCommandError):
            return _rejected(envelope.requestId, *parsed.diagnostics)
        if not self._state.present:
            return _rejected(
                envelope.requestId,
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    "/workspaceId",
                    f"workspace {self._state.workspace_id!r} holds no"
                    " document; create the workspace before activating a"
                    " control",
                ),
            )
        document = self._state.document
        projection = document["projections"].get(parsed.projectionId)
        if projection is None:
            return _rejected(
                envelope.requestId,
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    "/payload/projectionId",
                    f"projection {parsed.projectionId!r} does not exist",
                ),
            )
        control = next(
            (
                item
                for item in projection["controls"]
                if item["id"] == parsed.controlId
            ),
            None,
        )
        if control is None:
            return _rejected(
                envelope.requestId,
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    "/payload/controlId",
                    f"projection {parsed.projectionId!r} carries no"
                    f" control {parsed.controlId!r}",
                ),
            )
        if control["kind"] != "button":
            return _rejected(
                envelope.requestId,
                _diagnostic(
                    DiagnosticCode.TYPE,
                    "/payload/controlId",
                    f"control {parsed.controlId!r} has kind"
                    f" {control['kind']!r}; only button controls emit"
                    " control.activate",
                ),
            )
        matching = sorted(
            (
                binding_id,
                binding,
            )
            for binding_id, binding in document["bindings"].items()
            if binding["event"]["kind"] == "control.activate"
            and binding["event"]["projection"] == parsed.projectionId
            and binding["event"]["control"] == parsed.controlId
        )
        required = [entity_path("projections", parsed.projectionId)]
        required.extend(entity_path("bindings", binding_id) for binding_id, _ in matching)
        diagnostics = expectation_diagnostics(
            self._state, dict(envelope.expected or {}), required
        )
        if diagnostics:
            return _rejected(envelope.requestId, *sorted_diagnostics(diagnostics))

        # A button activation is a user event: the host routes a press
        # through the same handler an agent adapter would call with a
        # programmatic origin (SPEC.md sections 2.2 and 9.1).
        origin = "user"
        if not matching:
            result = {
                "status": "no-op",
                "origin": origin,
                "projection": parsed.projectionId,
                "control": parsed.controlId,
                "reason": "no binding connects this control to an action",
            }
            return self._commit_receipted(
                envelope.requestId, envelope.method, payload_hash, result
            )

        binding_id, binding = matching[0]
        action_id = binding["effect"]["action"]
        action = document["actions"].get(action_id)
        if action is None:
            return _rejected(
                envelope.requestId,
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    f"/bindings/{binding_id}/effect/action",
                    f"binding {binding_id!r} targets action"
                    f" {action_id!r}, which does not exist",
                ),
            )
        registration = self._action_registry.entry(
            action["implementation"]["registrationId"]
        )
        if registration is None:
            return _rejected(
                envelope.requestId,
                _diagnostic(
                    DiagnosticCode.REGISTRATION,
                    f"/actions/{action_id}/implementation/registrationId",
                    f"registration"
                    f" {action['implementation']['registrationId']!r} is"
                    " not in the trusted registry; the action is"
                    " disabled",
                    hint="Register the script again, or restore the"
                    " registration the action cites.",
                ),
            )
        # Admission checks permission before anything runs (SPEC.md
        # section 13.1). No grant store is attached to this service yet,
        # and grants are host generated outside exported documents
        # (section 15.1), so the required grant is missing here: the
        # successful approvalRequired result follows. It records the
        # admission state only; it grants no permission, reserves no
        # generation, and reports no execution.
        result = {
            "status": "approvalRequired",
            "origin": origin,
            "action": action_id,
            "binding": binding_id,
            "requiredPermission": registration.required_permission,
        }
        return self._commit_receipted(
            envelope.requestId, envelope.method, payload_hash, result
        )

    def _run_cancel(self, envelope: CommandEnvelope) -> CommandResponse:
        """Cancel one run (SPEC.md sections 12.6 and 13.5).

        Cancellation is idempotent and needs no expected revision. A
        queued or running run moves to ``cancelled`` in one durable
        transaction with its receipt; a run that already reached a
        terminal state answers its current state with a matching
        cancellation status and changes nothing.
        """

        payload_hash = canonical_hash(model_to_wire(envelope))
        replay = self._replay_or_reject(envelope.requestId, payload_hash)
        if replay is not None:
            return replay
        parsed = _parsed_payload(envelope)
        if isinstance(parsed, MeltCommandError):
            return _rejected(envelope.requestId, *parsed.diagnostics)
        run = self._runs.get(parsed.runId)
        if run is None:
            return _rejected(
                envelope.requestId,
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    "/payload/runId",
                    f"run {parsed.runId!r} does not exist",
                ),
            )
        if run.state in ACTIVE_RUN_STATES:
            cancelled = run.model_copy(
                update={"state": "cancelled", "updatedAt": _utc_now()}
            )
            result = {
                "runId": run.runId,
                "state": cancelled.state,
                "cancellation": {"status": "cancelled"},
            }
            return self._commit_receipted(
                envelope.requestId,
                envelope.method,
                payload_hash,
                result,
                changed=[run_path(run.runId)],
                run_records=[cancelled],
            )
        status = "alreadyCancelled" if run.state == "cancelled" else "notActive"
        result = {
            "runId": run.runId,
            "state": run.state,
            "cancellation": {"status": status},
        }
        return self._commit_receipted(
            envelope.requestId, envelope.method, payload_hash, result
        )

    def _run_inspect(self, envelope: CommandEnvelope) -> CommandResponse:
        """Return one run record and its output references."""

        parsed = _parsed_payload(envelope)
        if isinstance(parsed, MeltCommandError):
            return _rejected(envelope.requestId, *parsed.diagnostics)
        run = self._runs.get(parsed.runId)
        if run is None:
            return _rejected(
                envelope.requestId,
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    "/payload/runId",
                    f"run {parsed.runId!r} does not exist",
                ),
            )
        wire = record_to_wire(run)
        return OkResponse(
            requestId=envelope.requestId,
            ok=True,
            commitSequence=self._state.commit_sequence,
            result={"run": wire, "outputs": dict(wire.get("outputs", {}))},
        )

    # ------------------------------------------------------------------
    # Host capture operations (SPEC.md section 12.6)
    # ------------------------------------------------------------------

    def capture_artifact_token(self, data: bytes, media_type: str) -> CapturedArtifact:
        """Capture import bytes and mint one opaque token.

        This is the host import operation behind ``source.import`` and
        ``action.register``: the host hands over bytes it read with user
        consent, and the service answers a workspace-scoped handle that
        expires after 15 minutes. Document commands carry the token,
        never a filesystem path.
        """

        return self._tokens.capture(data, media_type)

    def redeem_artifact_token(self, token: str) -> "CapturedArtifact | None":
        """Return the capture of one token, or None when unusable."""

        return self._tokens.redeem(token)

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
        return self.dispatch(envelope)

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
) -> tuple[
    WorkspaceState,
    dict[str, dict],
    list[dict],
    dict[str, RunRecord],
    SourceCatalog,
]:
    """Rebuild the current state from the committed record stream.

    The verified artifact records rebuild the imported source catalog,
    so a reopened workspace compiles its saved sources against the same
    measured schemas without rereading any artifact bytes (SPEC.md
    section 14, startup steps 2 and 3).
    """

    latest_snapshot = None
    receipts: dict[str, dict] = {}
    notifications: list[dict] = []
    runs: dict[str, RunRecord] = {}
    artifacts: dict[str, CatalogArtifact] = {}
    for record in store.records():
        if record.kind == SNAPSHOT_KIND:
            latest_snapshot = record
        elif record.kind == RECEIPT_KIND:
            receipt = parse_record(record.data)
            receipts[receipt.requestId] = dict(record.data)
        elif record.kind == NOTIFICATION_KIND:
            parse_record(record.data)
            notifications.append(dict(record.data))
        elif record.kind == RUN_KIND:
            run = parse_record(record.data)
            runs[run.runId] = run
        elif record.kind == ARTIFACT_KIND:
            artifact = parse_record(record.data)
            artifacts[artifact.artifact] = CatalogArtifact.model_validate(
                {
                    "path": f"artifacts/{artifact.artifact[len('sha256:') :]}",
                    "kind": artifact.kind,
                    "mediaType": artifact.mediaType,
                    "byteLength": artifact.byteLength,
                    "schema": model_to_wire(artifact.schema),
                }
            )
    imports = SourceCatalog(artifacts)
    if latest_snapshot is None:
        state = WorkspaceState(
            workspace_id=store.workspace_id,
            epoch=store.epoch,
            commit_sequence=store.commit_sequence,
        )
        return state, receipts, notifications, runs, imports
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
    return state, receipts, notifications, runs, imports


def _snapshot_wire(
    state: WorkspaceState,
    observed_sequence: int,
    active_runs: "Sequence[dict]" = (),
) -> dict:
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
        "activeRuns": list(active_runs),
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
