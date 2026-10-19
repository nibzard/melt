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
   replacement and the expected state revision (section 12.3). The
   reference state of `melt/semantic-draft-1` follows the same rule
   with ``state.set``: an assignment must name the declared collection
   and its current incarnation, and a stored selection survives a
   definition change only while it stays valid (RFC 001 section 4).
4. The final candidate is validated once through the compiler stages,
   with the resulting parameter and reference state and the trusted
   catalog and registry inputs of the service.

The plan carries the new revisions, the resulting parameter state, the
identity incarnation changes, and the compiler read set observed during
planning. Committing applies every change or none: a plan with
diagnostics never reaches the store.

The extension operations of RFC 001 are planned here and refused for a
`melt/v0.1` workspace, whose document defines neither the
``collections`` nor the ``state`` map: those operations address record
paths that language never creates.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable

from melt.schema.canonical import model_to_wire
from melt.schema.commands import (
    CommandEnvelope,
    EntityCreate,
    EntityDelete,
    EntityReplace,
    LayoutReplace,
    ParameterSet,
    StateSet,
)
from melt.schema.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    sorted_diagnostics,
)
from melt.schema.document import Document, Parameter
from melt.schema.semantic import (
    SEMANTIC_LANGUAGE_ID,
    SemanticReference,
    SemanticState,
)
from runtime.compiler.compiler import (
    CompileRequest,
    compile_document,
    parameter_value_error,
    reference_collection_error,
    reference_incarnation_error,
)
from runtime.workspace.outputs import mint_output_incarnation
from runtime.workspace.semantic import mint_incarnation
from runtime.workspace.state import (
    LAYOUT_PATH,
    ParameterStateValue,
    ReferenceStateValue,
    STATE_ENTITY_PATH_PREFIX,
    WorkspaceState,
    entity_path,
    pointer_key,
    reference_state_path,
    reference_values_equal,
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
class PendingReferenceSet:
    """One ``state.set`` held until the final validation."""

    operation: int
    value: "SemanticReference | None"


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
    # The RFC 001 extension state of `melt/semantic-draft-1`.
    touched_reference_state_paths: dict[str, PendingReferenceSet] = field(
        default_factory=dict
    )
    new_reference_states: dict[str, ReferenceStateValue] = field(
        default_factory=dict
    )
    removed_reference_states: set[str] = field(default_factory=set)
    collection_incarnation_mints: dict[str, str] = field(default_factory=dict)
    collection_incarnation_removals: set[str] = field(default_factory=set)
    candidate_incarnations: dict[str, str] = field(default_factory=dict)
    # The stable output identities of RFC 001 section 8.1: one
    # incarnation per action of the extension language.
    output_incarnation_mints: dict[str, str] = field(default_factory=dict)
    output_incarnation_removals: set[str] = field(default_factory=set)
    candidate_output_incarnations: dict[str, str] = field(
        default_factory=dict
    )

    @property
    def ok(self) -> bool:
        return not self.diagnostics

    def changed_records(self) -> list[str]:
        """The record paths this transaction changes, sorted.

        A replaced record always counts as changed, even when the
        replacement appears equivalent (SPEC.md section 13.3). A
        ``parameter.set`` with an equal value changes nothing, and a
        ``state.set`` with an equal reference changes nothing too.
        """

        changed = [
            path
            for path, touch in self.entity_touches.items()
            if touch.created or touch.deleted or touch.exists_after
        ]
        changed.extend(state_path(parameter_id) for parameter_id in self.new_states)
        changed.extend(state_path(parameter_id) for parameter_id in self.removed_states)
        changed.extend(
            reference_state_path(state_id) for state_id in self.new_reference_states
        )
        changed.extend(
            reference_state_path(state_id)
            for state_id in self.removed_reference_states
        )
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


def expectation_diagnostics(
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
    mint: "Callable[[], str] | None" = None,
) -> MutationPlan:
    """Plan one ``workspace.apply`` transaction without writing.

    ``mint`` issues the incarnation of a collection the transaction
    creates. Tests may inject a deterministic minter; the service
    passes the opaque token minter of RFC 001 section 3.2.
    """

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

    issue_incarnation = mint or mint_incarnation
    language = state.document.get("language")
    candidate = copy.deepcopy(state.document)
    plan.candidate = candidate
    touches: dict[str, EntityTouch] = {}
    pending_sets: dict[str, PendingSet] = {}
    pending_reference_sets: dict[str, PendingReferenceSet] = {}
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

    def _language_error(path: str, collection: str) -> Diagnostic:
        return _diagnostic(
            DiagnosticCode.VERSION,
            path,
            f"the {language!r} document defines no {collection!r}"
            " entity collection; the semantic collections and reference"
            " state of RFC 001 need melt/semantic-draft-1",
            hint="Migrate the workspace to the extension language, or"
            " address a collection the language defines.",
        )

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
        elif isinstance(operation, StateSet):
            path = reference_state_path(operation.id)
            if path not in touched_order:
                touched_order.append(path)
        elif isinstance(operation, LayoutReplace):
            _touch(LAYOUT_PATH, index)

    expected = dict(envelope.expected or {})
    diagnostics = expectation_diagnostics(state, expected, touched_order)
    if diagnostics:
        plan.diagnostics = sorted_diagnostics(diagnostics)
        plan.entity_touches = touches
        plan.touched_state_paths = pending_sets
        plan.touched_reference_state_paths = pending_reference_sets
        return plan

    # ------------------------------------------------------------------
    # Stage B: apply the operations in order against the candidate
    # ------------------------------------------------------------------

    for index, operation in enumerate(operations):
        path = f"/operations/{index}"
        if isinstance(operation, (EntityCreate, EntityReplace, EntityDelete)):
            if operation.collection not in candidate:
                diagnostics.append(
                    _language_error(f"{path}/collection", operation.collection)
                )
                continue
            collection = candidate[operation.collection]
            record = entity_path(operation.collection, operation.id)
            if isinstance(operation, EntityCreate):
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
            else:
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
        elif isinstance(operation, StateSet):
            if "state" not in candidate:
                diagnostics.append(_language_error(f"{path}/op", "state"))
                continue
            pending_reference_sets[operation.id] = PendingReferenceSet(
                operation=index, value=operation.value
            )
        elif isinstance(operation, LayoutReplace):
            candidate["layout"] = model_to_wire(operation.value)
            _touch(LAYOUT_PATH, index)

    if diagnostics:
        plan.diagnostics = sorted_diagnostics(diagnostics)
        plan.entity_touches = touches
        plan.touched_state_paths = pending_sets
        plan.touched_reference_state_paths = pending_reference_sets
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
        plan.touched_reference_state_paths = pending_reference_sets
        return plan

    plan.new_states = resulting_states
    plan.entity_touches = touches
    plan.touched_state_paths = pending_sets

    # ------------------------------------------------------------------
    # Stage C': the semantic collections and reference state of the
    # melt/semantic-draft-1 extension (RFC 001 sections 3 and 4)
    # ------------------------------------------------------------------

    resulting_reference_states: dict[str, ReferenceStateValue] = {}
    candidate_incarnations = dict(state.collection_incarnations)
    for record, touch in touches.items():
        collection_name, _, entity_id = record.partition("/")
        if collection_name != "collections":
            continue
        if touch.created and touch.exists_after:
            # Creating the collection issues its opaque incarnation;
            # replacing a definition keeps it, because the collection
            # identity survives a binding change (RFC 001 3.2).
            incarnation = issue_incarnation()
            candidate_incarnations[entity_id] = incarnation
            plan.collection_incarnation_mints[entity_id] = incarnation
        if not touch.exists_after:
            candidate_incarnations.pop(entity_id, None)
            plan.collection_incarnation_removals.add(entity_id)
    plan.candidate_incarnations = candidate_incarnations

    # The stable output identities of RFC 001 section 8.1: creating an
    # action of the extension issues one opaque incarnation, replacing
    # a definition keeps it — the calculation's identity survives a
    # mapping change — and deleting the action removes it, so old
    # subscribers cannot attach to a recreated calculation.
    candidate_output_incarnations = dict(state.output_incarnations)
    if language == SEMANTIC_LANGUAGE_ID:
        for record, touch in touches.items():
            collection_name, _, entity_id = record.partition("/")
            if collection_name != "actions":
                continue
            if touch.created and touch.exists_after:
                incarnation = mint_output_incarnation()
                candidate_output_incarnations[entity_id] = incarnation
                plan.output_incarnation_mints[entity_id] = incarnation
            if not touch.exists_after:
                candidate_output_incarnations.pop(entity_id, None)
                plan.output_incarnation_removals.add(entity_id)
    plan.candidate_output_incarnations = candidate_output_incarnations

    state_definitions_after = candidate.get("state", {})
    collections_after = candidate.get("collections", {})
    for state_id in sorted(state_definitions_after):
        record = entity_path("state", state_id)
        touch = touches.get(record)
        pending = pending_reference_sets.get(state_id)
        entry = SemanticState.model_validate(state_definitions_after[state_id])
        current = state.reference_states.get(state_id)

        def _assignment_error(
            value: "SemanticReference | None",
        ) -> "tuple[str, str] | None":
            """The code and message of one invalid assignment, or None.

            A reference must match the declared collection
            (`E_TYPE`, a reference-type mismatch) and the collection's
            current incarnation in the candidate (`E_CONFLICT`, a
            stale identity), exactly as RFC 001 sections 3.2 and 4
            require at assignment time.
            """

            if value is None:
                return None
            message = reference_collection_error(entry, value)
            if message is not None:
                return DiagnosticCode.TYPE, message
            if value.collection in collections_after:
                message = reference_incarnation_error(value, candidate_incarnations)
                if message is not None:
                    return DiagnosticCode.CONFLICT, message
            return None

        if touch is not None and touch.created:
            value = pending.value if pending is not None else None
            if pending is not None:
                error = _assignment_error(pending.value)
                if error is not None:
                    code, message = error
                    diagnostics.append(
                        _diagnostic(
                            code,
                            f"/operations/{pending.operation}/value",
                            f"reference does not satisfy state"
                            f" {state_id!r}: {message}",
                        )
                    )
                    continue
            resulting_reference_states[state_id] = ReferenceStateValue(
                value=value, revision=1
            )
            continue

        if touch is not None and not touch.exists_after:
            continue

        value = (
            pending.value
            if pending is not None
            else current.value
            if current is not None
            else None
        )
        if current is None:
            # Cannot happen while every creation initializes state, but
            # a hand-forged snapshot is refused rather than silently
            # repaired.
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.STATE_MIGRATION,
                    f"/operations/{touch.last_operation}" if touch else "/operations",
                    f"reference state {state_id!r} holds no stored record",
                )
            )
            continue

        error = _assignment_error(value)
        if error is not None:
            code, message = error
            if pending is not None:
                diagnostics.append(
                    _diagnostic(
                        code,
                        f"/operations/{pending.operation}/value",
                        f"reference does not satisfy state {state_id!r}:"
                        f" {message}",
                    )
                )
            elif touch is not None:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.STATE_MIGRATION,
                        f"/operations/{touch.last_operation}",
                        f"reference state {state_id!r}: the stored"
                        f" selection violates the new definition:"
                        f" {message}",
                        hint="Supply state.set with a valid reference or"
                        " null and the expected state revision in the"
                        " same transaction (RFC 001 section 4).",
                    )
                )
            continue

        if pending is not None and not reference_values_equal(
            value, current.value
        ):
            resulting_reference_states[state_id] = ReferenceStateValue(
                value=value, revision=current.revision + 1
            )

    for record, touch in touches.items():
        if not record.startswith(f"{STATE_ENTITY_PATH_PREFIX}/"):
            continue
        state_id = record[len(STATE_ENTITY_PATH_PREFIX) + 1 :]
        if touch.deleted and not touch.exists_after:
            plan.removed_reference_states.add(state_id)

    if diagnostics:
        plan.diagnostics = sorted_diagnostics(diagnostics)
        return plan

    plan.new_reference_states = resulting_reference_states
    plan.touched_reference_state_paths = pending_reference_sets

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
    full_reference_states: dict[str, dict] = {}
    for state_id in state_definitions_after:
        if state_id in resulting_reference_states:
            state_value = resulting_reference_states[state_id]
        else:
            current = state.reference_states.get(state_id)
            if current is None:
                continue
            state_value = current
        full_reference_states[state_id] = {
            "value": None
            if state_value.value is None
            else model_to_wire(state_value.value),
            "revision": state_value.revision,
        }
    result = compile_document(
        CompileRequest(
            document=candidate,
            source_catalog=source_catalog,
            action_registry=action_registry,
            parameter_states=full_states,
            reference_states=full_reference_states,
            collection_incarnations=candidate_incarnations,
            output_incarnations=candidate_output_incarnations,
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


def initial_reference_states(
    document: Document,
) -> dict[str, ReferenceStateValue]:
    """Return the starting state of every reference state entry.

    The only default is null (RFC 001 section 4): a reference value
    names the incarnation of a collection the service has not issued
    before creation, so every entry starts empty at revision 1.
    """

    states = getattr(document, "state", None)
    if not states:
        return {}
    return {
        state_id: ReferenceStateValue(value=None, revision=1)
        for state_id in states
    }


__all__ = [
    "EntityTouch",
    "expectation_diagnostics",
    "MutationPlan",
    "PendingReferenceSet",
    "PendingSet",
    "initial_reference_states",
    "initial_state_from_document",
    "plan_mutation",
]
