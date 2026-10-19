"""Semantic collections and reference state through the real service
(T025, RFC 001 sections 3 and 4).

The tests drive every extension behavior through the same framed
service handlers the native host will use:

- Creation: `melt/semantic-draft-1` workspaces start with empty
  extension maps; a collection entity issues an opaque incarnation no
  client chose, and a state entity starts null at revision 1.
- Admission: the service reads the pinned table bytes and rejects a
  binding whose key column repeats or nulls a key, keeping the
  previous definitions.
- Assignment: `state.set` must name the declared collection and the
  current incarnation; a stale or forged incarnation conflicts, and a
  recreated collection issues a new incarnation the old reference
  cannot satisfy.
- Resolution: a stored reference resolves against the pinned version;
  a newer version that omits the key reports `missing` while the value
  stays stored, an unreadable artifact reports `unavailable`, and a
  restored key resolves again.
- Recovery: a reopen restores the incarnations, the selections, and
  their resolution without reading or executing anything.
- The v0.1 boundary: a `melt/v0.1` workspace refuses the extension
  operations and fields with `E_VERSION` and `E_SCHEMA`.

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
from runtime.storage import PlatformAdapter, storage_worker_available
from runtime.workspace import WorkspaceService

STORAGE_AVAILABLE, STORAGE_UNAVAILABLE_REASON = storage_worker_available()

requires_storage_worker = pytest.mark.skipif(
    not STORAGE_AVAILABLE, reason=STORAGE_UNAVAILABLE_REASON
)

pytestmark = requires_storage_worker

WORKSPACE = "supplier-demo"
PARQUET = "application/vnd.apache.parquet"

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

V01_EMPTY = {
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


def _load_builder():
    path = (
        Path(__file__).resolve().parents[2] / "scripts" / "build_fixture_artifacts.py"
    )
    spec = importlib.util.spec_from_file_location("build_fixture_artifacts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = _load_builder()


def suppliers_bytes(rows, *, nullable_key: bool = False) -> bytes:
    """One supplier table with the fixture schema, any rows.

    ``nullable_key`` builds a key column the import measures as
    nullable, which no collection may key on (RFC 001 section 3.1).
    """

    schema = pa.schema(
        [
            pa.field("supplier_id", pa.string(), nullable=nullable_key),
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


SUPPLIERS_V1 = [("S-42", "Alpha Components"), ("S-73", "Beta Systems")]
SUPPLIERS_WITHOUT_S42 = [("S-73", "Beta Systems")]
SUPPLIERS_RESTORED = [
    ("S-42", "Alpha Components returns"),
    ("S-73", "Beta Systems"),
]
SUPPLIERS_DUPLICATE = [
    ("S-42", "Alpha Components"),
    ("S-42", "Beta Systems"),
    ("S-73", "Gamma Supply"),
]


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


def import_suppliers(service, request_id, data, expected_revision):
    if not isinstance(data, bytes):
        data = suppliers_bytes(data)
    token = service.capture_artifact_token(data, PARQUET).token
    return service.dispatch(
        envelope(
            request_id,
            "source.import",
            expected={"sources/supplier-records": expected_revision},
            payload={
                "sourceId": "supplier-records",
                "artifactToken": token,
                "originLabel": "suppliers.parquet",
            },
        )
    )


def create_collection(service, request_id, version, expected_revision):
    return apply(
        service,
        request_id,
        {"collections/suppliers": expected_revision},
        [
            {
                "op": "entity.create",
                "collection": "collections",
                "id": "suppliers",
                "value": {
                    "source": {"sourceId": "supplier-records", "version": version},
                    "key": "supplier_id",
                },
            }
        ],
    )


def rebind_collection(service, request_id, version, expected_revision):
    return apply(
        service,
        request_id,
        {"collections/suppliers": expected_revision},
        [
            {
                "op": "entity.replace",
                "collection": "collections",
                "id": "suppliers",
                "value": {
                    "source": {"sourceId": "supplier-records", "version": version},
                    "key": "supplier_id",
                },
            }
        ],
    )


def create_state(service, request_id, collection="suppliers", expected_revision=0):
    return apply(
        service,
        request_id,
        {f"semantic-state/selected_supplier": expected_revision},
        [
            {
                "op": "entity.create",
                "collection": "state",
                "id": "selected_supplier",
                "value": {
                    "type": {"kind": "ref", "collection": collection},
                    "nullable": True,
                    "default": None,
                    "scope": "workspace",
                },
            }
        ],
    )


def select(service, request_id, incarnation, key, expected_revision):
    return apply(
        service,
        request_id,
        {"refstate/selected_supplier": expected_revision},
        [
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


def semantic_view(service):
    response = inspect(service)
    assert response.ok, response.errors
    return response.result["semantic"]


def incarnation_of(service):
    return semantic_view(service)["collections"]["suppliers"]["incarnation"]


def reference_state(service):
    return semantic_view(service)["referenceStates"]["selected_supplier"]


def codes(response):
    return [(error.code, error.path) for error in response.errors]


@pytest.fixture
def service(service_adapter: PlatformAdapter):
    """One semantic workspace with the supplier collection and state."""

    opened = WorkspaceService.open(service_adapter, WORKSPACE)
    try:
        created = opened.create_workspace(
            json.loads(json.dumps(SEMANTIC_EMPTY)), "01-create"
        )
        assert created.ok, created.errors
        yield opened
    finally:
        opened.close()


def seeded_service(service_adapter: PlatformAdapter) -> WorkspaceService:
    """A workspace holding one selected supplier `S-42`."""

    opened = WorkspaceService.open(service_adapter, WORKSPACE)
    created = opened.create_workspace(
        json.loads(json.dumps(SEMANTIC_EMPTY)), "01-create"
    )
    assert created.ok, created.errors
    assert import_suppliers(opened, "02-import", SUPPLIERS_V1, 0).ok
    assert create_collection(opened, "04-collection", 1, 0).ok
    assert create_state(opened, "05-state").ok
    incarnation = incarnation_of(opened)
    assert select(opened, "06-select", incarnation, "S-42", 1).ok
    return opened


# ---------------------------------------------------------------------------
# Creation and incarnations (RFC 001 sections 3.1 and 3.2)
# ---------------------------------------------------------------------------


def test_creation_requires_empty_extension_maps(service_adapter):
    service = WorkspaceService.open(service_adapter, "second-ws")
    try:
        document = json.loads(json.dumps(SEMANTIC_EMPTY))
        document["workspaceId"] = "second-ws"
        document["collections"] = {
            "suppliers": {
                "source": {"sourceId": "supplier-records", "version": 1},
                "key": "supplier_id",
            }
        }
        rejected = service.create_workspace(document, "c1")
        assert not rejected.ok
        assert ("E_SCHEMA", "/collections") in codes(rejected)
        document["collections"] = {}
        document["state"] = {
            "selected_supplier": {
                "type": {"kind": "ref", "collection": "suppliers"},
                "nullable": True,
                "default": None,
                "scope": "workspace",
            }
        }
        rejected = service.create_workspace(document, "c2")
        assert not rejected.ok
        assert ("E_SCHEMA", "/state") in codes(rejected)
    finally:
        service.close()


def test_collection_creation_issues_an_opaque_incarnation(service):
    assert import_suppliers(service, "02-import", SUPPLIERS_V1, 0).ok
    created = create_collection(service, "02-collection", 1, 0)
    assert created.ok, created.errors
    view = semantic_view(service)["collections"]["suppliers"]
    assert view["incarnation"].startswith("inc-")
    assert view["key"] == "supplier_id"
    assert view["source"] == {"sourceId": "supplier-records", "version": 1}
    assert view["revision"] == 1
    # The document never carries the incarnation: a client cannot
    # choose what the service alone issues (RFC 001 section 3.2).
    assert "incarnation" not in service.document["collections"]["suppliers"]


def test_rebinding_keeps_the_incarnation(service):
    assert import_suppliers(service, "02-import", SUPPLIERS_V1, 0).ok
    assert create_collection(service, "02-collection", 1, 0).ok
    first = incarnation_of(service)
    assert import_suppliers(service, "03-import", SUPPLIERS_RESTORED, 1).ok
    assert rebind_collection(service, "03-rebind", 1, 1).ok
    assert incarnation_of(service) == first


def test_state_creation_starts_null_at_revision_one(service):
    assert import_suppliers(service, "02-import", SUPPLIERS_V1, 0).ok
    assert create_collection(service, "02-collection", 1, 0).ok
    created = create_state(service, "03-state")
    assert created.ok, created.errors
    stored = reference_state(service)
    assert stored["value"] is None
    assert stored["revision"] == 1
    assert stored["resolution"] == {"status": "null"}


# ---------------------------------------------------------------------------
# Key verification at admission (RFC 001 section 3.1)
# ---------------------------------------------------------------------------


def test_duplicate_keys_reject_the_binding(service):
    assert import_suppliers(service, "02-import", SUPPLIERS_V1, 0).ok
    assert create_collection(service, "03-collection", 1, 0).ok
    assert import_suppliers(service, "04-import", SUPPLIERS_DUPLICATE, 1).ok
    rejected = rebind_collection(service, "05-rebind", 2, 1)
    assert not rejected.ok
    assert ("E_REFERENCE", "/collections/suppliers/key") in codes(rejected)
    assert "S-42" in rejected.errors[0].message
    # The workspace keeps its previous definitions.
    view = semantic_view(service)["collections"]["suppliers"]
    assert view["source"]["version"] == 1
    assert view["revision"] == 1


def test_creation_of_a_collection_with_duplicate_keys_is_refused(service):
    assert import_suppliers(service, "02-import", SUPPLIERS_DUPLICATE, 0).ok
    rejected = create_collection(service, "03-collection", 1, 0)
    assert not rejected.ok
    assert ("E_REFERENCE", "/collections/suppliers/key") in codes(rejected)
    assert "collections/suppliers" not in service.entity_revisions


def test_preview_reports_the_same_key_rejection(service):
    assert import_suppliers(service, "02-import", SUPPLIERS_DUPLICATE, 0).ok
    preview = service.dispatch(
        envelope(
            "p1",
            "workspace.preview",
            expected={"collections/suppliers": 0},
            operations=[
                {
                    "op": "entity.create",
                    "collection": "collections",
                    "id": "suppliers",
                    "value": {
                        "source": {
                            "sourceId": "supplier-records",
                            "version": 1,
                        },
                        "key": "supplier_id",
                    },
                }
            ],
        )
    )
    assert not preview.ok
    assert ("E_REFERENCE", "/collections/suppliers/key") in codes(preview)


def test_a_nullable_key_column_rejects_the_binding(service):
    data = suppliers_bytes(
        [("S-42", "Alpha"), (None, "Unnamed"), ("S-73", "Beta")],
        nullable_key=True,
    )
    assert import_suppliers(service, "02-import", data, 0).ok
    rejected = create_collection(service, "03-collection", 1, 0)
    assert not rejected.ok
    assert ("E_TYPE", "/collections/suppliers/key") in codes(rejected)
    assert "non-null" in rejected.errors[0].message


# ---------------------------------------------------------------------------
# Byte-level key verification (RFC 001 section 3.1)
# ---------------------------------------------------------------------------


def test_verify_collection_keys_reports_duplicates_nulls_and_types():
    from runtime.workspace.semantic import verify_collection_keys

    schema = {
        "columns": [
            {"id": "supplier_id", "type": "string", "nullable": False},
            {"id": "name", "type": "string", "nullable": False},
        ],
        "rowKey": None,
    }
    duplicates = verify_collection_keys(
        suppliers_bytes(SUPPLIERS_DUPLICATE), schema, "supplier_id", "/collections/s"
    )
    assert [item.code for item in duplicates] == ["E_REFERENCE"]
    assert duplicates[0].path == "/collections/s/key"
    assert "S-42" in duplicates[0].message

    nulls = verify_collection_keys(
        suppliers_bytes([("S-42", "Alpha"), (None, "Unnamed")], nullable_key=True),
        schema,
        "supplier_id",
        "/collections/s",
    )
    # The bytes carry a null the declared schema forbids.
    assert any("null" in item.message for item in nulls)

    unknown_column = verify_collection_keys(
        suppliers_bytes(SUPPLIERS_V1), schema, "supplier_code", "/collections/s"
    )
    assert [item.code for item in unknown_column] == ["E_REFERENCE"]
    assert unknown_column[0].path == "/collections/s/key"

    unreadable = verify_collection_keys(b"not parquet", schema, "supplier_id", "/c")
    assert [item.code for item in unreadable] == ["E_ARTIFACT"]


# ---------------------------------------------------------------------------
# Assignment validation (RFC 001 sections 3.2 and 4)
# ---------------------------------------------------------------------------


def test_assignment_stores_the_reference_and_resolves_it(service_adapter):
    service = seeded_service(service_adapter)
    try:
        stored = reference_state(service)
        assert stored["value"] == {
            "collection": "suppliers",
            "incarnation": incarnation_of(service),
            "key": "S-42",
        }
        assert stored["revision"] == 2
        assert stored["resolution"] == {"status": "resolved"}
    finally:
        service.close()


def test_assignment_with_a_forged_incarnation_conflicts(service_adapter):
    service = seeded_service(service_adapter)
    try:
        rejected = select(service, "07-forged", "inc-forged", "S-42", 2)
        assert not rejected.ok
        assert ("E_CONFLICT", "/operations/0/value") in codes(rejected)
        assert reference_state(service)["revision"] == 2
    finally:
        service.close()


def test_assignment_for_the_wrong_collection_fails_with_type(service_adapter):
    service = seeded_service(service_adapter)
    try:
        rejected = apply(
            service,
            "w1",
            {"refstate/selected_supplier": 2},
            [
                {
                    "op": "state.set",
                    "id": "selected_supplier",
                    "value": {
                        "collection": "products",
                        "incarnation": incarnation_of(service),
                        "key": "P-1",
                    },
                }
            ],
        )
        assert not rejected.ok
        assert ("E_TYPE", "/operations/0/value") in codes(rejected)
    finally:
        service.close()


def test_assignment_without_an_expectation_conflicts(service_adapter):
    service = seeded_service(service_adapter)
    try:
        rejected = service.dispatch(
            envelope(
                "w2",
                "workspace.apply",
                operations=[
                    {
                        "op": "state.set",
                        "id": "selected_supplier",
                        "value": None,
                    }
                ],
            )
        )
        assert not rejected.ok
        assert ("E_CONFLICT", "/expected") in codes(rejected)
    finally:
        service.close()


def test_clearing_the_selection_stores_null(service_adapter):
    service = seeded_service(service_adapter)
    try:
        cleared = apply(
            service,
            "c1",
            {"refstate/selected_supplier": 2},
            [{"op": "state.set", "id": "selected_supplier", "value": None}],
        )
        assert cleared.ok, cleared.errors
        stored = reference_state(service)
        assert stored["value"] is None
        assert stored["revision"] == 3
        assert stored["resolution"] == {"status": "null"}
    finally:
        service.close()


def test_equal_assignment_is_a_no_op(service_adapter):
    service = seeded_service(service_adapter)
    try:
        incarnation = incarnation_of(service)
        again = select(service, "07-again", incarnation, "S-42", 2)
        assert again.ok, again.errors
        assert again.result["changedRecords"] == []
        assert reference_state(service)["revision"] == 2
    finally:
        service.close()


def test_assignment_replays_from_its_receipt(service_adapter):
    service = seeded_service(service_adapter)
    try:
        incarnation = incarnation_of(service)
        body = {
            "protocolVersion": 1,
            "workspaceId": WORKSPACE,
            "requestId": "07-select",
            "method": "workspace.apply",
            "expected": {"refstate/selected_supplier": 2},
            "operations": [
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
        }
        first = service.dispatch(parse_command(json.loads(json.dumps(body))))
        assert first.ok, first.errors
        # A retry with the same id and the same content replays the
        # stored response, even though the stored revision moved on.
        replay = service.dispatch(parse_command(json.loads(json.dumps(body))))
        assert replay.ok
        assert replay.result == first.result
        reused = service.dispatch(
            parse_command(
                dict(json.loads(json.dumps(body)), operations=[
                    {"op": "state.set", "id": "selected_supplier", "value": None}
                ])
            )
        )
        assert not reused.ok
        assert ("E_REQUEST_REUSE", "/requestId") in codes(reused)
    finally:
        service.close()


def test_state_type_change_needs_a_migration_in_the_same_transaction(
    service_adapter,
):
    service = seeded_service(service_adapter)
    try:
        # A second logical source gives the migration another declared
        # collection to move the state entry onto.
        token = service.capture_artifact_token(
            suppliers_bytes([("S-42", "Alpha Components")]), PARQUET
        ).token
        imported = service.dispatch(
            envelope(
                "p2",
                "source.import",
                expected={"sources/parts": 0},
                payload={
                    "sourceId": "parts",
                    "artifactToken": token,
                    "originLabel": "parts.parquet",
                },
            )
        )
        assert imported.ok, imported.errors
        created = apply(
            service,
            "p3",
            {"collections/parts": 0},
            [
                {
                    "op": "entity.create",
                    "collection": "collections",
                    "id": "parts",
                    "value": {
                        "source": {"sourceId": "parts", "version": 1},
                        "key": "supplier_id",
                    },
                }
            ],
        )
        assert created.ok, created.errors

        rejected = apply(
            service,
            "p4",
            {"semantic-state/selected_supplier": 1},
            [
                {
                    "op": "entity.replace",
                    "collection": "state",
                    "id": "selected_supplier",
                    "value": {
                        "type": {"kind": "ref", "collection": "parts"},
                        "nullable": True,
                        "default": None,
                        "scope": "workspace",
                    },
                }
            ],
        )
        assert not rejected.ok
        assert ("E_STATE_MIGRATION", "/operations/0") in codes(rejected)

        migrated = apply(
            service,
            "p5",
            {"semantic-state/selected_supplier": 1, "refstate/selected_supplier": 2},
            [
                {"op": "state.set", "id": "selected_supplier", "value": None},
                {
                    "op": "entity.replace",
                    "collection": "state",
                    "id": "selected_supplier",
                    "value": {
                        "type": {"kind": "ref", "collection": "parts"},
                        "nullable": True,
                        "default": None,
                        "scope": "workspace",
                    },
                },
            ],
        )
        assert migrated.ok, migrated.errors
        stored = reference_state(service)
        assert stored["value"] is None
    finally:
        service.close()


# ---------------------------------------------------------------------------
# Missing and unavailable references (RFC 001 section 3.3)
# ---------------------------------------------------------------------------


def test_a_newer_version_that_omits_the_key_reports_missing(service_adapter):
    service = seeded_service(service_adapter)
    try:
        assert import_suppliers(service, "v2", SUPPLIERS_WITHOUT_S42, 1).ok
        rebound = rebind_collection(service, "03-rebind", 2, 1)
        assert rebound.ok, rebound.errors
        stored = reference_state(service)
        # The reference stays exactly as stored; nothing selects
        # another row and nothing clears it silently.
        assert stored["value"]["key"] == "S-42"
        assert stored["revision"] == 2
        assert stored["resolution"]["status"] == "missing"
    finally:
        service.close()


def test_a_returning_key_resolves_again(service_adapter):
    service = seeded_service(service_adapter)
    try:
        assert import_suppliers(service, "v2", SUPPLIERS_WITHOUT_S42, 1).ok
        assert rebind_collection(service, "03-rebind", 2, 1).ok
        assert import_suppliers(service, "v3", SUPPLIERS_RESTORED, 2).ok
        assert rebind_collection(service, "04-rebind", 3, 2).ok
        assert reference_state(service)["resolution"] == {"status": "resolved"}
    finally:
        service.close()


def test_an_unreadable_artifact_reports_unavailable(service_adapter):
    service = seeded_service(service_adapter)
    try:
        artifacts = service.store.layout.artifacts
        for path in sorted(artifacts.iterdir()):
            path.unlink()
        stored = reference_state(service)
        assert stored["resolution"]["status"] == "unavailable"
        assert stored["value"]["key"] == "S-42"
        # The startup contract reports the missing artifact
        # individually while the saved definitions stay readable.
        response = inspect(service, "inspect-after-loss")
        assert ("E_ARTIFACT", "/sources/supplier-records/versions/1/artifact") in [
            (item["code"], item["path"]) for item in response.result["diagnostics"]
        ]
    finally:
        service.close()


# ---------------------------------------------------------------------------
# Collection deletion and recreation (RFC 001 sections 3.2 and 3.3)
# ---------------------------------------------------------------------------


def test_a_referenced_collection_cannot_be_deleted(service_adapter):
    service = seeded_service(service_adapter)
    try:
        rejected = apply(
            service,
            "d1",
            {"collections/suppliers": 1},
            [
                {
                    "op": "entity.delete",
                    "collection": "collections",
                    "id": "suppliers",
                }
            ],
        )
        assert not rejected.ok
        assert ("E_REFERENCE", "/state/selected_supplier/type/collection") in codes(
            rejected
        )
    finally:
        service.close()


def test_the_same_transaction_may_remove_the_dependents(service_adapter):
    service = seeded_service(service_adapter)
    try:
        removed = apply(
            service,
            "d2",
            {"collections/suppliers": 1, "semantic-state/selected_supplier": 1},
            [
                {
                    "op": "entity.delete",
                    "collection": "state",
                    "id": "selected_supplier",
                },
                {
                    "op": "entity.delete",
                    "collection": "collections",
                    "id": "suppliers",
                },
            ],
        )
        assert removed.ok, removed.errors
        assert removed.result["changedRecords"] == [
            "collections/suppliers",
            "refstate/selected_supplier",
            "semantic-state/selected_supplier",
        ]
        view = semantic_view(service)
        assert view["collections"] == {}
        assert view["referenceStates"] == {}
    finally:
        service.close()


def test_recreation_issues_a_new_incarnation_the_old_reference_cannot_name(
    service_adapter,
):
    service = seeded_service(service_adapter)
    try:
        old_incarnation = incarnation_of(service)
        assert apply(
            service,
            "r1",
            {"collections/suppliers": 1, "semantic-state/selected_supplier": 1},
            [
                {
                    "op": "entity.delete",
                    "collection": "state",
                    "id": "selected_supplier",
                },
                {
                    "op": "entity.delete",
                    "collection": "collections",
                    "id": "suppliers",
                },
            ],
        ).ok
        assert create_collection(service, "04-recreate", 1, 0).ok
        new_incarnation = incarnation_of(service)
        assert new_incarnation != old_incarnation
        assert create_state(service, "06-state").ok
        # The old reference, with the old incarnation, can no longer
        # select anything in the recreated collection (RFC 001 3.2).
        rejected = select(service, "07-old", old_incarnation, "S-42", 1)
        assert not rejected.ok
        assert ("E_CONFLICT", "/operations/0/value") in codes(rejected)
        accepted = select(service, "08-new", new_incarnation, "S-42", 1)
        assert accepted.ok, accepted.errors
    finally:
        service.close()


# ---------------------------------------------------------------------------
# Recovery (RFC 001 sections 3.3, 4, and 9)
# ---------------------------------------------------------------------------


def test_restart_restores_incarnations_selections_and_resolution(
    service_adapter,
):
    service = seeded_service(service_adapter)
    try:
        incarnation = incarnation_of(service)
        stored = reference_state(service)
    finally:
        service.close()

    reopened = WorkspaceService.open(service_adapter, WORKSPACE)
    try:
        assert reopened.dispatch(
            envelope("h1", "session.hello",
                     payload={"protocolVersions": [1],
                              "languageVersions": ["melt/semantic-draft-1"],
                              "clientVersion": "test"})
        ).ok
        view = semantic_view(reopened)
        assert view["collections"]["suppliers"]["incarnation"] == incarnation
        assert view["referenceStates"]["selected_supplier"] == stored
        assert reopened.state.collection_incarnations == {
            "suppliers": incarnation
        }
        assert reopened.state.reference_states[
            "selected_supplier"
        ].value.key == "S-42"
    finally:
        reopened.close()


def test_restart_of_a_missing_selection_reports_missing(service_adapter):
    service = seeded_service(service_adapter)
    try:
        assert import_suppliers(service, "v2", SUPPLIERS_WITHOUT_S42, 1).ok
        assert rebind_collection(service, "03-rebind", 2, 1).ok
    finally:
        service.close()
    reopened = WorkspaceService.open(service_adapter, WORKSPACE)
    try:
        stored = reference_state(reopened)
        assert stored["resolution"]["status"] == "missing"
        assert stored["value"]["key"] == "S-42"
    finally:
        reopened.close()


def test_preview_reports_reference_state_changes(service_adapter):
    service = seeded_service(service_adapter)
    try:
        incarnation = incarnation_of(service)
        preview = service.dispatch(
            envelope(
                "pv1",
                "workspace.preview",
                expected={"refstate/selected_supplier": 2},
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
        changes = preview.result["referenceStateChanges"]
        assert changes["selected_supplier"]["value"]["key"] == "S-73"
        assert changes["selected_supplier"]["revision"] == 3
        assert preview.result["affectedRecords"] == ["refstate/selected_supplier"]
        # The dry run wrote nothing.
        assert reference_state(service)["value"]["key"] == "S-42"
    finally:
        service.close()


def test_inspection_collection_filters_bound_the_extension(service_adapter):
    service = seeded_service(service_adapter)
    try:
        collections_only = service.dispatch(
            envelope(
                "f1",
                "workspace.inspect",
                payload={"collection": "collections"},
            )
        )
        assert collections_only.ok
        assert sorted(collections_only.result["document"]) == [
            "collections",
            "language",
            "title",
            "workspaceId",
        ]
        state_only = service.dispatch(
            envelope("f2", "workspace.inspect", payload={"collection": "state"})
        )
        assert state_only.ok
        assert sorted(state_only.result["document"]["state"]) == [
            "selected_supplier"
        ]
        assert state_only.result["entityRevisions"] == {
            "semantic-state/selected_supplier": 1
        }
    finally:
        service.close()


# ---------------------------------------------------------------------------
# The v0.1 boundary (RFC 001 section 1)
# ---------------------------------------------------------------------------


def test_v01_workspaces_refuse_the_extension_operations(service_adapter):
    service = WorkspaceService.open(service_adapter, WORKSPACE)
    try:
        created = service.create_workspace(
            json.loads(json.dumps(V01_EMPTY)), "b1"
        )
        assert created.ok, created.errors
        rejected = apply(
            service,
            "b2",
            {"collections/suppliers": 0},
            [
                {
                    "op": "entity.create",
                    "collection": "collections",
                    "id": "suppliers",
                    "value": {
                        "source": {"sourceId": "supplier-records", "version": 1},
                        "key": "supplier_id",
                    },
                }
            ],
        )
        assert not rejected.ok
        assert ("E_VERSION", "/operations/0/collection") in codes(rejected)

        rejected = apply(
            service,
            "b3",
            {"semantic-state/selected_supplier": 0},
            [
                {
                    "op": "entity.create",
                    "collection": "state",
                    "id": "selected_supplier",
                    "value": {
                        "type": {"kind": "ref", "collection": "suppliers"},
                        "nullable": True,
                        "default": None,
                        "scope": "workspace",
                    },
                }
            ],
        )
        assert not rejected.ok
        assert ("E_VERSION", "/operations/0/collection") in codes(rejected)

        rejected = apply(
            service,
            "b4",
            {"refstate/selected_supplier": 0},
            [{"op": "state.set", "id": "selected_supplier", "value": None}],
        )
        assert not rejected.ok
        assert ("E_VERSION", "/operations/0/op") in codes(rejected)
        # Nothing was created and nothing was stored.
        assert service.entity_revisions == {
            "layout": 1,
        }
        assert service.state.reference_states == {}
    finally:
        service.close()


def test_v01_creation_rejects_a_document_with_extension_fields(service_adapter):
    service = WorkspaceService.open(service_adapter, WORKSPACE)
    try:
        document = json.loads(json.dumps(V01_EMPTY))
        document["collections"] = {}
        document["state"] = {}
        rejected = service.create_workspace(document, "b5")
        assert not rejected.ok
        assert ("E_SCHEMA", "/collections") in codes(rejected)
        assert ("E_SCHEMA", "/state") in codes(rejected)
    finally:
        service.close()


def test_v01_inspection_keeps_its_exact_shape(service_adapter):
    service = WorkspaceService.open(service_adapter, WORKSPACE)
    try:
        assert service.create_workspace(
            json.loads(json.dumps(V01_EMPTY)), "b6"
        ).ok
        response = inspect(service, "b7")
        assert response.ok
        assert "semantic" not in response.result
        assert sorted(response.result["document"]) == [
            "actions",
            "bindings",
            "language",
            "layout",
            "parameters",
            "projections",
            "sources",
            "title",
            "workspaceId",
        ]
    finally:
        service.close()


def test_hello_negotiates_the_workspace_language(service_adapter):
    service = seeded_service(service_adapter)
    try:
        speaking = service.dispatch(
            envelope(
                "h1",
                "session.hello",
                payload={
                    "protocolVersions": [1],
                    "languageVersions": ["melt/semantic-draft-1"],
                    "clientVersion": "test",
                },
            )
        )
        assert speaking.ok
        assert speaking.result["language"] == "melt/semantic-draft-1"

        silent = service.dispatch(
            envelope(
                "h2",
                "session.hello",
                payload={
                    "protocolVersions": [1],
                    "languageVersions": ["melt/v0.1"],
                    "clientVersion": "test",
                },
            )
        )
        assert not silent.ok
        assert ("E_VERSION", "/payload/languageVersions") in codes(silent)

        bilingual = service.dispatch(
            envelope(
                "h3",
                "session.hello",
                payload={
                    "protocolVersions": [1],
                    "languageVersions": ["melt/v0.1", "melt/semantic-draft-1"],
                    "clientVersion": "test",
                },
            )
        )
        assert bilingual.ok
        assert bilingual.result["language"] == "melt/semantic-draft-1"
    finally:
        service.close()
