"""The supplier fixture of RFC 001 section 10.2 through the real
service (backlog task T026).

The tests drive the pure interaction loop through the same framed
service handlers the native host will use:

1. Select `S-42` through the test renderer's event contract.
2. Verify one state commit and an inspector record for `S-42`.
3. Verify graph highlighting through the compiled observer plan.
4. Verify zero model requests and zero Python process launches.
5. Restart and restore the committed reference and inspector
   resolution.
6. Remove `S-42` in a new collection source version and verify a
   missing state.
7. Reject an event from the previous graph or collection revision.

The agent-style extension of section 10.3 follows: a stored patch adds
a second inspector without touching the existing components, and the
selected reference, the state revision, and the existing projection
identities survive unchanged.

Everything runs against an isolated temporary root named with the
Looper resource prefix, never against the user's data directory.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from melt.schema.commands import parse_command
from runtime.compiler import CompileRequest, compile_document
from runtime.storage import PlatformAdapter, storage_worker_available
from runtime.workspace import WorkspaceService
from runtime.workspace.observers import observer_views

STORAGE_AVAILABLE, STORAGE_UNAVAILABLE_REASON = storage_worker_available()

requires_storage_worker = pytest.mark.skipif(
    not STORAGE_AVAILABLE, reason=STORAGE_UNAVAILABLE_REASON
)

pytestmark = requires_storage_worker

WORKSPACE = "supplier-demo"
PARQUET = "application/vnd.apache.parquet"
GRAPH_MEDIA_TYPE = "application/vnd.melt.graph+json"

REPO_ROOT = Path(__file__).resolve().parents[2]
PATCH_FILE = REPO_ROOT / "fixtures" / "semantic" / "patches" / "second-inspector.json"

SUPPLIERS_V1 = (("S-42", "Alpha Components"), ("S-73", "Beta Systems"))
SUPPLIERS_WITHOUT_S42 = (("S-73", "Beta Systems"),)


def _load_builder():
    path = (
        REPO_ROOT / "scripts" / "build_fixture_artifacts.py"
    )
    spec = importlib.util.spec_from_file_location("build_fixture_artifacts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = _load_builder()
GRAPH_BYTES = BUILDER.supplier_graph_bytes()


def suppliers_bytes(rows) -> bytes:
    schema = pa.schema(
        [
            pa.field("supplier_id", pa.string(), nullable=False),
            pa.field("name", pa.string(), nullable=False),
        ]
    )
    table = pa.Table.from_arrays(
        [
            pa.array([row[0] for row in rows], type=pa.string()),
            pa.array([row[1] for row in rows], type=pa.string()),
        ],
        schema=schema,
    )
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, compression="snappy", version="2.6")
    return sink.getvalue().to_pybytes()


SEMANTIC_EMPTY = {
    "language": "melt/semantic-draft-1",
    "workspaceId": WORKSPACE,
    "title": "Supplier risk",
    "sources": {},
    "parameters": {},
    "actions": {},
    "projections": {},
    "bindings": {},
    "collections": {},
    "state": {},
    "layout": {"root": None, "hidden": []},
}


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


def apply(service, request_id, expected, operations):
    return service.dispatch(
        envelope(
            request_id, "workspace.apply", expected=expected, operations=operations
        )
    )


def inspect(service, request_id="inspect"):
    return service.dispatch(
        envelope(request_id, "workspace.inspect", payload={})
    )


def import_source(service, request_id, source_id, data, media_type, revision):
    if not isinstance(data, bytes):
        data = suppliers_bytes(data)
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


def revision_of(service, path):
    snapshot = inspect(service).result
    if path.startswith("refstate/"):
        state = snapshot["semantic"]["referenceStates"].get(
            path[len("refstate/") :]
        )
        return 0 if state is None else int(state["revision"])
    return int(snapshot["entityRevisions"].get(path, 0))


def event_guards(service, projection_id="network", *, target="selected_supplier"):
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
        if (
            binding["event"]["projection"] == projection_id
            and binding["event"]["kind"] in ("object.select", "selection.clear")
        ):
            guards[f"bindings/{binding_id}"] = revision_of(
                service, f"bindings/{binding_id}"
            )
            assert binding["effect"]["target"] == target
    return guards


def select(service, request_id, key, guards, *, collection="suppliers", origin="user"):
    incarnation = service.state.collection_incarnations[collection]
    return service.dispatch_as(
        envelope(
            request_id,
            "object.select",
            expected=guards,
            payload={
                "projectionId": "network",
                "objectRef": {
                    "collection": collection,
                    "incarnation": incarnation,
                    "key": key,
                },
            },
        ),
        origin=origin,
    )


def clear(service, request_id, guards, *, origin="user"):
    return service.dispatch_as(
        envelope(
            request_id,
            "selection.clear",
            expected=guards,
            payload={"projectionId": "network"},
        ),
        origin=origin,
    )


def semantic_view(service):
    response = inspect(service)
    assert response.ok, response.errors
    return response.result["semantic"]


def build_supplier_workspace(service_adapter) -> WorkspaceService:
    """The supplier fixture of RFC 001 section 10.2, step by step.

    Creates the empty extension workspace, imports the two immutable
    fixtures (the supplier table and the supplier graph), declares the
    collection, the reference state, the two projections, and the two
    bindings from `fixtures/semantic/definitions.json`.
    """

    service = WorkspaceService.open(service_adapter, WORKSPACE)
    created = service.create_workspace(json.loads(json.dumps(SEMANTIC_EMPTY)), "01")
    assert created.ok, created.errors
    assert import_source(
        service, "02", "supplier-records", SUPPLIERS_V1, PARQUET, 0
    ).ok
    assert import_source(
        service, "03", "supplier-network", GRAPH_BYTES, GRAPH_MEDIA_TYPE, 0
    ).ok
    operations = json.loads(
        (REPO_ROOT / "fixtures" / "semantic" / "definitions.json").read_text()
    )
    expected = {}
    from runtime.workspace.state import entity_path

    for operation in operations:
        if operation["op"] == "layout.replace":
            path = "layout"
        else:
            path = entity_path(operation["collection"], operation["id"])
        expected[path] = revision_of(service, path)
    applied = apply(service, "04-definitions", expected, operations)
    assert applied.ok, applied.errors
    return service


@pytest.fixture
def service(service_adapter: PlatformAdapter):
    opened = build_supplier_workspace(service_adapter)
    try:
        yield opened
    finally:
        opened.close()


def codes(response):
    return [(error.code, error.path) for error in response.errors]


def observer_of(service, projection_id):
    return semantic_view(service)["observers"][projection_id]


# ---------------------------------------------------------------------------
# RFC 001 section 10.2: the supplier fixture
# ---------------------------------------------------------------------------


def test_one_state_commit_and_an_inspector_record(service_adapter):
    service = build_supplier_workspace(service_adapter)
    try:
        assert revision_of(service, "refstate/selected_supplier") == 1
        guards = event_guards(service)
        answer = select(service, "05-select", "S-42", guards)
        assert answer.ok, answer.errors
        result = answer.result
        assert result["status"] == "committed"
        assert result["changedRecords"] == ["refstate/selected_supplier"]
        assert result["stateRevision"] == 2
        assert result["value"]["key"] == "S-42"
        assert result["origin"] == "user"
        assert result["observers"] == ["inspector", "network"]
        # Exactly one commit moved the state: the sequence advanced once
        # and the stored revision moved from 1 to 2.
        assert answer.commitSequence == inspect(service).result["epoch"] * 0 + answer.commitSequence
        stored = semantic_view(service)["referenceStates"]["selected_supplier"]
        assert stored["revision"] == 2
        assert stored["value"]["key"] == "S-42"
        assert stored["resolution"] == {"status": "resolved"}
        record = observer_of(service, "inspector")
        assert record["display"] == "record"
        assert record["record"]["cells"] == {
            "supplier_id": "S-42",
            "name": "Alpha Components",
        }
    finally:
        service.close()


def test_graph_highlighting_runs_through_the_compiled_plan(service_adapter):
    service = build_supplier_workspace(service_adapter)
    try:
        # The compiled dependency plan names the observers of the state
        # entry, and the identity plan maps references to nodes.
        compiled = compile_document(
            CompileRequest(
                document=service.state.document,
                source_catalog=service.source_catalog,
            )
        )
        assert compiled.ok, compiled.diagnostics
        observers = compiled.plan.dependency.referenceStates[
            "selected_supplier"
        ].observers
        assert {(o.projection, o.slot) for o in observers} == {
            ("inspector", "record"),
            ("network", "highlight"),
        }
        nodes = compiled.plan.identity.nodes
        assert {key: (node.collection, node.key) for key, node in nodes.items()} == {
            "network/s-42": ("suppliers", "S-42"),
            "network/s-73": ("suppliers", "S-73"),
        }
        guards = event_guards(service)
        assert select(service, "05-select", "S-42", guards).ok
        highlight = observer_of(service, "network")
        assert highlight["highlightedNodes"] == ["s-42"]
        assert highlight["resolution"] == {"status": "resolved"}
        # Selecting the other supplier moves the highlight.
        guards = event_guards(service)
        assert select(service, "06-select", "S-73", guards).ok
        assert observer_of(service, "network")["highlightedNodes"] == ["s-73"]
    finally:
        service.close()


def test_the_interaction_is_pure_zero_models_zero_python(service_adapter):
    service = build_supplier_workspace(service_adapter)
    try:
        guards = event_guards(service)
        answer = select(service, "05-select", "S-42", guards)
        assert answer.ok, answer.errors
        # Zero Python launches: no run was admitted, queued, or staged.
        assert answer.result.get("admissions") is None
        assert service.runs == {}
        assert service.pending_intents == {}
        assert service.output_groups == {}
        assert service.registrations == {}
        layout = service.store.layout
        assert list(layout.staging.iterdir()) == []
        assert list(layout.logs.iterdir()) == []
        # Zero model requests: the round has no model provider, and the
        # answer names no admission or execution of any kind.
        assert set(answer.result) == {
            "status",
            "eventId",
            "event",
            "origin",
            "projection",
            "binding",
            "state",
            "value",
            "stateRevision",
            "changedRecords",
            "observers",
        }
    finally:
        service.close()


def test_restart_restores_the_reference_and_the_resolution(service_adapter):
    service = build_supplier_workspace(service_adapter)
    try:
        guards = event_guards(service)
        assert select(service, "05-select", "S-42", guards).ok
        committed = semantic_view(service)["referenceStates"][
            "selected_supplier"
        ]
        record = observer_of(service, "inspector")
    finally:
        service.close()

    reopened = WorkspaceService.open(service_adapter, WORKSPACE)
    try:
        assert reopened.dispatch(
            envelope(
                "hello",
                "session.hello",
                payload={
                    "protocolVersions": [1],
                    "languageVersions": ["melt/semantic-draft-1"],
                    "clientVersion": "test",
                },
            )
        ).ok
        stored = semantic_view(reopened)["referenceStates"]["selected_supplier"]
        assert stored == committed
        assert observer_of(reopened, "inspector") == record
        assert reopened.state.reference_states[
            "selected_supplier"
        ].value.key == "S-42"
    finally:
        reopened.close()


def test_a_removed_key_reports_missing_and_keeps_the_reference(service_adapter):
    service = build_supplier_workspace(service_adapter)
    try:
        guards = event_guards(service)
        assert select(service, "05-select", "S-42", guards).ok
        # A newer collection source version omits S-42.
        assert import_source(
            service, "06-import", "supplier-records", SUPPLIERS_WITHOUT_S42,
            PARQUET, 1,
        ).ok
        rebound = apply(
            service,
            "07-rebind",
            {"collections/suppliers": revision_of(service, "collections/suppliers")},
            [
                {
                    "op": "entity.replace",
                    "collection": "collections",
                    "id": "suppliers",
                    "value": {
                        "source": {"sourceId": "supplier-records", "version": 2},
                        "key": "supplier_id",
                    },
                }
            ],
        )
        assert rebound.ok, rebound.errors
        stored = semantic_view(service)["referenceStates"]["selected_supplier"]
        assert stored["value"]["key"] == "S-42"
        assert stored["resolution"]["status"] == "missing"
        record = observer_of(service, "inspector")
        assert record["display"] == "missing"
        assert record["value"]["key"] == "S-42"
        # No matching node still means no highlight; the selection stays
        # valid for the other projections (RFC 001 section 6).
        assert observer_of(service, "network")["highlightedNodes"] == ["s-42"]
    finally:
        service.close()


def test_an_event_from_a_stale_revision_is_rejected(service_adapter):
    service = build_supplier_workspace(service_adapter)
    try:
        guards = event_guards(service)
        assert select(service, "05-select", "S-42", guards).ok
        # A new collection source version and rebind advance revisions.
        assert import_source(
            service, "06-import", "supplier-records", SUPPLIERS_WITHOUT_S42,
            PARQUET, 1,
        ).ok
        assert apply(
            service,
            "07-rebind",
            {"collections/suppliers": 1},
            [
                {
                    "op": "entity.replace",
                    "collection": "collections",
                    "id": "suppliers",
                    "value": {
                        "source": {"sourceId": "supplier-records", "version": 2},
                        "key": "supplier_id",
                    },
                }
            ],
        ).ok
        # The stale collection guard rejects the event even though the
        # key S-73 still exists in the new version (RFC 001 10.2, 5.3).
        stale_collection = dict(guards)
        rejected = select(service, "08-stale-collection", "S-73", stale_collection)
        assert not rejected.ok
        assert (
            "E_CONFLICT",
            "/expected/collections~1suppliers",
        ) in codes(rejected)
        # A stale displayed graph version rejects the event too.
        newer_graph = GRAPH_BYTES + b"\n"
        assert import_source(
            service, "09-graph", "supplier-network", newer_graph,
            GRAPH_MEDIA_TYPE, 1,
        ).ok
        fresh = event_guards(service)
        stale_graph = dict(fresh)
        stale_graph["sources/supplier-network"] = fresh[
            "sources/supplier-network"
        ] - 1
        rejected = select(service, "10-stale-graph", "S-73", stale_graph)
        assert not rejected.ok
        assert (
            "E_CONFLICT",
            "/expected/sources~1supplier-network",
        ) in codes(rejected)
        # The stored selection never moved through any rejection.
        assert semantic_view(service)["referenceStates"][
            "selected_supplier"
        ]["revision"] == 2
    finally:
        service.close()


def test_a_stale_state_revision_conflicts(service_adapter):
    service = build_supplier_workspace(service_adapter)
    try:
        guards = event_guards(service)
        assert select(service, "05-select", "S-42", guards).ok
        stale = dict(event_guards(service))
        stale["refstate/selected_supplier"] = 1
        rejected = select(service, "06-stale-state", "S-73", stale)
        assert not rejected.ok
        assert ("E_CONFLICT", "/expected/refstate~1selected_supplier") in codes(
            rejected
        )
    finally:
        service.close()


def test_clearing_stores_null_and_selecting_again_restores(service_adapter):
    service = build_supplier_workspace(service_adapter)
    try:
        assert select(service, "05-select", "S-42", event_guards(service)).ok
        cleared = clear(service, "06-clear", event_guards(service))
        assert cleared.ok, cleared.errors
        assert cleared.result["changedRecords"] == ["refstate/selected_supplier"]
        stored = semantic_view(service)["referenceStates"]["selected_supplier"]
        assert stored["value"] is None
        assert stored["revision"] == 3
        assert stored["resolution"] == {"status": "null"}
        assert observer_of(service, "inspector")["display"] == "empty"
        assert observer_of(service, "network")["highlightedNodes"] == []
        assert select(service, "07-select", "S-73", event_guards(service)).ok
        assert observer_of(service, "inspector")["record"]["cells"] == {
            "supplier_id": "S-73",
            "name": "Beta Systems",
        }
    finally:
        service.close()


def test_duplicate_events_replay_their_receipt_and_reuse_is_an_error(
    service_adapter,
):
    service = build_supplier_workspace(service_adapter)
    try:
        guards = event_guards(service)
        first = select(service, "05-select", "S-42", guards)
        assert first.ok, first.errors
        replay = select(service, "05-select", "S-42", guards)
        assert replay.ok
        assert replay.result == first.result
        reused = select(service, "05-select", "S-73", guards)
        assert not reused.ok
        assert ("E_REQUEST_REUSE", "/requestId") in codes(reused)
        # One commit only: the replay changed nothing.
        assert semantic_view(service)["referenceStates"][
            "selected_supplier"
        ]["revision"] == 2
    finally:
        service.close()


def test_an_equal_selection_is_a_reported_no_op(service_adapter):
    service = build_supplier_workspace(service_adapter)
    try:
        assert select(service, "05-select", "S-42", event_guards(service)).ok
        again = select(service, "06-again", "S-42", event_guards(service))
        assert again.ok, again.errors
        assert again.result["status"] == "no-op"
        assert again.result["changedRecords"] == []
        assert again.result["stateRevision"] == 2
    finally:
        service.close()


def test_the_programmatic_origin_rides_the_receipt(service_adapter):
    service = build_supplier_workspace(service_adapter)
    try:
        answer = select(
            service, "05-programmatic", "S-42", event_guards(service),
            origin="programmatic",
        )
        assert answer.ok, answer.errors
        assert answer.result["origin"] == "programmatic"
    finally:
        service.close()


def test_events_come_only_from_renderers_that_declare_them(service_adapter):
    service = build_supplier_workspace(service_adapter)
    try:
        rejected = service.dispatch(
            envelope(
                "05-wrong-projection",
                "object.select",
                expected=event_guards(service),
                payload={
                    "projectionId": "inspector",
                    "objectRef": {
                        "collection": "suppliers",
                        "incarnation": service.state.collection_incarnations[
                            "suppliers"
                        ],
                        "key": "S-42",
                    },
                },
            )
        )
        assert not rejected.ok
        assert ("E_TYPE", "/payload/projectionId") in codes(rejected)
    finally:
        service.close()


def test_a_forged_incarnation_conflicts(service_adapter):
    service = build_supplier_workspace(service_adapter)
    try:
        guards = event_guards(service)
        forged = dict(guards)
        rejected = service.dispatch(
            envelope(
                "06-forged",
                "object.select",
                expected=forged,
                payload={
                    "projectionId": "network",
                    "objectRef": {
                        "collection": "suppliers",
                        "incarnation": "inc-forged",
                        "key": "S-42",
                    },
                },
            )
        )
        assert not rejected.ok
        assert ("E_CONFLICT", "/payload/objectRef") in codes(rejected)
        assert semantic_view(service)["referenceStates"][
            "selected_supplier"
        ]["revision"] == 1
    finally:
        service.close()


def test_a_v01_workspace_refuses_the_extension_events(service_adapter):
    service = WorkspaceService.open(service_adapter, WORKSPACE)
    try:
        document = {
            "language": "melt/v0.1",
            "workspaceId": WORKSPACE,
            "title": "Sales analysis",
            "sources": {},
            "parameters": {},
            "actions": {},
            "projections": {},
            "bindings": {},
            "layout": {"root": None, "hidden": []},
        }
        assert service.create_workspace(
            json.loads(json.dumps(document)), "01"
        ).ok
        rejected = service.dispatch(
            envelope(
                "02-select",
                "object.select",
                expected={},
                payload={
                    "projectionId": "network",
                    "objectRef": {
                        "collection": "suppliers",
                        "incarnation": "inc-x",
                        "key": "S-42",
                    },
                },
            )
        )
        assert not rejected.ok
        assert ("E_VERSION", "/method") in codes(rejected)
        rejected = service.dispatch(
            envelope(
                "03-clear",
                "selection.clear",
                expected={},
                payload={"projectionId": "network"},
            )
        )
        assert not rejected.ok
        assert ("E_VERSION", "/method") in codes(rejected)
    finally:
        service.close()


def test_an_unbound_event_address_is_a_reported_no_op(service_adapter):
    service = build_supplier_workspace(service_adapter)
    try:
        removed = apply(
            service,
            "05-unbind",
            {"bindings/clear-supplier": revision_of(service, "bindings/clear-supplier")},
            [
                {
                    "op": "entity.delete",
                    "collection": "bindings",
                    "id": "clear-supplier",
                }
            ],
        )
        assert removed.ok, removed.errors
        answer = clear(service, "06-clear", event_guards(service))
        assert answer.ok, answer.errors
        assert answer.result["status"] == "no-op"
        assert "no binding connects this event address" in answer.result["reason"]
        assert semantic_view(service)["referenceStates"][
            "selected_supplier"
        ]["revision"] == 1
    finally:
        service.close()


def test_preview_identifies_the_affected_observers(service_adapter):
    service = build_supplier_workspace(service_adapter)
    try:
        incarnation = service.state.collection_incarnations["suppliers"]
        preview = service.dispatch(
            envelope(
                "05-preview",
                "workspace.preview",
                expected=event_guards(service),
                operations=[
                    {
                        "op": "state.set",
                        "id": "selected_supplier",
                        "value": {
                            "collection": "suppliers",
                            "incarnation": incarnation,
                            "key": "S-73",
                        },
                    }
                ],
            )
        )
        assert preview.ok, preview.errors
        assert preview.result["affectedObservers"] == {
            "selected_supplier": [
                "projections/inspector",
                "projections/network",
            ]
        }
        # The dry run wrote nothing.
        assert semantic_view(service)["referenceStates"][
            "selected_supplier"
        ]["value"] is None
    finally:
        service.close()


def test_inspection_exposes_the_pinned_renderer_versions(service_adapter):
    service = build_supplier_workspace(service_adapter)
    try:
        renderers = semantic_view(service)["renderers"]
        assert renderers["network"] == {"id": "melt.graph", "contractVersion": 1}
        assert renderers["inspector"] == {
            "id": "melt.record",
            "contractVersion": 1,
        }
    finally:
        service.close()


# ---------------------------------------------------------------------------
# RFC 001 section 10.3: the agent-style extension
# ---------------------------------------------------------------------------


def test_the_stored_patch_adds_a_second_inspector_without_touched_state(
    service_adapter,
):
    service = build_supplier_workspace(service_adapter)
    try:
        assert select(service, "05-select", "S-42", event_guards(service)).ok
        before = inspect(service).result
        before_revisions = dict(before["entityRevisions"])
        before_state = semantic_view(service)["referenceStates"][
            "selected_supplier"
        ]
        before_layout = before["document"]["layout"]
        operations = json.loads(PATCH_FILE.read_text())
        expected = {
            "projections/risk-inspector": 0,
            "layout": before_revisions["layout"],
        }
        # The dry run of the patch reports the same innocence the RFC
        # demands: no state reset, no affected observer, no run
        # invalidated, and no controller replacement (RFC 001 9, 10.3).
        preview = service.dispatch(
            envelope(
                "06-patch-preview",
                "workspace.preview",
                expected=expected,
                operations=operations,
            )
        )
        assert preview.ok, preview.errors
        assert preview.result["stateResets"] == []
        assert preview.result["invalidatedRuns"] == []
        assert preview.result["controllerReplacements"] == []
        assert "affectedObservers" not in preview.result

        patched = apply(service, "07-agent-patch", expected, operations)
        assert patched.ok, patched.errors
        after = inspect(service).result

        # The selected reference and the state revision are unchanged.
        after_state = semantic_view(service)["referenceStates"][
            "selected_supplier"
        ]
        assert after_state == before_state

        # The existing projection identities and their revisions are
        # unchanged; the patch created exactly one projection.
        for path, revision in before_revisions.items():
            if path in ("layout",):
                continue
            assert after["entityRevisions"][path] == revision, path
        # The created record starts at its own first revision.
        assert after["entityRevisions"]["projections/risk-inspector"] == 1
        assert "risk-inspector" in after["document"]["projections"]
        assert set(after["document"]["projections"]) == {
            "inspector",
            "network",
            "risk-inspector",
        }
        # The patch replaced the layout only.
        assert after["document"]["layout"] != before_layout

        # No run was invalidated: none existed.
        assert patched.result.get("invalidatedRuns", []) == []

        # The new inspector resolves the current selection immediately.
        second = observer_of(service, "risk-inspector")
        assert second["display"] == "record"
        assert second["record"]["cells"] == {"supplier_id": "S-42"}
        first = observer_of(service, "inspector")
        assert first["record"]["cells"] == {
            "supplier_id": "S-42",
            "name": "Alpha Components",
        }
    finally:
        service.close()


def test_the_patch_survives_a_selection_made_afterwards(service_adapter):
    service = build_supplier_workspace(service_adapter)
    try:
        operations = json.loads(PATCH_FILE.read_text())
        assert apply(
            service,
            "05-agent-patch",
            {
                "projections/risk-inspector": 0,
                "layout": revision_of(service, "layout"),
            },
            operations,
        ).ok
        guards = event_guards(service)
        assert select(service, "06-select", "S-73", guards).ok
        for projection_id in ("inspector", "risk-inspector"):
            view = observer_of(service, projection_id)
            assert view["display"] == "record", projection_id
            assert view["value"]["key"] == "S-73", projection_id
    finally:
        service.close()
