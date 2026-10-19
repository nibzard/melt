"""Inspection snapshots and preview effect reports (SPEC.md 12.4).

``workspace.inspect`` answers the bounded document, entity revisions,
parameter state, active runs, and the diagnostics of the committed
definitions. Optional collection and ID filters bound the answer, so a
caller can ask for one collection or a handful of records without
moving the whole document.

``workspace.preview`` answers what one planned transaction would do:
diagnostics, affected record paths, controller replacements, state
resets, invalidated runs, and the candidate document hash. Both paths
are read-only; neither reserves a revision or writes a record.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from melt.schema.canonical import model_to_wire
from melt.schema.records import RunRecord, record_to_wire
from melt.schema.semantic import SEMANTIC_LANGUAGE_ID
from runtime.compiler.catalog import ActionRegistry, SourceCatalog
from runtime.compiler.compiler import CompileRequest, compile_document
from runtime.workspace.mutations import MutationPlan
from runtime.workspace.state import (
    LAYOUT_PATH,
    STATE_ENTITY_PATH_PREFIX,
    WorkspaceState,
    entity_path,
)

ACTIVE_RUN_STATES = ("queued", "running")
"""Run states an inspection reports as active (SPEC.md section 7.3)."""

DOCUMENT_COLLECTIONS = (
    "sources",
    "parameters",
    "actions",
    "projections",
    "bindings",
)
"""The collections of a v0.1 document an inspection can bound."""

SEMANTIC_DOCUMENT_COLLECTIONS = ("collections", "state")
"""The collections a `melt/semantic-draft-1` document adds (RFC 001).

They appear in the bounded document and the revision list only when
the committed document carries them, so a `melt/v0.1` inspection
answer keeps exactly its v0.1 shape."""

ENTITY_COLLECTIONS = frozenset(DOCUMENT_COLLECTIONS) - {"sources"}
"""The collections the mutation operations address (SPEC.md 12.2)."""

SERVICE_COLLECTIONS = ("registrations",)
"""Service-owned record collections named by change notifications.

``registrations`` holds no document entities, so it appears only in
the unfiltered view: its revisions tell a subscriber what the registry
last committed (SPEC.md sections 7.2 and 12.5).
"""


def active_run_records(
    runs: Mapping[str, RunRecord]
) -> list[RunRecord]:
    """Return the queued or running runs, ordered by run ID."""

    return [
        runs[run_id]
        for run_id in sorted(runs)
        if runs[run_id].state in ACTIVE_RUN_STATES
    ]


def current_diagnostics(
    state: WorkspaceState,
    source_catalog: SourceCatalog,
    action_registry: ActionRegistry,
) -> list[dict]:
    """Recompile the committed document and report its diagnostics.

    Inspection stays readable when a registration or artifact left the
    trusted inputs: the diagnostics name the drift, while the saved
    projections remain inspectable (SPEC.md sections 7.2 and 14). The
    stored reference selections of a semantic workspace ride along, so
    a collection that lost its incarnation is named the same way, and
    the issued output incarnations ride along with them.
    """

    if state.document is None:
        return []
    result = compile_document(
        CompileRequest(
            document=state.document,
            source_catalog=source_catalog,
            action_registry=action_registry,
            parameter_states={
                parameter_id: {
                    "value": stored.value,
                    "revision": stored.revision,
                }
                for parameter_id, stored in state.parameter_states.items()
            },
            reference_states={
                state_id: {
                    "value": None
                    if stored.value is None
                    else model_to_wire(stored.value),
                    "revision": stored.revision,
                }
                for state_id, stored in state.reference_states.items()
            },
            collection_incarnations=dict(state.collection_incarnations),
            output_incarnations=dict(state.output_incarnations),
        )
    )
    return [model_to_wire(item) for item in result.diagnostics]


def snapshot_result(
    state: WorkspaceState,
    runs: Mapping[str, RunRecord],
    diagnostics: "Sequence[dict]",
    *,
    collection: "str | None" = None,
    ids: "Iterable[str] | None" = None,
    output_groups: "Mapping[str, Mapping] | None" = None,
    semantic: "Mapping[str, Mapping] | None" = None,
) -> dict:
    """Build the inspection snapshot of one committed state.

    With no filter the result carries the full document, every record
    revision, and every stored parameter value. A ``collection`` filter
    bounds the document to that one collection; ``ids`` bounds the
    entity collections to the listed identifiers. Sources and the
    layout tree belong to the unfiltered view only, because no entity
    collection addresses them. ``output_groups`` carries the finished
    result-status map of SPEC.md section 13.5 into the unfiltered view;
    the service computes it from its scheduling records. ``semantic``
    carries the RFC 001 view — collection incarnations and stored
    reference selections with their resolution — into the unfiltered
    view of a `melt/semantic-draft-1` workspace.
    """

    document = state.document or {}
    semantic_document = document.get("language") == SEMANTIC_LANGUAGE_ID
    wanted = frozenset(ids) if ids is not None else None
    document_collections = list(DOCUMENT_COLLECTIONS)
    if semantic_document:
        document_collections.extend(SEMANTIC_DOCUMENT_COLLECTIONS)
    included = (
        frozenset(document_collections) | frozenset(SERVICE_COLLECTIONS)
        if collection is None
        else {collection}
    )

    def _entities(name: str) -> dict:
        entities = dict(document.get(name, {}))
        addressable = ENTITY_COLLECTIONS | frozenset(SEMANTIC_DOCUMENT_COLLECTIONS)
        if wanted is not None and name in addressable:
            entities = {
                entity_id: value
                for entity_id, value in entities.items()
                if entity_id in wanted
            }
        return entities

    bounded: dict = {
        "language": document.get("language"),
        "workspaceId": document.get("workspaceId"),
        "title": document.get("title"),
    }
    for name in document_collections:
        if name in included and (semantic_document or name in DOCUMENT_COLLECTIONS):
            bounded[name] = _entities(name)
    if collection is None:
        bounded["layout"] = document.get("layout")

    revisions: dict[str, int] = {}
    for path, revision in sorted(state.entity_revisions.items()):
        if path == LAYOUT_PATH:
            if collection is None:
                revisions[path] = revision
            continue
        collection_name, _, entity_id = path.partition("/")
        if collection_name == STATE_ENTITY_PATH_PREFIX:
            # The RFC 001 `state` entity records address their paths
            # under the reserved `semantic-state` prefix.
            collection_name = "state"
        if collection_name not in included:
            continue
        if wanted is not None and entity_id not in wanted:
            continue
        revisions[path] = revision

    parameters_included = collection in (None, "parameters")
    parameter_states: dict[str, dict] = {}
    incarnations: dict[str, int] = {}
    if parameters_included:
        for parameter_id, stored in sorted(state.parameter_states.items()):
            if wanted is not None and parameter_id not in wanted:
                continue
            parameter_states[parameter_id] = {
                "value": stored.value,
                "revision": stored.revision,
            }
        for parameter_id, incarnation in sorted(state.incarnations.items()):
            if wanted is not None and parameter_id not in wanted:
                continue
            incarnations[parameter_id] = incarnation

    result = {
        "workspaceId": state.workspace_id,
        "epoch": state.epoch,
        "present": state.document is not None,
        "document": bounded,
        "entityRevisions": revisions,
        "parameterStates": parameter_states,
        "incarnations": incarnations,
        "activeRuns": [record_to_wire(run) for run in active_run_records(runs)],
        "diagnostics": list(diagnostics),
    }
    if output_groups is not None and collection is None:
        result["outputGroups"] = {
            group_id: dict(entry) for group_id, entry in sorted(output_groups.items())
        }
    if semantic is not None and collection is None:
        result["semantic"] = {
            name: entry for name, entry in sorted(semantic.items())
        }
    return result


def _controller_replacements(
    state: WorkspaceState, plan: MutationPlan
) -> list[dict]:
    """Report the panes whose controller a plan would replace.

    A controller survives while the projection ID and kind stay
    unchanged (SPEC.md section 8.5). A surviving ID whose kind changes,
    through replacement or a delete-and-create pair, replaces its
    controller; the dry run names it before any commit.
    """

    replacements: list[dict] = []
    before = (state.document or {}).get("projections", {})
    after = plan.candidate.get("projections", {})
    for path, touch in sorted(plan.entity_touches.items()):
        collection, _, projection_id = path.partition("/")
        if collection != "projections":
            continue
        if not touch.existed_before or not touch.exists_after:
            continue
        old = before.get(projection_id)
        new = after.get(projection_id)
        if old is None or new is None:
            continue
        if old.get("kind") != new.get("kind"):
            replacements.append(
                {
                    "projection": projection_id,
                    "fromKind": old.get("kind"),
                    "toKind": new.get("kind"),
                }
            )
    return replacements


def _state_resets(plan: MutationPlan) -> list[str]:
    """Report the parameters whose stored state a plan resets.

    A created parameter starts from its default or supplied value in a
    new identity incarnation; a deleted parameter loses its state
    entirely (SPEC.md section 12.3).
    """

    resets: list[str] = []
    for path, touch in sorted(plan.entity_touches.items()):
        collection, _, parameter_id = path.partition("/")
        if collection != "parameters":
            continue
        if touch.created or not touch.exists_after:
            resets.append(parameter_id)
    return resets


def _invalidated_runs(
    state: WorkspaceState, plan: MutationPlan, runs: Mapping[str, RunRecord]
) -> list[str]:
    """Report the active runs a plan would make ineligible.

    An action definition change, even an equivalent replacement,
    invalidates its admitted runs, and removing a target projection
    invalidates the whole output group's publication (SPEC.md sections
    12.3 and 13.3). Recreating a parameter invalidates every active run
    that captured it, because the recreation starts a new identity
    incarnation while the state revision restarts at one (section 12.3;
    backlog task T044). Terminal runs keep their historical disposition
    and are not reported.
    """

    invalidated: list[str] = []
    actions = (state.document or {}).get("actions", {})
    recreated = set(plan.incarnation_bumps)
    for run_id, run in sorted(runs.items()):
        if run.state not in ACTIVE_RUN_STATES:
            continue
        if entity_path("actions", run.actionId) in plan.entity_touches:
            invalidated.append(run_id)
            continue
        action = actions.get(run.actionId)
        if action is None:
            continue
        for target in action.get("targets", {}).values():
            touch = plan.entity_touches.get(entity_path("projections", target))
            if touch is not None and not touch.exists_after:
                invalidated.append(run_id)
                break
        else:
            captured_ids = set(run.parameterIds.values())
            if not captured_ids:
                # An older record without its id map falls back to the
                # definitions the action still declares.
                captured_ids = {
                    mapping["parameter"]
                    for mapping in action.get("parameters", {}).values()
                }
            if captured_ids & recreated:
                invalidated.append(run_id)
    return invalidated


def _affected_observers(plan: MutationPlan) -> "dict[str, list[str]]":
    """The observers of every reference state a plan would change.

    Preview identifies the projections whose selector reads a written
    state entry (RFC 001 section 9). An observer addition alone writes
    no state, so its preview reports no affected observer.
    """

    observers: dict[str, list[str]] = {}
    for state_id in sorted(plan.new_reference_states):
        for projection_id, projection in sorted(
            (plan.candidate.get("projections") or {}).items()
        ):
            for observed in (projection.get("observe") or {}).values():
                if (observed or {}).get("state") == state_id:
                    observers.setdefault(state_id, []).append(
                        f"projections/{projection_id}"
                    )
    return observers


def preview_effects(
    state: WorkspaceState,
    plan: MutationPlan,
    runs: Mapping[str, RunRecord],
) -> dict:
    """Build the effect report of one planned, valid candidate.

    The caller planned the transaction without writing; this function
    only reads the plan. ``diagnostics`` is empty because an invalid
    plan never reaches a preview result: it answers the same rejected
    response an apply would receive.
    """

    result = {
        "diagnostics": [],
        "affectedRecords": plan.changed_records(),
        "controllerReplacements": _controller_replacements(state, plan),
        "stateResets": _state_resets(plan),
        "invalidatedRuns": _invalidated_runs(state, plan, runs),
        "candidateHash": plan.document_hash,
        # What each surviving parameter's state would become. A reset
        # parameter that survives the plan lands here; a removed one
        # only appears in stateResets.
        "parameterStateChanges": {
            parameter_id: {"value": stored.value, "revision": stored.revision}
            for parameter_id, stored in sorted(plan.new_states.items())
        },
        # What each reference state record would become (RFC 001 4):
        # an assignment or a created entry, never a silent reset.
        "referenceStateChanges": {
            state_id: {
                "value": None
                if stored.value is None
                else model_to_wire(stored.value),
                "revision": stored.revision,
            }
            for state_id, stored in sorted(plan.new_reference_states.items())
        },
    }
    affected = _affected_observers(plan)
    if affected:
        # Only the extension writes reference state, so a `melt/v0.1`
        # preview keeps its exact v0.1 shape (RFC 001 section 9).
        result["affectedObservers"] = affected
    return result


__all__ = [
    "ACTIVE_RUN_STATES",
    "DOCUMENT_COLLECTIONS",
    "ENTITY_COLLECTIONS",
    "SEMANTIC_DOCUMENT_COLLECTIONS",
    "SERVICE_COLLECTIONS",
    "active_run_records",
    "current_diagnostics",
    "preview_effects",
    "snapshot_result",
]
