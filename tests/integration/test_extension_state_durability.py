"""Durability of the committed semantic extension maps (T038, RFC 001
sections 2, 3.2, and 8.1; SPEC.md section 14).

Every service transaction that adopts a new workspace state carries the
collection incarnations, the output incarnations, and the reference
states forward unchanged unless the transaction itself mutates them. A
renderer event, a source import, and a script registration each rewrite
the committed state; none of them touches an extension identity, so
none of them may drop one. The erasure this guards against was durable:
the snapshot of the losing transaction replaced the committed snapshot,
so a restart restored empty maps, every later mutation failed the
subscription check, and the stored selection disappeared.

The regression drives the three transactions through the public service
handlers on one `melt/semantic-draft-1` workspace that holds a
collection, a committed selection, and an action with a subscriber, and
asserts the maps keep their values through the transaction and a
restart, and that `workspace.apply` and `workspace.preview` still
succeed afterwards. A migrated v0.1 workspace repeats the check for one
routine import, the path that erased its fresh output incarnations.

Everything runs under the Looper resource prefix, never against the
user's data directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from melt.schema.commands import parse_command
from runtime.storage import PlatformAdapter
from runtime.workspace import WorkspaceService, local_environment
from runtime.workspace.state import entity_path

from test_scheduling import (
    BUILDER,
    LIGHT_CONTRACT,
    LIGHT_SALES_SCRIPT,
    FakeClock,
    requires_storage_worker,
)
from test_semantic_interactions import SUPPLIERS_V1, suppliers_bytes

pytestmark = requires_storage_worker

WORKSPACE = "durability-demo"
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFINITIONS = REPO_ROOT / "fixtures" / "semantic" / "definitions.json"

PARQUET = "application/vnd.apache.parquet"
GRAPH_MEDIA_TYPE = "application/vnd.melt.graph+json"
PYTHON = "text/x-python"


def envelope(request_id, method, *, expected=None, operations=None, payload=None):
    body = {
        "protocolVersion": 1,
        "workspaceId": WORKSPACE,
        "requestId": request_id,
        "method": method,
    }
    if expected is not None:
        body["expected"] = expected
    if operations is not None:
        body["operations"] = operations
    if payload is not None:
        body["payload"] = payload
    return parse_command(body)


def codes(response):
    return [(error.code, error.path) for error in response.errors]


def inspect(service, request_id="inspect"):
    return service.dispatch(
        envelope(request_id, "workspace.inspect", payload={})
    )


def revision_of(service, path):
    snapshot = inspect(service).result
    if path.startswith("refstate/"):
        state = snapshot["semantic"]["referenceStates"].get(
            path[len("refstate/") :]
        )
        return 0 if state is None else int(state["revision"])
    return int(snapshot["entityRevisions"].get(path, 0))


def import_source(service, request_id, source_id, data, media_type, revision):
    token = service.capture_artifact_token(data, media_type).token
    return service.dispatch(
        envelope(
            request_id,
            "source.import",
            expected={f"sources/{source_id}": revision},
            payload={
                "sourceId": source_id,
                "artifactToken": token,
                "originLabel": "fixture",
            },
        )
    )


def register_script(service, request_id, script, contract):
    token = service.capture_artifact_token(
        script.encode("utf-8"), PYTHON
    ).token
    return service.dispatch(
        envelope(
            request_id,
            "action.register",
            payload={
                "scriptToken": token,
                "environmentId": local_environment().environment_id,
                "contract": contract,
            },
        )
    )


def event_guards(service, projection_id="network"):
    """The revision guards one renderer event carries (RFC 001 5.1)."""

    document = inspect(service).result["document"]
    projection = document["projections"][projection_id]
    guards = {
        f"projections/{projection_id}": revision_of(
            service, f"projections/{projection_id}"
        ),
        f"collections/{projection['collection']}": revision_of(
            service, f"collections/{projection['collection']}"
        ),
        "refstate/selected_supplier": revision_of(
            service, "refstate/selected_supplier"
        ),
    }
    if projection.get("source") is not None:
        guards[f"sources/{projection['source']['sourceId']}"] = revision_of(
            service, f"sources/{projection['source']['sourceId']}"
        )
    for binding_id, binding in document["bindings"].items():
        if binding["event"]["projection"] == projection_id:
            guards[f"bindings/{binding_id}"] = revision_of(
                service, f"bindings/{binding_id}"
            )
    return guards


def select(service, request_id, key, *, origin="user"):
    collection = "suppliers"
    return service.dispatch_as(
        envelope(
            request_id,
            "object.select",
            expected=event_guards(service),
            payload={
                "projectionId": "network",
                "objectRef": {
                    "collection": collection,
                    "incarnation": service.state.collection_incarnations[
                        collection
                    ],
                    "key": key,
                },
            },
        ),
        origin=origin,
    )


def clear_selection(service, request_id, *, origin="user"):
    return service.dispatch_as(
        envelope(
            request_id,
            "selection.clear",
            expected=event_guards(service),
            payload={"projectionId": "network"},
        ),
        origin=origin,
    )


def extension_maps(service) -> dict:
    """The three extension maps as one comparable value."""

    return {
        "collection_incarnations": dict(service.state.collection_incarnations),
        "output_incarnations": dict(service.state.output_incarnations),
        "reference_states": {
            state_id: (stored.value, stored.revision)
            for state_id, stored in sorted(
                service.state.reference_states.items()
            )
        },
    }


def assert_inspection_still_exposes_the_state(service, key):
    """The inspected snapshot reports every committed identity.

    An erased map showed up here as a null collection incarnation and
    an empty referenceStates view, which is how the damage reached a
    host even before a mutation failed (RFC 001 section 9).
    """

    snapshot = inspect(service).result
    semantic = snapshot["semantic"]
    incarnation = service.state.collection_incarnations["suppliers"]
    assert semantic["collections"]["suppliers"]["incarnation"] == incarnation
    stored = semantic["referenceStates"]["selected_supplier"]
    assert stored["value"] == {
        "collection": "suppliers",
        "incarnation": incarnation,
        "key": key,
    }
    assert stored["resolution"]["status"] == "resolved"
    outputs = semantic["outputs"]["analyze-sales"]
    assert outputs["incarnation"] == service.state.output_incarnations[
        "analyze-sales"
    ]


def seed_extension_workspace(adapter: PlatformAdapter) -> WorkspaceService:
    """One workspace holding every kind of committed extension state.

    The supplier fixture of RFC 001 section 10.2 — the collection, the
    reference state, the graph, the inspector, and the two bindings —
    joined with the pane-free action of section 8.1 and one following
    subscriber, so all three extension maps hold committed values
    before the regression transactions start.
    """

    from test_semantic_interactions import GRAPH_BYTES

    service = WorkspaceService.open(adapter, WORKSPACE, clock=FakeClock())
    created = service.create_workspace(
        {
            "language": "melt/semantic-draft-1",
            "workspaceId": WORKSPACE,
            "title": "Supplier durability",
            "sources": {},
            "parameters": {
                "minimum": {
                    "type": "number",
                    "default": 0,
                    "nullable": False,
                    "constraints": {"minimum": 0, "maximum": 1000000000},
                }
            },
            "actions": {},
            "projections": {},
            "bindings": {},
            "collections": {},
            "state": {},
            "layout": {"root": None, "hidden": []},
        },
        "01-create",
    )
    assert created.ok, created.errors
    assert import_source(
        service,
        "02-suppliers",
        "supplier-records",
        suppliers_bytes(SUPPLIERS_V1),
        PARQUET,
        0,
    ).ok
    assert import_source(
        service, "03-graph", "supplier-network", GRAPH_BYTES, GRAPH_MEDIA_TYPE, 0
    ).ok
    assert import_source(
        service, "04-sales", "sales-input", BUILDER.sales_parquet_bytes(),
        PARQUET, 0,
    ).ok
    registered = register_script(
        service, "05-register", LIGHT_SALES_SCRIPT, LIGHT_CONTRACT
    )
    assert registered.ok, registered.errors
    registration_id = registered.result["registrationId"]

    operations = [
        {
            "op": "entity.create",
            "collection": "actions",
            "id": "analyze-sales",
            "value": {
                "implementation": {"registrationId": registration_id},
                "parameters": {"minimum": {"parameter": "minimum"}},
                "inputs": {"sales": {"sourceId": "sales-input", "version": 1}},
                "outputs": {"filtered": "table"},
                "outputGroup": "sales-results",
            },
        },
        *json.loads(DEFINITIONS.read_text()),
    ]
    expected = {}
    for operation in operations:
        if operation["op"] == "layout.replace":
            path = "layout"
        else:
            path = entity_path(operation["collection"], operation["id"])
        expected[path] = revision_of(service, path)
    applied = service.dispatch(
        envelope(
            "06-definitions",
            "workspace.apply",
            expected=expected,
            operations=operations,
        )
    )
    assert applied.ok, applied.errors

    # The subscriber joins the already-issued output incarnation
    # (RFC 001 section 8.2: subscribe-after-publication never reruns,
    # and here subscription follows creation before any publication).
    incarnation = service.state.output_incarnations["analyze-sales"]
    subscribed = service.dispatch(
        envelope(
            "07-subscribe",
            "workspace.apply",
            expected={
                "projections/sales": 0,
                "layout": revision_of(service, "layout"),
            },
            operations=[
                {
                    "op": "entity.create",
                    "collection": "projections",
                    "id": "sales",
                    "value": {
                        "kind": "table",
                        "title": "Filtered sales",
                        "source": {
                            "actionOutput": {
                                "action": "analyze-sales",
                                "output": "filtered",
                                "incarnation": incarnation,
                            },
                            "follow": "current",
                        },
                        "config": {"columns": None, "selection": "none"},
                        "controls": [],
                    },
                },
                {
                    "op": "layout.replace",
                    "value": {
                        "root": {
                            "kind": "split",
                            "axis": "horizontal",
                            "ratio": 0.6,
                            "first": {"kind": "pane", "projection": "network"},
                            "second": {
                                "kind": "pane",
                                "projection": "inspector",
                            },
                        },
                        "hidden": ["sales"],
                    },
                },
            ],
        )
    )
    assert subscribed.ok, subscribed.errors
    return service


def reopened(adapter: PlatformAdapter) -> WorkspaceService:
    return WorkspaceService.open(adapter, WORKSPACE, clock=FakeClock())


def retarget_registration(service, request_id, registration_id):
    """One later apply: the action adopts the edited registration."""

    return service.dispatch(
        envelope(
            request_id,
            "workspace.apply",
            expected={
                "actions/analyze-sales": revision_of(
                    service, "actions/analyze-sales"
                )
            },
            operations=[
                {
                    "op": "entity.replace",
                    "collection": "actions",
                    "id": "analyze-sales",
                    "value": {
                        "implementation": {
                            "registrationId": registration_id
                        },
                        "parameters": {"minimum": {"parameter": "minimum"}},
                        "inputs": {
                            "sales": {"sourceId": "sales-input", "version": 1}
                        },
                        "outputs": {"filtered": "table"},
                        "outputGroup": "sales-results",
                    },
                }
            ],
        )
    )


def preview_a_selection_change(service, request_id, key):
    """One later preview: a typed state assignment through a dry run."""

    incarnation = service.state.collection_incarnations["suppliers"]
    return service.dispatch(
        envelope(
            request_id,
            "workspace.preview",
            expected={
                "refstate/selected_supplier": revision_of(
                    service, "refstate/selected_supplier"
                )
            },
            operations=[
                {
                    "op": "state.set",
                    "id": "selected_supplier",
                    "value": {
                        "collection": "suppliers",
                        "incarnation": incarnation,
                        "key": key,
                    },
                }
            ],
        )
    )


# ---------------------------------------------------------------------------
# The three transactions on one extension workspace
# ---------------------------------------------------------------------------


def test_select_import_and_registration_keep_the_extension_maps(
    service_adapter,
):
    service = seed_extension_workspace(service_adapter)
    try:
        committed = select(service, "08-select", "S-42")
        assert committed.ok, committed.errors
        after_selection = extension_maps(service)
        assert set(after_selection["collection_incarnations"]) == {"suppliers"}
        assert set(after_selection["output_incarnations"]) == {
            "analyze-sales"
        }
        assert set(after_selection["reference_states"]) == {
            "selected_supplier"
        }
        stored = after_selection["reference_states"]["selected_supplier"]
        assert stored[0].collection == "suppliers"
        assert stored[0].key == "S-42"
        assert stored[0].incarnation == after_selection[
            "collection_incarnations"
        ]["suppliers"]
        assert stored[1] == 2
        assert_inspection_still_exposes_the_state(service, "S-42")

        # A routine import of version 2 keeps every map.
        imported = import_source(
            service,
            "09-import-v2",
            "supplier-records",
            suppliers_bytes(SUPPLIERS_V1),
            PARQUET,
            1,
        )
        assert imported.ok, imported.errors
        assert extension_maps(service) == after_selection

        # Registering an edited script keeps every map: the registration
        # is a new identity, but the committed extension state belongs
        # to the workspace, not to the registry (RFC 001 section 8.1).
        previous_registration = service.state.document["actions"][
            "analyze-sales"
        ]["implementation"]["registrationId"]
        edited = register_script(
            service,
            "10-register-edited",
            LIGHT_SALES_SCRIPT + "\n# edited\n",
            LIGHT_CONTRACT,
        )
        assert edited.ok, edited.errors
        assert edited.result["registrationId"] != previous_registration
        assert extension_maps(service) == after_selection

        # The workspace still accepts mutations and dry runs.
        adopted = retarget_registration(
            service, "11-retarget", edited.result["registrationId"]
        )
        assert adopted.ok, adopted.errors
        preview = preview_a_selection_change(service, "12-preview", "S-73")
        assert preview.ok, preview.errors
        assert preview.result["affectedObservers"] == {
            "selected_supplier": [
                "projections/inspector",
                "projections/network",
            ]
        }
        # Replacing the action kept its stable output identity.
        assert extension_maps(service) == after_selection
    finally:
        service.close()

    # The maps survive the restart with their committed values.
    restored = reopened(service_adapter)
    try:
        assert extension_maps(restored) == after_selection
        assert_inspection_still_exposes_the_state(restored, "S-42")
        later = preview_a_selection_change(restored, "13-restart", "S-73")
        assert later.ok, later.errors
    finally:
        restored.close()


def test_clearing_keeps_the_extension_maps(service_adapter):
    service = seed_extension_workspace(service_adapter)
    try:
        assert select(service, "08-select", "S-42").ok
        cleared = clear_selection(service, "09-clear")
        assert cleared.ok, cleared.errors
        after_clear = extension_maps(service)
        assert after_clear["reference_states"]["selected_supplier"] == (
            None,
            3,
        )
        assert set(after_clear["collection_incarnations"]) == {"suppliers"}
        assert set(after_clear["output_incarnations"]) == {"analyze-sales"}

        imported = import_source(
            service,
            "10-import-v2",
            "supplier-records",
            suppliers_bytes(SUPPLIERS_V1),
            PARQUET,
            1,
        )
        assert imported.ok, imported.errors
        edited = register_script(
            service,
            "11-register-edited",
            LIGHT_SALES_SCRIPT + "\n# edited\n",
            LIGHT_CONTRACT,
        )
        assert edited.ok, edited.errors
        assert extension_maps(service) == after_clear

        adopted = retarget_registration(
            service, "12-retarget", edited.result["registrationId"]
        )
        assert adopted.ok, adopted.errors
        assert preview_a_selection_change(service, "13-preview", "S-73").ok
    finally:
        service.close()

    restored = reopened(service_adapter)
    try:
        assert extension_maps(restored) == after_clear
    finally:
        restored.close()


# ---------------------------------------------------------------------------
# The migrated v0.1 workspace
# ---------------------------------------------------------------------------


def test_a_migrated_workspace_keeps_its_incarnations_through_an_import(
    service_adapter,
):
    from test_migration import migrate, seed_published
    from test_scheduling import envelope as sales_envelope

    service, _publication = seed_published(service_adapter)
    try:
        assert migrate(service).ok
        migrated = extension_maps(service)
        assert set(migrated["output_incarnations"]) == {"analyze-sales"}

        # One routine import must not erase the freshly issued output
        # incarnations the migration committed.
        token = service.capture_artifact_token(
            BUILDER.sales_parquet_bytes(), PARQUET
        ).token
        imported = service.dispatch(
            sales_envelope(
                "post-import",
                method="source.import",
                expected={"sources/sales-input": 1},
                payload={
                    "sourceId": "sales-input",
                    "artifactToken": token,
                    "originLabel": "sales.csv",
                },
            )
        )
        assert imported.ok, imported.errors
        assert extension_maps(service) == migrated

        layout = service.state.entity_revisions["layout"]
        applied = service.dispatch(
            sales_envelope(
                "post-apply",
                method="workspace.apply",
                expected={"layout": layout},
                operations=[
                    {
                        "op": "layout.replace",
                        "value": {"root": None, "hidden": ["sales", "revenue"]},
                    }
                ],
            )
        )
        assert applied.ok, applied.errors
        preview = service.dispatch(
            sales_envelope(
                "post-preview",
                method="workspace.preview",
                expected={"layout": service.state.entity_revisions["layout"]},
                operations=[
                    {
                        "op": "layout.replace",
                        "value": {
                            "root": {"kind": "pane", "projection": "sales"},
                            "hidden": ["revenue"],
                        },
                    }
                ],
            )
        )
        assert preview.ok, preview.errors
        assert extension_maps(service) == migrated
    finally:
        service.close()

    restored = WorkspaceService.open(service_adapter, "sales-demo")
    try:
        assert extension_maps(restored) == migrated
    finally:
        restored.close()
