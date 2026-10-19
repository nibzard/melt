"""Scheduling: generations, pending intents, and admission (T016).

The service owns every timer and every queue of the execution loop
(SPEC.md sections 9.3 and 13.1):

- A qualifying event immediately reserves a new generation for its
  output group, in the same transaction that admits the intent or
  commits the triggering user change. The reservation alone makes older
  runs ineligible for current publication; the service never waits for
  a timer to invalidate older work. A generation is never reused.
- Several bindings that target one action coalesce into one intent that
  captures the final parameter values of the transaction.
- At most one pending request exists per output group. A repeated event
  replaces it, restarts its timer, and persists the newest intent.
- Only one project execution runs at a time per workspace.

This module holds the pure scheduling pieces: run identifiers, the
generation counter of an output group, the invocation snapshot of an
admitted intent, the debounce due time, and the definition checks a
dispatch performs. The transactions that commit them belong to the
workspace service; the process boundary belongs to
:mod:`runtime.execution.launcher`.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Mapping

from melt.schema.diagnostics import Diagnostic, DiagnosticCode
from melt.schema.records import (
    RECORD_SCHEMA_VERSION,
    CapturedParameter,
    OutputGroupRecord,
    PendingIntentRecord,
    RunRecord,
)
from runtime.workspace.state import ParameterStateValue

RUN_ID_PREFIX = "run-"
"""Service-minted run identifiers carry this prefix."""

_ID_ENTROPY_BYTES = 18
"""Random bytes per run identifier: 144 bits, far beyond guessing range."""

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
"""The UTC form every scheduling record stamps."""

DEBOUNCE_CEILING_MS = 5000
"""The largest debounce a binding may declare (SPEC.md section 9.2)."""


class MeltSchedulingError(RuntimeError):
    """Raised when a run cannot be admitted or launched."""


def default_clock() -> float:
    """Return the current epoch time the service schedules with."""

    return time.time()


def timestamp_of(epoch: float) -> str:
    """Return the UTC timestamp of one epoch time.

    Sub-second precision survives, because a debounce timer shorter
    than one second must stay distinguishable from its start.
    """

    moment = datetime.fromtimestamp(epoch, tz=timezone.utc)
    if moment.microsecond:
        return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return moment.strftime(TIMESTAMP_FORMAT)


def epoch_of(timestamp: str) -> float:
    """Return the epoch time of one UTC scheduling timestamp."""

    for fmt in (TIMESTAMP_FORMAT, "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(timestamp, fmt).replace(
                tzinfo=timezone.utc
            ).timestamp()
        except ValueError:
            continue
    raise ValueError(f"timestamp {timestamp!r} is not a UTC scheduling stamp")


def mint_run_id() -> str:
    """Mint one opaque run identifier.

    The identifier follows the run-directory syntax of SPEC.md section
    14, so the staging and log directories of the run derive from it
    directly.
    """

    return RUN_ID_PREFIX + secrets.token_urlsafe(_ID_ENTROPY_BYTES)


# ---------------------------------------------------------------------------
# Generations and output group revisions (SPEC.md sections 9.3 and 13.3)
# ---------------------------------------------------------------------------


def reserve_generation(
    group: "OutputGroupRecord | None", output_group: str, workspace_id: str, now: str
) -> tuple[OutputGroupRecord, int]:
    """Reserve the next generation of one output group.

    The counters of a group begin at generation zero and revision one:
    the first reservation mints the record and returns generation one.
    The caller commits the returned record inside the admitting
    transaction, so the reservation and the intent it belongs to land
    together or not at all.
    """

    if group is None:
        reserved = OutputGroupRecord.model_validate(
            {
                "recordType": "outputGroup",
                "schemaVersion": RECORD_SCHEMA_VERSION,
                "outputGroup": output_group,
                "workspaceId": workspace_id,
                "generation": 1,
                "revision": 1,
                "createdAt": now,
                "updatedAt": now,
            }
        )
        return reserved, 1
    reserved = group.model_copy(
        update={"generation": group.generation + 1, "updatedAt": now}
    )
    return reserved, reserved.generation


def is_newest_generation(
    groups: Mapping[str, OutputGroupRecord], run: RunRecord
) -> bool:
    """Report whether one run still holds its group's newest generation.

    This is the check that makes an older run ineligible immediately:
    the reservation happens in the admitting transaction, not when a
    timer fires (SPEC.md section 9.3).
    """

    group = groups.get(run.outputGroup)
    if group is None:
        return False
    return run.generation == group.generation


def group_signature(document: "Mapping | None") -> dict[str, tuple]:
    """Return the publication-relevant signature of every output group.

    A group's signature covers its owning action, its output contract,
    and its targets with their kinds: exactly the dimensions whose
    change increments the output group revision (SPEC.md section 13.3).
    A projection title or a layout change does not appear here.
    """

    if document is None:
        return {}
    projections = document.get("projections", {})
    signatures: dict[str, tuple] = {}
    for action_id, action in sorted(document.get("actions", {}).items()):
        group = action.get("outputGroup")
        if group is None:
            continue
        targets = tuple(
            (name, target, (projections.get(target) or {}).get("kind"))
            for name, target in sorted(action.get("targets", {}).items())
        )
        signatures[group] = (
            action_id,
            tuple(sorted(action.get("outputs", {}).items())),
            targets,
        )
    return signatures


def changed_output_groups(
    before: "Mapping | None", after: "Mapping | None"
) -> set[str]:
    """Return the output groups whose publication contract changed.

    A group enters the result when its signature appeared, disappeared,
    or changed. The caller bumps the revision of every group in this set
    that already holds a record; a group with no record starts at
    revision one when it is first admitted against.
    """

    old = group_signature(before)
    new = group_signature(after)
    changed: set[str] = set()
    for group in set(old) | set(new):
        if old.get(group) != new.get(group):
            changed.add(group)
    return changed


def bump_group_revision(
    group: "OutputGroupRecord | None", output_group: str, workspace_id: str, now: str
) -> OutputGroupRecord:
    """Return one output group record with its revision incremented."""

    if group is None:
        return OutputGroupRecord.model_validate(
            {
                "recordType": "outputGroup",
                "schemaVersion": RECORD_SCHEMA_VERSION,
                "outputGroup": output_group,
                "workspaceId": workspace_id,
                "generation": 0,
                "revision": 2,
                "createdAt": now,
                "updatedAt": now,
            }
        )
    return group.model_copy(
        update={"revision": group.revision + 1, "updatedAt": now}
    )


# ---------------------------------------------------------------------------
# Triggering bindings (SPEC.md sections 9.2 and 9.3)
# ---------------------------------------------------------------------------


def triggered_actions(
    document: Mapping, changed_parameters: "set[str] | frozenset[str]"
) -> dict[str, int]:
    """Return the actions one committed user change triggers.

    Every binding whose ``parameter.changed`` event addresses a changed
    parameter and whose effect invokes an existing action contributes.
    Bindings observe committed user changes only, so the caller passes
    exactly the parameters whose stored value this transaction changed.

    Several bindings that target one action coalesce into one entry
    (SPEC.md section 13.1). The debounce of the coalesced intent is the
    largest debounce among them, so no coalesced binding can fire
    earlier than its own timer.
    """

    actions = document.get("actions", {})
    debounce: dict[str, int] = {}
    for binding in document.get("bindings", {}).values():
        event = binding.get("event", {})
        if event.get("kind") != "parameter.changed":
            continue
        if event.get("parameter") not in changed_parameters:
            continue
        effect = binding.get("effect", {})
        if effect.get("kind") != "action.invoke":
            continue
        action_id = effect.get("action")
        if action_id not in actions:
            continue
        milliseconds = int(binding.get("debounceMs", 0))
        debounce[action_id] = max(debounce.get(action_id, 0), milliseconds)
    return debounce


def value_changed_parameters(
    new_states: Mapping, requested_parameters: Mapping
) -> "frozenset[str]":
    """Return the parameters one transaction actually changed.

    Only a ``parameter.set`` that changed the stored value emits a user
    event: a created parameter starts from its default and an equal
    value written back is a no-op (SPEC.md sections 6.2 and 9.2).
    """

    return frozenset(
        parameter_id
        for parameter_id in new_states
        if parameter_id in requested_parameters
    )


# ---------------------------------------------------------------------------
# The invocation snapshot and the admitted records (SPEC.md section 7.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InvocationSnapshot:
    """Every fact an admitted intent captures (SPEC.md section 7.3).

    The snapshot is immutable from the moment of capture: the
    registration revision, the script hash, the environment fingerprint,
    the input source references, the parameter values with their state
    revisions, the action definition revision, the output group
    revision, the generation, and the permission decision reference.

    ``output_incarnation`` is the stable output identity of the owning
    action (RFC 001 section 8.1). The group name alone cannot identify
    a calculation, because a later transaction may hand it to another
    action, so the snapshot captures the identity the current pointers
    of the run's outputs resolve against.
    """

    action_id: str
    action_revision: int
    registration_id: str
    registration_revision: int
    script_artifact: str
    environment_fingerprint: str
    output_group: str
    output_group_revision: int
    generation: int
    parameters: dict[str, CapturedParameter]
    parameter_ids: dict[str, str]
    inputs: dict[str, dict]
    output_incarnation: "str | None" = None


def build_pending_intent(
    snapshot: InvocationSnapshot,
    *,
    workspace_id: str,
    intent_ref: str,
    debounce_ms: int,
    due_at_epoch: float,
    status: str,
    origin: str,
    reason: str,
    now: str,
) -> PendingIntentRecord:
    """Build one pending intent record from a captured snapshot.

    The intent is the persisted pending request of its output group:
    it carries the timer (``debounceMs`` and ``dueAt``), the origin of
    the event, and the status of the request. The caller expires the
    intent it replaces inside the same transaction.
    """

    return PendingIntentRecord.model_validate(
        {
            "recordType": "pendingIntent",
            "schemaVersion": RECORD_SCHEMA_VERSION,
            "intentRef": intent_ref,
            "workspaceId": workspace_id,
            "actionId": snapshot.action_id,
            "actionRevision": snapshot.action_revision,
            "registrationId": snapshot.registration_id,
            "registrationRevision": snapshot.registration_revision,
            "scriptArtifact": snapshot.script_artifact,
            "environmentFingerprint": snapshot.environment_fingerprint,
            "outputGroup": snapshot.output_group,
            "outputGroupRevision": snapshot.output_group_revision,
            "outputIncarnation": snapshot.output_incarnation,
            "generation": snapshot.generation,
            "parameters": {
                name: {
                    "value": captured.value,
                    "revision": captured.revision,
                    "incarnation": captured.incarnation,
                }
                for name, captured in sorted(snapshot.parameters.items())
            },
            "parameterIds": dict(sorted(snapshot.parameter_ids.items())),
            "inputs": {
                name: dict(reference)
                for name, reference in sorted(snapshot.inputs.items())
            },
            "debounceMs": debounce_ms,
            "dueAt": timestamp_of(due_at_epoch),
            "status": status,
            "origin": origin,
            "reason": reason,
            "createdAt": now,
            "updatedAt": now,
        }
    )


def build_run_record(
    intent: PendingIntentRecord,
    *,
    run_id: str,
    state: str = "queued",
    permission_decision: "str | None" = None,
    now: str,
) -> RunRecord:
    """Mint one run record from a dispatched pending intent.

    The run carries the full invocation snapshot of the intent, so
    changes to the workspace cannot change an admitted run's inputs
    (SPEC.md section 7.3). That includes the stable output identity of
    the owning action, so the run's publications resolve against the
    calculation that admitted it rather than the group name it happened
    to hold (RFC 001 section 8.1). Publication disposition starts at
    ``none`` and is decided by the publication path alone.
    """

    return RunRecord.model_validate(
        {
            "recordType": "run",
            "schemaVersion": RECORD_SCHEMA_VERSION,
            "runId": run_id,
            "workspaceId": intent.workspaceId,
            "actionId": intent.actionId,
            "actionRevision": intent.actionRevision,
            "outputGroup": intent.outputGroup,
            "outputGroupRevision": intent.outputGroupRevision,
            "outputIncarnation": intent.outputIncarnation,
            "generation": intent.generation,
            "registrationId": intent.registrationId,
            "registrationRevision": intent.registrationRevision,
            "scriptArtifact": intent.scriptArtifact,
            "environmentFingerprint": intent.environmentFingerprint,
            "inputs": {
                name: dict(reference) for name, reference in intent.inputs.items()
            },
            "parameters": {
                name: {
                    "value": captured.value,
                    "revision": captured.revision,
                    "incarnation": captured.incarnation,
                }
                for name, captured in sorted(intent.parameters.items())
            },
            "parameterIds": dict(sorted(intent.parameterIds.items())),
            "permissionDecision": permission_decision,
            "state": state,
            "disposition": "none",
            "outputs": {},
            "createdAt": now,
            "updatedAt": now,
        }
    )


def expire_intent(
    intent: PendingIntentRecord, *, reason: str, now: str
) -> PendingIntentRecord:
    """Return one intent record marked expired.

    A newer request replaces it, or its definitions went stale before
    dispatch. The record stays in history; the group's pending slot
    frees.
    """

    return intent.model_copy(
        update={"status": "expired", "reason": reason, "updatedAt": now}
    )


def interrupt_intent(
    intent: PendingIntentRecord, *, now: str
) -> PendingIntentRecord:
    """Return one intent record marked interrupted by a restart.

    A restart marks the newest pending intent interrupted; it does not
    automatically execute it (SPEC.md sections 9.3 and 14).
    """

    return intent.model_copy(
        update={
            "status": "interrupted",
            "reason": "service-restart",
            "updatedAt": now,
        }
    )


def due_intents(
    intents: Mapping[str, PendingIntentRecord], now_epoch: float
) -> list[PendingIntentRecord]:
    """Return the due pending intents, ordered by due time then group."""

    due = [
        intent
        for intent in intents.values()
        if intent.status == "pending" and epoch_of(intent.dueAt) <= now_epoch
    ]
    return sorted(due, key=lambda intent: (intent.dueAt, intent.outputGroup))


# ---------------------------------------------------------------------------
# The definition checks of a dispatch (SPEC.md sections 9.3 and 13.1)
# ---------------------------------------------------------------------------


def intent_definition_error(
    *,
    document: "Mapping | None",
    action_revision_of: "Callable[[str], int]",
    registry_entry_of: "Callable[[str], object]",
    group_revision_of: "Callable[[str], int]",
    intent: PendingIntentRecord,
) -> "Diagnostic | None":
    """Return the stale-definition error of one due intent, or None.

    On dispatch, reject stale action definitions, registrations that
    left the trusted registry, and output groups whose publication
    contract changed. Do not silently substitute a different script or
    action (SPEC.md sections 9.3 and 13.1).
    """

    actions = (document or {}).get("actions", {})
    action = actions.get(intent.actionId)
    if action is None:
        return Diagnostic(
            code=DiagnosticCode.REFERENCE,
            path=f"/actions/{intent.actionId}",
            message=f"action {intent.actionId!r} no longer exists; the"
            " captured intent cannot dispatch",
        )
    if action_revision_of(intent.actionId) != intent.actionRevision:
        return Diagnostic(
            code=DiagnosticCode.CONFLICT,
            path=f"/actions/{intent.actionId}",
            message=f"action {intent.actionId!r} moved to revision"
            f" {action_revision_of(intent.actionId)}; the captured"
            f" revision {intent.actionRevision} is stale",
        )
    entry = registry_entry_of(intent.registrationId)
    if entry is None:
        return Diagnostic(
            code=DiagnosticCode.REGISTRATION,
            path=f"/actions/{intent.actionId}/implementation/registrationId",
            message=f"registration {intent.registrationId!r} is no longer"
            " available; the captured intent cannot dispatch",
            hint="Register the script again; the saved projections stay"
            " readable.",
        )
    if getattr(entry, "revision", None) != intent.registrationRevision:
        return Diagnostic(
            code=DiagnosticCode.REGISTRATION,
            path=f"/actions/{intent.actionId}/implementation/registrationId",
            message=f"registration {intent.registrationId!r} moved to"
            f" revision {getattr(entry, 'revision')}; the captured"
            f" revision {intent.registrationRevision} is stale",
        )
    if group_revision_of(intent.outputGroup) != intent.outputGroupRevision:
        return Diagnostic(
            code=DiagnosticCode.CONFLICT,
            path=f"/actions/{intent.actionId}/outputGroup",
            message=f"output group {intent.outputGroup!r} moved to revision"
            f" {group_revision_of(intent.outputGroup)}; the captured"
            f" revision {intent.outputGroupRevision} is stale",
        )
    return None


def captured_parameter_identity_error(
    *,
    document: "Mapping | None",
    parameter_states: "Mapping[str, ParameterStateValue]",
    parameter_incarnations: "Mapping[str, int]",
    action_id: str,
    parameter_ids: "Mapping[str, str]",
    parameters: "Mapping[str, CapturedParameter]",
) -> "Diagnostic | None":
    """Return the identity error of one captured parameter set, or None.

    Deleting and recreating a parameter starts a new identity
    incarnation under the same identifier while the recreated state
    begins again at revision one (SPEC.md section 12.3), so an
    invocation that captured the previous incarnation can neither
    launch nor publish under the recreated name, value, and revision.
    A capture without an incarnation is a record saved before identity
    capture: it reads as the first incarnation, which no recreation
    can satisfy again. A mere value change keeps the identity and
    stays eligible here; its runs settle through the ordinary
    supersession of publication.
    """

    mappings = ((document or {}).get("actions", {}).get(action_id) or {}).get(
        "parameters", {}
    )
    for name in sorted(parameters):
        captured = parameters[name]
        parameter_id = parameter_ids.get(name)
        if parameter_id is None:
            # An older record without its id map falls back to the
            # definition the action still declares for this name.
            parameter_id = (mappings.get(name) or {}).get("parameter")
        if parameter_id is None:
            # No definition maps this name anymore; the definition
            # checks report that. Nothing to compare here.
            continue
        stored = parameter_states.get(parameter_id)
        if stored is None:
            return Diagnostic(
                code=DiagnosticCode.CONFLICT,
                path=f"/parameters/{parameter_id}",
                message=f"parameter {parameter_id!r} holds no stored state;"
                " the captured invocation names a parameter identity"
                " the document dropped",
                hint="Invoke the action again; its next admission"
                " captures the definitions of the current document"
                " (SPEC.md section 12.3).",
            )
        captured_identity = (
            1 if captured.incarnation is None else captured.incarnation
        )
        stored_identity = parameter_incarnations.get(parameter_id, 1)
        if captured_identity != stored_identity:
            return Diagnostic(
                code=DiagnosticCode.CONFLICT,
                path=f"/parameters/{parameter_id}",
                message=f"parameter {parameter_id!r} was deleted and"
                f" recreated: its identity moved to incarnation"
                f" {stored_identity}, while the captured invocation"
                f" holds incarnation {captured_identity}; a recreated"
                " parameter is a new identity even when its name,"
                " value, and state revision match",
                hint="Invoke the action again; its next admission"
                " captures the new parameter identity (SPEC.md"
                " section 12.3).",
            )
    return None


# ---------------------------------------------------------------------------
# The launch attempt report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunLaunchOutcome:
    """The report of one launch attempt of an admitted run.

    ``launched`` is False when a pre-launch recheck refused the run:
    ``refusal`` carries its diagnostics and ``state`` the run state the
    attempt left behind. A refused launch never reports failure as
    successful completion (SPEC.md section 13.1).
    """

    run_id: str
    launched: bool
    state: str
    refusal: tuple[Diagnostic, ...] = ()
    exit_code: "int | None" = None
    staged: "dict | None" = None
    manifest_path: "str | None" = None
    stdout_path: "str | None" = None
    stderr_path: "str | None" = None


@dataclass
class AdmissionRecords:
    """The scheduling records one admitting transaction commits.

    The service appends every item in one atomic transaction, together
    with the intent each run dispatched from, so a restart never sees a
    generation without its intent or a run without its snapshot.
    """

    groups: list[OutputGroupRecord] = field(default_factory=list)
    intents: list[PendingIntentRecord] = field(default_factory=list)
    runs: list[RunRecord] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.groups or self.intents or self.runs)

    def changed_paths(self) -> "list[str]":
        from runtime.workspace.state import intent_path, output_group_path, run_path

        paths = [output_group_path(group.outputGroup) for group in self.groups]
        paths.extend(intent_path(intent.intentRef) for intent in self.intents)
        paths.extend(run_path(run.runId) for run in self.runs)
        return sorted(set(paths))


__all__ = [
    "DEBOUNCE_CEILING_MS",
    "RUN_ID_PREFIX",
    "TIMESTAMP_FORMAT",
    "AdmissionRecords",
    "InvocationSnapshot",
    "RunLaunchOutcome",
    "build_pending_intent",
    "build_run_record",
    "bump_group_revision",
    "captured_parameter_identity_error",
    "changed_output_groups",
    "default_clock",
    "due_intents",
    "epoch_of",
    "expire_intent",
    "intent_definition_error",
    "interrupt_intent",
    "is_newest_generation",
    "mint_run_id",
    "reserve_generation",
    "timestamp_of",
    "triggered_actions",
    "value_changed_parameters",
]
