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
from runtime.compiler.catalog import ActionRegistry, SourceCatalog
from runtime.compiler.compiler import CompileRequest, compile_document
from runtime.workspace.mutations import MutationPlan
from runtime.workspace.state import (
    LAYOUT_PATH,
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
"""The collections of a document an inspection can bound."""

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
    projections remain inspectable (SPEC.md sections 7.2 and 14). A
    healthy workspace compiles cleanly and reports none.
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
) -> dict:
    """Build the inspection snapshot of one committed state.

    With no filter the result carries the full document, every record
    revision, and every stored parameter value. A ``collection`` filter
    bounds the document to that one collection; ``ids`` bounds the
    entity collections to the listed identifiers. Sources and the
    layout tree belong to the unfiltered view only, because no entity
    collection addresses them. ``output_groups`` carries the finished
    result-status map of SPEC.md section 13.5 into the unfiltered view;
    the service computes it from its scheduling records.
    """

    document = state.document or {}
    wanted = frozenset(ids) if ids is not None else None
    included = (
        frozenset(DOCUMENT_COLLECTIONS) | frozenset(SERVICE_COLLECTIONS)
        if collection is None
        else {collection}
    )

    def _entities(name: str) -> dict:
        entities = dict(document.get(name, {}))
        if wanted is not None and name in ENTITY_COLLECTIONS:
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
    for name in DOCUMENT_COLLECTIONS:
        if name in included:
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
    12.3 and 13.3). Terminal runs keep their historical disposition and
    are not reported.
    """

    invalidated: list[str] = []
    actions = (state.document or {}).get("actions", {})
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
    return invalidated


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

    return {
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
    }


__all__ = [
    "ACTIVE_RUN_STATES",
    "DOCUMENT_COLLECTIONS",
    "ENTITY_COLLECTIONS",
    "SERVICE_COLLECTIONS",
    "active_run_records",
    "current_diagnostics",
    "preview_effects",
    "snapshot_result",
]
