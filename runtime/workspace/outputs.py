"""Pane-independent outputs and the v0.1 migration (RFC 001 section 8).

The extension language of RFC 001 separates publication from panes:

- An action declares output names, kinds, and a publication group. It
  carries no targets. The service issues one stable output incarnation
  when the action entity is created, so a subscription can name the
  calculation it follows; deleting and recreating an action issues
  another one and old subscribers do not attach to the new calculation
  (section 8.1).
- A projection source may subscribe to an output. A following
  subscription resolves the output's current pointer at every commit
  sequence; a pinned subscription freezes one version of the output's
  stable logical source and does not advance (section 8.1).
- Publication commits every declared output of a group atomically and
  eligibility depends on the action and output contract alone: hiding,
  closing, or removing a projection neither cancels a run nor
  invalidates its outputs (section 8.2).
- A current pointer belongs to the output identity that published it:
  the owning action and its incarnation, never the reusable group
  name. An action that never published reports no pointer, and the
  previous owner keeps its own result when a transaction hands the
  group name to another action (section 8.1, backlog task T045).
- An explicit migration turns each v0.1 target into a following
  subscription, preserving source versions, projection IDs, parameter
  values, and layout (section 8.3).

This module holds the pure pieces: the incarnation minter, the
pointer and subscriber resolution the inspection view and the test
renderer share, the service-side subscription verification inputs,
and the migration candidate builder. The transactions belong to
:mod:`runtime.workspace.service`.
"""

from __future__ import annotations

import copy
import secrets
from typing import Any, Mapping

from melt.schema.records import RunRecord
from melt.schema.semantic import SEMANTIC_LANGUAGE_ID, is_subscription_wire

OUTPUT_INCARNATION_PREFIX = "out-"
"""The prefix of every service-issued output incarnation."""

_INCARNATION_ENTROPY_BYTES = 12
"""The random body of one output incarnation token."""


def mint_output_incarnation() -> str:
    """Issue one opaque output incarnation (RFC 001 section 8.1).

    The token is random and never reused, so a client cannot guess the
    identity of a calculation before the service creates it, and a
    recreated action cannot inherit the subscribers of its
    predecessor.
    """

    return OUTPUT_INCARNATION_PREFIX + secrets.token_urlsafe(
        _INCARNATION_ENTROPY_BYTES
    )


def _run_sort_key(run: RunRecord) -> tuple:
    return (run.generation, run.createdAt, run.runId)


def current_runs_by_owner(
    runs: Mapping[str, RunRecord],
) -> dict[str, RunRecord]:
    """The current run of every action identity, keyed by action ID.

    The current run of an output identity is the newest run of the
    owning action whose publication became current. Its committed
    output references are the output pointers: a successful publication
    advances them in one transaction, and an obsolete run stays
    historical without advancing anything (RFC 001 section 8.2).

    The key is the owning action, never the publication group name: a
    revision-checked transaction may move an action to another group
    and hand the freed name to an action that never ran, and a group
    keyed lookup would then lend the previous owner's run — and its
    source versions — to the new owner's subscribers (RFC 001 section
    8.1, backlog task T045).
    """

    current: dict[str, RunRecord] = {}
    for run in runs.values():
        if run.disposition != "current":
            continue
        held = current.get(run.actionId)
        if held is None or _run_sort_key(run) > _run_sort_key(held):
            current[run.actionId] = run
    return current


def owns_output_identity(run: RunRecord, incarnation: "str | None") -> bool:
    """Report whether one run belongs to the output identity named.

    The run must belong to the action that holds the identity, and —
    when the run captured the identity's stable incarnation — the
    captured incarnation must still be the one the service issued.
    A record saved before output identity capture holds no incarnation;
    it resolves through its owning action alone, exactly as it did
    before identity capture existed, and the invalidation that rides a
    delete-and-recreate transaction still retires it (RFC 001 8.1).
    """

    if run.outputIncarnation is None:
        return True
    return run.outputIncarnation == incarnation


def current_run_of(
    runs: Mapping[str, RunRecord],
    action_id: str,
    incarnation: "str | None",
) -> "RunRecord | None":
    """The current run of one action's output identity, or None.

    An action with no publication of its own has no current pointer:
    neither another action's run nor a historical run of a retired
    incarnation stands in for it (RFC 001 sections 8.1 and 8.2).
    """

    run = current_runs_by_owner(runs).get(action_id)
    if run is None or not owns_output_identity(run, incarnation):
        return None
    return run


def output_pointer(run: "RunRecord | None", output: str) -> "dict | None":
    """The pointer of one output, or None while no result is current."""

    if run is None or output not in run.outputs:
        return None
    reference = run.outputs[output]
    return {
        "sourceId": reference.sourceId,
        "version": reference.version,
        "runId": run.runId,
        "generation": run.generation,
    }


def subscriber_view(
    document: Mapping[str, Any],
    runs: Mapping[str, RunRecord],
    output_incarnations: Mapping[str, str],
    output_source_of: Mapping[tuple[str, str], str],
    projection_id: str,
) -> "dict | None":
    """Resolve one projection's subscription against committed state.

    The answer carries the follow mode, the resolved source reference,
    and the version status the subscriber must show. A following
    subscriber reports ``current`` or ``empty``; a pinned subscriber
    reports ``pinned`` with a ``stale`` flag while it holds a version
    the current pointer moved past. An output whose stable logical
    source cannot be resolved reports ``unavailable`` without changing
    anything (RFC 001 sections 8.1 and 8.2).

    Resolution binds to the output identity the subscription names: the
    owning action and its issued incarnation. The action's current
    publication group takes no part, because the group name is reusable
    and a run of another action must never stand in as this subscriber's
    current result (RFC 001 section 8.1, backlog task T045).
    """

    projection = (document.get("projections") or {}).get(projection_id)
    source = (projection or {}).get("source")
    if not is_subscription_wire(source):
        return None
    identity = source["actionOutput"]
    action = (document.get("actions") or {}).get(identity["action"])
    run = (
        None
        if action is None
        else current_run_of(
            runs,
            identity["action"],
            output_incarnations.get(identity["action"]),
        )
    )
    pointer = output_pointer(run, identity["output"])
    follow = source.get("follow")
    view: dict[str, Any] = {
        "projection": projection_id,
        "action": identity["action"],
        "output": identity["output"],
        "follow": follow,
    }
    if follow == "pinned":
        # The stable logical source of the output: the pointer names it
        # once a publication is current, and the service-owned mapping
        # names it for a historical version too.
        source_id = (
            pointer["sourceId"]
            if pointer is not None
            else output_source_of.get((identity["action"], identity["output"]))
        )
        version = source.get("version")
        versions = (
            (document.get("sources") or {}).get(source_id, {}).get("versions")
            if source_id is not None
            else None
        ) or {}
        if source_id is None or str(version) not in versions:
            view["status"] = "unavailable"
            view["reason"] = (
                "the output holds no published source version"
                f" {version!r} to pin"
                if source_id is not None
                else "the output has never published a source to pin"
            )
            return view
        view["source"] = {"sourceId": source_id, "version": version}
        view["status"] = "pinned"
        view["stale"] = pointer is None or pointer["version"] != version
        return view
    if pointer is None:
        view["status"] = "empty"
        return view
    view["source"] = {"sourceId": pointer["sourceId"], "version": pointer["version"]}
    view["status"] = "current"
    view["provenance"] = {
        "runId": pointer["runId"],
        "generation": pointer["generation"],
    }
    return view


def output_views(
    document: Mapping[str, Any],
    runs: Mapping[str, RunRecord],
    output_incarnations: Mapping[str, str],
    output_source_of: Mapping[tuple[str, str], str],
    entity_revisions: Mapping[str, int],
) -> dict[str, dict]:
    """Build the inspection view of every action's outputs.

    Inspection exposes the incarnation of each action's outputs, the
    current pointer of each output with its provenance, and every
    subscriber with its resolution, so a caller can add a view that
    follows a saved result without rerunning the calculation (RFC 001
    sections 8.1 and 9).

    Every pointer resolves through the action's own runs, so an action
    with no publication of its own reports no pointer even when another
    action reuses its former group name (backlog task T045).
    """

    subscribers: dict[tuple[str, str], list[dict]] = {}
    for projection_id in sorted(document.get("projections") or {}):
        source = (document["projections"][projection_id] or {}).get("source")
        if not is_subscription_wire(source):
            continue
        identity = source["actionOutput"]
        subscribers.setdefault(
            (identity["action"], identity["output"]), []
        ).append(
            subscriber_view(
                document,
                runs,
                output_incarnations,
                output_source_of,
                projection_id,
            )
        )
    views: dict[str, dict] = {}
    for action_id, action in sorted((document.get("actions") or {}).items()):
        run = current_run_of(
            runs, action_id, output_incarnations.get(action_id)
        )
        outputs: dict[str, dict] = {}
        for output_name, kind in sorted((action.get("outputs") or {}).items()):
            outputs[output_name] = {
                "kind": kind,
                "pointer": output_pointer(run, output_name),
                "subscribers": [
                    dict(entry)
                    for entry in subscribers.get((action_id, output_name), ())
                ],
            }
        views[action_id] = {
            "incarnation": output_incarnations.get(action_id),
            "revision": entity_revisions.get(f"actions/{action_id}", 0),
            "outputGroup": action.get("outputGroup"),
            "outputs": outputs,
        }
    return views


# ---------------------------------------------------------------------------
# The explicit v0.1 migration (RFC 001 section 8.3)
# ---------------------------------------------------------------------------


MIGRATION_EXPLANATION = (
    "Each v0.1 target becomes a following subscription. Source"
    " versions, projection IDs, parameter values, and layout are"
    " unchanged. Pane removal no longer invalidates output publication:"
    " eligibility depends on the action and its output contract, not"
    " on pane existence (RFC 001 section 8.3)."
)
"""What the migration preview explains (RFC 001 section 8.3)."""


def migration_candidate(
    document: Mapping[str, Any], incarnations: Mapping[str, str]
) -> "tuple[dict, list[dict]]":
    """Build the extension candidate of one `melt/v0.1` document.

    The candidate keeps every source, parameter, projection, binding,
    and the layout exactly as saved. It adds the empty extension maps,
    drops the target map of every action, and replaces the source of
    each target projection with one following subscription to the
    output that targeted it, carrying the incarnation the caller
    issued for that action. The caller validates the candidate before
    anything commits; a failed migration leaves v0.1 current.
    """

    candidate = copy.deepcopy(dict(document))
    candidate["language"] = SEMANTIC_LANGUAGE_ID
    candidate["collections"] = {}
    candidate["state"] = {}
    subscriptions: list[dict] = []
    for action_id in sorted(candidate.get("actions") or {}):
        action = candidate["actions"][action_id]
        targets = action.pop("targets", None) or {}
        incarnation = incarnations.get(action_id)
        for output_name in sorted(targets):
            projection_id = targets[output_name]
            projection = (candidate.get("projections") or {}).get(projection_id)
            if projection is None:  # pragma: no cover - v0.1 compilation held
                raise ValueError(
                    f"action {action_id!r} targets projection"
                    f" {projection_id!r}, which the document does not hold"
                )
            projection["source"] = {
                "actionOutput": {
                    "action": action_id,
                    "output": output_name,
                    "incarnation": incarnation,
                },
                "follow": "current",
            }
            subscriptions.append(
                {
                    "projection": projection_id,
                    "action": action_id,
                    "output": output_name,
                    "incarnation": incarnation,
                }
            )
    return candidate, subscriptions


__all__ = [
    "MIGRATION_EXPLANATION",
    "OUTPUT_INCARNATION_PREFIX",
    "current_run_of",
    "current_runs_by_owner",
    "migration_candidate",
    "mint_output_incarnation",
    "output_pointer",
    "output_views",
    "owns_output_identity",
    "subscriber_view",
]
