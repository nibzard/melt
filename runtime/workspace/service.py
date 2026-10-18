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

``action.register`` stores one trusted registration (SPEC.md sections
7.2 and 12.6): the captured script artifact and hash, the project root
and working-directory policy, the resolved environment fingerprint,
the parameter and input contract with declared outputs, the required
permission, and the fixed ``nonReplaySafe`` classification.
Registration verifies the stored bytes and never executes the script.
The registration identifier covers the captured bytes and the
environment, so edited scripts arrive as new registrations and a
changed contract over the same capture advances the registration's
immutable revision. Committed registration records rebuild the action
registry on restart, so saved actions compile again; an action whose
registration is unavailable stays disabled while its projections
remain readable.

Execution authority is separate from every document (SPEC.md section
15.1). Specific-run and project execution grants live in the service
record stream, minted and revoked only through the authenticated host
permission endpoint that no command envelope reaches. Admission
rechecks the grant set before anything runs, and the launch and
publication paths recheck it again, where revocation blocks
publication and requests cancellation. A grant never enters a
document, a snapshot, or a compiler input.

``table.page`` answers bounded pages through the trusted table reader
of :mod:`runtime.workspace.reader` (SPEC.md sections 8.2 and 12.7).
The request names an immutable source reference; the service resolves
it to stored artifact bytes, the reader validates operators against
the declared column types, filters before sorting and paging, places
nulls last with the original row position as the final tie-breaker,
and answers the source reference and query hash with cells in their
wire shapes. A page request mutates nothing, so it observes the commit
sequence and stores no receipt.

The service also owns scheduling (SPEC.md sections 7.3, 9.3, and 13.1).
Every admission captures the full invocation snapshot, reserves its
generation in the admitting transaction, and stores one pending intent
per output group; the reservation alone makes older runs ineligible for
current publication. Bindings that a committed user change fires
coalesce into one intent with the final parameter values, debounce
timers live in the service, and repeated events replace the pending
intent and restart its timer. Launch rechecks permission, registration
availability, and the resolved environment fingerprint immediately
before the fresh process starts, and a restart marks pending work
interrupted rather than executing it.

The service owns atomic publication too (SPEC.md sections 13.3 to
13.5). :meth:`WorkspaceService.publish_run` verifies the staged outputs
of one completed run independently of the runner's report, finalizes
the immutable artifact paths, rechecks every eligibility condition of
section 13.3 inside the owning transaction — including the publication
permission gate, where a denial rejects the publication and revocation
additionally requests cancellation — allocates the output source
versions through the service-only run-provenance hook, updates every
target reference together, and stores the run disposition with a
publication receipt in one commit. A missing or invalid output rejects
the entire publication, so the previous results remain together; a
verified but obsolete run archives as superseded historical output that
no current reference points at. Every output group reports its result
status with ``resultIsStale`` and the captured parameters of the
retained result through :meth:`output_group_status` and
``workspace.inspect``.

The service is the sole writer: it holds the workspace ``owner.lock``
through a :class:`~runtime.storage.WorkspaceStore` and commits each
transaction as one atomic append on the owning connection. Because the
store counts one sequence per committed transaction and no other writer
exists, the sequence of the next commit is predicted and verified after
the append.

State restoration on open loads the last committed snapshot, receipts,
notifications, run records, and grants from the record stream without
importing or executing project code (SPEC.md section 14, steps 2 and 3).
Opening then runs the startup recovery pass of backlog task T022
(SPEC.md sections 14 and 18.4): it skips committed records that no
longer validate and reports them as corrupt descriptors while the last
valid snapshot keeps the previous readable interface, it reports every
artifact a saved reference names but storage lacks, each one
individually, it marks previous active jobs and pending timers
interrupted, it reconciles every ``staging/<run-id>`` tree against the
committed publication receipts, and it records the explicit rerun and
recovery actions a host may offer. The pass executes nothing: an
unknown stored version refuses to open with files preserved, and
uncertain Python effects are never rerun. Its report is exposed as
``service.recovery_report`` and in the ``recovery`` view of
``workspace.inspect``.
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
    CapturedParameter,
    OutputGroupRecord,
    PendingIntentRecord,
    PermissionGrantRecord,
    PublicationRecord,
    RegistrationRecord,
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
from runtime.execution import launch_run, materialize_script, write_run_manifest
from runtime.execution.launcher import MeltLaunchError
from runtime.storage import PlatformAdapter, WorkspaceStore
from runtime.storage.errors import WorkspaceFormatError
from runtime.storage.workspace import HEADER_KIND
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
from runtime.workspace.permissions import (
    GRANT_KINDS,
    AuthorityDecision,
    authority_at_admission,
    authority_at_launch,
    authority_at_publication,
    build_grant,
    mint_grant_id,
    mint_intent_id,
    permission_error,
    revocation_blocks,
)
from runtime.workspace.publication import (
    CURRENT,
    NOT_PUBLISHABLE,
    REJECTED,
    SUPERSEDED,
    PublicationOutcome,
    VerifiedOutput,
    allocate_output_sources,
    append_output_versions,
    mint_publication_id,
    obsolescence_error,
    result_statuses,
    retarget_projections,
    target_ineligibility,
    verify_staged_outputs,
)
from runtime.workspace.reader import MeltReaderError, PageQuery, read_table_page
from runtime.workspace.recovery import (
    CorruptRecordNote,
    RecoveryReport,
    empty_report,
    missing_artifact_diagnostics,
    reconcile_staging,
    recovery_offers,
)
from runtime.workspace.registry import (
    EnvironmentCatalog,
    KnownEnvironment,
    MeltRegistrationError,
    build_registration,
    contract_of,
    local_environment,
    registration_entry_of,
    registration_identity,
    registration_to_wire,
    register_script,
    validate_contract,
)
from runtime.workspace.scheduling import (
    AdmissionRecords,
    InvocationSnapshot,
    RunLaunchOutcome,
    build_pending_intent,
    build_run_record,
    bump_group_revision,
    changed_output_groups,
    default_clock,
    due_intents,
    expire_intent,
    intent_definition_error,
    interrupt_intent,
    is_newest_generation,
    mint_run_id,
    reserve_generation,
    timestamp_of,
    triggered_actions,
    value_changed_parameters,
)
from runtime.workspace.state import (
    ARTIFACT_KIND,
    LAYOUT_PATH,
    NOTIFICATION_KIND,
    OUTPUT_GROUP_KIND,
    PENDING_INTENT_KIND,
    PERMISSION_KIND,
    PUBLICATION_KIND,
    RECEIPT_KIND,
    REGISTRATION_KIND,
    RUN_KIND,
    SNAPSHOT_KIND,
    ParameterStateValue,
    WorkspaceState,
    entity_path,
    permission_path,
    pointer_key,
    publication_path,
    registration_path,
    run_path,
    source_path,
    state_path,
)
from runtime.workspace.tokens import CapturedArtifact, TokenStore
from melt.runner.manifest import build_manifest

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
        registrations: "Mapping[str, RegistrationRecord] | None" = None,
        environments: "Mapping[str, str] | None" = None,
        grants: "Mapping[str, PermissionGrantRecord] | None" = None,
        output_groups: "Mapping[str, OutputGroupRecord] | None" = None,
        pending_intents: "Mapping[str, PendingIntentRecord] | None" = None,
        publications: "Mapping[str, PublicationRecord] | None" = None,
        frame_limit: int | None = None,
        limits: ServiceLimits = DEFAULT_LIMITS,
        clock: "Callable[[], float] | None" = None,
    ):
        self._store = store
        self._state = state
        self._receipts = receipts
        self._notifications = notifications
        self._runs: dict[str, RunRecord] = dict(runs or {})
        self._fixture_catalog = source_catalog or EMPTY_SOURCE_CATALOG
        self._imports = imports or SourceCatalog()
        self._source_catalog = self._merge_catalog()
        self._fixture_registry = action_registry or EMPTY_ACTION_REGISTRY
        self._registrations: dict[str, RegistrationRecord] = dict(
            registrations or {}
        )
        self._action_registry = self._merge_registry()
        self._environments = EnvironmentCatalog(environments)
        self._grants: dict[str, PermissionGrantRecord] = dict(grants or {})
        self._output_groups: dict[str, OutputGroupRecord] = dict(
            output_groups or {}
        )
        # One pending request per output group: the slot holds the
        # newest intent of the group, whatever its status (SPEC.md
        # section 13.1).
        self._pending_intents: dict[str, PendingIntentRecord] = dict(
            pending_intents or {}
        )
        self._publications: dict[str, PublicationRecord] = dict(
            publications or {}
        )
        self._limits = limits
        self._frame_limit = (
            DEFAULT_LIMITS.control_frame_bytes if frame_limit is None else frame_limit
        )
        self._clock = clock or default_clock
        self._tokens = TokenStore(state.workspace_id, store.artifacts)
        self._client: "dict | None" = None
        # The startup pass fills this in; direct construction starts
        # from an empty report.
        self._recovery = empty_report()

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
        environments: "Mapping[str, str] | None" = None,
        frame_limit: int | None = None,
        limits: ServiceLimits = DEFAULT_LIMITS,
        node: str | None = None,
        clock: "Callable[[], float] | None" = None,
    ) -> "WorkspaceService":
        """Take ownership of one workspace and restore its state."""

        store = WorkspaceStore.open(platform, workspace_id, node=node)
        try:
            restored = _load_state(store)
        except BaseException:
            store.close()
            raise
        service = cls(
            store,
            restored.state,
            restored.receipts,
            restored.notifications,
            runs=restored.runs,
            source_catalog=source_catalog,
            action_registry=action_registry,
            imports=restored.imports,
            registrations=restored.registrations,
            environments=environments,
            grants=restored.grants,
            output_groups=restored.output_groups,
            pending_intents=restored.pending_intents,
            publications=restored.publications,
            frame_limit=frame_limit,
            limits=limits,
            clock=clock,
        )
        try:
            service._recovery = service._startup_recovery(restored.corrupt_records)
        except BaseException:
            service.close()
            raise
        return service

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

    @property
    def action_registry(self) -> ActionRegistry:
        """The trusted action registry this service compiles against.

        Registrations the service committed override fixture entries,
        because the service captured and verified those scripts itself
        (SPEC.md sections 7.2 and 11.1).
        """

        return ActionRegistry(dict(self._action_registry.registrations))

    @property
    def registrations(self) -> dict[str, RegistrationRecord]:
        """Every committed registration, keyed by registration ID."""

        return dict(self._registrations)

    @property
    def grants(self) -> dict[str, PermissionGrantRecord]:
        """Every committed execution grant, keyed by grant ID.

        Grants live outside the exported document: they rebuild from the
        committed grant records on restart, and no document command can
        read or change them (SPEC.md section 15.1).
        """

        return dict(self._grants)

    @property
    def output_groups(self) -> dict[str, OutputGroupRecord]:
        """Every output group's counters, keyed by output group ID.

        The generation is the newest reserved request number of the
        group; a run holds current eligibility only while its captured
        generation equals it (SPEC.md sections 7.3 and 9.3).
        """

        return dict(self._output_groups)

    @property
    def pending_intents(self) -> dict[str, PendingIntentRecord]:
        """The newest pending intent of every output group.

        One pending request exists per output group. Repeated events
        replace the slot; a restart marks a timer-running intent
        interrupted instead of executing it (SPEC.md sections 9.3 and
        13.1).
        """

        return dict(self._pending_intents)

    @property
    def publications(self) -> dict[str, PublicationRecord]:
        """Every committed publication receipt, keyed by receipt ID.

        A receipt concludes one publication attempt: the run it
        published, the outcome, and the source references it allocated.
        Recovery reconciles staging directories against these receipts
        (SPEC.md sections 13.4 and 14, startup step 6).
        """

        return dict(self._publications)

    @property
    def recovery_report(self) -> dict:
        """What the startup recovery pass found and did (SPEC.md 14).

        The wire shape of :class:`~runtime.workspace.recovery
        .RecoveryReport`: corrupt descriptors skipped while loading,
        missing artifacts reported individually, the runs and intents
        the restart marked interrupted, the staging trees
        reconciliation removed or kept, and the explicit rerun and
        recovery actions a host may offer. The same view rides the
        unfiltered ``workspace.inspect`` result.
        """

        return self._recovery.wire()

    def output_group_status(self) -> dict[str, dict]:
        """The result status of every output group (SPEC.md 13.5).

        Each group reports ``empty``, ``pending``, ``current``,
        ``stale``, or ``error`` with a separate ``resultIsStale``
        boolean and the captured parameters of the retained result, so
        a host shows the parameter values that produced the visible
        result whenever they differ from the current controls.
        """

        return result_statuses(
            self._state.document,
            self._runs,
            self._output_groups,
            self._pending_intents,
            self._state.parameter_states,
            action_revision_of=lambda action_id: self._state.current_revision(
                entity_path("actions", action_id)
            ),
            registry_entry_of=self._action_registry.entry,
        )

    def run_is_newest_generation(self, run_id: str) -> bool:
        """Report whether one run holds its group's newest generation."""

        run = self._runs.get(run_id)
        if run is None:
            raise permission_error(
                f"run {run_id!r} does not exist",
                "/runId",
                code=DiagnosticCode.REFERENCE,
            )
        return is_newest_generation(self._output_groups, run)

    def known_environments(self) -> dict[str, KnownEnvironment]:
        """The environments this service may register scripts against."""

        return self._environments.entries()

    def _merge_catalog(self) -> SourceCatalog:
        return SourceCatalog(
            {**self._fixture_catalog.artifacts, **self._imports.artifacts}
        )

    def _merge_registry(self) -> ActionRegistry:
        return ActionRegistry(
            {
                **self._fixture_registry.registrations,
                **{
                    registration_id: registration_entry_of(record)
                    for registration_id, record in self._registrations.items()
                },
            }
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

    def dispatch(
        self, envelope: CommandEnvelope, *, origin: str = "user"
    ) -> CommandResponse:
        """Route one parsed envelope to the handler of its method.

        Every method except the session handshake addresses the
        workspace this service owns. Only methods with revision
        preconditions may carry ``expected``; the envelope model keeps
        operation methods and payload methods apart. ``origin`` is the
        event origin of the caller: the host channel speaks for the
        local user, and an agent adapter proposes the same commands as
        programmatic changes (SPEC.md sections 2.2 and 9.1).
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
        handlers: dict[str, Callable[[CommandEnvelope], CommandResponse]] = {
            "workspace.preview": self.preview,
            "workspace.create": self._workspace_create,
            "workspace.inspect": self._workspace_inspect,
            "workspace.subscribe": self._workspace_subscribe,
            "source.import": self._source_import,
            "action.register": self._action_register,
            "run.cancel": self._run_cancel,
            "run.inspect": self._run_inspect,
            "table.page": self._table_page,
        }
        if envelope.method == "workspace.apply":
            return self.apply(envelope, origin=origin)
        # Admission answers the event origin of its authenticated caller
        # (SPEC.md sections 2.2 and 9.1): the host channel speaks for
        # the local user, and an agent adapter proposes the same command
        # as a programmatic change.
        if envelope.method == "action.invoke":
            return self._action_invoke(envelope, origin=origin)
        if envelope.method == "control.activate":
            return self._control_activate(envelope, origin=origin)
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
    # Event origin (SPEC.md sections 2.2 and 9.1)
    # ------------------------------------------------------------------

    def dispatch_as(
        self, envelope: CommandEnvelope, *, origin: str
    ) -> CommandResponse:
        """Route one envelope under an explicit event origin.

        The transport supplies caller identity: the framed host channel
        speaks for the local user, while an agent adapter proposes the
        same commands as programmatic changes. Bindings observe
        committed user changes only (SPEC.md section 9.2), so the origin
        decides whether a parameter change may start an action.
        """

        if origin not in ("user", "programmatic"):
            raise ValueError(
                f"origin {origin!r} is neither 'user' nor 'programmatic'"
            )
        return self.dispatch(envelope, origin=origin)

    # ------------------------------------------------------------------
    # Mutations (SPEC.md sections 12.2, 12.3, 12.5)
    # ------------------------------------------------------------------

    def apply(
        self, envelope: CommandEnvelope, *, origin: str = "user"
    ) -> CommandResponse:
        """Apply one ``workspace.apply`` transaction.

        The origin derives from the authenticated caller (SPEC.md
        section 9.1): the host channel speaks for the local user, and
        only a committed user change fires the ``parameter.changed``
        bindings of the candidate (section 9.2). A programmatic change
        commits the same state without emitting any event.
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
        admission = None
        if origin == "user":
            # A committed user change reserves its generations in this
            # same transaction (SPEC.md sections 9.3 and 13.1). Invalid
            # edits never reach this point: the plan already rejected
            # them, so nothing was committed, reserved, or scheduled.
            changed = value_changed_parameters(
                plan.new_states, plan.touched_state_paths
            )
            triggers = triggered_actions(plan.candidate, changed)
            if triggers:
                # The triggered admissions capture the world this
                # transaction commits: the group revisions it bumps and
                # the action revisions it writes.
                bumps = self._group_revision_bumps(plan.candidate)
                admission = self._plan_triggered_admissions(
                    plan, triggers, self._clock(), bumps
                )
        return self._commit(
            envelope.requestId,
            envelope.method,
            payload_hash,
            plan,
            admission=admission,
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
                new_state,
                self._runs,
                [],
                output_groups=self.output_group_status(),
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
        admission: "AdmissionRecords | None" = None,
    ) -> CommandResponse:
        """Commit one planned transaction atomically or not at all.

        The scheduling records of the same transaction ride along: the
        generations a triggering user change reserved, the intents it
        admitted, and the output group revisions the mutation bumped.
        Everything lands in one atomic append, so a restart never sees a
        committed parameter change without the generation it reserves
        (SPEC.md sections 9.3 and 13.1).
        """

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

        scheduling = admission or AdmissionRecords()
        scheduling = AdmissionRecords(
            groups=[*scheduling.groups, *self._group_revision_bumps(plan.candidate)],
            intents=list(scheduling.intents),
            runs=list(scheduling.runs),
        )
        changed = sorted(set(plan.changed_records() + scheduling.changed_paths()))
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
            # The admissions a triggering user change fired ride in the
            # same transaction; an uneventful one reports none.
            admitted = [
                self._admission_summary(item)
                for item in scheduling.intents
                if item.status != "expired"
            ]
            if admitted:
                result["admissions"] = admitted
        else:
            result = result_of(new_state)
        active = tuple(record_to_wire(run) for run in active_run_records(self._runs))
        response = self._append_transaction(
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
            trailing_records=self._scheduling_records(scheduling),
        )
        self._adopt_scheduling(scheduling)
        return response

    def _group_revision_bumps(self, candidate: dict) -> list[OutputGroupRecord]:
        """Return the bumped records of groups whose contract changed.

        The output group revision increments when targets, target kinds,
        output contracts, or ownership change (SPEC.md section 13.3). A
        group without a record yet starts at revision one when it is
        first admitted against, so only existing records are bumped
        here. A projection title or a layout change never appears in
        the comparison.
        """

        bumps: list[OutputGroupRecord] = []
        now = _utc_now()
        for group_id in sorted(
            changed_output_groups(self._state.document, candidate)
        ):
            existing = self._output_groups.get(group_id)
            if existing is not None:
                bumps.append(
                    bump_group_revision(
                        existing,
                        output_group=group_id,
                        workspace_id=self._state.workspace_id,
                        now=now,
                    )
                )
        return bumps

    def _scheduling_records(
        self, scheduling: AdmissionRecords
    ) -> "list[tuple[str, dict]]":
        """Return the trailing record pairs one admission commits."""

        records: list[tuple[str, dict]] = []
        for group in scheduling.groups:
            records.append((OUTPUT_GROUP_KIND, record_to_wire(group)))
        for intent in scheduling.intents:
            records.append((PENDING_INTENT_KIND, record_to_wire(intent)))
        for run in scheduling.runs:
            records.append((RUN_KIND, record_to_wire(run)))
        return records

    def _adopt_scheduling(self, scheduling: AdmissionRecords) -> None:
        """Adopt the scheduling records of one committed transaction."""

        for group in scheduling.groups:
            self._output_groups[group.outputGroup] = group
        for intent in scheduling.intents:
            self._pending_intents[intent.outputGroup] = intent
        for run in scheduling.runs:
            self._runs[run.runId] = run

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
        admission: "AdmissionRecords | None" = None,
    ) -> OkResponse:
        """Commit one processed request with its receipt.

        Admission and cancellation store durable receipts so a retry
        replays the stored answer and reuse with different content fails
        (SPEC.md sections 12.5 and 12.6). The transaction advances the
        commit sequence once even when it changed no record; it appends
        one notification only when records changed. The scheduling
        records of an admission ride along in the same atomic append.
        """

        scheduling = admission or AdmissionRecords()
        all_changed = sorted(
            set(
                [
                    *changed,
                    *(run_path(run.runId) for run in run_records),
                    *scheduling.changed_paths(),
                ]
            )
        )
        new_state = self._state.with_commit(self._state.commit_sequence + 1)
        response = self._append_transaction(
            request_id,
            method,
            payload_hash,
            new_state,
            changed=all_changed,
            result=result,
            trailing_records=[
                (RUN_KIND, record_to_wire(run)) for run in run_records
            ]
            + self._scheduling_records(scheduling),
        )
        for run in run_records:
            self._runs[run.runId] = run
        self._adopt_scheduling(scheduling)
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
        readable (SPEC.md sections 7.2 and 12.4). Artifacts a saved
        reference names but storage lacks are reported individually,
        live on every inspection, exactly as startup reported them
        (SPEC.md section 14, step 4). The unfiltered view also carries
        the startup recovery report, so a host can offer its explicit
        rerun and recovery actions.
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
        diagnostics.extend(
            model_to_wire(item)
            for item in missing_artifact_diagnostics(
                self._state.document,
                self._store.artifacts,
                self._registrations,
            )
        )
        diagnostics.sort(key=lambda item: (item.get("path", ""), item.get("code", "")))
        result = snapshot_result(
            self._state,
            self._runs,
            diagnostics,
            collection=parsed.collection,
            ids=parsed.ids,
            output_groups=self.output_group_status(),
        )
        if parsed.collection is None:
            result["recovery"] = self._recovery.wire()
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
    # Action registration (SPEC.md sections 7.2 and 12.6)
    # ------------------------------------------------------------------

    def _action_register(self, envelope: CommandEnvelope) -> CommandResponse:
        """Register one captured script as a trusted registration.

        The script token stands for bytes the service itself captured
        and stored; the environment must already be known to the service
        (SPEC.md section 12.6). Registration verifies the stored bytes
        and never executes the script: no run record is created, no
        process is started, and no permission is granted. The
        registration identifier covers the captured bytes and the
        resolved environment, so an edited script arrives as a new
        registration while an unchanged capture resolves to the
        registration it already has. A changed contract over the same
        capture advances that registration's immutable revision.
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
                    " document; create the workspace before registering"
                    " scripts",
                ),
            )
        environment = self._environments.resolve(parsed.environmentId)
        if environment is None:
            return _rejected(
                envelope.requestId,
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    "/payload/environmentId",
                    f"environment {parsed.environmentId!r} is not known to"
                    " this service",
                    hint="Resolve the environment with the service first;"
                    " an environment that went missing disables new"
                    " registrations, not saved projections.",
                ),
            )
        captured = self._tokens.redeem(parsed.scriptToken)
        if captured is None:
            return _rejected(
                envelope.requestId,
                _diagnostic(
                    DiagnosticCode.ARTIFACT,
                    "/payload/scriptToken",
                    "script token is unknown, expired, or scoped to"
                    " another workspace",
                    hint="Capture the script again through the host"
                    " registration operation; a restart withdraws every"
                    " outstanding token.",
                ),
            )
        try:
            script = register_script(self._store.artifacts, captured)
            validate_contract(parsed.contract)
        except MeltRegistrationError as error:
            return _rejected(envelope.requestId, *error.diagnostics)

        registration_id = registration_identity(
            script.artifact, environment.fingerprint
        )
        existing = self._registrations.get(registration_id)
        if existing is not None and _same_contract(existing, parsed.contract):
            # The identical capture is already registered: answer the
            # stored registration and commit nothing but the receipt.
            result = _registration_result(existing, environment, created=False)
            return self._commit_receipted(
                envelope.requestId, envelope.method, payload_hash, result
            )
        revision = 1 if existing is None else existing.revision + 1
        record = build_registration(
            registration_id=registration_id,
            revision=revision,
            script=script,
            environment_fingerprint=environment.fingerprint,
            contract=parsed.contract,
            created_at=_utc_now(),
        )
        record_path = registration_path(registration_id)
        entity_revisions = dict(self._state.entity_revisions)
        entity_revisions[record_path] = revision
        new_state = WorkspaceState(
            workspace_id=self._state.workspace_id,
            epoch=self._state.epoch,
            commit_sequence=self._state.commit_sequence + 1,
            # Registration touches no document record; the committed
            # document passes through unchanged.
            document=self._state.document,
            entity_revisions=entity_revisions,
            parameter_states=dict(self._state.parameter_states),
            incarnations=dict(self._state.incarnations),
        )
        active = tuple(record_to_wire(run) for run in active_run_records(self._runs))
        result = _registration_result(record, environment, created=True)
        response = self._append_transaction(
            envelope.requestId,
            envelope.method,
            payload_hash,
            new_state,
            changed=[record_path],
            result=result,
            leading_records=[
                (
                    SNAPSHOT_KIND,
                    _snapshot_wire(new_state, self._state.commit_sequence, active),
                )
            ],
            trailing_records=[(REGISTRATION_KIND, registration_to_wire(record))],
        )
        self._registrations[registration_id] = record
        self._action_registry = self._merge_registry()
        return response

    # ------------------------------------------------------------------
    # Control activation and runs (SPEC.md sections 12.6, 13.1)
    # ------------------------------------------------------------------

    def _control_activate(
        self, envelope: CommandEnvelope, *, origin: str = "user"
    ) -> CommandResponse:
        """Route one button activation through its binding.

        The activation needs the projection revision and the revision of
        every binding that connects the control to an action (SPEC.md
        section 12.6). A control without a binding is a reported no-op.
        A bound activation is admitted in one transaction that reserves
        the generation, captures the invocation snapshot, and stores the
        pending intent. A covering live grant dispatches the immediately
        due intent into a minted run; a missing grant answers the
        successful ``approvalRequired`` result with the captured intent
        the host approval binds to. That is an admission state, never a
        permission grant and never a claimed execution (SPEC.md sections
        12.6, 13.1, and 15.1).

        ``origin`` derives from the authenticated caller (SPEC.md
        sections 2.2 and 9.1): the host channel routes a press as a user
        event, while an agent adapter calls the same handler with a
        programmatic origin, and the admitted intent records which one
        spoke.
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

        # The event origin comes from the authenticated caller that
        # dispatched the activation (SPEC.md sections 2.2 and 9.1).
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
        return self._admit_action(
            action_id,
            origin=origin,
            debounce_ms=int(binding.get("debounceMs", 0)),
            request_id=envelope.requestId,
            method=envelope.method,
            payload_hash=payload_hash,
            context={"binding": binding_id},
            action_not_found_path=f"/bindings/{binding_id}/effect/action",
        )

    def _action_invoke(
        self, envelope: CommandEnvelope, *, origin: str = "user"
    ) -> CommandResponse:
        """Invoke one action manually (SPEC.md sections 12.6 and 13.1).

        A manual invocation reserves a generation just like a bound
        event and requires ``expected["actions/<id>"]``. It carries no
        debounce: an admitted and granted invocation dispatches in the
        admitting transaction itself. Admission deduplicates both
        manual and bound invocations through the request receipt.

        ``origin`` derives from the authenticated caller (SPEC.md
        sections 2.2 and 9.1): the harness and the native host speak
        for the local user, while an agent adapter invokes the same
        handler as a programmatic change, and the admitted intent
        records which one spoke.
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
                    " document; create the workspace before invoking an"
                    " action",
                ),
            )
        record = entity_path("actions", parsed.actionId)
        diagnostics = expectation_diagnostics(
            self._state, dict(envelope.expected or {}), [record]
        )
        if diagnostics:
            return _rejected(envelope.requestId, *sorted_diagnostics(diagnostics))
        return self._admit_action(
            parsed.actionId,
            origin=origin,
            debounce_ms=0,
            request_id=envelope.requestId,
            method=envelope.method,
            payload_hash=payload_hash,
            context={},
            action_not_found_path="/payload/actionId",
        )

    # ------------------------------------------------------------------
    # Admission, debounce, and generations (SPEC.md sections 7.3, 9.3, 13.1)
    # ------------------------------------------------------------------

    def _captured_parameters(
        self,
        action: Mapping,
        new_states: "Mapping[str, ParameterStateValue] | None" = None,
    ) -> dict:
        """Return the parameter values an admission of this action captures.

        A user action captures all mapped parameter values when its
        pending intent is updated (SPEC.md section 9.3); the captured
        values and revisions ride with the intent, so the approval shows
        exactly what would run. A mutation-triggered admission passes
        the states its own transaction is committing, so coalesced
        bindings capture the final values (section 13.1).
        """

        captured: dict = {}
        for name, mapping in sorted(action.get("parameters", {}).items()):
            parameter_id = mapping["parameter"]
            stored = None
            if new_states is not None and parameter_id in new_states:
                stored = new_states[parameter_id]
            else:
                stored = self._state.parameter_states.get(parameter_id)
            captured[name] = {
                "parameter": parameter_id,
                "value": None if stored is None else stored.value,
                "stateRevision": 0 if stored is None else stored.revision,
            }
        return captured

    def _registration_facts(self, registration_id: str, registration) -> "tuple[str | None, str | None]":
        """Return the admission facts of one registration.

        A committed registration record is authoritative; a fixture
        registry entry may cite the same facts so an admission against
        it can capture its snapshot (SPEC.md section 7.3).
        """

        record = self._registrations.get(registration_id)
        if record is not None:
            return record.script.artifact, record.environmentFingerprint
        return (
            registration.script_artifact,
            registration.environment_fingerprint,
        )

    def _admission_snapshot(
        self,
        action_id: str,
        action: Mapping,
        registration,
        group_revision: int,
        generation: int,
        *,
        new_states: "Mapping[str, ParameterStateValue] | None" = None,
        action_revision: "int | None" = None,
    ) -> "InvocationSnapshot | Diagnostic":
        """Capture the invocation snapshot of one admission (section 7.3).

        Returns the snapshot, or the diagnostic that names the missing
        fact. The registration must expose its script hash and its
        resolved environment fingerprint: a committed registration
        always carries both, and a fixture registry entry may cite them.
        ``action_revision`` overrides the committed revision for an
        admission planned inside a transaction that replaces the action.
        """

        registration_id = action["implementation"]["registrationId"]
        script_artifact, fingerprint = self._registration_facts(
            registration_id, registration
        )
        if script_artifact is None or fingerprint is None:
            return _diagnostic(
                DiagnosticCode.REGISTRATION,
                f"/actions/{action_id}/implementation/registrationId",
                f"registration {registration_id!r} carries no captured"
                " script hash or environment fingerprint, so an admitted"
                " run could not snapshot it",
                hint="Register the script through action.register, or"
                " cite its admission facts in the trusted registry.",
            )
        captured = self._captured_parameters(action, new_states)
        return InvocationSnapshot(
            action_id=action_id,
            action_revision=(
                self._state.current_revision(entity_path("actions", action_id))
                if action_revision is None
                else action_revision
            ),
            registration_id=registration_id,
            registration_revision=registration.revision,
            script_artifact=script_artifact,
            environment_fingerprint=fingerprint,
            output_group=action["outputGroup"],
            output_group_revision=group_revision,
            generation=generation,
            parameters={
                name: CapturedParameter(
                    value=item["value"], revision=item["stateRevision"]
                )
                for name, item in captured.items()
            },
            parameter_ids={
                name: item["parameter"] for name, item in captured.items()
            },
            inputs={
                name: dict(reference)
                for name, reference in sorted(action.get("inputs", {}).items())
            },
        )

    def _plan_triggered_admissions(
        self,
        plan: MutationPlan,
        triggers: "Mapping[str, int]",
        now_epoch: float,
        group_bumps: "Sequence[OutputGroupRecord]" = (),
    ) -> AdmissionRecords:
        """Plan the admissions one committed user change triggers.

        Several bindings that target one action arrive coalesced into
        one entry, so one intent with the final parameter values of the
        transaction is admitted per action (SPEC.md section 13.1). The
        records commit with the mutation in one transaction, and every
        captured revision is the revision this transaction writes.
        """

        records = AdmissionRecords()
        group_view = {
            **self._output_groups,
            **{bump.outputGroup: bump for bump in group_bumps},
        }
        action_revisions = {
            path[len("actions/") :]: revision
            for path, revision in plan.new_revisions(self._state).items()
            if path.startswith("actions/")
        }
        for action_id in sorted(triggers):
            self._plan_one_admission(
                plan.candidate["actions"][action_id],
                action_id,
                debounce_ms=triggers[action_id],
                origin="user",
                now_epoch=now_epoch,
                records=records,
                new_states=plan.new_states,
                group_view=group_view,
                action_revision=action_revisions.get(action_id),
            )
        return records

    def _plan_one_admission(
        self,
        action: Mapping,
        action_id: str,
        *,
        debounce_ms: int,
        origin: str,
        now_epoch: float,
        records: AdmissionRecords,
        new_states: "Mapping[str, ParameterStateValue] | None" = None,
        group_view: "Mapping[str, OutputGroupRecord] | None" = None,
        action_revision: "int | None" = None,
    ) -> "PendingIntentRecord | None":
        """Plan one admission into the given record set.

        Reserves the generation of the action's output group, expires
        the pending intent the new request replaces, captures the
        invocation snapshot, checks authority, and appends the pending
        intent plus, for an immediately due granted admission, the run
        record. The caller commits every appended record atomically.
        """

        now = timestamp_of(now_epoch)
        workspace_id = self._state.workspace_id
        group_id = action["outputGroup"]
        groups = self._output_groups if group_view is None else group_view
        group, generation = reserve_generation(
            groups.get(group_id), group_id, workspace_id, now
        )
        records.groups.append(group)
        entry = self._action_registry.entry(
            action["implementation"]["registrationId"]
        )
        if entry is None:
            # The registration left the trusted registry, so the
            # trigger admits nothing. The generation stays reserved: a
            # qualifying event reserved it, and no older run may still
            # claim to be current (SPEC.md section 9.3).
            return None
        snapshot_or_error = self._admission_snapshot(
            action_id,
            action,
            entry,
            group.revision,
            generation,
            new_states=new_states,
            action_revision=action_revision,
        )
        if isinstance(snapshot_or_error, Diagnostic):
            # The registration carries no admission facts, so the
            # trigger admits nothing for the same reason.
            return None
        snapshot = snapshot_or_error
        replaced = self._pending_intents.get(group_id)
        if replaced is not None and replaced.status in (
            "pending",
            "approvalRequired",
        ):
            records.intents.append(
                expire_intent(replaced, reason="replaced", now=now)
            )
        intent_ref = mint_intent_id()
        decision = authority_at_admission(
            self._grants,
            required_permission=entry.required_permission,
            registration_id=snapshot.registration_id,
            registration_revision=snapshot.registration_revision,
            action_id=snapshot.action_id,
            action_revision=snapshot.action_revision,
            output_group=snapshot.output_group,
            output_group_revision=snapshot.output_group_revision,
            generation=snapshot.generation,
            intent_id=intent_ref,
        )
        due_at_epoch = now_epoch + debounce_ms / 1000
        granted = decision.granted and debounce_ms == 0
        status = "dispatched" if granted else (
            "pending" if decision.granted else "approvalRequired"
        )
        intent = build_pending_intent(
            snapshot,
            workspace_id=workspace_id,
            intent_ref=intent_ref,
            debounce_ms=debounce_ms,
            due_at_epoch=due_at_epoch,
            status=status,
            origin=origin,
            reason="" if decision.granted else decision.reason,
            now=now,
        )
        records.intents.append(intent)
        if granted:
            records.runs.append(
                build_run_record(
                    intent,
                    run_id=mint_run_id(),
                    permission_decision=decision.grant_id,
                    now=now,
                )
            )
        return intent

    def _admit_action(
        self,
        action_id: str,
        *,
        origin: str,
        debounce_ms: int,
        request_id: str,
        method: str,
        payload_hash: str,
        context: dict,
        action_not_found_path: str,
    ) -> CommandResponse:
        """Admit one intent of an action (SPEC.md sections 12.6 and 13.1).

        The admitting transaction reserves the generation of the output
        group, captures the full invocation snapshot, and stores the
        pending intent. A covering live grant dispatches an immediately
        due intent into a minted run record in the same transaction;
        without one the successful ``approvalRequired`` result carries
        the captured intent the host approval binds to. Either way the
        reservation already makes older runs ineligible for current
        publication (section 9.3).
        """

        document = self._state.document
        action = document["actions"].get(action_id)
        if action is None:
            return _rejected(
                request_id,
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    action_not_found_path,
                    f"action {action_id!r} does not exist",
                ),
            )
        registration = self._action_registry.entry(
            action["implementation"]["registrationId"]
        )
        if registration is None:
            return _rejected(
                request_id,
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
        facts_error = self._admission_facts_error(action_id, action, registration)
        if facts_error is not None:
            return _rejected(request_id, facts_error)
        now_epoch = self._clock()
        records = AdmissionRecords()
        intent = self._plan_one_admission(
            action,
            action_id,
            debounce_ms=debounce_ms,
            origin=origin,
            now_epoch=now_epoch,
            records=records,
        )
        run = records.runs[0] if records.runs else None
        result = {
            **context,
            "status": (
                "approvalRequired" if intent.status == "approvalRequired"
                else "granted"
            ),
            "origin": origin,
            "action": action_id,
            "requiredPermission": registration.required_permission,
            "intent": self._intent_result(intent),
            "run": None if run is None else self._run_summary(run),
        }
        if intent.status == "approvalRequired":
            result["reason"] = intent.reason
        else:
            result["permissionDecision"] = (
                None if run is None else run.permissionDecision
            )
        return self._commit_receipted(
            request_id,
            method,
            payload_hash,
            result,
            admission=records,
        )

    def _admission_facts_error(
        self, action_id: str, action: Mapping, registration
    ) -> "Diagnostic | None":
        """Return the diagnostic for missing admission facts, or None."""

        registration_id = action["implementation"]["registrationId"]
        script_artifact, fingerprint = self._registration_facts(
            registration_id, registration
        )
        if script_artifact is None or fingerprint is None:
            return _diagnostic(
                DiagnosticCode.REGISTRATION,
                f"/actions/{action_id}/implementation/registrationId",
                f"registration {registration_id!r} carries no captured"
                " script hash or environment fingerprint, so no run"
                " could snapshot it",
                hint="Register the script through action.register, or"
                " cite its admission facts in the trusted registry.",
            )
        return None

    def _intent_result(self, intent: PendingIntentRecord) -> dict:
        """The captured intent every admission answer carries."""

        captured: dict = {}
        for name in sorted(intent.parameters):
            captured[name] = {
                "parameter": intent.parameterIds.get(name, name),
                "value": intent.parameters[name].value,
                "stateRevision": intent.parameters[name].revision,
            }
        return {
            "intentRef": intent.intentRef,
            "actionId": intent.actionId,
            "actionRevision": intent.actionRevision,
            "registrationId": intent.registrationId,
            "registrationRevision": intent.registrationRevision,
            "scriptArtifact": intent.scriptArtifact,
            "environmentFingerprint": intent.environmentFingerprint,
            "outputGroup": intent.outputGroup,
            "outputGroupRevision": intent.outputGroupRevision,
            "generation": intent.generation,
            "capturedParameters": captured,
            "capturedInputs": {
                name: dict(reference)
                for name, reference in sorted(intent.inputs.items())
            },
            "debounceMs": intent.debounceMs,
            "dueAt": intent.dueAt,
            "status": intent.status,
            "origin": intent.origin,
        }

    def _run_summary(self, run: RunRecord) -> dict:
        """The bounded run view an admission answer carries."""

        return {
            "runId": run.runId,
            "actionId": run.actionId,
            "outputGroup": run.outputGroup,
            "outputGroupRevision": run.outputGroupRevision,
            "generation": run.generation,
            "state": run.state,
            "disposition": run.disposition,
            "permissionDecision": run.permissionDecision,
        }

    def _admission_summary(self, intent: PendingIntentRecord) -> dict:
        """The admission view a mutation answer reports."""

        summary = self._intent_result(intent)
        summary["origin"] = intent.origin
        return summary

    # ------------------------------------------------------------------
    # Debounce dispatch and launch (SPEC.md sections 9.3, 13.1, 13.2)
    # ------------------------------------------------------------------

    def dispatch_due_intents(
        self, now_epoch: "float | None" = None
    ) -> list[dict]:
        """Fire every due debounce timer (SPEC.md sections 9.3 and 13.1).

        Each due intent dispatches in its own transaction: definitions
        are rechecked and authority re-asked at dispatch time, a stale
        intent expires, an uncovered one returns to its approval state,
        and a covered one mints its run record from the captured
        snapshot. The values a run receives are the captured ones; no
        workspace change reaches an admitted run's inputs.
        """

        now_epoch = self._clock() if now_epoch is None else now_epoch
        results: list[dict] = []
        # due_intents reads a stable view: each dispatch replaces at
        # most the intent it dispatches, so the walk stays correct.
        for intent in due_intents(self._pending_intents, now_epoch):
            outcome = self._dispatch_one_intent(
                self._pending_intents[intent.outputGroup], now_epoch
            )
            results.append(outcome)
        return results

    def _intent_definition_error(
        self, intent: PendingIntentRecord
    ) -> "Diagnostic | None":
        """Recheck the definitions one due intent captured."""

        def _group_revision(group_id: str) -> int:
            group = self._output_groups.get(group_id)
            return 0 if group is None else group.revision

        return intent_definition_error(
            document=self._state.document,
            action_revision_of=lambda action_id: self._state.current_revision(
                entity_path("actions", action_id)
            ),
            registry_entry_of=self._action_registry.entry,
            group_revision_of=_group_revision,
            intent=intent,
        )

    def _dispatch_one_intent(
        self, intent: PendingIntentRecord, now_epoch: float
    ) -> dict:
        """Dispatch one due intent in one durable transaction."""

        now = timestamp_of(now_epoch)
        records = AdmissionRecords()
        outcome: dict = {
            "intentRef": intent.intentRef,
            "actionId": intent.actionId,
            "outputGroup": intent.outputGroup,
            "generation": intent.generation,
        }
        stale = self._intent_definition_error(intent)
        if stale is not None:
            expired = expire_intent(
                intent, reason=stale.message, now=now
            )
            records.intents.append(expired)
            outcome.update(
                {
                    "status": "expired",
                    "reason": stale.message,
                    "diagnostic": model_to_wire(stale),
                }
            )
            self._commit_scheduling(records)
            return outcome
        entry = self._action_registry.entry(intent.registrationId)
        decision = authority_at_admission(
            self._grants,
            required_permission=entry.required_permission,
            registration_id=intent.registrationId,
            registration_revision=intent.registrationRevision,
            action_id=intent.actionId,
            action_revision=intent.actionRevision,
            output_group=intent.outputGroup,
            output_group_revision=intent.outputGroupRevision,
            generation=intent.generation,
            intent_id=intent.intentRef,
        )
        if not decision.granted:
            # The timer fired without authority: the request waits for
            # its host approval again and reports why (SPEC.md sections
            # 12.6 and 13.1).
            waiting = intent.model_copy(
                update={
                    "status": "approvalRequired",
                    "reason": decision.reason,
                    "updatedAt": now,
                }
            )
            records.intents.append(waiting)
            outcome.update(
                {
                    "status": "approvalRequired",
                    "reason": decision.reason,
                }
            )
            self._commit_scheduling(records)
            return outcome
        run = build_run_record(
            intent,
            run_id=mint_run_id(),
            permission_decision=decision.grant_id,
            now=now,
        )
        records.intents.append(
            intent.model_copy(
                update={"status": "dispatched", "updatedAt": now}
            )
        )
        records.runs.append(run)
        outcome.update(
            {
                "status": "dispatched",
                "permissionDecision": decision.grant_id,
                "run": self._run_summary(run),
            }
        )
        self._commit_scheduling(records)
        return outcome

    def _commit_scheduling(self, records: AdmissionRecords) -> None:
        """Commit scheduling records alone, in one atomic transaction.

        Timer dispatch, run-state transitions, and restart marking share
        this path: no command envelope drives them, so no receipt is
        stored, and the transaction advances the commit sequence once
        with one notification for every changed record.
        """

        if records.empty:
            return
        new_state = self._state.with_commit(self._state.commit_sequence + 1)
        changed = records.changed_paths()
        notification = {
            "recordType": "changeNotification",
            "schemaVersion": RECORD_SCHEMA_VERSION,
            "workspaceId": new_state.workspace_id,
            "epoch": new_state.epoch,
            "commitSequence": new_state.commit_sequence,
            "changedRecords": changed,
        }
        parse_record(notification)
        payload = [(NOTIFICATION_KIND, notification)]
        payload.extend(self._scheduling_records(records))
        sequence, _ = self._store.append_records(payload)
        if sequence != new_state.commit_sequence:
            raise RuntimeError(
                f"scheduling commit landed at sequence {sequence} instead"
                f" of the predicted {new_state.commit_sequence}; the"
                " single-writer sequence prediction is broken"
            )
        self._state = new_state
        self._notifications.append(notification)
        self._adopt_scheduling(records)

    def _mark_restart_interrupted(
        self,
    ) -> "tuple[list[RunRecord], list[PendingIntentRecord]]":
        """Mark active jobs and pending timers interrupted (SPEC.md 14).

        Startup step 5 and section 9.3: a restart never executes or
        resumes uncertain Python effects. Queued or running runs and
        timer-running intents become ``interrupted`` in one durable
        transaction; an approval-required request keeps waiting for its
        host approval, because no work of its own was in flight.
        Returns the marked records, so the recovery report can name
        what the restart interrupted.
        """

        now = _utc_now()
        records = AdmissionRecords()
        marked_runs: list[RunRecord] = []
        marked_intents: list[PendingIntentRecord] = []
        for run_id in sorted(self._runs):
            run = self._runs[run_id]
            if run.state in ACTIVE_RUN_STATES:
                interrupted = run.model_copy(
                    update={"state": "interrupted", "updatedAt": now}
                )
                records.runs.append(interrupted)
                marked_runs.append(interrupted)
        for group in sorted(self._pending_intents):
            intent = self._pending_intents[group]
            if intent.status == "pending":
                interrupted = interrupt_intent(intent, now=now)
                records.intents.append(interrupted)
                marked_intents.append(interrupted)
        self._commit_scheduling(records)
        return marked_runs, marked_intents

    def _startup_recovery(
        self, corrupt_records: "Sequence[CorruptRecordNote]"
    ) -> RecoveryReport:
        """Run the startup recovery pass of SPEC.md section 14.

        Steps 4 through 7 on top of the restored records: mark previous
        active jobs and pending timers interrupted (step 5), report
        every artifact a saved reference names but storage lacks, each
        one individually (step 4), reconcile the ``staging/<run-id>``
        trees against the committed publication receipts (step 6), and
        record the explicit rerun and recovery actions a host may offer
        (step 7). The pass executes no project code and repairs
        nothing: damage is reported, files are preserved, and
        unacknowledged work stays available for an explicit retry
        (SPEC.md section 18.4).
        """

        marked_runs, marked_intents = self._mark_restart_interrupted()
        missing = missing_artifact_diagnostics(
            self._state.document, self._store.artifacts, self._registrations
        )
        verdicts = reconcile_staging(
            self._store.layout, self._runs, self._publications
        )
        offers = recovery_offers(marked_intents, verdicts, self._runs)
        return RecoveryReport(
            corrupt_records=tuple(corrupt_records),
            missing_artifacts=tuple(missing),
            interrupted_runs=tuple(run.runId for run in marked_runs),
            interrupted_intents=tuple(
                intent.intentRef for intent in marked_intents
            ),
            staging=tuple(verdicts),
            offered_actions=tuple(offers),
        )

    # ------------------------------------------------------------------
    # Launch (SPEC.md sections 13.1 and 13.2)
    # ------------------------------------------------------------------

    def launch_admitted_run(self, run_id: str) -> RunLaunchOutcome:
        """Launch one admitted run through its fresh process.

        The pre-launch rechecks run immediately before the process
        starts: registration availability, the resolved environment
        fingerprint against the admission snapshot, and the permission
        grant (SPEC.md sections 13.1 and 15.1). A refused recheck never
        executes and never reports failure as successful completion; a
        revoked grant additionally requests cancellation of the run.
        The child environment is credential-free by construction of the
        launcher (section 15.1).
        """

        run = self._runs.get(run_id)
        if run is None:
            raise permission_error(
                f"run {run_id!r} does not exist",
                "/runId",
                code=DiagnosticCode.REFERENCE,
            )
        if run.state != "queued":
            return RunLaunchOutcome(
                run_id=run_id,
                launched=False,
                state=run.state,
                refusal=(
                    _diagnostic(
                        DiagnosticCode.CONFLICT,
                        "/runId",
                        f"run {run_id!r} is {run.state!r}; only a queued"
                        " run can launch",
                    ),
                ),
            )
        running = [
            other for other in self._runs.values() if other.state == "running"
        ]
        if running:
            return RunLaunchOutcome(
                run_id=run_id,
                launched=False,
                state=run.state,
                refusal=(
                    _diagnostic(
                        DiagnosticCode.CONFLICT,
                        "/runId",
                        f"workspace already executes run"
                        f" {sorted(running)[0].runId!r}; only one project"
                        " execution runs at a time per workspace"
                        " (SPEC.md section 13.1)",
                    ),
                ),
            )
        refusal = self._launch_recheck_error(run)
        if refusal is not None:
            return RunLaunchOutcome(
                run_id=run_id,
                launched=False,
                state=run.state,
                refusal=(refusal,),
            )
        decision = self.execution_authority_for_run(run_id, at="launch")
        if not decision.granted:
            if revocation_blocks(decision):
                cancelled = run.model_copy(
                    update={
                        "state": "cancelled",
                        "updatedAt": _utc_now(),
                    }
                )
                self._commit_scheduling(AdmissionRecords(runs=[cancelled]))
                return RunLaunchOutcome(
                    run_id=run_id,
                    launched=False,
                    state="cancelled",
                    refusal=(
                        _diagnostic(
                            DiagnosticCode.PERMISSION,
                            "/permissionDecision",
                            f"the grant that admitted run {run_id!r} was"
                            " revoked; the launch was refused and the run"
                            " was cancelled (SPEC.md sections 13.5 and"
                            " 15.1)",
                        ),
                    ),
                )
            return RunLaunchOutcome(
                run_id=run_id,
                launched=False,
                state=run.state,
                refusal=(
                    _diagnostic(
                        DiagnosticCode.PERMISSION,
                        "/permissionDecision",
                        f"run {run_id!r} lost its execution permission:"
                        f" {decision.reason}; the run waits for a host"
                        " approval (SPEC.md sections 13.1 and 15.1)",
                    ),
                ),
            )

        registration = self._registrations[run.registrationId]
        layout = self._store.layout
        staging = layout.staging_directory(run.runId)
        try:
            script_bytes = self._store.artifacts.read(registration.script.artifact)
        except OSError as error:
            return RunLaunchOutcome(
                run_id=run_id,
                launched=False,
                state=run.state,
                refusal=(
                    _diagnostic(
                        DiagnosticCode.ARTIFACT,
                        "/scriptArtifact",
                        f"the captured script artifact of run {run_id!r}"
                        f" is unreadable: {error}",
                    ),
                ),
            )
        inputs: dict[str, str] = {}
        for name, reference in sorted(run.inputs.items()):
            version = (
                (self._state.document or {})
                .get("sources", {})
                .get(reference.sourceId, {})
                .get("versions", {})
                .get(str(reference.version))
            )
            if version is None:
                return RunLaunchOutcome(
                    run_id=run_id,
                    launched=False,
                    state=run.state,
                    refusal=(
                        _diagnostic(
                            DiagnosticCode.REFERENCE,
                            f"/inputs/{name}",
                            f"captured input {name!r} cites source"
                            f" {reference.sourceId!r} version"
                            f" {reference.version}, which the saved"
                            " document no longer holds",
                        ),
                    ),
                )
            inputs[name] = str(
                self._store.artifacts.path_for(version["artifact"])
            )
        script_path = materialize_script(staging, script_bytes)
        manifest = build_manifest(
            run_id=run.runId,
            workspace_id=self._state.workspace_id,
            action_id=run.actionId,
            script_path=script_path,
            parameters={
                name: captured.value
                for name, captured in run.parameters.items()
            },
            inputs=inputs,
            outputs=dict(registration.outputs),
            staging_directory=staging,
        )
        write_run_manifest(staging, manifest)
        self._transition_run(run, "running")
        try:
            result = launch_run(
                manifest, log_directory=layout.log_directory(run.runId)
            )
        except (MeltLaunchError, OSError) as error:
            failed = self._runs[run_id].model_copy(
                update={"state": "failed", "updatedAt": _utc_now()}
            )
            self._commit_scheduling(AdmissionRecords(runs=[failed]))
            return RunLaunchOutcome(
                run_id=run_id,
                launched=False,
                state="failed",
                refusal=(
                    _diagnostic(
                        DiagnosticCode.REFERENCE,
                        "/manifest",
                        f"the launch of run {run_id!r} failed"
                        f" operationally: {error}",
                    ),
                ),
            )
        state = "succeeded" if result.complete else "failed"
        finished = self._runs[run_id].model_copy(
            update={"state": state, "updatedAt": _utc_now()}
        )
        self._commit_scheduling(AdmissionRecords(runs=[finished]))
        return RunLaunchOutcome(
            run_id=run_id,
            launched=True,
            state=state,
            exit_code=result.exit_code,
            staged=result.staged,
            manifest_path=str(result.manifest_path),
            stdout_path=str(result.stdout_path),
            stderr_path=str(result.stderr_path),
        )

    def _launch_recheck_error(self, run: RunRecord) -> "Diagnostic | None":
        """Recheck registration and environment before one launch."""

        entry = self._action_registry.entry(run.registrationId)
        if entry is None:
            return _diagnostic(
                DiagnosticCode.REGISTRATION,
                "/registrationId",
                f"registration {run.registrationId!r} is no longer"
                " available; the run cannot launch (SPEC.md section 13.1)",
                hint="Register the script again; environment repair and"
                " re-registration are separate operations.",
            )
        if entry.revision != run.registrationRevision:
            return _diagnostic(
                DiagnosticCode.REGISTRATION,
                "/registrationId",
                f"registration {run.registrationId!r} moved to revision"
                f" {entry.revision}; the run captured revision"
                f" {run.registrationRevision} (SPEC.md section 7.3)",
            )
        registration = self._registrations.get(run.registrationId)
        if registration is None:
            return _diagnostic(
                DiagnosticCode.REGISTRATION,
                "/registrationId",
                f"registration {run.registrationId!r} holds no captured"
                " script in this workspace, so no fresh process could"
                " execute it",
                hint="Only a service-registered script can launch;"
                " a fixture registry entry cannot run.",
            )
        if registration.environmentFingerprint != run.environmentFingerprint:
            return _diagnostic(
                DiagnosticCode.REGISTRATION,
                "/environmentFingerprint",
                f"the registration now resolves environment"
                f" {registration.environmentFingerprint!r}, but the run"
                f" captured {run.environmentFingerprint!r}"
                " (SPEC.md section 7.3)",
            )
        resolved = local_environment().fingerprint
        if resolved != run.environmentFingerprint:
            return _diagnostic(
                DiagnosticCode.REGISTRATION,
                "/environmentFingerprint",
                f"the resolved project environment is {resolved!r}, but"
                f" the run captured {run.environmentFingerprint!r};"
                " environment repair is a separate operation"
                " (SPEC.md sections 13.1 and 14)",
            )
        return None

    def _transition_run(self, run: RunRecord, state: str) -> None:
        """Record one run state transition durably."""

        updated = run.model_copy(
            update={"state": state, "updatedAt": _utc_now()}
        )
        self._commit_scheduling(AdmissionRecords(runs=[updated]))

    def queued_run_ids(self) -> list[str]:
        """The queued runs in admission order, oldest generation first."""

        return [
            run.runId
            for run in sorted(
                self._runs.values(),
                key=lambda item: (item.createdAt, item.runId),
            )
            if run.state == "queued"
        ]

    def launch_next_run(self) -> "RunLaunchOutcome | None":
        """Launch the next queued run, if one exists.

        The harness drives the single execution slot of the workspace
        through this entry point: it launches at most one queued run
        per call and reports a refusal unchanged.
        """

        queued = self.queued_run_ids()
        if not queued:
            return None
        return self.launch_admitted_run(queued[0])

    # ------------------------------------------------------------------
    # Atomic publication (SPEC.md sections 13.3, 13.4, 13.5)
    # ------------------------------------------------------------------

    def publish_run(self, run_id: str) -> PublicationOutcome:
        """Publish one completed run inside one owning transaction.

        The six steps of SPEC.md section 13.4 run in order: verify the
        declared staged files independently, finalize the immutable
        artifact paths, recheck every eligibility condition of section
        13.3 — including the publication permission gate — inside the
        transaction, allocate the output source versions through the
        service-only run-provenance hook, update every target reference
        together, and store the run disposition with the publication
        receipt before the single commit acknowledges anything.

        A missing or invalid output rejects the entire publication, so
        the previous table and plot remain together. A verified but
        obsolete run archives as historical output with a superseded
        disposition; it never overwrites the current sources (section
        13.3). An operational failure while finalizing the artifacts —
        a full disk above all — aborts the attempt before any
        transaction: nothing commits, the run keeps its staged outputs
        and its eligibility, and a retry after the fault clears
        reconciles them (SPEC.md section 18.4).
        """

        run = self._runs.get(run_id)
        if run is None:
            raise permission_error(
                f"run {run_id!r} does not exist",
                "/runId",
                code=DiagnosticCode.REFERENCE,
            )
        if run.disposition != "none":
            # The publication of this run already concluded; report the
            # stored outcome without committing anything new.
            return PublicationOutcome(
                run_id=run_id,
                outcome=run.disposition,
                reason="the publication of this run already concluded",
                outputs={
                    name: {"sourceId": ref.sourceId, "version": ref.version}
                    for name, ref in sorted(run.outputs.items())
                },
                commit_sequence=self._state.commit_sequence,
            )
        if run.state != "succeeded":
            return PublicationOutcome(
                run_id=run_id,
                outcome=NOT_PUBLISHABLE,
                refusal=(
                    _diagnostic(
                        DiagnosticCode.CONFLICT,
                        "/runId",
                        f"run {run_id!r} is {run.state!r}; only a"
                        " successful process with valid, complete outputs"
                        " can publish (SPEC.md sections 7.3 and 13.3)",
                    ),
                ),
                commit_sequence=self._state.commit_sequence,
            )
        registration = self._registrations.get(run.registrationId)
        if registration is None:
            return PublicationOutcome(
                run_id=run_id,
                outcome=NOT_PUBLISHABLE,
                refusal=(
                    _diagnostic(
                        DiagnosticCode.REGISTRATION,
                        "/registrationId",
                        f"registration {run.registrationId!r} holds no"
                        " captured script in this workspace, so its"
                        " declared outputs cannot be verified",
                    ),
                ),
                commit_sequence=self._state.commit_sequence,
            )

        # Step 1: verify every declared output against the actual files.
        staging = self._store.layout.staging_directory(run_id)
        verified, errors = verify_staged_outputs(
            staging,
            registration.outputs,
            staged_byte_limit=self._limits.staged_output_bytes_per_run,
        )
        if errors:
            return self._conclude_publication(
                run,
                outcome=REJECTED,
                reason=errors[0].message,
                refusal=errors,
            )

        # Step 2: synchronize the staged files into their immutable
        # content-addressed paths. A crash from here on can leave
        # unreferenced files, never referenced missing ones (SPEC.md
        # sections 13.4 and 14). An operational write failure — a full
        # disk above all — aborts the attempt before any transaction:
        # nothing commits, the run stays eligible, and a retry after
        # the fault clears reconciles the same staged outputs (SPEC.md
        # section 18.4).
        try:
            for name in sorted(verified):
                self._store.artifacts.write(verified[name].payload)
        except OSError as error:
            return PublicationOutcome(
                run_id=run_id,
                outcome=NOT_PUBLISHABLE,
                refusal=(
                    _diagnostic(
                        DiagnosticCode.ARTIFACT,
                        "/outputs",
                        f"finalizing the staged outputs of run"
                        f" {run_id!r} failed operationally: {error}",
                        hint="Free disk space and retry the publication;"
                        " nothing was committed and the previous"
                        " results remain together (SPEC.md section"
                        " 18.4).",
                    ),
                ),
                commit_sequence=self._state.commit_sequence,
            )

        # Step 3: recheck every eligibility condition inside the owning
        # transaction (SPEC.md sections 13.3 and 13.5).
        document = self._state.document
        action = document["actions"].get(run.actionId)
        entry = self._action_registry.entry(run.registrationId)
        group = self._output_groups.get(run.outputGroup)
        decision = self.execution_authority_for_run(run_id, at="publication")
        if not decision.granted:
            blocked = revocation_blocks(decision)
            concluded = run
            if blocked:
                # Revocation requests cancellation of the owning run;
                # an active run moves to cancelled inside the same
                # publication transaction (SPEC.md sections 13.3 and
                # 13.5).
                concluded = self._run_with_cancellation(run)
            refusal = _diagnostic(
                DiagnosticCode.PERMISSION,
                "/permissionDecision",
                f"the publication of run {run_id!r} was blocked:"
                f" {decision.reason}"
                + (
                    "; revocation requested cancellation of the run"
                    if blocked
                    else ""
                ),
                hint="A host permission control can grant execution"
                " authority again; a blocked publication never claims"
                " the run never executed (SPEC.md sections 13.5 and"
                " 15.1).",
            )
            return self._conclude_publication(
                concluded,
                outcome=REJECTED,
                reason=refusal.message,
                refusal=(refusal,),
                cancellation_requested=blocked,
            )
        targets_broken = (
            None
            if action is None
            else target_ineligibility(document, run.actionId, action)
        )
        if targets_broken is not None:
            return self._conclude_publication(
                run,
                outcome=REJECTED,
                reason=targets_broken.message,
                refusal=(targets_broken,),
            )
        obsolete = obsolescence_error(
            document=document,
            run=run,
            action=action,
            registration_entry=entry,
            group=group,
            parameter_states=self._state.parameter_states,
            action_revision_of=lambda action_id: self._state.current_revision(
                entity_path("actions", action_id)
            ),
        )
        become_current = obsolete is None

        # Step 4: allocate the source versions, attach run provenance,
        # and update every target reference together.
        sources = allocate_output_sources(
            document, self._runs, run.actionId, verified
        )
        candidate, references = append_output_versions(
            document,
            run_id,
            sources,
            verified,
            become_current=become_current,
        )
        superseded_currents: list[RunRecord] = []
        if become_current:
            candidate = retarget_projections(candidate, action, references)
            now = _utc_now()
            superseded_currents = [
                self._runs[run_id].model_copy(
                    update={"disposition": SUPERSEDED, "updatedAt": now}
                )
                for run_id in sorted(self._runs)
                if run_id != run.runId
                and self._runs[run_id].outputGroup == run.outputGroup
                and self._runs[run_id].disposition == CURRENT
            ]
        compile_result = compile_document(
            CompileRequest(
                document=candidate,
                source_catalog=self._publication_catalog(verified),
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
        if not compile_result.ok:
            # The candidate with the new references does not compile:
            # nothing commits, and the finalized files stay
            # unreferenced until recovery reconciles them (SPEC.md
            # section 13.4).
            return self._conclude_publication(
                run,
                outcome=REJECTED,
                reason=compile_result.diagnostics[0].message,
                refusal=tuple(compile_result.diagnostics),
            )
        return self._conclude_publication(
            run,
            outcome=CURRENT if become_current else SUPERSEDED,
            reason="" if become_current else obsolete.message,
            candidate=candidate,
            outputs=references,
            verified=verified,
            superseded_currents=superseded_currents,
        )

    def _run_with_cancellation(self, run: RunRecord) -> RunRecord:
        """Return one run record with cancellation requested.

        Cancellation stops scheduling and signals the owned process
        group; a run that already reached a terminal state keeps it,
        because a finished process is reported honestly (SPEC.md
        section 13.5).
        """

        if run.state not in ACTIVE_RUN_STATES:
            return run
        return run.model_copy(
            update={"state": "cancelled", "updatedAt": _utc_now()}
        )

    def _publication_catalog(
        self, verified: "Mapping[str, VerifiedOutput]"
    ) -> SourceCatalog:
        """Return the compile catalog with the verified outputs merged."""

        return SourceCatalog(
            {
                **self._source_catalog.artifacts,
                **{
                    item.measured.artifact: self._catalog_entry_of(
                        item.measured
                    )
                    for item in verified.values()
                },
            }
        )

    def _conclude_publication(
        self,
        run: RunRecord,
        *,
        outcome: str,
        reason: str,
        refusal: "Sequence[Diagnostic]" = (),
        candidate: "dict | None" = None,
        outputs: "Mapping[str, Mapping]" = None,
        verified: "Mapping[str, VerifiedOutput]" = None,
        superseded_currents: "Sequence[RunRecord]" = (),
        cancellation_requested: bool = False,
    ) -> PublicationOutcome:
        """Commit one publication conclusion in one atomic transaction.

        Steps 5 and 6 of SPEC.md section 13.4: the run record stores its
        disposition and output references, the publication receipt lands
        beside it, the candidate document (when the publication
        allocated source versions) commits with the snapshot, and one
        commit sequence acknowledges everything. The artifacts the
        publication verified ride along as trailing records so a
        reopened workspace rebuilds its catalog without rereading bytes.
        """

        now = _utc_now()
        outputs = dict(outputs or {})
        verified = verified or {}
        references = {
            name: {"sourceId": ref["sourceId"], "version": ref["version"]}
            for name, ref in outputs.items()
        }
        concluded_wire = record_to_wire(run)
        concluded_wire.update(
            {
                "disposition": outcome,
                "outputs": dict(references),
                "updatedAt": now,
            }
        )
        concluded = parse_record(concluded_wire)
        publication = PublicationRecord.model_validate(
            {
                "recordType": "publication",
                "schemaVersion": RECORD_SCHEMA_VERSION,
                "publicationId": mint_publication_id(),
                "workspaceId": self._state.workspace_id,
                "runId": concluded.runId,
                "actionId": concluded.actionId,
                "outputGroup": concluded.outputGroup,
                "generation": concluded.generation,
                "outcome": outcome,
                "reason": reason,
                "outputs": {
                    name: {
                        "sourceId": ref["sourceId"],
                        "version": ref["version"],
                    }
                    for name, ref in outputs.items()
                },
                "cancellationRequested": cancellation_requested,
                "publishedAt": now,
            }
        )

        entity_revisions = dict(self._state.entity_revisions)
        changed = [run_path(concluded.runId), publication_path(publication.publicationId)]
        for other in superseded_currents:
            changed.append(run_path(other.runId))
        document = self._state.document
        if candidate is not None:
            normalized = model_to_wire(Document.model_validate(candidate))
            for name, ref in sorted(references.items()):
                record = source_path(ref["sourceId"])
                entity_revisions[record] = (
                    entity_revisions.get(record, 0) + 1
                )
                changed.append(record)
            if outcome == CURRENT:
                for name, target in sorted(
                    (candidate.get("actions", {}).get(concluded.actionId) or {})
                    .get("targets", {})
                    .items()
                ):
                    if name not in references:
                        continue
                    record = entity_path("projections", target)
                    entity_revisions[record] = (
                        entity_revisions.get(record, 0) + 1
                    )
                    changed.append(record)
            document = normalized
        new_state = WorkspaceState(
            workspace_id=self._state.workspace_id,
            epoch=self._state.epoch,
            commit_sequence=self._state.commit_sequence + 1,
            document=document,
            entity_revisions=entity_revisions,
            parameter_states=dict(self._state.parameter_states),
            incarnations=dict(self._state.incarnations),
        )

        runs_view = dict(self._runs)
        runs_view[concluded.runId] = concluded
        for other in superseded_currents:
            runs_view[other.runId] = other
        active = tuple(
            record_to_wire(item) for item in active_run_records(runs_view)
        )
        trailing: list[tuple[str, dict]] = []
        for name in sorted(verified):
            measured = verified[name].measured
            if measured.artifact not in self._imports.artifacts:
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
                            "createdAt": now,
                        },
                    )
                )
        trailing.append((RUN_KIND, record_to_wire(concluded)))
        for other in superseded_currents:
            trailing.append((RUN_KIND, record_to_wire(other)))
        trailing.append(
            (PUBLICATION_KIND, record_to_wire(publication))
        )
        self._append_publication(new_state, sorted(set(changed)), active, trailing)

        adopted_catalog = self._imports
        for name in sorted(verified):
            measured = verified[name].measured
            if measured.artifact not in adopted_catalog.artifacts:
                adopted_catalog = SourceCatalog(
                    {
                        **adopted_catalog.artifacts,
                        measured.artifact: self._catalog_entry_of(measured),
                    }
                )
        if adopted_catalog is not self._imports:
            self._imports = adopted_catalog
            self._source_catalog = self._merge_catalog()
        return PublicationOutcome(
            run_id=concluded.runId,
            outcome=outcome,
            reason=reason,
            refusal=tuple(refusal),
            outputs=references,
            cancellation_requested=cancellation_requested,
            commit_sequence=new_state.commit_sequence,
        )

    def _append_publication(
        self,
        new_state: WorkspaceState,
        changed: "Sequence[str]",
        active_runs: "Sequence[dict]",
        trailing_records: "Sequence[tuple[str, dict]]",
    ) -> None:
        """Append one publication transaction atomically (section 13.4).

        No command envelope drives a publication, so no request receipt
        exists: the snapshot leads, the run, artifact, and publication
        records trail, and the notification lands last. The transaction
        advances the commit sequence exactly once.
        """

        notification = {
            "recordType": "changeNotification",
            "schemaVersion": RECORD_SCHEMA_VERSION,
            "workspaceId": new_state.workspace_id,
            "epoch": new_state.epoch,
            "commitSequence": new_state.commit_sequence,
            "changedRecords": list(changed),
        }
        parse_record(notification)
        records: list[tuple[str, dict]] = [
            (
                SNAPSHOT_KIND,
                _snapshot_wire(
                    new_state, self._state.commit_sequence, active_runs
                ),
            )
        ]
        records.extend(trailing_records)
        records.append((NOTIFICATION_KIND, notification))
        for kind, data in records:
            parse_record(data)  # refuse to commit a record we would reject
        sequence, _ = self._store.append_records(records)
        if sequence != new_state.commit_sequence:
            raise RuntimeError(
                f"publication commit landed at sequence {sequence} instead"
                f" of the predicted {new_state.commit_sequence}; the"
                " single-writer sequence prediction is broken"
            )
        self._state = new_state
        self._notifications.append(notification)
        for kind, data in trailing_records:
            if kind == RUN_KIND:
                run = parse_record(data)
                self._runs[run.runId] = run
            elif kind == PUBLICATION_KIND:
                publication = parse_record(data)
                self._publications[publication.publicationId] = publication

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
    # Trusted table paging (SPEC.md sections 8.2 and 12.7)
    # ------------------------------------------------------------------

    def _table_page(self, envelope: CommandEnvelope) -> CommandResponse:
        """Answer one bounded table page (SPEC.md section 12.7).

        The request names an immutable source reference. The service
        resolves it to the stored artifact bytes — never a path a
        client could choose — validates the operators against the
        declared column types, and pages the filtered, sorted rows.
        Reads answer the sequence they observed and store no receipt,
        because a page request mutates nothing.
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
                    " document; create the workspace before reading a"
                    " table page",
                ),
            )
        document = self._state.document
        source = document["sources"].get(parsed.source.sourceId)
        if source is None:
            return _rejected(
                envelope.requestId,
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    "/payload/source/sourceId",
                    f"source {parsed.source.sourceId!r} does not exist",
                ),
            )
        if source.get("kind") != "table":
            return _rejected(
                envelope.requestId,
                _diagnostic(
                    DiagnosticCode.TYPE,
                    "/payload/source/sourceId",
                    f"source {parsed.source.sourceId!r} has kind"
                    f" {source.get('kind')!r}; only a table source pages"
                    " (SPEC.md section 12.7)",
                ),
            )
        version = (source.get("versions") or {}).get(
            str(parsed.source.version)
        )
        if version is None:
            return _rejected(
                envelope.requestId,
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    "/payload/source/version",
                    f"source {parsed.source.sourceId!r} holds no version"
                    f" {parsed.source.version}",
                ),
            )
        try:
            payload = self._store.artifacts.read(version["artifact"])
        except OSError as error:
            return _rejected(
                envelope.requestId,
                _diagnostic(
                    DiagnosticCode.ARTIFACT,
                    "/payload/source",
                    f"the artifact of source"
                    f" {parsed.source.sourceId!r} version"
                    f" {parsed.source.version} is unreadable: {error}",
                    hint="Report the missing artifact individually;"
                    " saved projections stay readable (SPEC.md section"
                    " 14).",
                ),
            )
        query = PageQuery(
            source=model_to_wire(parsed.source),
            sort=[model_to_wire(key) for key in parsed.sort],
            filters=[model_to_wire(item) for item in parsed.filters],
            offset=parsed.offset,
            limit=parsed.limit,
        )
        page, errors = read_table_page(
            payload, version["schema"], query, limits=self._limits
        )
        if errors is not None:
            return _rejected(envelope.requestId, *sorted_diagnostics(errors))
        return OkResponse(
            requestId=envelope.requestId,
            ok=True,
            commitSequence=self._state.commit_sequence,
            result=page,
        )

    # ------------------------------------------------------------------
    # Host permission operations (SPEC.md sections 12.6 and 15.1)
    # ------------------------------------------------------------------

    def grant_execution_permission(
        self,
        *,
        kind: str,
        registration_id: str,
        granted_by: str,
        required_permission: "str | None" = None,
        action_id: "str | None" = None,
        action_revision: "int | None" = None,
        registration_revision: "int | None" = None,
        output_group: "str | None" = None,
        output_group_revision: "int | None" = None,
        intent_id: "str | None" = None,
        generation: "int | None" = None,
    ) -> PermissionGrantRecord:
        """Mint one execution grant for a host permission control.

        This is the authenticated native permission endpoint behind a
        host-generated permission control (SPEC.md sections 12.6 and
        15.1). No command envelope method reaches it, no document field
        names a grant, and no source text, model output, or terminal
        output can supply one: the host control alone calls this with
        the identity of the control the user operated.

        A ``projectExecution`` grant covers every admission of one
        trusted registration under the explicit project trust policy the
        host recorded. A ``specificRun`` grant covers exactly the
        captured admission intent it pins; the host reads those pins
        from the approval-required result of the activation that
        produced the intent.
        """

        if kind not in GRANT_KINDS:
            raise permission_error(
                f"grant kind {kind!r} is neither specificRun nor"
                " projectExecution",
                "/kind",
                hint="Grant either one captured intent or one whole"
                " trusted registration (SPEC.md section 15.1).",
            )
        entry = self._action_registry.entry(registration_id)
        if entry is None:
            raise permission_error(
                f"registration {registration_id!r} is not in the trusted"
                " registry; a grant cannot cover it",
                "/registrationId",
                hint="Register the script first; an unavailable"
                " registration cannot gain execution authority.",
                code=DiagnosticCode.REFERENCE,
            )
        if required_permission is None:
            required_permission = entry.required_permission
        elif required_permission != entry.required_permission:
            raise permission_error(
                f"the grant names permission {required_permission!r}, but"
                f" registration {registration_id!r} requires"
                f" {entry.required_permission!r}",
                "/requiredPermission",
                hint="Grant exactly the permission the registration"
                " requires.",
            )
        # build_grant enforces the kind-specific scope and raises
        # MeltPermissionError with the record's own diagnostics.
        record = build_grant(
            grantId=mint_grant_id(),
            workspaceId=self._state.workspace_id,
            kind=kind,
            requiredPermission=required_permission,
            registrationId=registration_id,
            actionId=action_id,
            actionRevision=action_revision,
            registrationRevision=registration_revision,
            outputGroup=output_group,
            outputGroupRevision=output_group_revision,
            intentId=intent_id,
            generation=generation,
            grantedBy=granted_by,
            grantedAt=_utc_now(),
        )
        self._commit_permission_record(record)
        # The approval applies to its captured intent (SPEC.md section
        # 12.6): every approval-required request this grant covers
        # dispatches now, with its own definitions rechecked, so the
        # approval never executes a newer request.
        self._dispatch_covered_intents(record)
        return record

    def _dispatch_covered_intents(self, grant: PermissionGrantRecord) -> None:
        """Dispatch every pending intent one new grant covers."""

        for group in sorted(self._pending_intents):
            intent = self._pending_intents[group]
            if intent.status != "approvalRequired":
                continue
            if not self._grant_covers_intent(grant, intent):
                continue
            self._dispatch_one_intent(intent, self._clock())

    def _grant_covers_intent(
        self, grant: PermissionGrantRecord, intent: PendingIntentRecord
    ) -> bool:
        """Report whether one grant covers one captured intent."""

        if grant.kind == "projectExecution":
            return grant.registrationId == intent.registrationId
        return (
            grant.intentId == intent.intentRef
            and grant.registrationId == intent.registrationId
            and grant.registrationRevision == intent.registrationRevision
            and grant.actionId == intent.actionId
            and grant.actionRevision == intent.actionRevision
            and grant.outputGroup == intent.outputGroup
            and (
                grant.outputGroupRevision is None
                or grant.outputGroupRevision == intent.outputGroupRevision
            )
            and (grant.generation is None or grant.generation == intent.generation)
        )

    def revoke_execution_permission(
        self, grant_id: str, *, revoked_by: str
    ) -> PermissionGrantRecord:
        """Revoke one execution grant. Idempotent and immediate.

        Revocation covers nothing afterwards: the next admission asks
        for approval again, and every launch and publication recheck of
        a run that relied on the grant denies it and requests
        cancellation (SPEC.md sections 13.3 and 13.5).
        """

        record = self._grants.get(grant_id)
        if record is None:
            raise permission_error(
                f"grant {grant_id!r} does not exist in this workspace",
                "/grantId",
            )
        if record.revokedAt is not None:
            return record
        updated = record.model_copy(
            update={"revokedAt": _utc_now(), "revokedBy": revoked_by}
        )
        self._commit_permission_record(updated)
        return updated

    def execution_authority_for_run(
        self, run_id: str, *, at: str = "launch"
    ) -> AuthorityDecision:
        """Recheck one admitted run's authority against live grants.

        The launch path calls this with ``at="launch"`` immediately
        before it starts the project process, and the publication path
        calls it with ``at="publication"`` inside the owning
        transaction (SPEC.md sections 13.1, 13.3, and 15.1). A denial
        whose reason is ``permission-revoked`` blocks the publication
        immediately and requests cancellation of the run.
        """

        run = self._runs.get(run_id)
        if run is None:
            raise permission_error(
                f"run {run_id!r} does not exist",
                "/runId",
                code=DiagnosticCode.REFERENCE,
            )
        entry = self._action_registry.entry(run.registrationId)
        required = None if entry is None else entry.required_permission
        gate = authority_at_publication if at == "publication" else authority_at_launch
        return gate(self._grants, run, required_permission=required)

    def _commit_permission_record(self, record: PermissionGrantRecord) -> None:
        """Commit one grant record and its notification atomically.

        A grant transaction advances the commit sequence once and
        notifies the ``permissions/<grant-id>`` record, so a subscriber
        learns that the grant set changed. It stores no command
        receipt: the permission endpoint is not a document command
        (SPEC.md section 12.6).
        """

        new_state = self._state.with_commit(self._state.commit_sequence + 1)
        wire = record_to_wire(record)
        parse_record(wire)  # refuse to commit a record we would reject
        notification = {
            "recordType": "changeNotification",
            "schemaVersion": RECORD_SCHEMA_VERSION,
            "workspaceId": new_state.workspace_id,
            "epoch": new_state.epoch,
            "commitSequence": new_state.commit_sequence,
            "changedRecords": [permission_path(record.grantId)],
        }
        parse_record(notification)
        sequence, _ = self._store.append_records(
            [(PERMISSION_KIND, wire), (NOTIFICATION_KIND, notification)]
        )
        if sequence != new_state.commit_sequence:
            raise RuntimeError(
                f"grant commit landed at sequence {sequence} instead of"
                f" the predicted {new_state.commit_sequence}; the"
                " single-writer sequence prediction is broken"
            )
        self._state = new_state
        self._notifications.append(notification)
        self._grants[record.grantId] = record

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

    def handle_request(
        self, payload: bytes, *, origin: str = "user"
    ) -> "CommandResponse | dict":
        """Parse one request payload and return its response.

        A payload that does not decode into an envelope has no request
        identifier, so the transport-level rejection is a bare error
        object rather than a :class:`RejectedResponse`. ``origin`` is
        the event origin of the caller the transport authenticated
        (SPEC.md sections 2.2 and 9.1): the framed host channel speaks
        for the local user, and an agent adapter uses the same entry
        point with a programmatic origin.
        """

        try:
            envelope = parse_command(payload)
        except MeltCommandError as error:
            request_id = _salvage_request_id(payload)
            if request_id is None:
                return {"ok": False, "errors": model_to_wire_diagnostics(error)}
            return _rejected(request_id, *error.diagnostics)
        return self.dispatch(envelope, origin=origin)

    def process_frame(self, data: bytes, *, origin: str = "user") -> bytes:
        """Answer one framed request with one framed response."""

        try:
            payload, _ = decode_frame(data, limit=self._frame_limit)
        except MeltCommandError as error:
            return _encode_json(
                {"ok": False, "errors": model_to_wire_diagnostics(error)}
            )
        response = self.handle_request(payload, origin=origin)
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


LOADED_RECORD_KINDS = frozenset(
    {
        SNAPSHOT_KIND,
        RECEIPT_KIND,
        NOTIFICATION_KIND,
        RUN_KIND,
        ARTIFACT_KIND,
        REGISTRATION_KIND,
        PERMISSION_KIND,
        OUTPUT_GROUP_KIND,
        PENDING_INTENT_KIND,
        PUBLICATION_KIND,
    }
)
"""The record kinds the loader reads; anything else refuses the open."""


@dataclass(frozen=True)
class RestoredState:
    """Everything the record stream rebuilt on open (SPEC.md section 14).

    ``corrupt_records`` names the committed records that failed
    validation and were skipped: the last snapshot that still validates
    keeps the previous readable interface, and the recovery pass
    reports the damage instead of failing the workspace (SPEC.md
    sections 14 step 3 and 18.4).
    """

    state: WorkspaceState
    receipts: dict[str, dict]
    notifications: list[dict]
    runs: dict[str, RunRecord]
    imports: SourceCatalog
    registrations: dict[str, RegistrationRecord]
    grants: dict[str, PermissionGrantRecord]
    output_groups: dict[str, OutputGroupRecord]
    pending_intents: dict[str, PendingIntentRecord]
    publications: dict[str, PublicationRecord]
    corrupt_records: tuple[CorruptRecordNote, ...] = ()


def _load_state(store: WorkspaceStore) -> RestoredState:
    """Rebuild the current state from the committed record stream.

    The verified artifact records rebuild the imported source catalog,
    the committed registration records rebuild the action registry, and
    the committed grant records rebuild the permission grant set, so a
    reopened workspace compiles its saved sources against the same
    measured schemas, its saved actions against the same registrations,
    and its execution authority against the same grants, without
    rereading any artifact bytes or executing project code (SPEC.md
    section 14, startup steps 2 and 3).

    The committed output group records rebuild the reserved generations
    and the group revisions, the newest pending intent record of each
    group rebuilds its pending request, and the publication receipts
    rebuild the publication history a recovery reconciles staging trees
    against (section 14, startup step 6), all without executing
    anything.

    A committed record that fails validation is a corrupt descriptor:
    the loader skips it and reports it, so the last valid snapshot
    preserves the readable interface (SPEC.md section 18.4). A record
    that reports an unsupported stored version — an unknown record
    kind or record type, an unsupported record schema version, or a
    snapshot in an unknown language — refuses the whole open with
    files preserved instead (section 14: never silently migrate,
    reset, or overwrite).
    """

    latest_snapshot = None
    receipts: dict[str, dict] = {}
    notifications: list[dict] = []
    runs: dict[str, RunRecord] = {}
    artifacts: dict[str, CatalogArtifact] = {}
    registrations: dict[str, RegistrationRecord] = {}
    grants: dict[str, PermissionGrantRecord] = {}
    output_groups: dict[str, OutputGroupRecord] = {}
    pending_intents: dict[str, PendingIntentRecord] = {}
    publications: dict[str, PublicationRecord] = {}
    corrupt: list[CorruptRecordNote] = []
    for record in store.records():
        if record.kind == HEADER_KIND:
            # The workspace header belongs to the storage layer and is
            # negotiated there, not loaded here.
            continue
        if record.kind not in LOADED_RECORD_KINDS:
            # A record kind this service cannot read was written by a
            # newer service: refuse with files preserved instead of
            # silently dropping stored data (SPEC.md section 14).
            raise WorkspaceFormatError(
                store.workspace_id,
                f"committed record {record.kind!r} at entry"
                f" {record.id} has a type this service does not read",
            )
        if record.kind == SNAPSHOT_KIND:
            # Refuses an unknown stored language before anything else,
            # and its refusal is not a descriptor the loop could skip.
            _check_snapshot_language(store.workspace_id, record)
        try:
            if record.kind == SNAPSHOT_KIND:
                parse_record(record.data)
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
            elif record.kind == REGISTRATION_KIND:
                registration = parse_record(record.data)
                registrations[registration.registrationId] = registration
            elif record.kind == PERMISSION_KIND:
                grant = parse_record(record.data)
                grants[grant.grantId] = grant
            elif record.kind == OUTPUT_GROUP_KIND:
                group = parse_record(record.data)
                output_groups[group.outputGroup] = group
            elif record.kind == PENDING_INTENT_KIND:
                intent = parse_record(record.data)
                pending_intents[intent.outputGroup] = intent
            elif record.kind == PUBLICATION_KIND:
                publication = parse_record(record.data)
                publications[publication.publicationId] = publication
        except MeltSchemaError as error:
            _refuse_or_note_version_error(store.workspace_id, record, error)
            first = error.diagnostics[0]
            corrupt.append(
                CorruptRecordNote(
                    entry_id=record.id,
                    kind=record.kind,
                    code=str(first.code),
                    message=first.message,
                )
            )
    imports = SourceCatalog(artifacts)
    if latest_snapshot is None:
        state = WorkspaceState(
            workspace_id=store.workspace_id,
            epoch=store.epoch,
            commit_sequence=store.commit_sequence,
        )
    else:
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
    return RestoredState(
        state=state,
        receipts=receipts,
        notifications=notifications,
        runs=runs,
        imports=imports,
        registrations=registrations,
        grants=grants,
        output_groups=output_groups,
        pending_intents=pending_intents,
        publications=publications,
        corrupt_records=tuple(corrupt),
    )


def _check_snapshot_language(workspace_id: str, record) -> None:
    """Refuse a snapshot stored in an unknown language version.

    The language of a snapshot is readable before its full payload is
    validated, so the check runs first: an unknown language opens the
    recovery screen with files preserved instead of masquerading as a
    corrupt descriptor the loader could skip (SPEC.md section 14).
    """

    language = record.data.get("language") if isinstance(record.data, dict) else None
    if language is not None and language != LANGUAGE_VERSION:
        raise WorkspaceFormatError(
            workspace_id,
            "the committed snapshot stores language"
            f" {language!r}, but this service compiles"
            f" {LANGUAGE_VERSION!r}",
        )


def _refuse_or_note_version_error(
    workspace_id: str, record, error: MeltSchemaError
) -> None:
    """Turn version-class failures into a refusal that preserves files.

    A skipped record is fine when its bytes went bad, but a record a
    newer service wrote — an unknown record type or an unsupported
    record schema version — must not be silently dropped: the stored
    data is unreadable, so the open refuses with ``E_VERSION`` and the
    database stays untouched for recovery mode (SPEC.md section 14).
    """

    for item in error.diagnostics:
        if item.code == DiagnosticCode.VERSION:
            raise WorkspaceFormatError(
                workspace_id,
                f"committed record {record.kind!r} at entry"
                f" {record.id} reports an unsupported stored version:"
                f" {item.message}",
            ) from error


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


def _same_contract(record: RegistrationRecord, contract) -> bool:
    """Report whether a registration already declares this contract."""

    return model_to_wire(contract_of(record)) == model_to_wire(contract)


def _registration_result(
    record: RegistrationRecord, environment: KnownEnvironment, *, created: bool
) -> dict:
    """Build the answer of one ``action.register`` request (12.6)."""

    return {
        "registrationId": record.registrationId,
        "revision": record.revision,
        "created": created,
        "script": {
            "artifact": record.script.artifact,
            "byteLength": record.script.byteLength,
            "mediaType": record.script.mediaType,
        },
        "environmentId": environment.environment_id,
        "environmentFingerprint": record.environmentFingerprint,
        "projectRoot": record.projectRoot,
        "workingDirectoryPolicy": record.workingDirectoryPolicy,
        "requiredPermission": record.requiredPermission,
        "replayClassification": record.replayClassification,
    }


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
