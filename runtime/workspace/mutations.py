"""Mutation transactions against a private candidate (SPEC.md 12.2, 12.3).

A ``workspace.apply`` request is planned before anything is written:

1. Every touched record path must appear in ``expected``; every
   expectation must match the current revision of that path. Zero means
   expected absence (section 12.2).
2. Operations apply in order against a private candidate document.
   Creation requires absence at that point; replace and delete require
   presence. Intermediate references may stay incomplete.
3. State continuity is checked against the final definitions: changing
   a parameter definition requires its current value to remain valid,
   or the same transaction must supply ``parameter.set`` with a valid
   replacement and the expected state revision (section 12.3).
4. The final candidate is validated once through the compiler stages,
   with the resulting parameter state and the trusted catalog and
   registry inputs of the service.

The plan carries the new revisions, the resulting parameter state, the
identity incarnation changes, and the compiler read set observed during
planning. Committing applies every change or none: a plan with
diagnostics never reaches the store.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from melt.schema.canonical import model_to_wire
from melt.schema.commands import (
    CommandEnvelope,
    EntityCreate,
    EntityDelete,
    EntityReplace,
    LayoutReplace,
    ParameterSet,
)
from melt.schema.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    sorted_diagnostics,
)
from melt.schema.document import Document, Parameter
from runtime.compiler.compiler import (
    CompileRequest,
    compile_document,
    parameter_value_error,
)
from runtime.workspace.state import (
    LAYOUT_PATH,
    ParameterStateValue,
    WorkspaceState,
    entity_path,
    pointer_key,
    state_path,
    values_equal,
)


def _diagnostic(
    code: str,
    path: str,
    message: str,
    related_paths: "list[str] | tuple[str, ...]" = (),
    hint: str = "",
) -> Diagnostic:
    return Diagnostic(
        code=code,
        path=path,
        message=message,
        relatedPaths=tuple(related_paths),
        hint=hint,
    )


@dataclass
class EntityTouch:
    """How one entity record path was touched inside the transaction."""

    first_operation: int
    last_operation: int
    existed_before: bool
    exists_after: bool
    created: bool = False
    deleted: bool = False


@dataclass
class PendingSet:
    """One ``parameter.set`` held until the final validation."""

    operation: int
    value: "bool | float | str | None"


@dataclass
class MutationPlan:
    """The result of planning one mutation transaction."""

    candidate: dict
    document_hash: "str | None" = None
    diagnostics: list[Diagnostic] = field(default_factory=list)
    entity_touches: dict[str, EntityTouch] = field(default_factory=dict)
    touched_state_paths: dict[str, PendingSet] = field(default_factory=dict)
    new_states: dict[str, ParameterStateValue] = field(default_factory=dict)
    removed_states: set[str] = field(default_factory=set)
    incarnation_bumps: dict[str, int] = field(default_factory=dict)
    observed_revisions: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.diagnostics

    def changed_records(self) -> list[str]:
        """The record paths this transaction changes, sorted.

        A replaced record always counts as changed, even when the
        replacement appears equivalent (SPEC.md section 13.3). A
        ``parameter.set`` with an equal value changes nothing.
        """

        changed = [
            path
            for path, touch in self.entity_touches.items()
            if touch.created or touch.deleted or touch.exists_after
        ]
        changed.extend(state_path(parameter_id) for parameter_id in self.new_states)
        changed.extend(state_path(parameter_id) for parameter_id in self.removed_states)
        return sorted(set(changed))

    def new_revisions(self, state: WorkspaceState) -> dict[str, int]:
        """The revision of every touched, surviving record path.

        Each changed record increments its own revision exactly once per
        transaction, from its pre-transaction revision (SPEC.md 12.5).
        A record created and deleted in one transaction ends absent.
        """

        revisions: dict[str, int] = {}
        for path, touch in self.entity_touches.items():
            if not touch.exists_after:
                continue
            before = state.entity_revisions.get(path, 0) if touch.existed_before else 0
            revisions[path] = before + 1
        return revisions


def _expectation_diagnostics(
    state: WorkspaceState,
    expected: dict[str, int],
    touched_paths: "list[str]",
) -> list[Diagnostic]:
    """Check the expectation map of one transaction (SPEC.md 12.2).

    Every touched record path needs an expectation. Every expectation,
    touched or not, must match the current revision of its path; an
    expectation for an untouched record acts as a read assertion.
    """

    diagnostics: list[Diagnostic] = []
    for path in touched_paths:
        if path not in expected:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.CONFLICT,
                    "/expected",
                    f"expected must carry {path!r}: the transaction"
                    " touches that record",
                    hint="Add the record path with its current revision;"
                    " use zero for expected absence.",
                )
            )
    for path in sorted(expected):
        wanted = expected[path]
        current = state.current_revision(path)
        if wanted != current:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.CONFLICT,
                    f"/expected/{pointer_key(path)}",
                    f"expected revision {wanted} for {path!r}, but the"
                    f" current revision is {current}",
                    hint="Refresh the expectation from an inspection, or"
                    " retry after the intervening change.",
                )
            )
    return diagnostics


def plan_mutation(
    state: WorkspaceState,
    envelope: CommandEnvelope,
    *,
    source_catalog: Any = None,
    action_registry: Any = None,
) -> MutationPlan:
    """Plan one ``workspace.apply`` transaction without writing."""

    observed = state.read_set()
    plan = MutationPlan(candidate={}, observed_revisions=observed)
    if state.document is None:
        plan.diagnostics.append(
            _diagnostic(
                DiagnosticCode.REFERENCE,
                "/workspaceId",
                f"workspace {state.workspace_id!r} holds no document;"
                " create the workspace before applying mutations",
            )
        )
        return plan

    candidate = copy.deepcopy(state.document)
    plan.candidate = candidate
    touches: dict[str, EntityTouch] = {}
    pending_sets: dict[str, PendingSet] = {}
    touched_order: list[str] = []

    def _touch(path: str, index: int) -> EntityTouch:
        touch = touches.get(path)
        if touch is None:
            touch = EntityTouch(
                first_operation=index,
                last_operation=index,
                existed_before=state.entity_revisions.get(path, 0) > 0,
                exists_after=state.entity_revisions.get(path, 0) > 0,
            )
            touches[path] = touch
            touched_order.append(path)
        touch.last_operation = index
        return touch

    # ------------------------------------------------------------------
    # Stage A: the expectation map
    # ------------------------------------------------------------------

    operations = list(envelope.operations or ())
    for index, operation in enumerate(operations):
        if isinstance(operation, (EntityCreate, EntityReplace, EntityDelete)):
            _touch(entity_path(operation.collection, operation.id), index)
        elif isinstance(operation, ParameterSet):
            path = state_path(operation.id)
            if path not in touched_order:
                touched_order.append(path)
        elif isinstance(operation, LayoutReplace):
            _touch(LAYOUT_PATH, index)

    expected = dict(envelope.expected or {})
    diagnostics = _expectation_diagnostics(state, expected, touched_order)
    if diagnostics:
        plan.diagnostics = sorted_diagnostics(diagnostics)
        plan.entity_touches = touches
        plan.touched_state_paths = pending_sets
        return plan

    # ------------------------------------------------------------------
    # Stage B: apply the operations in order against the candidate
    # ------------------------------------------------------------------

    for index, operation in enumerate(operations):
        path = f"/operations/{index}"
        if isinstance(operation, EntityCreate):
            collection = candidate[operation.collection]
            record = entity_path(operation.collection, operation.id)
            if operation.id in collection:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.CONFLICT,
                        f"{path}/id",
                        f"entity {record!r} already exists at revision"
                        f" {state.current_revision(record)}; creation"
                        " requires absence",
                    )
                )
                continue
            collection[operation.id] = model_to_wire(operation.value)
            touch = _touch(record, index)
            touch.exists_after = True
            touch.created = True
            touch.deleted = False
        elif isinstance(operation, EntityReplace):
            collection = candidate[operation.collection]
            record = entity_path(operation.collection, operation.id)
            if operation.id not in collection:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.REFERENCE,
                        f"{path}/id",
                        f"entity {record!r} does not exist; replace and"
                        " delete require an existing record",
                    )
                )
                continue
            collection[operation.id] = model_to_wire(operation.value)
            touch = _touch(record, index)
            touch.exists_after = True
        elif isinstance(operation, EntityDelete):
            collection = candidate[operation.collection]
            record = entity_path(operation.collection, operation.id)
            if operation.id not in collection:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.REFERENCE,
                        f"{path}/id",
                        f"entity {record!r} does not exist; replace and"
                        " delete require an existing record",
                    )
                )
                continue
            del collection[operation.id]
            touch = _touch(record, index)
            touch.exists_after = False
            touch.deleted = True
        elif isinstance(operation, ParameterSet):
            pending_sets[operation.id] = PendingSet(
                operation=index, value=operation.value
            )
        elif isinstance(operation, LayoutReplace):
            candidate["layout"] = model_to_wire(operation.value)
            _touch(LAYOUT_PATH, index)

    if diagnostics:
        plan.diagnostics = sorted_diagnostics(diagnostics)
        plan.entity_touches = touches
        plan.touched_state_paths = pending_sets
        return plan

    # ------------------------------------------------------------------
    # Stage C: state continuity and migrations (SPEC.md 12.3)
    # ------------------------------------------------------------------

    definitions_after = candidate["parameters"]
    for parameter_id in sorted(pending_sets):
        pending = pending_sets[parameter_id]
        if parameter_id not in definitions_after:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    f"/operations/{pending.operation}/id",
                    f"parameter {parameter_id!r} does not exist in the"
                    " candidate document",
                )
            )

    resulting_states: dict[str, ParameterStateValue] = {}
    for parameter_id in sorted(definitions_after):
        record = entity_path("parameters", parameter_id)
        touch = touches.get(record)
        pending = pending_sets.get(parameter_id)
        parameter = Parameter.model_validate(definitions_after[parameter_id])
        current = state.parameter_states.get(parameter_id)

        if touch is not None and touch.created:
            # A created parameter starts a new identity incarnation. Its
            # state begins at the supplied value or the default, at
            # revision 1; the previous state of a deleted namesake is
            # discarded and cannot migrate (SPEC.md section 12.3).
            value = pending.value if pending is not None else parameter.default
            if pending is not None:
                reason = parameter_value_error(parameter, pending.value)
                if reason is not None:
                    diagnostics.append(
                        _diagnostic(
                            DiagnosticCode.TYPE,
                            f"/operations/{pending.operation}/value",
                            f"value {pending.value!r} does not satisfy"
                            f" parameter {parameter_id!r}: {reason}",
                        )
                    )
                    continue
            if value is None and not parameter.nullable:
                # A null default is a document error that the final
                # validation reports at its own path.
                continue
            resulting_states[parameter_id] = ParameterStateValue(
                value=value, revision=1
            )
            plan.incarnation_bumps[parameter_id] = (
                state.incarnations.get(parameter_id, 0) + 1
            )
            continue

        if touch is not None and touch.deleted:
            continue

        if touch is not None and not touch.exists_after:
            continue

        # The definition is untouched or replaced; the stored value
        # carries over unless the transaction supplies a replacement.
        if current is None:
            # Cannot happen while every creation initializes state, but
            # a hand-forged snapshot is refused rather than silently
            # repaired.
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.STATE_MIGRATION,
                    f"/operations/{touch.last_operation}" if touch else "/operations",
                    f"parameter {parameter_id!r} holds no stored state",
                )
            )
            continue

        value = pending.value if pending is not None else current.value
        reason = parameter_value_error(parameter, value)
        if reason is not None:
            if touch is not None:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.STATE_MIGRATION,
                        f"/operations/{touch.last_operation}",
                        f"parameter {parameter_id!r}: the resulting state"
                        f" {value!r} does not satisfy the new definition:"
                        f" {reason}",
                        hint="Supply parameter.set with a valid"
                        " replacement and the expected state revision in"
                        " the same transaction (SPEC.md section 12.3).",
                    )
                )
            elif pending is not None:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.TYPE,
                        f"/operations/{pending.operation}/value",
                        f"value {value!r} does not satisfy parameter"
                        f" {parameter_id!r}: {reason}",
                    )
                )
            else:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.STATE_MIGRATION,
                        f"/parameters/{parameter_id}",
                        f"stored state {value!r} violates the definition:"
                        f" {reason}",
                    )
                )
            continue

        if pending is not None and not values_equal(value, current.value):
            resulting_states[parameter_id] = ParameterStateValue(
                value=value, revision=current.revision + 1
            )

    for record, touch in touches.items():
        if not record.startswith("parameters/"):
            continue
        parameter_id = record[len("parameters/") :]
        if touch.deleted and not touch.exists_after:
            plan.removed_states.add(parameter_id)

    if diagnostics:
        plan.diagnostics = sorted_diagnostics(diagnostics)
        plan.entity_touches = touches
        plan.touched_state_paths = pending_sets
        return plan

    plan.new_states = resulting_states
    plan.entity_touches = touches
    plan.touched_state_paths = pending_sets

    # ------------------------------------------------------------------
    # Stage D: validate the final candidate once through the compiler
    # ------------------------------------------------------------------

    full_states: dict[str, dict] = {}
    for parameter_id in definitions_after:
        if parameter_id in resulting_states:
            state_value = resulting_states[parameter_id]
        else:
            current = state.parameter_states.get(parameter_id)
            if current is None:
                continue
            state_value = current
        full_states[parameter_id] = {
            "value": state_value.value,
            "revision": state_value.revision,
        }
    result = compile_document(
        CompileRequest(
            document=candidate,
            source_catalog=source_catalog,
            action_registry=action_registry,
            parameter_states=full_states,
        )
    )
    if not result.ok:
        plan.diagnostics = sorted_diagnostics(result.diagnostics)
        return plan
    plan.document_hash = result.plan.documentHash if result.plan else None
    return plan


def initial_state_from_document(
    document: Document,
) -> dict[str, ParameterStateValue]:
    """Return the starting state of every parameter of a new workspace.

    Defaults apply only when a parameter is created (SPEC.md 6.1), so
    workspace creation stores each default at revision 1.
    """

    return {
        parameter_id: ParameterStateValue(
            value=parameter.default, revision=1
        )
        for parameter_id, parameter in document.parameters.items()
    }


__all__ = [
    "EntityTouch",
    "MutationPlan",
    "PendingSet",
    "initial_state_from_document",
    "plan_mutation",
]
